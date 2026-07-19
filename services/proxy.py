import time
import socket
import logging
from collections import deque
from typing import Optional
from fastapi import Request, Response, HTTPException
from fastapi.responses import HTMLResponse

logger = logging.getLogger("proxy")

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}

_live_hits: dict = {}
_live_hits_lock = None # Will be initialized if needed, or use a simple dict if thread-safety isn't critical for this.
import threading
_live_hits_lock = threading.Lock()

def _web_watch(j: dict, proc, port: int) -> None:
    polls = 0
    miss_streak = 0
    while True:
        if j.get("stop_requested"): return
        if j.get("proc") is not proc or proc.poll() is not None: return
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.7):
                up = True
        except OSError:
            up = False
        if up:
            miss_streak = 0
            if not j.get("web"):
                j["web"] = True
                logger.info("Job %s web up on :%s", j.get("id"), port)
        else:
            miss_streak += 1
            if j.get("web") and miss_streak >= 3:
                j["web"] = False
        polls += 1
        time.sleep(0.75 if polls < 45 else 2.5)

def _live_page(title: str, body: str, accent: str = "#0f0e0c") -> HTMLResponse:
    import os
    site = os.getenv("SITE_BASE_URL", "").strip().rstrip("/")
    report = f'<p class="note"><a style="color:inherit" href="{site}/report-abuse">Report abuse</a></p>' if site else ""
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#faf9f6;font-family:Georgia,serif;color:#141310}}
.card{{max-width:430px;margin:20px;padding:34px 30px;text-align:center;background:#fff;border:1px solid #e4e0d5;border-top:3px solid {accent};box-shadow:0 18px 40px -26px rgba(20,19,16,.28)}}
h1{{font-size:21px;margin:0 0 10px}}p{{font-size:14px;line-height:1.65;color:#5c584e}}
.note{{margin-top:16px;padding-top:12px;border-top:1px dashed #e4e0d5;font-size:12px}}
</style></head><body><div class="card">{body}{report}</div></body></html>"""
    return HTMLResponse(html)

def _live_rate_ok(slug: str, ip: str) -> bool:
    now = time.time()
    key = (slug, ip)
    with _live_hits_lock:
        q = _live_hits.get(key)
        if q is None: q = _live_hits[key] = deque()
        while q and now - q[0] > 60: q.popleft()
        if len(q) >= 60: return False
        q.append(now)
        return True

async def proxy_request(j: dict, request: Request, full_path: str):
    import httpx
    if not _live_rate_ok(j["web_slug"], request.client.host if request.client else "?"):
        return HTMLResponse(_live_page("Slow down", "<h1>Rate limit reached</h1>").body, status_code=429)

    query = request.url.query
    target = f"http://127.0.0.1:{j['port']}/{full_path}" + (f"?{query}" if query else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    headers["x-forwarded-for"] = request.client.host if request.client else ""
    headers["x-forwarded-prefix"] = f"/live/{j['web_slug']}"
    
    try:
        body = await request.body()
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            resp = await client.request(request.method, target, content=body, headers=headers)
    except Exception:
        return HTMLResponse(_live_page("Job busy", "<h1>The job took too long to answer</h1>").body, status_code=504)

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
    loc = resp.headers.get("location")
    if loc:
        if loc.startswith("/") and not loc.startswith("//"):
            out_headers["location"] = f"/live/{j['web_slug']}{loc}"
    
    return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)
