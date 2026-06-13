"""End-to-end check of the Phase 1 wiring that run.py performs, with synthetic
data instead of PDAL/Cyvl/APS: grid -> nearest-segment condition -> binned road
mesh -> repave edits derived from road objects -> priced CostReport.
"""
import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString

from pipeline import pavement as pv
from pipeline.cost import cells_to_sqft, estimate
from pipeline.ground_mesh import ground_grid_mesh

CRS = "EPSG:26919"
ROI = 8.0
CELL = 1.0
TREATMENT_FOR_BIN = {"poor": "full_depth_recon", "fair": "mill_and_overlay"}

CATALOG = {
    "pavement": {"crack_seal_sqft": 0.5, "mill_and_overlay_sqft": 4.5,
                 "full_depth_recon_sqft": 12.0},
    "curb_linear_ft": 35.0, "curb_ramp_each": 2500.0, "regrade_sqft": 8.0,
    "relocation": {"default": 1500.0},
}


def _road_patch():
    rng = np.random.default_rng(1)
    n = 2000
    pts = np.column_stack([rng.uniform(0, ROI, n), rng.uniform(0, ROI, n), np.zeros(n)])
    return pts, ["road"] * n


def _pavements_over_patch(origin):
    # good segment along the bottom edge, poor along the top edge, in world coords
    ox, oy = origin
    good = LineString([(ox + 0, oy + 1), (ox + ROI, oy + 1)])
    poor = LineString([(ox + 0, oy + 7), (ox + ROI, oy + 7)])
    return gpd.GeoDataFrame({"score": [95.0, 20.0], "label": ["Good", "Poor"],
                             "client_seg_id": ["s_good", "s_poor"]},
                            geometry=[good, poor], crs=CRS)


def test_phase1_chain_colors_and_prices():
    origin = (324000.0, 4694000.0)   # plausible Davis Sq UTM origin
    pts, labels = _road_patch()

    spec = pv.grid_spec((0, 0, ROI, ROI), CELL, origin=origin)
    gx, gy = pv.cell_centers(spec)
    pav = _pavements_over_patch(origin)
    cond = pv.assign_condition(gx, gy, pav, max_dist_m=10.0)
    road_bins = cond["bin"]

    # ground mesh is built in local (shifted) coords, like run.py
    objs = ground_grid_mesh(pts, labels, (0, 0, ROI, ROI), cell=CELL, road_bins=road_bins)
    names = {o["name"] for o in objs}
    assert "ground_road_good" in names
    assert "ground_road_poor" in names

    # derive repave edits exactly as run.py does
    edits = [{"op": "repave", "target": o["name"],
              "treatment": TREATMENT_FOR_BIN[o["name"].rsplit("_", 1)[1]],
              "area_sqft": cells_to_sqft(len(o["faces"]), CELL)}
             for o in objs
             if o["name"].startswith("ground_road_")
             and o["name"].rsplit("_", 1)[1] in TREATMENT_FOR_BIN]
    assert any(e["target"] == "ground_road_poor" for e in edits)
    assert not any(e["target"] == "ground_road_good" for e in edits)  # good not repaved

    report = estimate(edits, CATALOG)
    assert report.total > 0
    # only the poor section is repaved, so the repave total = its cell area * unit
    poor_obj = next(o for o in objs if o["name"] == "ground_road_poor")
    poor_area = cells_to_sqft(len(poor_obj["faces"]), CELL)
    assert report.by_op()["repave"] == pytest.approx(poor_area * 12.0, abs=0.01)
