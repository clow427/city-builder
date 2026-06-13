"""Serialize scene metadata for the interactive viewer (Phase 2).

run.py builds the CAD scene in local (origin-shifted) coordinates; the scenario
and clearance checks live in world UTM. These helpers bridge the two:

  * scene_meta   — origin/crs/roi so the viewer can map local <-> UTM
  * asset_registry — each draggable object's authored UTM position + type
  * obstacles    — building footprints + curb lines (UTM) for clearance checks
"""
from __future__ import annotations

import re

_IDX = re.compile(r"_\d+$")


def asset_registry(objs, origin) -> list[dict]:
    """{name, type, utm} for every object carrying a drag `anchor` (local)."""
    ox, oy = origin
    out = []
    for o in objs:
        a = o.get("anchor")
        if not a:
            continue
        out.append({"name": o["name"],
                    "type": _IDX.sub("", o["name"]).lower(),
                    "utm": [a[0] + ox, a[1] + oy, a[2]]})
    return out


def scene_meta(block_id, crs_str, origin, roi_m, cell, bbox_proj) -> dict:
    return {"block_id": block_id, "crs": crs_str,
            "origin": [float(origin[0]), float(origin[1])],
            "roi_m": float(roi_m), "cell": float(cell),
            "bbox_proj": [float(v) for v in bbox_proj]}


def _line_coords(geom) -> list:
    """Flatten any line/polygon-boundary geometry to lists of [x, y] rings."""
    if geom is None or geom.is_empty:
        return []
    gt = geom.geom_type
    if gt == "LineString":
        return [[[x, y] for x, y in geom.coords]]
    if gt == "Polygon":
        return _line_coords(geom.boundary)
    if gt in ("MultiLineString", "MultiPolygon", "GeometryCollection"):
        res = []
        for g in geom.geoms:
            res += _line_coords(g if gt != "MultiPolygon" else g.boundary)
        return res
    return []


def obstacles(buildings, curb_geom, bbox_proj, crs_str) -> dict:
    """Clearance obstacles in UTM: building exterior rings + curb lines."""
    rings = [[[x, y] for x, y in poly.exterior.coords]
             for poly in buildings if poly is not None and not poly.is_empty]
    return {"crs": crs_str,
            "buildings": rings,
            "curbs": _line_coords(curb_geom),
            "roi_bounds": [float(v) for v in bbox_proj]}
