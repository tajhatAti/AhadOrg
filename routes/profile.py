"""Profile: profile read/update, sessions + revoke, password change,
account deletion, login history, preferences, activity log."""
from typing import Optional, List

from fastapi import APIRouter, Header, HTTPException, Request

from routes.deps import *  # shared kernel (config, helpers, models)


class UserPreferencesUpdate(BaseModel):
    theme: Optional[str] = None
    language: Optional[str] = None
    timezone: Optional[str] = None
    notifications_enabled: Optional[bool] = None
    email_notifications: Optional[bool] = None


from services import runner_client
from services.twofa import _verify_second_factor

router = APIRouter()


@router.get("/profile")
def get_profile(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    links = json.loads(user["links"]) if user["links"] else []
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "phone": user["phone"],
        "custom_code": user["custom_code"],
        "links": links,
        "created_at": user["created_at"],
        "password_changed_at": user["password_changed_at"] if "password_changed_at" in user.keys() else None,
        "is_admin": bool(user["is_admin"]) if "is_admin" in user.keys() else False,
    }


@router.post("/profile/update")
def update_profile(payload: ProfileUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        phone = payload.phone if payload.phone is not None else user["phone"]
        custom_code = payload.custom_code if payload.custom_code is not None else user["custom_code"]
        links_json = json.dumps([l.dict() for l in payload.links]) if payload.links is not None else user["links"]

        conn.execute("""
            UPDATE users SET phone=?, custom_code=?, links=?, updated_at=?
            WHERE id=?
        """, (phone, custom_code, links_json, now_utc_str(), user["id"]))
        conn.commit()
        return {"message": "Profile updated successfully."}
    finally:
        conn.close()


@router.post("/account/delete")
def delete_account(payload: AccountDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    if not verify_password(payload.password, user["password"]):
        raise HTTPException(status_code=400, detail="Incorrect password.")

    conn = get_db_connection()
    try:
        # Collect deployed job ids BEFORE deleting rows, so the processes can
        # be stopped afterwards (a deleted account must never leave code
        # running on the runner — abuse hole otherwise).
        rids = [dict(r)["runner_job_id"] for r in conn.execute(
            "SELECT runner_job_id FROM jobs WHERE user_id = ? AND runner_job_id IS NOT NULL",
            (user["id"],)).fetchall() if dict(r).get("runner_job_id")]
        # Delete owned data (kept domains); running processes are stopped below.
        for table in ("jobs", "snippets", "user_2fa", "user_preferences",
                      "login_history", "sessions"):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user["id"],))
        conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        conn.commit()
    finally:
        conn.close()
    for rid in rids:
        try:
            runner_client._runner_http("POST", f"/internal/jobs/{rid}/stop")
        except Exception:
            pass
    return {"message": "Account deleted permanently."}


@router.post("/account/change-password")
def change_password(payload: ChangePassword, authorization: Optional[str] = Header(None), request: Request = None):
    """In-settings password change (user is already signed in):
       current password + new password, PLUS an authenticator code when
       2FA is enabled. On success EVERY other session/device is signed out —
       only the session making the change survives."""
    user, current = get_current_user_and_session(authorization)
    rate_limit(f"{client_ip(request) if request else 'na'}:changepw:{user['id']}")

    if not verify_password(payload.current_password, user["password"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    new_password = validate_password(payload.new_password)
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=400, detail="New password must be different from the current one.")

    conn = get_db_connection()
    try:
        # Extra proof for accounts protected with 2FA
        row = conn.execute(
            "SELECT is_enabled FROM user_2fa WHERE user_id = ?", (user["id"],)
        ).fetchone()
        if row and row["is_enabled"]:
            if not (payload.totp_code or "").strip():
                raise HTTPException(status_code=400, detail="Enter your authenticator code to confirm.")
            _verify_second_factor(conn, user["id"], payload.totp_code)

        conn.execute(
            "UPDATE users SET password=?, password_changed_at=?, updated_at=? WHERE id=?",
            (hash_password(new_password), now_utc_str(), now_utc_str(), user["id"]),
        )
        # Security standard: a password change kicks out every OTHER device.
        cur = conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND id != ?", (user["id"], current["id"])
        )
        conn.commit()
        revoked = getattr(cur, "rowcount", 0) or 0
        _log_security_event(conn, user["id"], "password_changed",
                            f"Password changed from Settings · {revoked} other device(s) signed out")
        return {
            "message": "Password updated. For your security, all other devices have been signed out.",
            "other_sessions_revoked": revoked,
        }
    finally:
        conn.close()




# ----------------------------
# Sessions
# ----------------------------
@router.get("/sessions")
def list_sessions(authorization: Optional[str] = Header(None)):
    user, current_session = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, device_info, ip_address, created_at, last_seen FROM sessions WHERE user_id = ? ORDER BY last_seen DESC",
            (user["id"],)
        ).fetchall()
        return {
            "sessions": [
                {
                    "id": r["id"],
                    "device_info": r["device_info"],
                    "ip_address": r["ip_address"],
                    "created_at": r["created_at"],
                    "last_seen": r["last_seen"],
                    "is_current": r["id"] == current_session["id"]
                } for r in rows
            ]
        }
    finally:
        conn.close()


@router.post("/sessions/revoke")
def revoke_session(payload: SessionRevoke, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM sessions WHERE id = ? AND user_id = ?", (payload.session_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found.")
        conn.execute("DELETE FROM sessions WHERE id = ?", (payload.session_id,))
        conn.commit()
        return {"message": "Session logged out."}
    finally:
        conn.close()


# ----------------------------
# Forgot Password
# ----------------------------
@router.get("/login-history")
def get_login_history(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute("""
            SELECT ip_address, device_info, location, success, created_at 
            FROM login_history 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 20
        """, (user["id"],)).fetchall()
        return {"history": [dict(r) for r in rows]}
    finally:
        conn.close()




@router.get("/preferences")
def get_preferences(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_preferences WHERE user_id = ?", (user["id"],)).fetchone()
        if not row:
            return {"theme": "dark", "language": "en", "timezone": "UTC", 
                   "notifications_enabled": True, "email_notifications": True}
        return dict(row)
    finally:
        conn.close()


@router.put("/preferences")
def update_preferences(payload: UserPreferencesUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    current_time = now_utc_str()
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_preferences WHERE user_id = ?", (user["id"],)).fetchone()
        
        if row:
            theme = payload.theme if payload.theme is not None else row["theme"]
            language = payload.language if payload.language is not None else row["language"]
            timezone = payload.timezone if payload.timezone is not None else row["timezone"]
            notifications = 1 if payload.notifications_enabled else 0 if payload.notifications_enabled is not None else row["notifications_enabled"]
            email_notif = 1 if payload.email_notifications else 0 if payload.email_notifications is not None else row["email_notifications"]
            
            conn.execute(
                "UPDATE user_preferences SET theme=?, language=?, timezone=?, notifications_enabled=?, email_notifications=?, updated_at=? WHERE user_id=?",
                (theme, language, timezone, notifications, email_notif, current_time, user["id"])
            )
        else:
            conn.execute(
                "INSERT INTO user_preferences (user_id, theme, language, timezone, notifications_enabled, email_notifications, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user["id"], payload.theme or "dark", payload.language or "en", payload.timezone or "UTC",
                 1 if payload.notifications_enabled else 0, 1 if payload.email_notifications else 0, current_time, current_time)
            )
        conn.commit()
        return {"message": "Preferences updated"}
    finally:
        conn.close()


# ================================
# ================================
# ACTIVITY LOG
# ================================
class ActivityLogEntry(BaseModel):
    action: str
    details: str = ""


@router.get("/activity-log")
def get_activity_log(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM activity_log WHERE user_id = ? ORDER BY created_at DESC LIMIT 100",
            (user["id"],)
        ).fetchall()
        return {"activities": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.post("/activity-log")
def log_activity(payload: ActivityLogEntry, authorization: Optional[str] = Header(None), request: Request = None):
    user, _ = get_current_user_and_session(authorization)
    current_time = now_utc_str()
    ip = client_ip(request) if request else "unknown"
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO activity_log (user_id, action, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], payload.action, details := payload.details or "", ip, current_time)
        )
        conn.commit()
        return {"message": "Activity logged"}
    finally:
        conn.close()
