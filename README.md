# Cyvl × Autodesk — point-cloud street segmenter

Two viewers by design: **APS Viewer** (top priority — editable CAD, move/delete
cars) and **Potree** (raw classified cloud). The Autodesk path is built and proven
first (Tasks 1-6) with a dummy scene, then the real scene replaces the dummy.

## Setup
- `python3.11 -m venv .venv && source .venv/bin/activate`
- `pip install -r requirements.txt` (PDAL fails on pip/macOS — install via conda:
  `conda install -c conda-forge pdal python-pdal`)
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
`pytest -v`  (12 tests, no PDAL/data needed — they use synthetic fixtures)

## Note
`run.py` writes `out/classified.laz`; writing `.laz` needs the LAZ backend
(`pip install lazrs` or `laspy[laz]`). If unavailable, change the extension to
`.las` in run.py.
