import os
import re
import json
import time
import asyncio
import secrets
import logging
import pyotp
import qrcode
import io
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

import bcrypt
import requests
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional, List

# Database layer (supports SQLite for local dev and PostgreSQL/Supabase for prod).
# The engine is chosen automatically via the DATABASE_URL env var.
from database import (
    get_db_connection,
    init_db,
    DIALECT,
    IntegrityError as DBIntegrityError,
)

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("ahad-co-app")

# ----------------------------
# Paths / Config
# ----------------------------
BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"
STATIC_DIR = BASE_DIR / "static"

OTP_EXPIRY_MINUTES = int(os.getenv("OTP_EXPIRY_MINUTES", "10"))
MAX_OTP_ATTEMPTS = int(os.getenv("MAX_OTP_ATTEMPTS", "5"))  # wrong codes before the code dies
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"

USERNAME_REGEX = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
VALID_ENTRY_TYPES = {"phone", "email", "code", "link", "note", "password", "secret_file", "file"}

# ----------------------------
# App
# ----------------------------
app = FastAPI(title="Ahad Co Auth System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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


class VaultEntryCreate(BaseModel):
    type: str
    label: str
    value: str


class VaultEntryUpdate(BaseModel):
    id: int
    type: Optional[str] = None
    label: Optional[str] = None
    value: Optional[str] = None


class VaultEntryDelete(BaseModel):
    id: int


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
# ----------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_utc_str() -> str:
    return now_utc().isoformat()


# ----------------------------
# Schema initialisation
# ----------------------------
# get_db_connection() and init_db() are imported from database.py (see top of
# file). They transparently support both SQLite (local dev) and PostgreSQL
# (Supabase / managed Postgres) via the DATABASE_URL env var.


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


def send_email(receiver_email: str, subject: str, otp: str, username: str, purpose: str):
    brevo_api_key = os.getenv("BREVO_API_KEY", "").strip()
    sender_email = os.getenv("SENDER_EMAIL", "").strip()
    sender_name = os.getenv("SENDER_NAME", "Ahad Co").strip()

    if not brevo_api_key or not sender_email:
        logger.error("BREVO_API_KEY or SENDER_EMAIL missing.")
        raise HTTPException(status_code=500, detail="Email service is not configured.")

    headers = {"accept": "application/json", "api-key": brevo_api_key, "content-type": "application/json"}

    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height:1.6; background:#0B0C14; padding:24px;">
        <div style="max-width:420px;margin:0 auto;background:#14152a;border-radius:16px;padding:28px;color:#F5F5FA;">
          <h2 style="color:#7C6CF6;margin-top:0;">{purpose}</h2>
          <p>Hello <b>{username}</b>,</p>
          <p>Your verification code is:</p>
          <div style="font-size:32px;font-weight:bold;letter-spacing:6px;color:#2FD9C4;text-align:center;
                      background:rgba(255,255,255,0.06);padding:16px;border-radius:12px;margin:16px 0;">
            {otp}
          </div>
          <p style="color:#A0A0B2;font-size:13px;">This code expires in {OTP_EXPIRY_MINUTES} minutes.
          If you did not request this, you can safely ignore this email.</p>
          <p style="color:#A0A0B2;font-size:12px;margin-top:24px;">— Ahad Co</p>
        </div>
      </body>
    </html>
    """

    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": receiver_email}],
        "subject": subject,
        "textContent": f"Hello {username},\n\nYour code is: {otp}\n\nExpires in {OTP_EXPIRY_MINUTES} minutes.",
        "htmlContent": html_content
    }

    try:
        response = requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=20)
        logger.info("Brevo status: %s", response.status_code)
        if response.status_code not in (200, 201, 202):
            raise HTTPException(status_code=500, detail="Failed to send email. Please try again.")
    except requests.RequestException:
        logger.exception("Brevo request failed")
        raise HTTPException(status_code=500, detail="Email service is temporarily unavailable.")


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
@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def read_index():
    if not INDEX_FILE.exists():
        raise HTTPException(status_code=404, detail="index.html not found.")
    return FileResponse(INDEX_FILE)


# Client-side routing: every app section has a real URL (/vault, /code, …).
# Deep links / refreshes on these paths must serve the SPA shell — the
# frontend router reads the path and opens the right section (after auth).
#
# The API lives at ROOT (GET /contacts returns JSON), so section URLs that
# collide with an API GET need content negotiation: a browser navigation
# (Accept: text/html, no Authorization header — tokens ride fetch headers,
# never visible in address-bar navigations) gets the SPA shell, while the
# SPA's own authed fetch on the same path gets the JSON data. Registered
# BEFORE the API routes on purpose, since route order decides the match.
def _spa_negotiator(fn_name: str):
    def handler(request: Request):
        accept = (request.headers.get("accept") or "").lower()
        auth_hdr = request.headers.get("authorization")
        if "text/html" in accept and not auth_hdr:
            if not INDEX_FILE.exists():
                raise HTTPException(status_code=404, detail="index.html not found.")
            return FileResponse(INDEX_FILE)
        fn = globals().get(fn_name)   # resolved at request time (defined below)
        if fn is None:
            raise HTTPException(status_code=404, detail="Not found.")
        return fn(authorization=auth_hdr)
    return handler


_NEGOTIATED = {
    "contacts": "list_contacts", "wifi": "list_wifi", "vault": "list_vault",
    "cards": "list_cards", "identities": "list_identities", "servers": "list_servers",
    "recovery": "list_recovery", "notes": "list_notes", "bookmarks": "list_bookmarks",
    "tasks": "list_tasks", "profile": "get_profile",
}
for _p, _fn in _NEGOTIATED.items():
    app.get("/" + _p, include_in_schema=False)(_spa_negotiator(_fn))

# Section URLs with NO API collision can serve the shell directly.
CLIENT_ONLY_PATHS = [
    "dashboard", "seeds", "code", "jobs", "runspace", "admin", "activity",
    "sign-in", "sign-up", "login", "forgot",
]
for _p in CLIENT_ONLY_PATHS:
    app.get("/" + _p, include_in_schema=False)(read_index)

# Back-compat heal: a frontend bug once produced published-page links like
# /code/s/<token> (the tab path was glued onto the origin). Redirect any
# shared copies to the real public page instead of a cold JSON 404.
@app.get("/code/s/{token}", include_in_schema=False)
def code_share_redirect(token: str):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/s/{token}", status_code=301)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "database": DIALECT,
        "brevo_api_key_set": bool(os.getenv("BREVO_API_KEY", "").strip()),
        "sender_email_set": bool(os.getenv("SENDER_EMAIL", "").strip()),
    }


# ----------------------------
# Signup / Verify (auto-login) / Resend
# ----------------------------
class AvailabilityCheck(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None


@app.post("/auth/check-availability")
def check_availability(payload: AvailabilityCheck, request: Request):
    """Early duplicate check for the sign-up form (on blur / before submit).

    Returns which of the two fields is taken by a VERIFIED account, so the UI
    can say \"already registered\" — unverified leftovers don't count as taken,
    matching the /signup rule that refreshes them instead of blocking.
    """
    rate_limit(f"{client_ip(request)}:avail")
    username = (payload.username or "").strip()
    email = (payload.email or "").strip().lower()

    username_taken = False
    email_taken = False
    conn = get_db_connection()
    try:
        if username:
            row = conn.execute(
                "SELECT is_verified FROM users WHERE username = ?", (username,)
            ).fetchone()
            username_taken = bool(row and row["is_verified"] == 1)
        if email:
            row = conn.execute(
                "SELECT is_verified FROM users WHERE email = ?", (email,)
            ).fetchone()
            email_taken = bool(row and row["is_verified"] == 1)
    finally:
        conn.close()

    return {"username_taken": username_taken, "email_taken": email_taken}


@app.post("/signup")
def signup(user: UserSignup, request: Request):
    rate_limit(f"{client_ip(request)}:signup")
    rate_limit_custom(
        f"{client_ip(request)}:signup:daily", 86400, SIGNUP_DAILY_MAX,
        "Too many new accounts from this network today. Please try again tomorrow.")
    if user.agreed_terms is not True:
        raise HTTPException(status_code=400, detail="Please accept the Terms of Use to create an account.")

    username = validate_username(user.username)
    email = str(user.email).strip().lower()
    password = validate_password(user.password)

    otp = generate_otp()
    hashed_pw = hash_password(password)
    current_time = now_utc_str()

    conn = get_db_connection()
    cursor = conn.cursor()
    inserted_user_id = None

    try:
        existing = cursor.execute(
            "SELECT id, username, email, is_verified FROM users WHERE username = ? OR email = ?",
            (username, email),
        ).fetchone()

        if existing:
            if existing["is_verified"] == 1:
                # Genuinely taken by an active account -> cannot reuse.
                raise HTTPException(status_code=400, detail="Username or email is already taken.")
            # Unverified account from an incomplete signup (e.g. the user lost
            # the OTP page while checking mail). Don't block them: refresh the
            # OTP + password and re-send, so they can finish verifying instead
            # of being stuck on "already taken".
            otp = generate_otp()
            current_time = now_utc_str()
            cursor.execute("""
                UPDATE users SET password=?, otp=?, otp_created_at=?, agreed_terms_at=?, updated_at=?
                WHERE id=?
            """, (hashed_pw, otp, current_time, current_time, current_time, existing["id"]))
            conn.commit()
            send_email(email, "Verify your Ahad Co account", otp, username, "Email Verification")
            return {
                "message": "Welcome back! A fresh verification code was sent to your email.",
                "resent": True,
                "expires_in": OTP_EXPIRY_MINUTES * 60,
            }

        cursor.execute("""
            INSERT INTO users (username, email, password, otp, otp_created_at,
                is_verified, created_at, updated_at, agreed_terms_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
        """, (username, email, hashed_pw, otp, current_time, current_time, current_time, current_time))
        conn.commit()
        inserted_user_id = cursor.lastrowid

        send_email(email, "Verify your Ahad Co account", otp, username, "Email Verification")
        return {"message": "Account created. Check your email for the verification code.", "expires_in": OTP_EXPIRY_MINUTES * 60}

    except HTTPException:
        if inserted_user_id:
            cursor.execute("DELETE FROM users WHERE id = ?", (inserted_user_id,))
            conn.commit()
        raise
    except DBIntegrityError:
        raise HTTPException(status_code=400, detail="Username or email is already taken.")
    finally:
        conn.close()


@app.post("/resend-otp")
def resend_otp(payload: ResendOTP, request: Request):
    username = validate_username(payload.username)
    rate_limit(f"{client_ip(request)}:resend:{username}")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        row = cursor.execute("SELECT id, email, is_verified FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Account not found.")
        if row["is_verified"] == 1:
            return {"message": "Account is already verified."}

        new_otp = generate_otp()
        current_time = now_utc_str()
        cursor.execute("UPDATE users SET otp=?, otp_created_at=?, updated_at=? WHERE id=?",
                        (new_otp, current_time, current_time, row["id"]))
        conn.commit()

        send_email(row["email"], "Your new verification code", new_otp, username, "Email Verification")
        return {"message": "A new code has been sent to your email.", "expires_in": OTP_EXPIRY_MINUTES * 60}
    finally:
        conn.close()


@app.post("/verify")
def verify_otp(user: UserVerify, request: Request):
    username = validate_username(user.username)
    otp = user.otp.strip()
    rate_limit(f"{client_ip(request)}:verify:{username}")

    if not otp.isdigit() or len(otp) != 6:
        raise HTTPException(status_code=400, detail="Code must be 6 digits.")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        row = cursor.execute(
            "SELECT id, otp, otp_created_at, otp_attempts, is_verified, username, email FROM users WHERE username = ?", (username,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Account not found.")

        if row["is_verified"] == 0:
            db_otp = row["otp"]
            otp_created_at = row["otp_created_at"]
            if not db_otp or not otp_created_at:
                raise HTTPException(status_code=400, detail="No active code found. Please resend.")

            created_time = datetime.fromisoformat(otp_created_at)
            if now_utc() > created_time + timedelta(minutes=OTP_EXPIRY_MINUTES):
                raise HTTPException(status_code=400, detail="Code has expired. Please resend.")
            if db_otp != otp:
                # Wrong-code limiter, server-side: after MAX_OTP_ATTEMPTS wrong
                # tries the code is invalidated and a fresh one is required.
                attempts = (row["otp_attempts"] or 0) + 1
                if attempts >= MAX_OTP_ATTEMPTS:
                    cursor.execute(
                        "UPDATE users SET otp=NULL, otp_created_at=NULL, otp_attempts=0, updated_at=? WHERE id=?",
                        (now_utc_str(), row["id"]))
                    conn.commit()
                    raise HTTPException(status_code=400, detail="Too many incorrect attempts — please request a new code.")
                cursor.execute("UPDATE users SET otp_attempts=?, updated_at=? WHERE id=?",
                               (attempts, now_utc_str(), row["id"]))
                conn.commit()
                raise HTTPException(status_code=400, detail="Incorrect code.")

            cursor.execute("""
                UPDATE users SET is_verified=1, otp=NULL, otp_created_at=NULL, otp_attempts=0, updated_at=?
                WHERE id=?
            """, (now_utc_str(), row["id"]))
            conn.commit()
            record_login_attempt(row["id"], request, success=True, location="Email verification")

        # Auto-login: create a session immediately after successful verification
        _grant_admin_if_configured(row["id"], row["email"])
        token = create_session(row["id"], request)
        return {"message": "Verification successful!", "token": token, "username": row["username"]}
    finally:
        conn.close()


# ----------------------------
# Login / Logout / Sessions
# ----------------------------
@app.post("/login")
def login(user: UserLogin, request: Request):
    identifier = user.username.strip()
    rate_limit(f"{client_ip(request)}:login:{identifier.lower()}")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if "@" in identifier:
            row = cursor.execute(
                "SELECT id, username, password, is_verified, email, is_suspended FROM users WHERE email = ?", (identifier.lower(),)
            ).fetchone()
        else:
            row = cursor.execute(
                "SELECT id, username, password, is_verified, email, is_suspended FROM users WHERE username = ?", (identifier,)
            ).fetchone()

        if not row or not verify_password(user.password, row["password"]):
            # Record the failed attempt if we could identify the account.
            if row:
                record_login_attempt(row["id"], request, success=False)
            raise HTTPException(status_code=400, detail="Incorrect username/email or password.")
        if row["is_verified"] == 0:
            # Correct credentials, but email not verified yet. Instead of an
            # error, route them straight to verification so they can finish
            # without having to re-signup.
            remaining = OTP_EXPIRY_MINUTES * 60
            try:
                r2 = cursor.execute("SELECT otp_created_at FROM users WHERE id = ?", (row["id"],)).fetchone()
                if r2 and r2["otp_created_at"]:
                    created = datetime.fromisoformat(r2["otp_created_at"])
                    remaining = max(0, OTP_EXPIRY_MINUTES * 60 - int((now_utc() - created).total_seconds()))
            except Exception:
                pass
            return {
                "need_verify": True,
                "username": row["username"],
                "message": "Please verify your email to continue. A code was sent when you signed up.",
                "expires_in": remaining,
            }
        if "is_suspended" in row.keys() and row["is_suspended"]:
            raise HTTPException(
                status_code=403,
                detail="This account is suspended. If you think this is a mistake, contact the site owner.")
    finally:
        conn.close()

    record_login_attempt(row["id"], request, success=True)
    _grant_admin_if_configured(row["id"], row["email"])
    token = create_session(row["id"], request)
    return {"message": "Login successful!", "username": row["username"], "token": token}


@app.post("/logout")
def logout(authorization: Optional[str] = Header(None)):
    _, session_row = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_row["id"],))
        conn.commit()
        return {"message": "Logged out successfully."}
    finally:
        conn.close()


@app.get("/sessions")
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


@app.post("/sessions/revoke")
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
@app.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, request: Request):
    email = str(payload.email).strip().lower()
    rate_limit(f"{client_ip(request)}:forgot:{email}")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        row = cursor.execute("SELECT id, username FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            return {"message": "If this email exists, a reset code has been sent."}

        otp = generate_otp()
        current_time = now_utc_str()
        cursor.execute("""
            UPDATE users SET reset_otp=?, reset_otp_created_at=?, reset_verified=0, updated_at=?
            WHERE id=?
        """, (otp, current_time, current_time, row["id"]))
        conn.commit()

        send_email(email, "Reset your Ahad Co password", otp, row["username"], "Password Reset")
        return {"message": "If this email exists, a reset code has been sent.", "expires_in": OTP_EXPIRY_MINUTES * 60}
    finally:
        conn.close()


@app.post("/verify-reset-otp")
def verify_reset_otp(payload: VerifyResetOTP, request: Request):
    email = str(payload.email).strip().lower()
    otp = payload.otp.strip()
    rate_limit(f"{client_ip(request)}:resetverify:{email}")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        row = cursor.execute(
            "SELECT id, reset_otp, reset_otp_created_at, reset_otp_attempts FROM users WHERE email = ?", (email,)
        ).fetchone()
        if not row or not row["reset_otp"]:
            raise HTTPException(status_code=400, detail="Please request a reset code first.")

        created_time = datetime.fromisoformat(row["reset_otp_created_at"])
        if now_utc() > created_time + timedelta(minutes=OTP_EXPIRY_MINUTES):
            raise HTTPException(status_code=400, detail="Code has expired. Please request a new one.")
        if row["reset_otp"] != otp:
            attempts = (row["reset_otp_attempts"] or 0) + 1
            if attempts >= MAX_OTP_ATTEMPTS:
                cursor.execute(
                    "UPDATE users SET reset_otp=NULL, reset_otp_created_at=NULL, reset_otp_attempts=0, updated_at=? WHERE id=?",
                    (now_utc_str(), row["id"]))
                conn.commit()
                raise HTTPException(status_code=400, detail="Too many incorrect attempts — please request a new code.")
            cursor.execute("UPDATE users SET reset_otp_attempts=?, updated_at=? WHERE id=?",
                           (attempts, now_utc_str(), row["id"]))
            conn.commit()
            raise HTTPException(status_code=400, detail="Incorrect code.")

        cursor.execute("UPDATE users SET reset_verified=1, reset_otp_attempts=0, updated_at=? WHERE id=?", (now_utc_str(), row["id"]))
        conn.commit()
        return {"message": "Code verified. You can now set a new password."}
    finally:
        conn.close()


@app.post("/reset-password")
def reset_password(payload: ResetPassword, request: Request):
    email = str(payload.email).strip().lower()
    new_password = validate_password(payload.new_password)
    rate_limit(f"{client_ip(request)}:resetpw:{email}")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        row = cursor.execute(
            "SELECT id, reset_otp, reset_verified FROM users WHERE email = ?", (email,)
        ).fetchone()
        if not row or row["reset_verified"] != 1 or row["reset_otp"] != payload.otp.strip():
            raise HTTPException(status_code=400, detail="Please verify the reset code first.")

        hashed_pw = hash_password(new_password)
        cursor.execute("""
            UPDATE users SET password=?, reset_otp=NULL, reset_otp_created_at=NULL,
                reset_verified=0, password_changed_at=?, updated_at=?
            WHERE id=?
        """, (hashed_pw, now_utc_str(), now_utc_str(), row["id"]))
        # Reset password -> log out of all devices for safety
        cursor.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        conn.commit()
        return {"message": "Password updated successfully. Please sign in again."}
    finally:
        conn.close()


# ----------------------------
# Profile
# ----------------------------
@app.get("/profile")
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


@app.post("/profile/update")
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


# ----------------------------
# Data Vault (multiple phone/email/code/link/note entries)
# ----------------------------
@app.get("/vault")
def list_vault(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, type, label, value, created_at, updated_at FROM vault_entries WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],)
        ).fetchall()
        return {"entries": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/vault/add")
def add_vault_entry(payload: VaultEntryCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    entry_type = payload.type.strip().lower()
    if entry_type not in VALID_ENTRY_TYPES:
        raise HTTPException(status_code=400, detail="Invalid entry type.")
    label = payload.label.strip()
    value = payload.value.strip()
    if not label or not value:
        raise HTTPException(status_code=400, detail="Label and value are required.")

    conn = get_db_connection()
    try:
        current_time = now_utc_str()
        cursor = conn.execute("""
            INSERT INTO vault_entries (user_id, type, label, value, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user["id"], entry_type, label, value, current_time, current_time))
        conn.commit()
        return {"message": "Entry added.", "id": cursor.lastrowid}
    finally:
        conn.close()


@app.post("/vault/update")
def update_vault_entry(payload: VaultEntryUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM vault_entries WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Entry not found.")

        entry_type = payload.type.strip().lower() if payload.type else row["type"]
        if entry_type not in VALID_ENTRY_TYPES:
            raise HTTPException(status_code=400, detail="Invalid entry type.")
        label = payload.label.strip() if payload.label is not None else row["label"]
        value = payload.value.strip() if payload.value is not None else row["value"]

        conn.execute("""
            UPDATE vault_entries SET type=?, label=?, value=?, updated_at=? WHERE id=?
        """, (entry_type, label, value, now_utc_str(), payload.id))
        conn.commit()
        return {"message": "Entry updated."}
    finally:
        conn.close()


@app.post("/vault/delete")
def delete_vault_entry(payload: VaultEntryDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM vault_entries WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Entry not found.")
        conn.execute("DELETE FROM vault_entries WHERE id = ?", (payload.id,))
        conn.commit()
        return {"message": "Entry deleted."}
    finally:
        conn.close()


# ----------------------------
# Account Deletion
# ----------------------------
@app.post("/account/delete")
def delete_account(payload: AccountDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    if not verify_password(payload.password, user["password"]):
        raise HTTPException(status_code=400, detail="Incorrect password.")

    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM vault_entries WHERE user_id = ?", (user["id"],))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        conn.commit()
        return {"message": "Account deleted permanently."}
    finally:
        conn.close()


@app.post("/account/change-password")
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
# Two-Factor Authentication (2FA)
# ----------------------------
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


def _verify_second_factor(conn, user_id: int, code: str) -> tuple:
    """Verify a 6-digit TOTP code OR a single-use backup code for `user_id`.
    Returns (row, remaining_backup_count) on success, raises 400 otherwise.
    A used backup code is consumed (removed) immediately."""
    row = conn.execute("SELECT * FROM user_2fa WHERE user_id = ?", (user_id,)).fetchone()
    if not row or not row["is_enabled"]:
        raise HTTPException(status_code=400, detail="2FA is not enabled on this account.")
    code = (code or "").strip()
    backup_codes = json.loads(row["backup_codes"] or "[]")
    if code and code.lower() in [c.lower() for c in backup_codes]:
        remaining = [c for c in backup_codes if c.lower() != code.lower()]
        conn.execute("UPDATE user_2fa SET backup_codes=?, updated_at=? WHERE user_id=?",
                     (json.dumps(remaining), now_utc_str(), user_id))
        conn.commit()
        return row, len(remaining)
    totp = pyotp.TOTP(row["secret"])
    if not totp.verify(code):
        raise HTTPException(status_code=400, detail="Incorrect authenticator code.")
    return row, len(backup_codes)


@app.get("/2fa/status")
def get_2fa_status(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_2fa WHERE user_id = ?", (user["id"],)).fetchone()
        if not row:
            return {"enabled": False, "backup_codes_count": 0}
        return {
            "enabled": bool(row["is_enabled"]),
            "backup_codes_count": len(json.loads(row["backup_codes"] or "[]"))
        }
    finally:
        conn.close()


@app.post("/2fa/setup")
def setup_2fa(payload: TwoFactorSetup, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        current_time = now_utc_str()
        
        if payload.enable:
            # Generate new TOTP secret
            secret = pyotp.random_base32()
            
            # Generate backup codes (10 × single-use)
            backup_codes = [secrets.token_hex(8) for _ in range(10)]
            
            # Store temporarily (not enabled yet)
            if DIALECT == "postgres":
                conn.execute("""
                    INSERT INTO user_2fa (user_id, secret, is_enabled, backup_codes, created_at, updated_at)
                    VALUES (?, ?, 0, ?, ?, ?)
                    ON CONFLICT (user_id) DO UPDATE SET
                        secret = EXCLUDED.secret,
                        is_enabled = 0,
                        backup_codes = EXCLUDED.backup_codes,
                        updated_at = EXCLUDED.updated_at
                """, (user["id"], secret, json.dumps(backup_codes), current_time, current_time))
            else:
                conn.execute("""
                    INSERT OR REPLACE INTO user_2fa (user_id, secret, is_enabled, backup_codes, created_at, updated_at)
                    VALUES (?, ?, 0, ?, ?, ?)
                """, (user["id"], secret, json.dumps(backup_codes), current_time, current_time))
            conn.commit()
            
            # Generate QR code
            totp = pyotp.TOTP(secret)
            uri = totp.provisioning_uri(name=user["username"], issuer_name="Ahad Co")
            
            # Generate QR image
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(uri)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            
            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            qr_base64 = base64.b64encode(buffer.getvalue()).decode()
            
            return {
                "secret": secret,
                "qr_code": f"data:image/png;base64,{qr_base64}",
                "backup_codes": backup_codes,
                "message": "Scan the QR code with your authenticator app"
            }
        else:
            # Disabling 2FA is a sensitive action — it MUST go through
            # /2fa/disable, which re-verifies password + a current code.
            raise HTTPException(
                status_code=400,
                detail="To disable 2FA, confirm with your password and an authenticator code."
            )
    finally:
        conn.close()


@app.post("/2fa/disable")
def disable_2fa(payload: TwoFactorConfirm, authorization: Optional[str] = Header(None)):
    """Disable 2FA — requires the account password AND a valid current
    authenticator (or backup) code. One-click disable is never allowed."""
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        if not verify_password(payload.password, user["password"]):
            raise HTTPException(status_code=400, detail="Incorrect password.")
        _verify_second_factor(conn, user["id"], payload.code)
        conn.execute("DELETE FROM user_2fa WHERE user_id = ?", (user["id"],))
        conn.commit()
        _log_security_event(conn, user["id"], "2fa_disabled", "Two-factor authentication was disabled")
        return {"message": "Two-factor authentication disabled."}
    finally:
        conn.close()


@app.post("/2fa/backup-codes")
def regenerate_backup_codes(payload: TwoFactorConfirm, authorization: Optional[str] = Header(None)):
    """Mint 10 fresh single-use backup codes (old ones stop working).
    Requires password + a valid current authenticator code."""
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        if not verify_password(payload.password, user["password"]):
            raise HTTPException(status_code=400, detail="Incorrect password.")
        _verify_second_factor(conn, user["id"], payload.code)
        codes = [secrets.token_hex(8) for _ in range(10)]
        conn.execute("UPDATE user_2fa SET backup_codes=?, updated_at=? WHERE user_id=?",
                     (json.dumps(codes), now_utc_str(), user["id"]))
        conn.commit()
        _log_security_event(conn, user["id"], "2fa_backup_regenerated", "Backup codes were regenerated")
        return {"backup_codes": codes, "message": "New backup codes generated."}
    finally:
        conn.close()


@app.post("/2fa/verify-setup")
def verify_2fa_setup(payload: TwoFactorVerify, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_2fa WHERE user_id = ?", (user["id"],)).fetchone()
        if not row or not row["secret"]:
            raise HTTPException(status_code=400, detail="2FA setup not initiated")
        
        if row["is_enabled"]:
            raise HTTPException(status_code=400, detail="2FA is already enabled")
        
        totp = pyotp.TOTP(row["secret"])
        if not totp.verify(payload.code):
            raise HTTPException(status_code=400, detail="Invalid verification code")
        
        # Enable 2FA
        conn.execute("UPDATE user_2fa SET is_enabled=1, updated_at=? WHERE user_id=?",
                     (now_utc_str(), user["id"]))
        conn.commit()
        _log_security_event(conn, user["id"], "2fa_enabled", "Two-factor authentication was enabled")

        # Hand the freshly-minted backup codes to the final setup screen so
        # the user can download/copy them (this is the ONLY time they see them).
        codes = json.loads(row["backup_codes"] or "[]")
        return {"message": "2FA enabled successfully!", "backup_codes": codes}
    finally:
        conn.close()


@app.post("/2fa/verify-login")
def verify_2fa_login(payload: TwoFactorVerify, authorization: Optional[str] = Header(None)):
    """Verify 2FA code during login when 2FA is enabled"""
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_2fa WHERE user_id = ?", (user["id"],)).fetchone()
        if not row or not row["is_enabled"]:
            raise HTTPException(status_code=400, detail="2FA not enabled")
        
        # Check if it's a backup code
        backup_codes = json.loads(row["backup_codes"] or "[]")
        if payload.code in backup_codes:
            # Remove used backup code
            backup_codes.remove(payload.code)
            conn.execute("UPDATE user_2fa SET backup_codes=? WHERE user_id=?", 
                         (json.dumps(backup_codes), user["id"]))
            conn.commit()
            return {"message": "Backup code accepted", "backup_codes_remaining": len(backup_codes)}
        
        # Verify TOTP
        totp = pyotp.TOTP(row["secret"])
        if not totp.verify(payload.code):
            raise HTTPException(status_code=400, detail="Invalid 2FA code")
        
        return {"message": "2FA verified successfully"}
    finally:
        conn.close()


# ----------------------------
# Login History
# ----------------------------
@app.get("/login-history")
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


# ----------------------------
# API Keys (for developers)
# ----------------------------
class APIKeyCreate(BaseModel):
    name: str


class APIKeyRevoke(BaseModel):
    key_id: int


class NoteCreate(BaseModel):
    title: str
    content: str
    color: Optional[str] = "#7C6CF6"


class NoteUpdate(BaseModel):
    id: int
    title: Optional[str] = None
    content: Optional[str] = None
    color: Optional[str] = None
    pinned: Optional[bool] = None


class NoteDelete(BaseModel):
    id: int


class BookmarkCreate(BaseModel):
    title: str
    url: str
    description: Optional[str] = None
    category: Optional[str] = None


class BookmarkUpdate(BaseModel):
    id: int
    title: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None


class BookmarkDelete(BaseModel):
    id: int


class PasswordGeneratorRequest(BaseModel):
    length: int = 16
    include_uppercase: bool = True
    include_numbers: bool = True
    include_symbols: bool = True


# ----------------------------
# Cards
# ----------------------------
class CardCreate(BaseModel):
    label: str
    holder: Optional[str] = None
    number: str
    expiry: Optional[str] = None
    cvv: Optional[str] = None
    brand: Optional[str] = None
    note: Optional[str] = None
    color: Optional[str] = "#6366f1"


class CardUpdate(BaseModel):
    id: int
    label: Optional[str] = None
    holder: Optional[str] = None
    number: Optional[str] = None
    expiry: Optional[str] = None
    cvv: Optional[str] = None
    brand: Optional[str] = None
    note: Optional[str] = None
    color: Optional[str] = None


class CardDelete(BaseModel):
    id: int


# ----------------------------
# Tasks
# ----------------------------
class TaskCreate(BaseModel):
    title: str
    priority: Optional[int] = 0


class TaskUpdate(BaseModel):
    id: int
    title: Optional[str] = None
    completed: Optional[bool] = None
    priority: Optional[int] = None


class TaskDelete(BaseModel):
    id: int


# ----------------------------
# Identities
# ----------------------------
class IdentityCreate(BaseModel):
    type: str
    label: str
    fields: Optional[dict] = None


class IdentityUpdate(BaseModel):
    id: int
    type: Optional[str] = None
    label: Optional[str] = None
    fields: Optional[dict] = None


# ----------------------------
# Contacts
# ----------------------------
class ContactCreate(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    address: Optional[str] = None
    note: Optional[str] = None


class ContactUpdate(BaseModel):
    id: int
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    address: Optional[str] = None
    note: Optional[str] = None


# ----------------------------
# WiFi
# ----------------------------
class WifiCreate(BaseModel):
    label: str
    ssid: str
    password: Optional[str] = None
    security: Optional[str] = "WPA"
    hidden: Optional[bool] = False
    location: Optional[str] = None


class WifiUpdate(BaseModel):
    id: int
    label: Optional[str] = None
    ssid: Optional[str] = None
    password: Optional[str] = None
    security: Optional[str] = None
    hidden: Optional[bool] = None
    location: Optional[str] = None


# ----------------------------
# Servers
# ----------------------------
class ServerCreate(BaseModel):
    name: str
    host: str
    port: Optional[int] = 22
    username: Optional[str] = None
    password: Optional[str] = None
    keyfile: Optional[str] = None
    note: Optional[str] = None


class ServerUpdate(BaseModel):
    id: int
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    keyfile: Optional[str] = None
    note: Optional[str] = None


# ----------------------------
# Recovery phrases
# ----------------------------
class RecoveryCreate(BaseModel):
    label: str
    words: str
    word_count: Optional[int] = 12


class RecoveryUpdate(BaseModel):
    id: int
    label: Optional[str] = None
    words: Optional[str] = None
    word_count: Optional[int] = None


class GenericDelete(BaseModel):
    id: int


# ----------------------------
# Snippets (code / pastebin)
# ----------------------------
class SnippetCreate(BaseModel):
    title: str
    language: Optional[str] = "text"
    content: str


class SnippetUpdate(BaseModel):
    id: int
    title: Optional[str] = None
    language: Optional[str] = None
    content: Optional[str] = None


class SnippetShare(BaseModel):
    id: int
    share: bool = True


class CategoryCreate(BaseModel):
    name: str
    icon: Optional[str] = "📁"
    color: Optional[str] = "#7C6CF6"


class CategoryUpdate(BaseModel):
    id: int
    name: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None


class CategoryDelete(BaseModel):
    id: int


class UserPreferencesUpdate(BaseModel):
    theme: Optional[str] = None
    language: Optional[str] = None
    timezone: Optional[str] = None
    notifications_enabled: Optional[bool] = None
    email_notifications: Optional[bool] = None


class ActivityLogEntry(BaseModel):
    action: str
    details: Optional[str] = None


@app.get("/api-keys")
def list_api_keys(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, last_used, created_at FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],)
        ).fetchall()
        return {"keys": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api-keys")
def create_api_key(payload: APIKeyCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    key = f"ahad_{secrets.token_hex(24)}"
    key_hash = hash_password(key)
    current_time = now_utc_str()
    
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO api_keys (user_id, name, key_hash, created_at) VALUES (?, ?, ?, ?)",
            (user["id"], payload.name, key_hash, current_time)
        )
        conn.commit()
        return {"message": "API key created", "key": key, "id": cursor.lastrowid}
    finally:
        conn.close()


@app.post("/api-keys/revoke")
def revoke_api_key(payload: APIKeyRevoke, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM api_keys WHERE id = ? AND user_id = ?", 
                          (payload.key_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="API key not found")
        conn.execute("DELETE FROM api_keys WHERE id = ?", (payload.key_id,))
        conn.commit()
        return {"message": "API key revoked"}
    finally:
        conn.close()


# ================================
# NOTES / DIARY
# ================================
@app.get("/notes")
def list_notes(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM user_notes WHERE user_id = ? ORDER BY pinned DESC, updated_at DESC",
            (user["id"],)
        ).fetchall()
        return {"notes": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/notes")
def create_note(payload: NoteCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    current_time = now_utc_str()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO user_notes (user_id, title, content, color, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], payload.title, payload.content, payload.color, current_time, current_time)
        )
        conn.commit()
        return {"message": "Note created", "id": cursor.lastrowid}
    finally:
        conn.close()


@app.put("/notes")
def update_note(payload: NoteUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_notes WHERE id = ? AND user_id = ?", 
                          (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Note not found")
        
        title = payload.title if payload.title is not None else row["title"]
        content = payload.content if payload.content is not None else row["content"]
        color = payload.color if payload.color is not None else row["color"]
        # Preserve existing pinned state if not explicitly passed
        if payload.pinned is not None:
            pinned = 1 if payload.pinned else 0
        else:
            pinned = row["pinned"]
        
        conn.execute(
            "UPDATE user_notes SET title=?, content=?, color=?, pinned=?, updated_at=? WHERE id=?",
            (title, content, color, pinned, now_utc_str(), payload.id)
        )
        conn.commit()
        return {"message": "Note updated"}
    finally:
        conn.close()


@app.delete("/notes")
def delete_note(payload: NoteDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM user_notes WHERE id = ? AND user_id = ?", 
                          (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Note not found")
        conn.execute("DELETE FROM user_notes WHERE id = ?", (payload.id,))
        conn.commit()
        return {"message": "Note deleted"}
    finally:
        conn.close()


# ================================
# BOOKMARKS
# ================================
@app.get("/bookmarks")
def list_bookmarks(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM user_bookmarks WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],)
        ).fetchall()
        return {"bookmarks": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/bookmarks")
def create_bookmark(payload: BookmarkCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    current_time = now_utc_str()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO user_bookmarks (user_id, title, url, description, category, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user["id"], payload.title, payload.url, payload.description, payload.category, current_time, current_time)
        )
        conn.commit()
        return {"message": "Bookmark created", "id": cursor.lastrowid}
    finally:
        conn.close()


@app.put("/bookmarks")
def update_bookmark(payload: BookmarkUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_bookmarks WHERE id = ? AND user_id = ?", 
                          (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bookmark not found")
        
        title = payload.title if payload.title is not None else row["title"]
        url = payload.url if payload.url is not None else row["url"]
        description = payload.description if payload.description is not None else row["description"]
        category = payload.category if payload.category is not None else row["category"]
        
        conn.execute(
            "UPDATE user_bookmarks SET title=?, url=?, description=?, category=?, updated_at=? WHERE id=?",
            (title, url, description, category, now_utc_str(), payload.id)
        )
        conn.commit()
        return {"message": "Bookmark updated"}
    finally:
        conn.close()


@app.delete("/bookmarks")
def delete_bookmark(payload: BookmarkDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM user_bookmarks WHERE id = ? AND user_id = ?", 
                          (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bookmark not found")
        conn.execute("DELETE FROM user_bookmarks WHERE id = ?", (payload.id,))
        conn.commit()
        return {"message": "Bookmark deleted"}
    finally:
        conn.close()


# ================================
# CATEGORIES / TAGS
# ================================
@app.get("/categories")
def list_categories(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM user_categories WHERE user_id = ? ORDER BY name",
            (user["id"],)
        ).fetchall()
        return {"categories": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/categories")
def create_category(payload: CategoryCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    current_time = now_utc_str()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO user_categories (user_id, name, icon, color, created_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], payload.name, payload.icon, payload.color, current_time)
        )
        conn.commit()
        return {"message": "Category created", "id": cursor.lastrowid}
    finally:
        conn.close()


@app.put("/categories")
def update_category(payload: CategoryUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_categories WHERE id = ? AND user_id = ?", 
                          (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Category not found")
        
        name = payload.name if payload.name is not None else row["name"]
        icon = payload.icon if payload.icon is not None else row["icon"]
        color = payload.color if payload.color is not None else row["color"]
        
        conn.execute(
            "UPDATE user_categories SET name=?, icon=?, color=? WHERE id=?",
            (name, icon, color, payload.id)
        )
        conn.commit()
        return {"message": "Category updated"}
    finally:
        conn.close()


@app.delete("/categories")
def delete_category(payload: CategoryDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM user_categories WHERE id = ? AND user_id = ?", 
                          (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Category not found")
        conn.execute("DELETE FROM user_categories WHERE id = ?", (payload.id,))
        conn.commit()
        return {"message": "Category deleted"}
    finally:
        conn.close()


# ================================
# CARDS (secure payment-card vault)
# ================================
def _detect_brand(number: str) -> str:
    n = re.sub(r"\D", "", number or "")
    if n.startswith("4"):
        return "Visa"
    if n[:2] in ("51", "52", "53", "54", "55") or 2221 <= int(n[:4] or "0") <= 2720:
        return "Mastercard"
    if n.startswith("34") or n.startswith("37"):
        return "Amex"
    if n.startswith("6"):
        return "Discover"
    return "Card"


@app.get("/cards")
def list_cards(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, label, holder, number, expiry, cvv, brand, note, color, created_at, updated_at "
            "FROM user_cards WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
        return {"cards": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/cards")
def create_card(payload: CardCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    number = re.sub(r"\D", "", payload.number)
    if len(number) < 12:
        raise HTTPException(status_code=400, detail="Enter a valid card number.")
    label = payload.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label is required.")
    brand = payload.brand or _detect_brand(number)
    current_time = now_utc_str()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO user_cards (user_id, label, holder, number, expiry, cvv, brand, note, color, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], label, payload.holder, number, payload.expiry, payload.cvv,
             brand, payload.note, payload.color or "#6366f1", current_time, current_time),
        )
        conn.commit()
        return {"message": "Card saved.", "id": cursor.lastrowid, "brand": brand}
    finally:
        conn.close()


@app.put("/cards")
def update_card(payload: CardUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_cards WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Card not found.")

        def pick(field, clean=lambda x: x):
            val = getattr(payload, field)
            return clean(val) if val is not None else row[field]

        number = pick("number", lambda v: re.sub(r"\D", "", v))
        brand = pick("brand") or _detect_brand(number)

        conn.execute(
            "UPDATE user_cards SET label=?, holder=?, number=?, expiry=?, cvv=?, brand=?, note=?, color=?, updated_at=? "
            "WHERE id=?",
            (pick("label", lambda v: v.strip()), pick("holder"), number, pick("expiry"), pick("cvv"),
             brand, pick("note"), pick("color", lambda v: v), now_utc_str(), payload.id),
        )
        conn.commit()
        return {"message": "Card updated.", "brand": brand}
    finally:
        conn.close()


@app.delete("/cards")
def delete_card(payload: CardDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM user_cards WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Card not found.")
        conn.execute("DELETE FROM user_cards WHERE id = ?", (payload.id,))
        conn.commit()
        return {"message": "Card deleted."}
    finally:
        conn.close()


# ================================
# TASKS (to-do)
# ================================
@app.get("/tasks")
def list_tasks(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, title, completed, priority, created_at, updated_at FROM user_tasks "
            "WHERE user_id = ? ORDER BY completed ASC, priority DESC, created_at DESC",
            (user["id"],),
        ).fetchall()
        return {"tasks": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/tasks")
def create_task(payload: TaskCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Task title is required.")
    current_time = now_utc_str()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO user_tasks (user_id, title, completed, priority, created_at, updated_at) VALUES (?, ?, 0, ?, ?, ?)",
            (user["id"], title, int(payload.priority or 0), current_time, current_time),
        )
        conn.commit()
        return {"message": "Task created.", "id": cursor.lastrowid}
    finally:
        conn.close()


@app.put("/tasks")
def update_task(payload: TaskUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_tasks WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found.")
        title = payload.title if payload.title is not None else row["title"]
        completed = 1 if payload.completed else 0 if payload.completed is not None else row["completed"]
        priority = int(payload.priority) if payload.priority is not None else row["priority"]
        conn.execute(
            "UPDATE user_tasks SET title=?, completed=?, priority=?, updated_at=? WHERE id=?",
            (title, completed, priority, now_utc_str(), payload.id),
        )
        conn.commit()
        return {"message": "Task updated."}
    finally:
        conn.close()


@app.delete("/tasks")
def delete_task(payload: TaskDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM user_tasks WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found.")
        conn.execute("DELETE FROM user_tasks WHERE id = ?", (payload.id,))
        conn.commit()
        return {"message": "Task deleted."}
    finally:
        conn.close()


# ================================
# IDENTITIES (passport / license / ID / address)
# ================================
@app.get("/identities")
def list_identities(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, type, label, fields, created_at, updated_at FROM user_identities "
            "WHERE user_id = ? ORDER BY created_at DESC", (user["id"],),
        ).fetchall()
        return {"identities": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/identities")
def create_identity(payload: IdentityCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    label = payload.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label is required.")
    ct = now_utc_str()
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "INSERT INTO user_identities (user_id, type, label, fields, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], payload.type, label, json.dumps(payload.fields or {}), ct, ct),
        )
        conn.commit()
        return {"message": "Identity saved.", "id": cur.lastrowid}
    finally:
        conn.close()


@app.put("/identities")
def update_identity(payload: IdentityUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_identities WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Identity not found.")
        typ = payload.type if payload.type is not None else row["type"]
        label = payload.label if payload.label is not None else row["label"]
        fields = json.dumps(payload.fields) if payload.fields is not None else row["fields"]
        conn.execute("UPDATE user_identities SET type=?, label=?, fields=?, updated_at=? WHERE id=?",
                     (typ, label, fields, now_utc_str(), payload.id))
        conn.commit()
        return {"message": "Identity updated."}
    finally:
        conn.close()


@app.delete("/identities")
def delete_identity(payload: GenericDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM user_identities WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Identity not found.")
        conn.execute("DELETE FROM user_identities WHERE id = ?", (payload.id,))
        conn.commit()
        return {"message": "Identity deleted."}
    finally:
        conn.close()


# ================================
# CONTACTS
# ================================
@app.get("/contacts")
def list_contacts(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, email, phone, company, address, note, created_at, updated_at "
            "FROM user_contacts WHERE user_id = ? ORDER BY LOWER(name), name", (user["id"],),
        ).fetchall()
        return {"contacts": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/contacts")
def create_contact(payload: ContactCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required.")
    ct = now_utc_str()
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "INSERT INTO user_contacts (user_id, name, email, phone, company, address, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], name, payload.email, payload.phone, payload.company, payload.address, payload.note, ct, ct),
        )
        conn.commit()
        return {"message": "Contact saved.", "id": cur.lastrowid}
    finally:
        conn.close()


@app.put("/contacts")
def update_contact(payload: ContactUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_contacts WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Contact not found.")
        def g(f):
            v = getattr(payload, f)
            return v if v is not None else row[f]
        conn.execute(
            "UPDATE user_contacts SET name=?, email=?, phone=?, company=?, address=?, note=?, updated_at=? WHERE id=?",
            (g("name"), g("email"), g("phone"), g("company"), g("address"), g("note"), now_utc_str(), payload.id),
        )
        conn.commit()
        return {"message": "Contact updated."}
    finally:
        conn.close()


@app.delete("/contacts")
def delete_contact(payload: GenericDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM user_contacts WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Contact not found.")
        conn.execute("DELETE FROM user_contacts WHERE id = ?", (payload.id,))
        conn.commit()
        return {"message": "Contact deleted."}
    finally:
        conn.close()


# ================================
# WIFI
# ================================
@app.get("/wifi")
def list_wifi(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, label, ssid, password, security, hidden, location, created_at, updated_at "
            "FROM user_wifi WHERE user_id = ? ORDER BY created_at DESC", (user["id"],),
        ).fetchall()
        return {"wifi": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/wifi")
def create_wifi(payload: WifiCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    label = payload.label.strip()
    ssid = payload.ssid.strip()
    if not label or not ssid:
        raise HTTPException(status_code=400, detail="Label and SSID are required.")
    ct = now_utc_str()
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "INSERT INTO user_wifi (user_id, label, ssid, password, security, hidden, location, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], label, ssid, payload.password, payload.security or "WPA",
             1 if payload.hidden else 0, payload.location, ct, ct),
        )
        conn.commit()
        return {"message": "WiFi saved.", "id": cur.lastrowid}
    finally:
        conn.close()


@app.put("/wifi")
def update_wifi(payload: WifiUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_wifi WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="WiFi not found.")
        label = payload.label if payload.label is not None else row["label"]
        ssid = payload.ssid if payload.ssid is not None else row["ssid"]
        password = payload.password if payload.password is not None else row["password"]
        security = payload.security if payload.security is not None else row["security"]
        hidden = 1 if payload.hidden else 0 if payload.hidden is not None else row["hidden"]
        location = payload.location if payload.location is not None else row["location"]
        conn.execute(
            "UPDATE user_wifi SET label=?, ssid=?, password=?, security=?, hidden=?, location=?, updated_at=? WHERE id=?",
            (label, ssid, password, security, hidden, location, now_utc_str(), payload.id),
        )
        conn.commit()
        return {"message": "WiFi updated."}
    finally:
        conn.close()


@app.delete("/wifi")
def delete_wifi(payload: GenericDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM user_wifi WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="WiFi not found.")
        conn.execute("DELETE FROM user_wifi WHERE id = ?", (payload.id,))
        conn.commit()
        return {"message": "WiFi deleted."}
    finally:
        conn.close()


# ================================
# SERVERS (SSH)
# ================================
@app.get("/servers")
def list_servers(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, host, port, username, password, keyfile, note, created_at, updated_at "
            "FROM user_servers WHERE user_id = ? ORDER BY created_at DESC", (user["id"],),
        ).fetchall()
        return {"servers": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/servers")
def create_server(payload: ServerCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    name = payload.name.strip()
    host = payload.host.strip()
    if not name or not host:
        raise HTTPException(status_code=400, detail="Name and host are required.")
    ct = now_utc_str()
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "INSERT INTO user_servers (user_id, name, host, port, username, password, keyfile, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], name, host, payload.port or 22, payload.username, payload.password, payload.keyfile, payload.note, ct, ct),
        )
        conn.commit()
        return {"message": "Server saved.", "id": cur.lastrowid}
    finally:
        conn.close()


@app.put("/servers")
def update_server(payload: ServerUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_servers WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Server not found.")
        def g(f):
            v = getattr(payload, f)
            return v if v is not None else row[f]
        port = payload.port if payload.port is not None else row["port"]
        conn.execute(
            "UPDATE user_servers SET name=?, host=?, port=?, username=?, password=?, keyfile=?, note=?, updated_at=? WHERE id=?",
            (g("name"), g("host"), port, g("username"), g("password"), g("keyfile"), g("note"), now_utc_str(), payload.id),
        )
        conn.commit()
        return {"message": "Server updated."}
    finally:
        conn.close()


@app.delete("/servers")
def delete_server(payload: GenericDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM user_servers WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Server not found.")
        conn.execute("DELETE FROM user_servers WHERE id = ?", (payload.id,))
        conn.commit()
        return {"message": "Server deleted."}
    finally:
        conn.close()


# ================================
# RECOVERY PHRASES
# ================================
@app.get("/recovery")
def list_recovery(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, label, words, word_count, created_at, updated_at "
            "FROM user_recovery WHERE user_id = ? ORDER BY created_at DESC", (user["id"],),
        ).fetchall()
        return {"recovery": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/recovery")
def create_recovery(payload: RecoveryCreate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    label = payload.label.strip()
    words = payload.words.strip()
    if not label or not words:
        raise HTTPException(status_code=400, detail="Label and words are required.")
    ct = now_utc_str()
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "INSERT INTO user_recovery (user_id, label, words, word_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], label, words, payload.word_count or 12, ct, ct),
        )
        conn.commit()
        return {"message": "Recovery phrase saved.", "id": cur.lastrowid}
    finally:
        conn.close()


@app.put("/recovery")
def update_recovery(payload: RecoveryUpdate, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM user_recovery WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Recovery phrase not found.")
        label = payload.label if payload.label is not None else row["label"]
        words = payload.words if payload.words is not None else row["words"]
        wc = payload.word_count if payload.word_count is not None else row["word_count"]
        conn.execute("UPDATE user_recovery SET label=?, words=?, word_count=?, updated_at=? WHERE id=?",
                     (label, words, wc, now_utc_str(), payload.id))
        conn.commit()
        return {"message": "Recovery phrase updated."}
    finally:
        conn.close()


@app.delete("/recovery")
def delete_recovery(payload: GenericDelete, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM user_recovery WHERE id = ? AND user_id = ?", (payload.id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Recovery phrase not found.")
        conn.execute("DELETE FROM user_recovery WHERE id = ?", (payload.id,))
        conn.commit()
        return {"message": "Recovery phrase deleted."}
    finally:
        conn.close()


# ================================
# WIFI GUEST SHARE LINKS — /w/{token} shows ONLY the join QR
# ================================
WIFI_SHARE_TTL_MINUTES = 60


def _wifi_share_page(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#faf9f6;
font-family:Georgia,'Times New Roman',serif;color:#141310}}
.card{{max-width:360px;margin:20px;padding:32px 26px;text-align:center;background:#fff;
border:1px solid #e4e0d5;border-top:3px solid #0f0e0c;box-shadow:0 18px 40px -26px rgba(20,19,16,.28)}}
.card img{{width:220px;height:220px;border:1px solid #e4e0d5;border-radius:8px}}
h1{{font-size:20px;margin:0 0 8px;font-weight:600}}
p{{font-size:13.5px;line-height:1.6;color:#5c584e;margin:6px 0}}
.tag{{display:inline-block;font-size:11px;letter-spacing:.08em;text-transform:uppercase;
color:#8a8474;border:1px solid #e4e0d5;border-radius:999px;padding:3px 10px;margin-top:12px}}
</style></head><body><div class="card">{body_html}</div></body></html>"""


@app.post("/wifi/{wifi_id}/share")
def share_wifi(wifi_id: int, request: Request, authorization: Optional[str] = Header(None)):
    """Create a one-hour / first-view guest link that shows only the join QR."""
    user, _ = get_current_user_and_session(authorization)
    rate_limit(f"{client_ip(request)}:wifi-share")
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM user_wifi WHERE id = ? AND user_id = ?", (wifi_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="WiFi not found.")
        w = dict(row)
        qr_payload = f"WIFI:T:{w.get('security') or 'WPA'};S:{w.get('ssid') or ''};P:{w.get('password') or ''};;"
        token = secrets.token_urlsafe(18)
        now = now_utc()
        # Clear out ancient links of this user while we're here (housekeeping).
        conn.execute("DELETE FROM wifi_shares WHERE user_id = ? AND expires_at < ?",
                     (user["id"], (now - timedelta(days=2)).isoformat()))
        conn.execute(
            "INSERT INTO wifi_shares (token, user_id, wifi_id, ssid, qr_payload, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token, user["id"], wifi_id, w.get("ssid") or "", qr_payload,
             now.isoformat(), (now + timedelta(minutes=WIFI_SHARE_TTL_MINUTES)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    base = str(request.base_url).rstrip("/")
    return {"url": f"{base}/w/{token}", "expires_in": WIFI_SHARE_TTL_MINUTES * 60,
            "note": "Shows only the QR. Dies after 1 hour or the first view."}


@app.get("/w/{token}")
def view_wifi_share(token: str):
    """Public guest page: join QR for a shared WiFi, no login required.

    First successful view burns the link (also dies at the 1-hour mark)."""
    from fastapi.responses import HTMLResponse

    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM wifi_shares WHERE token = ?", (token,)).fetchone()
        if not row:
            return HTMLResponse(_wifi_share_page("Link invalid",
                "<h1>This link is invalid</h1><p>It never existed or was revoked.</p>"), status_code=404)
        share = dict(row)
        now = now_utc_str()
        if share["expires_at"] < now:
            return HTMLResponse(_wifi_share_page("Link expired",
                "<h1>This link has expired</h1><p>WiFi guest links live for one hour only. Ask for a fresh one.</p>"), status_code=410)
        if share.get("viewed_at"):
            return HTMLResponse(_wifi_share_page("Link already used",
                "<h1>This link was already opened</h1><p>For your host's security each WiFi guest link works exactly once. Ask for a fresh one.</p>"), status_code=410)

        # Burn-on-read: mark viewed BEFORE serving so a refresh can't replay it.
        conn.execute("UPDATE wifi_shares SET viewed_at = ? WHERE token = ? AND viewed_at IS NULL", (now, token))
        conn.commit()
    finally:
        conn.close()

    # Render the QR inline (single request — the link is already one-time).
    qr = qrcode.QRCode(version=1, box_size=9, border=2)
    qr.add_data(share["qr_payload"])
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    qr_b64 = base64.b64encode(buffer.getvalue()).decode()
    import html as _html
    ssid = _html.escape(share["ssid"])
    return HTMLResponse(_wifi_share_page("WiFi Guest Access",
        f'<h1>WiFi Guest Access</h1><img src="data:image/png;base64,{qr_b64}" alt="WiFi QR code">'
        f"<p>Point your phone camera at the code to join <b>{ssid}</b> instantly — no typing needed.</p>"
        f'<span class="tag">one-time link · now used</span>'))


# ================================
# ADMIN PANEL (owner-only) + ABUSE INBOX
# ================================
import os as _os
ADMIN_EMAILS = {
    e.strip().lower()
    for e in _os.getenv("ADMIN_EMAILS", _os.getenv("ADMIN_EMAIL", "")).split(",")
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


def require_admin(authorization):
    """404 (not 403) for everyone else — the panel's existence stays private."""
    user, session = get_current_user_and_session(authorization)
    if not ("is_admin" in user.keys() and user["is_admin"]):
        raise HTTPException(status_code=404, detail="Not found.")
    return user, session


def _admin_audit(conn, admin_id: int, action: str, target: str = "", details: str = ""):
    conn.execute(
        "INSERT INTO admin_audit_log (admin_id, action, target, details, created_at) VALUES (?,?,?,?,?)",
        (admin_id, action, target, details, now_utc_str()),
    )


def _stop_user_jobs_best_effort(user_id: int):
    """On suspend: tell the runner to stop every job the account deployed."""
    try:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT runner_job_id FROM jobs WHERE user_id=? AND runner_job_id IS NOT NULL",
                (user_id,),
            ).fetchall()
            rids = [dict(r)["runner_job_id"] for r in rows if dict(r).get("runner_job_id")]
        finally:
            conn.close()
        for rid in rids:
            try:
                _runner_http("POST", f"/internal/jobs/{rid}/stop")
            except Exception:
                pass
    except Exception:
        pass


class AdminSuspend(BaseModel):
    user_id: int
    suspended: bool
    code: Optional[str] = None


class AbuseReportIn(BaseModel):
    url: str
    reason: Optional[str] = ""


@app.get("/admin/overview")
def admin_overview_route(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db_connection()
    try:
        users = dict(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone())["c"]
        suspended = dict(conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_suspended=1").fetchone())["c"]
        verified = dict(conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_verified=1").fetchone())["c"]
        jobs_total = dict(conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone())["c"]
        deployed = dict(conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE runner_job_id IS NOT NULL").fetchone())["c"]
        threshold = (now_utc() - timedelta(days=13)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c "
            "FROM users WHERE created_at >= ? GROUP BY day ORDER BY day",
            (threshold,),
        ).fetchall()
        series = [{"day": dict(r)["day"], "count": dict(r)["c"]} for r in rows]
        out = {
            "users": users, "suspended": suspended, "verified": verified,
            "jobs_total": jobs_total, "jobs_deployed": deployed,
            "jobs_max_per_user": MAX_JOBS_PER_USER,
            "capacity_max": users * MAX_JOBS_PER_USER,
            "signups_daily": series,
        }
    finally:
        conn.close()
    # Runner-wide picture (best-effort): how many slots the worker fleet has.
    try:
        resp = _runner_http("GET", "/internal/jobs")
        payload = resp.json() if resp is not None else None
        if payload:
            out["runner_capacity"] = payload.get("capacity")
            out["runner_running"] = sum(
                1 for j in (payload.get("jobs") or []) if j.get("status") == "running")
    except Exception:
        pass
    return out


@app.get("/admin/users")
def admin_users_route(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT u.id, u.username, u.email, u.is_verified, u.is_suspended, u.is_admin,
                   u.created_at,
                   (SELECT COUNT(*) FROM jobs j WHERE j.user_id = u.id) AS job_count
            FROM users u ORDER BY u.id DESC LIMIT 200
            """
        ).fetchall()
        return {"users": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/admin/jobs")
def admin_jobs_route(authorization: Optional[str] = Header(None)):
    """Job METADATA only (+ live status/uptime from the runner, best-effort) —
    never the code. Privacy stays intact."""
    require_admin(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT j.id, j.name, j.language, j.created_at, j.runner_job_id,
                   u.username AS owner, u.is_suspended AS owner_suspended
            FROM jobs j JOIN users u ON u.id = j.user_id
            ORDER BY j.id DESC LIMIT 300
            """
        ).fetchall()
        jobs = [dict(r) for r in rows]
    finally:
        conn.close()
    # Enrich with the runner's live view (status/uptime). Best-effort: if the
    # runner is asleep or unreachable the metadata list still answers.
    live = {}
    try:
        resp = _runner_http("GET", "/internal/jobs")
        payload = resp.json() if resp is not None else None
        for j in (payload or {}).get("jobs", []) or []:
            live[j.get("id")] = j
    except Exception:
        live = {}
    for row in jobs:
        info = live.get(row.get("runner_job_id")) or {}
        row["live_status"] = info.get("status")
        row["uptime_s"] = info.get("uptime_s")
        row["web_slug"] = info.get("web_slug")
    return {"jobs": jobs}


@app.post("/admin/users/set-suspended")
def admin_set_suspended(payload: AdminSuspend, authorization: Optional[str] = Header(None)):
    admin, _ = require_admin(authorization)
    if payload.user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="You cannot suspend your own account.")
    conn = get_db_connection()
    try:
        # Destructive actions demand the admin's own second factor, every time.
        row = conn.execute("SELECT is_enabled FROM user_2fa WHERE user_id=?", (admin["id"],)).fetchone()
        if not row or not row["is_enabled"]:
            raise HTTPException(
                status_code=409,
                detail="Enable 2FA on your admin account first — destructive actions require it.")
        _verify_second_factor(conn, admin["id"], payload.code or "")
        target = conn.execute("SELECT id, username FROM users WHERE id=?", (payload.user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found.")
        conn.execute(
            "UPDATE users SET is_suspended=?, updated_at=? WHERE id=?",
            (1 if payload.suspended else 0, now_utc_str(), payload.user_id),
        )
        if payload.suspended:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (payload.user_id,))
        _admin_audit(conn, admin["id"], "suspend" if payload.suspended else "reactivate", target["username"], "")
        conn.commit()
    finally:
        conn.close()
    if payload.suspended:
        _stop_user_jobs_best_effort(payload.user_id)
    return {"message": ("Account suspended. Their sessions are closed and jobs are stopping."
                        if payload.suspended else "Account reactivated.")}


@app.get("/admin/audit-log")
def admin_audit_route(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT a.id, a.action, a.target, a.details, a.created_at, u.username AS admin_name
            FROM admin_audit_log a LEFT JOIN users u ON u.id = a.admin_id
            ORDER BY a.id DESC LIMIT 100
            """
        ).fetchall()
        return {"audit": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/admin/abuse-reports")
def admin_abuse_route(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, url, reason, ip, status, created_at FROM abuse_reports ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"reports": [dict(r) for r in rows]}
    finally:
        conn.close()


# ---- public abuse inbox ----
@app.get("/report-abuse", include_in_schema=False)
def report_abuse_page():
    html = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Report abuse · Ahad Co</title>
<style>
body{margin:0;font-family:Inter,system-ui,sans-serif;background:#0B0C14;color:#F5F5FA;display:grid;place-items:center;min-height:100vh;padding:20px;box-sizing:border-box}
.card{max-width:460px;width:100%;background:#14152a;border:1px solid #262852;border-radius:18px;padding:28px}
h1{font-size:20px;margin:0 0 6px}p{color:#A0A0B2;font-size:13.5px;line-height:1.6;margin:0 0 16px}
input,textarea{width:100%;box-sizing:border-box;background:#0B0C14;border:1px solid #262852;color:#F5F5FA;border-radius:10px;padding:11px 13px;font-size:14px;margin-bottom:10px;font-family:inherit}
textarea{min-height:90px;resize:vertical}
button{width:100%;padding:12px;border:0;border-radius:10px;background:#7C6CF6;color:#fff;font-weight:600;font-size:14px;cursor:pointer}
button:disabled{opacity:.6;cursor:default}
#msg{margin-top:12px;font-size:13.5px;text-align:center;min-height:18px}
.ok{color:#2FD9C4}.err{color:#ff7b7b}
</style></head><body><div class="card">
<h1>Report abuse</h1>
<p>Saw a live page hosted on RunSpace doing something shady — phishing, spam, malware, crypto-mining? Tell us. The site owner reviews every report and can suspend the account.</p>
<input id="url" placeholder="https://… (the page or job URL)">
<textarea id="reason" placeholder="What's wrong with it? (optional, but helps)"></textarea>
<button id="btn">Send report</button>
<div id="msg"></div>
</div>
<script>
const urlInp = document.getElementById("url");
const q = new URLSearchParams(location.search).get("url");
if (q) urlInp.value = q;
document.getElementById("btn").addEventListener("click", async () => {
  const btn = document.getElementById("btn"), msg = document.getElementById("msg");
  if (!urlInp.value.trim()) { msg.className = "err"; msg.textContent = "Paste the URL first."; return; }
  btn.disabled = true; btn.textContent = "Sending…"; msg.textContent = "";
  try {
    const r = await fetch("/report-abuse", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: urlInp.value.trim(), reason: document.getElementById("reason").value }) });
    const j = await r.json().catch(() => ({}));
    if (r.ok) { msg.className = "ok"; msg.textContent = "Thanks — the report reached the site owner."; btn.textContent = "Sent ✓"; }
    else { msg.className = "err"; msg.textContent = j.detail || "Could not send. Try again."; btn.disabled = false; btn.textContent = "Send report"; }
  } catch (e) { msg.className = "err"; msg.textContent = "Network error — try again."; btn.disabled = false; btn.textContent = "Send report"; }
});
</script></body></html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)


@app.post("/report-abuse")
def report_abuse_submit(payload: AbuseReportIn, request: Request):
    rate_limit_custom(
        f"{client_ip(request)}:abuse", 3600, 5,
        "Too many reports from this network. Try again later.")
    url = (payload.url or "").strip()
    if not url or len(url) > 500:
        raise HTTPException(status_code=400, detail="A valid page URL is required.")
    reason = (payload.reason or "").strip()[:800]
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO abuse_reports (url, reason, ip, created_at) VALUES (?,?,?,?)",
            (url, reason, client_ip(request), now_utc_str()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"message": "Thanks — the report reached the site owner."}


# ================================
# QR CODE GENERATOR (for WiFi / anything)
# ================================
@app.get("/qr")
def make_qr(text: Optional[str] = None, q: Optional[str] = None, authorization: Optional[str] = Header(None)):
    # Frontend historically calls ?q=… ; older callers used ?text=… — accept both.
    _ = authorization
    payload = text or q or ""
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    qr_b64 = base64.b64encode(buffer.getvalue()).decode()
    return {"qr": f"data:image/png;base64,{qr_b64}"}


# ================================
# GLOBAL SEARCH
# ================================
@app.get("/search")
def global_search(q: str, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    term = "%" + (q or "").strip().lower() + "%"
    if not (q or "").strip():
        return {"results": []}
    conn = get_db_connection()
    out = []
    try:
        def run(kind, sql, cols):
            try:
                rows = conn.execute(sql, [user["id"]] + [term] * cols).fetchall()
                for r in rows:
                    out.append({"kind": kind, "id": r["id"], "title": r["title"], "sub": r["sub"]})
            except Exception as exc:  # noqa: BLE001
                logger.warning("search %s failed: %s", kind, exc)

        run("vault", "SELECT id, label AS title, value AS sub FROM vault_entries WHERE user_id = ? AND (LOWER(label) LIKE ? OR LOWER(value) LIKE ?)", 2)
        run("card", "SELECT id, label AS title, holder AS sub FROM user_cards WHERE user_id = ? AND (LOWER(label) LIKE ? OR LOWER(holder) LIKE ?)", 2)
        run("note", "SELECT id, title AS title, substr(content,1,60) AS sub FROM user_notes WHERE user_id = ? AND (LOWER(title) LIKE ? OR LOWER(content) LIKE ?)", 2)
        run("bookmark", "SELECT id, title AS title, url AS sub FROM user_bookmarks WHERE user_id = ? AND (LOWER(title) LIKE ? OR LOWER(url) LIKE ?)", 2)
        run("task", "SELECT id, title AS title, '' AS sub FROM user_tasks WHERE user_id = ? AND LOWER(title) LIKE ?", 1)
        run("contact", "SELECT id, name AS title, COALESCE(email,'') AS sub FROM user_contacts WHERE user_id = ? AND (LOWER(name) LIKE ? OR LOWER(COALESCE(email,'')) LIKE ? OR LOWER(COALESCE(phone,'')) LIKE ?)", 3)
        run("identity", "SELECT id, label AS title, type AS sub FROM user_identities WHERE user_id = ? AND (LOWER(label) LIKE ? OR LOWER(type) LIKE ?)", 2)
        run("wifi", "SELECT id, label AS title, ssid AS sub FROM user_wifi WHERE user_id = ? AND (LOWER(label) LIKE ? OR LOWER(ssid) LIKE ?)", 2)
        run("server", "SELECT id, name AS title, host AS sub FROM user_servers WHERE user_id = ? AND (LOWER(name) LIKE ? OR LOWER(host) LIKE ?)", 2)
        run("recovery", "SELECT id, label AS title, '' AS sub FROM user_recovery WHERE user_id = ? AND LOWER(label) LIKE ?", 1)
        run("snippet", "SELECT id, title AS title, language AS sub FROM snippets WHERE user_id = ? AND (LOWER(title) LIKE ? OR LOWER(content) LIKE ?)", 2)
        return {"results": out[:30]}
    finally:
        conn.close()


# ================================
# SNIPPETS (code / pastebin)
# ================================
@app.get("/snippets")
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


@app.post("/snippets")
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


@app.put("/snippets")
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


@app.delete("/snippets")
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


@app.post("/snippets/share")
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


@app.get("/s/{token}")
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
class ExecuteCodeRequest(BaseModel):
    language: str
    code: str
    stdin: Optional[str] = None


@app.post("/api/execute")
def execute_code(payload: ExecuteCodeRequest, request: Request, authorization: Optional[str] = Header(None)):
    """Proxy code execution to the separate runner service.

    User → this endpoint (auth required) → runner service (shared secret).
    The user NEVER sees the runner URL or secret — those stay server-side.
    """
    # 1) User must be logged in.
    get_current_user_and_session(authorization)

    # 2) Rate limit (per-user, stricter than general rate limiter).
    rate_limit(f"{client_ip(request)}:exec")

    # 3) Get runner config from env.
    runner_url = os.getenv("RUNNER_SERVICE_URL", "").strip().rstrip("/")
    runner_secret = os.getenv("RUNNER_SERVICE_SECRET", "").strip()

    if not runner_url or not runner_secret:
        raise HTTPException(
            status_code=503,
            detail="Code execution is not configured. Set RUNNER_SERVICE_URL and RUNNER_SERVICE_SECRET.",
        )

    # 4) Forward to runner service (server-to-server, secret never sent to browser).
    try:
        response = requests.post(
            runner_url + "/internal/execute",
            json={
                "language": payload.language,
                "code": payload.code,
                "stdin": payload.stdin or "",
            },
            headers={
                "Authorization": "Bearer " + runner_secret,
                "Content-Type": "application/json",
            },
            # execution time (MAX_EXECUTION_TIME_MS) + auto pip-install budget
            timeout=130,
        )
    except requests.ConnectionError:
        logger.error("Runner service unreachable at %s", runner_url)
        raise HTTPException(
            status_code=503,
            detail="Code execution service is temporarily unavailable. Please try again later.",
        )
    except requests.Timeout:
        raise HTTPException(
            status_code=504,
            detail="Code execution took too long. Please simplify your code.",
        )

    if response.status_code == 401:
        raise HTTPException(status_code=500, detail="Runner authentication failed. Contact admin.")
    if response.status_code == 403:
        raise HTTPException(status_code=500, detail="Runner secret mismatch. Contact admin.")
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail="Code execution service returned an error ({}).".format(response.status_code),
        )

    result = response.json()
    # Pass through stdout/stderr/exit_code/execution_time to the user.
    # The runner URL and secret are NEVER in this response.
    return result


# ================================
# ALWAYS-ON JOBS (24/7 background tasks — mini PythonAnywhere)
# ================================
# Job DEFINITIONS live in our DB (survive runner restarts); the PROCESSES run
# inside the runner service. Same secret, same proxy pattern as /api/execute.

MAX_JOBS_PER_USER = 3  # free tier guardrail


class JobCreateRequest(BaseModel):
    name: str
    language: str
    code: str


def _runner_http(method: str, path: str, json_body=None):
    """Call the runner service with the shared secret; map every transport
    failure to a clean HTTPException the frontend can display."""
    runner_url = os.getenv("RUNNER_SERVICE_URL", "").strip().rstrip("/")
    runner_secret = os.getenv("RUNNER_SERVICE_SECRET", "").strip()
    if not runner_url or not runner_secret:
        raise HTTPException(status_code=503, detail="Jobs are not configured. Set RUNNER_SERVICE_URL and RUNNER_SERVICE_SECRET.")
    try:
        return requests.request(
            method, runner_url + path,
            json=json_body,
            headers={"Authorization": "Bearer " + runner_secret},
            timeout=20,
        )
    except requests.ConnectionError:
        raise HTTPException(status_code=503, detail="Job service is waking up or unreachable — try again in 30 seconds.")
    except requests.Timeout:
        raise HTTPException(status_code=504, detail="Job service took too long to respond.")


def _job_web_fields(info: dict) -> dict:
    """Translate a runner job view into frontend web fields (public URL etc.).

    The proxy lives on the RUNNER service, so the public URL is simply the
    runner's own base URL + /live/{slug}/."""
    slug = (info or {}).get("web_slug")
    runner_url = os.getenv("RUNNER_SERVICE_URL", "").strip().rstrip("/")
    if not slug or not runner_url:
        return {}
    out = {
        "web": bool(info.get("web")),
        "web_public": bool(info.get("web_public", True)),
        "web_url": f"{runner_url}/live/{slug}/",
    }
    key = info.get("access_key")
    if not out["web_public"] and key:
        out["web_private_url"] = out["web_url"] + "?key=" + key
    return out


def _get_own_job(job_id: int, user: dict) -> dict:
    """Fetch a job row owned by this user or 404."""
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user["id"])).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found.")
    return dict(row)


@app.post("/api/jobs")
def create_job(payload: JobCreateRequest, request: Request, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    rate_limit(f"{client_ip(request)}:exec")

    name = payload.name.strip()[:60]
    if not name:
        raise HTTPException(status_code=422, detail="Give the job a name.")

    conn = get_db_connection()
    try:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM jobs WHERE user_id = ?", (user["id"],)).fetchone()
        if (dict(cnt)["c"] if cnt else 0) >= MAX_JOBS_PER_USER:
            raise HTTPException(status_code=429, detail=f"Max {MAX_JOBS_PER_USER} jobs per account (free tier).")
    except HTTPException:
        conn.close()
        raise
    conn.close()

    resp = _runner_http("POST", "/internal/jobs", {
        "language": payload.language, "code": payload.code,
        "name": f"u{user['id']}-{name}",
    })
    if resp.status_code == 201:
        info = resp.json()
    elif resp.status_code in (401, 403):
        raise HTTPException(status_code=500, detail="Runner secret mismatch. Contact admin.")
    else:
        try:
            detail = resp.json().get("detail", "Runner rejected the job.")
        except Exception:
            detail = "Runner rejected the job."
        raise HTTPException(status_code=resp.status_code if 400 <= resp.status_code < 500 else 502, detail=detail)

    now = now_utc_str()
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO jobs (user_id, name, language, code, runner_job_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], name, payload.language, payload.code, info["id"], now, now),
        )
        conn.commit()
        info["job_db_id"] = cursor.lastrowid
        info.update(_job_web_fields(info))  # web / web_url (web often False seconds after birth)
        return info
    finally:
        conn.close()


@app.get("/api/jobs")
def list_jobs(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM jobs WHERE user_id = ? ORDER BY id DESC", (user["id"],)).fetchall()
    finally:
        conn.close()

    # Live status from the runner — best effort (it may be asleep/restarted).
    live, runner_state = {}, "ok"
    try:
        resp = _runner_http("GET", "/internal/jobs")
        if resp.status_code == 200:
            live = {j["id"]: j for j in resp.json().get("jobs", [])}
        else:
            runner_state = "unreachable"
    except HTTPException as e:
        runner_state = e.detail

    jobs = []
    for r in rows:
        r = dict(r)
        rid = r.get("runner_job_id")
        if rid and rid in live:
            info = live[rid]
            r.update({"status": info["status"], "uptime_s": info.get("uptime_s", 0), "restarts": info.get("restarts", 0)})
            r.update(_job_web_fields(info))                # web / web_url / access
        else:
            r.update({"status": "offline", "uptime_s": 0, "restarts": 0})
        r.pop("code", None)  # never ship stored code back in list payloads
        jobs.append(r)
    return {"jobs": jobs, "runner": runner_state, "max_per_user": MAX_JOBS_PER_USER}


@app.get("/api/jobs/{job_id}/logs")
def job_logs(job_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if not rid:
        return {"status": "offline", "logs": "(never started)"}
    resp = _runner_http("GET", f"/internal/jobs/{rid}")
    if resp.status_code == 404:
        return {"status": "offline", "logs": "(runner restarted — press ▶ Restart to relaunch)"}
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not fetch logs from runner.")
    info = resp.json()
    return {"status": info.get("status"), "logs": info.get("logs", ""), "uptime_s": info.get("uptime_s", 0), "restarts": info.get("restarts", 0)}


@app.get("/api/jobs/{job_id}/logs/stream")
async def job_logs_stream(job_id: int, token: Optional[str] = None):
    """Server-Sent Events: push a job's logs to the dashboard in real time.

    EventSource can't send Authorization headers, so the session token comes
    as a ?token= query param; we validate it against the sessions table the
    same way get_current_user_and_session does.
    """
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    conn = get_db_connection()
    try:
        session_row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        if not session_row:
            raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (session_row["user_id"],)).fetchone()
        if not user_row:
            raise HTTPException(status_code=401, detail="Account not found.")
    finally:
        conn.close()

    row = _get_own_job(job_id, user_row)
    rid = row.get("runner_job_id")

    async def gen():
        last = None
        while True:
            info = None
            if rid:
                try:
                    resp = await asyncio.to_thread(_runner_http, "GET", f"/internal/jobs/{rid}")
                    if resp.status_code == 200:
                        info = resp.json()
                except Exception:
                    info = None
            payload = {
                "status": (info or {}).get("status", "offline"),
                "logs": (info or {}).get("logs", "(runner unreachable — retrying…)"),
                "uptime_s": (info or {}).get("uptime_s", 0),
                "restarts": (info or {}).get("restarts", 0),
            }
            blob = json.dumps(payload, ensure_ascii=False)
            if blob != last:
                last = blob
                yield f"data: {blob}\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if rid:
        resp = _runner_http("POST", f"/internal/jobs/{rid}/stop")
        if resp.status_code not in (200, 404):
            raise HTTPException(status_code=502, detail="Runner refused to stop the job.")
    return {"status": "stopped"}


@app.post("/api/jobs/{job_id}/restart")
def restart_job(job_id: int, request: Request, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    rate_limit(f"{client_ip(request)}:exec")
    row = _get_own_job(job_id, user)

    rid = row.get("runner_job_id")
    if rid:
        try:
            _runner_http("POST", f"/internal/jobs/{rid}/stop")
        except HTTPException:
            pass  # old copy may already be gone (runner restart) — fine

    resp = _runner_http("POST", "/internal/jobs", {
        "language": row["language"], "code": row["code"],
        "name": f"u{user['id']}-{row['name']}",
    })
    if resp.status_code == 201:
        info = resp.json()
    else:
        try:
            detail = resp.json().get("detail", "Runner rejected the job.")
        except Exception:
            detail = "Runner rejected the job."
        raise HTTPException(status_code=502, detail=detail)

    conn = get_db_connection()
    try:
        conn.execute("UPDATE jobs SET runner_job_id = ?, updated_at = ? WHERE id = ?", (info["id"], now_utc_str(), job_id))
        conn.commit()
    finally:
        conn.close()
    info["job_db_id"] = job_id
    info.update(_job_web_fields(info))
    return info


class JobAccessToggle(BaseModel):
    public: bool = True


@app.post("/api/jobs/{job_id}/access")
def toggle_job_access(job_id: int, payload: JobAccessToggle, authorization: Optional[str] = Header(None)):
    """Public ⇄ Private toggle for a job's live web URL."""
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if not rid:
        raise HTTPException(status_code=409, detail="Job is not up on the runner — press Restart first.")
    resp = _runner_http("POST", f"/internal/jobs/{rid}/access", {"public": payload.public})
    if resp.status_code == 404:
        raise HTTPException(status_code=409, detail="Runner restarted — press Restart to relaunch, then retry.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Runner refused the access change.")
    info = resp.json()
    info.update(_job_web_fields(info))
    info["job_db_id"] = job_id
    return info


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    row = _get_own_job(job_id, user)
    rid = row.get("runner_job_id")
    if rid:
        try:
            _runner_http("POST", f"/internal/jobs/{rid}/stop")
        except HTTPException:
            pass  # best effort
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()
    return {"message": "Job deleted."}


# ================================
# USER PREFERENCES
# ================================
@app.get("/preferences")
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


@app.put("/preferences")
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
# PASSWORD GENERATOR
# ================================
@app.post("/generate-password")
def generate_password(payload: PasswordGeneratorRequest, authorization: Optional[str] = Header(None)):
    import string
    
    chars = ""
    if payload.include_uppercase:
        chars += string.ascii_uppercase
    if payload.include_symbols:
        chars += "!@#$%^&*()_+-=[]{}|;:,.<>?"
    if payload.include_numbers:
        chars += string.digits
    chars += string.ascii_lowercase
    
    if not chars:
        chars = string.ascii_lowercase
    
    password = ''.join(secrets.choice(chars) for _ in range(payload.length))
    
    return {"password": password, "length": payload.length}


# ================================
# NOTIFICATIONS
# ================================
@app.get("/notifications")
def list_notifications(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
            (user["id"],)
        ).fetchall()
        return {"notifications": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/notifications/read")
def mark_notification_read(notification_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        conn.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", 
                    (notification_id, user["id"]))
        conn.commit()
        return {"message": "Notification marked as read"}
    finally:
        conn.close()


@app.post("/notifications/read-all")
def mark_all_read(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
        conn.commit()
        return {"message": "All notifications marked as read"}
    finally:
        conn.close()


@app.delete("/notifications")
def delete_notification(notification_id: int, authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM notifications WHERE id=? AND user_id=?", (notification_id, user["id"]))
        conn.commit()
        return {"message": "Notification deleted"}
    finally:
        conn.close()


# ================================
# ACTIVITY LOG
# ================================
@app.get("/activity-log")
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


@app.post("/activity-log")
def log_activity(payload: ActivityLogEntry, authorization: Optional[str] = Header(None), request: Request = None):
    user, _ = get_current_user_and_session(authorization)
    current_time = now_utc_str()
    ip = client_ip(request) if request else "unknown"
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO activity_log (user_id, action, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?)",
            (user["id"], payload.action, payload.details, ip, current_time)
        )
        conn.commit()
        return {"message": "Activity logged"}
    finally:
        conn.close()


# ================================
# STATS / DASHBOARD DATA
# ================================
@app.get("/stats")
def get_user_stats(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        notes_count = conn.execute(
            "SELECT COUNT(*) as count FROM user_notes WHERE user_id = ?", (user["id"],)
        ).fetchone()["count"]
        
        bookmarks_count = conn.execute(
            "SELECT COUNT(*) as count FROM user_bookmarks WHERE user_id = ?", (user["id"],)
        ).fetchone()["count"]
        
        vault_count = conn.execute(
            "SELECT COUNT(*) as count FROM vault_entries WHERE user_id = ?", (user["id"],)
        ).fetchone()["count"]
        
        sessions_count = conn.execute(
            "SELECT COUNT(*) as count FROM sessions WHERE user_id = ?", (user["id"],)
        ).fetchone()["count"]
        cards_count = conn.execute(
            "SELECT COUNT(*) as count FROM user_cards WHERE user_id = ?", (user["id"],)
        ).fetchone()["count"]
        tasks_count = conn.execute(
            "SELECT COUNT(*) as count FROM user_tasks WHERE user_id = ? AND completed = 0", (user["id"],)
        ).fetchone()["count"]

        return {
            "notes": notes_count,
            "bookmarks": bookmarks_count,
            "vault_entries": vault_count,
            "cards": cards_count,
            "open_tasks": tasks_count,
            "active_sessions": sessions_count,
            "member_since": user["created_at"]
        }
    finally:
        conn.close()


# ================================
# EXPORT / IMPORT DATA
# ================================
@app.get("/export-data")
def export_user_data(authorization: Optional[str] = Header(None)):
    user, _ = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        user_data = {
            "username": user["username"],
            "email": user["email"],
            "phone": user["phone"],
            "custom_code": user["custom_code"],
            "links": json.loads(user["links"]) if user["links"] else [],
            "created_at": user["created_at"]
        }

        def _rows(table):
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM {table} WHERE user_id = ?", (user["id"],)
            ).fetchall()]

        notes = _rows("user_notes")
        bookmarks = _rows("user_bookmarks")
        vault = _rows("vault_entries")
        cards = _rows("user_cards")
        tasks = _rows("user_tasks")
        identities = _rows("user_identities")
        contacts = _rows("user_contacts")
        wifi = _rows("user_wifi")
        servers = _rows("user_servers")
        recovery = _rows("user_recovery")
        try:
            snippets = _rows("snippets")
        except Exception:
            snippets = []

        return {
            "user": user_data,
            "notes": notes,
            "bookmarks": bookmarks,
            "vault": vault,
            "cards": cards,
            "tasks": tasks,
            "identities": identities,
            "contacts": contacts,
            "wifi": wifi,
            "servers": servers,
            "recovery": recovery,
            "snippets": snippets,
            "exported_at": now_utc_str()
        }
    finally:
        conn.close()
