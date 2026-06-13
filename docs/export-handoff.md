# Export Handoff — what makes the model export to APS cleanly

Audience: another agent picking up this repo. Goal: reproduce a clean, editable,
correctly-colored, correctly-oriented Autodesk Viewer scene from generated geometry.
This is the distilled "why it works" — the non-obvious decisions only. Repo overview
lives in `docs/autodesk-integration-summary.md`; this file is the export path specifically.

## The export chain (one sentence)

Generated meshes → multi-object **OBJ + MTL** → **zip** → OSS signed-S3 upload →
Model Derivative **SVF2** translate (`compressedUrn` + `rootFilename`) → manifest poll →
Viewer loads URN. Code: `pipeline/to_cad.py` (writer) + `aps/upload.py` + `aps/translate.py`,
driven by `run.py`.

## The decisions that made it export well

### 1. OBJ `o <name>` groups → per-object selectable nodes (the big one)
Each object written as its own `o name` group in the OBJ (`write_obj`, `pipeline/to_cad.py`).
Model Derivative turns every `o` group into an individually selectable node (dbId) in the
Viewer — move/delete/hide work with **zero extra metadata**. This is the whole reason the
scene is editable instead of one frozen blob.
- **Rule: names must not contain spaces.** A space splits the name and breaks the node.
  We use `car_01`, `tree_03_canopy`, etc. (zero-padded, underscore-joined).
- Multi-part objects use a base name + suffix (`car_01` body + `car_01_cabin`) so related
  parts read as siblings.

### 2. Colors survive only via zipped OBJ+MTL with `rootFilename`
Material color reaches the Viewer **only** when OBJ + MTL are zipped together and the job is
submitted with `compressedUrn: true` + `rootFilename: "scene.obj"` (`aps/translate.py:start_translation`).
A bare OBJ translates fine but renders untextured/gray. The MTL (`write_mtl`) defines flat `Kd`
diffuse colors per material (asphalt, car, glass, trunk, canopy, pole, hydrant, etc.); each
object's `usemtl` line picks one. `mttlib scene.mtl` must be the first OBJ line.

### 3. SVF2 derivatives are cached per OSS object key → version the key every run
Re-uploading changed geometry under the **same** object key serves the **stale** cached model.
Two defenses, use both:
- `x-ads-force: "true"` on the translate job (`aps/translate.py`).
- **Version the object key per run**: `scene_cad_v1.zip`, `scene_cad_v2.zip`, … The live site
  is on `scene_cad_v6.zip` (see `deploy/index.html` URN). Bump it whenever geometry changes or
  you will demo an old scene and not know why.

### 4. Z-up survey data vs Y-up viewer
LiDAR/UTM is Z-up; the Viewer defaults Y-up. Fix is in the viewer, not the export:
`navigation.setWorldUpVector(0,0,1)` plus a street-level initial camera. Without it the scene
loads on its side. (Geometry is written Z-up in `to_cad.py` — don't pre-rotate it; fix at view time.)

### 5. Shift geometry to a local origin before export
Raw UTM coords are ~330000, 4690000 — huge floats cause Viewer precision jitter. `run.py`
subtracts a local origin so the scene sits near (0,0). The point-cloud overlay is realigned in
the viewer with `model.getData().placementWithOffset`.

### 6. Upload is the 3-step signed-S3 dance, not a single PUT
`aps/upload.py:upload_object`: `GET .../signeds3upload` (get uploadKey + S3 URL) → `PUT` the
binary straight to S3 → `POST .../signeds3upload` with uploadKey to finalize. The returned
`objectId` → `urlsafe_b64encode` **without padding** (`.rstrip("=")`) = the URN. Padding left on
will break Model Derivative.

### 7. Auth scope gotcha
2-legged token. Translate/upload need `data:read data:write data:create bucket:create bucket:read`.
The **viewer** token must be `data:read` — `viewer:read` returns `400 invalid_scope` on 2-legged
flows. Viewer fetches its token from a tiny endpoint (`deploy/api/token.js` in prod via Vercel env
vars, `viewer/token_server.py` locally). Credentials never touch the client.

## Mesh generation notes (why objects look right)

- Everything is built from 3 primitives in `to_cad.py`: `mesh_box`, `mesh_cylinder`, `mesh_cone`.
  Faces are 0-indexed within each object; `write_obj` applies the global vertex offset and emits
  1-based `f` indices. Don't hand-author global indices.
- **Typed parametric models** per asset class (`asset_objects`): tree = trunk cylinder + canopy
  cone, pole = cylinder, hydrant = short red cylinder, manhole = flush disk, signal = pole + head
  box. Cars = body box + cabin box, PCA yaw applied (`car_objects`).
- `SKIP` set (CURB/SIDEWALK/RAMP/GUARDRAILS) — these are part of the ground mesh, not standalone
  objects, so they're dropped from `asset_objects` to avoid double geometry.
- Test the full APS round-trip before real segmentation exists with `write_dummy_scene()` (two car
  boxes). This is how the Autodesk path was proven first (Tasks 1-6).

## Limitations that shape the export (don't fight these)

- APS Viewer has **no native point cloud** (no LAS/LAZ in Model Derivative). The raw cloud is a
  three.js `THREE.PointCloud` overlay — visual only, not selectable, not part of the model.
- No programmatic LAZ→RCS (ReCap) path on macOS/Linux, so OBJ+MTL→SVF2 is the working route.
- Street-side scan sees facades only: cars are heuristic, buildings are extruded footprints.

## Reproduce / re-export checklist

1. `.env` has `APS_CLIENT_ID` / `APS_CLIENT_SECRET` (gitignored).
2. Generate or edit geometry → `write_obj` + `write_mtl` into `out/`.
3. **Bump the object key** (`scene_cad_vN.zip`). Zip OBJ+MTL together.
4. `upload_object` → `start_translation(root_filename="scene.obj")` → `wait_until_done`.
5. Paste the printed URN into `deploy/index.html` (or `viewer/index.html`) `const URN=`.
6. Verify in viewer: objects selectable, colored, Z-up. If stale → you reused the object key.
