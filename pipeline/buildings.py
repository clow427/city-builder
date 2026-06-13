"""OSM building footprints + point cloud -> realistic 3D building meshes.

Footprints come from the Overpass API (lat/lon), projected to the tile CRS and
clipped to the ROI. Where the scan covers a building densely, the roof is a
0.5m heightfield of the building's own non-ground points (real roof shape:
gables, dormers, stepped massing), with vertical walls dropping to ground
around the footprint. Sparse buildings fall back to a flat extrusion at
tag/default height.
"""
import json
import urllib.request
import numpy as np
import shapely
from shapely.geometry import Polygon
from pyproj import Transformer
from scipy.spatial import cKDTree

MIN_PTS_HEIGHTFIELD = 1500
CELL = 0.5


def fetch_osm_buildings(bbox_proj, dst_crs):
    import os
    t = Transformer.from_crs(dst_crs, 4326, always_xy=True)
    xmin, ymin, xmax, ymax = bbox_proj
    (w, s), (e, n) = t.transform(xmin, ymin), t.transform(xmax, ymax)
    cache = f"data/osm_{s:.5f}_{w:.5f}_{n:.5f}_{e:.5f}.json"
    if os.path.exists(cache):
        data = json.load(open(cache))
    else:
        q = f'[out:json][timeout:30];way["building"]({s},{w},{n},{e});out geom;'
        data = None
        for host in ("https://overpass-api.de/api/interpreter",
                     "https://overpass.kumi.systems/api/interpreter"):
            try:
                req = urllib.request.Request(host, data=q.encode(),
                                             headers={"User-Agent": "cyvl-hackathon-demo"})
                data = json.load(urllib.request.urlopen(req, timeout=60))
                break
            except Exception as exc:
                print(f"overpass {host} failed: {exc}")
        if data is None:
            raise RuntimeError("all Overpass mirrors failed")
        json.dump(data, open(cache, "w"))
    back = Transformer.from_crs(4326, dst_crs, always_xy=True)
    out = []
    for el in data.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        ring = [back.transform(p["lon"], p["lat"]) for p in el["geometry"]]
        if len(ring) < 4:
            continue
        poly = Polygon(ring)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            continue
        out.append({"poly": poly, "tags": el.get("tags", {})})
    return out


def _tag_height(tags):
    h = tags.get("height")
    if h:
        try:
            return float(str(h).lower().replace("m", "").strip())
        except ValueError:
            pass
    lv = tags.get("building:levels")
    if lv:
        try:
            return float(lv) * 3.0
        except ValueError:
            pass
    return None


def _flat_building(i, poly, gz, h):
    """Extruded footprint with flat roof (fallback for sparse scans)."""
    ring = list(poly.exterior.coords)[:-1]
    n = len(ring)
    verts = []
    for x, y in ring:
        verts.append((x, y, gz))
        verts.append((x, y, gz + h))
    faces = [(2 * j, 2 * ((j + 1) % n), 2 * ((j + 1) % n) + 1, 2 * j + 1)
             for j in range(n)]
    objs = [{"name": f"building_{i:02d}", "material": "wall",
             "verts": verts, "faces": faces}]
    rverts, rfaces = [], []
    vmap = {}
    for tri in shapely.delaunay_triangles(poly).geoms:
        if not poly.contains(tri.centroid):
            continue
        idx = []
        for x, y in list(tri.exterior.coords)[:-1]:
            key = (round(x, 3), round(y, 3))
            if key not in vmap:
                vmap[key] = len(rverts)
                rverts.append((x, y, gz + h))
            idx.append(vmap[key])
        rfaces.append(tuple(idx))
    if rfaces:
        objs.append({"name": f"building_{i:02d}_roof", "material": "roof",
                     "verts": rverts, "faces": rfaces})
    return objs


def _heightfield_building(i, poly, pts, gz, cell=CELL):
    """Roof as a per-cell heightfield of the building's own points + skirt walls."""
    xmin, ymin, xmax, ymax = poly.bounds
    nx = max(int(np.ceil((xmax - xmin) / cell)), 1)
    ny = max(int(np.ceil((ymax - ymin) / cell)), 1)

    ix = np.clip(((pts[:, 0] - xmin) / cell).astype(int), 0, nx - 1)
    iy = np.clip(((pts[:, 1] - ymin) / cell).astype(int), 0, ny - 1)
    flat = ix * ny + iy
    zsum = np.zeros(nx * ny)
    cnt = np.zeros(nx * ny)
    np.add.at(zsum, flat, pts[:, 2])
    np.add.at(cnt, flat, 1)

    ci, cj = np.divmod(np.arange(nx * ny), ny)
    centers_x = xmin + (ci + 0.5) * cell
    centers_y = ymin + (cj + 0.5) * cell
    inside = shapely.contains_xy(poly.buffer(0.15), centers_x, centers_y)

    zcell = np.full(nx * ny, np.nan)
    has = cnt > 0
    zcell[has] = zsum[has] / cnt[has]
    # clamp: roofs live between 2.5m above ground and the building's p98
    zhi = float(np.percentile(pts[:, 2], 98))
    zcell = np.clip(zcell, gz + 2.5, zhi)

    fill_src = inside & has
    if fill_src.sum() < 10:
        return None
    need = inside & ~has
    if need.any():
        tree = cKDTree(np.column_stack([centers_x[fill_src], centers_y[fill_src]]))
        _, nn = tree.query(np.column_stack([centers_x[need], centers_y[need]]))
        zcell[need] = zcell[fill_src][nn]

    zg = zcell.reshape(nx, ny)
    ing = inside.reshape(nx, ny)
    # 3x3 mean smoothing over inside cells (kills single-cell tree spikes)
    zs = np.zeros((nx, ny))
    ws = np.zeros((nx, ny))
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            sx = slice(max(dx, 0), nx + min(dx, 0))
            tx = slice(max(-dx, 0), nx + min(-dx, 0))
            sy = slice(max(dy, 0), ny + min(dy, 0))
            ty = slice(max(-dy, 0), ny + min(-dy, 0))
            valid = ing[sx, sy] & ~np.isnan(zg[sx, sy])
            zs[tx, ty] += np.where(valid, zg[sx, sy], 0)
            ws[tx, ty] += valid
    with np.errstate(invalid="ignore"):
        zg = np.where(ws > 0, zs / np.maximum(ws, 1), np.nan)

    # vertex heights = mean of adjacent inside cells
    vz = np.zeros((nx + 1, ny + 1))
    vw = np.zeros((nx + 1, ny + 1))
    for dx in (0, 1):
        for dy in (0, 1):
            valid = ing & ~np.isnan(zg)
            vz[dx:nx + dx, dy:ny + dy] += np.where(valid, zg, 0)
            vw[dx:nx + dx, dy:ny + dy] += valid
    vz = np.where(vw > 0, vz / np.maximum(vw, 1), gz)

    roof_v, roof_f, vmap = [], [], {}

    def vid(a, b):
        if (a, b) not in vmap:
            vmap[(a, b)] = len(roof_v)
            roof_v.append((xmin + a * cell, ymin + b * cell, vz[a, b]))
        return vmap[(a, b)]

    wall_v, wall_f = [], []

    def wall_quad(a1, b1, a2, b2):
        x1, y1, z1 = xmin + a1 * cell, ymin + b1 * cell, vz[a1, b1]
        x2, y2, z2 = xmin + a2 * cell, ymin + b2 * cell, vz[a2, b2]
        k = len(wall_v)
        wall_v.extend([(x1, y1, gz), (x2, y2, gz), (x2, y2, z2), (x1, y1, z1)])
        wall_f.append((k, k + 1, k + 2, k + 3))

    occupied = ing & ~np.isnan(zg)
    for a in range(nx):
        for b in range(ny):
            if not occupied[a, b]:
                continue
            roof_f.append((vid(a, b), vid(a + 1, b), vid(a + 1, b + 1), vid(a, b + 1)))
            if a == 0 or not occupied[a - 1, b]:
                wall_quad(a, b, a, b + 1)
            if a == nx - 1 or not occupied[a + 1, b]:
                wall_quad(a + 1, b, a + 1, b + 1)
            if b == 0 or not occupied[a, b - 1]:
                wall_quad(a, b, a + 1, b)
            if b == ny - 1 or not occupied[a, b + 1]:
                wall_quad(a, b + 1, a + 1, b + 1)

    return [{"name": f"building_{i:02d}_roof", "material": "roof",
             "verts": roof_v, "faces": roof_f},
            {"name": f"building_{i:02d}", "material": "wall",
             "verts": wall_v, "faces": wall_f}]


def building_objects(builds, all_points, ground_pts, bbox_proj, detail_points=None):
    """detail_points: non-ground points (walls/roofs); defaults to all_points."""
    if detail_points is None:
        detail_points = all_points
    objs = []
    roi = shapely.box(*bbox_proj)
    gtree = cKDTree(ground_pts[:, :2])
    for i, b in enumerate(builds, 1):
        poly = b["poly"].intersection(roi)
        if poly.is_empty or poly.area < 4:
            continue
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.geom_type != "Polygon":
            continue
        _, gi = gtree.query([poly.centroid.x, poly.centroid.y])
        gz = float(ground_pts[gi, 2])

        inside = shapely.contains_xy(poly, detail_points[:, 0], detail_points[:, 1])
        pts = detail_points[inside]
        made = None
        if len(pts) >= MIN_PTS_HEIGHTFIELD:
            made = _heightfield_building(i, poly, pts, gz)
        if made is None:
            h = _tag_height(b["tags"])
            if len(pts) > 200:
                h = float(np.percentile(pts[:, 2], 98)) - gz
            h = max(h or 6.0, 3.0)
            made = _flat_building(i, poly, gz, h)
        objs.extend(made)
    return objs
