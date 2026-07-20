"""Auth: availability, signup, email OTP, resend, login, logout,
password reset, and TOTP two-factor authentication."""
from typing import Optional, List

from fastapi import APIRouter, Header, HTTPException, Request

from routes.deps import *  # shared kernel (config, helpers, models)


import io
import base64

import pyotp
import qrcode

from services import email as email_service
from services.twofa import _verify_second_factor

router = APIRouter()


class AvailabilityCheck(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None


@router.post("/auth/check-availability")
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


@router.post("/signup")
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
            email_service.send_email(email, "Verify your Ahad Co account", otp, username, "Email Verification")
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

        email_service.send_email(email, "Verify your Ahad Co account", otp, username, "Email Verification")
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


@router.post("/resend-otp")
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

        email_service.send_email(row["email"], "Your new verification code", new_otp, username, "Email Verification")
        return {"message": "A new code has been sent to your email.", "expires_in": OTP_EXPIRY_MINUTES * 60}
    finally:
        conn.close()


@router.post("/verify")
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
@router.post("/login")
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


@router.post("/logout")
def logout(authorization: Optional[str] = Header(None)):
    # Idempotent: a double-click, a stale tab, or an already-expired session
    # must NEVER surface "Session expired" — the user asked to be logged out,
    # and being logged out is the end state either way.
    if not authorization:
        return {"message": "Logged out successfully."}
    token = authorization.replace("Bearer ", "").strip()
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id, user_id FROM sessions WHERE token = ?", (token,)).fetchone()
        if row:
            # activity-trail entry first — after the session is gone the
            # client can't post it (and shouldn't see a 401 for trying)
            conn.execute(
                "INSERT INTO activity_log (user_id, action, details, created_at) VALUES (?,?,?,?)",
                (row["user_id"], "info:Signed out", "Session ended", now_utc_str()))
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        return {"message": "Logged out successfully."}
    finally:
        conn.close()




@router.post("/forgot-password")
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

        email_service.send_email(email, "Reset your Ahad Co password", otp, row["username"], "Password Reset")
        return {"message": "If this email exists, a reset code has been sent.", "expires_in": OTP_EXPIRY_MINUTES * 60}
    finally:
        conn.close()


@router.post("/verify-reset-otp")
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


@router.post("/reset-password")
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


# ----------------------------
# Two-Factor Authentication (2FA)
# ----------------------------
@router.get("/2fa/status")
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


@router.post("/2fa/setup")
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


@router.post("/2fa/disable")
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


@router.post("/2fa/backup-codes")
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


@router.post("/2fa/verify-setup")
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
        # valid_window=1: tolerate a few seconds of phone clock drift (RFC 6238)
        if not totp.verify(payload.code, valid_window=1):
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


@router.post("/2fa/verify-login")
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
        
        # Verify TOTP (valid_window=1 for clock drift, RFC 6238)
        totp = pyotp.TOTP(row["secret"])
        if not totp.verify(payload.code, valid_window=1):
            raise HTTPException(status_code=400, detail="Invalid 2FA code")
        
        return {"message": "2FA verified successfully"}
    finally:
        conn.close()


# ----------------------------
# Login History
# ----------------------------
