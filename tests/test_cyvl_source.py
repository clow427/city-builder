import geopandas as gpd
import pytest
from shapely.geometry import Point

from pipeline import cyvl_source as cs


def _davis_sq_points():
    # two lon/lat points near Davis Square, Somerville (no CRS set, like raw SDK)
    return gpd.GeoDataFrame(
        {"score": [90.0, 30.0]},
        geometry=[Point(-71.1218, 42.3967), Point(-71.1205, 42.3972)],
    )


def test_to_working_crs_assumes_4326_and_reprojects_to_utm():
    gdf = _davis_sq_points()
    assert gdf.crs is None
    out = cs.to_working_crs(gdf, "EPSG:26919")
    assert out.crs.to_epsg() == 26919
    # Davis Sq in UTM 19N: easting ~324 km, northing ~4.694 Mm
    x, y = out.geometry.iloc[0].x, out.geometry.iloc[0].y
    assert 320_000 < x < 330_000
    assert 4_690_000 < y < 4_700_000


def test_reproject_round_trips_back_to_lonlat():
    gdf = _davis_sq_points().set_crs("EPSG:4326")
    there = cs.to_working_crs(gdf, "EPSG:26919")
    back = there.to_crs("EPSG:4326")
    assert back.geometry.iloc[0].x == pytest.approx(-71.1218, abs=1e-6)
    assert back.geometry.iloc[0].y == pytest.approx(42.3967, abs=1e-6)


def test_crop_bbox_in_working_crs():
    gdf = cs.to_working_crs(_davis_sq_points(), "EPSG:26919")
    p0 = gdf.geometry.iloc[0]
    # tight bbox around the first point only
    bbox = (p0.x - 5, p0.y - 5, p0.x + 5, p0.y + 5)
    cropped = cs.crop_bbox(gdf, bbox)
    assert len(cropped) == 1
    assert cs.crop_bbox(gdf, None) is gdf


def test_unknown_layer_raises():
    with pytest.raises(KeyError):
        cs._raw_layer("not_a_layer", scene=None, source="s3", local_dir="data")


def test_auto_source_requires_something():
    # auto with no scene and no local file -> falls through to s3 (network);
    # here we just assert the source-selection logic picks 's3', not that it
    # actually fetches. Patch _from_s3 to avoid the network.
    called = {}

    def fake_s3(accessor):
        called["accessor"] = accessor
        return _stub_gdf()

    cs._from_s3, real = fake_s3, cs._from_s3
    try:
        cs.load_layer("pavements", "EPSG:26919", local_dir="/nonexistent")
    finally:
        cs._from_s3 = real
    assert called["accessor"] == "pavements"


def _stub_gdf():
    return gpd.GeoDataFrame({"score": [1.0]}, geometry=[Point(-71.12, 42.39)],
                            crs="EPSG:4326")
