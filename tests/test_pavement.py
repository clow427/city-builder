import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Point

from pipeline import pavement as pv

CRS = "EPSG:26919"


def test_condition_bin_thresholds():
    assert pv.condition_bin(95) == "good"
    assert pv.condition_bin(70) == "good"
    assert pv.condition_bin(55) == "fair"
    assert pv.condition_bin(40) == "fair"
    assert pv.condition_bin(10) == "poor"
    assert pv.condition_bin(None) is None
    assert pv.condition_bin(float("nan")) is None


def test_label_bin_normalizes_text():
    assert pv.label_bin("Good") == "good"
    assert pv.label_bin("very poor") == "poor"
    assert pv.label_bin("Satisfactory") == "good"
    assert pv.label_bin("nonsense") is None
    assert pv.label_bin(None) is None


def test_grid_spec_and_centers_match_ground_mesh_flattening():
    spec = pv.grid_spec((0, 0, 1.0, 1.0), cell=0.5, origin=(10.0, 20.0))
    x0, y0, cell, nx, ny = spec
    assert (nx, ny) == (2, 2)
    cx, cy = pv.cell_centers(spec)
    # flatten is ix*ny+iy: index 0 -> (ix0,iy0), index 1 -> (ix0,iy1)
    assert cx[0] == pytest.approx(10.25) and cy[0] == pytest.approx(20.25)
    assert cx[1] == pytest.approx(10.25) and cy[1] == pytest.approx(20.75)
    assert cx[2] == pytest.approx(10.75) and cy[2] == pytest.approx(20.25)


def test_count_in_cells_bins_points():
    spec = pv.grid_spec((0, 0, 1.0, 1.0), cell=0.5)  # 2x2 grid, origin 0
    px = [0.25, 0.25, 0.75]   # cell (0,0), (0,0), (1,0)
    py = [0.25, 0.30, 0.25]
    counts = pv.count_in_cells(px, py, spec)
    assert counts[0] == 2          # ix0,iy0
    assert counts[pv_flat(1, 0, spec)] == 1
    assert counts.sum() == 3
    # a point outside the grid is ignored
    assert pv.count_in_cells([5.0], [5.0], spec).sum() == 0


def pv_flat(ix, iy, spec):
    _, _, _, nx, ny = spec
    return ix * ny + iy


def _pavements():
    # two parallel road segments along x, ~8 m apart, different conditions
    good = LineString([(0, 0), (20, 0)])
    poor = LineString([(0, 8), (20, 8)])
    return gpd.GeoDataFrame(
        {"score": [92.0, 25.0], "label": ["Good", "Poor"],
         "client_seg_id": ["seg_good", "seg_poor"]},
        geometry=[good, poor], crs=CRS)


def test_assign_condition_nearest_segment():
    pav = _pavements()
    # three cells: near good line, near poor line, far away
    cx = np.array([10.0, 10.0, 200.0])
    cy = np.array([0.5, 7.5, 200.0])
    res = pv.assign_condition(cx, cy, pav, max_dist_m=15.0)
    assert res["bin"][0] == "good"
    assert res["seg_id"][0] == "seg_good"
    assert res["bin"][1] == "poor"
    assert res["score"][1] == pytest.approx(25.0)
    # far cell: no match within max_dist
    assert res["bin"][2] is None
    assert np.isnan(res["score"][2])


def test_assign_condition_falls_back_to_score_when_label_missing():
    pav = _pavements()
    pav["label"] = [None, None]   # force score-based binning
    res = pv.assign_condition(np.array([10.0]), np.array([0.5]), pav)
    assert res["bin"][0] == "good"   # score 92 -> good


def test_assign_condition_empty_inputs():
    res = pv.assign_condition([], [], _pavements())
    assert len(res["bin"]) == 0
    empty = gpd.GeoDataFrame({"score": []}, geometry=[], crs=CRS)
    res2 = pv.assign_condition([1.0], [1.0], empty)
    assert res2["bin"][0] is None
