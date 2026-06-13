# Street Scan to CAD: Cyvl LiDAR x Autodesk Platform Services

Hackathon build summary. Pipeline that turns raw vehicle-mounted LiDAR street scans (Cyvl.ai survey data, Somerville MA) into an interactive CAD scene rendered in the Autodesk Viewer, with editable objects (cars, trees, poles, hydrants, buildings) reconstructed from the point cloud.

Demo location: College Avenue, Ball Square, West Somerville, MA (42.399430, -71.119058).

## 1. What It Does

1. Takes one raw LAZ tile (~110M points, UTM 19N, no semantic labels) from Cyvl's street survey.
2. Crops an 80m region of interest, classifies ground (PDAL SMRF), splits road vs sidewalk using the scan vehicle's drive paths.
3. Detects parked cars geometrically (height band above local ground + DBSCAN + PCA-oriented bounding boxes).
4. Lifts Cyvl's existing 2D computer-vision detections (trees, utility poles, hydrants, manholes, catch basins, traffic signals) into 3D by cropping the point cloud at each detection's coordinates and measuring ground elevation + object height.
5. Extrudes OSM building footprints to point-cloud-measured heights.
6. Writes everything as a multi-object OBJ + MTL CAD scene and pushes it through APS to the Autodesk Viewer in a browser, where individual objects are selectable, movable, and deletable -- with the raw colored point cloud rendered on top as a toggleable overlay for before/after comparison.

## 2. Autodesk APIs Used (the core of the project)

### 2.1 Authentication API v2 (2-legged OAuth)
- `POST /authentication/v2/token`, `client_credentials` grant, Basic auth header.
- Scopes: `data:read data:write data:create bucket:create bucket:read`.
- Finding: `viewer:read` is rejected on 2-legged flows (`400 invalid_scope`); the Viewer works fine with a `data:read` token served from a small local token endpoint.

### 2.2 Object Storage Service (OSS) v2
- Bucket creation (`POST /oss/v2/buckets`, transient policy, 409 = already exists).
- Direct-to-S3 signed upload (3 steps): `GET .../signeds3upload` -> `PUT` binary to the signed S3 URL -> `POST .../signeds3upload` with the uploadKey to finalize.
- The returned objectId, urlsafe-base64-encoded without padding, becomes the URN for Model Derivative.

### 2.3 Model Derivative API v2
- `POST /modelderivative/v2/designdata/job` translating OBJ -> SVF2 (`x-ads-force: true` to retranslate).
- Multi-file input: OBJ + MTL zipped together, job submitted with `compressedUrn: true` + `rootFilename: "scene.obj"` -- this is how material colors survive into the viewer.
- Manifest polling (`GET .../designdata/{urn}/manifest`) until `success`.
- Finding: SVF2 derivatives are cached per OSS object key. Re-uploading changed geometry under the same key serves the stale model; we version object keys per run (`scene_cad_v1.zip`, `scene_cad_v2.zip`, ...).
- Finding: OBJ named groups (`o name`) become individually selectable nodes (dbIds) in the viewer -- this is what makes per-object interaction possible with zero extra work. Names must not contain spaces.

### 2.4 Viewer SDK v7
- `GuiViewer3D`, env `AutodeskProduction2`, api `streamingV2`, token from local endpoint.
- Per-object interaction: `SELECTION_CHANGED_EVENT` + instance tree; "move car" uses fragment proxies (`getFragmentProxy` -> mutate `position` -> `updateAnimTransform` -> `invalidate`), "delete" uses `viewer.hide(dbId)`.
- Raw point cloud inside the Autodesk Viewer: 1M+ LiDAR points loaded as a `THREE.PointCloud` (three.js r71) overlay with `BufferGeometry` and `geometry.isPoints = true`, per the official APS blog technique. Aligned to the model by applying `model.getData().placementWithOffset` (the viewer's global offset) to the overlay.
- Survey data is Z-up; the viewer defaults to Y-up. `navigation.setWorldUpVector(0,0,1)` + a street-level initial camera fixes top-down home view and first-person navigation.

## 3. Pipeline Detail (non-Autodesk side)

- PDAL streaming crop + SMRF ground filter (memory-safe on 110M-point tiles).
- Voxel downsample (0.10-0.15m), road/sidewalk via vectorized point-in-polygon against buffered drive-path centerlines.
- Cars: points 0.3-2.5m above local ground (cKDTree ground lookup), DBSCAN footprint clustering, PCA-oriented boxes gated to real car dimensions, rendered as body + cabin meshes.
- 2D->3D asset lifting: reproject each Cyvl CV detection (lat/lon) to UTM, cylinder-crop the cloud, derive ground z and height; typed parametric CAD models per asset class (tree = trunk + canopy cone, pole = cylinder, hydrant = red cylinder, manhole = flush disk, signal = pole + head).
- Ground: 0.5m heightfield grid -> triangulated solid surface, asphalt/concrete materials.
- Buildings: OSM Overpass footprints, height = 98th-percentile point elevation inside footprint where the scan covers it, else OSM tags, else default.

## 4. Honest Limitations

- The APS Viewer has no native point cloud ingestion (no LAS/LAZ in Model Derivative); the raw cloud is a three.js overlay -- visual only, not selectable, not part of the model.
- ReCap (RCS/RCP) would be the native Autodesk point cloud route, but there is no programmatic LAZ->RCS path on macOS/Linux.
- Street-side scanning sees facades only: car detection is heuristic (half-occluded vehicles), building backs/roofs are unobserved, so buildings are extruded footprints rather than reconstructed surfaces.
- This tile's RGB fields are empty; point colors are synthesized from elevation + LiDAR intensity.

## 5. Questions / Asks for the Autodesk Team

1. Is this the intended shape of a "scan to CAD" workflow on APS, or would you structure it differently?
2. Any roadmap for native point cloud support in the Viewer or LAS/LAZ/E57 input in Model Derivative? That single capability would remove our biggest workaround.
3. Is there a supported headless/cross-platform path into ReCap (RCS) for fleet-scale LiDAR like Cyvl's?
4. For programmatic CAD scene generation, is OBJ+MTL via Model Derivative the recommended input, or would IFC/DWG/FBX give richer semantics (object types, properties) in the viewer?
5. Could Design Automation (e.g. AutoCAD/Revit engines) be the right next step to emit native DWG/RVT deliverables from this pipeline?

## 6. Repo Map

- `aps/` -- auth, OSS upload, Model Derivative client
- `pipeline/` -- crop, segment (ground/road/cars), lift_assets (2D->3D), ground_mesh, buildings, to_cad (OBJ/MTL writer + parametric models)
- `viewer/` -- token server, viewer page, point-cloud overlay + interactions
- `run.py` -- end-to-end driver: LAZ tile -> classified scene -> APS -> URN
- `export_points.py` -- colored point binary for the viewer overlay
