"""Code Studio: snippet CRUD, publish/unpublish, the standalone
/s/{token} page, and the legacy /code/s/{token} redirect."""
from typing import Optional, List

from fastapi import APIRouter, Header, HTTPException, Request

from routes.deps import *  # shared kernel (config, helpers, models)


from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/snippets")
def list_snippets(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, title, language, content, share_token, is_public, views, created_at, updated_at "
            "FROM snippets WHERE user_id = ? ORDER BY updated_at DESC", (user["id"],),
        ).fetchall()
        return {"snippets": [dict(r) for r in rows]}
    finally:
        conn.close()


class GenericDelete(BaseModel):
    id: int


class SnippetShare(BaseModel):
    id: int
    share: bool = True


class SnippetCreate(BaseModel):
    title: str
    language: Optional[str] = "text"
    content: str


class SnippetUpdate(BaseModel):
    id: int
    title: Optional[str] = None
    language: Optional[str] = None
    content: Optional[str] = None


@router.post("/snippets")
def create_snippet(payload: SnippetCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    title = payload.title.strip() or "Untitled snippet"
    content = payload.content
    if not content.strip():
        raise HTTPException(status_code=400, detail="Snippet content cannot be empty.")
    ct = now_utc_str()
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "INSERT INTO snippets (user_id, title, language, content, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], title, (payload.language or "text")[:32], content, ct, ct),
        )
        conn.commit()
        return {"message": "Snippet saved.", "id": cur.lastrowid}
    finally:
        conn.close()


@router.put("/snippets")
def update_snippet(payload: SnippetUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM snippets WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Snippet not found.")
        title = payload.title if payload.title is not None else row["title"]
        language = payload.language if payload.language is not None else row["language"]
        content = payload.content if payload.content is not None else row["content"]
        conn.execute("UPDATE snippets SET title=?, language=?, content=?, updated_at=? WHERE id=?",
                     (title, language, content, now_utc_str(), payload.id))
        conn.commit()
        return {"message": "Snippet updated."}
    finally:
        conn.close()


@router.delete("/snippets")
def delete_snippet(payload: GenericDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM snippets WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Snippet not found.")
        conn.execute("DELETE FROM snippets WHERE id = ?", (payload.id,))
        conn.commit()
        return {"message": "Snippet deleted."}
    finally:
        conn.close()


@router.post("/snippets/share")
def toggle_snippet_share(payload: SnippetShare, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id, share_token FROM snippets WHERE id = ? AND user_id = ?",
                           (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Snippet not found.")
        token = row["share_token"]
        if payload.share:
            if not token:
                token = secrets.token_urlsafe(12)
                conn.execute("UPDATE snippets SET share_token=?, is_public=1 WHERE id=?", (token, payload.id))
                conn.commit()
        else:
            conn.execute("UPDATE snippets SET share_token=NULL, is_public=0 WHERE id=?", (payload.id,))
            conn.commit()
            token = None
        return {"share": payload.share, "token": token,
                "url": f"/s/{token}" if token else None}
    finally:
        conn.close()


@router.get("/s/{token}")
def view_shared_snippet(token: str):
    """Public PUBLISHED page — NO auth, NO editor UI.

    This is the finished, standalone output (GitHub-Pages style), not a tool:
      * HTML snippets -> served verbatim as the user's own HTML document
        (a true standalone static page, exactly like deploying index.html).
      * Other languages -> a single clean viewer that just renders/runs the
        content. No Copy/Download/source/console/tabs — only the output.
    """
    from snippet_page import build_published_page
    from fastapi.responses import HTMLResponse

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT title, language, content, created_at, views FROM snippets "
            "WHERE share_token = ? AND is_public = 1", (token,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="This page is private or no longer published.")
        conn.execute("UPDATE snippets SET views = views + 1 WHERE share_token = ?", (token,))
        conn.commit()
    finally:
        conn.close()

    html, is_raw = build_published_page(row)
    # For HTML we serve the user's document as-is (true standalone page).
    return HTMLResponse(content=html)





# ================================
# CODE EXECUTION PROXY (main website → runner service)
# ================================
