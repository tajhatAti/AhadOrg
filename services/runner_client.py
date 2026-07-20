"""Client for the RunSpace runner (job execution + management) — TWO modes:

  • EMBEDDED (default): RUNNER_SERVICE_URL unset → the runner lives INSIDE
    this process (single Render web service). Calls go through an in-process
    ASGI client (fastapi.testclient.TestClient) — identical request/response
    semantics, zero network, zero second service.

  • REMOTE: RUNNER_SERVICE_URL set → classic server-to-server HTTPS calls to
    the separate runner service (kept for anyone running the two-service
    layout; the runner/ folder still ships standalone).

Runner URL/secret stay server-side; the browser never sees them. Test drivers
monkeypatch runner_client._runner_http — call it module-attr style.
"""
import os
import logging

import requests
from fastapi import HTTPException

logger = logging.getLogger("ahad-co-app")

MAX_JOBS_PER_USER = 3  # free tier guardrail


def runner_cfg():
    url = os.getenv("RUNNER_SERVICE_URL", "").strip().rstrip("/")
    secret = os.getenv("RUNNER_SERVICE_SECRET", "").strip()
    return url, secret


def embedded_mode() -> bool:
    """Single-service deployment → the runner runs in-process."""
    return not os.getenv("RUNNER_SERVICE_URL", "").strip()


def public_base_url() -> str:
    """Where /live/* is publicly reachable.
    Embedded → this main service. Remote → the runner service."""
    runner_url = os.getenv("RUNNER_SERVICE_URL", "").strip().rstrip("/")
    if runner_url:
        return runner_url
    base = (
        os.getenv("SITE_BASE_URL", "").strip()
        or os.getenv("PUBLIC_BASE_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    )
    if not base:  # local dev fallback
        base = "http://127.0.0.1:{}".format(os.getenv("PORT", "8000"))
    return base.rstrip("/")


_tc = None


def _embedded_client():
    """In-process ASGI client bound to the runner app. Created lazily so the
    app module itself can decide activation order (secret generation first)."""
    global _tc
    if _tc is None:
        from fastapi.testclient import TestClient
        import runner.app as rapp
        _tc = TestClient(rapp.app, raise_server_exceptions=False)
    return _tc


def _runner_http(method: str, path: str, json_body=None):
    """Call the runner (embedded or remote) with the shared secret; map every
    transport failure to a clean HTTPException the frontend can display."""
    if embedded_mode():
        secret = os.getenv("RUNNER_SERVICE_SECRET", "").strip()
        if not secret:
            raise HTTPException(status_code=503, detail="Runner is not configured.")
        try:
            return _embedded_client().request(
                method, path, json=json_body,
                headers={"Authorization": "Bearer " + secret},
                timeout=130,
            )
        except HTTPException:
            raise
        except Exception as exc:  # in-process — a failure here means the runner itself blew up
            logger.exception("embedded runner call failed: %s %s", method, path)
            raise HTTPException(status_code=503, detail=f"Job engine error — please try again. ({type(exc).__name__})")

    runner_url, runner_secret = runner_cfg()
    if not runner_url or not runner_secret:
        raise HTTPException(status_code=503, detail="Jobs are not configured. Set RUNNER_SERVICE_URL and RUNNER_SERVICE_SECRET.")
    try:
        return requests.request(
            method, runner_url + path,
            json=json_body,
            headers={"Authorization": "Bearer " + runner_secret},
            timeout=20,
        )
    except requests.ConnectionError:
        raise HTTPException(status_code=503, detail="Waking up your RunSpace... this can take up to a minute on the free tier.")
    except requests.Timeout:
        raise HTTPException(status_code=504, detail="Waking up your RunSpace... this can take up to a minute on the free tier.")


def _job_web_fields(info: dict) -> dict:
    """Translate a runner job view into frontend web fields (public URL etc.).

    In embedded mode the /live gateway is served by THIS main service, so the
    public URL is <site-base>/live/{slug}/; in remote mode it's the runner's."""
    slug = (info or {}).get("web_slug")
    base = public_base_url() if embedded_mode() else runner_cfg()[0]
    if not slug or not base:
        return {}
    out = {
        "web": bool(info.get("web")),
        "web_public": bool(info.get("web_public", True)),
        "web_url": f"{base}/live/{slug}/",
    }
    key = info.get("access_key")
    if not out["web_public"] and key:
        out["web_private_url"] = out["web_url"] + "?key=" + key
    return out
