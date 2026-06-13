"""Single source of truth for Cyvl infrastructure layers, in one working CRS.

Phase 0 of StreetForge: stop hand-downloading GeoJSON and reconcile coordinate
systems. Cyvl ships its layers in **EPSG:4326** (lon/lat); the city-builder
LiDAR pipeline works in whatever CRS the LAS header declares (NAD83 UTM 19N,
**EPSG:26919**, for the current tiles), while the SDK's poses and LiDAR tiles are
**EPSG:32619** (WGS84 UTM 19N). 26919 and 32619 differ by ~1-2 m on the ground.

The rule this module enforces: pick **one working CRS per block** (the LAS
header's CRS) and reproject every Cyvl layer into it before any spatial join.
`geopandas.to_crs` handles the NAD83<->WGS84 datum shift, so a 4326 layer lands
correctly on 26919 ground. Only mixing SDK 32619 geometry directly onto 26919
data reintroduces the offset — always round-trip through `to_working_crs`.

Layers can come from three sources (lazy-imported so unit tests need none):
  * the Cyvl SDK            -> `scene.pavements`, `scene.distresses`, ...
  * the public S3 bucket    -> GeoJSON over plain HTTPS
  * a local directory       -> the fork's existing `data/*_v2.geojson`
"""
from __future__ import annotations

import os

# Friendly accessor -> exported GeoJSON basename (S3 `data/` + local dir).
LAYER_GEOJSON = {
    "pavements": "pavements_v2",
    "distresses": "distresses_v2",
    "inspection_cells": "distressInspectionCells_v2",
    "signs": "signs_v2",
    "assets": "aboveGroundAssets_v2",
    "markings": "sam_v2",
    "rollup": "rollup_v2",
    "drive_paths": "streetviewImagePaths_v2",
}

S3_HTTPS_BASE = "https://cyvl-hackathon.s3.amazonaws.com/data"
LAYERS_4326 = "EPSG:4326"   # every Cyvl product layer ships in WGS84 lon/lat


# --------------------------------------------------------------- CRS reconcile

def working_crs_from_las(las_path):
    """The block's working CRS, read straight from the LAS header.

    This is the one CRS everything else is reprojected into. Delegates to
    pipeline.crop.las_crs, which raises if the header carries no CRS.
    """
    from pipeline.crop import las_crs

    return las_crs(las_path)


def to_working_crs(gdf, working_crs, *, assume_crs=LAYERS_4326):
    """Reproject a layer into the working CRS (datum shift handled by pyproj).

    If the GeoDataFrame has no CRS set, it is assumed to be `assume_crs`
    (the Cyvl layers are always 4326) before reprojecting.
    """
    if gdf.crs is None:
        gdf = gdf.set_crs(assume_crs, allow_override=True)
    return gdf.to_crs(working_crs)


def crop_bbox(gdf, bbox_proj):
    """Crop to (xmin, ymin, xmax, ymax) expressed in the GeoDataFrame's CRS."""
    if bbox_proj is None:
        return gdf
    xmin, ymin, xmax, ymax = bbox_proj
    return gdf.cx[xmin:xmax, ymin:ymax]


# --------------------------------------------------------------- layer sources

def _from_sdk(scene, accessor):
    return getattr(scene, accessor)


def _from_s3(accessor):
    import geopandas as gpd

    name = LAYER_GEOJSON[accessor]
    return gpd.read_file(f"{S3_HTTPS_BASE}/{name}.geojson")


def _from_local(accessor, local_dir):
    import geopandas as gpd

    name = LAYER_GEOJSON[accessor]
    return gpd.read_file(os.path.join(local_dir, f"{name}.geojson"))


def _raw_layer(accessor, *, scene, source, local_dir):
    """Fetch a layer in its native 4326 from the chosen source."""
    if accessor not in LAYER_GEOJSON:
        raise KeyError(f"unknown layer {accessor!r}; known: {sorted(LAYER_GEOJSON)}")
    if source == "auto":
        if scene is not None:
            source = "sdk"
        elif local_dir and os.path.exists(
                os.path.join(local_dir, f"{LAYER_GEOJSON[accessor]}.geojson")):
            source = "local"
        else:
            source = "s3"
    if source == "sdk":
        if scene is None:
            raise ValueError("source='sdk' needs a Scene (see load_scene)")
        return _from_sdk(scene, accessor)
    if source == "s3":
        return _from_s3(accessor)
    if source == "local":
        return _from_local(accessor, local_dir)
    raise ValueError(f"unknown source {source!r}; use auto/sdk/s3/local")


def load_layer(accessor, working_crs, *, scene=None, source="auto",
               local_dir="data", bbox_proj=None):
    """Load one Cyvl layer, reprojected into the working CRS (+ optional crop).

    Args:
        accessor: friendly layer name (pavements, distresses, assets, ...).
        working_crs: target CRS (the block's LAS CRS; see working_crs_from_las).
        scene: an optional cyvl Scene; when given, `source="auto"` reads the SDK.
        source: "auto" | "sdk" | "s3" | "local".
        local_dir: directory of `*_v2.geojson` for source="local"/auto.
        bbox_proj: optional (xmin, ymin, xmax, ymax) crop, in the working CRS.

    Returns:
        A GeoDataFrame in `working_crs`.
    """
    gdf = _raw_layer(accessor, scene=scene, source=source, local_dir=local_dir)
    gdf = to_working_crs(gdf, working_crs)
    return crop_bbox(gdf, bbox_proj)


def load_layers(accessors, working_crs, **kwargs):
    """Load several layers into one working CRS; returns {accessor: GeoDataFrame}."""
    return {a: load_layer(a, working_crs, **kwargs) for a in accessors}


def load_scene(name="somerville"):
    """Thin wrapper over `cyvl.load_scene` (lazy import of the SDK)."""
    import cyvl

    return cyvl.load_scene(name)
