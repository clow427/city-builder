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


from shapely.geometry import Point
from shapely.ops import unary_union


def classify_road_sidewalk(ground_points, road_lines, width_m):
    """'road' if within width_m/2 of any road line (same CRS as points), else 'sidewalk'."""
    road_poly = unary_union([ln.buffer(width_m / 2.0) for ln in road_lines])
    return ["road" if road_poly.contains(Point(x, y)) else "sidewalk"
            for x, y, _ in ground_points]


def load_road_lines(geojson_path, bbox_proj, src_epsg, dst_crs):
    import geopandas as gpd
    gdf = gpd.read_file(geojson_path).set_crs(src_epsg, allow_override=True).to_crs(dst_crs)
    if bbox_proj is not None:
        xmin, ymin, xmax, ymax = bbox_proj
        gdf = gdf.cx[xmin:xmax, ymin:ymax]
    return list(gdf.geometry)


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


def keep_car_clusters(clusters, length=(2.0, 6.0), width=(1.4, 2.2), height=(1.2, 2.2)):
    cars = []
    for c in clusters:
        b = _aabb(c)
        if (length[0] <= b["length"] <= length[1] and
                width[0] <= b["width"] <= width[1] and
                height[0] <= b["height"] <= height[1]):
            cars.append(b)
    return cars
