import numpy as np
from pipeline.lift_assets import cylinder_indices, z_extent


def test_cylinder_indices_selects_within_radius():
    pts = np.array([[0, 0, 0], [0.3, 0, 1], [5, 5, 0]], dtype=float)
    idx = cylinder_indices(pts, (0, 0), 0.5)
    assert set(idx.tolist()) == {0, 1}


def test_z_extent_returns_ground_top_height():
    gz, tz, h = z_extent(np.array([0.0, 0.5, 1.2, 0.6, 0.9]))
    assert gz <= 0.1 and tz >= 1.1
    assert h > 0.9
