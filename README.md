# StreetForge — Cyvl × Autodesk street intervention & cost tool

Two viewers by design: **APS Viewer** (top priority — editable CAD, move/delete
cars) and **Potree** (raw classified cloud). The Autodesk path is built and proven
first (Tasks 1-6) with a dummy scene, then the real scene replaces the dummy.

On top of that base, StreetForge prices street interventions. See
`docs/streetforge-implementation-guide.md` for the full plan. Implemented so far:

- **Phase 0 (seed)** — `pipeline/cyvl_source.py`: load Cyvl layers
  (pavements/distresses/assets/…) from the SDK, S3, or local GeoJSON and
  reproject them all into one working CRS (the LAS header's), so 4326 layers join
  cleanly onto the 26919 ground. `working_crs_from_las` / `to_working_crs`.
- **Phase 1 — pavement condition + cost.**
  - `pipeline/pavement.py` bins each road cell by the nearest pavement segment's
    `score`/`label` (good/fair/poor) and counts distresses per cell.
  - `pipeline/ground_mesh.py` emits `ground_road_good/fair/poor` as separate,
    selectable OBJ objects with a green→red material ramp.
  - `pipeline/cost.py` + `config/unit_costs.yaml`: `estimate(edits, catalog)
    -> CostReport` (itemized, md/csv/json export). `run.py` writes a static
    whole-block repair estimate to `out/estimate.{md,json}`.
  - Viewer: roads render colored by condition; the **Cost estimate** button shows
    the itemized total (served from `/api/estimate`).

## Setup
- `python3.11 -m venv .venv && source .venv/bin/activate`
- `pip install -r requirements.txt` (PDAL fails on pip/macOS — install via conda:
  `conda install -c conda-forge pdal python-pdal`)
- `pyyaml` (cost catalog) is in `requirements.txt`. For the Cyvl SDK source path,
  `pip install -e ../sandbox/cyvl-spatial-sdk` (optional — `cyvl_source` also
  reads S3/local GeoJSON without it).
- APS Client ID/Secret in `.env` (gitignored). Note: use `data:read` for the
  viewer token; `viewer:read` is invalid for 2-legged OAuth.

## Run order
1. Autodesk path proven with a dummy:
   `python -c "from pipeline.to_cad import write_dummy_scene; write_dummy_scene()"`
   then `aps/` auth+upload+translate, then `viewer/`.
2. `aws s3 cp s3://cyvl-hackathon/pointclouds/pointclouds_v2.geojson . --profile cyvl-hackathon`
3. `python -m pipeline.select_tiles pointclouds_v2.geojson` → run the printed curl
   commands to download tiles into `./laz`, then set `LAZ_DIR` in `.env`.
4. Download the vector layers used by run.py:
   `mkdir -p data && aws s3 cp s3://cyvl-hackathon/data/pavements_v2.geojson data/ --profile cyvl-hackathon`
   `aws s3 cp s3://cyvl-hackathon/data/aboveGroundAssets_v2.geojson data/ --profile cyvl-hackathon`
5. `pdal info "$LAZ_DIR"/*.laz --summary` → note the EPSG.
6. `python run.py` → builds classified.laz + scene.obj, uploads, translates, prints the real URN.
7. Paste URN into `viewer/index.html`, run `python viewer/token_server.py`, open http://localhost:8080.
8. (Potree) view `out/classified.laz` for the raw segmentation demo.

## Tests
`pytest -v`  (no PDAL/data needed — synthetic fixtures). Covers the segmenter
plus the StreetForge additions: `test_cost.py`, `test_pavement.py`,
`test_ground_mesh.py`, `test_cyvl_source.py`, and the `test_phase1_integration.py`
end-to-end (grid → condition → binned mesh → priced report).

## Note
`run.py` writes `out/classified.laz`; writing `.laz` needs the LAZ backend
(`pip install lazrs` or `laspy[laz]`). If unavailable, change the extension to
`.las` in run.py.
