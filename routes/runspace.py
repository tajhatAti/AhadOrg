"""RunSpace: one-shot code execution proxy + 24/7 always-on jobs
(start/stop/restart, live logs SSE, public URL access control)."""
from typing import Optional, List

from fastapi import APIRouter, Header, HTTPException, Request

from routes.deps import *  # shared kernel (config, helpers, models)


class JobCreateRequest(BaseModel):
    name: str
    language: str
    code: str


import asyncio

import requests
from fastapi.responses import StreamingResponse

from services import runner_client
from services.runner_client import MAX_JOBS_PER_USER

router = APIRouter()


class ExecuteCodeRequest(BaseModel):
    language: str
    code: str
    stdin: Optional[str] = None


@router.post("/api/execute")
def execute_code(payload: ExecuteCodeRequest, request: Request, authorization: Optional[str] = Header(None)):
    """Proxy code execution to the separate runner service.

    User → this endpoint (auth required) → runner service (shared secret).
    The user NEVER sees the runner URL or secret — those stay server-side.
    """
    # 1) User must be logged in.
    user, _ = get_current_user_and_session(authorization)

    # 2) Rate limit — per ACCOUNT (never per-IP: CGNAT-shared mobile IPs
    #    would let strangers burn each other's allowance).
    rate_limit_user(user["id"], "exec")

    # 3) Get runner config from env.
    runner_url = os.getenv("RUNNER_SERVICE_URL", "").strip().rstrip("/")
    runner_secret = os.getenv("RUNNER_SERVICE_SECRET", "").strip()

    if not runner_url or not runner_secret:
        raise HTTPException(
            status_code=503,
            detail="Code execution is not configured. Set RUNNER_SERVICE_URL and RUNNER_SERVICE_SECRET.",
        )

    # 4) Forward to runner service (server-to-server, secret never sent to browser).
    try:
        response = requests.post(
            runner_url + "/internal/execute",
            json={
                "language": payload.language,
                "code": payload.code,
                "stdin": payload.stdin or "",
            },
            headers={
                "Authorization": "Bearer " + runner_secret,
                "Content-Type": "application/json",
            },
            # execution time (MAX_EXECUTION_TIME_MS) + auto pip-install budget
            timeout=130,
        )
    except requests.ConnectionError:
        logger.error("Runner service unreachable at %s", runner_url)
        raise HTTPException(
            status_code=503,
            detail="Code execution service is temporarily unavailable. Please try again later.",
        )
    except requests.Timeout:
        raise HTTPException(
            status_code=504,
            detail="Code execution took too long. Please simplify your code.",
        )

    if response.status_code == 401:
        raise HTTPException(status_code=500, detail="Runner authentication failed. Contact admin.")
    if response.status_code == 403:
        raise HTTPException(status_code=500, detail="Runner secret mismatch. Contact admin.")
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="Code execution service returned an error ({}).".format(response.status_code),
        )

    result = response.json()
    # Pass through stdout/stderr/exit_code/execution_time to the user.
    # The runner URL and secret are NEVER in this response.
    return result


# ================================
# ALWAYS-ON JOBS (24/7 background tasks — mini PythonAnywhere)
# ================================
# Job DEFINITIONS live in our DB (survive runner restarts); the PROCESSES run
# inside the runner service. Same secret, same proxy pattern as /api/execute.

def _get_own_job(job_id: int, user: dict) -> dict:
    """Fetch a job row owned by this user or 404."""
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found.")
    return dict(row)


@router.post("/api/jobs")
def create_job(payload: JobCreateRequest, request: Request, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    rate_limit_user(user["id"], "exec")

    name = payload.name.strip()[:60]
    if not name:
        raise HTTPException(status_code=422, detail="Give the job a name.")

    conn = get_db_connection()
    try:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE user_id = ?", (user["id"],)).fetchone()
        if (dict(cnt)["c"] if cnt else 0) >= MAX_JOBS_PER_USER:
            raise HTTPException(status_code=429, detail=f"Max {MAX_JOBS_PER_USER} jobs per account (free tier).")
    except HTTPException:
        conn.close()
        raise
    conn.close()

    resp = runner_client._runner_http("POST", "/internal/jobs", {
        "language": payload.language, "code": payload.code,
        "name": f"u{user['id']}-{name}",
    })
    if resp.status_code == 201:
        info = resp.json()
    elif resp.status_code in (401, 403):
        raise HTTPException(status_code=500, detail="Runner secret mismatch. Contact admin.")
    else:
        try:
            detail = resp.json().get("detail", "Runner rejected the job.")
        except Exception:
            detail = "Runner rejected the job."
        raise HTTPException(status_code=resp.status_code if 400 <= resp.status_code < 500 else 502, detail=detail)

    now = now_utc_str()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO jobs (user_id, name, language, code, runner_job_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], name, payload.language, payload.code, info["id"], now, now),
        )
        conn.commit()
        info["job_db_id"] = cursor.lastrowid
        info.update(runner_client._job_web_fields(info))  # web / web_url (web often False seconds after birth)
        return info
    finally:
        conn.close()


@router.get("/api/jobs")
def list_jobs(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM jobs WHERE user_id = ? ORDER BY id DESC", (user["id"],)).fetchall()
    finally:
        conn.close()

    # Live status from the runner — best effort (it may be asleep/restarted).
    live, runner_state = {}, "ok"
    try:
        resp = runner_client._runner_http("GET", "/internal/jobs")
        if resp.status_code == 200:
            live = {j["id"]: j for j in resp.json().get("jobs", [])}
        else:
            runner_state = "unreachable"
    except HTTPException as e:
        runner_state = e.detail

    jobs = []
    for r in rows:
        r = dict(r)
        rid = r.get("runner_job_id")
        if rid and rid in live:
            info = live[rid]
            r.update({"status": info["status"], "uptime_s": info.get("uptime_s", 0), "restarts": info.get("restarts", 0)})
            r.update(runner_client._job_web_fields(info))                # web / web_url / access
        else:
            r.update({"status": "offline", "uptime_s": 0, "restarts": 0})
        r.pop("code", None)  # never ship stored code back in list payloads
        jobs.append(r)
    return {"jobs": jobs, "runner": runner_state, "max_per_user": MAX_JOBS_PER_USER}


@router.get("/api/jobs/{job_id}/logs")
def job_logs(job_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if not rid:
        return {"status": "offline", "logs": "(never started)"}
    resp = runner_client._runner_http("GET", f"/internal/jobs/{rid}")
    if resp.status_code == 404:
        return {"status": "offline", "logs": "(runner restarted — press ▶ Restart to relaunch)"}
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not fetch logs from runner.")
    info = resp.json()
    return {"status": info.get("status"), "logs": info.get("logs", ""), "uptime_s": info.get("uptime_s", 0), "restarts": info.get("restarts", 0)}


@router.get("/api/jobs/{job_id}/logs/stream")
async def job_logs_stream(job_id: int, token: Optional[str] = None):
    """Server-Sent Events: push a job's logs to the dashboard in real time.

    EventSource can't send Authorization headers, so the session token comes
    as a ?token= query param; we validate it against the sessions table the
    same way get_current_user_and_session does.
    """
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    conn = get_db_connection()
    try:
        session_row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        if not session_row:
            raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (session_row["user_id"],)).fetchone()
        if not user_row:
            raise HTTPException(status_code=401, detail="Account not found.")
    finally:
        conn.close()

    row = _get_own_job(job_id, user_row)
    rid = row.get("runner_job_id")

    async def gen():
        last = None
        while True:
            info = None
            if rid:
                try:
                    resp = await asyncio.to_thread(_runner_http, "GET", f"/internal/jobs/{rid}")
                    if resp.status_code == 200:
                        info = resp.json()
                except Exception:
                    info = None
            payload = {
                "status": (info or {}).get("status", "offline"),
                "logs": (info or {}).get("logs", "(runner unreachable — retrying…)"),
                "uptime_s": (info or {}).get("uptime_s", 0),
                "restarts": (info or {}).get("restarts", 0),
            }
            blob = json.dumps(payload, ensure_ascii=False)
            if blob != last:
                last = blob
                yield f"data: {blob}\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if rid:
        resp = runner_client._runner_http("POST", f"/internal/jobs/{rid}/stop")
        if resp.status_code not in (200, 404):
            raise HTTPException(status_code=502, detail="Runner refused to stop the job.")
    return {"status": "stopped"}


@router.post("/api/jobs/{job_id}/restart")
def restart_job(job_id: int, request: Request, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    rate_limit_user(user["id"], "exec")
    row = _get_own_job(job_id, user)

    rid = row.get("runner_job_id")
    if rid:
        try:
            runner_client._runner_http("POST", f"/internal/jobs/{rid}/stop")
        except HTTPException:
            pass  # old copy may already be gone (runner restart) — fine

    resp = runner_client._runner_http("POST", "/internal/jobs", {
        "language": row["language"], "code": row["code"],
        "name": f"u{user['id']}-{row['name']}",
    })
    if resp.status_code == 201:
        info = resp.json()
    else:
        try:
            detail = resp.json().get("detail", "Runner rejected the job.")
        except Exception:
            detail = "Runner rejected the job."
        raise HTTPException(status_code=502, detail=detail)

    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET runner_job_id = ?, updated_at = ? WHERE id = ?", (info["id"], now_utc_str(), job_id))
        conn.commit()
    finally:
        conn.close()
    info["job_db_id"] = job_id
    info.update(runner_client._job_web_fields(info))
    return info


class JobAccessToggle(BaseModel):
    public: bool = True


@router.post("/api/jobs/{job_id}/access")
def toggle_job_access(job_id: int, payload: JobAccessToggle, authorization: Optional[str] = Header(None)):
    """Public ⇄ Private toggle for a job's live web URL."""
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if not rid:
        raise HTTPException(status_code=409, detail="Job is not up on the runner — press Restart first.")
    resp = runner_client._runner_http("POST", f"/internal/jobs/{rid}/access", {"public": payload.public})
    if resp.status_code == 404:
        raise HTTPException(status_code=409, detail="Runner restarted — press Restart to relaunch, then retry.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Runner refused the access change.")
    info = resp.json()
    info.update(runner_client._job_web_fields(info))
    info["job_db_id"] = job_id
    return info


@router.delete("/api/jobs/{job_id}")
def delete_job(job_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if rid:
        try:
            runner_client._runner_http("POST", f"/internal/jobs/{rid}/stop")
        except HTTPException:
            pass  # best effort
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()
    return {"message": "Job deleted."}


# ================================
# USER PREFERENCES
# ================================
