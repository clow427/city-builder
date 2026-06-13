import numpy as np
import geopandas as gpd
from scipy.spatial import cKDTree


def cylinder_indices(points, center_xy, radius):
    dx = points[:, 0] - center_xy[0]
    dy = points[:, 1] - center_xy[1]
    return np.nonzero(dx * dx + dy * dy <= radius * radius)[0]


def z_extent(z_values):
    gz = float(np.percentile(z_values, 2))
    tz = float(np.percentile(z_values, 98))
    return gz, tz, tz - gz


def lift_assets(points, assets_geojson, src_epsg, dst_crs, bbox_proj, radius=0.8):
    gdf = gpd.read_file(assets_geojson).set_crs(src_epsg, allow_override=True).to_crs(dst_crs)
    if bbox_proj is not None:
        xmin, ymin, xmax, ymax = bbox_proj
        gdf = gdf.cx[xmin:xmax, ymin:ymax]
    tree = cKDTree(points[:, :2])
    out = []
    for _, row in gdf.iterrows():
        if row.geometry is None:
            continue
        c = row.geometry.centroid       # robust to Point/LineString/Polygon assets
        cx, cy = c.x, c.y
        near = tree.query_ball_point([cx, cy], radius)
        if not near:
            continue
        gz, tz, h = z_extent(points[near, 2])
        out.append({"type": row.get("asset_type") or row.get("Type") or "asset",
                    "x": float(cx), "y": float(cy),
                    "ground_z": gz, "top_z": tz, "height": h})
    return out
