# Backend tests for the security batch:
# change-password (+2FA gating, session revocation), secured 2FA disable,
# backup-code regen, full export, profile password_changed_at.
import json, os, sys, tempfile

os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyotp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app import app, get_db_connection, hash_password, now_utc_str  # noqa: E402

c = TestClient(app)


def make_user(username, email, password, with_2fa=False):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (username, email, password, is_verified, role, created_at, updated_at) "
        "VALUES (?, ?, ?, 1, 'user', ?, ?)",
        (username, email, hash_password(password), now_utc_str(), now_utc_str()),
    )
    uid = cur.lastrowid
    conn.commit()
    conn.close()
    tok = None
    r = c.post("/login", json={"username": email, "email": email, "password": password})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    if with_2fa:
        h = {"Authorization": f"Bearer {tok}"}
        s = c.post("/2fa/setup", json={"enable": True}, headers=h)
        secret = s.json()["secret"]
        code = pyotp.TOTP(secret).now()
        v = c.post("/2fa/verify-setup", json={"code": code}, headers=h)
        assert v.status_code == 200, v.text
    return uid, tok


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("✓ " if cond else "✗ FAIL ") + name + (f" — {extra}" if extra else ""))


# ---------- 1) change-password: wrong current password rejected ----------
uid1, tok1 = make_user("pwuser1", "pw1@t.dev", "oldpass-1")
r = c.post("/account/change-password", json={
    "current_password": "WRONG", "new_password": "newpass-1"}, headers=auth(tok1))
check("change-password rejects wrong current password", r.status_code == 400 and "incorrect" in r.text)

# ---------- 2) change-password ok → OTHER sessions revoked, current survives ----------
r2 = c.post("/login", json={"username": "pwuser1", "email": "pw1@t.dev", "password": "oldpass-1"})
tok1b = r2.json()["token"]  # second device
r = c.post("/account/change-password", json={
    "current_password": "oldpass-1", "new_password": "newpass-1"}, headers=auth(tok1))
check("change-password succeeds", r.status_code == 200, r.text)
j = r.json()
check("response reports revoked sessions", j.get("other_sessions_revoked", 0) >= 1, str(j))
check("current session still valid", c.get("/profile", headers=auth(tok1)).status_code == 200)
check("other session kicked out", c.get("/profile", headers=auth(tok1b)).status_code == 401)
prof = c.get("/profile", headers=auth(tok1)).json()
check("profile exposes password_changed_at", prof.get("password_changed_at") is not None)

# ---------- 3) change-password with 2FA ON: code required + enforced ----------
uid2, tok2 = make_user("pwuser2", "pw2@t.dev", "oldpass-2", with_2fa=True)
r = c.post("/account/change-password", json={
    "current_password": "oldpass-2", "new_password": "newpass-2"}, headers=auth(tok2))
check("2FA account: change without code → 400", r.status_code == 400)
st = c.get("/2fa/status", headers=auth(tok2)).json()
secret_row = get_db_connection()
sec = secret_row.execute("SELECT secret FROM user_2fa WHERE user_id = ?", (uid2,)).fetchone()["secret"]
secret_row.close()
r = c.post("/account/change-password", json={
    "current_password": "oldpass-2", "new_password": "newpass-2",
    "totp_code": pyotp.TOTP(sec).now()}, headers=auth(tok2))
check("2FA account: change with valid code → 200", r.status_code == 200, r.text)

# ---------- 4) /2fa/setup enable:false must NOT disable ----------
r = c.post("/2fa/setup", json={"enable": False}, headers=auth(tok2))
check("one-click disable via /2fa/setup is rejected", r.status_code == 400)
st = c.get("/2fa/status", headers=auth(tok2)).json()
check("2FA still enabled after rejected disable", st["enabled"] is True)

# ---------- 5) /2fa/disable needs password + code ----------
r = c.post("/2fa/disable", json={"password": "WRONG", "code": "123456"}, headers=auth(tok2))
check("disable with wrong password → 400", r.status_code == 400)
r = c.post("/2fa/disable", json={"password": "oldpass-2", "code": "000000"}, headers=auth(tok2))
check("disable with wrong code → 400", r.status_code == 400)
r = c.post("/2fa/disable", json={"password": "pw3-would-be", "code": pyotp.TOTP(sec).now()}, headers=auth(tok2))
check("disable with BOTH wrong+right mixed → still 400", r.status_code == 400)
r = c.post("/2fa/disable", json={"password": "oldpass-2", "code": pyotp.TOTP(sec).now()}, headers=auth(tok2))
# password was changed to newpass-2 above
check("disable with stale old password → 400", r.status_code == 400)
r = c.post("/2fa/disable", json={"password": "newpass-2", "code": pyotp.TOTP(sec).now()}, headers=auth(tok2))
check("disable with correct password+TOTP → 200", r.status_code == 200, r.text)
check("2FA actually off", c.get("/2fa/status", headers=auth(tok2)).json()["enabled"] is False)

# ---------- 6) backup code regen — 10 new codes, old ones die ----------
uid3, tok3 = make_user("pwuser3", "pw3@t.dev", "oldpass-3", with_2fa=True)
cn = get_db_connection()
old_codes = json.loads(cn.execute("SELECT backup_codes FROM user_2fa WHERE user_id = ?", (uid3,)).fetchone()["backup_codes"])
sec3 = cn.execute("SELECT secret FROM user_2fa WHERE user_id = ?", (uid3,)).fetchone()["secret"]
cn.close()
r = c.post("/2fa/backup-codes", json={"password": "oldpass-3", "code": pyotp.TOTP(sec3).now()}, headers=auth(tok3))
check("backup-code regen returns 10 codes", r.status_code == 200 and len(r.json()["backup_codes"]) == 10)
new_codes = r.json()["backup_codes"]
check("new codes differ from old", set(new_codes) != set(old_codes))

# backup code can be USED to disable (single consumption)
r = c.post("/2fa/disable", json={"password": "oldpass-3", "code": new_codes[0]}, headers=auth(tok3))
check("fresh backup code can authorise disable", r.status_code == 200, r.text)

# ---------- 7) the vault is gone — no data can leak from dead features ----------
r = c.get("/export-data", headers=auth(tok3))
check("export-data route is dead (404 even for authed user)", r.status_code == 404)
cn = get_db_connection()
tables = {row[0] for row in cn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()} \
    if os.environ.get("DB_PATH") else set()
cn.close()
VAULT_TABLES = {"vault_entries", "user_notes", "user_bookmarks", "user_categories",
                "user_cards", "user_tasks", "user_identities", "user_contacts",
                "user_wifi", "wifi_shares", "user_servers", "user_recovery",
                "api_keys", "notifications"}
check("no vault tables remain in the live schema",
      not (VAULT_TABLES & tables), "leftovers: " + ",".join(VAULT_TABLES & tables))

# ---------- 8) single-use setup code still intact (security audit) ----------
uid4, tok4 = make_user("pwuser4", "pw4@t.dev", "oldpass-4")
s = c.post("/2fa/setup", json={"enable": True}, headers=auth(tok4)).json()
code = pyotp.TOTP(s["secret"]).now()
v = c.post("/2fa/verify-setup", json={"code": code}, headers=auth(tok4))
check("verify-setup returns the backup codes once", len(v.json().get("backup_codes", [])) == 10)

fails = [n for n, ok in results if not ok]
print("\n================ %d pass, %d fail ================" % (len(results) - len(fails), len(fails)))
sys.exit(1 if fails else 0)
