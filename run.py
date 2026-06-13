"""End-to-end driver: one Cyvl LiDAR tile -> classified scene -> APS Viewer.

Real Cyvl tiles are ~100M points. Flow: crop to an ROI via PDAL (streaming,
memory-safe) -> SMRF ground filter -> voxel downsample -> road/sidewalk on
ground -> CARS via height-band detection (keep points 0.3-2.5 m above local
ground, cluster their footprint, keep car-shaped blobs; this is inherently
ground-contacting and strips buildings/walls/canopy) -> lift Cyvl 2D assets ->
shift to local origin -> push through APS. Run in the conda `hack` env.
"""
import glob
import json
import os
import numpy as np
import laspy
import pdal
from scipy.spatial import cKDTree
from sklearn.cluster import DBSCAN
import config
from pipeline.crop import las_crs
from pipeline.segment import split_ground, classify_ground3, load_road_lines, load_asset_lines, _obb
from pipeline.lift_assets import lift_assets
from pipeline.classify_io import write_colored_las
from pipeline.to_cad import write_obj, write_mtl, car_objects, asset_objects
from pipeline.ground_mesh import ground_grid_mesh
from aps.auth import get_token
from aps.upload import ensure_bucket, upload_object
from aps.translate import start_translation, wait_until_done

ROI_M = float(os.environ.get("ROI_M", 80.0))
VOXEL = 0.10
H_LO, H_HI = 0.3, 2.5          # car height band above local ground (m)


def voxel_downsample(p, v):
    keys = np.floor(p[:, :3] / v).astype(np.int64)
    _, idx = np.unique(keys, axis=0, return_index=True)
    return p[np.sort(idx)]


# Resolve ROI center first (env overrides tile default) so we can pick the
# tile whose bounding box actually contains the requested point.
_all_laz = sorted(glob.glob(config.LAZ_DIR + "/*.laz"))
if not _all_laz:
    raise FileNotFoundError(f"no .laz files in {config.LAZ_DIR!r}")

# Use the first tile to resolve a default center, then re-pick once we know cx/cy.
with laspy.open(_all_laz[0]) as _f:
    _h = _all_laz[0] and _f.header
    cx = (_h.mins[0] + _h.maxs[0]) / 2
    cy = (_h.mins[1] + _h.maxs[1]) / 2
cx = float(os.environ.get("ROI_CX", cx))
cy = float(os.environ.get("ROI_CY", cy))

# Pick the tile whose bounding box contains cx/cy; fall back to nearest centroid.
def _pick_tile(files, x, y):
    for f in files:
        with laspy.open(f) as lf:
            h = lf.header
            if h.mins[0] <= x <= h.maxs[0] and h.mins[1] <= y <= h.maxs[1]:
                return f
    def _dist(f):
        with laspy.open(f) as lf:
            h = lf.header
            return ((h.mins[0]+h.maxs[0])/2 - x)**2 + ((h.mins[1]+h.maxs[1])/2 - y)**2
    return min(files, key=_dist)

LAS = _pick_tile(_all_laz, cx, cy)
print(f"Using tile: {os.path.basename(LAS)}")
crs = las_crs(LAS)
block_id = os.environ.get("BLOCK_ID") or os.path.splitext(os.path.basename(LAS))[0]
OUT_DIR = os.path.join("out", block_id)
os.makedirs(OUT_DIR, exist_ok=True)
bbox_proj = (cx - ROI_M / 2, cy - ROI_M / 2, cx + ROI_M / 2, cy + ROI_M / 2)
print("ROI bbox_proj:", bbox_proj)

# PDAL pass 1: crop to ROI in streaming mode (readers.las + filters.crop are
# both streamable, so the full tile is never loaded into memory at once) and
# write the small cropped result to a temp file.
cropped = os.path.join(OUT_DIR, "_cropped.las")
crop_pl = pdal.Pipeline(json.dumps({"pipeline": [
    {"type": "readers.las", "filename": LAS},
    {"type": "filters.crop",
     "bounds": f"([{bbox_proj[0]},{bbox_proj[2]}],[{bbox_proj[1]},{bbox_proj[3]}])"},
    {"type": "writers.las", "filename": cropped},
]}))
crop_pl.execute_streaming(chunk_size=1_000_000)

# PDAL pass 2: SMRF ground classification on the (now small) cropped point set.
pl = pdal.Pipeline(json.dumps({"pipeline": [
    cropped,
    {"type": "filters.smrf"},
]}))
pl.execute()
arr = pl.arrays[0]
xyz = np.column_stack([arr["X"], arr["Y"], arr["Z"]]).astype(float)
gmask = arr["Classification"] == 2
print(f"ROI points: {len(xyz)}  ground: {int(gmask.sum())}")
ground, nonground = split_ground(xyz, gmask)

ground_ds = voxel_downsample(ground, VOXEL)
ng_ds = voxel_downsample(nonground, VOXEL)

# ground classes (vectorized): road from drive paths + pavement lines,
# sidewalk from Cyvl SIDEWALK/RAMP centerlines, everything else grass.
road_lines = (load_road_lines("data/pavements_v2.geojson", bbox_proj, 4326, crs)
              + load_road_lines("data/streetviewImagePaths_v2.geojson", bbox_proj, 4326, crs))
sw_lines = load_asset_lines("data/aboveGroundAssets_v2.geojson", bbox_proj, 4326, crs,
                            types=("SIDEWALK", "RAMP"))
labels = classify_ground3(ground_ds, road_lines, sw_lines, road_w=8.0, sw_w=3.6)
print(f"ground_ds: {len(ground_ds)}  road: {labels.count('road')}  "
      f"sidewalk: {labels.count('sidewalk')}  grass: {labels.count('grass')}")

# CARS: height band above local ground -> cluster footprint -> car-shaped boxes
gt = cKDTree(ground_ds[:, :2])
_, gi = gt.query(ng_ds[:, :2], k=1)
h_above = ng_ds[:, 2] - ground_ds[gi, 2]
band = ng_ds[(h_above > H_LO) & (h_above < H_HI)]
cars, car_pts = [], []
lbl = DBSCAN(eps=0.5, min_samples=12).fit_predict(band[:, :2]) if len(band) else []
for k in set(lbl):
    if k == -1:
        continue
    c = band[lbl == k]
    b = _obb(c)              # oriented box: real footprint dims + heading
    # width floor 0.8: street-side scans see only one flank of a car, so the
    # footprint can be half a car wide. Render at real car width regardless.
    if 2.0 <= b["length"] <= 7.0 and 0.8 <= b["width"] <= 3.0 and len(c) >= 150:
        b["width"] = max(b["width"], 1.7)
        cars.append(b)
        car_pts.append(c)
print(f"band points: {len(band)}  cars detected: {len(cars)}")

# lift Cyvl 2D CV assets (hydrants/manholes/signs) into 3D within ROI
assets = lift_assets(xyz, "data/aboveGroundAssets_v2.geojson", 4326, crs, bbox_proj)
print(f"assets lifted: {len(assets)}")

# buildings: OSM footprints extruded to point-measured heights
from pipeline.buildings import fetch_osm_buildings, building_objects
builds = fetch_osm_buildings(bbox_proj, crs)
bld_objs = building_objects(builds, xyz, ground_ds, bbox_proj, detail_points=nonground)
print(f"buildings: {len(builds)} footprints -> {len(bld_objs)} meshes")

# sanity-filter cars: must sit on/near the road, never inside a building
import shapely
from shapely.ops import unary_union
road_zone = unary_union([ln.buffer(6.0) for ln in road_lines])
bld_union = unary_union([b["poly"] for b in builds]) if builds else None
kept = []
for b in cars:
    x, y = b["center"][0], b["center"][1]
    if not road_zone.contains(shapely.Point(x, y)):
        continue
    if bld_union is not None and bld_union.contains(shapely.Point(x, y)):
        continue
    kept.append(b)
print(f"cars: {len(cars)} -> {len(kept)} after road/building filter")
cars = kept

# colored point cloud for Potree: ground (road/sidewalk) + car points (car)
all_pts = ground_ds
all_cls = list(labels)
if car_pts:
    cp = np.vstack(car_pts)
    all_pts = np.vstack([ground_ds, cp])
    all_cls = list(labels) + ["car"] * len(cp)
write_colored_las(os.path.join(OUT_DIR, "classified.laz"), all_pts, all_cls)

# CAD scene for APS Viewer — shift to local origin so coords are ~0..ROI_M
ox, oy = bbox_proj[0], bbox_proj[1]
for b in cars:
    b["center"] = [b["center"][0] - ox, b["center"][1] - oy, b["center"][2]]
for a in assets:
    a["x"], a["y"] = a["x"] - ox, a["y"] - oy
ground_shift = ground_ds.copy()
ground_shift[:, 0] -= ox
ground_shift[:, 1] -= oy

for o in bld_objs:
    o["verts"] = [(x - ox, y - oy, z) for x, y, z in o["verts"]]

# pavement condition: bin road cells by nearest Cyvl pavement segment score so
# the ground mesh emits selectable ground_road_good/fair/poor objects. Best
# effort — if the Cyvl layer can't be loaded, fall back to a single asphalt road.
CELL = 0.5
from pipeline import cyvl_source, pavement as pv
spec = pv.grid_spec((0, 0, ROI_M, ROI_M), CELL, origin=(ox, oy))
gx, gy = pv.cell_centers(spec)
road_bins = None
try:
    pav = cyvl_source.load_layer("pavements", crs, source="auto",
                                 local_dir="data", bbox_proj=bbox_proj)
    cond = pv.assign_condition(gx, gy, pav)
    road_bins = cond["bin"]
    binned = sum(1 for b in road_bins if b)
    print(f"pavement condition: {binned} cells matched a segment "
          f"(good {list(road_bins).count('good')}, fair {list(road_bins).count('fair')}, "
          f"poor {list(road_bins).count('poor')})")
    try:
        dist = cyvl_source.load_layer("distresses", crs, source="auto",
                                      local_dir="data", bbox_proj=bbox_proj)
        cen = dist.geometry.centroid
        dc = pv.count_in_cells(cen.x.values, cen.y.values, spec)
        print(f"distresses in ROI: {int(dc.sum())}")
    except Exception as e:
        print(f"distress density skipped: {e}")
except Exception as e:
    print(f"pavement condition skipped ({e}); roads render as plain asphalt")

ground_objs = ground_grid_mesh(ground_shift, labels, (0, 0, ROI_M, ROI_M),
                               cell=CELL, road_bins=road_bins)
objs = ground_objs + bld_objs + car_objects(cars) + asset_objects(assets)
write_mtl(os.path.join(OUT_DIR, "scene.mtl"))
write_obj(os.path.join(OUT_DIR, "scene.obj"), objs, mtl_filename="scene.mtl")
print(f"scene.obj objects: {len(objs)} (ground {len(ground_objs)} + cars {len(cars)} + assets {len(assets)})")

# static whole-block estimate: price bringing fair/poor road up to standard.
# Area per bin comes straight from the emitted road object's cell (face) count.
from pipeline.cost import estimate, load_catalog, cells_to_sqft
TREATMENT_FOR_BIN = {"poor": "full_depth_recon", "fair": "mill_and_overlay"}
edits = [{"op": "repave", "target": o["name"],
          "treatment": TREATMENT_FOR_BIN[o["name"].rsplit("_", 1)[1]],
          "area_sqft": cells_to_sqft(len(o["faces"]), CELL)}
         for o in ground_objs
         if o["name"].rsplit("_", 1)[1] in TREATMENT_FOR_BIN
         and o["name"].startswith("ground_road_")]
if edits:
    report = estimate(edits, load_catalog())
    report.write(os.path.join(OUT_DIR, "estimate.md"))
    report.write(os.path.join(OUT_DIR, "estimate.json"))
    print(f"baseline repair estimate: ${report.total:,.2f} "
          f"({len(edits)} road sections) -> out/estimate.md")

# Phase 2 artifacts: let the viewer map local<->UTM, know each draggable asset's
# authored position, and clearance-check relocations against buildings/curbs.
from pipeline.scene_export import asset_registry, scene_meta, obstacles
crs_str = f"EPSG:{crs.to_epsg()}" if crs.to_epsg() else crs.to_string()
json.dump(scene_meta(block_id, crs_str, (ox, oy), ROI_M, CELL, bbox_proj),
          open(os.path.join(OUT_DIR, "scene_meta.json"), "w"))
json.dump(asset_registry(objs, (ox, oy)), open(os.path.join(OUT_DIR, "assets.json"), "w"))
json.dump(obstacles([b["poly"] for b in builds], road_zone, bbox_proj, crs_str),
          open(os.path.join(OUT_DIR, "obstacles.json"), "w"))
print(f"wrote scene_meta/assets ({len(asset_registry(objs, (ox, oy)))} draggable)"
      f"/obstacles to {OUT_DIR}/")

# zip OBJ + MTL (Model Derivative needs a zip for multi-file inputs)
import zipfile
zip_path = os.path.join(OUT_DIR, "scene_cad.zip")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(os.path.join(OUT_DIR, "scene.obj"), "scene.obj")
    z.write(os.path.join(OUT_DIR, "scene.mtl"), "scene.mtl")

# APS: upload under a fresh key (avoids stale SVF2 cache) -> translate -> URN
tok = get_token()
bucket = os.environ.get("APS_BUCKET", f"cb-{config.APS_CLIENT_ID[:12].lower()}")
ensure_bucket(tok, bucket)
object_key = os.environ.get("SCENE_KEY", f"scene_{block_id}.zip")
urn = upload_object(tok, bucket, object_key, zip_path)
start_translation(tok, urn, root_filename="scene.obj")
wait_until_done(tok, urn)
print(f"\nScene '{block_id}' URN: {urn}")

# Auto-register in viewer/scenes.json so the dropdown picks it up immediately.
scenes_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer", "scenes.json")
scenes = json.load(open(scenes_path)) if os.path.exists(scenes_path) else []
label = os.environ.get("SCENE_LABEL", block_id.replace("_", " ").title())
entry = {"id": block_id, "label": label, "urn": urn, "data_dir": OUT_DIR}
scenes = [s for s in scenes if s.get("id") != block_id]  # replace if already exists
scenes.append(entry)
json.dump(scenes, open(scenes_path, "w"), indent=2)
print(f"Registered '{block_id}' in viewer/scenes.json ({len(scenes)} scenes total)")
