import json
from fastapi import APIRouter, Header, HTTPException, Request
from typing import Optional

from database import get_db_connection
from models import ProfileUpdate, SessionRevoke, AccountDelete, ChangePassword, UserPreferencesUpdate
from utils import now_utc_str, verify_password, hash_password, validate_password, client_ip, rate_limit
from auth_deps import get_current_user_and_session
from services.twofa import _verify_second_factor

router = APIRouter(tags=["profile"])

@router.get("/profile")
def get_profile(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    links = json.loads(user["links"]) if user["links"] else []
    return {
        "id": user["id"], "username": user["username"], "email": user["email"],
        "phone": user["phone"], "custom_code": user["custom_code"], "links": links,
        "created_at": user["created_at"], "is_admin": bool(user.get("is_admin")),
    }

@router.post("/profile/update")
def update_profile(payload: ProfileUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        phone = payload.phone if payload.phone is not None else user["phone"]
        custom_code = payload.custom_code if payload.custom_code is not None else user["custom_code"]
        links_json = json.dumps([l.dict() for l in payload.links]) if payload.links is not None else user["links"]
        conn.execute("UPDATE users SET phone=?, custom_code=?, links=?, updated_at=? WHERE id=?",
                     (phone, custom_code, links_json, now_utc_str(), user["id"]))
        conn.commit()
        return {"message": "Updated."}
    finally: conn.close()

@router.get("/sessions")
def list_sessions(authorization: Optional[str] = Header(None)):
    user, current = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM sessions WHERE user_id = ? ORDER BY last_seen DESC", (user["id"],)).fetchall()
        return {"sessions": [{"id": r["id"], "device_info": r["device_info"], "ip_address": r["ip_address"], "created_at": r["created_at"], "last_seen": r["last_seen"], "is_current": r["id"] == current["id"]} for r in rows]}
    finally: conn.close()

@router.post("/sessions/revoke")
def revoke_session(payload: SessionRevoke, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (payload.session_id, user["id"]))
        conn.commit()
        return {"message": "Revoked."}
    finally: conn.close()

@router.post("/account/change-password")
def change_password(payload: ChangePassword, authorization: Optional[str] = Header(None), request: Request = None):
    user, current = get_current_user_and_session(authorization)
    rate_limit(f"{client_ip(request)}:changepw")
    if not verify_password(payload.current_password, user["password"]): raise HTTPException(400, detail="Incorrect.")
    new_pw = validate_password(payload.new_password)
    conn = get_db_connection()
    try:
        if payload.totp_code: _verify_second_factor(conn, user["id"], payload.totp_code)
        conn.execute("UPDATE users SET password=?, updated_at=? WHERE id=?", (hash_password(new_pw), now_utc_str(), user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND id != ?", (user["id"], current["id"]))
        conn.commit()
        return {"message": "Updated."}
    finally: conn.close()

@router.get("/preferences")
def get_preferences(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_preferences WHERE user_id = ?", (user["id"],)).fetchone()
        return dict(row) if row else {"theme": "dark"}
    finally: conn.close()

@router.put("/preferences")
def update_preferences(payload: UserPreferencesUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        # Simple upsert
        row = conn.execute("SELECT id FROM user_preferences WHERE user_id = ?", (user["id"],)).fetchone()
        if row: conn.execute("UPDATE user_preferences SET theme=?, updated_at=? WHERE user_id=?", (payload.theme or "dark", now_utc_str(), user["id"]))
        else: conn.execute("INSERT INTO user_preferences (user_id, theme, created_at, updated_at) VALUES (?, ?, ?, ?)", (user["id"], payload.theme or "dark", now_utc_str(), now_utc_str()))
        conn.commit()
        return {"message": "Updated."}
    finally: conn.close()
