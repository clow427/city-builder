import glob
import os
import numpy as np
import laspy
from config import LAZ_DIR


def load_points(laz_dir=None):
    laz_dir = laz_dir or LAZ_DIR
    files = sorted(glob.glob(os.path.join(laz_dir, "*.laz")))
    if not files:
        raise FileNotFoundError(f"no .laz in {laz_dir!r}; set LAZ_DIR in .env")
    xs, rgbs = [], []
    for f in files:
        las = laspy.read(f)
        xs.append(np.column_stack([las.x, las.y, las.z]))
        if hasattr(las, "red"):
            rgbs.append(np.column_stack([las.red, las.green, las.blue]))
    pts = np.vstack(xs)
    rgb = np.vstack(rgbs) if rgbs else np.zeros_like(pts)
    return pts, rgb, files[0]


def crop_xy(points, bbox_proj):
    xmin, ymin, xmax, ymax = bbox_proj
    m = (points[:, 0] >= xmin) & (points[:, 0] <= xmax) & \
        (points[:, 1] >= ymin) & (points[:, 1] <= ymax)
    return points[m]


def las_crs(laz_path):
    with laspy.open(laz_path) as f:
        crs = f.header.parse_crs()
    if crs is None:
        raise ValueError("LAS has no CRS; run `pdal info <file>` to find it and hardcode")
    return crs
