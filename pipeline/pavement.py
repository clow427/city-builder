"""Per-cell pavement condition for the ground heightfield.

Joins the ground-mesh grid against Cyvl's `pavements` layer (PCI-style `score`
0-100, higher = better; plus a `label` of Good/Fair/Poor) and `distresses`
layer so each road cell can be binned and recolored green->red.

Everything works in a single projected working CRS (meters). The caller is
responsible for reprojecting the Cyvl layers into that CRS first
(see pipeline.cyvl_source) — these functions assume the GeoDataFrames and the
cell coordinates already share it.

Grid convention matches pipeline.ground_mesh: a `bbox` of (xmin, ymin, xmax,
ymax) with square `cell`, flattened as `ix * ny + iy`. An `origin` (ox, oy)
shifts the local grid back into world coordinates for the spatial join.
"""
from __future__ import annotations

import numpy as np

CONDITION_ORDER = ["good", "fair", "poor"]
DEFAULT_GOOD_MIN = 70.0   # score >= this -> good
DEFAULT_FAIR_MIN = 40.0   # score >= this -> fair, else poor
DEFAULT_MAX_JOIN_M = 15.0  # cell center must be this close to a segment

# Cyvl `label` text -> our three bins (lower-cased lookup).
_LABEL_BINS = {
    "excellent": "good", "good": "good", "satisfactory": "good",
    "fair": "fair", "moderate": "fair",
    "poor": "poor", "very poor": "poor", "serious": "poor",
    "failed": "poor", "critical": "poor",
}


def condition_bin(score, good_min: float = DEFAULT_GOOD_MIN,
                  fair_min: float = DEFAULT_FAIR_MIN) -> str | None:
    """Bin a numeric pavement score (0-100, higher better) into good/fair/poor."""
    if score is None or (isinstance(score, float) and np.isnan(score)):
        return None
    if score >= good_min:
        return "good"
    if score >= fair_min:
        return "fair"
    return "poor"


def label_bin(label) -> str | None:
    """Map a Cyvl condition `label` string to our bin, or None if unknown."""
    if label is None or (isinstance(label, float) and np.isnan(label)):
        return None
    return _LABEL_BINS.get(str(label).strip().lower())


# ----------------------------------------------------------------- grid helpers

def grid_spec(bbox, cell: float, origin=(0.0, 0.0)):
    """World-space grid spec: (x0, y0, cell, nx, ny) for the lower-left corner.

    `origin` shifts the (local) bbox into world coordinates, so a local bbox of
    (0, 0, ROI, ROI) with origin=(ox, oy) lands the grid at the UTM tile corner.
    """
    xmin, ymin, xmax, ymax = bbox
    nx = max(int(np.ceil((xmax - xmin) / cell)), 1)
    ny = max(int(np.ceil((ymax - ymin) / cell)), 1)
    return origin[0] + xmin, origin[1] + ymin, float(cell), nx, ny


def cell_centers(spec):
    """(cx, cy) world coordinates of each cell center, flattened ix*ny+iy."""
    x0, y0, cell, nx, ny = spec
    ci, cj = np.divmod(np.arange(nx * ny), ny)
    return x0 + (ci + 0.5) * cell, y0 + (cj + 0.5) * cell


def count_in_cells(px, py, spec) -> np.ndarray:
    """Count points (px, py) falling in each grid cell; returns (nx*ny,) int."""
    x0, y0, cell, nx, ny = spec
    px, py = np.asarray(px, float), np.asarray(py, float)
    counts = np.zeros(nx * ny, dtype=int)
    if len(px) == 0:
        return counts
    ix = ((px - x0) / cell).astype(int)
    iy = ((py - y0) / cell).astype(int)
    inside = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
    np.add.at(counts, ix[inside] * ny + iy[inside], 1)
    return counts


# ----------------------------------------------------------------- spatial join

def assign_condition(cx, cy, pavements_gdf, *, max_dist_m: float = DEFAULT_MAX_JOIN_M,
                     good_min: float = DEFAULT_GOOD_MIN,
                     fair_min: float = DEFAULT_FAIR_MIN) -> dict:
    """Nearest-pavement condition for each (cx, cy) cell center.

    Args:
        cx, cy: cell-center coordinates in the pavements' CRS.
        pavements_gdf: the `pavements` layer (LineStrings) in that same CRS,
            with `score` and/or `label` columns.
        max_dist_m: cells farther than this from any segment get no match.

    Returns:
        dict of equal-length arrays keyed by cell:
            score (float, NaN if unmatched), label (object), bin (object,
            None if unmatched), seg_id (object), dist_m (float).
    """
    import geopandas as gpd

    cx, cy = np.asarray(cx, float), np.asarray(cy, float)
    n = len(cx)
    out = {
        "score": np.full(n, np.nan),
        "label": np.full(n, None, dtype=object),
        "bin": np.full(n, None, dtype=object),
        "seg_id": np.full(n, None, dtype=object),
        "dist_m": np.full(n, np.nan),
    }
    if n == 0 or pavements_gdf is None or len(pavements_gdf) == 0:
        return out

    pts = gpd.GeoDataFrame(
        {"_i": np.arange(n)},
        geometry=gpd.points_from_xy(cx, cy),
        crs=pavements_gdf.crs,
    )
    cols = ["geometry"]
    for c in ("score", "label", "client_seg_id"):
        if c in pavements_gdf.columns:
            cols.append(c)
    right = pavements_gdf[cols].reset_index(drop=True)

    joined = gpd.sjoin_nearest(pts, right, how="left", max_distance=max_dist_m,
                               distance_col="dist_m")
    # ties can duplicate a left row; keep the first (nearest) match per cell
    joined = joined[~joined["_i"].duplicated(keep="first")].sort_values("_i")
    idx = joined["_i"].to_numpy()

    if "score" in joined:
        out["score"][idx] = joined["score"].to_numpy(dtype=float, na_value=np.nan)
    if "label" in joined:
        out["label"][idx] = joined["label"].to_numpy(dtype=object)
    if "client_seg_id" in joined:
        out["seg_id"][idx] = joined["client_seg_id"].to_numpy(dtype=object)
    out["dist_m"][idx] = joined["dist_m"].to_numpy(dtype=float, na_value=np.nan)

    # bin: prefer the dataset label, fall back to the numeric score
    for i in range(n):
        b = label_bin(out["label"][i])
        if b is None:
            b = condition_bin(out["score"][i], good_min, fair_min)
        out["bin"][i] = b
    return out
