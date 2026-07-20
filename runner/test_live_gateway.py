"""End-to-end test for the public web gateway (/live/{slug}/).

Boots the real runner via uvicorn on a local port, starts a job that listens
on $PORT, then drives HTTP + WebSocket + private-key + not-running scenarios
through the public route. Run:  python3 test_live_gateway.py
"""
import os, sys, time, json, signal, subprocess, asyncio

PORT = 8100 + (os.getpid() % 180)   # unique per run: stale sandboxes can keep old uvicorns alive
BASE = f"http://127.0.0.1:{PORT}"
SECRET = "gateway-test-secret"

passed = failed = 0
def R(ok, msg):
    global passed, failed
    if ok: passed += 1; print("  PASS:", msg)
    else: failed += 1; print("  FAIL:", msg)

WEB_JOB = r"""
import http.server, socketserver, os
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/bounce"):
            self.send_response(302); self.send_header("Location", "/landed"); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
        self.wfile.write(b"hello " + self.path.encode())
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
        self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
        self.wfile.write(b"echo:" + body)
    def log_message(self, *a): pass
socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer(("0.0.0.0", int(os.environ["PORT"])), H).serve_forever()
"""

WS_JOB = r"""
import asyncio, os, websockets
async def echo(ws):
    async for m in ws:
        await ws.send("pong:" + m)
async def main():
    async with websockets.serve(echo, "0.0.0.0", int(os.environ["PORT"])):
        await asyncio.Future()
asyncio.run(main())
"""

def _hdr(extra=None):
    h = {"Authorization": "Bearer " + SECRET, "Content-Type": "application/json"}
    if extra: h.update(extra)
    return h

def wait_hello(slug, suffix="", timeout=40):
    """Poll until the /live/ route reliably serves the job's own content."""
    import urllib.request, urllib.error
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(BASE + f"/live/{slug}/{suffix}", timeout=5)
            body = r.read().decode("utf-8", "replace")
            if body.startswith("hello") and "Card" not in body[:20]:
                return True
        except Exception:
            pass
        time.sleep(0.7)
    return False

def http(method, path, body=None, headers=None):
    import urllib.request, urllib.error
    req = urllib.request.Request(BASE + path, method=method,
                                 data=(json.dumps(body).encode() if isinstance(body, (dict, list)) else body),
                                 headers=headers or _hdr())
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

def main():
    os.environ["RUNNER_SERVICE_SECRET"] = SECRET
    os.environ["PUBLIC_BASE_URL"] = BASE
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=dict(os.environ), start_new_session=True,
    )
    try:
        # wait for boot
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(BASE + "/health", timeout=2); break
            except Exception: time.sleep(0.4)
        else:
            raise SystemExit("runner failed to boot")

        print("=== HTTP web job ===")
        st, body, _ = http("POST", "/internal/jobs", {"language": "python", "code": WEB_JOB, "name": "Web Demo!"})
        R(st == 201, f"job created (got {st})")
        job = json.loads(body)
        R(bool(job.get("web_slug")), f"slug assigned: {job.get('web_slug')}")
        R(job.get("web") is False, "web=False at birth")
        R(job.get("web_public") is True, "public by default")
        slug = job["web_slug"]; jid = job["id"]

        R(wait_hello(slug), "watchdog detected the listener → live URL works")
        st, body, _ = http("GET", f"/live/{slug}/abc?x=1", headers={})
        R(st == 200 and body == "hello /abc?x=1", f"HTTP GET proxied, prefix stripped ({st}, {body!r})")
        st, body, _ = http("POST", f"/live/{slug}/submit", body=b"rawdata", headers={"Content-Type": "text/plain"})
        R(st == 200 and body == "echo:rawdata", f"HTTP POST body proxied ({body!r})")

        # root-absolute redirect must keep the /live/{slug} prefix
        import urllib.request
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        opener = urllib.request.build_opener(NoRedirect)
        r = None
        try:
            opener.open(BASE + f"/live/{slug}/bounce", timeout=10)
        except Exception as e:
            r = e
        loc = getattr(r, "headers", {}).get("Location", "") if r else ""
        R(loc == f"/live/{slug}/landed", f"redirect Location rewritten ({loc!r})")

        print("=== slug survives crash auto-restart ===")
        CRASH_ONCE = r"""
import os, http.server, socketserver
marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "already")
if not os.path.exists(marker):
    open(marker, "w").write("x")
    raise SystemExit("simulated crash")
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
        self.wfile.write(b"hello from restarted")
    def log_message(self, *a): pass
socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer(("0.0.0.0", int(os.environ["PORT"])), H).serve_forever()
"""
        st, body, _ = http("POST", "/internal/jobs", {"language": "python", "code": CRASH_ONCE, "name": "Phoenix"})
        cjob = json.loads(body); cslug, cjid = cjob["web_slug"], cjob["id"]
        ok = False
        deadline = time.time() + 30  # crash → 5s restart delay → listener up
        while time.time() < deadline:
            st_, body_, _ = http("GET", f"/live/{cslug}/", headers={})
            if st_ == 200 and "hello from restarted" in body_:
                ok = True; break
            time.sleep(1.0)
        R(ok, "same /live/ slug serves after crash auto-restart")
        st, body, _ = http("GET", f"/internal/jobs/{cjid}")
        R(json.loads(body).get("restarts", 0) >= 1, "restart counter proves it really crashed & restarted")
        http("POST", f"/internal/jobs/{cjid}/stop")

        print("=== stopped job page ===")
        st, body, _ = http("POST", f"/internal/jobs/{jid}/stop")
        R(st == 200, f"job stopped ({st})")
        st, body, _ = http("GET", f"/live/{slug}/", headers={})
        R(st in (200,) and "not running" in body, "stopped job shows friendly 'not running' page")

        print("=== unknown slug ===")
        st, body, _ = http("GET", "/live/nope-123/", headers={})
        R("No job lives at this address" in body, "unknown slug → free-again page")

        print("=== private key gate ===")
        st, body, _ = http("POST", "/internal/jobs", {"language": "python", "code": WEB_JOB, "name": "Secret Web"})
        job2 = json.loads(body); slug2, jid2 = job2["web_slug"], job2["id"]
        st, body, _ = http("POST", f"/internal/jobs/{jid2}/access", {"public": False})
        info = json.loads(body)
        R(info.get("web_public") is False, "access toggled to private")
        key = info.get("access_key")
        R(bool(key), "access_key returned while private")
        R(wait_hello(slug2, suffix=f"?key={key}"), "private job reachable with key from the start")
        st, body, _ = http("GET", f"/live/{slug2}/", headers={})
        R(st == 401 and "private" in body.lower(), f"private without key → 401 ({st})")
        st, body, _ = http("GET", f"/live/{slug2}/?key={key}", headers={})
        R(st == 200 and body.startswith("hello"), f"private with ?key= → 200 ({st})")
        st, body, _ = http("GET", f"/live/{slug2}/", headers={"X-Access-Key": key})
        R(st == 200, "private with X-Access-Key header → 200")

        print("=== rate limit (60/min/IP) ===")
        st, body, _ = http("POST", "/internal/jobs", {"language": "python", "code": WEB_JOB, "name": "RateTest"})
        job4 = json.loads(body); slug4, jid4 = job4["web_slug"], job4["id"]
        R(wait_hello(slug4), "rate-test job live")
        hits = 0; st_ = 200
        for i in range(70):
            st_, _, _ = http("GET", f"/live/{slug4}/", headers={})
            if st_ == 429:
                break
            hits += 1
        R(st_ == 429 and hits >= 58 and hits <= 62, f"rate limited after {hits} requests (429)")
        http("POST", f"/internal/jobs/{jid4}/stop")

        print("=== WebSocket bridge ===")
        st, body, _ = http("POST", "/internal/jobs", {"language": "python", "code": WS_JOB, "name": "WS Echo"})
        job3 = json.loads(body); slug3, jid3 = job3["web_slug"], job3["id"]
        time.sleep(6)
        async def ws_test():
            import websockets
            uri = f"ws://127.0.0.1:{PORT}/live/{slug3}/ws"
            async with websockets.connect(uri) as ws:
                await ws.send("ping")
                return await asyncio.wait_for(ws.recv(), timeout=10)
        try:
            got = asyncio.run(ws_test())
            R(got == "pong:ping", f"WS round-trip through bridge ({got!r})")
        except Exception as e:
            R(False, f"WS bridge failed: {e}")

        # cleanup
        for j in (jid2, jid3):
            http("POST", f"/internal/jobs/{j}/stop")

    finally:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception: proc.kill()
        time.sleep(1)

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
