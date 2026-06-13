# Cyvl Point-Cloud Street Segmenter → Autodesk APS

**Design spec · 2026-06-10**
Project: Cyvl × Autodesk hackathon bonus track ("Best Capture-to-Design")
Owner: Xavier
Repo: `~/github/hackathonBuckets`
Time budget: ~5 hours

---

## 0. Read this first — what we are building and why

We take Cyvl's raw 3D LiDAR scan of one block in Somerville (millions of colored
points), **figure out which points are which thing** (ground, road, sidewalk,
cars), and then turn the result into a CAD model we can open, measure, and
**move** inside Autodesk's web Viewer. The headline demo: grab a car, slide it
down the road or delete it, watch the model update live in the browser.

The Autodesk API is not a side detail. It is the point of the project. The whole
story is **capture → understand → design**: Cyvl captures reality, we classify
it, Autodesk is where it becomes an editable design. Section 6 is the most
important section in this document — it is the part the hackathon judges care
about.

### The one architectural fact that shapes everything

**Autodesk's APS Viewer cannot render raw point clouds.** This is the official
Autodesk answer, not a guess (see Section 9, sources). The Viewer is built for
**lines and meshes** only. So we cannot just throw the LiDAR `.laz` file at
Autodesk and call it done.

That single fact splits our pipeline into two halves with two different viewers:

1. **The raw, dense, photoreal half** lives in **Potree** (a free browser point-
   cloud viewer). Cyvl already ships Potree tilesets for every tile, so this is
   nearly free for us. This is where we *show the segmentation* — points colored
   by class.
2. **The clean, editable, CAD half** lives in the **Autodesk APS Viewer**. We
   abstract the messy points into simple shapes — each car becomes a 3D box, the
   road becomes a surface — export that as a mesh file (OBJ), and push *that*
   through the Autodesk cloud into the Viewer. This is where we *move the car*.

Everything below follows from this split.

---

## 1. Inputs — the data we actually have

All confirmed by inspecting the live `s3://cyvl-hackathon` bucket on 2026-06-10.

### 1a. The point cloud (the star of the show)
- 514 LAZ tiles of Somerville, ~473 GB total, streamed from a **public**
  CloudFront CDN (no credentials needed).
- Each point has **XYZ position + RGB color**. Filename pattern:
  `global_xyz_rgb_icgu_<tile>_<start>_<end>.laz`.
- **No semantic classification is baked in.** The points are *not* pre-labeled
  as "car" or "ground." That labeling is the work we do. (If they were
  pre-labeled, this whole project would be a one-line filter — they are not.)
- A manifest `pointclouds/pointclouds_v2.geojson` (514 footprint polygons +
  CDN URLs + centroid lon/lat/alt) lets us pick just the tiles over our area
  and download only those. Tiles are 100–375 MB each — we grab a few, not 514.
- **Download budget: 10 GB default cap (configurable, never silently exceeded).**
  The selector takes an *area* — either a bounding box or a point-A→point-B
  corridor — finds the tiles whose footprints intersect it, sums their sizes
  (HEAD request per tile), and refuses to exceed the cap (default 10 GB; raise it
  explicitly if a bigger corridor is wanted). A one-block demo needs ~1–5 tiles
  (<2 GB), so 10 GB is already generous headroom.
- **Manual download with a fixed path.** The selector only *lists* the chosen
  tiles and emits a ready-to-run download command — it does **not** auto-download.
  Xavier downloads the tiles himself into a folder of his choosing and sets
  `LAZ_DIR` in `.env` to that path. The pipeline reads LAZ from `LAZ_DIR`. Fixed,
  predictable, no surprise 473 GB pull.
- Cyvl also ships a **Potree 2.0** web tileset per tile (`<file>.laz.potree/`),
  entry point `metadata.json`. Free browser viewer, no work to build.

### 1b. The vector layers (our cheat sheet for road vs sidewalk)
11 GeoJSON layers in `data/`, all **EPSG:4326 (WGS84 lon/lat)**. The ones we use:
- `pavements_v2` — 5,080 road-segment **LineStrings** with street name, width
  proxy (`area_sqft`/`length_ft`), condition. This is how we know where the road
  is without re-deriving it from points.
- `sam_v2` — 7,116 pavement-marking LineStrings (lane lines, crosswalks). Helps
  refine the road edge.
- `aboveGroundAssets_v2` — 8,254 **Points** (lat/lon) of surface assets:
  manholes, drains, **fire hydrants**, ramps, etc., each with `asset_type` /
  `Type` and a photo. These are Cyvl's CV detections in 2D.
- `signs_v2` — 3,782 **Points** (lat/lon) of MUTCD-classified traffic signs.
- The point layers above are the input to the **2D→3D asset lifting** described
  below — Cyvl already told us *what* and *where (in 2D)*; we recover the 3D
  geometry from the cloud.

**Key insight:** there is **no "cars" layer**. Cars exist *only* in the raw point
cloud, so car detection must come from the points (heuristic). But for assets
Cyvl *did* detect (hydrants, signs, manholes…), we should not re-detect from
scratch — we **lift the existing 2D detection into 3D**, which is both easier and
more accurate. Two complementary tracks: heuristic for the unknown (cars),
detection-reuse for the known (assets).

### 1b-ii. 2D→3D asset lifting (the cross-reference idea)
Cyvl's CV gives each asset a **lat/lon + label** in 2D but **no Z and no 3D
extent**. We recover those from the point cloud:
1. Reproject the asset's lon/lat (WGS84) into the LAS CRS → an (X, Y).
2. Crop the cloud to a small cylinder/box around (X, Y) (radius ~0.5–1.0 m).
3. The points in that column **are** the asset in 3D. From them we get the
   ground Z, the top Z (height), and an oriented bounding box.
4. Optionally snap to the nearest non-ground cluster from segmentation to clean
   up the extent.

Result: each hydrant/sign/manhole becomes a **located, measurable 3D object
carrying Cyvl's CV label** — more reliable than the car heuristic because the
"what + where" is already solved; we only add Z/extent. These lifted assets are
also exported into the APS Viewer as modeled objects (Section 6). This is the
literal bridge between Cyvl's 2D vector detections, the 3D cloud, and Autodesk.

### 1c. Coordinate systems — the gotcha that will bite if ignored
- LAZ point data is **georeferenced/projected** (likely a US survey foot State
  Plane or UTM zone — confirm with `pdal info` at implementation time).
- The GeoJSON vectors are **WGS84 lon/lat (degrees)**.
- These do **not** line up until reprojected into a common CRS. **Before** we use
  the road vectors to label points, we reproject the vectors into the point
  cloud's CRS (using `pyproj`). Skipping this silently misclassifies everything.

### 1d. Autodesk APS credentials
- We have a working APS app **Client ID + Secret**, stored in
  `~/github/hackathonBuckets/.env` (gitignored, never in chat).
- Free APS account. Model Derivative consumes cloud credits; the Viewer itself is
  free.

---

## 2. Output — definition of done

We are done when all of these are true:

1. **Classified point cloud** of one Somerville block, points colored by class:
   ground = gray, road = dark, sidewalk = tan, cars = yellow. Viewable in Potree
   in the browser.
2. **Extracted cars**: a list of car instances, each with an oriented bounding
   box (position, dimensions, heading).
2b. **Lifted assets**: Cyvl's 2D-detected hydrants/signs/manholes located in 3D
   (X, Y from the vector layer; Z + height from the cloud), each labeled.
3. **CAD model in the Autodesk APS Viewer**: an OBJ containing the road surface +
   one box per car + one modeled shape per lifted asset, uploaded to Autodesk,
   translated to SVF2, and rendered in an APS Viewer web page. Each car and asset
   is a *separate selectable object*.
4. **The money demo**: in the APS Viewer, one car can be (a) deleted or (b)
   translated down the road, live, via the Viewer's transform API.

Stretch (only if ahead of schedule): color the road surface by Cyvl pavement
condition score; animate the car move; add signs as modeled posts.

---

## 3. Architecture — the pipeline end to end

```
                         ┌─────────────────────────────────────────┐
                         │  manifest (pointclouds_v2.geojson)        │
                         └───────────────┬───────────────────────────┘
                                         │  filter by area (bbox / A→B corridor), cap 10 GB default
                                         ▼
   [1] select_tiles.py  ──► prints tile list + sizes + download command (NO auto-download)
                                         │
                          Xavier downloads chosen tiles → LAZ_DIR (fixed path in .env)
                                         │
                                         ▼  laspy load from LAZ_DIR + crop to ~100×100 m
                              [2] segment.py
                                  ├─ ground filter (PDAL SMRF/CSF)  → ground / non-ground
                                  ├─ road vs sidewalk  (reproject Cyvl vectors, buffer,
                                  │                      point-in-polygon on ground pts)
                                  ├─ car detection     (cluster non-ground pts,
                                  │                      size/height filter → car boxes)
                                  └─ asset lifting     (Cyvl 2D points → reproject → cylinder
                                                         crop → 3D labeled asset boxes)
                                         │
                    ┌────────────────────┴─────────────────────┐
                    ▼                                            ▼
   [3a] write classified+colored LAS            [3b] to_cad.py: OBJ
        → view in Potree (raw, photoreal)             ├─ road surface mesh
                                                       ├─ one box mesh per car (named groups)
                                                       └─ one shape per lifted asset (named groups)
                                                            │
                                                            ▼
                                                   [4] aps/ pipeline
                                                       ├─ auth.py        (2-legged OAuth v2)
                                                       ├─ upload.py      (OSS signed-S3 upload)
                                                       └─ translate.py   (Model Derivative → SVF2, poll)
                                                            │  URN
                                                            ▼
                                                   [5] viewer/ (APS Viewer web page)
                                                       └─ load URN, select a car, move/delete it
```

Two outputs, two viewers, on purpose (Section 0). Potree shows the dense reality;
APS shows the editable design.

---

## 4. Components — file by file, what each does and depends on

Each module is small and single-purpose so it can be built and tested alone.

### `pipeline/select_tiles.py`  ← planner, not downloader
- **Does:** reads the manifest, filters tile footprints intersecting the requested
  *area* (a bounding box, or a buffered point-A→point-B corridor), HEADs each
  candidate on the CDN to get its byte size, accumulates until the **size cap
  (default 10 GB)**, and **prints** the chosen tiles with running total + a ready-to-paste download
  command (`curl`/`aws`). It does **not** download anything itself.
- **Why manual:** Xavier downloads the chosen tiles into a folder he picks and
  sets `LAZ_DIR` to it; this guarantees a fixed, known path and prevents an
  accidental 473 GB pull.
- **Input:** area spec (bbox or A/B + corridor width), manifest path, size cap
  (default 10 GB).
- **Output:** printed tile list + sizes + download command + matching
  `.laz.potree/` URLs for Potree. Writes a small `selected_tiles.json` manifest of
  the picks for the rest of the pipeline to read.
- **Depends on:** `requests` (HEAD only), `json`, `shapely`.

### `pipeline/segment.py`  ← the core
- **Does:** the three-step classification.
  1. **Ground filter.** Run PDAL's `filters.smrf` (Simple Morphological Filter)
     or `filters.csf` (Cloth Simulation Filter) to split ground from non-ground.
     This is a standard, well-tested LiDAR operation — minutes of work, not
     research.
  2. **Road vs sidewalk.** Reproject `pavements_v2` (+`sam_v2`) lines into the
     LAS CRS, buffer each road line by half its width to get road polygons, then
     for each *ground* point test point-in-polygon: inside = road, outside =
     sidewalk/other. (`shapely`/`geopandas` with an STRtree spatial index so it
     is fast over millions of points.)
  3. **Car detection (the heuristic).** On the *non-ground* points: Euclidean
     clustering (scikit-learn `DBSCAN`, or PDAL `filters.cluster`) to group points
     into objects. Keep a cluster as a *car* if its oriented bounding box fits car
     proportions — roughly length 2–6 m, width 1.4–2.2 m, height 1.2–2.2 m, base
     near ground level. RGB and point density are tie-breakers. Output an oriented
     bounding box per kept cluster. (See Section 7 for the honest accuracy story.)
- **Output:** per-point class array; list of car boxes (center, size, heading).
- **Depends on:** `laspy`, `pdal`, `numpy`, `shapely`/`geopandas`, `scikit-learn`,
  `pyproj`.

### `pipeline/lift_assets.py`  ← 2D CV detections → 3D
- **Does:** the cross-reference described in Section 1b-ii. Loads `aboveGroundAssets_v2`
  (hydrants, manholes, drains…) and `signs_v2`, filters to the block bbox,
  reprojects each point's lon/lat into the LAS CRS, crops a small cylinder
  (radius ~0.5–1.0 m) of cloud points around each (X, Y), and from that column
  derives ground Z, top Z (height), and an oriented bounding box. Emits a labeled
  3D asset per detection (`{type, label, x, y, z, height, bbox}`).
- **Why separate from car detection:** assets reuse Cyvl's existing CV labels
  (accurate "what + where"); only Z/extent is recovered. Cars have no 2D layer, so
  they stay in `segment.py`'s heuristic clustering.
- **Input:** LAS (cropped), `aboveGroundAssets_v2`/`signs_v2`, LAS CRS, crop radius.
- **Output:** list of labeled 3D assets.
- **Depends on:** `laspy`, `numpy`, `geopandas`, `pyproj`, `scipy` (KD-tree for the
  radius crop).

### `pipeline/to_cad.py`
- **Does:** turns the abstract result into a mesh file Autodesk can ingest.
  - Road surface: triangulate the road-classified ground points (or simply a flat
    extruded slab from the road polygons) into a mesh.
  - Cars: one box mesh per detected car, written as a **named OBJ group**
    (`o car_01`, `o car_02`, …) so each becomes a *separate selectable object* in
    the Viewer — required for the move/delete demo.
  - Lifted assets: one simple modeled shape per asset, also a named group
    (`o hydrant_01` = small cylinder + dome, `o sign_03` = thin post + panel,
    `o manhole_02` = flat disc), sized from the recovered 3D extent and labeled
    with Cyvl's type so it is selectable/identifiable in the Viewer.
- **Why OBJ:** OBJ is one of Model Derivative's 60 supported inputs, is a trivial
  text mesh format we can hand-write, reliably translates to a 3D SVF2 model, and
  its `o`/`g` groups survive as distinct Viewer objects (dbIds).
- **Output:** `out/scene.obj` (+ `.mtl` for the class colors).
- **Depends on:** `numpy`, `trimesh` (or hand-written OBJ writer), `ezdxf`
  (optional — also emit a `.dxf` "CAD deliverable" for credibility).

### `aps/auth.py`
- **Does:** 2-legged OAuth. `POST {APS_HOST}/authentication/v2/token` with header
  `Authorization: Basic base64(CLIENT_ID:CLIENT_SECRET)` and body
  `grant_type=client_credentials&scope=data:read data:write data:create
  bucket:create bucket:read viewer:read`. Returns a bearer token (~1 h TTL).
- **Depends on:** `requests`, `.env` (`python-dotenv`).

### `aps/upload.py`
- **Does:** OSS (Object Storage Service). Create a bucket
  (`POST /oss/v2/buckets`), then upload `scene.obj` using the **signed-S3 upload
  flow** (`GET .../objects/:name/signeds3upload` → `PUT` to the returned S3 URL →
  `POST .../signeds3upload` to finalize). Returns the object's URN
  (base64 of the objectId) — the handle everything downstream uses.
  *Note:* Autodesk deprecated the old direct `PUT object` endpoint in favor of
  signed-S3 upload; confirm the exact shape against current docs at build time.
- **Depends on:** `aps/auth.py`, `requests`.

### `aps/translate.py`
- **Does:** Model Derivative. `POST /modelderivative/v2/designdata/job` with body
  `{input:{urn}, output:{formats:[{type:'svf2', views:['2d','3d']}]}}` and header
  `x-ads-force: true`. Then poll
  `GET /modelderivative/v2/designdata/:urn/manifest` until `status: success`.
  Prints the URN for the Viewer.
- **Depends on:** `aps/auth.py`, `aps/upload.py`, `requests`.

### `viewer/` (static web page: `index.html` + `viewer.js`)
- **Does:** loads the Autodesk Viewer JS SDK, authenticates (token from a tiny
  local endpoint or pasted), `Autodesk.Viewing.Document.load("urn:"+URN)`, shows
  the model. UI: click a car to select it, then a button to **Delete** (hide the
  fragment) or **Move** (apply a translation via `setPlacementTransform` /
  `FragmentPointer` on that car's dbId, then `viewer.impl.invalidate(true,true,true)`).
- **Depends on:** APS Viewer SDK (CDN script), a token from `aps/auth.py`.

### Supporting files
- `.env` — APS creds + `AWS_PROFILE=cyvl-hackathon` + `LAZ_DIR=<path Xavier
  downloads tiles to>` (done/gitignored; `LAZ_DIR` added when path is known).
- `.gitignore` — excludes `.env`, data, build output (done).
- `requirements.txt` — `laspy[laz]`, `pdal`/`python-pdal`, `numpy`, `scipy`,
  `shapely`, `geopandas`, `pyproj`, `scikit-learn`, `trimesh`, `ezdxf`,
  `requests`, `python-dotenv`.
- `README.md` — run order, the two-viewer explanation.

---

## 5. Data flow — one trip through the pipeline

1. Pick area (Davis Square block default, ~42.3967, -71.1218). `select_tiles.py`
   lists the intersecting tiles under the 50 GB cap and prints the download
   command. **Xavier downloads** the chosen tiles into `LAZ_DIR` and tells the
   pipeline that path.
2. `segment.py` loads from `LAZ_DIR` with `laspy`, crops to ~100×100 m, runs the
   ground filter, reprojects + buffers the Cyvl road vectors to label road vs
   sidewalk on ground points, then clusters non-ground points and keeps
   car-shaped boxes. `lift_assets.py` cross-references Cyvl's 2D hydrant/sign/
   manhole points into 3D over the same crop.
3. `segment.py` writes a classified + recolored LAS. We point a Potree viewer at
   it (or at Cyvl's prebuilt tileset for context) — **Potree demo ready.**
4. `to_cad.py` builds the road-surface mesh + one named box per car → `scene.obj`.
5. `aps/auth.py` gets a token; `aps/upload.py` creates a bucket and uploads
   `scene.obj`; `aps/translate.py` translates it to SVF2 and returns the URN.
6. `viewer/index.html` loads the URN. We select a car, hit Move/Delete — **APS
   demo ready.**

---

## 6. How we use the Autodesk API (the heart of the project)

This is the section the judges care about. Four APS APIs, in order:

### 6.1 Authentication (OAuth v2) — *the front door*
Every APS call needs a bearer token. We use **2-legged** (app-only, "Client
Credentials" grant) because there is no human user to log in — our script *is* the
client. Migration note grounded in Autodesk docs: **v2 passes the client id/secret
in the `Authorization: Basic` header**, not the body (v1 behavior). Endpoint:
`POST https://developer.api.autodesk.com/authentication/v2/token`.

### 6.2 Data Management / OSS — *cloud storage for our file*
Model Derivative only translates files that already live in Autodesk's cloud, so
first we put `scene.obj` into an **OSS bucket** (Object Storage Service). Create
bucket → upload object (signed-S3 flow) → receive an **objectId**, which we
base64-encode into a **URN**. The URN is the identity of our model across every
later call.

### 6.3 Model Derivative — *turn our OBJ into something the Viewer reads*
The Viewer does not read OBJ directly; it reads Autodesk's streaming format
**SVF2**. Model Derivative is the translator. We POST a translation **job**
(input = our URN, output = SVF2, 3D view), then **poll the manifest** until it
reports success. Autodesk supports **60 input formats**; OBJ is one, and it yields
a 3D SVF2 model. This is the literal "capture-to-design" bridge: a file derived
from a Cyvl scan becomes an Autodesk-native design.

### 6.4 Viewer — *show it, and move the car*
The APS **Viewer** is a free WebGL/JavaScript library. We load our URN and get an
interactive, measurable 3D model in the browser. Because we wrote each car *and
each lifted asset* (hydrant, sign, manhole) as a separate named OBJ object, each
is a distinct selectable element (dbId/fragment) in the Viewer — click a hydrant
and it identifies itself with Cyvl's CV label. To **move** a car we apply a transform — Autodesk's documented mechanisms
are `model.setPlacementTransform()` for whole-model placement and the
`FragmentPointer` / fragment-proxy API
(`viewer.impl.getFragmentProxy(model, fragId)`, set the world matrix, then
`viewer.impl.invalidate(true, true, true)`) for individual objects. To **delete**,
we hide that fragment. This is the "what if it moved?" payoff, running entirely in
Autodesk's Viewer.

### 6.5 What we deliberately do NOT use, and why
- **Reality Capture API** — turns *images* into a mesh/point cloud. We already
  *have* point clouds; running photogrammetry would be redundant and slow.
- **Design Automation (headless Civil 3D/AutoCAD)** — the "real Civil 3D corridor"
  path. Powerful but needs an authored AppBundle and likely will not finish in
  5 hours. Out of scope; noted as a future direction.
- **Pushing raw LAZ to the Viewer** — impossible (Viewer has no point-cloud
  support). This is exactly why the abstraction-to-mesh step exists.

---

## 7. The honest accuracy story (heuristic car detection)

We chose the heuristic method over a pretrained ML model because the ML route
(CUDA setup + weights + domain mismatch: most LiDAR seg nets are trained on
roof-mounted spinning LiDAR, not Cyvl's sensor) would likely eat the entire
5-hour budget on environment alone.

What the heuristic gets right and wrong:
- **Ground vs non-ground:** reliable. SMRF/CSF are industry-standard.
- **Road vs sidewalk:** good, because we lean on Cyvl's real road vectors rather
  than guessing from points.
- **Cars:** ~good-enough for a demo, not production. A size/shape filter on
  clusters will catch most parked cars but will also grab some look-alikes
  (dumpsters, large bushes, utility cabinets) and may split/merge cars that touch
  or are partly occluded. We tune the size thresholds on the chosen block and
  accept residual error. We will **say this out loud** in the demo rather than
  imply perfect detection.

Mitigations if a cluster is ambiguous: use RGB (cars are uniform-colored panels),
height-above-ground, and footprint rectangularity as extra filters.

---

## 8. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| PDAL hard to install on macOS | Medium | Use `conda`/`pixi` for PDAL, or fall back to a pure-Python CSF ground filter on `laspy` arrays. |
| LAS CRS unknown / mismatched with vectors | High if ignored | `pdal info` first; reproject vectors with `pyproj` into LAS CRS before buffering. |
| Car detector false positives/negatives | Medium | Tune size/RGB filters on the block; disclose limits in demo. |
| OSS upload endpoint shape changed (signed-S3) | Medium | Verify against current OSS docs at build; the signed-S3 flow is the current method. |
| Model Derivative credit limits / quota | Low-Med | Translate once, reuse the URN; do not re-translate the same file. |
| OBJ groups don't become separate Viewer objects | Low | Confirm with a 2-car test OBJ before building the full scene; fall back to per-car separate OBJs if needed. |
| 2D asset lat/lon offset from true 3D position | Medium | Use a crop radius (~0.5–1 m) that tolerates small offsets; snap to nearest non-ground cluster; widen radius if empty. |
| HEAD sizing slow over many tiles | Low | HEAD is header-only (fast); cache sizes in `selected_tiles.json`; a one-block area is only a handful of tiles anyway. |
| 5h overrun | Medium | MVP gate: Potree-colored cloud + one car in APS Viewer that moves. Everything else is stretch. |

---

## 9. Sources (verified 2026-06-10)

- APS Viewer has **no point-cloud support** (line/mesh only):
  https://aps.autodesk.com/blog/basic-point-clouds-forge-viewer
- Moving/transforming objects in the Viewer (`setPlacementTransform`, fragment
  proxies): https://aps.autodesk.com/blog/dynamic-model-placement ·
  https://aps.autodesk.com/blog/know-how-complex-component-transformations-viewer-part-1-basics
- 2-legged OAuth v2 (client credentials, Basic auth header):
  https://aps.autodesk.com/en/docs/oauth/v2/tutorials/get-2-legged-token ·
  https://aps.autodesk.com/blog/migration-guide-oauth2-v1-v2
- Model Derivative supports 60 input formats → SVF2:
  https://aps.autodesk.com/en/docs/model-derivative/v2/developers_guide/supported-translations ·
  https://aps.autodesk.com/blog/theres-table-derivable-file-formats-autodesk-forge-model-derivative-api
- Cyvl data facts: from `s3://cyvl-hackathon` `index.md`, `schemas.md`,
  `pointclouds/README.md` (inspected live).

---

## 10. Rough 5-hour budget

Autodesk is top priority, so the APS round-trip is built and proven **first** with
a dummy OBJ, before any LiDAR work.

| Time | Work |
|---|---|
| 0:00–0:30 | Env + deps; OBJ writer + dummy 2-box scene; verify APS auth returns a token. |
| 0:30–1:30 | APS: OSS upload + Model Derivative; **Viewer renders the dummy model in the browser** (Autodesk proven). |
| 1:30–2:00 | APS Viewer: select + move/delete a car (Autodesk demo done). |
| 2:00–2:30 | `select_tiles.py` pick area under 10 GB; Xavier downloads to `LAZ_DIR`; `pdal info` a tile. |
| 2:30–4:00 | `segment.py` ground/road/sidewalk/cars + `lift_assets.py` 2D→3D; colored LAS; Potree view (second demo). |
| 4:00–5:00 | `run.py` swaps the dummy OBJ for the real scene; re-upload/translate; polish. |
