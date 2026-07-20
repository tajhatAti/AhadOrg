"""STEP-4 EVIDENCE — embedded single-service mode, FOR REAL.

Boots the FULL main app with NO runner env vars (→ the runner activates
in-process), then proves the money path end-to-end over real HTTP:

    create user → login → POST /api/jobs (python web script)
    → job starts IN THIS PROCESS → poll until its public /live/{slug}/ URL
    → GET 200 with the marker   (public URL responding — no second service)
    → path-prefix strip works   (GET /live/{slug}/sub/page)
    → POST forwarding works     (POST /live/{slug}/echo, the §5 requirement)
    → stop → friendly offline page (not a bare 502)

Run:  python3 tests/test_embedded_single_service.py
"""
import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = int(os.getenv("TEST_PORT", "8977"))
BASE = f"http://127.0.0.1:{PORT}"

os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("RUNNER_SERVICE_URL", None)     # ← forces embedded mode
os.environ.pop("RUNNER_SERVICE_SECRET", None)  # ← app generates an internal one
os.environ["PORT"] = str(PORT)
os.environ["SITE_BASE_URL"] = BASE

passed = failed = 0
def check(name, cond, extra=""):
    global passed, failed
    ok = bool(cond)
    if ok: passed += 1
    else: failed += 1
    print(("✓ " if ok else "✗ FAIL ") + name + ("" if ok else f" — {extra}"))

import app as appmod  # noqa: E402  (embedded runner activates here)
import database  # noqa: E402
import routes.deps as _deps  # noqa: E402
import services.runner_client as runner_client  # noqa: E402

check("embedded mode activated (RUNNER_SERVICE_URL unset)", appmod.EMBEDDED_RUNNER is True)
check("runner_client agrees it's embedded", runner_client.embedded_mode() is True)
check("public base url = this service", runner_client.public_base_url() == BASE)

# seed a verified user
now = _deps.now_utc_str()
conn = database.get_db_connection()
conn.execute(
    "INSERT INTO users (username, email, password, is_verified, role, created_at, updated_at)"
    " VALUES ('webbie', 'webbie@t.dev', ?, 1, 'user', ?, ?)",
    (_deps.hash_password("pass-123"), now, now),
)
conn.commit(); conn.close()

# serve
import uvicorn  # noqa: E402
config = uvicorn.Config(appmod.app, host="127.0.0.1", port=PORT, log_level="warning")
server = uvicorn.Server(config)
t = threading.Thread(target=server.run, daemon=True)
t.start()

import requests  # noqa: E402
for _ in range(60):
    try:
        if requests.get(BASE + "/health", timeout=1).status_code == 200:
            break
    except Exception:
        pass
    time.sleep(0.25)
else:
    print("server never came up"); sys.exit(2)
check("server up (health 200)", True)
check("health reports runner=embedded", requests.get(BASE + "/health").json().get("runner") == "embedded")

# login
r = requests.post(BASE + "/login", json={"username": "webbie", "password": "pass-123"})
check("login 200", r.status_code == 200, r.text[:120])
tok = r.json()["token"]
H = {"Authorization": "Bearer " + tok}

# quick one-shot execution through the embedded engine
r = requests.post(BASE + "/api/execute", json={"language": "python", "code": "print('exec-ok')"}, headers=H)
check("one-shot execute runs IN-PROCESS (200 + stdout)", r.status_code == 200 and "exec-ok" in (r.json().get("stdout") or ""), r.text[:160])

# ---- the 24/7 job with a web listener ----
JOB_CODE = r'''
import os, http.server
PORT = int(os.environ.get("PORT", "8000"))
class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain"):
        data = body.encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a): pass
    def do_GET(self):
        self._send(200, "EMBEDDED-JOB-ALIVE path=" + self.path)
    def do_POST(self):
        n = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(n).decode()
        self._send(200, "echo:" + self.path + " body=" + body)
http.server.ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
'''

r = requests.post(BASE + "/api/jobs", json={"name": "probe-web", "language": "python", "code": JOB_CODE}, headers=H)
check("job created (HTTP<300)", r.status_code < 300, f"{r.status_code} {r.text[:200]}")
job_db_id = r.json().get("job_db_id")
check("job got a runner id + slug", bool(r.json().get("id")) and bool(r.json().get("web_slug")), r.text[:200])

# poll until the watchdog marks the job web=true with a public URL
web_url, live_view = None, None
for _ in range(80):
    r = requests.get(BASE + "/api/jobs", headers=H)
    rows = r.json().get("jobs", [])
    live_view = next((j for j in rows if j.get("name") == "probe-web"), None)
    if live_view and live_view.get("web") and live_view.get("web_url"):
        web_url = live_view["web_url"]
        break
    time.sleep(0.4)
check("watchdog detected the listener → public URL issued", bool(web_url), json.dumps(live_view)[:200])
check("URL points at THIS service /live/", web_url and web_url.startswith(BASE + "/live/"), str(web_url))

# THE MOMENT OF TRUTH — public URL responding, served by the same process
r = requests.get(web_url, timeout=10)
check("PUBLIC URL RESPONDS 200 (no second service!)", r.status_code == 200 and "EMBEDDED-JOB-ALIVE" in r.text, f"{r.status_code} {r.text[:120]}")

# path-prefix strip: /live/{slug}/sub/page → job sees /sub/page
r = requests.get(web_url + "sub/page", timeout=10)
check("path-prefix stripped correctly", "path=/sub/page" in r.text, r.text[:120])

# POST forwarding (the §5 fix): body + method reach the job intact
r = requests.post(web_url + "echo", data="hello=1", timeout=10)
check("POST forwarded through the proxy", r.status_code == 200 and "echo:/echo body=hello=1" in r.text, f"{r.status_code} {r.text[:120]}")

# logs endpoint shows the real job log
r = requests.get(BASE + f"/api/jobs/{job_db_id}/logs", headers=H)
check("job logs readable", r.status_code == 200 and "started" in (r.json().get("logs") or ""), r.text[:120])

# stop → friendly offline page (not a bare crash)
requests.post(BASE + f"/api/jobs/{job_db_id}/stop", headers=H)
time.sleep(1.2)
r = requests.get(web_url, timeout=10)
check("stopped job → friendly offline page", "not running" in r.text.lower(), f"{r.status_code} {r.text[:120]}")

print(f"\n{passed} passed, {failed} failed")
try:
    server.should_exit = True
except Exception:
    pass
sys.exit(1 if failed else 0)
