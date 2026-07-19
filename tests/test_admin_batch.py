# Backend tests for the developer-first pivot:
# ToS gate at signup, per-IP daily signup cap, admin grant/404 stealth,
# overview stats, suspend/reactivate with the admin's OWN 2FA, suspended
# lockout (login 403 + token 401), max-3-jobs server cap, public abuse inbox.
import os, sys, tempfile

os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyotp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
from app import app, get_db_connection, hash_password, now_utc_str  # noqa: E402

# No outbound mail / runner calls inside tests.
appmod.send_email = lambda *a, **k: None
appmod._runner_http = lambda *a, **k: None

c = TestClient(app)
TEST_IP = "testclient"

results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("✓ " if cond else "✗ FAIL ") + name + (f" — {extra}" if extra else ""))


def make_user(username, email, password, with_2fa=False):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (username, email, password, is_verified, role, created_at, updated_at) "
        "VALUES (?, ?, ?, 1, 'user', ?, ?)",
        (username, email, hash_password(password), now_utc_str(), now_utc_str()),
    )
    uid = cur.lastrowid
    conn.commit()
    conn.close()
    r = c.post("/login", json={"username": email, "email": email, "password": password})
    assert r.status_code == 200, (username, r.text)
    tok = r.json()["token"]
    if with_2fa:
        h = {"Authorization": f"Bearer {tok}"}
        s = c.post("/2fa/setup", json={"enable": True}, headers=h)
        assert s.status_code == 200, s.text
        secret = s.json()["secret"]
        v = c.post("/2fa/verify-setup", json={"code": pyotp.TOTP(secret).now()}, headers=h)
        assert v.status_code == 200, v.text
    return uid, tok


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def db_secret(uid):
    conn = get_db_connection()
    row = conn.execute("SELECT secret FROM user_2fa WHERE user_id = ?", (uid,)).fetchone()
    conn.close()
    return row["secret"] if row else None


# ============ 1) SIGNUP GATE: Terms of Use required ============
r = c.post("/signup", json={"username": "tosuser1", "email": "tos1@t.dev", "password": "pass-123"})
check("signup WITHOUT agreed_terms → 400", r.status_code == 400 and "Terms" in r.text, r.text[:90])
r = c.post("/signup", json={"username": "tosuser1", "email": "tos1@t.dev",
                            "password": "pass-123", "agreed_terms": True})
check("signup WITH agreed_terms → 200", r.status_code == 200, r.text[:90])
conn = get_db_connection()
row = conn.execute("SELECT agreed_terms_at FROM users WHERE email = 'tos1@t.dev'").fetchone()
conn.close()
check("agreed_terms_at stored", bool(row and row["agreed_terms_at"]))

# ============ 2) SIGNUP DAILY CAP (10/IP/day) ============
# The 5-minute base limiter would trip first in a fast test loop — clear only
# THAT bucket between calls so the daily bucket is what we exercise.
appmod._attempts.pop(f"{TEST_IP}:signup", None)
appmod._attempts.pop(f"{TEST_IP}:signup:daily", None)
ok = 0
for i in range(10):
    r = c.post("/signup", json={"username": f"capuser{i}", "email": f"cap{i}@t.dev",
                                "password": "pass-123", "agreed_terms": True})
    if r.status_code == 200:
        ok += 1
    appmod._attempts.pop(f"{TEST_IP}:signup", None)
check("10 signups from one IP succeed", ok == 10, f"ok={ok}")
r = c.post("/signup", json={"username": "capuserX", "email": "capX@t.dev",
                            "password": "pass-123", "agreed_terms": True})
check("11th signup same IP → 429 daily cap", r.status_code == 429 and "Too many new accounts" in r.text, r.text[:90])

# ============ 3) ADMIN VISIBILITY: 404 for everyone else ============
uid_u, tok_u = make_user("regular1", "regular1@t.dev", "pass-123")
r = c.get("/admin/overview", headers=auth(tok_u))
check("non-admin /admin/overview → 404 (stealth)", r.status_code == 404)
for p in ("/admin/users", "/admin/jobs", "/admin/audit-log", "/admin/abuse-reports"):
    check(f"non-admin {p} → 404", c.get(p, headers=auth(tok_u)).status_code == 404)
r = c.post("/admin/users/set-suspended", json={"user_id": uid_u, "suspended": True}, headers=auth(tok_u))
check("non-admin suspend attempt → 404", r.status_code == 404)
check("no-token /admin/overview → 401", c.get("/admin/overview").status_code == 401)

# ============ 4) ADMIN GRANT via env + /profile flag ============
appmod.ADMIN_EMAILS = {"boss@t.dev"}
uid_a, tok_a = make_user("boss", "boss@t.dev", "pass-123", with_2fa=True)
prof = c.get("/profile", headers=auth(tok_a)).json()
check("admin profile is_admin=True", prof.get("is_admin") is True)
check("admin profile exposes id", prof.get("id") == uid_a)
prof_u = c.get("/profile", headers=auth(tok_u)).json()
check("regular profile is_admin=False", prof_u.get("is_admin") is False)

# ============ 5) ADMIN OVERVIEW STATS ============
conn = get_db_connection()
for i in range(2):  # two deployed-looking jobs for the regular user
    conn.execute(
        "INSERT INTO jobs (user_id, name, language, code, runner_job_id, created_at, updated_at) "
        "VALUES (?, ?, 'python', 'print(1)', ?, ?, ?)",
        (uid_u, f"job-{i}", f"rid-{i}", now_utc_str(), now_utc_str()))
conn.execute(
    "INSERT INTO jobs (user_id, name, language, code, runner_job_id, created_at, updated_at) "
    "VALUES (?, 'saved-only', 'python', 'print(1)', NULL, ?, ?)",
    (uid_u, now_utc_str(), now_utc_str()))
conn.commit()
conn.close()
r = c.get("/admin/overview", headers=auth(tok_a))
check("admin overview 200", r.status_code == 200, r.text[:120])
ov = r.json()
check("overview counts users", ov.get("users", 0) >= 12, str(ov.get("users")))
check("overview jobs_total/jobs_deployed", ov.get("jobs_total") == 3 and ov.get("jobs_deployed") == 2, str(ov))
check("overview capacity = users × 3", ov.get("capacity_max") == ov.get("users") * 3)
check("overview signups_daily is a series", isinstance(ov.get("signups_daily"), list))

r = c.get("/admin/jobs", headers=auth(tok_a))
jobs = r.json().get("jobs", [])
check("admin jobs lists metadata only", len(jobs) == 3 and "code" not in jobs[0], str(jobs[:1]))
check("admin jobs shows owner", jobs[0].get("owner") == "regular1")

# ============ 6) SUSPEND / REACTIVATE (admin 2FA gate) ============
uid_b, tok_b = make_user("boss2", "boss2@t.dev", "pass-123")  # admin by email? no — not in ADMIN_EMAILS
appmod.ADMIN_EMAILS = {"boss@t.dev", "boss2@t.dev"}
c.post("/login", json={"username": "boss2@t.dev", "password": "pass-123"})  # grant fires
r = c.post("/admin/users/set-suspended", json={"user_id": uid_u, "suspended": True}, headers=auth(tok_b))
check("admin WITHOUT 2FA suspending → 409", r.status_code == 409, r.text[:120])

bad = {"user_id": uid_u, "suspended": True, "code": "000000"}
r = c.post("/admin/users/set-suspended", json=bad, headers=auth(tok_a))
check("bad 2FA code → 400", r.status_code == 400, r.text[:120])

r = c.post("/admin/users/set-suspended", json={"user_id": uid_a, "suspended": True, "code": "000000"}, headers=auth(tok_a))
check("self-suspend blocked → 400", r.status_code == 400, r.text[:120])

secret = db_secret(uid_a)
r = c.post("/admin/users/set-suspended",
           json={"user_id": uid_u, "suspended": True, "code": pyotp.TOTP(secret).now()},
           headers=auth(tok_a))
check("suspend with valid 2FA → 200", r.status_code == 200, r.text[:120])
check("suspended user's token → 401", c.get("/profile", headers=auth(tok_u)).status_code == 401)
r = c.post("/login", json={"username": "regular1@t.dev", "password": "pass-123"})
check("suspended user login → 403", r.status_code == 403 and "suspended" in r.text.lower(), r.text[:120])
r = c.post("/api/jobs", json={"name": "x", "language": "python", "code": "print(1)"}, headers=auth(tok_u))
check("suspended user jobs API → 401", r.status_code == 401)

r = c.post("/admin/users/set-suspended",
           json={"user_id": uid_u, "suspended": False, "code": pyotp.TOTP(secret).now()},
           headers=auth(tok_a))
check("reactivate → 200", r.status_code == 200, r.text[:120])
appmod._attempts.pop(f"{TEST_IP}:login:regular1@t.dev", None)  # dodge the base login limiter
r = c.post("/login", json={"username": "regular1@t.dev", "password": "pass-123"})
check("reactivated user can login again", r.status_code == 200, r.text[:120])
tok_u = r.json()["token"]

r = c.get("/admin/audit-log", headers=auth(tok_a))
acts = [a["action"] for a in r.json().get("audit", [])]
check("audit log records suspend + reactivate", "suspend" in acts and "reactivate" in acts, str(acts))

# ============ 7) MAX-3-JOBS CAP (server-side) ============
r = c.post("/api/jobs", json={"name": "one-too-many", "language": "python", "code": "print(1)"}, headers=auth(tok_u))
check("4th job → 429 cap", r.status_code == 429 and "Max 3" in r.text, r.text[:120])

# ============ 8) ABUSE INBOX ============
r = c.post("/report-abuse", json={"url": "https://runner.example/live/shady-bot/", "reason": "phishing"})
check("public report-abuse → 200", r.status_code == 200, r.text[:120])
r = c.post("/report-abuse", json={"url": "   "})
check("blank URL → 400", r.status_code == 400, r.text[:120])
r = c.get("/admin/abuse-reports", headers=auth(tok_a))
reps = r.json().get("reports", [])
check("admin sees the report", any("shady-bot" in (x.get("url") or "") for x in reps), str(reps[:1]))
r = c.get("/report-abuse")
check("report page renders", r.status_code == 200 and "Report abuse" in r.text)

# ============ 9) SPA SHELL PATHS ============
for p in ("/runspace", "/admin"):
    r = c.get(p, headers={"Accept": "text/html"})
    check(f"{p} serves the SPA shell", r.status_code == 200 and "RunSpace" in r.text)

print(f"\n{sum(1 for _, ok in results if ok)}/{len(results)} checks passed")
if not all(ok for _, ok in results):
    sys.exit(1)
