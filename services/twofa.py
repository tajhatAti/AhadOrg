import json
from datetime import datetime, timezone
import pyotp
from fastapi import HTTPException

def now_utc_str() -> str:
    return datetime.now(timezone.utc).isoformat()

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
