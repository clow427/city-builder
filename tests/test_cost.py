import json

import pytest

from pipeline.cost import (
    CostError,
    CostReport,
    cells_to_sqft,
    estimate,
    load_catalog,
)

CATALOG = {
    "pavement": {
        "crack_seal_sqft": 0.50,
        "mill_and_overlay_sqft": 4.50,
        "full_depth_recon_sqft": 12.00,
    },
    "curb_linear_ft": 35.00,
    "sidewalk_sqft": 12.00,
    "curb_ramp_each": 2500.00,
    "regrade_sqft": 8.00,
    "relocation": {
        "utility_pole": 8000.00,
        "hydrant": 4500.00,
        "sign": 600.00,
        "default": 1500.00,
    },
}


def test_repave_area_times_unit():
    r = estimate([{"op": "repave", "treatment": "mill_and_overlay", "area_sqft": 100}],
                 CATALOG)
    assert len(r.line_items) == 1
    assert r.line_items[0].amount == pytest.approx(450.0)
    assert r.total == pytest.approx(450.0)


def test_repave_resolves_area_from_objects_map():
    objects = {"ground_road_poor_03": {"area_sqft": 200.0}}
    r = estimate([{"op": "repave", "target": "ground_road_poor_03",
                   "treatment": "full_depth_recon"}], CATALOG, objects=objects)
    assert r.total == pytest.approx(200.0 * 12.0)
    assert r.line_items[0].target == "ground_road_poor_03"


def test_repave_unknown_treatment_raises():
    with pytest.raises(CostError):
        estimate([{"op": "repave", "treatment": "fairy_dust", "area_sqft": 1}], CATALOG)


def test_relocate_uses_asset_table_and_infers_type_from_target():
    r = estimate([{"op": "relocate", "target": "utility_pole_07",
                   "from_utm": [0, 0, 0], "to_utm": [3, 4, 0]}], CATALOG)
    li = r.line_items[0]
    assert li.amount == pytest.approx(8000.0)
    assert "5.0 m" in li.description  # 3-4-5 move distance reported


def test_relocate_unknown_type_falls_back_to_default():
    r = estimate([{"op": "relocate", "asset_type": "bench"}], CATALOG)
    assert r.total == pytest.approx(1500.0)


def test_relocate_distance_scaling_adds_run_line_item():
    catalog = {**CATALOG, "relocation_per_m": {"utility_pole": 50.0}}
    r = estimate([{"op": "relocate", "asset_type": "utility_pole",
                   "from_utm": [0, 0, 0], "to_utm": [0, 10, 0]}], catalog)
    # base 8000 + 10 m * 50 = 8500, split across two line items
    assert len(r.line_items) == 2
    assert r.total == pytest.approx(8500.0)


def test_relocate_no_scaling_when_table_absent():
    r = estimate([{"op": "relocate", "asset_type": "utility_pole",
                   "from_utm": [0, 0, 0], "to_utm": [0, 10, 0]}], CATALOG)
    assert len(r.line_items) == 1
    assert r.total == pytest.approx(8000.0)


def test_add_ramp_is_per_each():
    r = estimate([{"op": "add_ramp", "at_utm": [1, 2, 3]}], CATALOG)
    assert r.total == pytest.approx(2500.0)


def test_regrade_from_cells_and_cell_size():
    # 2 cells of 0.5 m edge -> 2 * 0.25 m^2 * 10.7639 sqft * $8
    r = estimate([{"op": "regrade", "cells": ["c1", "c2"], "cell_m": 0.5}], CATALOG)
    expected = cells_to_sqft(2, 0.5) * 8.0
    assert r.total == pytest.approx(expected, abs=0.01)  # total is rounded to cents


def test_widen_emits_pavement_and_curb_items():
    r = estimate([{"op": "widen", "segment": "seg_12", "delta_ft": 4,
                   "length_ft": 30}], CATALOG)
    # area = 4 * 30 = 120 sqft @ full_depth_recon 12 ; curb = 30 ft @ 35
    assert len(r.line_items) == 2
    assert r.total == pytest.approx(120 * 12.0 + 30 * 35.0)


def test_add_road_prices_pavement_area_and_two_curbs():
    # explicit length_m + width_m (what the viewer carries)
    r = estimate([{"op": "add_road", "target": "road_01",
                   "width_m": 7.0, "length_m": 30.0}], CATALOG)
    from pipeline.cost import FT_PER_M, SQFT_PER_SQM
    area_sqft = 30.0 * 7.0 * SQFT_PER_SQM
    curb_ft = 30.0 * FT_PER_M * 2.0
    assert len(r.line_items) == 2
    assert r.total == pytest.approx(area_sqft * 12.0 + curb_ft * 35.0)
    assert r.by_op()["add_road"] == pytest.approx(r.total)


def test_add_road_derives_length_from_path_when_absent():
    # no length_m -> horizontal length comes off path_utm (3-4-5 -> 5 m run)
    r = estimate([{"op": "add_road", "target": "road_02", "width_m": 6.0,
                   "path_utm": [[0, 0, -1.0], [3, 4, -2.0]]}], CATALOG)
    from pipeline.cost import FT_PER_M, SQFT_PER_SQM
    assert r.total == pytest.approx(5.0 * 6.0 * SQFT_PER_SQM * 12.0
                                    + 5.0 * FT_PER_M * 2.0 * 35.0)


def test_add_road_needs_width_and_length():
    with pytest.raises(CostError):
        estimate([{"op": "add_road", "target": "road_03", "width_m": 7.0}], CATALOG)


def test_move_road_is_free_and_emits_no_line_item():
    road = {"op": "add_road", "target": "road_01", "width_m": 7.0, "length_m": 30.0}
    move = {"op": "move_road", "target": "road_01",
            "from_path_utm": [[0, 0, 0], [30, 0, 0]],
            "to_path_utm": [[10, 5, -1], [40, 5, -1]], "width_m": 7.0}
    base = estimate([road], CATALOG).total
    r = estimate([road, move], CATALOG)        # relocating a road adds nothing
    assert r.total == pytest.approx(base)
    assert all(li.op != "move_road" for li in r.line_items)


def test_total_is_sum_and_by_op_groups():
    edits = [
        {"op": "repave", "treatment": "crack_seal", "area_sqft": 100},
        {"op": "add_ramp"},
        {"op": "relocate", "asset_type": "hydrant"},
    ]
    r = estimate(edits, CATALOG)
    assert r.total == pytest.approx(50.0 + 2500.0 + 4500.0)
    assert r.by_op()["repave"] == pytest.approx(50.0)


def test_unknown_op_raises_in_strict_skips_otherwise():
    with pytest.raises(CostError):
        estimate([{"op": "teleport"}], CATALOG)
    r = estimate([{"op": "teleport"}, {"op": "add_ramp"}], CATALOG, strict=False)
    assert r.total == pytest.approx(2500.0)


def test_missing_quantity_raises():
    with pytest.raises(CostError):
        estimate([{"op": "repave", "treatment": "crack_seal"}], CATALOG)


def test_exports_roundtrip():
    r = estimate([{"op": "repave", "treatment": "mill_and_overlay", "area_sqft": 10}],
                 CATALOG)
    data = json.loads(r.to_json())
    assert data["total"] == pytest.approx(45.0)
    assert data["line_items"][0]["amount"] == pytest.approx(45.0)
    assert "TOTAL" in r.to_csv()
    assert "Total" in r.to_markdown()


def test_write_dispatches_on_extension(tmp_path):
    r = estimate([{"op": "add_ramp"}], CATALOG)
    for ext in ("md", "csv", "json"):
        p = tmp_path / f"estimate.{ext}"
        r.write(str(p))
        assert p.exists() and p.read_text()
    with pytest.raises(CostError):
        r.write(str(tmp_path / "estimate.pdf"))


def test_load_catalog_default_file_matches_shape():
    catalog = load_catalog()
    assert "pavement" in catalog and "relocation" in catalog
    assert catalog["pavement"]["mill_and_overlay_sqft"] > 0
