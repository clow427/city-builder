# cyvl

**Build on Cyvl's physical-world data.** Street-level imagery, dense LiDAR, and
infrastructure layers for Somerville, MA — with full 3D↔2D projection powered by the
same calibration and SLAM poses as Cyvl's production pipeline.

```bash
pip install "cyvl[viz] @ git+https://github.com/roadgnar/cyvl-spatial-sdk"
```

```python
import cyvl

scene = cyvl.load_scene("somerville")          # public CDN, no credentials

frame = scene.nearest_frame(-71.1218, 42.3967)   # closest photo to Davis Sq
pts = frame.points_in_view()                   # LiDAR projected into the photo
frame.show(overlay=pts)                        # intensity-ramped overlay

xyz = frame.unproject(1920, 1400)              # any pixel -> 3D world point
m = cyvl.measure(frame, (1500, 1500), (2400, 1500))
print(m)                                       # Measurement(3.95 m / 12.96 ft)

frame.open_viewer()                            # photo + 3D in your browser
```

| `locate(frame, "traffic sign")` — SAM3 masks lifted to 3D via LiDAR | Same-pass LiDAR drape (intensity ramp) |
|---|---|
| ![text to 3D](docs/images/sign_projection.jpg) | ![points in view](docs/images/points_in_view.jpg) |

## What's in the box

- **311,784 posed camera frames** — every photo has a validated 6-DoF pose
  (`camera_to_utm`) and production-calibrated intrinsics. Poses match Cyvl's
  internal geolocation pipeline to sub-millimeter.
- **514 LiDAR tiles as COPC** — query any street's worth of colored points over
  HTTP in seconds (`frame.lidar()`, `scene.lidar_around()`); no bulk downloads.
- **215k infrastructure features** — pavement condition scores, 84k distresses,
  3.8k MUTCD-classified signs, 8.3k surface assets, 7.1k road markings, all as
  GeoDataFrames with join keys across layers.
- **Projection both ways** — `frame.project()` (world → pixels, with frustum and
  distortion handling) and `frame.unproject()` (pixel → 3D via LiDAR depth).
- **A browser viewer** — `frame.open_viewer()` renders the point cloud through
  the photo's exact calibrated camera; drag to orbit, double-click to snap back.

## Examples

Notebooks (start here):

| | What it shows |
|---|---|
| [`01_quickstart`](examples/01_quickstart.ipynb) | Load the scene, explore layers, display photos, city map |
| [`02_lidar_into_image`](examples/02_lidar_into_image.ipynb) | `points_in_view`, per-pixel depth, the 3D viewer |
| [`03_click_to_3d_and_measure`](examples/03_click_to_3d_and_measure.ipynb) | Pixel → world coordinates, real measurements from one photo |
| [`04_city_assets_map`](examples/04_city_assets_map.ipynb) | Cross-layer joins: worst streets, sign condition, photo evidence |

Scripts (`python examples/scripts/<name>.py --help`):

| | What it produces |
|---|---|
| [`export_nerfstudio.py`](examples/scripts/export_nerfstudio.py) | `transforms.json` + images for any neighborhood — train **NeRFs / Gaussian Splats** on real streets, no COLMAP |
| [`make_depth_map.py`](examples/scripts/make_depth_map.py) | LiDAR **depth maps** (npz + preview) aligned to any photo |
| [`find_anything.py`](examples/scripts/find_anything.py) | **Text prompt → 3D coordinates**: SAM3 masks lifted through the LiDAR (needs a [fal.ai](https://fal.ai/dashboard/keys) key) |
| [`export_ply.py`](examples/scripts/export_ply.py) | Colored **PLY of any block** for CloudCompare / Blender / CAD |

## SAM3 segmentation (optional)

`pip install "cyvl[sam]"`, grab a free key at [fal.ai/dashboard/keys](https://fal.ai/dashboard/keys),
and `export FAL_KEY="..."` (notebooks: `os.environ["FAL_KEY"] = "..."`, or pass
`api_key=` to any call). Then anything you can name becomes a 3D measurement:

```python
from cyvl.segment import locate

hits = locate(frame, "fire hydrant")     # SAM3 mask -> same-pass LiDAR -> 3D
print(hits[0].lonlat, hits[0].distance_m)
```

## The data

Everything ships from a public CDN (first `load_scene` caches ~120 MB of parquet
in `~/.cache/cyvl`; images and LiDAR stream on demand). The same artifacts are in
the hackathon S3 bucket — see its `README.md` / `schemas.md` for column-level docs,
or set `CYVL_SCENE_DIR=/path/to/parquet-dir` to use a local copy.

Good to know:

- **Coordinates**: layers are WGS84; poses and LiDAR share UTM 19N (EPSG:32619).
  `cyvl.geometry` converts both ways.
- **Same-pass LiDAR + 3 m min-range by default**: the city was scanned by ~85 passes days
  apart. `frame.lidar()` selects the frame's own pass (`same_pass=False` for the
  merged cloud, `pass_window_s` to tune; ~2 s suits busy streets).
- **No dynamic-object classification** in this dataset — moving vehicles are in
  the point clouds; the same-pass time window is the mitigation.
- **Sign coverage is partial**: the signs layer covers the city's south/southeast
  only — that's the extent of the delivered sign survey (verified against Cyvl's
  production API, not a data loss). Pavement/distress/imagery layers are city-wide.
- **360° panoramas** are a geo-only layer (`scene.pano`) — no poses/projection.
- A small set of frames (2.3%, `has_lidar_pass == False`) have poses but no
  own-pass LiDAR; the SDK falls back to the densest nearby pass with a warning.

## Get the data

The dataset is also hosted in a public, read-only S3 bucket (`cyvl-hackathon`). See
the [**S3 access guide**](S3_GUIDE.md) for every way to list, download, or query
it directly — with AWS credentials, anonymously via `--no-sign-request`, or over
plain HTTPS with no AWS tooling at all.

## Development

```bash
uv venv && uv pip install -e '.[viz,dev]'
uv run pytest          # offline tests against the bundled mini scene
uv run ruff check src/ tests/
```
