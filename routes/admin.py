"""Admin console (owner-only, 404-stealth for everyone else) plus the
public abuse inbox. Destructive actions re-verify the admin's own 2FA."""
from typing import Optional, List

from fastapi import APIRouter, Header, HTTPException, Request

from routes.deps import *  # shared kernel (config, helpers, models)


from fastapi.responses import HTMLResponse

from services import runner_client
from services.runner_client import MAX_JOBS_PER_USER
from services.twofa import _verify_second_factor

router = APIRouter()


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
                runner_client._runner_http("POST", f"/internal/jobs/{rid}/stop")
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




@router.get("/admin/overview")
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
        resp = runner_client._runner_http("GET", "/internal/jobs")
        payload = resp.json() if resp is not None else None
        if payload:
            out["runner_capacity"] = payload.get("capacity")
            out["runner_running"] = sum(
                1 for j in (payload.get("jobs") or []) if j.get("status") == "running")
    except Exception:
        pass
    return out


@router.get("/admin/users")
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


@router.get("/admin/jobs")
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
        resp = runner_client._runner_http("GET", "/internal/jobs")
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


@router.post("/admin/users/set-suspended")
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


@router.get("/admin/audit-log")
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


@router.get("/admin/abuse-reports")
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


# ---- public abuse inbox ----
@router.get("/report-abuse", include_in_schema=False)
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


@router.post("/report-abuse")
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
# GLOBAL SEARCH (snippets + RunSpace apps)
# ================================
