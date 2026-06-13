import json
import numpy as np


def split_ground(points, ground_mask):
    return points[ground_mask], points[~ground_mask]


def ground_mask_pdal(laz_path):
    """Run PDAL SMRF; return boolean ground mask aligned to file point order."""
    import pdal  # lazy: pdal is conda-only, not needed for unit tests
    p = pdal.Pipeline(json.dumps({"pipeline": [laz_path, {"type": "filters.smrf"}]}))
    p.execute()
    return p.arrays[0]["Classification"] == 2


import shapely
from shapely.ops import unary_union


def classify_road_sidewalk(ground_points, road_lines, width_m):
    """'road' if within width_m/2 of any road line (same CRS as points), else 'sidewalk'.

    Vectorized with shapely.contains_xy so it scales to millions of points.
    """
    road_poly = unary_union([ln.buffer(width_m / 2.0) for ln in road_lines])
    gp = np.asarray(ground_points)
    if len(gp) == 0:
        return []
    inside = shapely.contains_xy(road_poly, gp[:, 0], gp[:, 1])
    return ["road" if b else "sidewalk" for b in inside]


def load_road_lines(geojson_path, bbox_proj, src_epsg, dst_crs):
    import geopandas as gpd
    gdf = gpd.read_file(geojson_path).set_crs(src_epsg, allow_override=True).to_crs(dst_crs)
    if bbox_proj is not None:
        xmin, ymin, xmax, ymax = bbox_proj
        gdf = gdf.cx[xmin:xmax, ymin:ymax]
    return list(gdf.geometry)


def load_asset_lines(geojson_path, bbox_proj, src_epsg, dst_crs, types):
    """Geometries of given asset_type values (e.g. sidewalk centerlines)."""
    import geopandas as gpd
    gdf = gpd.read_file(geojson_path).set_crs(src_epsg, allow_override=True).to_crs(dst_crs)
    gdf = gdf[gdf["asset_type"].isin(types)]
    if bbox_proj is not None:
        xmin, ymin, xmax, ymax = bbox_proj
        gdf = gdf.cx[xmin:xmax, ymin:ymax]
    return [g for g in gdf.geometry if g is not None]


def classify_ground3(ground_points, road_lines, sidewalk_lines, road_w=8.0, sw_w=3.6):
    """Three-way ground labels: road > sidewalk > grass (vectorized)."""
    gp = np.asarray(ground_points)
    if len(gp) == 0:
        return []
    road_poly = unary_union([ln.buffer(road_w / 2.0) for ln in road_lines])
    in_road = shapely.contains_xy(road_poly, gp[:, 0], gp[:, 1])
    if sidewalk_lines:
        sw_poly = unary_union([g.buffer(sw_w / 2.0) for g in sidewalk_lines])
        in_sw = shapely.contains_xy(sw_poly, gp[:, 0], gp[:, 1])
    else:
        in_sw = np.zeros(len(gp), dtype=bool)
    return ["road" if r else ("sidewalk" if s else "grass")
            for r, s in zip(in_road, in_sw)]


from sklearn.cluster import DBSCAN


def cluster_objects(nonground_points, eps=0.5, min_samples=30):
    if len(nonground_points) == 0:
        return []
    labels = DBSCAN(eps=eps, min_samples=min_samples).fit_predict(nonground_points)
    return [nonground_points[labels == k] for k in set(labels) if k != -1]


def _aabb(cluster):
    mins, maxs = cluster.min(axis=0), cluster.max(axis=0)
    size = maxs - mins
    dims = sorted([size[0], size[1]])
    return {"center": ((mins + maxs) / 2).tolist(),
            "width": float(dims[0]), "length": float(dims[1]),
            "height": float(size[2]), "min": mins.tolist(), "max": maxs.tolist()}


def _obb(cluster):
    """Oriented (PCA-aligned) bounding box of a cluster footprint, with yaw."""
    xy = cluster[:, :2]
    mean = xy.mean(axis=0)
    c = xy - mean
    cov = np.cov(c.T)
    _, vecs = np.linalg.eigh(cov)
    ax = vecs[:, -1]
    yaw = float(np.arctan2(ax[1], ax[0]))
    rot = np.array([[np.cos(-yaw), -np.sin(-yaw)], [np.sin(-yaw), np.cos(-yaw)]])
    local = c @ rot.T
    mins, maxs = local.min(axis=0), local.max(axis=0)
    back = np.array([[np.cos(yaw), -np.sin(yaw)], [np.sin(yaw), np.cos(yaw)]])
    cen = mean + back @ ((mins + maxs) / 2)
    zmin, zmax = float(cluster[:, 2].min()), float(cluster[:, 2].max())
    return {"center": [float(cen[0]), float(cen[1]), (zmin + zmax) / 2],
            "length": float(maxs[0] - mins[0]), "width": float(maxs[1] - mins[1]),
            "height": zmax - zmin, "yaw": yaw}


def keep_car_clusters(clusters, length=(2.0, 6.0), width=(1.4, 2.2), height=(1.2, 2.2)):
    cars = []
    for c in clusters:
        b = _aabb(c)
        if (length[0] <= b["length"] <= length[1] and
                width[0] <= b["width"] <= width[1] and
                height[0] <= b["height"] <= height[1]):
            cars.append(b)
    return cars
