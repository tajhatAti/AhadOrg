"""Client for the RunSpace runner service (job execution + management).
Runner URL/secret stay server-side; the browser never sees them. Test drivers
monkeypatch runner_client._runner_http — call it module-attr style."""
import os
import logging
import requests
from fastapi import HTTPException

logger = logging.getLogger("ahad-co-app")

def runner_cfg():
    url = os.getenv("RUNNER_SERVICE_URL", "").strip().rstrip("/")
    secret = os.getenv("RUNNER_SERVICE_SECRET", "").strip()
    return url, secret


MAX_JOBS_PER_USER = 3  # free tier guardrail


def _runner_http(method: str, path: str, json_body=None):
    """Call the runner service with the shared secret; map every transport
    failure to a clean HTTPException the frontend can display."""
    runner_url = os.getenv("RUNNER_SERVICE_URL", "").strip().rstrip("/")
    runner_secret = os.getenv("RUNNER_SERVICE_SECRET", "").strip()
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
        raise HTTPException(status_code=503, detail="Job service is waking up or unreachable — try again in 30 seconds.")
    except requests.Timeout:
        raise HTTPException(status_code=504, detail="Job service took too long to respond.")


def _job_web_fields(info: dict) -> dict:
    """Translate a runner job view into frontend web fields (public URL etc.).

    The proxy lives on the RUNNER service, so the public URL is simply the
    runner's own base URL + /live/{slug}/."""
    slug = (info or {}).get("web_slug")
    runner_url = os.getenv("RUNNER_SERVICE_URL", "").strip().rstrip("/")
    if not slug or not runner_url:
        return {}
    out = {
        "web": bool(info.get("web")),
        "web_public": bool(info.get("web_public", True)),
        "web_url": f"{runner_url}/live/{slug}/",
    }
    key = info.get("access_key")
    if not out["web_public"] and key:
        out["web_private_url"] = out["web_url"] + "?key=" + key
    return out
