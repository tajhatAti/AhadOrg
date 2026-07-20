"""End-to-end regression test for the SQLite path of the app.

Run:  python tests/test_sqlite_flow.py
Uses FastAPI's TestClient; mocks email sending so no Brevo key is needed.
"""
import os
import sys
import tempfile

# Force a temp SQLite DB before importing the app
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
# Ensure DATABASE_URL is unset so we exercise the SQLite path
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
from database import DIALECT  # noqa: E402

assert DIALECT == "sqlite", f"Expected sqlite dialect, got {DIALECT}"

# Mock email so signup/verify/reset flows don't need Brevo
import services.email as _email_svc
_email_svc.send_email = lambda *a, **k: None

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app.app)


def check(cond, msg):
    if not cond:
        print(f"  FAIL: {msg}")
        raise SystemExit(1)
    print(f"  ok: {msg}")


USERNAME = "ahad_test"
EMAIL = "ahadtest@example.com"
PASSWORD = "supersecret"

print("[1] health")
r = client.get("/health")
check(r.status_code == 200, "health 200")

print("[2] signup")
r = client.post("/signup", json={"username": USERNAME, "email": EMAIL, "password": PASSWORD, "agreed_terms": True})
check(r.status_code == 200, f"signup 200 (got {r.status_code} {r.text})")

# duplicate signup of an UNVERIFIED account now re-sends the OTP instead of
# erroring (so users who lose the OTP page while checking mail can finish).
r = client.post("/signup", json={"username": USERNAME, "email": "x@example.com", "password": PASSWORD, "agreed_terms": True})
check(r.status_code == 200 and r.json().get("resent") is True, "duplicate UNVERIFIED signup re-sends OTP")

# read OTP straight from DB to verify
import database  # noqa: E402

conn = database.get_db_connection()
row = conn.execute("SELECT otp FROM users WHERE username = ?", (USERNAME,)).fetchone()
otp = row["otp"]
conn.close()
check(bool(otp), "otp stored in DB")

print("[3] verify (auto-login)")
r = client.post("/verify", json={"username": USERNAME, "otp": otp})
check(r.status_code == 200, f"verify 200 (got {r.status_code} {r.text})")
token = r.json().get("token")
check(bool(token), "token returned after verify")
auth = {"Authorization": f"Bearer {token}"}

print("[4] login (by username and by email)")
r = client.post("/login", json={"username": USERNAME, "password": PASSWORD})
check(r.status_code == 200, "login by username")
r = client.post("/login", json={"username": EMAIL, "password": PASSWORD})
check(r.status_code == 200, "login by email")
# case-insensitive username login
r = client.post("/login", json={"username": "AHAD_TEST", "password": PASSWORD})
check(r.status_code == 200, "login by uppercase username (NOCASE)")

print("[5] profile")
r = client.get("/profile", headers=auth)
check(r.status_code == 200 and r.json()["username"] == USERNAME, "profile fetch")
r = client.post("/profile/update", headers=auth,
                json={"phone": "+8801000000000", "links": [{"label": "site", "url": "https://x.com"}]})
check(r.status_code == 200, "profile update")
r = client.get("/profile", headers=auth)
check(r.json()["phone"] == "+8801000000000", "phone persisted")

print("[6] removed vault-era endpoints are GONE (404 even when authed)")
for dead in ("/vault", "/vault/add", "/vault/update", "/vault/delete",
             "/notes", "/bookmarks", "/categories", "/api-keys",
             "/notifications", "/export-data", "/generate-password", "/qr"):
    r = client.get(dead, headers=auth)
    check(r.status_code == 404, f"GET {dead} -> 404")
    r = client.post(dead, headers=auth, json={})
    check(r.status_code == 404, f"POST {dead} -> 404")

print("[7] snippets CRUD + publish (Code Editor — kept feature)")
r = client.post("/snippets", headers=auth, json={"title": "hello.py", "language": "python", "content": "print('hi')"})
check("id" in r.json(), "snippet create returns id")
sid = r.json()["id"]
r = client.put("/snippets", headers=auth, json={"id": sid, "title": "hello v2"})
check(r.status_code == 200, "snippet update")
r = client.get("/snippets", headers=auth)
check(len(r.json()["snippets"]) == 1, "snippet list has 1")
r = client.post("/snippets/share", headers=auth, json={"id": sid, "share": True})
check(r.status_code == 200 and r.json().get("token"), "snippet publish returns token")
token = r.json()["token"]
r = client.get(f"/s/{token}")
check(r.status_code == 200, "public published page loads (no auth)")
r = client.post("/snippets/share", headers=auth, json={"id": sid, "share": False})
check(r.status_code == 200, "snippet unpublish")
r = client.get(f"/s/{token}")
check(r.status_code == 404, "unpublished page now 404s")
r = client.request("DELETE", "/snippets", headers=auth, json={"id": sid})
check(r.status_code == 200, "snippet delete")

print("[8] RunSpace — embedded engine executes in-process (single service)")
r = client.post("/api/execute", headers=auth, json={"language": "python", "code": "print(6*7)"})
check(r.status_code == 200 and "42" in (r.json().get("stdout") or ""), "execute -> real stdout via embedded runner")
r = client.get("/api/jobs", headers=auth)
check(r.status_code == 200 and r.json().get("max_per_user") == 3, "jobs list + per-user cap surfaced")

print("[9] global search (kept feature)")
client.post("/snippets", headers=auth, json={"title": "needle-script", "language": "python", "content": "x = 1"})
r = client.get("/search?q=needle", headers=auth)
check(r.status_code == 200 and any(x["kind"] == "snippet" for x in r.json()["results"]), "search finds snippet")

print("[10] preferences")
r = client.get("/preferences", headers=auth)
check(r.status_code == 200, "preferences get (default)")
r = client.put("/preferences", headers=auth, json={"theme": "light", "language": "bn"})
check(r.status_code == 200, "preferences update")
r = client.get("/preferences", headers=auth)
check(r.json()["theme"] == "light", "preferences persisted")

print("[11] dashboard stats (hosting product numbers, not vault counts)")
r = client.get("/stats", headers=auth)
check(r.status_code == 200, "stats 200")
body = r.json()
check("jobs_total" in body and "snippets" in body and "published" in body, "stats has hosting fields")
check("active_sessions" in body and body["active_sessions"] >= 1, "stats returns session count")

print("[12] activity-log")
r = client.post("/activity-log", headers=auth, json={"action": "test_action", "details": "ci"})
check(r.status_code == 200, "activity log add")
r = client.get("/activity-log", headers=auth)
check(any(a["action"] == "test_action" for a in r.json().get("activities", r.json() if isinstance(r.json(), list) else [])), "activity log list contains new entry")

print("[13] sessions")
r = client.get("/sessions", headers=auth)
check(len(r.json()["sessions"]) >= 1, "session list")

print("[14] 2FA setup + verify")
r = client.post("/2fa/setup", headers=auth, json={"enable": True})
check("secret" in r.json(), "2fa setup returns secret")
secret = r.json()["secret"]
import pyotp  # noqa: E402

code = pyotp.TOTP(secret).now()
r = client.post("/2fa/verify-setup", headers=auth, json={"code": code})
check(r.status_code == 200, f"2fa verify-setup (got {r.status_code} {r.text})")
r = client.get("/2fa/status", headers=auth)
check(r.json()["enabled"] is True, "2fa status enabled")
# run setup again (upsert path: INSERT OR REPLACE / ON CONFLICT) — a re-setup
# rotates the secret and returns 2FA to the pending state until re-verified
r = client.post("/2fa/setup", headers=auth, json={"enable": True})
check(r.status_code == 200, "2fa re-setup (upsert) works")
secret2 = r.json()["secret"]
r = client.post("/2fa/verify-setup", headers=auth, json={"code": pyotp.TOTP(secret2).now()})
check(r.status_code == 200, "2fa re-verify after rotation")
# one-click disable must NOT work — it requires password + a current code
r = client.post("/2fa/setup", headers=auth, json={"enable": False})
check(r.status_code == 400, "setup(enable=False) refused without confirmations")
r = client.post("/2fa/disable", headers=auth, json={"password": PASSWORD, "code": pyotp.TOTP(secret2).now()})
check(r.status_code == 200, f"2fa disable via /2fa/disable (got {r.status_code} {r.text})")
r = client.get("/2fa/status", headers=auth)
check(r.json()["enabled"] is False, "2fa status disabled")

print("[15] logout + invalid token")
r = client.post("/logout", headers=auth)
check(r.status_code == 200, "logout")
r = client.get("/profile", headers=auth)
check(r.status_code == 401, "old token rejected after logout")

print("[16] forgot/reset password flow")
r = client.post("/forgot-password", json={"email": EMAIL})
check(r.status_code == 200, "forgot-password")
conn = database.get_db_connection()
row = conn.execute("SELECT reset_otp FROM users WHERE email = ?", (EMAIL,)).fetchone()
reset_otp = row["reset_otp"]
conn.close()
r = client.post("/verify-reset-otp", json={"email": EMAIL, "otp": reset_otp})
check(r.status_code == 200, "verify-reset-otp")
r = client.post("/reset-password", json={"email": EMAIL, "otp": reset_otp, "new_password": "brandnewpw"})
check(r.status_code == 200, "reset-password")
r = client.post("/login", json={"username": USERNAME, "password": "brandnewpw"})
check(r.status_code == 200, "login with new password")

print("\nALL SQLITE TESTS PASSED ✅")
