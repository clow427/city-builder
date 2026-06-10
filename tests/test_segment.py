import numpy as np
from pipeline.segment import split_ground


def test_split_ground_separates_by_mask():
    pts = np.array([[0, 0, 0], [1, 1, 2.0]], dtype=float)
    ground, nonground = split_ground(pts, np.array([True, False]))
    assert ground.shape[0] == 1 and nonground.shape[0] == 1
    assert ground[0, 2] == 0.0
