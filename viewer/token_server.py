"""Local viewer server: APS token + interactive scenario persistence.

Endpoints
  GET  /api/token      -> APS viewer token (data:read)
  GET  /api/scene_meta -> scene origin/crs/roi for local<->UTM conversion
  GET  /api/assets     -> draggable asset registry (name, type, authored UTM)
  GET  /api/scenario   -> {scenario, estimate} (the live proposal + running cost)
  POST /api/scenario   -> apply {edit|undo|clear|scenario}; returns
                          {scenario, estimate, warnings} (clearance advisories)
  GET  /api/estimate   -> the static whole-block estimate written by run.py

The scenario JSON (out/scenario.json) is the source of truth; the cost engine
re-prices it on every change. Request logic lives in module-level functions so
it can be unit-tested without binding a socket.
"""
import http.server
import json
import os
import re
import socketserver
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from aps.auth import get_token  # noqa: E402
from pipeline import clearance  # noqa: E402
from pipeline.cost import estimate, load_catalog  # noqa: E402
from pipeline.scenario import Scenario  # noqa: E402

OUT_DIR = os.path.join(REPO, "out")
_TRAILING_INDEX = re.compile(r"_\d+$")
_CATALOG = None


def catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = load_catalog()
    return _CATALOG


# --------------------------------------------------------------- request logic

def _read_json(path, default=None):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def scene_meta(out_dir=OUT_DIR) -> dict:
    return _read_json(os.path.join(out_dir, "scene_meta.json"), {}) or {}


def asset_registry(out_dir=OUT_DIR) -> list:
    return _read_json(os.path.join(out_dir, "assets.json"), []) or []


def read_scenario(out_dir=OUT_DIR) -> Scenario:
    meta = scene_meta(out_dir)
    return Scenario.load_or_new(
        os.path.join(out_dir, "scenario.json"),
        block_id=meta.get("block_id", "block"),
        crs=meta.get("crs", "EPSG:26919"))


def _estimate_dict(scen: Scenario) -> dict:
    return estimate(scen.edits, catalog(), strict=False).to_dict()


def _asset_type(edit) -> str:
    if edit.get("asset_type"):
        return str(edit["asset_type"]).lower()
    return _TRAILING_INDEX.sub("", edit.get("target") or "").lower() or "asset"


def check_clearance(edit, out_dir=OUT_DIR) -> list:
    """Advisory clearance check for a relocate edit, using out/obstacles.json."""
    d = _read_json(os.path.join(out_dir, "obstacles.json"))
    to = edit.get("to_utm")
    if d is None or to is None:
        return []
    buildings, curbs = clearance.obstacles_from_dict(d)
    roi = d.get("roi_bounds") or scene_meta(out_dir).get("bbox_proj")
    return clearance.validate_relocation(_asset_type(edit), (to[0], to[1]),
                                         buildings=buildings, curbs=curbs,
                                         roi_bounds=roi)


def scenario_state(out_dir=OUT_DIR) -> dict:
    scen = read_scenario(out_dir)
    return {"scenario": scen.to_dict(), "estimate": _estimate_dict(scen)}


def apply_post(body: dict, out_dir=OUT_DIR) -> dict:
    """Apply one scenario mutation and return {scenario, estimate, warnings}."""
    scen = read_scenario(out_dir)
    warnings = []
    if body.get("clear"):
        scen.clear()
    elif body.get("undo"):
        scen.undo()
    elif "scenario" in body:
        scen = Scenario.from_dict(body["scenario"])
    elif "edit" in body:
        edit = body["edit"]
        if edit.get("op") == "relocate":
            warnings = check_clearance(edit, out_dir)
        scen.add_edit(edit)
    else:
        raise ValueError("POST body needs one of: edit, undo, clear, scenario")
    scen.save(os.path.join(out_dir, "scenario.json"))
    return {"scenario": scen.to_dict(), "estimate": _estimate_dict(scen),
            "warnings": warnings}


# ----------------------------------------------------------------- HTTP handler

class H(http.server.SimpleHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/token":
            return self._json({"access_token": get_token("data:read"), "expires_in": 3600})
        if self.path == "/api/scene_meta":
            return self._json(scene_meta())
        if self.path == "/api/assets":
            return self._json(asset_registry())
        if self.path == "/api/scenario":
            return self._json(scenario_state())
        if self.path == "/api/estimate":
            data = _read_json(os.path.join(OUT_DIR, "estimate.json"))
            return self._json(data if data is not None else
                              {"currency": "USD", "total": 0, "by_op": {},
                               "line_items": [], "error": "no estimate yet"})
        return super().do_GET()

    def do_POST(self):
        if self.path != "/api/scenario":
            return self._json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            return self._json(apply_post(body))
        except Exception as e:  # noqa: BLE001 — surface the message to the client
            return self._json({"error": str(e)}, 400)


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print("open http://localhost:8080/index.html")
    socketserver.TCPServer(("", 8080), H).serve_forever()


if __name__ == "__main__":
    main()
