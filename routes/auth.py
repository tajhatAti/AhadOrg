import os
import json
import secrets
from datetime import timedelta
from fastapi import APIRouter, HTTPException, Request, Header
from typing import Optional

from database import get_db_connection, IntegrityError as DBIntegrityError
from models import (
    UserSignup, UserVerify, UserLogin, ResendOTP,
    ForgotPasswordRequest, VerifyResetOTP, ResetPassword,
    AvailabilityCheck, TwoFactorSetup, TwoFactorVerify, TwoFactorConfirm
)
from utils import (
    rate_limit, rate_limit_custom, client_ip, now_utc, now_utc_str,
    validate_username, validate_password, generate_otp, hash_password,
    verify_password, parse_device
)
from services.email import send_email
from services.twofa import _verify_second_factor
from auth_deps import get_current_user_and_session

router = APIRouter(tags=["auth"])

@router.post("/logout")
def logout(authorization: Optional[str] = Header(None)):
    _, session_row = get_current_user_and_session(authorization)
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_row["id"],))
        conn.commit()
        return {"message": "Logged out."}
    finally: conn.close()

@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, request: Request):
    email = str(payload.email).strip().lower()
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id, username FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            otp, ct = generate_otp(), now_utc_str()
            conn.execute("UPDATE users SET reset_otp=?, reset_otp_created_at=?, reset_verified=0, updated_at=? WHERE id=?",
                         (otp, ct, ct, row["id"]))
            conn.commit()
            send_email(email, "Reset Password", otp, row["username"], "Reset")
        return {"message": "Sent if exists."}
    finally: conn.close()

@router.post("/verify-reset-otp")
def verify_reset_otp(payload: VerifyResetOTP):
    email = payload.email.strip().lower()
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row or row["reset_otp"] != payload.otp.strip(): raise HTTPException(400, detail="Invalid.")
        conn.execute("UPDATE users SET reset_verified=1, updated_at=? WHERE id=?", (now_utc_str(), row["id"]))
        conn.commit()
        return {"message": "Verified."}
    finally: conn.close()

@router.post("/reset-password")
def reset_password(payload: ResetPassword):
    email = payload.email.strip().lower()
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row or row["reset_verified"] != 1 or row["reset_otp"] != payload.otp.strip():
            raise HTTPException(400, detail="Not verified.")
        hashed = hash_password(validate_password(payload.new_password))
        conn.execute("UPDATE users SET password=?, reset_otp=NULL, reset_verified=0, updated_at=? WHERE id=?",
                     (hashed, now_utc_str(), row["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        conn.commit()
        return {"message": "Updated."}
    finally: conn.close()

OTP_EXPIRY_MINUTES = int(os.getenv("OTP_EXPIRY_MINUTES", "10"))
MAX_OTP_ATTEMPTS = int(os.getenv("MAX_OTP_ATTEMPTS", "5"))
SIGNUP_DAILY_MAX = 10

def create_session(user_id: int, request: Request) -> str:
    token = secrets.token_hex(32)
    device_info = parse_device(request.headers.get("user-agent", ""))
    ip = client_ip(request)
    current_time = now_utc_str()
    conn = get_db_connection()
    try:
        conn.execute("INSERT INTO sessions (user_id, token, device_info, ip_address, created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                     (user_id, token, device_info, ip, current_time, current_time))
        conn.commit()
    finally: conn.close()
    return token

def record_login_attempt(user_id: int, request: Request, success: bool, location: Optional[str] = None):
    try:
        conn = get_db_connection()
        try:
            conn.execute("INSERT INTO login_history (user_id, ip_address, device_info, location, success, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (user_id, client_ip(request), parse_device(request.headers.get("user-agent", "")), location, 1 if success else 0, now_utc_str()))
            conn.commit()
        finally: conn.close()
    except Exception: pass

@router.post("/check-availability")
def check_availability(payload: AvailabilityCheck, request: Request):
    rate_limit(f"{client_ip(request)}:avail")
    username = (payload.username or "").strip()
    email = (payload.email or "").strip().lower()
    username_taken = email_taken = False
    conn = get_db_connection()
    try:
        if username:
            row = conn.execute("SELECT is_verified FROM users WHERE username = ?", (username,)).fetchone()
            username_taken = bool(row and row["is_verified"] == 1)
        if email:
            row = conn.execute("SELECT is_verified FROM users WHERE email = ?", (email,)).fetchone()
            email_taken = bool(row and row["is_verified"] == 1)
    finally: conn.close()
    return {"username_taken": username_taken, "email_taken": email_taken}

@router.post("/signup")
def signup(user: UserSignup, request: Request):
    rate_limit(f"{client_ip(request)}:signup")
    rate_limit_custom(f"{client_ip(request)}:signup:daily", 86400, SIGNUP_DAILY_MAX, "Daily limit reached.")
    if not user.agreed_terms: raise HTTPException(400, detail="Terms not accepted.")
    
    username = validate_username(user.username)
    email = str(user.email).strip().lower()
    password = validate_password(user.password)
    otp, hashed_pw, ct = generate_otp(), hash_password(password), now_utc_str()
    
    conn = get_db_connection()
    try:
        existing = conn.execute("SELECT id, is_verified FROM users WHERE username = ? OR email = ?", (username, email)).fetchone()
        if existing:
            if existing["is_verified"] == 1: raise HTTPException(400, detail="Taken.")
            conn.execute("UPDATE users SET password=?, otp=?, otp_created_at=?, agreed_terms_at=?, updated_at=? WHERE id=?",
                         (hashed_pw, otp, ct, ct, ct, existing["id"]))
            conn.commit()
            send_email(email, "Verify Account", otp, username, "Verification")
            return {"message": "Resent.", "resent": True}
        
        conn.execute("INSERT INTO users (username, email, password, otp, otp_created_at, is_verified, created_at, updated_at, agreed_terms_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                     (username, email, hashed_pw, otp, ct, ct, ct, ct))
        conn.commit()
        send_email(email, "Verify Account", otp, username, "Verification")
        return {"message": "Created."}
    except DBIntegrityError: raise HTTPException(400, detail="Taken.")
    finally: conn.close()

@router.post("/verify")
def verify_otp(user: UserVerify, request: Request):
    username = validate_username(user.username)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row: raise HTTPException(404)
        if row["is_verified"] == 0:
            if not row["otp"] or now_utc() > datetime.fromisoformat(row["otp_created_at"]) + timedelta(minutes=OTP_EXPIRY_MINUTES):
                raise HTTPException(400, detail="Expired.")
            if row["otp"] != user.otp.strip():
                attempts = (row["otp_attempts"] or 0) + 1
                conn.execute("UPDATE users SET otp_attempts=?, updated_at=? WHERE id=?", (attempts, now_utc_str(), row["id"]))
                conn.commit()
                if attempts >= MAX_OTP_ATTEMPTS: raise HTTPException(400, detail="Too many attempts.")
                raise HTTPException(400, detail="Incorrect.")
            conn.execute("UPDATE users SET is_verified=1, otp=NULL, otp_attempts=0, updated_at=? WHERE id=?", (now_utc_str(), row["id"]))
            conn.commit()
        token = create_session(row["id"], request)
        return {"token": token, "username": row["username"]}
    finally: conn.close()

@router.post("/login")
def login(user: UserLogin, request: Request):
    ident = user.username.strip()
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ? OR email = ?", (ident, ident.lower())).fetchone()
        if not row or not verify_password(user.password, row["password"]):
            if row: record_login_attempt(row["id"], request, False)
            raise HTTPException(400, detail="Incorrect.")
        if row["is_verified"] == 0: return {"need_verify": True, "username": row["username"]}
        if row.get("is_suspended"): raise HTTPException(403, detail="Suspended.")
        record_login_attempt(row["id"], request, True)
        token = create_session(row["id"], request)
        return {"token": token, "username": row["username"]}
    finally: conn.close()
