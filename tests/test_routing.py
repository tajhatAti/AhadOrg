# Backend tests for client-side URL routing (post-vault surface):
# - Browser navigation (Accept: text/html, no Authorization) on a section URL
#   -> serves the SPA shell (index.html), even on paths that collide with API.
# - Authed fetch on the SAME path -> the API JSON (negotiation intact).
# - Pure client paths (/dashboard, /code, /sign-in) serve the shell.
# - Vault-era paths are now plain 404s — no shell, no data, nothing.
# - Real public pages (/health, /s/{token}, /terms) unaffected.
import os, sys, tempfile

os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from app import app, get_db_connection, hash_password, now_utc_str  # noqa: E402

c = TestClient(app)
results = []
def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("✓ " if cond else "✗ FAIL ") + name + (f" — {extra}" if extra else ""))

HTML_NAV = {"Accept": "text/html,application/xhtml+xml"}

# log in for the API-half of the negotiation checks
conn = get_db_connection()
conn.execute(
    "INSERT INTO users (username, email, password, is_verified, role, created_at, updated_at) "
    "VALUES ('router', 'rt@t.dev', ?, 1, 'user', ?, ?)",
    (hash_password("pass-r"), now_utc_str(), now_utc_str()),
)
conn.commit(); conn.close()
tok = c.post("/login", json={"username": "rt@t.dev", "email": "rt@t.dev", "password": "pass-r"}).json()["token"]
AUTH = {"Authorization": f"Bearer {tok}"}

# /profile collides with the JSON API — the only negotiated path left.
COLLIDERS = ["/profile"]
DIRECT = ["/dashboard", "/code", "/jobs", "/runspace", "/activity",
          "/sign-in", "/sign-up", "/login", "/forgot"]

for p in COLLIDERS:
    r = c.get(p, headers=HTML_NAV)
    check(f"browser nav {p} -> SPA shell", r.status_code == 200 and "text/html" in r.headers.get("content-type", "") and "<html" in r.text.lower())

for p in COLLIDERS:
    r = c.get(p, headers=AUTH)
    check(f"authed fetch {p} -> JSON data", r.status_code == 200 and "json" in r.headers.get("content-type", ""))

for p in DIRECT:
    r = c.get(p, headers=HTML_NAV)
    check(f"client path {p} -> SPA shell", r.status_code == 200 and "<html" in r.text.lower())

# plain GET / still the shell; /health + /terms unaffected
r = c.get("/", headers=HTML_NAV)
check("GET / still serves landing shell", r.status_code == 200 and "<html" in r.text.lower())
r = c.get("/health")
check("/health untouched", r.status_code == 200 and r.json().get("status") == "ok")
r = c.get("/terms")
check("/terms serves the ToS page", r.status_code == 200 and "Acceptable use" in r.text)

# POST on the colliding path must STILL be the API (not the shell)
r = c.post("/profile/update", json={"phone": "+8801700000000"}, headers=AUTH)
check("POST /profile/update still works via API", r.status_code in (200, 201), r.text[:120])

# ---- the vault is gone: every legacy path is a flat 404, browser or fetch ----
DEAD = ["/contacts", "/wifi", "/vault", "/cards", "/identities", "/servers",
        "/recovery", "/notes", "/bookmarks", "/tasks", "/seeds",
        "/api-keys", "/notifications", "/export-data", "/generate-password", "/qr"]
for p in DEAD:
    r = c.get(p, headers=HTML_NAV)
    check(f"browser nav {p} -> 404 (no shell)", r.status_code == 404)
for p in DEAD:
    r = c.get(p, headers=AUTH)
    check(f"authed fetch {p} -> 404 (no data)", r.status_code == 404 and "json" not in r.headers.get("content-type", "").replace("application/json", "json") or r.status_code == 404)

fails = results.count(False)
print(f"\n================ {len(results)-fails} pass, {fails} fail ================")
sys.exit(1 if fails else 0)
