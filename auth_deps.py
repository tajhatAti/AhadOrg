import os
import logging
from typing import Optional, List
from fastapi import Header, HTTPException, Request
from database import get_db_connection
from utils import now_utc_str

logger = logging.getLogger("auth-deps")

def get_current_user_and_session(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated. Please sign in.")

    token = authorization.split(" ", 1)[1].strip()
    conn = get_db_connection()
    try:
        session_row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        if not session_row:
            raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")

        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (session_row["user_id"],)).fetchone()
        if not user_row:
            raise HTTPException(status_code=401, detail="Account not found.")
        if user_row.get("is_suspended"):
            raise HTTPException(status_code=401, detail="This account is suspended.")

        conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now_utc_str(), session_row["id"]))
        conn.commit()

        return user_row, session_row
    finally:
        conn.close()

def require_role(allowed_roles: List[str]):
    def role_checker(authorization: Optional[str] = Header(None)):
        user_row, session_row = get_current_user_and_session(authorization)
        user_role = user_row.get("role", "user")
        if user_role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Forbidden.")
        return user_row, session_row
    return role_checker

def require_admin(authorization: Optional[str] = Header(None)):
    user_row, session_row = get_current_user_and_session(authorization)
    if not user_row.get("is_admin"):
        raise HTTPException(status_code=404, detail="Not found.")
    return user_row, session_row
