"""Dashboard data: global stats counters and the cross-product search."""
from typing import Optional, List

from fastapi import APIRouter, Header, HTTPException, Request

from routes.deps import *  # shared kernel (config, helpers, models)


router = APIRouter()


@router.get("/search")
def global_search(q: str, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    if not (q or "").strip():
        return {"results": []}
    term = "%" + q.strip().lower() + "%"
    conn = get_db_connection()
    out = []
    try:
        def run(kind, sql, limit):
            rows = conn.execute(sql, (user["id"], term, term)).fetchall()
            for r in rows[:limit]:
                d = dict(r)
                d["kind"] = kind
                out.append(d)
        run("snippet", "SELECT id, title AS title, language AS sub FROM snippets WHERE user_id = ? AND (LOWER(title) LIKE ? OR LOWER(content) LIKE ?)", 12)
        run("runspace", "SELECT id, name AS title, language AS sub FROM jobs WHERE user_id = ? AND (LOWER(name) LIKE ? OR LOWER(code) LIKE ?)", 8)
        return {"results": out[:20]}
    finally:
        conn.close()


# ================================
# SNIPPETS (code / pastebin)
# ================================


@router.get("/stats")
def get_user_stats(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        jobs_total = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE user_id = ?", (user["id"],)
        ).fetchone()["c"]
        jobs_deployed = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE user_id = ? AND runner_job_id IS NOT NULL", (user["id"],)
        ).fetchone()["c"]
        snippets_total = conn.execute(
            "SELECT COUNT(*) AS c FROM snippets WHERE user_id = ?", (user["id"],)
        ).fetchone()["c"]
        snippets_published = conn.execute(
            "SELECT COUNT(*) AS c FROM snippets WHERE user_id = ? AND is_public = 1", (user["id"],)
        ).fetchone()["c"]
        sessions_count = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE user_id = ?", (user["id"],)
        ).fetchone()["c"]
        return {
            "jobs_total": jobs_total,
            "jobs_deployed": jobs_deployed,
            "snippets": snippets_total,
            "published": snippets_published,
            "active_sessions": sessions_count,
            "member_since": user["created_at"],
        }
    finally:
        conn.close()
