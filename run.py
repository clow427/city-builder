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


LAS = glob.glob(config.LAZ_DIR + "/*.laz")[0]
crs = las_crs(LAS)

with laspy.open(LAS) as f:
    h = f.header
    cx = (h.mins[0] + h.maxs[0]) / 2
    cy = (h.mins[1] + h.maxs[1]) / 2
# optional ROI center override (UTM) to focus on an asset-dense spot
cx = float(os.environ.get("ROI_CX", cx))
cy = float(os.environ.get("ROI_CY", cy))
bbox_proj = (cx - ROI_M / 2, cy - ROI_M / 2, cx + ROI_M / 2, cy + ROI_M / 2)
print("ROI bbox_proj:", bbox_proj)

# PDAL: stream + crop to ROI, then SMRF ground classification
pl = pdal.Pipeline(json.dumps({"pipeline": [
    LAS,
    {"type": "filters.crop",
     "bounds": f"([{bbox_proj[0]},{bbox_proj[2]}],[{bbox_proj[1]},{bbox_proj[3]}])"},
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
lbl = DBSCAN(eps=0.5, min_samples=12).fit_predict(band[:, :2])
cars, car_pts = [], []
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
write_colored_las("out/classified.laz", all_pts, all_cls)

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

ground_objs = ground_grid_mesh(ground_shift, labels, (0, 0, ROI_M, ROI_M), cell=0.5)
objs = ground_objs + bld_objs + car_objects(cars) + asset_objects(assets)
write_mtl("out/scene.mtl")
write_obj("out/scene.obj", objs, mtl_filename="scene.mtl")
print(f"scene.obj objects: {len(objs)} (ground {len(ground_objs)} + cars {len(cars)} + assets {len(assets)})")

# zip OBJ + MTL (Model Derivative needs a zip for multi-file inputs)
import zipfile
with zipfile.ZipFile("out/scene_cad.zip", "w", zipfile.ZIP_DEFLATED) as z:
    z.write("out/scene.obj", "scene.obj")
    z.write("out/scene.mtl", "scene.mtl")

# APS: upload under a fresh key (avoids stale SVF2 cache) -> translate -> URN
tok = get_token()
bucket = "cyvl-hack-xavier"
ensure_bucket(tok, bucket)
object_key = os.environ.get("SCENE_KEY", "scene_cad_v1.zip")
urn = upload_object(tok, bucket, object_key, "out/scene_cad.zip")
start_translation(tok, urn, root_filename="scene.obj")
wait_until_done(tok, urn)
print("\nPASTE THIS URN INTO viewer/index.html:\n", urn)
