import http.server, socketserver, json, os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from aps.auth import get_token

EMPTY_ESTIMATE = {"currency": "USD", "total": 0, "by_op": {}, "line_items": [],
                  "error": "no estimate yet — run run.py to generate out/estimate.json"}

class H(http.server.SimpleHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/token":
            tok = get_token("data:read")
            return self._json({"access_token": tok, "expires_in": 3600})
        if self.path == "/api/estimate":
            # the cost report is written at the repo root by run.py
            path = os.path.join(REPO, "out", "estimate.json")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    return self._json(f.read())
            return self._json(EMPTY_ESTIMATE)
        return super().do_GET()

os.chdir(os.path.dirname(os.path.abspath(__file__)))
print("open http://localhost:8080/index.html")
socketserver.TCPServer(("", 8080), H).serve_forever()
