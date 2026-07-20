"""E2E driver — boots the REAL app (modular routes/ + services/) on
127.0.0.1:$PORT with a known fixture DB so the jsdom harness
(test-pivot.js) can drive the full UI end-to-end.

Fixtures
  users:  boss / regular1        (password: pass-123, both verified)
  2FA:    boss has TOTP enabled  (secret JBSWY3DPEHPK3PXP — classic test secret)
  jobs:   boss owns "shilo-bot" (runner_job_id rid-abc) + "draft-api" (never deployed)
  admin:  boss@t.dev is granted is_admin at login (patched ADMIN_EMAILS)

Patched for the sandbox:
  services.email.send_email            -> no-op (no Brevo key needed)
  services.runner_client._runner_http  -> fake runner (jobs: [], capacity: 3)
  routes.deps.ADMIN_EMAILS             -> {"boss@t.dev"}
Consumers call these module-attr style, so the patches propagate.

Run:  PORT=8931 python3 tests/e2e/driver.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PORT = int(os.getenv("PORT", "8931"))
DB_PATH = os.getenv("DB_PATH", "/tmp/pivot_e2e.db")
os.environ["DB_PATH"] = DB_PATH
os.environ.pop("DATABASE_URL", None)  # always exercise SQLite in the sandbox

# Fresh fixture DB every launch
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB_PATH + suffix)
    except FileNotFoundError:
        pass

import app as appmod  # noqa: E402  (init_db() runs at import)
import database  # noqa: E402
import routes.deps as _deps  # noqa: E402
import services.email as _email_svc  # noqa: E402
import services.runner_client as _runner_svc  # noqa: E402

# ---------- patches ----------
_email_svc.send_email = lambda *a, **k: None
_deps.ADMIN_EMAILS = {"boss@t.dev"}


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def _fake_runner_http(method, path, json_body=None):
    """Stand-in runner: nothing actually runs, but every endpoint the app
    calls answers with a well-formed payload."""
    if path == "/internal/jobs":
        return _FakeResp(200, {"jobs": [], "capacity": 3})
    if path == "/execute" or path == "/run":
        return _FakeResp(200, {"output": "(fake runner) executed\n", "exit_code": 0})
    if path.endswith("/logs"):
        return _FakeResp(200, {"logs": "(fake runner) no logs\n", "status": "offline"})
    if path.startswith("/internal/jobs/"):
        # start / stop / restart / access / delete / single view
        if method == "GET":
            return _FakeResp(404, {"detail": "no such job"})
        return _FakeResp(200, {"ok": True, "id": "rid-fake", "status": "running",
                               "web": False})
    return _FakeResp(200, {})


_runner_svc._runner_http = _fake_runner_http

# ---------- fixtures ----------
conn = database.get_db_connection()
now = _deps.now_utc_str()
pw = _deps.hash_password("pass-123")

conn.execute(
    "INSERT INTO users (username, email, password, is_verified, role, created_at, updated_at)"
    " VALUES ('boss', 'boss@t.dev', ?, 1, 'user', ?, ?)",
    (pw, now, now),
)
conn.execute(
    "INSERT INTO users (username, email, password, is_verified, role, created_at, updated_at)"
    " VALUES ('regular1', 'regular1@t.dev', ?, 1, 'user', ?, ?)",
    (pw, now, now),
)
boss_id = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()["id"]

conn.execute(
    "INSERT INTO user_2fa (user_id, secret, is_enabled, backup_codes, created_at, updated_at)"
    " VALUES (?, 'JBSWY3DPEHPK3PXP', 1, ?, ?, ?)",
    (boss_id, json.dumps(["e2e-backup-code-0001", "e2e-backup-code-0002"]), now, now),
)

conn.execute(
    "INSERT INTO jobs (user_id, name, language, code, runner_job_id, created_at, updated_at)"
    " VALUES (?, 'shilo-bot', 'python', 'print(\"shilo online\")', 'rid-abc', ?, ?)",
    (boss_id, now, now),
)
conn.execute(
    "INSERT INTO jobs (user_id, name, language, code, runner_job_id, created_at, updated_at)"
    " VALUES (?, 'draft-api', 'python', '# draft — never deployed', NULL, ?, ?)",
    (boss_id, now, now),
)
conn.commit()
conn.close()

print(f"[driver] fixtures ready on {DB_PATH} — boss/regular1 (pass-123), boss 2FA on, 2 jobs")
print(f"[driver] serving REAL app on http://127.0.0.1:{PORT}")

import uvicorn  # noqa: E402

uvicorn.run(appmod.app, host="127.0.0.1", port=PORT, log_level="warning")
