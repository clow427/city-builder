"""Tests for the request logic in viewer/token_server.py (no socket bound)."""
import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load_server():
    spec = importlib.util.spec_from_file_location(
        "token_server", REPO / "viewer" / "token_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ts = _load_server()


def _seed(out_dir):
    (out_dir / "scene_meta.json").write_text(json.dumps(
        {"block_id": "davis_sq_a", "crs": "EPSG:26919",
         "bbox_proj": [0, 0, 80, 80]}))
    # a building covering (5,5) and a curb along the y-axis
    (out_dir / "obstacles.json").write_text(json.dumps(
        {"buildings": [[[0, 0], [10, 0], [10, 10], [0, 10]]],
         "curbs": [[[0, 0], [0, 80]]],
         "roi_bounds": [0, 0, 80, 80]}))


def test_empty_scenario_state(tmp_path):
    _seed(tmp_path)
    state = ts.scenario_state(out_dir=str(tmp_path))
    assert state["scenario"]["block_id"] == "davis_sq_a"
    assert state["scenario"]["edits"] == []
    assert state["estimate"]["total"] == 0


def test_apply_relocate_persists_and_prices(tmp_path):
    _seed(tmp_path)
    edit = {"op": "relocate", "target": "utility_pole_07", "asset_type": "utility_pole",
            "from_utm": [40, 40, 0], "to_utm": [40, 50, 0]}
    res = ts.apply_post({"edit": edit}, out_dir=str(tmp_path))
    assert len(res["scenario"]["edits"]) == 1
    assert res["estimate"]["total"] == pytest.approx(8000.0)
    # persisted to disk
    saved = json.loads((tmp_path / "scenario.json").read_text())
    assert saved["edits"][0]["target"] == "utility_pole_07"
    # clear of buildings/curb -> no warnings
    assert res["warnings"] == []


def test_relocate_into_building_warns(tmp_path):
    _seed(tmp_path)
    edit = {"op": "relocate", "target": "hydrant_01", "asset_type": "hydrant",
            "from_utm": [40, 40, 0], "to_utm": [5, 5, 0]}  # inside the building
    res = ts.apply_post({"edit": edit}, out_dir=str(tmp_path))
    assert any("building" in w for w in res["warnings"])
    # advisory only — the edit is still recorded
    assert len(res["scenario"]["edits"]) == 1


def test_undo_and_clear(tmp_path):
    _seed(tmp_path)
    e = {"op": "add_ramp", "at_utm": [10, 10, 0]}
    ts.apply_post({"edit": e}, out_dir=str(tmp_path))
    ts.apply_post({"edit": e}, out_dir=str(tmp_path))
    after_undo = ts.apply_post({"undo": True}, out_dir=str(tmp_path))
    assert len(after_undo["scenario"]["edits"]) == 1
    after_clear = ts.apply_post({"clear": True}, out_dir=str(tmp_path))
    assert after_clear["scenario"]["edits"] == []


def test_bad_post_raises(tmp_path):
    _seed(tmp_path)
    with pytest.raises(ValueError):
        ts.apply_post({"nonsense": True}, out_dir=str(tmp_path))
