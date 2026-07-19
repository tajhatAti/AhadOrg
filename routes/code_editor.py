import secrets
from fastapi import APIRouter, Header, HTTPException
from typing import Optional

from database import get_db_connection
from models import SnippetCreate, SnippetUpdate, GenericDelete, SnippetShare
from utils import now_utc_str
from auth_deps import get_current_user_and_session
from snippet_page import build_published_page
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["snippets"])

@router.get("/snippets")
def list_snippets(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM snippets WHERE user_id = ? ORDER BY updated_at DESC", (user["id"],)).fetchall()
        return {"snippets": [dict(r) for r in rows]}
    finally: conn.close()

@router.post("/snippets")
def create_snippet(payload: SnippetCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    ct = now_utc_str()
    conn = get_db_connection()
    try:
        cur = conn.execute("INSERT INTO snippets (user_id, title, language, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (user["id"], payload.title, payload.language or "text", payload.content, ct, ct))
        conn.commit()
        return {"id": cur.lastrowid}
    finally: conn.close()

@router.put("/snippets")
def update_snippet(payload: SnippetUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM snippets WHERE id=? AND user_id=?", (payload.id, user["id"])).fetchone()
        if not row: raise HTTPException(404)
        conn.execute("UPDATE snippets SET title=COALESCE(?, title), language=COALESCE(?, language), content=COALESCE(?, content), updated_at=? WHERE id=?",
                     (payload.title, payload.language, payload.content, now_utc_str(), payload.id))
        conn.commit()
        return {"message": "Updated."}
    finally: conn.close()

@router.delete("/snippets")
def delete_snippet(payload: GenericDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM snippets WHERE id=? AND user_id=?", (payload.id, user["id"]))
        conn.commit()
        return {"message": "Deleted."}
    finally: conn.close()

@router.post("/snippets/share")
def toggle_share(payload: SnippetShare, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT share_token FROM snippets WHERE id=? AND user_id=?", (payload.id, user["id"])).fetchone()
        if not row: raise HTTPException(404)
        token = row["share_token"]
        if payload.share and not token:
            token = secrets.token_urlsafe(12)
            conn.execute("UPDATE snippets SET share_token=?, is_public=1 WHERE id=?", (token, payload.id))
        elif not payload.share:
            token = None
            conn.execute("UPDATE snippets SET share_token=NULL, is_public=0 WHERE id=?", (payload.id,))
        conn.commit()
        return {"token": token}
    finally: conn.close()

@router.get("/s/{token}")
def view_shared(token: str):
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM snippets WHERE share_token=? AND is_public=1", (token,)).fetchone()
        if not row: raise HTTPException(404)
        conn.execute("UPDATE snippets SET views = views + 1 WHERE share_token = ?", (token,))
        conn.commit()
    finally: conn.close()
    html, _ = build_published_page(row)
    return HTMLResponse(html)
