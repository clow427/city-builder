# Cyvl Point-Cloud Street Segmenter → Autodesk APS — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Classify one Somerville block's Cyvl LiDAR into ground/road/sidewalk/cars + lift Cyvl's 2D CV asset detections (hydrants/signs/manholes) into 3D, then export an OBJ that is uploaded through the Autodesk APS cloud and rendered in the APS Viewer where individual cars can be moved or deleted.

**Architecture:** Two-viewer split forced by one fact — the APS Viewer cannot render raw point clouds. Raw classified points are shown in Potree; abstracted CAD meshes (road surface + per-car boxes + per-asset shapes) go through APS (OAuth → OSS → Model Derivative → Viewer). Pure geometry/IO logic is unit-tested; PDAL and network steps are smoke-tested.

**Build order (Autodesk-first):** Autodesk integration is top priority, so we prove the *entire* APS round-trip with a hand-made dummy OBJ FIRST (auth → upload → translate → **Viewer renders a model**), then add move/delete, and only then build the real segmentation pipeline that swaps the dummy OBJ for the real scene. This guarantees the Autodesk demo exists early and de-risked, regardless of how far the segmentation gets.

**Tech Stack:** Python 3.11 (`laspy`, `pdal`, `numpy`, `scipy`, `shapely`, `geopandas`, `pyproj`, `scikit-learn`, `trimesh`, `ezdxf`, `requests`, `python-dotenv`), pytest, Autodesk APS (Authentication v2, OSS, Model Derivative, Viewer JS SDK), Potree.

**Spec:** `docs/superpowers/specs/2026-06-10-cyvl-pointcloud-segmenter-aps-design.md`

---

## File Structure

```
hackathonBuckets/
  .env                       # done — APS creds, AWS_PROFILE, LAZ_DIR (added in Task 7)
  .gitignore                 # done
  requirements.txt           # Task 0
  config.py                  # Task 0
  pipeline/
    __init__.py
    to_cad.py                # Task 1 — OBJ writer (early: makes dummy + real scenes)
    select_tiles.py          # Task 7
    crop.py                  # Task 8
    segment.py               # Tasks 9-11
    lift_assets.py           # Task 12
    classify_io.py           # Task 13
  aps/
    __init__.py
    auth.py                  # Task 2 — 2-legged OAuth v2
    upload.py                # Task 3 — OSS bucket + signed-S3 upload
    translate.py             # Task 4 — Model Derivative job + poll
  viewer/
    index.html               # Task 5 — APS Viewer page (load), Task 6 (move/delete UI)
    viewer.js                # Task 5 (load), Task 6 (move/delete)
    token_server.py          # Task 5
  tests/
    conftest.py              # Task 0
    test_to_cad.py           # Task 1
    test_auth.py             # Task 2
    test_select_tiles.py     # Task 7
    test_crop.py             # Task 8
    test_segment.py          # Tasks 9-11
    test_lift_assets.py      # Task 12
  out/                       # gitignored — scene.obj, classified.laz
  run.py                     # Task 14
  README.md                  # Task 14
```

Pure logic (OBJ text, auth header, filtering, cropping, cluster filters) is separated from IO/network so it unit-tests without data or credentials.

---

## Task 0: Project scaffold

**Files:**
- Create: `requirements.txt`, `config.py`, `pipeline/__init__.py`, `aps/__init__.py`, `tests/conftest.py`

- [ ] **Step 1: Write `requirements.txt`**

```
laspy[laz]==2.5.4
pdal==3.4.5
numpy==1.26.4
scipy==1.13.1
shapely==2.0.5
geopandas==1.0.1
pyproj==3.6.1
scikit-learn==1.5.1
trimesh==4.4.3
ezdxf==1.3.3
requests==2.32.3
python-dotenv==1.0.1
pytest==8.3.2
```

- [ ] **Step 2: Create a virtualenv and install**

Run:
```bash
cd /Users/xaviercyvl/github/hackathonBuckets
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```
Expected: all install. If `pdal` pip wheel fails on macOS, use conda: `conda create -n hack -c conda-forge python=3.11 pdal python-pdal` then `pip install` the rest. (PDAL is the only likely-painful dep — spec Section 8. It is NOT needed until Task 9, so do not block the Autodesk tasks on it.)

- [ ] **Step 3: Write `config.py`**

```python
import os
from dotenv import load_dotenv

load_dotenv()

# Autodesk APS
APS_HOST = "https://developer.api.autodesk.com"
APS_CLIENT_ID = os.environ.get("APS_CLIENT_ID", "")
APS_CLIENT_SECRET = os.environ.get("APS_CLIENT_SECRET", "")
APS_SCOPES = "data:read data:write data:create bucket:create bucket:read viewer:read"

# Cyvl data
AWS_PROFILE = os.environ.get("AWS_PROFILE", "cyvl-hackathon")
BUCKET = "s3://cyvl-hackathon"
LAZ_DIR = os.environ.get("LAZ_DIR", "")  # where Xavier downloaded the tiles

# Default work area: one block around Davis Square (lon/lat WGS84)
DEFAULT_BBOX = (-71.1235, 42.3955, -71.1200, 42.3980)

# Tile download budget
DEFAULT_CAP_GB = 10.0
```

- [ ] **Step 4: Write `tests/conftest.py`**

```python
import numpy as np
import pytest


@pytest.fixture
def synthetic_manifest():
    def feat(lon, lat, name):
        return {
            "properties": {
                "filename": name, "lon": lon, "lat": lat,
                "baseUrl": "https://cdn.example", "lasPath": f"/x/{name}",
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [lon - 0.001, lat - 0.001], [lon + 0.001, lat - 0.001],
                    [lon + 0.001, lat + 0.001], [lon - 0.001, lat + 0.001],
                    [lon - 0.001, lat - 0.001],
                ]],
            },
        }
    return {"features": [
        feat(-71.1218, 42.3967, "a.laz"),
        feat(-71.1210, 42.3970, "b.laz"),
        feat(-71.0000, 42.0000, "far.laz"),
    ]}


@pytest.fixture
def synthetic_points():
    rng = np.random.default_rng(0)
    ground = np.column_stack([
        rng.uniform(0, 20, 4000), rng.uniform(0, 20, 4000), np.zeros(4000)])
    car = np.column_stack([
        rng.uniform(4, 8.5, 1500),
        rng.uniform(4.5, 6.3, 1500),
        rng.uniform(0.1, 1.6, 1500),
    ])
    return ground, car
```

- [ ] **Step 5: Commit**

```bash
git init
git add requirements.txt config.py pipeline/__init__.py aps/__init__.py tests/conftest.py .gitignore
git commit -m "chore: scaffold cyvl-aps point-cloud project"
```
(`.env` is gitignored — confirm `git status` does NOT list it before committing.)

---

## Task 1: OBJ writer (built early — feeds the dummy AND real scenes)

**Files:**
- Create: `pipeline/to_cad.py`, `tests/test_to_cad.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_to_cad.py
from pipeline.to_cad import box_obj, write_obj


def test_box_obj_has_8_vertices_named_group():
    text, nverts = box_obj("car_01", center=(0, 0, 0.75), size=(4.5, 1.8, 1.5), voffset=0)
    assert text.startswith("o car_01")
    assert text.count("\nv ") == 8
    assert nverts == 8


def test_write_obj_offsets_face_indices(tmp_path):
    objs = [
        {"name": "car_01", "kind": "box", "center": (0, 0, 0.75), "size": (4.5, 1.8, 1.5)},
        {"name": "car_02", "kind": "box", "center": (10, 0, 0.75), "size": (4.5, 1.8, 1.5)},
    ]
    p = tmp_path / "scene.obj"
    write_obj(str(p), objs)
    body = p.read_text()
    assert body.count("o car_") == 2
    assert " 9 " in body  # second box references vertices 9..16
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/test_to_cad.py -v` → FAIL (no module).

- [ ] **Step 3: Implement `pipeline/to_cad.py`**

```python
_CUBE = [(-.5, -.5, -.5), (.5, -.5, -.5), (.5, .5, -.5), (-.5, .5, -.5),
         (-.5, -.5, .5), (.5, -.5, .5), (.5, .5, .5), (-.5, .5, .5)]
_FACES = [(1, 2, 3, 4), (5, 6, 7, 8), (1, 2, 6, 5),
          (2, 3, 7, 6), (3, 4, 8, 7), (4, 1, 5, 8)]


def box_obj(name, center, size, voffset):
    """OBJ text for one named box. voffset = vertices already written."""
    cx, cy, cz = center
    sx, sy, sz = size
    lines = [f"o {name}"]
    for ox, oy, oz in _CUBE:
        lines.append(f"v {cx + ox * sx:.4f} {cy + oy * sy:.4f} {cz + oz * sz:.4f}")
    for a, b, c, d in _FACES:
        lines.append(f"f {a + voffset} {b + voffset} {c + voffset} {d + voffset}")
    return "\n".join(lines) + "\n", 8


def write_obj(path, objects):
    """Write a multi-object OBJ. Each object: {name, kind:'box', center, size}."""
    chunks, voffset = [], 0
    for o in objects:
        if o["kind"] == "box":
            text, n = box_obj(o["name"], o["center"], o["size"], voffset)
            chunks.append(text)
            voffset += n
    open(path, "w").write("".join(chunks))


def cars_to_objects(car_boxes):
    objs = []
    for i, b in enumerate(car_boxes, 1):
        objs.append({"name": f"car_{i:02d}", "kind": "box",
                     "center": tuple(b["center"]),
                     "size": (b["length"], b["width"], b["height"])})
    return objs


def assets_to_objects(assets):
    objs = []
    for i, a in enumerate(assets, 1):
        h = max(a["height"], 0.3)
        objs.append({"name": f'{a["type"].lower()}_{i:02d}', "kind": "box",
                     "center": (a["x"], a["y"], a["ground_z"] + h / 2),
                     "size": (0.6, 0.6, h)})
    return objs


def write_dummy_scene(path="out/scene.obj"):
    """Two boxes so the APS round-trip can be tested before segmentation exists."""
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_obj(path, [
        {"name": "car_01", "kind": "box", "center": (0, 0, 0.75), "size": (4.5, 1.8, 1.5)},
        {"name": "car_02", "kind": "box", "center": (8, 0, 0.75), "size": (4.5, 1.8, 1.5)},
    ])
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_to_cad.py -v` → 2 passed.

- [ ] **Step 5: Generate the dummy scene for the APS tasks**

Run: `python -c "from pipeline.to_cad import write_dummy_scene; write_dummy_scene()"`
Expected: `out/scene.obj` exists with two boxes.

- [ ] **Step 6: Commit**

```bash
git add pipeline/to_cad.py tests/test_to_cad.py
git commit -m "feat: OBJ writer with named groups + dummy scene"
```

---

## Task 2: APS authentication (2-legged OAuth v2)

**Files:**
- Create: `aps/auth.py`, `tests/test_auth.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_auth.py
import base64
from aps.auth import basic_auth_header


def test_basic_auth_header_base64_of_id_colon_secret():
    h = basic_auth_header("abc", "xyz")
    assert h == "Basic " + base64.b64encode(b"abc:xyz").decode()
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/test_auth.py -v` → FAIL.

- [ ] **Step 3: Implement `aps/auth.py`**

```python
import base64
import requests
from config import APS_HOST, APS_CLIENT_ID, APS_CLIENT_SECRET, APS_SCOPES


def basic_auth_header(client_id, client_secret):
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def get_token(scopes=APS_SCOPES):
    """2-legged client-credentials token. POST /authentication/v2/token, Basic auth header."""
    r = requests.post(
        f"{APS_HOST}/authentication/v2/token",
        headers={
            "Authorization": basic_auth_header(APS_CLIENT_ID, APS_CLIENT_SECRET),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": scopes},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_auth.py -v` → PASS.

- [ ] **Step 5: Smoke-test against the live endpoint**

Run: `python -c "from aps.auth import get_token; print(get_token()[:12], '...')"`
Expected: prints a token prefix. 401/invalid_client → recheck `.env` keys. **Confirms the Autodesk credentials work.**

- [ ] **Step 6: Commit**

```bash
git add aps/auth.py tests/test_auth.py
git commit -m "feat: APS 2-legged OAuth v2 token"
```

---

## Task 3: APS OSS upload (signed-S3)

**Files:**
- Create: `aps/upload.py`

- [ ] **Step 1: Implement `aps/upload.py`**

(Network IO — verified by Step 2 smoke run. Endpoint shapes per current OSS docs; the direct PUT object endpoint is deprecated in favor of signed-S3.)

```python
import base64
import requests
from config import APS_HOST


def ensure_bucket(token, bucket_key):
    r = requests.post(
        f"{APS_HOST}/oss/v2/buckets",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"bucketKey": bucket_key, "policyKey": "transient"},
        timeout=30,
    )
    if r.status_code not in (200, 409):  # 409 = already exists
        r.raise_for_status()


def upload_object(token, bucket_key, object_key, file_path):
    """Signed-S3 upload: get URL -> PUT to S3 -> complete. Returns URN (base64 objectId)."""
    base = f"{APS_HOST}/oss/v2/buckets/{bucket_key}/objects/{object_key}"
    h = {"Authorization": f"Bearer {token}"}
    signed = requests.get(f"{base}/signeds3upload", headers=h, timeout=30).json()
    upload_key = signed["uploadKey"]
    put_url = signed["urls"][0]
    with open(file_path, "rb") as f:
        requests.put(put_url, data=f, timeout=120).raise_for_status()
    done = requests.post(
        f"{base}/signeds3upload",
        headers={**h, "Content-Type": "application/json"},
        json={"uploadKey": upload_key},
        timeout=30,
    )
    done.raise_for_status()
    object_id = done.json()["objectId"]
    return base64.urlsafe_b64encode(object_id.encode()).decode().rstrip("=")
```

- [ ] **Step 2: Smoke-test upload (uses the dummy scene.obj from Task 1)**

Run:
```bash
python -c "
from aps.auth import get_token; from aps.upload import ensure_bucket, upload_object
t=get_token(); b='cyvl-hack-xavier'
ensure_bucket(t,b); print('URN:', upload_object(t,b,'scene.obj','out/scene.obj'))
"
```
Expected: prints a URN.

- [ ] **Step 3: Commit**

```bash
git add aps/upload.py
git commit -m "feat: APS OSS bucket + signed-S3 upload"
```

---

## Task 4: APS Model Derivative (translate + poll)

**Files:**
- Create: `aps/translate.py`

- [ ] **Step 1: Implement `aps/translate.py`**

```python
import time
import requests
from config import APS_HOST


def start_translation(token, urn):
    r = requests.post(
        f"{APS_HOST}/modelderivative/v2/designdata/job",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "x-ads-force": "true"},
        json={"input": {"urn": urn},
              "output": {"formats": [{"type": "svf2", "views": ["2d", "3d"]}]}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def wait_until_done(token, urn, timeout_s=600, interval_s=10):
    url = f"{APS_HOST}/modelderivative/v2/designdata/{urn}/manifest"
    waited = 0
    while waited < timeout_s:
        m = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
        status = m.get("status")
        print("translate status:", status, m.get("progress"))
        if status == "success":
            return m
        if status == "failed":
            raise RuntimeError(f"translation failed: {m}")
        time.sleep(interval_s)
        waited += interval_s
    raise TimeoutError("translation did not finish in time")
```

- [ ] **Step 2: Smoke-test translation (reuse URN from Task 3)**

Run:
```bash
python -c "
from aps.auth import get_token; from aps.translate import start_translation, wait_until_done
t=get_token(); urn='<paste URN from Task 3>'
start_translation(t,urn); wait_until_done(t,urn); print('done')
"
```
Expected: status progresses to `success`. Do NOT re-translate the same URN (saves credits).

- [ ] **Step 3: Commit**

```bash
git add aps/translate.py
git commit -m "feat: APS Model Derivative translate to SVF2 + poll"
```

---

## Task 5: APS Viewer page — load + render the model  ← TOP-PRIORITY MILESTONE

**Files:**
- Create: `viewer/index.html`, `viewer/viewer.js`, `viewer/token_server.py`

At the end of this task the dummy two-box OBJ is visible and orbitable in the
Autodesk APS Viewer in the browser. The Autodesk integration is now proven
end-to-end before any segmentation work.

- [ ] **Step 1: Implement `viewer/token_server.py`**

```python
import http.server, socketserver, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aps.auth import get_token

class H(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/token":
            tok = get_token("viewer:read")
            body = json.dumps({"access_token": tok, "expires_in": 3600}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(body); return
        return super().do_GET()

os.chdir(os.path.dirname(os.path.abspath(__file__)))
print("open http://localhost:8080/index.html")
socketserver.TCPServer(("", 8080), H).serve_forever()
```

- [ ] **Step 2: Implement `viewer/index.html` (load only — no move UI yet)**

```html
<!doctype html><html><head>
<meta charset="utf-8">
<link rel="stylesheet" href="https://developer.api.autodesk.com/modelderivative/v2/viewers/7.*/style.min.css">
<script src="https://developer.api.autodesk.com/modelderivative/v2/viewers/7.*/viewer3D.min.js"></script>
<style>#v{position:absolute;inset:0}</style>
</head><body>
<div id="v"></div>
<script>const URN="<paste URN from Task 4>";</script>
<script src="viewer.js"></script>
</body></html>
```

- [ ] **Step 3: Implement `viewer/viewer.js` (load only)**

```javascript
let viewer;

Autodesk.Viewing.Initializer({
  env: "AutodeskProduction2", api: "streamingV2",
  getAccessToken: cb => fetch("/api/token").then(r => r.json())
    .then(t => cb(t.access_token, t.expires_in)),
}, () => {
  viewer = new Autodesk.Viewing.GuiViewer3D(document.getElementById("v"));
  viewer.start();
  Autodesk.Viewing.Document.load("urn:" + URN, doc => {
    viewer.loadDocumentNode(doc, doc.getRoot().getDefaultGeometry());
  }, err => console.error("load failed", err));
});
```

- [ ] **Step 4: Verify in the browser (manual)**

Run: `python viewer/token_server.py`, open `http://localhost:8080/index.html`.
Expected: the two dummy boxes load and can be orbited. **Autodesk integration proven.** If the model is empty, confirm the URN translated to 3D (Task 4 manifest had a 3D derivative).

- [ ] **Step 5: Commit**

```bash
git add viewer/index.html viewer/viewer.js viewer/token_server.py
git commit -m "feat: APS Viewer page renders the model"
```

---

## Task 6: APS Viewer — select + move/delete a car

**Files:**
- Modify: `viewer/index.html`, `viewer/viewer.js`

- [ ] **Step 1: Add the control bar to `viewer/index.html`**

Insert just inside `<body>` (before `<div id="v">`):
```html
<div style="position:absolute;z-index:9;top:8px;left:8px">
  <button onclick="moveCar()">Move selected car</button>
  <button onclick="deleteCar()">Delete selected car</button>
</div>
```

- [ ] **Step 2: Append selection + move/delete to `viewer/viewer.js`**

Add inside the Initializer callback, after `viewer.loadDocumentNode(...)` is set up,
register selection tracking; and add the two functions at file scope:

```javascript
// at file scope (top-level), add:
let selected = null;

// inside the Initializer callback, after viewer.start():
viewer.addEventListener(Autodesk.Viewing.SELECTION_CHANGED_EVENT,
  e => selected = e.dbIdArray[0] ?? null);

// at file scope, add:
function moveCar() {
  if (selected == null) return alert("Select a car first");
  const tree = viewer.model.getInstanceTree();
  tree.enumNodeFragments(selected, fragId => {
    const fp = viewer.impl.getFragmentProxy(viewer.model, fragId);
    fp.getAnimTransform();
    fp.position.x += 5;            // slide 5 m along X
    fp.updateAnimTransform();
  });
  viewer.impl.invalidate(true, true, true);
}

function deleteCar() {
  if (selected == null) return alert("Select a car first");
  viewer.hide(selected);
}
```

- [ ] **Step 3: Verify in the browser (manual)**

Run: `python viewer/token_server.py`, open the page.
Expected: click a box → it selects; **Move** slides it 5 m; **Delete** hides it. **Second demo checkpoint — the "what if it moved" payoff.**

- [ ] **Step 4: Commit**

```bash
git add viewer/index.html viewer/viewer.js
git commit -m "feat: move/delete selected car in APS Viewer"
```

---

## Task 7: Tile selector (planner, 10 GB budget)

**Files:**
- Create: `pipeline/select_tiles.py`, `tests/test_select_tiles.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_select_tiles.py
from pipeline.select_tiles import tiles_in_bbox, corridor_bbox, select_under_budget


def test_tiles_in_bbox_keeps_only_intersecting(synthetic_manifest):
    bbox = (-71.1235, 42.3955, -71.1200, 42.3980)
    names = [t["filename"] for t in tiles_in_bbox(synthetic_manifest, bbox)]
    assert "a.laz" in names and "b.laz" in names
    assert "far.laz" not in names


def test_corridor_bbox_spans_two_points_with_margin():
    minlon, minlat, maxlon, maxlat = corridor_bbox((-71.123, 42.395), (-71.120, 42.398), 50)
    assert minlon < -71.123 and maxlon > -71.120
    assert minlat < 42.395 and maxlat > 42.398


def test_select_under_budget_stops_before_cap():
    tiles = [{"filename": f"{i}.laz", "size": 3_000_000_000} for i in range(10)]
    picked = select_under_budget(tiles, cap_bytes=10_000_000_000)
    assert len(picked) == 3
    assert sum(t["size"] for t in picked) <= 10_000_000_000
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/test_select_tiles.py -v` → FAIL.

- [ ] **Step 3: Implement `pipeline/select_tiles.py`**

```python
import json
import math
import sys
import requests
from shapely.geometry import shape, box
from config import DEFAULT_BBOX, DEFAULT_CAP_GB


def tiles_in_bbox(manifest, bbox):
    region = box(*bbox)
    return [f["properties"] for f in manifest["features"]
            if shape(f["geometry"]).intersects(region)]


def corridor_bbox(a, b, margin_m=50.0):
    minlon, maxlon = sorted([a[0], b[0]])
    minlat, maxlat = sorted([a[1], b[1]])
    midlat = (minlat + maxlat) / 2
    dlat = margin_m / 111_320.0
    dlon = margin_m / (111_320.0 * math.cos(math.radians(midlat)))
    return (minlon - dlon, minlat - dlat, maxlon + dlon, maxlat + dlat)


def head_size(props):
    url = props["baseUrl"] + props["lasPath"]
    r = requests.head(url, allow_redirects=True, timeout=30)
    r.raise_for_status()
    return int(r.headers.get("Content-Length", 0))


def select_under_budget(tiles, cap_bytes):
    picked, total = [], 0
    for t in sorted(tiles, key=lambda x: x["size"]):
        if total + t["size"] <= cap_bytes:
            picked.append(t); total += t["size"]
    return picked


def plan(manifest_path, bbox=DEFAULT_BBOX, cap_gb=DEFAULT_CAP_GB):
    manifest = json.load(open(manifest_path))
    candidates = tiles_in_bbox(manifest, bbox)
    for t in candidates:
        t["size"] = head_size(t)
    picked = select_under_budget(candidates, int(cap_gb * 1024**3))
    total_gb = sum(t["size"] for t in picked) / 1024**3
    print(f"# {len(picked)} tiles, {total_gb:.2f} GB (cap {cap_gb} GB)")
    print("mkdir -p ./laz && cd ./laz")
    for t in picked:
        print(f'curl -O "{t["baseUrl"] + t["lasPath"]}"')
        print(f'#   Potree: {t["baseUrl"] + t["lasPath"]}.potree/metadata.json')
    json.dump(picked, open("selected_tiles.json", "w"), indent=2)


if __name__ == "__main__":
    plan(sys.argv[1] if len(sys.argv) > 1 else "pointclouds_v2.geojson")
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_select_tiles.py -v` → 3 passed.

- [ ] **Step 5: Run for real, download, set LAZ_DIR**

```bash
aws s3 cp s3://cyvl-hackathon/pointclouds/pointclouds_v2.geojson . --profile cyvl-hackathon
python -m pipeline.select_tiles pointclouds_v2.geojson
```
Run the printed `curl` commands into `./laz`, then add to `.env`:
`LAZ_DIR=/Users/xaviercyvl/github/hackathonBuckets/laz`

- [ ] **Step 6: Commit**

```bash
git add pipeline/select_tiles.py tests/test_select_tiles.py
git commit -m "feat: tile selector with area filter and 10GB budget"
```

---

## Task 8: Load + crop LAZ

**Files:**
- Create: `pipeline/crop.py`, `tests/test_crop.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_crop.py
import numpy as np
from pipeline.crop import crop_xy


def test_crop_xy_keeps_only_inside():
    pts = np.array([[0, 0, 1], [5, 5, 1], [50, 50, 1]], dtype=float)
    out = crop_xy(pts, (-1, -1, 10, 10))
    assert out.shape[0] == 2
    assert (out[:, 0] <= 10).all()
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/test_crop.py -v` → FAIL.

- [ ] **Step 3: Implement `pipeline/crop.py`**

```python
import glob
import os
import numpy as np
import laspy
from config import LAZ_DIR


def load_points(laz_dir=None):
    laz_dir = laz_dir or LAZ_DIR
    files = sorted(glob.glob(os.path.join(laz_dir, "*.laz")))
    if not files:
        raise FileNotFoundError(f"no .laz in {laz_dir!r}; set LAZ_DIR in .env")
    xs, rgbs = [], []
    for f in files:
        las = laspy.read(f)
        xs.append(np.column_stack([las.x, las.y, las.z]))
        if hasattr(las, "red"):
            rgbs.append(np.column_stack([las.red, las.green, las.blue]))
    pts = np.vstack(xs)
    rgb = np.vstack(rgbs) if rgbs else np.zeros_like(pts)
    return pts, rgb, files[0]


def crop_xy(points, bbox_proj):
    xmin, ymin, xmax, ymax = bbox_proj
    m = (points[:, 0] >= xmin) & (points[:, 0] <= xmax) & \
        (points[:, 1] >= ymin) & (points[:, 1] <= ymax)
    return points[m]


def las_crs(laz_path):
    las = laspy.read(laz_path)
    crs = las.header.parse_crs()
    if crs is None:
        raise ValueError("LAS has no CRS; run `pdal info <file>` to find it and hardcode")
    return crs
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_crop.py -v` → 1 passed.

- [ ] **Step 5: Inspect a real tile's CRS (one-time, informs Tasks 10/12)**

Run: `pdal info "$LAZ_DIR"/*.laz --summary | head -40`
Note the EPSG. If `parse_crs()` returns None, add `LAS_EPSG` to `config.py`.

- [ ] **Step 6: Commit**

```bash
git add pipeline/crop.py tests/test_crop.py
git commit -m "feat: LAZ load + xy crop + CRS read"
```

---

## Task 9: Ground filter (PDAL)

**Files:**
- Create: `pipeline/segment.py`, `tests/test_segment.py`

- [ ] **Step 1: Write failing test (pure split helper)**

```python
# tests/test_segment.py
import numpy as np
from pipeline.segment import split_ground


def test_split_ground_separates_by_mask():
    pts = np.array([[0, 0, 0], [1, 1, 2.0]], dtype=float)
    ground, nonground = split_ground(pts, np.array([True, False]))
    assert ground.shape[0] == 1 and nonground.shape[0] == 1
    assert ground[0, 2] == 0.0
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/test_segment.py -v` → FAIL.

- [ ] **Step 3: Implement ground functions in `pipeline/segment.py`**

```python
import json
import numpy as np
import pdal


def split_ground(points, ground_mask):
    return points[ground_mask], points[~ground_mask]


def ground_mask_pdal(laz_path):
    """Run PDAL SMRF; return boolean ground mask aligned to file point order."""
    p = pdal.Pipeline(json.dumps({"pipeline": [laz_path, {"type": "filters.smrf"}]}))
    p.execute()
    return p.arrays[0]["Classification"] == 2
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_segment.py::test_split_ground_separates_by_mask -v` → PASS.

- [ ] **Step 5: Smoke-test PDAL on a real tile**

Run:
```bash
python -c "from pipeline.segment import ground_mask_pdal; import glob,config; m=ground_mask_pdal(glob.glob(config.LAZ_DIR+'/*.laz')[0]); print('ground:', m.sum(),'/',len(m))"
```
Expected: non-zero ground count below total. If slow on a full tile, crop first and write a temp LAS for PDAL.

- [ ] **Step 6: Commit**

```bash
git add pipeline/segment.py tests/test_segment.py
git commit -m "feat: PDAL ground filter + split helper"
```

---

## Task 10: Road vs sidewalk classification

**Files:**
- Modify: `pipeline/segment.py`, `tests/test_segment.py`

- [ ] **Step 1: Write failing test**

```python
# add to tests/test_segment.py
from shapely.geometry import LineString
from pipeline.segment import classify_road_sidewalk


def test_classify_road_sidewalk_by_polygon():
    ground = np.array([[0, 0, 0], [0, 10, 0]], dtype=float)
    road_lines = [LineString([(-5, 0), (5, 0)])]
    labels = classify_road_sidewalk(ground, road_lines, width_m=3.0)
    assert labels[0] == "road"
    assert labels[1] == "sidewalk"
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/test_segment.py::test_classify_road_sidewalk_by_polygon -v` → FAIL.

- [ ] **Step 3: Implement in `pipeline/segment.py`**

```python
from shapely.geometry import Point
from shapely.ops import unary_union


def classify_road_sidewalk(ground_points, road_lines, width_m):
    """'road' if within width_m/2 of any road line (same CRS as points), else 'sidewalk'."""
    road_poly = unary_union([ln.buffer(width_m / 2.0) for ln in road_lines])
    return ["road" if road_poly.contains(Point(x, y)) else "sidewalk"
            for x, y, _ in ground_points]


def load_road_lines(geojson_path, bbox_proj, src_epsg, dst_crs):
    import geopandas as gpd
    gdf = gpd.read_file(geojson_path).set_crs(src_epsg, allow_override=True).to_crs(dst_crs)
    if bbox_proj is not None:
        xmin, ymin, xmax, ymax = bbox_proj
        gdf = gdf.cx[xmin:xmax, ymin:ymax]
    return list(gdf.geometry)
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_segment.py::test_classify_road_sidewalk_by_polygon -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/segment.py tests/test_segment.py
git commit -m "feat: road vs sidewalk classification from Cyvl vectors"
```

---

## Task 11: Car detection (heuristic)

**Files:**
- Modify: `pipeline/segment.py`, `tests/test_segment.py`

- [ ] **Step 1: Write failing test**

```python
# add to tests/test_segment.py
from pipeline.segment import cluster_objects, keep_car_clusters


def test_keep_car_clusters_filters_by_size(synthetic_points):
    ground, car = synthetic_points
    tiny = np.array([[15, 15, 0.2], [15.1, 15.1, 0.25]])
    cars = keep_car_clusters([car, tiny])
    assert len(cars) == 1
    assert 2.0 <= cars[0]["length"] <= 6.0
    assert 1.4 <= cars[0]["width"] <= 2.2
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/test_segment.py::test_keep_car_clusters_filters_by_size -v` → FAIL.

- [ ] **Step 3: Implement in `pipeline/segment.py`**

```python
from sklearn.cluster import DBSCAN


def cluster_objects(nonground_points, eps=0.5, min_samples=30):
    if len(nonground_points) == 0:
        return []
    labels = DBSCAN(eps=eps, min_samples=min_samples).fit_predict(nonground_points)
    return [nonground_points[labels == k] for k in set(labels) if k != -1]


def _aabb(cluster):
    mins, maxs = cluster.min(axis=0), cluster.max(axis=0)
    size = maxs - mins
    dims = sorted([size[0], size[1]])
    return {"center": ((mins + maxs) / 2).tolist(),
            "width": float(dims[0]), "length": float(dims[1]),
            "height": float(size[2]), "min": mins.tolist(), "max": maxs.tolist()}


def keep_car_clusters(clusters, length=(2.0, 6.0), width=(1.4, 2.2), height=(1.2, 2.2)):
    cars = []
    for c in clusters:
        b = _aabb(c)
        if (length[0] <= b["length"] <= length[1] and
                width[0] <= b["width"] <= width[1] and
                height[0] <= b["height"] <= height[1]):
            cars.append(b)
    return cars
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_segment.py::test_keep_car_clusters_filters_by_size -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/segment.py tests/test_segment.py
git commit -m "feat: heuristic car detection via clustering + size filter"
```

---

## Task 12: Asset lifting (2D CV → 3D)

**Files:**
- Create: `pipeline/lift_assets.py`, `tests/test_lift_assets.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_lift_assets.py
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
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/test_lift_assets.py -v` → FAIL.

- [ ] **Step 3: Implement `pipeline/lift_assets.py`**

```python
import numpy as np
import geopandas as gpd
from scipy.spatial import cKDTree


def cylinder_indices(points, center_xy, radius):
    dx = points[:, 0] - center_xy[0]
    dy = points[:, 1] - center_xy[1]
    return np.nonzero(dx * dx + dy * dy <= radius * radius)[0]


def z_extent(z_values):
    gz = float(np.percentile(z_values, 2))
    tz = float(np.percentile(z_values, 98))
    return gz, tz, tz - gz


def lift_assets(points, assets_geojson, src_epsg, dst_crs, bbox_proj, radius=0.8):
    gdf = gpd.read_file(assets_geojson).set_crs(src_epsg, allow_override=True).to_crs(dst_crs)
    if bbox_proj is not None:
        xmin, ymin, xmax, ymax = bbox_proj
        gdf = gdf.cx[xmin:xmax, ymin:ymax]
    tree = cKDTree(points[:, :2])
    out = []
    for _, row in gdf.iterrows():
        cx, cy = row.geometry.x, row.geometry.y
        near = tree.query_ball_point([cx, cy], radius)
        if not near:
            continue
        gz, tz, h = z_extent(points[near, 2])
        out.append({"type": row.get("Type") or row.get("asset_type") or "asset",
                    "x": float(cx), "y": float(cy),
                    "ground_z": gz, "top_z": tz, "height": h})
    return out
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/test_lift_assets.py -v` → 2 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/lift_assets.py tests/test_lift_assets.py
git commit -m "feat: lift Cyvl 2D CV asset detections into 3D"
```

---

## Task 13: Write classified + colored LAS (Potree input)

**Files:**
- Create: `pipeline/classify_io.py`

- [ ] **Step 1: Implement `pipeline/classify_io.py`**

(Thin IO over laspy; verified by Potree in Step 2.)

```python
import numpy as np
import laspy

CLASS_RGB = {
    "ground":   (40000, 40000, 40000),
    "road":     (10000, 10000, 10000),
    "sidewalk": (50000, 40000, 25000),
    "car":      (60000, 60000, 0),
    "other":    (20000, 20000, 60000),
}


def write_colored_las(path, points, classes):
    header = laspy.LasHeader(point_format=3, version="1.2")
    las = laspy.LasData(header)
    las.x, las.y, las.z = points[:, 0], points[:, 1], points[:, 2]
    rgb = np.array([CLASS_RGB.get(c, CLASS_RGB["other"]) for c in classes], dtype=np.uint16)
    las.red, las.green, las.blue = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    las.write(path)
```

- [ ] **Step 2: Build a Potree tileset and view (manual)**

```bash
PotreeConverter out/classified.laz -o out/potree --overwrite   # if installed
# serve and open Potree at out/potree/metadata.json
```
Expected: points colored by class (cars yellow). **Potree demo checkpoint.** (Fallback: view Cyvl's prebuilt `.laz.potree/` tileset for context if PotreeConverter is unavailable.)

- [ ] **Step 3: Commit**

```bash
git add pipeline/classify_io.py
git commit -m "feat: write class-colored LAS for Potree"
```

---

## Task 14: End-to-end driver + README

**Files:**
- Create: `run.py`, `README.md`

- [ ] **Step 1: Implement `run.py`**

```python
import glob
import pyproj
import config
from pipeline.crop import load_points, las_crs
from pipeline.segment import (ground_mask_pdal, split_ground,
                              classify_road_sidewalk, load_road_lines,
                              cluster_objects, keep_car_clusters)
from pipeline.lift_assets import lift_assets
from pipeline.classify_io import write_colored_las
from pipeline.to_cad import write_obj, cars_to_objects, assets_to_objects
from aps.auth import get_token
from aps.upload import ensure_bucket, upload_object
from aps.translate import start_translation, wait_until_done

LAS = glob.glob(config.LAZ_DIR + "/*.laz")[0]
crs = las_crs(LAS)

# reproject DEFAULT_BBOX (WGS84) corners into the LAS CRS -> projected bbox
tf = pyproj.Transformer.from_crs(4326, crs, always_xy=True)
minx, miny = tf.transform(config.DEFAULT_BBOX[0], config.DEFAULT_BBOX[1])
maxx, maxy = tf.transform(config.DEFAULT_BBOX[2], config.DEFAULT_BBOX[3])
bbox_proj = (minx, miny, maxx, maxy)

pts, rgb, _ = load_points()
gmask = ground_mask_pdal(LAS)
ground, nonground = split_ground(pts, gmask)

road_lines = load_road_lines("data/pavements_v2.geojson", bbox_proj, 4326, crs)
labels = classify_road_sidewalk(ground, road_lines, width_m=7.0)

cars = keep_car_clusters(cluster_objects(nonground))
assets = lift_assets(pts, "data/aboveGroundAssets_v2.geojson", 4326, crs, bbox_proj)

write_colored_las("out/classified.laz", ground, labels)
write_obj("out/scene.obj", cars_to_objects(cars) + assets_to_objects(assets))

tok = get_token()
bucket = "cyvl-hack-xavier"
ensure_bucket(tok, bucket)
urn = upload_object(tok, bucket, "scene.obj", "out/scene.obj")
start_translation(tok, urn)
wait_until_done(tok, urn)
print("PASTE THIS URN INTO viewer/index.html:", urn)
```

- [ ] **Step 2: Write `README.md`**

```markdown
# Cyvl × Autodesk — point-cloud street segmenter

Two viewers by design: **APS Viewer** (top priority — editable CAD, move/delete
cars) and **Potree** (raw classified cloud). Autodesk path is built and proven
first (Tasks 1-6) with a dummy scene, then the real scene replaces the dummy.

## Run order
1. APS path proven with dummy: `python -c "from pipeline.to_cad import write_dummy_scene; write_dummy_scene()"` then Tasks 2-6.
2. `python -m pipeline.select_tiles pointclouds_v2.geojson` → download printed tiles → set `LAZ_DIR` in `.env`
3. `pdal info "$LAZ_DIR"/*.laz --summary` → note EPSG
4. `python run.py` → builds classified.laz + scene.obj, uploads, translates, prints the real URN
5. Paste URN into `viewer/index.html`, run `python viewer/token_server.py`, open http://localhost:8080
6. (Potree) view `out/classified.laz`

## Tests
`pytest -v`
```

- [ ] **Step 3: Run full test suite + the pipeline**

Run: `pytest -v && python run.py`
Expected: all tests pass; `run.py` prints a URN.

- [ ] **Step 4: Commit**

```bash
git add run.py README.md
git commit -m "feat: end-to-end driver + README"
```

---

## Self-Review

**Spec coverage:** OBJ writer (Task 1) ✓ · OAuth v2 (Task 2) ✓ · OSS signed-S3 (Task 3) ✓ · Model Derivative (Task 4) ✓ · Viewer renders model (Task 5) ✓ · Viewer move/delete (Task 6) ✓ · tile budget/selector + manual download + LAZ_DIR (Task 7) ✓ · load+crop+CRS (Task 8) ✓ · ground filter (Task 9) ✓ · road/sidewalk via vectors (Task 10) ✓ · car heuristic (Task 11) ✓ · 2D→3D asset lifting (Task 12) ✓ · colored LAS/Potree (Task 13) ✓ · end-to-end (Task 14) ✓.

**Build-order check:** Autodesk integration (Tasks 1-6) completes before any LiDAR/segmentation work (Tasks 7-13), using a dummy OBJ — matches the "Autodesk is top priority, Viewer before move" requirement. The real scene swaps in at Task 14.

**Placeholder scan:** the `<paste URN>` markers in Tasks 4/5 are deliberate manual hand-offs between a network smoke run and the viewer, not code TODOs. `bbox_proj` is fully resolved in Task 14 via the pyproj transform (no longer deferred).

**Type consistency:** `keep_car_clusters` → dicts with `center/length/width/height` consumed by `cars_to_objects` with those exact keys ✓. `lift_assets` → `type/x/y/ground_z/top_z/height` consumed by `assets_to_objects` ✓. `upload_object` → URN string consumed by `start_translation` and the viewer ✓. `get_token`/scopes consistent ✓. `load_road_lines`/`lift_assets` accept `bbox_proj=None` safely (tested helpers never pass projected bbox) ✓.

**Known cut for the MVP:** road surface omitted from the OBJ (cars + assets only) to keep Task 14 lean; add a road slab box if time allows. Does not block either demo.
