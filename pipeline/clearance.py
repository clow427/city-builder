"""Clearance checks for asset relocation (Phase 2).

Pure shapely predicates over obstacle geometry. All coordinates share the
scenario's working CRS (UTM, meters) so distances are real. `validate_relocation`
returns a list of human-readable violation strings (empty = clear); it is
advisory — the viewer surfaces warnings but does not block the move.
"""
from __future__ import annotations

from shapely.geometry import LineString, Point, shape

# Minimum offset from a curb line, meters, by asset type (lower-cased).
MIN_CURB_OFFSET_M = {
    "hydrant": 0.5,
    "utility_pole": 0.6,
    "traffic_signal_pole": 0.6,
    "luminaries": 0.6,
    "default": 0.3,
}


def in_any_polygon(xy, polygons) -> bool:
    """True if (x, y) is inside any of the given shapely polygons."""
    p = Point(xy[0], xy[1])
    return any(poly.contains(p) for poly in polygons)


def min_distance_to_lines(xy, lines) -> float | None:
    """Nearest distance from (x, y) to any line, or None if no lines."""
    if not lines:
        return None
    p = Point(xy[0], xy[1])
    return min(p.distance(ln) for ln in lines)


def _curb_floor(asset_type, override) -> float:
    if override is not None:
        return float(override)
    return MIN_CURB_OFFSET_M.get((asset_type or "default").lower(),
                                 MIN_CURB_OFFSET_M["default"])


def validate_relocation(asset_type, xy, *, buildings=None, curbs=None,
                        roi_bounds=None, min_curb_offset_m=None) -> list[str]:
    """Check a proposed relocation destination; return violation messages.

    Args:
        asset_type: e.g. "hydrant", "utility_pole" (case-insensitive).
        xy: destination (x, y) in the working CRS.
        buildings: iterable of shapely Polygons (footprints).
        curbs: iterable of shapely LineStrings (curb / road edges).
        roi_bounds: optional (xmin, ymin, xmax, ymax) block extent.
        min_curb_offset_m: override the per-type curb floor (meters).

    Returns:
        List of violation strings (empty when the destination is clear).
    """
    violations: list[str] = []
    at = (asset_type or "asset").lower()

    if buildings and in_any_polygon(xy, buildings):
        violations.append(f"{at} would sit inside a building footprint")

    if roi_bounds is not None:
        xmin, ymin, xmax, ymax = roi_bounds
        if not (xmin <= xy[0] <= xmax and ymin <= xy[1] <= ymax):
            violations.append(f"{at} would sit outside the block ROI")

    if curbs:
        floor = _curb_floor(asset_type, min_curb_offset_m)
        d = min_distance_to_lines(xy, curbs)
        if d is not None and d < floor:
            violations.append(
                f"{at} is {d:.2f} m from a curb (needs >= {floor:.2f} m)")

    return violations


# -------------------------------------------------------------- obstacle loading

def obstacles_from_dict(d: dict):
    """Build (buildings, curbs) shapely lists from an obstacles.json dict.

    Format: {"buildings": [[[x,y],...ring], ...], "curbs": [[[x,y],...], ...]}.
    """
    buildings = [shape({"type": "Polygon", "coordinates": [ring]})
                 for ring in (d.get("buildings") or [])]
    curbs = [LineString(coords) for coords in (d.get("curbs") or [])
             if len(coords) >= 2]
    return buildings, curbs
