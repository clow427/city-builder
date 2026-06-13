from shapely.geometry import LineString, Polygon

from pipeline import clearance as cl


def _building():
    return Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])


def test_in_any_polygon():
    assert cl.in_any_polygon((5, 5), [_building()])
    assert not cl.in_any_polygon((50, 50), [_building()])


def test_min_distance_to_lines():
    line = LineString([(0, 0), (0, 10)])   # the y-axis
    assert cl.min_distance_to_lines((3, 5), [line]) == 3.0
    assert cl.min_distance_to_lines((3, 5), []) is None


def test_relocation_inside_building_flagged():
    v = cl.validate_relocation("utility_pole", (5, 5), buildings=[_building()])
    assert any("building" in m for m in v)


def test_relocation_outside_building_clear():
    v = cl.validate_relocation("utility_pole", (50, 50), buildings=[_building()])
    assert v == []


def test_hydrant_curb_offset_violation_and_pass():
    curb = LineString([(0, 0), (0, 20)])
    too_close = cl.validate_relocation("hydrant", (0.2, 5), curbs=[curb])
    assert any("curb" in m for m in too_close)
    far_enough = cl.validate_relocation("hydrant", (2.0, 5), curbs=[curb])
    assert far_enough == []


def test_roi_bounds_violation():
    v = cl.validate_relocation("sign", (100, 100), roi_bounds=(0, 0, 80, 80))
    assert any("ROI" in m for m in v)


def test_override_curb_floor():
    curb = LineString([(0, 0), (0, 20)])
    # default sign floor is 0.3; at 0.4 m it's clear, but override to 1.0 flags it
    assert cl.validate_relocation("sign", (0.4, 5), curbs=[curb]) == []
    flagged = cl.validate_relocation("sign", (0.4, 5), curbs=[curb], min_curb_offset_m=1.0)
    assert flagged


def test_obstacles_from_dict():
    d = {"buildings": [[[0, 0], [10, 0], [10, 10], [0, 10]]],
         "curbs": [[[0, 0], [0, 20]]]}
    buildings, curbs = cl.obstacles_from_dict(d)
    assert len(buildings) == 1 and len(curbs) == 1
    assert cl.in_any_polygon((5, 5), buildings)
