"""Ahad Co — RunSpace: free code/bot hosting.

Thin ASGI shell: app init, middleware, static mount, the SPA host (landing +
deep-link negotiation for client-routed sections), /terms, /health, and the
include_router lines for every domain module in routes/.
"""
import os
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from database import DIALECT, init_db  # noqa: F401  (init_db already ran via routes.deps)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("ahad-co-app")

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
TERMS_FILE = BASE_DIR / "terms.html"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Ahad Co — RunSpace")


def _enable_embedded_runner() -> bool:
    """Single-service mode: when RUNNER_SERVICE_URL is NOT set, the job runner
    lives inside THIS process (one Render web service — the whole point of the
    consolidation). Two effects:

      1. services.runner_client talks to the runner through an in-process
         ASGI client instead of the network.
      2. The public /live/{slug}/* gateway (HTTP + WebSocket) is mounted on
         THIS app — the handlers are reused verbatim from runner.app.

    Setting RUNNER_SERVICE_URL restores the classic two-service layout and
    this function leaves everything alone (return False).
    """
    if os.getenv("RUNNER_SERVICE_URL", "").strip():
        return False
    import secrets as _secrets
    # runner.app reads SECRET at import; generate a throwaway internal one
    # unless the operator pinned their own.
    os.environ.setdefault("RUNNER_SERVICE_SECRET", _secrets.token_urlsafe(24))
    import runner.app as _rapp
    # Visitor-facing pages/URLs must point at THIS service, not a runner host.
    base = (os.getenv("SITE_BASE_URL", "").strip()
            or os.getenv("PUBLIC_BASE_URL", "").strip()
            or os.getenv("RENDER_EXTERNAL_URL", "").strip())


    if base and not _rapp.PUBLIC_BASE_URL:
        _rapp.PUBLIC_BASE_URL = base.rstrip("/")
    from services.proxy import router as _proxy_router
    app.include_router(_proxy_router)
    logger.info("Embedded runner ACTIVE — jobs + /live gateway run in this process.")
    return True


EMBEDDED_RUNNER = _enable_embedded_runner()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# -------------------------------
# SPA host (landing + client-routed sections)
# -------------------------------
@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def read_index():
    if not INDEX_FILE.exists():
        raise HTTPException(status_code=404, detail="index.html not found.")
    return FileResponse(INDEX_FILE)


# Client-side routing: every app section has a real URL (/code, /jobs, …).
# Deep links / refreshes on these paths must serve the SPA shell — the
# frontend router reads the path and opens the right section (after auth).
#
# The API lives at ROOT (GET /profile returns JSON), so section URLs that
# collide with an API GET need content negotiation: a browser navigation
# (Accept: text/html, no Authorization header — tokens ride fetch headers,
# never visible in address-bar navigations) gets the SPA shell, while the
# SPA's own authed fetch on the same path gets the JSON data. Registered
# BEFORE the API routes on purpose, since route order decides the match.
def _spa_negotiator(fn_name: str):
    def handler(request: Request):
        accept = (request.headers.get("accept") or "").lower()
        auth_hdr = request.headers.get("authorization")
        if "text/html" in accept and not auth_hdr:
            if not INDEX_FILE.exists():
                raise HTTPException(status_code=404, detail="index.html not found.")
            return FileResponse(INDEX_FILE)
        fn = _NEGOTIATED_FNS.get(fn_name)   # resolved at request time
        if fn is None:
            raise HTTPException(status_code=404, detail="Not found.")
        return fn(authorization=auth_hdr)
    return handler


from routes.profile import get_profile as _negotiated_profile


_NEGOTIATED = {
    "profile": "get_profile",
}
_NEGOTIATED_FNS = {"get_profile": _negotiated_profile}
for _p, _fn in _NEGOTIATED.items():
    app.get("/" + _p, include_in_schema=False)(_spa_negotiator(_fn))

# Section URLs with NO API collision can serve the shell directly.
CLIENT_ONLY_PATHS = [
    "dashboard", "code", "jobs", "runspace", "admin", "activity",
    "sign-in", "sign-up", "login", "forgot",
]
for _p in CLIENT_ONLY_PATHS:
    app.get("/" + _p, include_in_schema=False)(read_index)

# Back-compat heal: a frontend bug once produced published-page links like
# /code/s/<token> (the tab path was glued onto the origin). Redirect any
# shared copies to the real public page instead of a cold JSON 404.
@app.get("/code/s/{token}", include_in_schema=False)
def code_share_redirect(token: str):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/s/{token}", status_code=301)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "database": DIALECT,
        "runner": "embedded" if EMBEDDED_RUNNER else "remote",
        "brevo_api_key_set": bool(os.getenv("BREVO_API_KEY", "").strip()),
        "sender_email_set": bool(os.getenv("SENDER_EMAIL", "").strip()),
    }


# ----------------------------
# Signup / Verify (auto-login) / Resend
# ----------------------------


@app.get("/terms", include_in_schema=False)
def terms_page():
    if not TERMS_FILE.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(TERMS_FILE)


# -------------------------------
# Domain routers
# -------------------------------
from routes.auth import router as auth_router
from routes.profile import router as profile_router
from routes.dashboard import router as dashboard_router
from routes.code_editor import router as code_editor_router
from routes.runspace import router as runspace_router
from routes.admin import router as admin_router

for _r in (auth_router, profile_router, dashboard_router,
           code_editor_router, runspace_router, admin_router):
    app.include_router(_r)


# ---- back-compat re-exports (tests + drivers import these from `app`) ----
from routes.deps import get_db_connection, hash_password, now_utc_str  # noqa: E402,F401
from services.runner_client import _job_web_fields, _runner_http  # noqa: E402,F401
