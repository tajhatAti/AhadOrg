from fastapi import APIRouter, Header
from typing import Optional
from database import get_db_connection
from auth_deps import get_current_user_and_session

router = APIRouter(tags=["dashboard"])

@router.get("/stats")
def get_user_stats(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        jobs_total = conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (user["id"],)).fetchone()[0]
        snippets_total = conn.execute("SELECT COUNT(*) FROM snippets WHERE user_id = ?", (user["id"],)).fetchone()[0]
        return {
            "jobs_total": jobs_total,
            "snippets_total": snippets_total,
            "member_since": user["created_at"],
        }
    finally: conn.close()
