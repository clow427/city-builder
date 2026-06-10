import numpy as np
from pipeline.segment import split_ground


def test_split_ground_separates_by_mask():
    pts = np.array([[0, 0, 0], [1, 1, 2.0]], dtype=float)
    ground, nonground = split_ground(pts, np.array([True, False]))
    assert ground.shape[0] == 1 and nonground.shape[0] == 1
    assert ground[0, 2] == 0.0


from shapely.geometry import LineString
from pipeline.segment import classify_road_sidewalk


def test_classify_road_sidewalk_by_polygon():
    ground = np.array([[0, 0, 0], [0, 10, 0]], dtype=float)
    road_lines = [LineString([(-5, 0), (5, 0)])]
    labels = classify_road_sidewalk(ground, road_lines, width_m=3.0)
    assert labels[0] == "road"
    assert labels[1] == "sidewalk"


from pipeline.segment import cluster_objects, keep_car_clusters


def test_keep_car_clusters_filters_by_size(synthetic_points):
    ground, car = synthetic_points
    tiny = np.array([[15, 15, 0.2], [15.1, 15.1, 0.25]])
    cars = keep_car_clusters([car, tiny])
    assert len(cars) == 1
    assert 2.0 <= cars[0]["length"] <= 6.0
    assert 1.4 <= cars[0]["width"] <= 2.2
