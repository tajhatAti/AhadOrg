import re
import time
import secrets
import bcrypt
from datetime import datetime, timezone
from fastapi import HTTPException, Request
from collections import defaultdict

# --- Constants ---
USERNAME_REGEX = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")

# --- Rate Limiter ---
_attempts = defaultdict(list)

def rate_limit_custom(key: str, window_s: int, max_attempts: int, detail: str):
    now = time.time()
    window_start = now - window_s
    _attempts[key] = [t for t in _attempts[key] if t > window_start]
    if len(_attempts[key]) >= max_attempts:
        raise HTTPException(status_code=429, detail=detail)
    _attempts[key].append(now)

def rate_limit(key: str):
    rate_limit_custom(key, 300, 6, "Too many attempts. Please try again in a few minutes.")

def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"

# --- Formatting ---
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_utc_str() -> str:
    return now_utc().isoformat()

# --- Auth Helpers ---
def validate_username(username: str) -> str:
    username = username.strip()
    if not USERNAME_REGEX.fullmatch(username):
        raise HTTPException(status_code=400, detail="Username must be 3-30 characters.")
    return username

def validate_password(password: str) -> str:
    password = password.strip()
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    return password

def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False

def parse_device(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "android" in ua: os_name = "Android"
    elif "iphone" in ua or "ipad" in ua: os_name = "iOS"
    elif "windows" in ua: os_name = "Windows"
    elif "mac os" in ua: os_name = "macOS"
    elif "linux" in ua: os_name = "Linux"
    else: os_name = "Unknown OS"
    
    if "chrome" in ua and "edg" not in ua: browser = "Chrome"
    elif "firefox" in ua: browser = "Firefox"
    elif "safari" in ua and "chrome" not in ua: browser = "Safari"
    elif "edg" in ua: browser = "Edge"
    else: browser = "Browser"
    return f"{browser} on {os_name}"
