# FULL ROUTE MATRIX — exercise every DB-touching endpoint with real data and
# confirm none of them dies with a 500. This is the post-migration "has each
# route actually been called?" checklist the master prompt demands.
#
#   route -> exercised with real payload -> expected status (not 500) -> PASS
#
# Runner-dependent routes (jobs/execute) assert their GRACEFUL degradation
# (503 with a clear JSON detail) since no runner is configured in tests.
import os, sys, tempfile, json

os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyotp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
from app import app, get_db_connection, hash_password, now_utc_str  # noqa: E402

# Email delivery is a no-op in the test sandbox (no SMTP configured there by design)
import services.email as _email_svc
_email_svc.send_email = lambda *a, **k: None

c = TestClient(app)
results = []
def check(name, r, ok_codes=(200, 201)):
    good = (r.status_code in ok_codes) and r.status_code != 500
    results.append((name, r.status_code, bool(good)))
    print(("✓ " if good else "✗ FAIL ") + f"{name:56s} [{r.status_code}]" + (f" — {r.text[:100]}" if not good else ""))

def conn_exec(sql, params=()):
    # fetch rows BEFORE closing — a sqlite cursor is dead after conn.close()
    conn = get_db_connection()
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    conn.commit(); conn.close()
    return rows

# ---------- auth ----------
conn_exec("INSERT INTO users (username, email, password, is_verified, role, created_at, updated_at) VALUES ('matrix','mx@t.dev',?,1,'user',?,?)",
          (hash_password("Pw-matrix-1"), now_utc_str(), now_utc_str()))
r = c.post("/auth/check-availability", json={"username": "matrix-check-xyz"}); check("POST /auth/check-availability (free name)", r)
r = c.post("/auth/check-availability", json={"username": "matrix"}); check("POST /auth/check-availability (taken → still no 500)", r, (200, 400, 409))
r = c.post("/login", json={"username": "mx@t.dev", "email": "mx@t.dev", "password": "Pw-matrix-1"})
check("POST /login", r); tok = r.json()["token"]; H = {"Authorization": f"Bearer {tok}"}
r = c.get("/profile", headers=H); check("GET /profile", r)
r = c.post("/profile/update", json={"phone": "+8801700000000"}, headers=H); check("POST /profile/update", r)

# signup + OTP routes (synthetic OTP row → real verify path)
conn_exec("INSERT INTO users (username, email, password, otp, otp_created_at, is_verified, role, created_at, updated_at) VALUES ('otpuser','otp@t.dev',?,'123456',?,0,'user',?,?)",
          (hash_password("Pw-otp-1"), now_utc_str(), now_utc_str(), now_utc_str()))
r = c.post("/verify", json={"username": "otpuser", "otp": "123456"}); check("POST /verify (valid OTP)", r)
r = c.post("/resend-otp", json={"username": "nope@t.dev"}); check("POST /resend-otp (unknown user, graceful)", r, (200, 400, 404))

# forgot-password flow with a planted reset OTP
conn_exec("UPDATE users SET otp='654321', otp_created_at=? WHERE username='matrix'", (now_utc_str(),))
r = c.post("/forgot-password", json={"email": "mx@t.dev"}); check("POST /forgot-password", r, (200, 201))
conn_exec("UPDATE users SET reset_otp='654321', reset_otp_created_at=? WHERE username='matrix'", (now_utc_str(),))
r = c.post("/verify-reset-otp", json={"email": "mx@t.dev", "otp": "654321"}); check("POST /verify-reset-otp", r, (200, 201))
r = c.post("/reset-password", json={"email": "mx@t.dev", "otp": "654321", "new_password": "Pw-matrix-1"}); check("POST /reset-password", r, (200, 201, 400))

r = c.post("/login", json={"username": "mx@t.dev", "password": "Pw-matrix-1"}); tokA = r.json()["token"]; HA = {"Authorization": f"Bearer {tokA}"}
r2 = c.post("/login", json={"username": "mx@t.dev", "password": "Pw-matrix-1"}); tokB = r2.json()["token"]; H2 = {"Authorization": f"Bearer {tokB}"}
r = c.get("/sessions", headers=HA); check("GET /sessions", r)
sessions_list = r.json().get("sessions") or []
victim = next((s for s in sessions_list if not s.get("is_current")), sessions_list[0] if sessions_list else {})
r = c.post("/sessions/revoke", json={"session_id": victim.get("id") or 0}, headers=HA); check("POST /sessions/revoke", r, (200, 400, 404))
# main ops token — minted AFTER the revoke so it survives the whole matrix
r3 = c.post("/login", json={"username": "mx@t.dev", "password": "Pw-matrix-1"}); tok2 = r3.json()["token"]; H2 = {"Authorization": f"Bearer {tok2}"}
r = c.get("/login-history", headers=H2); check("GET /login-history", r)

# ---------- 2FA ----------
r = c.get("/2fa/status", headers=H2); check("GET /2fa/status", r)
r = c.post("/2fa/setup", json={"enable": True}, headers=H2); check("POST /2fa/setup", r)
secret = r.json()["secret"]
r = c.post("/2fa/verify-setup", json={"code": pyotp.TOTP(secret).now()}, headers=H2); check("POST /2fa/verify-setup", r)
stale_code = (r.json().get("backup_codes") or ["x"])[0]
# Backup-code REGENERATION must kill the old set — correct security behaviour.
r = c.post("/2fa/backup-codes", json={"password": "Pw-matrix-1", "code": pyotp.TOTP(secret).now()}, headers=H2); check("POST /2fa/backup-codes", r)
fresh_code = (r.json().get("backup_codes") or ["x"])[0]
r = c.post("/2fa/verify-login", json={"token": "bogus", "code": "000000"}); check("POST /2fa/verify-login (graceful reject)", r, (400, 401, 404))
r = c.post("/2fa/disable", json={"password": "Pw-matrix-1", "code": stale_code}, headers=H2); check("POST /2fa/disable (stale code correctly dead)", r, (400,))
r = c.post("/2fa/disable", json={"password": "Pw-matrix-1", "code": fresh_code}, headers=H2); check("POST /2fa/disable (fresh backup code)", r)

# ---------- section CRUD ----------
def crud(section, create, update_key_id, update):
    r = c.post(section, json=create, headers=H2); check(f"POST {section} (create)", r, (200, 201))
    iid = r.json().get("id")
    r = c.get(section, headers=H2); check(f"GET {section} (list)", r)
    if iid:
        u = dict(update); u[update_key_id] = iid
        r = c.put(section, json=u, headers=H2); check(f"PUT {section} (update)", r)
        r = c.request("DELETE", section, json={update_key_id: iid}, headers=H2); check(f"DELETE {section}", r)

# note: vault uses route-style paths (/vault/add|update|delete)
def vault_crud():
    r = c.post("/vault/add", json={"type": "password", "label": "G", "value": "v"}, headers=H2); check("POST /vault/add", r, (200, 201))
    vid = r.json().get("id")
    r = c.get("/vault", headers=H2); check("GET /vault", r)
    if vid:
        r = c.post("/vault/update", json={"id": vid, "type": "password", "label": "G2", "value": "v2"}, headers=H2); check("POST /vault/update", r)
        r = c.post("/vault/delete", json={"id": vid}, headers=H2); check("POST /vault/delete", r)
crud("/snippets", {"title": "hello", "language": "python", "content": "print(1)"}, "id", {"title": "hello2", "language": "python", "content": "print(2)"})

r = c.get("/search?q=amma", headers=H2); check("GET /search", r)
r = c.post("/snippets", json={"title": "pub", "language": "html", "content": "<h1>hi</h1>"}, headers=H2)
sid = r.json()["id"]
r = c.post("/snippets/share", json={"id": sid, "share": True}, headers=H2); check("POST /snippets/share", r)
share_tok = r.json()["token"]
r = c.get("/s/" + share_tok); check("GET /s/{token} (published page)", r)
try:
    r = c.get(f"/code/s/{share_tok}", follow_redirects=False)
except TypeError:
    r = c.get(f"/code/s/{share_tok}")
check("GET /code/s/{token} (legacy link healed)", r, (200, 301, 302, 307, 308))

# ---------- jobs + execute (embedded runner → REAL execution here) ----------
# Single-service mode activates automatically (no RUNNER_SERVICE_URL in the
# sandbox), so these routes now exercise the in-process engine for real.
r = c.get("/api/jobs", headers=H2); check("GET /api/jobs (200, runner state reported)", r)
r = c.post("/api/jobs", json={"name": "j", "language": "python", "code": "print(1)"}, headers=H2)
check("POST /api/jobs (embedded: real 201)", r, (200, 201))
r = c.post("/api/execute", json={"language": "python", "code": "print(1)"}, headers=H2)
check("POST /api/execute (embedded: real 200)", r)
ok_exec = r.status_code == 200 and (r.json().get("stdout") or "").strip() == "1"
results.append(("execute stdout == '1'", r.status_code, ok_exec))
print(("✓ " if ok_exec else "✗ FAIL ") + f"{'execute stdout == 1 (really ran in-process)':56s} [{r.status_code}]")
conn_exec("INSERT INTO jobs (user_id, name, language, code, runner_job_id, created_at, updated_at) SELECT id,'jx','python','x',NULL,?,? FROM users WHERE username='matrix'", (now_utc_str(), now_utc_str()))
jid = dict(conn_exec("SELECT id FROM jobs WHERE name='jx'")[0])["id"]
r = c.get(f"/api/jobs/{jid}/logs", headers=H2); check("GET /api/jobs/{id}/logs (never-started)", r)
r = c.post(f"/api/jobs/{jid}/stop", headers=H2); check("POST /api/jobs/{id}/stop (no runner id)", r)
r = c.post(f"/api/jobs/{jid}/restart", headers=H2); check("POST /api/jobs/{id}/restart (graceful)", r, (200, 502, 503))
# after the restart above the job IS live on the embedded runner, so the
# access toggle works for real (it 409s only when there's no runner job).
r = c.post(f"/api/jobs/{jid}/access", json={"public": False}, headers=H2)
check("POST /api/jobs/{id}/access (live → 200)", r, (200, 409))
r = c.delete(f"/api/jobs/{jid}", headers=H2); check("DELETE /api/jobs/{id}", r)

# ---------- misc ----------
r = c.get("/preferences", headers=H2); check("GET /preferences", r)
r = c.put("/preferences", json={"theme": "light", "language": "en", "timezone": "Asia/Dhaka", "notifications_enabled": True, "email_notifications": False}, headers=H2); check("PUT /preferences", r)
r = c.get("/activity-log", headers=H2); check("GET /activity-log", r)
r = c.post("/activity-log", json={"action": "matrix-test", "details": "x"}, headers=H2); check("POST /activity-log", r, (200, 201, 400))
r = c.get("/stats", headers=H2); check("GET /stats", r)
# 2FA is disabled by now, so a plain current+new password change must succeed.
# ---------- the vault is GONE — not hidden, GONE ----------
for dead in ("/vault", "/notes", "/bookmarks", "/cards", "/tasks", "/identities",
             "/contacts", "/wifi", "/servers", "/recovery", "/seeds",
             "/api-keys", "/notifications", "/export-data", "/generate-password",
             "/qr?q=x", "/categories"):
    rr = c.get(dead, headers=H2)
    check(f"dead route {dead} → 404/405", rr, (404, 405))

r = c.get("/terms"); check("GET /terms (ToS page)", r)
r = c.get("/report-abuse"); check("GET /report-abuse", r)
r = c.post("/report-abuse", json={"url": "https://example.onrender.com/live/x", "reason": "spam test"}); check("POST /report-abuse", r, (200, 201))

r = c.post("/account/change-password", json={"current_password": "Pw-matrix-1", "new_password": "Pw-matrix-2"}, headers=H2); check("POST /account/change-password", r)
r = c.post("/logout", headers=H2); check("POST /logout", r)

# ---------- destructive last: delete-account on a throwaway user ----------
conn_exec("INSERT INTO users (username, email, password, is_verified, role, created_at, updated_at) VALUES ('gone','gone@t.dev',?,1,'user',?,?)",
          (hash_password("Pw-gone-1"), now_utc_str(), now_utc_str()))
r = c.post("/login", json={"username": "gone@t.dev", "password": "Pw-gone-1"})
tg = r.json()["token"]
r = c.post("/account/delete", json={"password": "Pw-gone-1"}, headers={"Authorization": f"Bearer {tg}"}); check("POST /account/delete (throwaway user)", r, (200, 400))

fails = [x for x in results if not x[2]]
print(f"\n================ {len(results)-len(fails)} pass, {len(fails)} fail ================")
if fails:
    for name, code, _ in fails: print(f"  … {name} [{code}]")
sys.exit(1 if fails else 0)
