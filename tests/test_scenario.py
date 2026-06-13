import json

from pipeline.scenario import (
    Scenario,
    add_ramp_edit,
    add_road_edit,
    relocate_edit,
)


def test_add_undo_clear():
    s = Scenario(block_id="davis_sq_a", crs="EPSG:26919")
    s.add_edit({"op": "add_ramp", "at_utm": [1, 2, 3]})
    s.add_edit({"op": "relocate", "target": "hydrant_01"})
    assert len(s.edits) == 2
    popped = s.undo()
    assert popped["target"] == "hydrant_01"
    assert len(s.edits) == 1
    s.clear()
    assert s.edits == []
    assert s.undo() is None


def test_relocate_edit_builder_shape():
    e = relocate_edit("utility_pole_07", (1, 2, 3), (4, 6, 3), asset_type="utility_pole")
    assert e["op"] == "relocate"
    assert e["from_utm"] == [1.0, 2.0, 3.0]
    assert e["to_utm"] == [4.0, 6.0, 3.0]
    assert e["asset_type"] == "utility_pole"


def test_add_ramp_edit_builder():
    assert add_ramp_edit((10, 20, 0)) == {"op": "add_ramp", "at_utm": [10.0, 20.0, 0.0]}


def test_add_road_edit_builder_shape():
    e = add_road_edit("road_01", [(0, 0, -1), (30, 0, -2)], width_m=7, length_m=30)
    assert e["op"] == "add_road"
    assert e["asset_type"] == "road"
    assert e["path_utm"] == [[0.0, 0.0, -1.0], [30.0, 0.0, -2.0]]
    assert e["width_m"] == 7.0 and e["length_m"] == 30.0


def test_add_road_edit_omits_length_when_unset():
    e = add_road_edit("road_02", [(0, 0, 0), (3, 4, 0)], width_m=6)
    assert "length_m" not in e   # cost engine derives it from path_utm


def test_save_load_roundtrip(tmp_path):
    s = Scenario(block_id="b1", crs="EPSG:26919")
    s.add_edit(relocate_edit("hydrant_01", (0, 0, 0), (3, 4, 0), "hydrant"))
    p = tmp_path / "out" / "scenario.json"   # parent created on save
    s.save(p)
    loaded = Scenario.load(p)
    assert loaded.block_id == "b1"
    assert loaded.edits[0]["target"] == "hydrant_01"
    # on-disk shape matches the guide
    raw = json.loads(p.read_text())
    assert set(raw) == {"block_id", "crs", "edits"}


def test_load_or_new_falls_back_to_defaults(tmp_path):
    s = Scenario.load_or_new(tmp_path / "missing.json", block_id="seed", crs="EPSG:26919")
    assert s.block_id == "seed"
    assert s.edits == []


def test_edits_for_target_filters():
    s = Scenario()
    s.add_edit({"op": "relocate", "target": "a"})
    s.add_edit({"op": "relocate", "target": "b"})
    s.add_edit({"op": "relocate", "target": "a"})
    assert len(s.edits_for("a")) == 2
