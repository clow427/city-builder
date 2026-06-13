import numpy as np

from pipeline.ground_mesh import ground_grid_mesh


def _road_points(n=400):
    # a flat 4x4 m road patch of ground points, all labelled "road"
    rng = np.random.default_rng(0)
    pts = np.column_stack([rng.uniform(0, 4, n), rng.uniform(0, 4, n), np.zeros(n)])
    return pts, ["road"] * n


def test_single_road_object_without_bins():
    pts, labels = _road_points()
    objs = ground_grid_mesh(pts, labels, (0, 0, 4, 4), cell=1.0)
    names = {o["name"] for o in objs}
    assert names == {"ground_road"}
    assert objs[0]["material"] == "asphalt"


def test_binned_road_emits_separate_condition_objects():
    pts, labels = _road_points()
    # 4x4 grid (cell=1) -> 16 cells, flattened ix*ny+iy. Make the left half good,
    # right half poor.
    nx = ny = 4
    bins = np.empty(nx * ny, dtype=object)
    for ix in range(nx):
        for iy in range(ny):
            bins[ix * ny + iy] = "good" if ix < 2 else "poor"
    objs = ground_grid_mesh(pts, labels, (0, 0, 4, 4), cell=1.0, road_bins=bins)
    by_name = {o["name"]: o for o in objs}
    assert "ground_road_good" in by_name
    assert "ground_road_poor" in by_name
    assert "ground_road_fair" not in by_name  # none assigned
    assert by_name["ground_road_good"]["material"] == "road_good"
    assert by_name["ground_road_poor"]["material"] == "road_poor"


def test_unbinned_road_cells_fall_back_to_asphalt():
    pts, labels = _road_points()
    bins = np.full(16, None, dtype=object)   # no cell has a condition match
    objs = ground_grid_mesh(pts, labels, (0, 0, 4, 4), cell=1.0, road_bins=bins)
    names = {o["name"] for o in objs}
    assert names == {"ground_road"}
    assert objs[0]["material"] == "asphalt"
