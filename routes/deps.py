"""Shared kernel for every route module: config, rate limiting, validators,
password hashing, sessions, login history, the authed-user dependency, admin
grant, the security-event logger, and ALL cross-module pydantic models.

Route modules import from here (never from app.py) so there is no circularity:
app.py includes the routers; the routers see only deps + services."""
import os
import re
import json
import time
import secrets
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional, List

import bcrypt
import pyotp
from fastapi import HTTPException, Header, Request
from pydantic import BaseModel, EmailStr

from database import (
    get_db_connection,
    init_db,
    DIALECT,
    IntegrityError as DBIntegrityError,
)

logger = logging.getLogger("ahad-co-app")

OTP_EXPIRY_MINUTES = int(os.getenv("OTP_EXPIRY_MINUTES", "10"))
MAX_OTP_ATTEMPTS = int(os.getenv("MAX_OTP_ATTEMPTS", "5"))  # wrong codes before the code dies

USERNAME_REGEX = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")

# ----------------------------
# Simple in-memory rate limiter
# ----------------------------
RATE_LIMIT_WINDOW = 300
RATE_LIMIT_MAX_ATTEMPTS = 6
_attempts = defaultdict(list)


SIGNUP_DAILY_MAX = 10  # abuse guard: max fresh accounts per IP per day


def rate_limit_custom(key: str, window_s: int, max_attempts: int, detail: str):
    now = time.time()
    window_start = now - window_s
    _attempts[key] = [t for t in _attempts[key] if t > window_start]
    if len(_attempts[key]) >= max_attempts:
        raise HTTPException(status_code=429, detail=detail)
    _attempts[key].append(now)


def rate_limit(key: str):
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    _attempts[key] = [t for t in _attempts[key] if t > window_start]
    if len(_attempts[key]) >= RATE_LIMIT_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many attempts. Please try again in a few minutes.")
    _attempts[key].append(now)


def rate_limit_user(user_id, bucket: str, max_attempts: int = 30, window_s: int = 300):
    """Per-ACCOUNT limiter for AUTHENTICATED actions (run code, deploy, restart).

    Never key these buckets by IP: our audience (Telegram bot devs, many on
    Bangladeshi mobile networks) sits behind CGNAT — hundreds of strangers
    share ONE public IP, so an IP-keyed bucket lets random people burn the
    allowance of everyone else on that IP ("Limit reached" with zero usage).
    Per-IP limits stay ONLY on unauthenticated anti-bot routes (signup/login)."""
    rate_limit_custom(
        f"u{user_id}:{bucket}", window_s, max_attempts,
        "You are clicking too fast — wait a few seconds and try again.")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def parse_device(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "android" in ua:
        os_name = "Android"
    elif "iphone" in ua or "ipad" in ua:
        os_name = "iOS"
    elif "windows" in ua:
        os_name = "Windows"
    elif "mac os" in ua or "macintosh" in ua:
        os_name = "macOS"
    elif "linux" in ua:
        os_name = "Linux"
    else:
        os_name = "Unknown OS"

    if "chrome" in ua and "edg" not in ua:
        browser = "Chrome"
    elif "firefox" in ua:
        browser = "Firefox"
    elif "safari" in ua and "chrome" not in ua:
        browser = "Safari"
    elif "edg" in ua:
        browser = "Edge"
    else:
        browser = "Browser"

    return f"{browser} on {os_name}"


# ----------------------------
# Models
# ----------------------------
class UserSignup(BaseModel):
    username: str
    email: EmailStr
    password: str
    agreed_terms: Optional[bool] = None


class UserVerify(BaseModel):
    username: str
    otp: str


class UserLogin(BaseModel):
    username: str
    password: str


class ResendOTP(BaseModel):
    username: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class VerifyResetOTP(BaseModel):
    email: EmailStr
    otp: str


class ResetPassword(BaseModel):
    email: EmailStr
    otp: str
    new_password: str


class LinkItem(BaseModel):
    label: str
    url: str


class ProfileUpdate(BaseModel):
    phone: Optional[str] = None
    custom_code: Optional[str] = None
    links: Optional[List[LinkItem]] = None


class SessionRevoke(BaseModel):
    session_id: int


class AccountDelete(BaseModel):
    password: str


class TwoFactorSetup(BaseModel):
    enable: bool


class TwoFactorVerify(BaseModel):
    code: str
    temp_token: Optional[str] = None


class TwoFactorConfirm(BaseModel):
    """password + current 2FA code — required for disable / regen backup codes."""
    password: str
    code: str


class ChangePassword(BaseModel):
    current_password: str
    new_password: str
    totp_code: Optional[str] = None

# ----------------------------
# DB Helpers
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_utc_str() -> str:
    return now_utc().isoformat()


init_db()


def validate_username(username: str) -> str:
    username = username.strip()
    if not USERNAME_REGEX.fullmatch(username):
        raise HTTPException(status_code=400, detail="Username must be 3-30 characters (letters, numbers, _, ., - only).")
    return username


def validate_password(password: str) -> str:
    password = password.strip()
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if len(password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password is too long (max 72 characters).")
    return password


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def generate_token() -> str:
    return secrets.token_hex(32)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False


def create_session(user_id: int, request: Request) -> str:
    token = generate_token()
    device_info = parse_device(request.headers.get("user-agent", ""))
    ip = client_ip(request)
    current_time = now_utc_str()

    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO sessions (user_id, token, device_info, ip_address, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, token, device_info, ip, current_time, current_time))
        conn.commit()
    finally:
        conn.close()

    return token


def record_login_attempt(user_id: int, request: Request, success: bool, location: Optional[str] = None):
    """Persist a login attempt (success or failure) to login_history.

    Failures here must never break the calling request, so all errors are
    swallowed and only logged. Used by the login/verify flows so the user
    can see a real activity trail on their dashboard.
    """
    try:
        conn = get_db_connection()
        try:
            conn.execute("""
                INSERT INTO login_history (user_id, ip_address, device_info, location, success, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, client_ip(request), parse_device(request.headers.get("user-agent", "")),
                  location, 1 if success else 0, now_utc_str()))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record login attempt: %s", exc)




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
        if "is_suspended" in user_row.keys() and user_row["is_suspended"]:
            # Suspended accounts are locked out of EVERY authed route (jobs included).
            raise HTTPException(status_code=401, detail="This account is suspended.")

        conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now_utc_str(), session_row["id"]))
        conn.commit()

        return user_row, session_row
    finally:
        conn.close()


def require_role(allowed_roles: List[str]):
    """FastAPI dependency to enforce role-based access control (RBAC)."""
    def role_checker(authorization: Optional[str] = Header(None)):
        user_row, session_row = get_current_user_and_session(authorization)
        user_role = user_row["role"] if "role" in user_row.keys() else "user"
        if user_role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Forbidden: You do not have sufficient privileges for this action.")
        return user_row, session_row
    return role_checker


# ----------------------------
# Static / Health
# ----------------------------
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", os.getenv("ADMIN_EMAIL", "")).split(",")
    if e.strip()
}


def _grant_admin_if_configured(user_id: int, email: str):
    """The owner's account (matched by ADMIN_EMAIL(S) env) gets is_admin=1
    automatically on login/verify — no manual DB editing needed."""
    if not ADMIN_EMAILS or not email:
        return
    if email.lower() not in ADMIN_EMAILS:
        return
    try:
        conn = get_db_connection()
        try:
            conn.execute("UPDATE users SET is_admin=1, updated_at=? WHERE id=? AND (is_admin IS NULL OR is_admin=0)",
                         (now_utc_str(), user_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("admin grant failed for user %s: %s", user_id, exc)




def _log_security_event(conn, user_id: int, action: str, details: str):
    """Fire-and-forget security event into the user-visible activity log."""
    try:
        conn.execute(
            "INSERT INTO activity_log (user_id, action, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, f"security:{action}", details, "", now_utc_str()),
        )
        conn.commit()
    except Exception:
        pass


# `from routes.deps import *` must also export single-underscore helpers
# (_grant_admin_if_configured, _log_security_event, …) — star imports skip
# them by default unless __all__ says otherwise.
__all__ = [n for n in dir() if not n.startswith("__")]
