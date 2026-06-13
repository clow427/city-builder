# StreetForge — Street Intervention Design & Cost Tool

> **Project description & implementation guide.**
> *"StreetForge" is a working name — swap it for whatever you land on.*
>
> Built on the `city-builder` fork (itself a fork of `hackathonBuckets`),
> the Cyvl Spatial SDK, and Autodesk Platform Services (APS).

---

## 1. Project Overview & Vision

StreetForge turns a real, scanned city block into a **browser-based design
table for street interventions**. A planner loads a Somerville block, sees its
actual pavement condition, infrastructure, and grade reconstructed as CAD, then
**proposes changes and watches the cost update live**:

- repave or patch a deteriorated pavement section,
- widen / narrow / realign a street or add a curb bump-out,
- pick up and relocate a utility pole, hydrant, sign, or signal,
- add a curb ramp or re-grade a sidewalk to meet accessibility (ADA) rules,

…and get an **itemized cost estimate** for the proposal before anyone touches
asphalt.

The CAD model is generated **from Cyvl LiDAR point-cloud data via Autodesk APS**
(point cloud → classified mesh → OBJ/MTL → APS Model Derivative → SVF2 → APS
Viewer), and editing happens **interactively inside that viewer**.

### The five capability pillars

| # | Pillar | What the user does | What it costs |
|---|--------|--------------------|---------------|
| 1 | **Pavement repair** | Select a road section, choose a treatment | $/sq ft × area |
| 2 | **Street geometry** | Widen / narrow / realign a segment, add bump-outs | Δ pavement area + curb |
| 3 | **Infrastructure repositioning** | Drag a pole / hydrant / sign / signal to a new spot | $/relocation by type |
| 4 | **Accessibility (ADA)** | Flag steep slopes / missing ramps, add ramp / re-grade | $/ramp, $/sq ft re-grade |
| 5 | **Cost anticipation** | Live running total + exportable report | sum of the above |

### What changes vs. the fork

The current fork **visualizes** a city (classified cloud → editable CAD with
movable cars as a tech demo). StreetForge **proposes and prices interventions**.
The same Autodesk + Cyvl plumbing is reused almost entirely; the new work is
*additive pipeline stages* (pavement condition, slope/ADA, cost) and *new viewer
panels* (repair, relocate, accessibility, cost) on top of the existing
select/move/delete machinery.

---

## 2. Data Foundation — the Cyvl Spatial SDK

The SDK (`sandbox/cyvl-spatial-sdk/`) is the source of truth for geometry and
infrastructure attributes. Key entry points:

```python
import cyvl
scene = cyvl.load_scene("somerville")   # -> Scene
```

### Infrastructure layers (GeoDataFrames, EPSG:4326)

Accessed as dynamic attributes on `Scene` (see `LAYER_ALIASES` in
`src/cyvl/scene.py`):

| Accessor | Underlying layer | Use in StreetForge |
|----------|------------------|--------------------|
| `scene.pavements` | `pavements` | Per-street condition `score`, `length_ft`, address → pavement coloring + repair targeting |
| `scene.distresses` | `distresses` (~84k) | Individual cracks/potholes → distress density per cell |
| `scene.inspection_cells` | `distressInspectionCells` | Survey grid → align with our mesh cells |
| `scene.signs` | `signs` (~3.8k, MUTCD) | Selectable/relocatable sign assets |
| `scene.assets` | `aboveGroundAssets` (~8.3k) | Poles, hydrants, ramps, curbs → relocation + ADA |
| `scene.markings` | `sam` (~7.1k) | Lane/crosswalk markings → geometry edits |
| `scene.rollup` | `rollup` | City-wide aggregated condition |
| `scene.drive_paths` | `streetviewImagePaths` | Road centerlines for classification |

> The fork currently reads equivalent GeoJSON files
> (`data/pavements_v2.geojson`, `data/aboveGroundAssets_v2.geojson`) downloaded
> by hand. StreetForge should migrate these reads to the SDK layer accessors (or
> the S3 reads below) so the schema and field names stay consistent.

### LiDAR access

```python
from cyvl import PointCloud
pc = scene.lidar_around(x_utm, y_utm, radius_m=30.0)   # -> PointCloud
# pc.xyz (N,3 UTM), pc.rgb, pc.intensity, pc.gps_time, pc.scan_range
```

Also `frame.lidar(radius_m=...)` for per-photo, same-pass clouds. See
`src/cyvl/lidar.py` (`PointCloud.crop_radius`, `crop_min_range`, `concatenate`).

### Geometry, projection & measurement

```python
from cyvl.geometry import lonlat_to_utm, utm_to_lonlat   # src/cyvl/geometry.py
from cyvl import measure                                  # src/cyvl/measure.py
m = measure(frame, px_a, px_b, lidar=pc)                  # -> Measurement (.meters/.feet)
world_xyz = frame.unproject(px, py, lidar=pc)             # pixel -> 3D UTM point
```

These give real-world dimensions for free — essential for sizing repairs and
pricing by area / linear foot.

### Coordinate systems (⚠ reconcile this early)

- **Cyvl layers:** WGS84, **EPSG:4326** (lon/lat).
- **Cyvl SDK poses & LiDAR:** UTM 19N, **EPSG:32619**.
- **city-builder pipeline:** reads the EPSG **from the LAS header** — for the
  current tiles this is **EPSG:26919** (NAD83 UTM 19N), *not* 32619.

26919 (NAD83) and 32619 (WGS84) differ by ~1–2 m in absolute position. The fork
is internally consistent because it derives everything from the LAS header and
shifts to a local origin. **The risk appears when joining SDK-provided geometry
(32619) onto LAS-derived geometry (26919).** StreetForge must pick one working
CRS per block and reproject all inputs into it (geopandas `to_crs`) before any
spatial join. Flag this as **Phase 0 work**.

### S3 access (replace the manual curl flow)

`sandbox/cyvl-spatial-sdk/S3_GUIDE.md`: bucket `cyvl-hackathon` (us-east-1,
public/read-only). Three credential-free paths — AWS CLI `--no-sign-request`,
plain HTTPS, and read-in-place via DuckDB / pandas / geopandas, e.g.:

```python
import geopandas as gpd
gdf = gpd.read_file("https://cyvl-hackathon.s3.amazonaws.com/data/aboveGroundAssets_v2.geojson")
```

**Opportunity:** the fork's `pipeline/select_tiles.py` only *prints* curl
commands for the user to run. StreetForge can pull tiles + layers
programmatically via the SDK / S3, removing a manual step.

---

## 3. Current Architecture (the fork, mapped honestly)

End-to-end pipeline as it exists today:

```
run.py
 ├─ pipeline/crop.py        load_points(), crop_xy(), las_crs()
 ├─ pipeline/segment.py     ground_mask_pdal() [PDAL SMRF] → classify_ground3()
 │                          (road/sidewalk/grass via buffered centerlines),
 │                          cluster_objects() [DBSCAN], _obb() [PCA box],
 │                          keep_car_clusters()
 ├─ pipeline/lift_assets.py lift_assets()  (Cyvl 2D asset pts → 3D via cylinder crop + Z percentiles)
 ├─ pipeline/ground_mesh.py ground_grid_mesh()  (per-cell mean-Z heightfield)
 ├─ pipeline/buildings.py   fetch_osm_buildings(), building_objects()
 ├─ pipeline/to_cad.py      car_objects(), asset_objects(), write_obj(), write_mtl()
 ├─ zip scene.obj + scene.mtl
 ├─ aps/auth.py             get_token()  [2-legged OAuth v2, client credentials]
 ├─ aps/upload.py           ensure_bucket(), upload_object()  [signed-S3 3-step]
 └─ aps/translate.py        start_translation() [→ SVF2], wait_until_done()
        → viewer/  (Autodesk Viewer SDK v7, AutodeskProduction2 / streamingV2)
```

Parallel output: `pipeline/classify_io.py write_colored_las()` → a Potree-viewable
classified `.laz` (raw photoreal cloud).

### Constraints to design around

From `docs/autodesk-integration-summary.md` and the source:

- **APS Viewer has no native point cloud.** Model Derivative ingests meshes, not
  LAS/LAZ. The raw cloud appears only as a **three.js (r71) `PointCloud`
  overlay** (`viewer/viewer.js loadPointCloud()`), aligned via
  `model.getData().placementWithOffset`. It is visual, **not selectable**. → All
  editable/priced objects must be **mesh** objects in the OBJ.
- **SVF2 is cached per OSS object key.** Re-uploading the same key serves a stale
  model. The fork versions keys (`scene_cad_v6.zip`). StreetForge regenerates the
  scene per scenario, so **bump the object key (or use `x-ads-force: true`) on
  every regen.**
- **Viewer token scope is `data:read`** on the 2-legged flow (`viewer:read` is
  invalid). Keep this.
- **Z-up survey data:** the viewer fixes world-up via
  `setWorldUpVector(0,0,1)` in `setHumanView()`. Any new overlays must respect
  this.

---

## 4. Target Architecture — the five pillars

Each pillar = (data source) → (pipeline change) → (viewer UX) → (cost hook).
Everything is additive; nothing below rewrites the Autodesk plumbing.

### 4.1 Pavement repair testing

**Data:** `scene.pavements.score` (condition) + `scene.distresses` (point/line
features), reprojected into the block's working CRS.

**Pipeline:** new `pipeline/pavement.py`. `ground_grid_mesh()` already grids the
ROI into cells and computes, per cell, a mean Z and a majority surface class
(`pipeline/ground_mesh.py:11-79`). Extend it (or post-process its cells) to
carry two more per-cell attributes:

- `condition` — nearest pavement segment's `score` (spatial join of cell center
  to `scene.pavements`),
- `distress_density` — count of `scene.distresses` features within the cell.

Emit road cells in **condition bins** as separate OBJ objects/materials
(`ground_road_good`, `ground_road_fair`, `ground_road_poor`) instead of one flat
`asphalt`, so each bin is a selectable dbId and can be recolored.

**Viewer:** color road cells by condition (green→red ramp). Select a pavement
object → panel offers a **treatment**: crack seal / mill-and-overlay /
full-depth reconstruct → recolor to "repaired" + add the edit to the scenario.

**Cost hook:** `area_sqft = cell_count × cell² (m²) × 10.7639` → `× unit_cost[treatment]`.

### 4.2 Street geometry changes

**Data:** road centerlines from `scene.pavements` / `scene.drive_paths`; road
width currently comes from buffering those lines in
`pipeline/segment.py classify_ground3()` and feeding the mesh.

**Pipeline:** parameterize the road buffer width per segment. A "widen by X"
edit changes the buffer half-width → re-run `classify_ground3` +
`ground_grid_mesh` for the affected ROI only and regenerate the OBJ. Bump-outs =
local polygon additions to the road buffer at intersection corners.

**Viewer:** handles on a selected segment to widen/narrow/realign; regenerate
and reload the model (new object key).

**Cost hook:** Δ pavement area (sq ft, added vs. removed cells) + new/removed
curb linear feet.

### 4.3 Infrastructure repositioning

**Data:** `scene.assets` (poles, hydrants, signs, signals) already lifted to 3D
by `pipeline/lift_assets.py` and emitted as **named, selectable** CAD objects by
`to_cad.py asset_objects()` + `write_obj()` (`o hydrant_01`, `o utility_pole_03`,
…). Each name becomes a viewer dbId.

**Viewer:** the move hook already exists — `viewer/viewer.js moveCar()` uses
`tree.enumNodeFragments` → `getFragmentProxy` → `fp.position` →
`updateAnimTransform()`. Today it hard-codes `position.x += 5`. Generalize to:

- **free drag** (pointer-plane intersection on the ground) + optional snap,
- read the asset's original UTM position and compute the **move distance** (in
  meters, using the same local-origin scale),
- **persist** the new position into the scenario JSON (the fragment transform is
  ephemeral; the scenario is the source of truth).

Add lightweight **clearance validation** (e.g. pole not inside a building
footprint, hydrant min offset from curb).

**Cost hook:** `unit_cost.relocation[asset_type]` (per-each), optionally scaled
by move distance for utility runs.

### 4.4 Accessibility (ADA) analysis

**Data:** the per-cell ground heightfield from `ground_grid_mesh()` (it already
computes per-cell Z and raises sidewalk cells +0.12 m,
`pipeline/ground_mesh.py:46`); `scene.assets` RAMP / CURB features; intersection
geometry from centerlines.

**Pipeline:** new `pipeline/accessibility.py`:

- **Running slope** = ΔZ between adjacent sidewalk cells / cell length; flag
  > 5%.
- **Cross slope** = lateral ΔZ across the sidewalk width; flag > 2%.
- **Missing curb ramps:** intersection corners with a CURB but no nearby RAMP
  asset.

Emit ADA-violation cells as a distinct object/material for overlay.

**Viewer:** toggleable heatmap of violations; actions **"add curb ramp"**
(places a ramp asset + edit) and **"re-grade"** (marks cells for regrading).

**Cost hook:** `unit_cost.curb_ramp` (per-each), `unit_cost.regrade_sqft` × area.

### 4.5 Cost anticipation engine

**New module `pipeline/cost.py` + catalog `config/unit_costs.yaml`.** Pure
functions, unit-testable in the existing synthetic-fixture style:

```python
def estimate(edits: list[dict], catalog: dict) -> CostReport: ...
```

`CostReport` aggregates line items → itemized + total, exportable as
markdown / CSV / JSON.

**Starter catalog (placeholder figures — tune before any real use):**

```yaml
# config/unit_costs.yaml — illustrative defaults only. Replace with local,
# current figures (municipal bid tabs / DOT unit-cost books) before relying on them.
pavement:
  crack_seal_sqft:        0.50
  mill_and_overlay_sqft:  4.50
  full_depth_recon_sqft: 12.00
curb_linear_ft:           35.00
sidewalk_sqft:            12.00
curb_ramp_each:         2500.00
regrade_sqft:              8.00
relocation:
  utility_pole:         8000.00
  traffic_signal:      15000.00
  hydrant:              4500.00
  sign:                  600.00
  default:              1500.00
```

**Viewer:** a cost panel shows the running total and per-edit breakdown; a
"Download estimate" button exports the report.

---

## 5. Edit / Scenario State Model

A single JSON document is the **source of truth** for a proposal (the viewer's
fragment transforms and recolors are ephemeral; this persists them). Shape:

```json
{
  "block_id": "davis_sq_a",
  "crs": "EPSG:26919",
  "edits": [
    { "op": "repave",   "target": "ground_road_poor_03", "treatment": "mill_and_overlay" },
    { "op": "relocate", "target": "utility_pole_07", "from_utm": [x, y, z], "to_utm": [x, y, z] },
    { "op": "add_ramp", "at_utm": [x, y, z] },
    { "op": "widen",    "segment": "seg_12", "delta_ft": 4 },
    { "op": "regrade",  "cells": ["c_104_88", "c_104_89"] }
  ]
}
```

- The **viewer produces** these edits as the user interacts.
- The **cost engine consumes** them (`estimate(scenario["edits"], catalog)`).
- A small local endpoint persists/loads scenarios — extend the existing
  `viewer/token_server.py` pattern with `GET/POST /api/scenario`, or add a
  sibling FastAPI/Flask app. (Interactive-first, per the design decision; the
  JSON is just the save layer.)
- "Apply scenario" re-runs the affected pipeline stages and regenerates the OBJ
  → new object key → re-translate → reload.

---

## 6. Implementation Roadmap (phased)

Each phase lists files touched / new modules / new tests. Tests follow the
existing `tests/` synthetic-fixture style (no PDAL/data needed — see
`tests/conftest.py`).

**Phase 0 — Foundation.**
CRS reconciliation (single working CRS per block; reproject all inputs). Swap
manual curl downloads for SDK / S3 reads.
*Files:* `pipeline/crop.py`, `pipeline/select_tiles.py`, `config.py`, new
`pipeline/cyvl_source.py`. *Tests:* CRS round-trip, layer load shape.

**Phase 1 — Pavement condition + cost (read-only, fastest win).**
Per-cell condition/distress; condition-binned road objects; cost engine +
catalog; static estimate over the whole block.
*Files:* new `pipeline/pavement.py`, new `pipeline/cost.py`, new
`config/unit_costs.yaml`, edit `pipeline/ground_mesh.py`, `pipeline/to_cad.py`,
`run.py`. *Viewer:* condition coloring, cost panel. *Tests:* spatial join,
`estimate()` line items.

**Phase 2 — Interactive asset repositioning.**
Generalize `moveCar()` → free drag + snap + clearance check; persist to scenario
JSON; per-move cost; scenario endpoint.
*Files:* `viewer/viewer.js`, `viewer/token_server.py` (or new app),
`pipeline/cost.py`. *Tests:* relocation cost, clearance predicate.

**Phase 3 — Accessibility.**
Slope/cross-slope analysis, missing-ramp detection, violation overlay, add-ramp
/ re-grade actions + costs.
*Files:* new `pipeline/accessibility.py`, `pipeline/to_cad.py`, `viewer/viewer.js`,
`pipeline/cost.py`. *Tests:* slope thresholds, ramp-gap detection.

**Phase 4 — Street geometry editing.**
Parameterized road width / realign / bump-outs; partial ROI regeneration.
*Files:* `pipeline/segment.py`, `pipeline/ground_mesh.py`, `run.py`,
`viewer/viewer.js`. *Tests:* buffer-width → area delta.

**Phase 5 — Native CAD hand-off (noted, out of current scope).**
DWG/DXF export via APS Design Automation (headless AutoCAD/Civil 3D) or
`ezdxf` (already in `requirements.txt`) so proposals hand off to civil
workflows. Captured here for the roadmap; **not** part of the current build,
which stays on the SVF2 Viewer path.

---

## 7. APS / Autodesk Integration Notes

Reuse the existing modules **unchanged**:

- `aps/auth.py get_token()` — 2-legged OAuth v2; viewer token stays `data:read`.
- `aps/upload.py` — signed-S3 3-step (`signeds3upload` GET → PUT to S3 →
  POST finalize); URN = unpadded urlsafe-base64 of the objectId.
- `aps/translate.py start_translation()` — OBJ→SVF2; for the zipped OBJ+MTL use
  `compressedUrn: true` + `rootFilename: "scene.obj"` (colors survive) and
  `x-ads-force: true`.

**Caching discipline:** because StreetForge regenerates the scene per scenario,
**version the OSS object key every regen** (e.g. `scene_<block>_<scenario>_<n>.zip`)
or force re-translation; otherwise the viewer shows a stale SVF2.

**Viewer extension points** (`viewer/viewer.js`):

- `SELECTION_CHANGED_EVENT` → already tracks `selected` dbId (line 13).
- `OBJECT_TREE_CREATED_EVENT` → enumerate leaf objects (line 15) — reuse to
  build the per-object edit panels.
- Move: `getFragmentProxy` / `fp.position` / `updateAnimTransform()` /
  `viewer.impl.invalidate(true,true,true)` (lines 94-104) — generalize for drag.
- New toolbar panels: Repair, Relocate, Accessibility, Cost.

---

## 8. Setup, Run & Verification

### Environment (from `README.md`)

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# PDAL fails on pip/macOS — install via conda:
conda install -c conda-forge pdal python-pdal
```

`.env` (gitignored): `APS_CLIENT_ID`, `APS_CLIENT_SECRET`, `LAZ_DIR`,
`AWS_PROFILE=cyvl-hackathon`. Viewer token scope = `data:read`.

### New dependencies (add when implementing)

- `pyyaml` — cost catalog.
- the Cyvl SDK — `pip install -e ../sandbox/cyvl-spatial-sdk` (gives
  `cyvl.load_scene` + layer accessors).
- *(optional)* `fastapi`/`uvicorn` or `flask` — scenario persistence endpoint.

### Run order (extends the existing flow)

1. Phase 0 source: pull tiles + layers via SDK/S3 (replaces the curl step).
2. `python run.py` → classified mesh + condition coloring + cost → upload →
   translate → URN.
3. Paste URN into `viewer/index.html`; `python viewer/token_server.py`; open
   `http://localhost:8080`.
4. Interact: repair / relocate / add ramp; watch the cost panel; export the
   estimate.

### Definition of done (per pillar)

- **Pavement:** road colored by real `scene.pavements.score`; selecting a
  section and choosing a treatment updates color + cost.
- **Geometry:** a width change regenerates the mesh and re-prices.
- **Repositioning:** dragging an asset persists to the scenario and prices the
  move.
- **Accessibility:** violation heatmap matches computed slopes; adding a ramp
  clears the flag and adds its cost.
- **Cost:** itemized report total = sum of all edits; exports to CSV/JSON/MD.

### Verification

- Pipeline modules are pure/unit-testable (`pytest -v`, synthetic fixtures) —
  add tests per phase above.
- APS round-trip proven via the dummy scene first
  (`write_dummy_scene()`), exactly as the fork does, before wiring real edits.

---

## 9. Risks & Open Questions

- **CRS mismatch (26919 vs 32619).** Highest-priority footgun; resolve in
  Phase 0. Always reproject to one working CRS before spatial joins.
- **APS Viewer point-cloud limitation.** Only meshes are selectable/priced; the
  raw cloud is a non-interactive three.js overlay. Anything editable must be a
  mesh object in the OBJ.
- **Street-side scan occlusion.** Vehicle LiDAR sees facades and the near street
  surface; far-side detail, roofs, and back-of-sidewalk grading are partial.
  Affects slope accuracy and asset completeness.
- **Same-pass LiDAR windowing.** `frame.lidar()` defaults to same-pass within a
  ~5 s window; ~2.3% of frames lack their own pass. Coverage gaps may need
  `same_pass=False` merges for full blocks.
- **Cost-catalog accuracy.** Estimates are only as good as the tuned unit costs;
  defaults are illustrative placeholders, not bid-grade numbers.
- **SVF2 caching.** Forgetting to version the object key per regen shows stale
  models — bake key-bumping into the regeneration step.

---

*References (read-only, cited above): `pipeline/ground_mesh.py`,
`pipeline/segment.py`, `pipeline/to_cad.py`, `pipeline/lift_assets.py`,
`aps/*.py`, `viewer/viewer.js`, `run.py`, `config.py`,
`docs/autodesk-integration-summary.md`, and the Cyvl SDK
`sandbox/cyvl-spatial-sdk/src/cyvl/{scene,frame,lidar,measure,geometry}.py`,
`sandbox/cyvl-spatial-sdk/S3_GUIDE.md`.*
