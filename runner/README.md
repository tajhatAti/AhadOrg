# Code Execution Runner

Separate service for running user-submitted code in a sandboxed subprocess.

## Deploy on Render

1. Create a new Web Service on Render, connect this `runner/` directory
   (or the whole repo with Root Directory set to `runner/`).
2. Runtime: **Docker**.
3. Set environment variables:
   - `RUNNER_SERVICE_SECRET` — a strong random string (generate with
     `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`).
     **This must match** the `RUNNER_SERVICE_SECRET` on the main website.
   - `MAX_EXECUTION_TIME_MS` — default `10000` (10 seconds per run).
   - `MAX_MEMORY_MB` — default `256`.
4. Deploy. The health endpoint is `/health`.

## Supported languages

Python, JavaScript (Node), TypeScript, Bash, Ruby, PHP, Perl, Lua,
C, C++, Java, Go, Rust, SQL (SQLite), Plaintext.

## Security model

- Each execution runs in a fresh temp directory (deleted after).
- Memory limit via `RLIMIT_AS` (Linux).
- Wall-clock timeout kills the entire process group.
- Auth: every request needs `Authorization: Bearer <secret>`.

## Note on full sandbox isolation

This subprocess-based runner works without Docker `--privileged` (important
on Render's free tier). For maximum isolation (network namespace, fork
limits, etc.), consider swapping the execution backend to self-hosted
[Piston](https://github.com/engineer-man/piston) which uses Linux `isolate`.
The API contract (`/internal/execute`) is compatible.

## Public job URLs (web services)

Jobs that bind a listening socket get a public URL automatically:

```
https://<runner-host>/live/{job-slug}/  →  http://127.0.0.1:{job port}/
```

* Every job is handed a `PORT` env var (pool 11000–11099) — code should bind
  `0.0.0.0` + that port (Flask/Express/FastAPI all read `PORT` already).
* A watchdog detects the listener; the dashboard then shows the URL with
  Copy / Open buttons. Slug + port belong to the job record, so crash
  auto-restarts keep the same address.
* HTTP **and** WebSocket traffic is proxied; root-absolute redirects are
  rewritten so the `/live/{slug}` prefix survives.
* Per-URL rate limit: 60 req/min/IP (`LIVE_RATE_LIMIT`). Private mode needs
  the owner's `?key=` / `X-Access-Key:` access key
  (`POST /internal/jobs/{id}/access` toggles it).

Env (optional):

* `PUBLIC_BASE_URL` — used only to print pretty URLs in the job log
  (e.g. `https://ahad-code-runner.onrender.com`).
* `LIVE_PORT_MIN` / `LIVE_PORT_MAX` — port pool (default 11000–11099).
* `LIVE_RATE_LIMIT` — default 60 req/min/IP.
