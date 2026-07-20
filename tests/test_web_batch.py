# Public web product surfaces (post-vault):
# - job web_url composition (public slug / private key / no-runner safety)
# - published snippet pages still render + still carry the abuse-report link
# - vault-era public surfaces are unreachable
import os, sys, tempfile

os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from app import app, get_db_connection, hash_password, now_utc_str, _job_web_fields  # noqa: E402

c = TestClient(app)
results = []
def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("✓ " if cond else "✗ FAIL ") + name + (f" — {extra}" if extra else ""))

def make_user(username, email, password):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO users (username, email, password, is_verified, role, created_at, updated_at) VALUES (?, ?, ?, 1, 'user', ?, ?)",
        (username, email, hash_password(password), now_utc_str(), now_utc_str()))
    conn.commit(); conn.close()
    r = c.post("/login", json={"username": email, "email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]

def auth(t): return {"Authorization": f"Bearer {t}"}

# ---------------- published snippet page ----------------
tok = make_user("publisher", "pub@t.dev", "pass-123")
r = c.post("/snippets", json={"title": "Public hello", "language": "html",
                              "content": "<h1>hello RunSpace</h1>"}, headers=auth(tok))
sid = r.json()["id"]
r = c.post("/snippets/share", json={"id": sid, "share": True}, headers=auth(tok))
check("publish returns a share token", r.status_code == 200 and r.json().get("token"), r.text[:120])
token = r.json()["token"]
r = c.get("/s/" + token)
check("published page 200 with content", r.status_code == 200 and "hello RunSpace" in r.text)
# HTML snippets are the user's own full document — served verbatim by design.
# Non-HTML goes through the clean viewer, which must carry the abuse link.
r2 = c.post("/snippets", json={"title": "Viewer page", "language": "python",
                               "content": "print('viewer hello')"}, headers=auth(tok))
sid2 = r2.json()["id"]
rv = c.post("/snippets/share", json={"id": sid2, "share": True}, headers=auth(tok)).json()
r = c.get("/s/" + rv["token"])
check("clean viewer page 200 with content", r.status_code == 200 and "viewer hello" in r.text)
check("viewer page carries abuse-report link", "/report-abuse" in r.text)
r = c.post("/snippets/share", json={"id": sid, "share": False}, headers=auth(tok))
check("unpublish works", r.status_code == 200)
r = c.get("/s/" + token)
check("unpublished page gone (404)", r.status_code == 404)

# ---------------- job web_url composition ----------------
os.environ["RUNNER_SERVICE_URL"] = "https://ahad-code-runner.onrender.com"
f = _job_web_fields({"web_slug": "bot-1a2b3c", "web": True, "web_public": True})
check("public job gets web_url", f.get("web_url") == "https://ahad-code-runner.onrender.com/live/bot-1a2b3c/")
check("public job marked web", f.get("web") is True and f.get("web_public") is True)
check("public job has no private url", "web_private_url" not in f)
f = _job_web_fields({"web_slug": "bot-1a2b3c", "web": False, "web_public": False, "access_key": "K3Y"})
check("private job gets private url w/ key", f.get("web_private_url", "").endswith("/live/bot-1a2b3c/?key=K3Y"))
f = _job_web_fields({"status": "offline"})
check("no slug → no web fields", f == {})
del os.environ["RUNNER_SERVICE_URL"]
f = _job_web_fields({"web_slug": "x-1", "web": True, "web_public": True})
# embedded single-service mode: the gateway moves onto THIS app, so the URL
# is built from the site base (env) or the local dev fallback — never "{}".
expected_base = (os.getenv("SITE_BASE_URL", "").strip() or os.getenv("PUBLIC_BASE_URL", "").strip()
                 or os.getenv("RENDER_EXTERNAL_URL", "").strip()
                 or "http://127.0.0.1:" + os.getenv("PORT", "8000")).rstrip("/")
check("embedded mode → url on this service's base", f.get("web_url") == f"{expected_base}/live/x-1/")

# ---------------- jobs access endpoint guard rails ----------------
tok3 = make_user("jobtoggler", "jt@t.dev", "pass-789")
conn = get_db_connection()
uid = dict(conn.execute("SELECT id FROM users WHERE username='jobtoggler'").fetchone())["id"]
cur = conn.cursor()
cur.execute("INSERT INTO jobs (user_id, name, language, code, runner_job_id, created_at, updated_at) VALUES (?, 'no-runner', 'python', 'print(1)', NULL, ?, ?)",
            (uid, now_utc_str(), now_utc_str()))
job_no_runner = cur.lastrowid
conn.commit(); conn.close()
r = c.post(f"/api/jobs/{job_no_runner}/access", json={"public": False}, headers=auth(tok3))
check("access toggle w/o runner job → 409 with hint", r.status_code == 409 and "Restart" in r.text, r.text[:120])
r = c.post("/api/jobs/99999/access", json={"public": True}, headers=auth(tok3))
check("access toggle unknown job → 404", r.status_code == 404)

# ---------------- vault-era public surfaces are dead ----------------
for dead in ("/w/some-token", "/qr", "/generate-password", "/api-keys",
             "/wifi", "/recovery", "/seeds", "/export-data"):
    r = c.get(dead, headers=auth(tok3))
    check(f"{dead} → 404", r.status_code == 404)

# terms + abuse pages alive (public, unauthenticated)
r = c.get("/terms")
check("/terms 200 unauthenticated", r.status_code == 200 and "Terms of Service" in r.text)
r = c.get("/report-abuse")
check("/report-abuse 200 unauthenticated", r.status_code == 200)

fails = results.count(False)
print(f"\n================ {len(results)-fails} pass, {fails} fail ================")
sys.exit(1 if fails else 0)
