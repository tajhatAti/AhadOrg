"""TOTP second-factor verification (6-digit app codes + single-use backup
codes). Shared by the 2FA endpoints, password change, and admin actions."""
import json

import pyotp
from fastapi import HTTPException

from routes.deps import now_utc_str


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
    # valid_window=1 (RFC 6238 guidance): accept the previous/current/next 30s
    # step so phones with a few seconds of clock drift — or codes typed right
    # at a step rollover — are not wrongly rejected.
    if not totp.verify(code, valid_window=1):
        raise HTTPException(status_code=400, detail="Incorrect authenticator code.")
    return row, len(backup_codes)
