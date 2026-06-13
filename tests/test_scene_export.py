from shapely.geometry import MultiPolygon, Polygon

from pipeline import clearance as cl
from pipeline.scene_export import asset_registry, obstacles, scene_meta


def test_asset_registry_local_to_utm_and_type():
    objs = [
        {"name": "ground_road", "faces": []},                       # no anchor
        {"name": "utility_pole_03", "anchor": [10.0, 20.0, 1.0]},
        {"name": "hydrant_01", "anchor": [5.0, 5.0, 0.5]},
    ]
    reg = asset_registry(objs, origin=(1000.0, 2000.0))
    names = {r["name"]: r for r in reg}
    assert "ground_road" not in names           # only anchored objects
    assert names["utility_pole_03"]["type"] == "utility_pole"
    assert names["utility_pole_03"]["utm"] == [1010.0, 2020.0, 1.0]
    assert names["hydrant_01"]["utm"] == [1005.0, 2005.0, 0.5]


def test_scene_meta_shape():
    m = scene_meta("davis_sq_a", "EPSG:26919", (1000, 2000), 80, 0.5, (1000, 2000, 1080, 2080))
    assert m["block_id"] == "davis_sq_a"
    assert m["origin"] == [1000.0, 2000.0]
    assert m["bbox_proj"] == [1000.0, 2000.0, 1080.0, 2080.0]


def test_obstacles_roundtrip_through_clearance():
    buildings = [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])]
    # a curb as a polygon boundary (road zone) -> its ring becomes curb lines
    road_zone = MultiPolygon([Polygon([(20, 0), (40, 0), (40, 40), (20, 40)])])
    d = obstacles(buildings, road_zone, (0, 0, 80, 80), "EPSG:26919")
    assert len(d["buildings"]) == 1
    assert len(d["curbs"]) >= 1
    # feed straight back into the clearance loader the endpoint uses
    bs, cs = cl.obstacles_from_dict(d)
    assert cl.in_any_polygon((5, 5), bs)
    assert cl.validate_relocation("hydrant", (5, 5), buildings=bs)  # inside -> flagged
