import json
import asyncio
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from typing import Optional

from database import get_db_connection
from models import JobCreateRequest, GenericDelete, JobAccessToggle
from utils import now_utc_str, client_ip, rate_limit
from auth_deps import get_current_user_and_session
from services.job_runner import start_job, stop_job, get_job_info, list_all_jobs

router = APIRouter(tags=["runspace"])

MAX_JOBS_PER_USER = 3

@router.post("/api/jobs")
def create_job_route(payload: JobCreateRequest, request: Request, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    rate_limit(f"{client_ip(request)}:jobs")
    conn = get_db_connection()
    try:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE user_id=?", (user["id"],)).fetchone()["c"]
        if cnt >= MAX_JOBS_PER_USER: raise HTTPException(429, detail="Limit reached.")
        
        env_vars_json = json.dumps(payload.env_vars) if payload.env_vars else None
        info = start_job(payload.language, payload.code, f"u{user['id']}-{payload.name}", env_vars=env_vars_json)
        conn.execute("INSERT INTO jobs (user_id, name, language, code, runner_job_id, created_at, updated_at, env_vars) VALUES (?,?,?,?,?,?,?,?)",
                     (user["id"], payload.name, payload.language, payload.code, info["id"], now_utc_str(), now_utc_str(), env_vars_json))
        conn.commit()
        return {"id": info["id"]}
    finally: conn.close()

@router.get("/api/jobs/{job_id}")
def get_job_detail_route(job_id: str, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE (runner_job_id=? OR id=?) AND user_id=?", (job_id, job_id, user["id"])).fetchone()
        if not row: raise HTTPException(404)
        return dict(row)
    finally: conn.close()

class JobUpdateRequest(BaseModel):
    code: str

@router.put("/api/jobs/{job_id}")
def update_job_route(job_id: str, payload: JobUpdateRequest, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM jobs WHERE runner_job_id=? AND user_id=?", (job_id, user["id"])).fetchone()
        if not row: raise HTTPException(404)
        conn.execute("UPDATE jobs SET code=?, updated_at=? WHERE runner_job_id=?", (payload.code, now_utc_str(), job_id))
        conn.commit()
        return {"message": "Updated."}
    finally: conn.close()
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM jobs WHERE user_id = ?", (user["id"],)).fetchall()
        jobs = []
        for r in rows:
            info = get_job_info(r["runner_job_id"])
            d = dict(r)
            d["status"] = info["status"] if info else "offline"
            d.pop("code", None)
            jobs.append(d)
        return {"jobs": jobs}
    finally: conn.close()

@router.post("/api/jobs/{job_id}/stop")
def stop_job_route(job_id: str, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT runner_job_id FROM jobs WHERE runner_job_id=? AND user_id=?", (job_id, user["id"])).fetchone()
        if not row: raise HTTPException(404)
        stop_job(job_id)
        return {"message": "Stopped."}
    finally: conn.close()

@router.get("/api/jobs/{job_id}/logs/stream")
async def job_logs_stream(job_id: str, token: str):
    conn = get_db_connection()
    try:
        session = conn.execute("SELECT user_id FROM sessions WHERE token=?", (token,)).fetchone()
        if not session: raise HTTPException(401)
        job = conn.execute("SELECT runner_job_id FROM jobs WHERE runner_job_id=? AND user_id=?", (job_id, session["user_id"])).fetchone()
        if not job: raise HTTPException(404)
    finally: conn.close()

    async def gen():
        while True:
            info = get_job_info(job_id)
            if not info: break
            yield f"data: {json.dumps({'status': info['status'], 'logs': chr(10).join(info['log'])})}\n\n"
            await asyncio.sleep(1.5)
    return StreamingResponse(gen(), media_type="text/event-stream")
