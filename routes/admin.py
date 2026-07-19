from fastapi import APIRouter, Header, HTTPException
from typing import Optional

from database import get_db_connection
from models import AdminSuspend, AbuseReportIn
from auth_deps import require_admin
from utils import now_utc_str

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/overview")
def admin_overview(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db_connection()
    try:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        jobs_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        from services.job_runner import list_all_jobs, MAX_BG_JOBS
        running_jobs = sum(1 for j in list_all_jobs() if j["status"] == "running")
        return {
            "users": users,
            "jobs_total": jobs_total,
            "running_jobs": running_jobs,
            "capacity": MAX_BG_JOBS,
            "theoretical_max": users * 3
        }
    finally: conn.close()

@router.get("/users")
def admin_users(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT id, username, email, is_verified, is_suspended, created_at FROM users ORDER BY id DESC").fetchall()
        return {"users": [dict(r) for r in rows]}
    finally: conn.close()

@router.post("/users/set-suspended")
def set_suspended(payload: AdminSuspend, authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET is_suspended=?, updated_at=? WHERE id=?", (1 if payload.suspended else 0, now_utc_str(), payload.user_id))
        if payload.suspended: conn.execute("DELETE FROM sessions WHERE user_id=?", (payload.user_id,))
        conn.commit()
        return {"message": "Updated."}
    finally: conn.close()
