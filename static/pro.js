/* =========================================
   AHAD CO — RunSpace: free code hosting
   Code Studio · 24/7 jobs · published pages
   ========================================= */

const API = "";
let signupUsername = "";
let authToken = localStorage.getItem("ahad_token") || null;
let resendTimerInterval = null;
let currentTab = "overview";
let editingSnippetId = null;
let _livePreviewTimer = null;


/* ---------------- SCREEN NAV ---------------- */
function showScreen(id) {
  // Hide all top-level screens (new class names: nav/hero/section/foot/auth/dashboard)
  document.querySelectorAll(".nav, .hero, .section, .foot, .auth, .dashboard").forEach(el => {
    el.classList.add("hidden");
    el.style.display = "none";
  });

  if (id === "screen-landing") {
    const show = (sel) => {
      const el = document.querySelector(sel);
      if (el) { el.classList.remove("hidden"); el.style.display = ""; }
    };
    show(".nav");
    show("#screen-landing");
    document.querySelectorAll(".section").forEach(s => { s.classList.remove("hidden"); s.style.display = ""; });
    show(".foot");
    window.scrollTo({ top: 0 });
    return;
  }

  const target = document.getElementById(id);
  if (target) {
    target.classList.remove("hidden");
    target.style.display = "";
  }
}

/* ---------------- SIDEBAR DRAWER (mobile) ---------------- */
function openSideMenu() {
  const tabs = document.querySelector(".dash-tabs");
  const ov = document.getElementById("sideOverlay");
  if (tabs) tabs.classList.add("open");
  if (ov) ov.classList.remove("hidden");
}
function closeSideMenu() {
  const tabs = document.querySelector(".dash-tabs");
  const ov = document.getElementById("sideOverlay");
  if (tabs) tabs.classList.remove("open");
  if (ov) ov.classList.add("hidden");
}

/* Smooth-scroll to an in-page section (used by the marketing nav links). */
function scrollToId(id) {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: "smooth" });
}

function switchTab(tabId) {
  // "more" isn't a real tab — on mobile it opens the LEFT side drawer.
  if (tabId === "more") { openSideMenu(); return; }
  // Pseudo-tabs (e.g. the Activity launcher has no data-tab) must NOT
  // blank the dashboard — a missing/unknown tab target = no-op.
  if (!tabId || !document.getElementById(`tab-${tabId}`)) return;
  currentTab = tabId;
  // Leaving the code studio while the editor is fullscreen would trap the
  // overlay over the next section — always collapse it first.
  if (tabId !== "code") exitEditorFullscreen();
  document.querySelectorAll(".dash-tab").forEach(tab => {
    tab.classList.toggle("active", !!tab.dataset.tab && tab.dataset.tab === tabId);
  });
  document.querySelectorAll(".dash-tab-content").forEach(c => c.classList.remove("active"));
  const t = document.getElementById(`tab-${tabId}`);
  t.classList.add("active");
  // Sync mobile bottom-nav highlight (map extra tabs back to "more").
  const map = { profile: "more", admin: "more" };
  document.querySelectorAll(".bn-item").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === (map[tabId] || tabId));
  });
  // ⚡ Jobs tab: live-refresh statuses while it's open, stop polling otherwise.
  if (tabId === "jobs") { startJobPolling(); } else { stopJobPolling(); }
  // Admin console loads fresh every time it's opened (owner-only anyway).
  if (tabId === "admin" && typeof loadAdminPanel === "function") { loadAdminPanel(); }
  // ⚙️ Settings: keep the security panel truthful every time it opens.
  if (tabId === "profile") { refreshSecurityPanel(); loadSessionsList(); }
  if (tabId === "code") { initCodeMirror(); if (cmEditor) cmEditor.refresh(); }
  // 🔗 Every section is a REAL URL — back/forward + refresh + sharing work.
  if (!_routeNav) {
    const p = TAB_PATHS[tabId];
    if (p) { try { if (_clientPath() !== p) history.pushState({ tab: tabId }, "", p); } catch (e2) {} }
  }
}

/* ---------------- TOAST ---------------- */
/* ---------------- TOAST (single-slot, never stacks) ----------------
   One toast at a time, fixed top-center. Same message again → timer
   resets + a tiny pulse. Different message → content is REPLACED, not
   stacked. Auto-dismiss ~2.8s. Long text clamps to one line; click to
   expand the full detail. */
let _toastEl = null, _toastTimer = null, _toastKey = null;

function toast(message, type = "success") {
  const container = document.getElementById("toastContainer");
  if (!container) return;
  // While the server is down the banner already says everything — suppress
  // the wave of identical per-section "Could not load …" toasts.
  if (_netDown && typeof message === "string" && message.indexOf("Could not load") === 0) return;
  const key = type + "|" + message;
  clearTimeout(_toastTimer);

  if (!_toastEl || !document.body.contains(_toastEl)) {
    _toastEl = document.createElement("div");
    container.appendChild(_toastEl);
    _toastEl.addEventListener("click", () => _toastEl.classList.toggle("expanded"));
  }

  const sameAsShowing = (_toastKey === key && _toastEl.classList.contains("show"));
  _toastKey = key;
  _toastEl.className = `toast ${type} show`;
  const icons = { success: "check", error: "x", warning: "alert", info: "info" };
  _toastEl.innerHTML = `<span class="toast-ic">${ic(icons[type] || "info")}</span><span class="toast-msg"></span>`;
  _toastEl.querySelector(".toast-msg").textContent = message;

  if (sameAsShowing) {
    // refresh: brief pulse so repeat actions are visible, never multiplied
    _toastEl.classList.remove("pulse");
    void _toastEl.offsetWidth;
    _toastEl.classList.add("pulse");
  }

  _toastTimer = setTimeout(() => {
    _toastEl.classList.remove("show");
    _toastKey = null;
    setTimeout(() => { if (_toastEl && !_toastEl.classList.contains("show")) { _toastEl.remove(); _toastEl = null; } }, 250);
  }, 2800);
}

/* ---------------- ACTIVITY LOG ---------------- */
// A live, client-side feed of account/security events (kept in localStorage so
// it survives reloads). Mirrors what a user would expect to see on a site like
// GitHub's security log: "verification email sent", "wrong OTP", "sign-in
// successful", "username already taken", etc.
const ACTIVITY_KEY = "ahad_activity_log";
const ACTIVITY_MAX = 60;

function _loadActivity() {
  try { return JSON.parse(localStorage.getItem(ACTIVITY_KEY) || "[]"); }
  catch (e) { return []; }
}

function _saveActivity(list) {
  try { localStorage.setItem(ACTIVITY_KEY, JSON.stringify(list.slice(0, ACTIVITY_MAX))); }
  catch (e) {}
}

function logEvent(type, title, meta) {
  // type: success | error | info | warning
  const entry = {
    type: type || "info",
    title: title || "Event",
    meta: meta || "",
    ts: new Date().toISOString(),
  };
  const list = _loadActivity();
  list.unshift(entry);
  _saveActivity(list);
  renderActivity();
  // Mirror to the server-side activity log — that's the source of truth
  // that survives redeploys (localStorage is only a render cache).
  // Fire-and-forget: failures (dead token mid-logout, network blip) must
  // NEVER surface as an unhandled rejection or a scare-toast.
  if (authToken) {
    api("/activity-log", "POST", { action: `${type}:${title}`, details: meta || "" }, true).catch(() => {});
  }
}

function _activityIcon(t) {
  const name = ({ success: "check", error: "x", warning: "alert", info: "info" })[t];
  return name ? ic(name) : '<span class="dot-sq"></span>';
}

function _fmtTime(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch (e) { return ""; }
}

function renderActivity() {
  const list = _loadActivity();
  const box = document.getElementById("activityList");
  if (!box) return;
  if (!list.length) {
    box.innerHTML = `<div class="ap-empty">No activity yet. Events like sign-ups, OTP, and logins will appear here in real time.</div>`;
    return;
  }
  box.innerHTML = list.map(e => `
    <div class="ap-item">
      <div class="ap-ic ${e.type}">${_activityIcon(e.type)}</div>
      <div class="ap-body">
        <div class="ap-title">${escapeHtml(e.title)}</div>
        ${e.meta ? `<div class="ap-meta">${escapeHtml(e.meta)}</div>` : ""}
        <div class="ap-meta">${_fmtTime(e.ts)}</div>
      </div>
    </div>
  `).join("");
}

/* Pull the server's activity log and display THAT — fixes the mismatch
   where stale localStorage entries outlived the accounts on the server. */
async function syncActivityFromServer() {
  if (!authToken) return;
  try {
    const rows = await api("/activity-log", "GET", null, true);
    const arr = Array.isArray(rows) ? rows : (rows.activities || rows.items || []);
    const list = arr.map(r => {
      const a = r.action || "info:Event";
      const i = a.indexOf(":");
      let ts = r.created_at || "";
      if (ts && ts.indexOf("T") === -1) ts = ts.replace(" ", "T") + "Z";
      return { type: i > 0 ? a.slice(0, i) : "info", title: i > 0 ? a.slice(i + 1) : a, meta: r.details || "", ts };
    });
    _saveActivity(list);
    renderActivity();
  } catch (e) { /* keep whatever we have locally */ }
}

/* Wipe leftovers from a previous account before a fresh sign-in. */
function resetLocalActivity() {
  try { localStorage.removeItem(ACTIVITY_KEY); } catch (e) {}
}

function openActivityPanel() {
  const p = document.getElementById("activityPanel");
  const o = document.getElementById("activityOverlay");
  if (p) p.classList.add("open");
  if (o) o.classList.remove("hidden");
  renderActivity();
  syncActivityFromServer();
}

function closeActivityPanel() {
  const p = document.getElementById("activityPanel");
  const o = document.getElementById("activityOverlay");
  if (p) p.classList.remove("open");
  if (o) o.classList.add("hidden");
}

/* ---------------- LEGACY MORE-SHEET (removed) ----------------
   The old bottom "more sheet" duplicated the left drawer AND both could open
   at once (mixed/stale content bug). It's gone from the markup now — these
   shims keep any leftover reference harmless by routing to the real drawer. */
function openMoreSheet() { openSideMenu(); }
function closeMoreSheet() { closeSideMenu(); }

/* ---------------- API HELPER ---------------- */
async function api(path, method = "POST", body = null, auth = false) {
  const headers = { "Content-Type": "application/json" };
  if (auth && authToken) headers["Authorization"] = "Bearer " + authToken;

  let res;
  try {
    res = await fetch(API + path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : null
    });
  } catch (netErr) {
    // Server down / sleeping (free plan) / no internet — this is INFRA, not
    // a user error. Banner (not 11 racing toasts) + marked error kind so the
    // dashboard keeps the session and retries instead of logging you out.
    _serverDown();
    const e = new Error("Waking up your RunSpace... this can take up to a minute on the free tier");
    e.kind = "infra";
    throw e;
  }

  const data = await res.json().catch(() => ({}));

  if (res.status === 401 && auth) {
    localStorage.removeItem("ahad_token");
    localStorage.removeItem("ahad_auth_token");
    localStorage.removeItem("ahad_user");
    toast("Session expired. Please sign in again.", "error");
    setTimeout(() => window.location.reload(), 1500);
    throw new Error("Session expired.");
  }

  // Proxy/gateway failures while the service is cold (502/503/504 with NO
  // app-level JSON detail) are infra. A 503 carrying a FastAPI detail — e.g.
  // "Jobs are not configured" — is a NORMAL app error, shown as-is.
  if (res.status >= 502 && res.status <= 504 && !(data && data.detail)) {
    _serverDown();
    const e = new Error("Server is waking up (HTTP " + res.status + ") — please wait a moment…");
    e.kind = "infra";
    throw e;
  }
  _serverUp();  // any well-formed response = the backend is alive again

  if (!res.ok) throw new Error(data.detail || "Something went wrong");
  return data;
}

/* ---------------- SERVER-UP BANNER ----------------
   One sticky banner while the backend is unreachable, instead of a dozen
   racing "Could not load …" toasts. Heals itself on the next good response. */
let _netDown = false;
function _serverDown() {
  if (_netDown) return;
  _netDown = true;
  let b = document.getElementById("netBanner");
  if (!b) {
    b = document.createElement("div");
    b.id = "netBanner";
    b.className = "net-banner";
    document.body.appendChild(b);
    b.addEventListener("click", () => window.location.reload());
  }
  b.innerHTML = `${ic("refresh")}<span>Waking up your RunSpace... this can take up to a minute on the free tier. Please wait...</span>`;
  requestAnimationFrame(() => b.classList.add("show"));
}
function _serverUp() {
  if (!_netDown) return;
  _netDown = false;
  const b = document.getElementById("netBanner");
  if (b) b.classList.remove("show");
  toast("Back online ✓", "success");
}

function setLoading(btn, loading) {
  if (!btn) return;
  btn.classList.toggle("loading", loading);
  btn.disabled = loading;
}

/* ---------------- AUTH BUTTON MICRO-ANIMATIONS ----------------
   Continue buttons: press (CSS scale .97) → centered spinner (same
   size, no text) → ✓ for a short beat → the next screen fades in.
   On error the label returns with a short horizontal shake.
   Fintech-style: subtle, fast, no glitter. CSS lives in classic.css. */
function btnBusy(btn) {
  if (!btn) return;
  if (!btn.dataset.origHtml) btn.dataset.origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.classList.add("btn-busy");
  btn.innerHTML = '<span class="btn-spinner" aria-hidden="true"></span>';
}
function btnOk(btn, after) {
  if (!btn) { if (after) after(); return; }
  btn.classList.remove("btn-busy");
  btn.innerHTML = '<span class="btn-check" aria-hidden="true">✓</span>';
  setTimeout(() => {
    btn.disabled = false;
    if (btn.dataset.origHtml) { btn.innerHTML = btn.dataset.origHtml; delete btn.dataset.origHtml; }
    if (after) after();
  }, 420);
}
function btnFail(btn) {
  if (!btn) return;
  btn.classList.remove("btn-busy");
  btn.disabled = false;
  if (btn.dataset.origHtml) { btn.innerHTML = btn.dataset.origHtml; delete btn.dataset.origHtml; }
  btn.classList.add("btn-shake");
  setTimeout(() => btn.classList.remove("btn-shake"), 340);
}

/* ---------------- SIGN-UP AVAILABILITY (already-registered check) ------ */
function _clearTaken(el) {
  const f = el && el.closest(".field");
  if (!f) return;
  const n = f.querySelector(".field-taken");
  if (n) n.remove();
  el.classList.remove("taken");
}
function _showTaken(el) {
  if (!el || el.classList.contains("taken")) return;
  const f = el.closest(".field");
  if (!f) return;
  const note = document.createElement("div");
  note.className = "field-taken";
  note.innerHTML = 'This email/username is already registered. <a onclick="showScreen(\'screen-forgot1\')">Reset password →</a>';
  f.appendChild(note);
  el.classList.add("taken");
}
function _wireAvailability(inputId, field) {
  const el = document.getElementById(inputId);
  if (!el) return;
  el.addEventListener("blur", async () => {
    _clearTaken(el);
    const v = el.value.trim();
    if (v.length < 3) return;
    try {
      const r = await api("/auth/check-availability", "POST",
        field === "username" ? { username: v } : { email: v });
      if ((field === "username" && r.username_taken) || (field === "email" && r.email_taken)) _showTaken(el);
    } catch (e) { /* endpoint hiccup — the submit check will catch it */ }
  });
  el.addEventListener("input", () => _clearTaken(el));
}
_wireAvailability("su_username", "username");
_wireAvailability("su_email", "email");

function escapeHtml(text) {
  if (!text) return "";
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

/* ---------------- APP ICON SYSTEM (ONE outline family, Lucide-style) --------
   Inside the authenticated app there are NO colorful emoji icons — every
   glyph comes from this single stroke-icon set (24×24, stroke=currentColor,
   1.7 width, round caps). ic("lock") → inline SVG.
*/
const _IC_PATHS = {
  lock:        '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  moon:        '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z"/>',
  sun:         '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5 5l1.8 1.8M17.2 17.2 19 19M5 19l1.8-1.8M17.2 6.8 19 5"/>',
  phone:       '<rect x="7.5" y="3" width="9" height="18" rx="2"/><path d="M11 17.5h2"/>',
  link:        '<path d="M10.5 13.5a4.2 4.2 0 0 0 6 0l3-3a4.24 4.24 0 1 0-6-6l-1.5 1.5"/><path d="M13.5 10.5a4.2 4.2 0 0 0-6 0l-3 3a4.24 4.24 0 1 0 6 6l1.5-1.5"/>',
  server:      '<rect x="4" y="4" width="16" height="6.5" rx="1.5"/><rect x="4" y="13.5" width="16" height="6.5" rx="1.5"/><path d="M8 7.3h.01M8 16.8h.01M12.5 7.3H17M12.5 16.8H17"/>',
  file:        '<path d="M6 3.5h8.5L19 8v12.5H6z"/><path d="M14 3.5V8H19"/>',
  copy:        '<rect x="8.5" y="8.5" width="11" height="12" rx="2"/><path d="M15.5 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h2.5"/>',
  trash:       '<path d="M4.5 6.5h15M9.5 6.2V4.8A1.3 1.3 0 0 1 10.8 3.5h2.4a1.3 1.3 0 0 1 1.3 1.3v1.4"/><path d="M6.5 6.5 7.4 19a1.6 1.6 0 0 0 1.6 1.5h6a1.6 1.6 0 0 0 1.6-1.5l.9-12.5"/><path d="M10.2 10.5v6M13.8 10.5v6"/>',
  eye:         '<path d="M2.8 12S6.3 5.8 12 5.8 21.2 12 21.2 12 17.8 18.2 12 18.2 2.8 12 2.8 12z"/><circle cx="12" cy="12" r="2.8"/>',
  globe:       '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5a13.5 13.5 0 0 1 0 17M12 3.5a13.5 13.5 0 0 0 0 17"/>',
  shield:      '<path d="M12 3.5 5 6v5.5c0 4.5 3 7.6 7 9 4-1.4 7-4.5 7-9V6z"/><path d="M9.2 11.8l2 2 3.6-4"/>',
  alert:       '<path d="M12 4 2.8 19.5h18.4z"/><path d="M12 10v4.2M12 17.2v.1"/>',
  refresh:     '<path d="M20 5.5v5h-5"/><path d="M19.5 10.5a8 8 0 1 0 .7 4"/>',
  download:    '<path d="M12 4v11M7.5 11 12 15.5 16.5 11"/><path d="M4.5 19.5h15"/>',
  history:     '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2.2"/>',
  'log-out':   '<path d="M14.5 4.5H7a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h7.5"/><path d="M10.5 12h10M17 8.5l3.5 3.5-3.5 3.5"/>',
  rocket:      '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
  'folder-open':'<path d="M3.5 7A2.5 2.5 0 0 1 6 4.5h3.5L12 7h6a2.5 2.5 0 0 1 2.2 1.3"/><path d="M3.5 7h14.3a2 2 0 0 1 1.9 2.6l-1.8 6.2A2.5 2.5 0 0 1 15.5 18H5a2.5 2.5 0 0 1-2.5-2.5z"/>',
  zap:         '<path d="M13 2 5 13.5h5L8.8 22l8-11.5h-5L13 2z"/>',
  code:        '<path d="M9 8l-4.5 4L9 16M15 8l4.5 4L15 16"/>',
  minimize:    '<path d="M5.5 9.5h3a1.5 1.5 0 0 0 1.5-1.5v-3M15.5 9.5h3a1.5 1.5 0 0 1 1.5 1.5v-3M5.5 14.5h3A1.5 1.5 0 0 1 10 16v3M15.5 14.5h3a1.5 1.5 0 0 0-1.5 1.5v3"/>',
  play:        '<path d="M8 5.2v13.6c0 .9 1 1.5 1.8 1L20 13a1.2 1.2 0 0 0 0-2L9.8 4.3A1.2 1.2 0 0 0 8 5.2z"/>',
  check:       '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  square:      '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
  x:           '<path d="M6 6l12 12M18 6 6 18"/>',
  info:        '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 7.6v.1"/>',
  external:    '<path d="M14.5 4h5.5v5.5"/><path d="M20 4 11 13"/><path d="M19 13.5V17a2.5 2.5 0 0 1-2.5 2.5h-10A2.5 2.5 0 0 1 4 17V7a2.5 2.5 0 0 1 2.5-2.5H10"/>',
};

function ic(name, cls) {
  const p = _IC_PATHS[name] || _IC_PATHS.file;
  return `<svg class="ic${cls ? " " + cls : ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
}

/* ---------------- PASSWORD STRENGTH ---------------- */
function checkStrength(password, fillEl, labelEl) {
  let score = 0;
  if (password.length >= 6) score++;
  if (password.length >= 10) score++;
  if (/[A-Z]/.test(password)) score++;
  if (/[0-9]/.test(password)) score++;
  if (/[^A-Za-z0-9]/.test(password)) score++;
  let pct = (score / 5) * 100;
  let color = "#ef4444", label = "Weak";
  if (score >= 4) { color = "#10b981"; label = "Strong"; }
  else if (score >= 2) { color = "#f59e0b"; label = "Good"; }
  if (fillEl) { fillEl.style.width = pct + "%"; fillEl.style.background = color; }
  if (labelEl) labelEl.textContent = password ? label : "";
}

/* ---------------- OTP HELPERS ---------------- */
function setupOtpBoxes(containerId, onComplete) {
  const boxes = document.querySelectorAll(`#${containerId} input`);
  boxes.forEach((box, i) => {
    box.addEventListener("input", () => {
      box.value = box.value.replace(/[^0-9]/g, "");
      if (box.value && i < boxes.length - 1) boxes[i + 1].focus();
      if (getOtpValue(containerId).length === 6) onComplete();
    });
    box.addEventListener("keydown", e => {
      if (e.key === "Backspace" && !box.value && i > 0) boxes[i - 1].focus();
    });
    box.addEventListener("paste", e => {
      e.preventDefault();
      const pasted = (e.clipboardData.getData("text") || "").replace(/[^0-9]/g, "").slice(0, 6);
      pasted.split("").forEach((ch, idx) => { if (boxes[idx]) boxes[idx].value = ch; });
      if (pasted.length === 6) { boxes[5].focus(); onComplete(); }
    });
  });
}

function getOtpValue(containerId) {
  return Array.from(document.querySelectorAll(`#${containerId} input`)).map(i => i.value).join("");
}

function clearOtpBoxes(containerId) {
  document.querySelectorAll(`#${containerId} input`).forEach(i => i.value = "");
}

function startResendTimer(seconds = 45) {
  const timerEl = document.getElementById("resendTimer");
  const linkEl = document.getElementById("resendLink");
  if (!timerEl || !linkEl) return;
  linkEl.classList.add("disabled");
  clearInterval(resendTimerInterval);
  let remaining = seconds;
  resendTimerInterval = setInterval(() => {
    remaining--;
    const m = String(Math.floor(remaining / 60)).padStart(2, "0");
    const s = String(remaining % 60).padStart(2, "0");
    timerEl.textContent = `Resend in ${m}:${s}`;
    if (remaining <= 0) {
      clearInterval(resendTimerInterval);
      timerEl.textContent = "";
      linkEl.classList.remove("disabled");
    }
  }, 1000);
}

/* ---------------- OTP EXPIRY COUNTDOWN ("Expires in 09:42") ---------- */
let _otpExpireInterval = null;
function startOtpExpiry(seconds = 600, elId = "otpExpire") {
  const el = document.getElementById(elId);
  if (!el) return;
  clearInterval(_otpExpireInterval);
  let remaining = Math.max(0, parseInt(seconds, 10) || 600);
  const tick = () => {
    if (remaining <= 0) {
      clearInterval(_otpExpireInterval);
      el.textContent = "Code expired — resend a new one.";
      el.classList.add("expired");
      return;
    }
    const m = String(Math.floor(remaining / 60)).padStart(2, "0");
    const s = String(remaining % 60).padStart(2, "0");
    el.textContent = `Expires in ${m}:${s}`;
    el.classList.remove("expired");
    remaining--;
  };
  tick();
  _otpExpireInterval = setInterval(tick, 1000);
}
function stopOtpExpiry() { clearInterval(_otpExpireInterval); _otpExpireInterval = null; }

/* ---------------- OTP WRONG-CODE: red flash + shake + auto-clear ------ */
function otpShake(containerId) {
  const wrap = document.getElementById(containerId);
  if (!wrap) return;
  wrap.classList.add("otp-err");
  setTimeout(() => {
    wrap.classList.remove("otp-err");
    clearOtpBoxes(containerId);
    const first = wrap.querySelector("input");
    if (first) first.focus();
  }, 380);
}

/* ---------------- PASSWORD SHOW/HIDE EYES ----------------------------- */
function initPasswordEyes() {
  const EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/></svg>';
  const EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9.9 5.9A9.4 9.4 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a16.6 16.6 0 0 1-2.2 3.1M6.1 6.9A16.2 16.2 0 0 0 2.5 12S6 18.5 12 18.5a9.1 9.1 0 0 0 3.3-.6"/><path d="M9.5 9.7a3 3 0 0 0 4.6 4"/><path d="M3 3l18 18"/></svg>';
  ["su_password", "si_password", "fp_newpass", "fp_confirmpass"].forEach(id => {
    const input = document.getElementById(id);
    if (!input || input.dataset.eye) return;
    input.dataset.eye = "1";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pw-eye";
    btn.tabIndex = -1;  // keep Tab / mobile "next field" flowing username→email→password
    btn.setAttribute("aria-label", "Show or hide password");
    btn.innerHTML = EYE;
    btn.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.innerHTML = show ? EYE_OFF : EYE;
      btn.classList.toggle("on", show);
      input.focus();
    });
    // Wrap input in a relative holder so the eye centers exactly on the field.
    const wrap = document.createElement("span");
    wrap.className = "pw-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    wrap.appendChild(btn);
  });
}
initPasswordEyes();

/* ==================== SIGNUP ==================== */
document.addEventListener("submit", e => {
  if (e.target.id === "formSignup") handleSignup(e);
  else if (e.target.id === "formSignin") handleSignin(e);
  else if (e.target.id === "formForgot1") handleForgot1(e);
  else if (e.target.id === "formForgot3") handleForgot3(e);
});

async function handleSignup(e) {
  e.preventDefault();
  const btn = document.getElementById("btnSignup");
  const username = document.getElementById("su_username").value.trim();
  const email = document.getElementById("su_email").value.trim();
  const password = document.getElementById("su_password").value;
  if (username.length < 3) { toast("Username must be at least 3 characters", "error"); return; }
  const termsEl = document.getElementById("su_terms");
  if (termsEl && !termsEl.checked) {
    toast("Please accept the Terms of Use to continue.", "error");
    termsEl.focus();
    return;
  }
  // Early duplicate check — say "already registered" BEFORE the OTP dance.
  try {
    const av = await api("/auth/check-availability", "POST", { username, email });
    if (av.username_taken || av.email_taken) {
      _showTaken(document.getElementById(av.username_taken ? "su_username" : "su_email"));
      toast("This email/username is already registered.", "error");
      return;
    }
  } catch (e) { /* check endpoint hiccup — /signup will decide anyway */ }
  btnBusy(btn);
  try {
    const res = await api("/signup", "POST", { username, email, password, agreed_terms: true });
    signupUsername = username;
    localStorage.setItem("ahad_signup_username", username);
    localStorage.setItem("ahad_signup_email", email);
    clearOtpBoxes("otpBoxesSignup");
    document.getElementById("otpEmailNote").textContent = `A 6-digit code was sent to ${email}. It's valid for 10 minutes — you can switch apps to check your mail safely.`;
    logEvent("success", "Verification email sent", `Code sent to ${email}`);
    const toastMsg = res.resent ? "Welcome back — a fresh code was sent to your email." : "Verification code sent! Check your email.";
    btnOk(btn, () => {
      showScreen("screen-otp");
      startResendTimer(45);
      startOtpExpiry(res.expires_in || 600, "otpExpire");
      toast(toastMsg, "success");
    });
  } catch (err) {
    btnFail(btn);
    logEvent("error", "Sign-up failed", err.message);
    toast(err.message, "error");
  }
}

setupOtpBoxes("otpBoxesSignup", () => document.getElementById("btnVerify").click());
setupOtpBoxes("otpBoxesForgot", () => document.getElementById("btnForgot2").click());

document.getElementById("btnVerify").addEventListener("click", async () => {
  const btn = document.getElementById("btnVerify");
  const otp = getOtpValue("otpBoxesSignup");
  let username = signupUsername || localStorage.getItem("ahad_signup_username");
  if (!username) { toast("Username not found. Please sign up again.", "error"); showScreen("screen-signup"); return; }
  if (otp.length !== 6) { toast("Enter the 6-digit code", "error"); return; }
  btnBusy(btn);
  try {
    const data = await api("/verify", "POST", { username, otp });
    authToken = data.token;
    localStorage.setItem("ahad_token", authToken);
    localStorage.removeItem("ahad_signup_username");
    localStorage.removeItem("ahad_signup_email");
    signupUsername = "";
    // Clear the signup form + OTP so the entered email/username never lingers.
    clearOtpBoxes("otpBoxesSignup");
    document.getElementById("su_username").value = "";
    document.getElementById("su_email").value = "";
    document.getElementById("su_password").value = "";
    resetLocalActivity();   // drop any stale entries from earlier sessions
    logEvent("success", "Email verified", `Account confirmed: ${username}`);
    await loadDashboard();
    btnOk(btn, () => {
      stopOtpExpiry();
      showScreen("screen-dashboard");
      _consumeReturnTo();   // deep link pending? go there now
      toast("Email verified! Welcome!", "success");
      syncActivityFromServer();
    });
  } catch (err) {
    btnFail(btn);
    otpShake("otpBoxesSignup");
    logEvent("error", "Wrong / invalid OTP", err.message);
    toast(err.message, "error");
  }
  finally { setLoading(btn, false); }
});

document.getElementById("resendLink").addEventListener("click", async () => {
  const username = signupUsername || localStorage.getItem("ahad_signup_username");
  if (!username) { toast("Username not found.", "error"); showScreen("screen-signup"); return; }
  try {
    const r = await api("/resend-otp", "POST", { username });
    logEvent("info", "New code requested", `Resent OTP to ${username}`);
    toast("New code sent!", "success"); startResendTimer(45);
    startOtpExpiry(r.expires_in || 600, "otpExpire");
  }
  catch (err) { logEvent("error", "Resend failed", err.message); toast(err.message, "error"); }
});

/* ==================== SIGNIN ==================== */
async function handleSignin(e) {
  e.preventDefault();
  const btn = document.getElementById("btnSignin");
  const username = document.getElementById("si_username").value.trim();
  const password = document.getElementById("si_password").value;
  btnBusy(btn);
  try {
    const data = await api("/login", "POST", { username, password });
    // Backend routes unverified accounts to verification instead of erroring.
    if (data.need_verify) {
      signupUsername = data.username;
      localStorage.setItem("ahad_signup_username", data.username);
      clearOtpBoxes("otpBoxesSignup");
      document.getElementById("otpEmailNote").textContent =
        "Your email isn't verified yet. Enter the 6-digit code, or resend a new one below.";
      logEvent("warning", "Verification required", `Please verify ${data.username}`);
      btnOk(btn, () => {
        showScreen("screen-otp");
        startResendTimer(10);
        startOtpExpiry(data.expires_in || 600, "otpExpire");
        toast("Please verify your email to continue.", "warning");
      });
      return;
    }
    authToken = data.token;
    localStorage.setItem("ahad_token", authToken);
    // Clear the form so the entered username/password never lingers (e.g. via bfcache Back).
    document.getElementById("si_username").value = "";
    document.getElementById("si_password").value = "";
    resetLocalActivity();   // no leftovers from any previous account
    logEvent("success", "Sign-in successful", `Welcome back, ${data.username}`);
    await loadDashboard();
    btnOk(btn, () => {
      showScreen("screen-dashboard");
      _consumeReturnTo();   // deep link pending? go there now
      toast("Welcome back!", "success");
      syncActivityFromServer();
    });
  } catch (err) {
    btnFail(btn);
    logEvent("error", "Sign-in failed", err.message);
    toast(err.message, "error");
  }
}

/* ==================== FORGOT PASSWORD ==================== */
let forgotEmail = "";
let forgotOtp = "";

async function handleForgot1(e) {
  e.preventDefault();
  const btn = document.getElementById("btnForgot1");
  forgotEmail = document.getElementById("fp_email").value.trim();
  btnBusy(btn);
  try {
    const r = await api("/forgot-password", "POST", { email: forgotEmail });
    clearOtpBoxes("otpBoxesForgot");
    btnOk(btn, () => {
      showScreen("screen-forgot2");
      startOtpExpiry(r.expires_in || 600, "otpExpireReset");
      toast("If this email exists, a code has been sent", "success");
    });
  } catch (err) { btnFail(btn); toast(err.message, "error"); }
}

document.getElementById("btnForgot2").addEventListener("click", async () => {
  const btn = document.getElementById("btnForgot2");
  forgotOtp = getOtpValue("otpBoxesForgot");
  if (forgotOtp.length !== 6) { toast("Enter the 6-digit code", "error"); return; }
  btnBusy(btn);
  try {
    await api("/verify-reset-otp", "POST", { email: forgotEmail, otp: forgotOtp });
    btnOk(btn, () => {
      stopOtpExpiry();
      toast("Code verified!", "success");
      showScreen("screen-forgot3");
    });
  } catch (err) { btnFail(btn); otpShake("otpBoxesForgot"); toast(err.message, "error"); }
});

async function handleForgot3(e) {
  e.preventDefault();
  const btn = document.getElementById("btnForgot3");
  const p1 = document.getElementById("fp_newpass").value;
  const p2 = document.getElementById("fp_confirmpass").value;
  if (p1 !== p2) { toast("Passwords do not match", "error"); return; }
  btnBusy(btn);
  try {
    await api("/reset-password", "POST", { email: forgotEmail, otp: forgotOtp, new_password: p1 });
    btnOk(btn, () => {
      showScreen("screen-forgot-success");
      let count = 3;
      const cd = document.getElementById("successCountdown");
      const iv = setInterval(() => {
        count--; cd.textContent = count;
        if (count <= 0) { clearInterval(iv); showScreen("screen-signin"); }
      }, 1000);
    });
  } catch (err) { btnFail(btn); toast(err.message, "error"); }
}

/* ==================== DASHBOARD ==================== */
let _dashRetries = 0, _dashRetryTimer = null;
function _scheduleDashboardRetry() {
  if (_dashRetries >= 8) return;          // give up after ~2 min — banner stays up
  _dashRetries += 1;
  clearTimeout(_dashRetryTimer);
  _dashRetryTimer = setTimeout(() => { if (authToken) loadDashboard(); }, Math.min(4000 * _dashRetries, 20000));
}

async function loadDashboard() {
  // 1) Critical auth check — ONLY a profile/401 failure ends the session.
  try {
    const profile = await api("/profile", "GET", null, true);
    _dashRetries = 0;                     // healthy again — reset the backoff
    document.getElementById("dashUsername").textContent = profile.username;
    document.getElementById("dashUsername2").textContent = profile.username;
    document.getElementById("profileUsername").value = profile.username;
    document.getElementById("profileEmail").value = profile.email;
    document.getElementById("profilePhone").value = profile.phone || "";
    document.getElementById("profileCode").value = profile.custom_code || "";

    _lastProfile = profile;
    applyAdminVisibility(profile);
    refreshSecurityPanel();
    loadSessionsList();
  } catch (err) {
    console.error("Dashboard auth error:", err);
    // Infra failure (server asleep / 502-504 / no network): the session is
    // VALID — do NOT log the user out. Banner retries in the background.
    if (err && err.kind === "infra") { _scheduleDashboardRetry(); return; }
    toast("Session expired. Please login again.", "error");
    authToken = null;
    localStorage.removeItem("ahad_token");
    showScreen("screen-signin");
    return;
  }

  // 2) Section loads are NON-FATAL. If one section fails (network glitch,
  //    transient 500, etc.) the dashboard must still show so the user can
  //    use the buttons and retry. Don't collapse the whole UI on a section
  //    failure, and never clear the token here.
  try {
    await Promise.all([loadSnippets()]);
  } catch (err) {
    console.error("Section load error (non-fatal):", err);
  }
  await loadStats(); // updates the stat counters

  showScreen("screen-dashboard");
}

/* ==================== ADD-NEW FORM TOGGLE (ONE source of truth) ========
   Old pattern: a click listener on the button PLUS swapping .onclick between
   show/hide. On many mobile browsers BOTH handlers fire on the same tap —
   show() then hide() instantly — so the button looked dead after the first
   use and only a reload revived it.
   New pattern: exactly ONE listener per button; the open state is READ FROM
   THE DOM at toggle time (like an updater fn — no stale closure, no ghost
   handler). The form element itself is the only source of truth. */
function _clearIds(ids) { (ids || []).forEach(i => { const e = document.getElementById(i); if (e) e.value = ""; }); }

/* ---------------- SECTION LOAD FAILURE — NEVER a stuck spinner ----------------
   Every section's loader must ALWAYS end in a real state: data, empty, or a
   clear inline error WITH a retry button. A failed fetch used to leave the
   "Loading…" spinner running forever, which read as a frozen app. */
/* Professional loading state: Supabase-style shimmering skeleton bars
   (subtle pulse, no gimmicks) — used by every section while data flies. */
function _skel(rows = 3) {
  let h = '<div class="skel-wrap" aria-hidden="true">';
  for (let i = 0; i < rows; i++) {
    h += '<div class="skel-row"><span class="skel-ic"></span>' +
         '<span class="skel-lines"><i class="skel-bar w70"></i><i class="skel-bar w40"></i></span></div>';
  }
  return h + "</div>";
}

/* Delete feedback: row slides/fades away (220ms) before the list re-renders. */
function _rowOut(btnOrEl) {
  const row = btnOrEl && btnOrEl.closest &&
    btnOrEl.closest(".snippet-item, .job-card");
  if (!row) return Promise.resolve();
  row.classList.add("row-leave");
  return new Promise(r => setTimeout(r, 220));
}

function _loadErrorBox(list, what, retryFn, e) {
  if (!list) return;
  const infra = !!(e && e.kind === "infra");
  list.innerHTML = "";
  const box = document.createElement("div");
  box.className = "load-error";
  box.innerHTML =
    '<div class="load-error-ic">' + ic(infra ? "refresh" : "alert") + '</div>' +
    '<div class="load-error-tx"><b>' + (infra ? "Server waking up…" : ("Couldn\u2019t load " + what)) + '</b>' +
    '<span>' + (infra
      ? "The free-plan server is starting — this retries by itself (about a minute)."
      : escapeHtml((e && e.message) || "Something went wrong")) + '</span></div>';
  const btn = document.createElement("button");
  btn.className = "xbtn";
  btn.innerHTML = ic("refresh") + " Retry";
  btn.addEventListener("click", () => { retryFn(); });
  box.appendChild(btn);
  list.appendChild(box);
}

/* Null-safe smooth scroll — never crash if an element is not mounted yet. */
function _scrollToEl(el) { if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); }

const _RUNNABLE_LANGS = {"html":1, "css":1, "javascript":1, "js":1, "markdown":1, "md":1};

/* Show exactly ONE primary action per language type:
   markup/docs (html/css/js/md) → Preview; execution langs → Run. */
function syncRunPreviewButtons() {
  const lang = (document.getElementById("snippetLanguage") || {}).value || "html";
  const previewable = !!_RUNNABLE_LANGS[lang.toLowerCase()];
  const run = document.getElementById("btnRunCode");
  const prev = document.getElementById("btnRunSnippet");
  if (run) run.style.display = previewable ? "none" : "";
  if (prev) prev.style.display = previewable ? "" : "none";
}

/* Fullscreen editor — clean BINARY state, never a trap.
   · .cs-canvas.full → position:fixed inset:0 (TRUE 100% viewport)
   · a floating "Exit" button is injected INSIDE the canvas while it's full,
     so the exit control can never be hidden behind the canvas itself
   · Esc also exits (wired at boot) · switching tabs auto-exits (switchTab) */
function _ensureEdExitBtn() {
  let b = document.getElementById("edExitBtn");
  if (!b) {
    b = document.createElement("button");
    b.id = "edExitBtn";
    b.type = "button";
    b.className = "ed-exit";
    b.innerHTML = ic("minimize") + '<span>Exit fullscreen</span><kbd>Esc</kbd>';
    b.addEventListener("click", exitEditorFullscreen);
  }
  return b;
}
function enterEditorFullscreen() {
  const c = document.getElementById("ideSplit");
  if (!c || c.classList.contains("full")) return;
  c.classList.add("full");
  document.body.classList.add("ed-full");
  c.appendChild(_ensureEdExitBtn());
  const t = document.getElementById("btnEditorFull");
  if (t) t.classList.add("on");
}
function exitEditorFullscreen() {
  const c = document.getElementById("ideSplit");
  if (!c || !c.classList.contains("full")) return;
  c.classList.remove("full");
  document.body.classList.remove("ed-full");
  const ex = document.getElementById("edExitBtn");
  if (ex) ex.remove();
  const t = document.getElementById("btnEditorFull");
  if (t) t.classList.remove("on");
}
function toggleEditorFullscreen() {
  const c = document.getElementById("ideSplit");
  if (!c) return;
  if (c.classList.contains("full")) exitEditorFullscreen(); else enterEditorFullscreen();
}

let cmEditor = null;
function initCodeMirror() {
  const ta = document.getElementById("snippetContent");
  if (!ta || typeof CodeMirror === "undefined") return;
  if (cmEditor) return;
  cmEditor = CodeMirror.fromTextArea(ta, {
    lineNumbers: true,
    theme: "default",
    mode: "python",
    lineWrapping: true,
    indentUnit: 2,
    tabSize: 2,
    extraKeys: {
      "Ctrl-S": function(cm) { saveSnippet(); },
      "Cmd-S": function(cm) { saveSnippet(); },
      "Ctrl-Enter": function(cm) {
        const curLang = (document.getElementById("snippetLanguage").value || "").toLowerCase();
        if (_RUNNABLE_LANGS[curLang]) { runLivePreview(); } else { executeCode(); }
      },
      "Cmd-Enter": function(cm) {
        const curLang = (document.getElementById("snippetLanguage").value || "").toLowerCase();
        if (_RUNNABLE_LANGS[curLang]) { runLivePreview(); } else { executeCode(); }
      }
    }
  });
  cmEditor.on("change", (cm) => {
    ta.value = cm.getValue();
    updateEditorMeta();
    clearTimeout(_livePreviewTimer);
    const l = (document.getElementById("snippetLanguage").value || "").toLowerCase();
    if (_RUNNABLE_LANGS[l]) _livePreviewTimer = setTimeout(runLivePreview, 400);
  });
  updateCodeMirrorMode();
}

function updateCodeMirrorMode() {
  if (!cmEditor || typeof CodeMirror === "undefined") return;
  const lang = (document.getElementById("snippetLanguage").value || "text").toLowerCase();
  let mode = "text/plain";
  if (lang === "python" || lang === "python3") mode = "python";
  else if (lang === "javascript" || lang === "js") mode = "javascript";
  else if (lang === "html") mode = "htmlmixed";
  else if (lang === "css") mode = "css";
  else if (lang === "markdown" || lang === "md") mode = "markdown";
  else if (lang === "bash" || lang === "sh") mode = "shell";
  else if (lang === "c" || lang === "cpp" || lang === "c++") mode = "text/x-csrc";
  else if (lang === "java") mode = "text/x-java";
  else if (lang === "sql") mode = "sql";
  cmEditor.setOption("mode", mode);
}

function newSnippetDraft(quiet) {
  editingSnippetId = null;
  document.getElementById("snippetTitle").value = "";
  const ta = document.getElementById("snippetContent");
  ta.value = "";
  if (cmEditor) cmEditor.setValue("");
  document.getElementById("snippetLanguage").value = "html";
  updateCodeMirrorMode();
  updateEditorMeta();
  syncRunPreviewButtons();
  runLivePreview();
  if (cmEditor) cmEditor.focus(); else ta.focus();
  if (!quiet) toast("New snippet — write something and press Run", "info");
}

async function saveSnippet(keepEditor) {
  const title = document.getElementById("snippetTitle").value.trim();
  const language = document.getElementById("snippetLanguage").value;
  const content = cmEditor ? cmEditor.getValue() : document.getElementById("snippetContent").value;
  if (!content.trim()) { toast("Snippet content cannot be empty!", "error"); return; }
  try {
    let savedId = editingSnippetId;
    if (editingSnippetId) { await api("/snippets", "PUT", { id: editingSnippetId, title: title || "Untitled", language, content }, true); toast("Snippet updated! </>", "success"); }
    else {
      const r = await api("/snippets", "POST", { title: title || "Untitled snippet", language, content }, true);
      editingSnippetId = r.id; savedId = r.id; toast("Snippet saved! </>", "success");
    }
    logEvent("success", "Snippet saved", title || "Untitled");
    await loadSnippets();
    // Reset to a clean editor after save — no stale content on the next "new".
    if (!keepEditor) newSnippetDraft(true);
    return savedId;
  } catch (err) { toast(err.message, "error"); return null; }
}

function updateEditorMeta() {
  const ta = document.getElementById("snippetContent");
  const meta = document.getElementById("editorMeta");
  if (!ta || !meta) return;
  const val = cmEditor ? cmEditor.getValue() : (ta.value || "");
  const lines = val.split("\n").length;
  meta.textContent = lines + " lines · " + val.length + " chars";
  updateGutter();
}

/* Build the srcdoc for the live preview iframe, matching the share page. */
function _buildPreviewSrcdoc(body, lang) {
  body = body || "";
  lang = (lang || "text").toLowerCase();
  if (lang === "html") return body;
  if (lang === "css") {
    return '<!DOCTYPE html><html><head><meta charset="utf-8"><style>' + body + '</style></head>' +
      '<body style="font-family:system-ui,sans-serif;padding:24px;color:#111;background:#fff">' +
      '<h1>Heading</h1><p>Paragraph to show your <strong>CSS</strong>. <a href="#">A link</a>.</p>' +
      '<button>Button</button><ul><li>Item one</li><li>Item two</li></ul><input placeholder="Input"></body></html>';
  }
  if (lang === "markdown" || lang === "md") {
    return '<!DOCTYPE html><html><head><meta charset="utf-8"><script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"><\/script></head><body style="font-family:system-ui,sans-serif;padding:28px;max-width:720px;margin:0 auto;color:#1a1a2e;line-height:1.7;background:#fff"><div id="r"></div><script>document.getElementById("r").innerHTML = (window.marked ? marked.parse(decodeURIComponent(atob("' + btoaSafe(encodeURIComponent(body)) + '"))) : "");<\/script></body></html>';
  }
  if (lang === "javascript" || lang === "js") {
    var safe = body.split("<\/script>").join("<\\/script>");
    return '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="font-family:system-ui,sans-serif;padding:20px;color:#111;background:#fff"><scr' + 'ipt>(function(){var P=function(t,a){parent.postMessage({__ideConsole:true,type:t,msg:Array.prototype.map.call(a,function(x){try{return typeof x==="object"?JSON.stringify(x):String(x)}catch(e){return String(x)}}).join(" ")},\'*\')};["log","info","warn","error"].forEach(function(m){console[m]=function(){P(m==="error"?"err":(m==="warn"?"warn":"info"),arguments)}});window.onerror=function(m,s,l,c){P("err",[m+" (line "+l+")"])};try{\n' + safe + '\n}catch(e){P("err",[e.message])}})();<\/scr' + 'ipt></body></html>';
  }
  return "";
}

/* base64 of a UTF-8-safe string (for embedding into the markdown preview). */
function btoaSafe(str) {
  try { return btoa(str); } catch (e) { return btoa(unescape(encodeURIComponent(str))); }
}

function runLivePreview() {
  const lang = document.getElementById("snippetLanguage").value;
  const content = cmEditor ? cmEditor.getValue() : document.getElementById("snippetContent").value;
  const frame = document.getElementById("livePreview");
  const pmeta = document.getElementById("previewMeta");
  if (!frame) return;
  const runnable = !!_RUNNABLE_LANGS[(lang || "").toLowerCase()];
  const consoleBox = document.getElementById("ideConsole");
  const icBody = document.getElementById("icBody");
  if (consoleBox) consoleBox.style.display = "none";
  if (icBody) icBody.innerHTML = "";
  if (!runnable) {
    frame.srcdoc = '<!DOCTYPE html><html><body style="font-family:system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0;color:#94a3b8;background:#f8fafc;text-align:center;padding:20px"><div><div style="color:#94a3b8;width:34px;margin:0 auto">' + ic("eye") + '</div><p style="margin-top:10px;font-size:14px">Live preview supports<br><b>HTML, CSS, JavaScript &amp; Markdown</b>.</p><p style="font-size:12px;color:#cbd5e1;margin-top:6px">Other languages show in the share link as highlighted code.</p></div></body></html>';
    if (pmeta) pmeta.textContent = "no preview";
    return;
  }
  frame.srcdoc = _buildPreviewSrcdoc(content, lang);
  if (pmeta) pmeta.textContent = "live · " + lang;
}

/* Capture console messages from the JS preview iframe. */
window.addEventListener("message", function (ev) {
  var d = ev.data;
  if (!d || !d.__ideConsole) return;
  var box = document.getElementById("ideConsole");
  var body = document.getElementById("icBody");
  if (!box || !body) return;
  box.style.display = "flex";
  var ln = document.createElement("div");
  ln.className = "ln " + (d.type === "err" ? "err" : "info");
  ln.textContent = (d.type === "err" ? "✕ " : "› ") + d.msg;
  body.appendChild(ln);
  body.scrollTop = body.scrollHeight;
});

/* Simple JSON / HTML / CSS formatter (best-effort, client-side). */
function formatSnippet() {
  const ta = document.getElementById("snippetContent");
  const lang = document.getElementById("snippetLanguage").value;
  const orig = cmEditor ? cmEditor.getValue() : ta.value;
  let out = orig;
  try {
    if (lang === "json") { out = JSON.stringify(JSON.parse(orig), null, 2); }
    else if (lang === "html") { out = _formatMarkup(orig); }
    else if (lang === "css") { out = _formatCSS(orig); }
    else { out = orig.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim() + "\n"; }
    ta.value = out;
    if (cmEditor) cmEditor.setValue(out);
    updateEditorMeta();
    runLivePreview();
    toast("Formatted", "success");
  } catch (e) { toast("Could not format: " + e.message, "error"); }
}
function _formatMarkup(src) { return src.replace(/>\s*</g, ">\n<").replace(/^\s+|\s+$/g, "") + "\n"; }
function _formatCSS(src) { return src.replace(/\s*\{\s*/g, " {\n  ").replace(/;\s*/g, ";\n  ").replace(/\s*\}\s*/g, "\n}\n").replace(/\n\s*\n/g, "\n").trim() + "\n"; }

async function loadSnippets() {
  const list = document.getElementById("snippetsList");
  if (!list) return;
  list.innerHTML = _skel(3);
  try {
    const data = await api("/snippets", "GET", null, true);
    const snips = data.snippets || [];
    const count = document.getElementById("snippetCount");
    if (count) count.textContent = snips.length + " saved";
    if (!snips.length) { list.innerHTML = `<div class="empty-state"><div class="empty-icon">${ic("code","gold")}</div><p>No snippets saved yet</p><small>Write code above and press Save</small></div>`; return; }
    const origin = window.location.origin + window.location.pathname.replace(/index\.html$/, "").replace(/\/$/, "");
    list.innerHTML = snips.map(s => {
      const shared = s.share_token && s.is_public;
      const url = shared ? (origin + "/s/" + s.share_token) : "";
      const preview = (s.content || "").substring(0, 120);
      return '<div class="snippet-item">' +
        '<div class="snippet-top">' +
          '<div class="snippet-head"><span class="snippet-lang">' + escapeHtml(s.language || "text") + '</span><h4>' + escapeHtml(s.title) + '</h4></div>' +
          '<div class="snippet-actions">' +
            '<button class="xbtn" onclick="loadSnippetIntoEditor(' + s.id + ')">' + ic("folder-open") + ' Open</button>' +
            '<button class="xbtn" onclick="copySnippetCode(' + s.id + ')">' + ic("copy") + ' Copy</button>' +
            '<button class="xbtn" onclick="toggleSnippetShare(' + s.id + ', ' + (shared ? 1 : 0) + ', this)">' + (shared ? ic("globe") + " Unpublish" : ic("rocket") + " Publish") + '</button>' +
            '<button class="xbtn delete" onclick="deleteSnippet(' + s.id + ', this)" title="Delete">' + ic("trash") + '</button>' +
          '</div>' +
        '</div>' +
        '<pre class="snippet-code"><code>' + escapeHtml(preview) + ((s.content || "").length > 120 ? "\n…" : "") + '</code></pre>' +
        (shared ? '<div class="snippet-share-url"><span>Published at:</span><code>' + escapeHtml(url) + '</code><a class="xbtn" href="' + escapeHtml(url) + '" target="_blank" rel="noopener">Open page ↗</a></div>' : '<div class="snippet-share-url muted"><span>Not published — click Publish to deploy a standalone static page.</span></div>') +
      '</div>';
    }).join("");
  } catch (err) { if (!err || err.kind !== "infra") toast("Could not load snippets: " + err.message, "error"); _loadErrorBox(document.getElementById("snippetsList"), "snippets", loadSnippets, err); }
}

/* Load a saved snippet into the editor. */
async function loadSnippetIntoEditor(id) {
  try {
    const data = await api("/snippets", "GET", null, true);
    const s = (data.snippets || []).find(x => x.id === id);
    if (!s) return;
    editingSnippetId = id;
    document.getElementById("snippetTitle").value = s.title || "";
    document.getElementById("snippetLanguage").value = s.language || "text";
    const val = s.content || "";
    document.getElementById("snippetContent").value = val;
    if (cmEditor) cmEditor.setValue(val);
    updateCodeMirrorMode();
    updateEditorMeta();
    runLivePreview();
    toast("Loaded into editor", "info");
    _scrollToEl(document.querySelector("#tab-code .cs-canvas"));
  } catch (err) { toast(err.message, "error"); }
}

async function deleteSnippet(id, btn) {
  if (!confirm("Delete this snippet?")) return;
  try { await api("/snippets", "DELETE", { id }, true); toast("Snippet deleted!", "success"); if (editingSnippetId === id) newSnippetDraft(); await _rowOut(btn); await loadSnippets(); }
  catch (err) { toast(err.message, "error"); }
}

/* Share the snippet currently in the editor (creates if unsaved). */
async function shareCurrentSnippet() {
  const btn = document.getElementById("btnShareSnippet");
  const oldLabel = btn ? btn.textContent : "";
  if (btn && !btn.classList.contains("loading")) btn.textContent = "Publishing…";
  try {
    let id = editingSnippetId;
    if (!id) { id = await saveSnippet(true); }
    if (!id) return;
    await toggleSnippetShare(id, undefined, btn);
  } finally {
    if (btn) btn.textContent = oldLabel;
  }
}

// Visible published-link bar under the studio header: link + Open + Copy.
function showPubBar(url) {
  let bar = document.getElementById("pubBar");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "pubBar";
    bar.className = "pub-bar";
    const header = document.querySelector(".cs-header");
    if (header && header.parentNode) header.parentNode.insertBefore(bar, header.nextSibling);
    else return;
  }
  bar.style.display = "flex";
  bar.innerHTML =
    '<span class="pub-ic">' + ic("link") + '</span>' +
    '<a class="pub-link" href="' + escapeHtml(url) + '" target="_blank" rel="noopener">' + escapeHtml(url) + '</a>' +
    '<span class="pub-acts">' +
      '<button class="xbtn" id="pubOpen">Open ↗</button>' +
      '<button class="xbtn" id="pubCopy">Copy</button>' +
      '<button class="xbtn" id="pubClose">✕</button>' +
    '</span>';
  bar.querySelector("#pubOpen").addEventListener("click", () => window.open(url, "_blank", "noopener"));
  bar.querySelector("#pubCopy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(url); toast("Link copied", "success"); }
    catch (e) { toast("Copy failed", "error"); }
  });
  bar.querySelector("#pubClose").addEventListener("click", () => { bar.style.display = "none"; });
}

async function toggleSnippetShare(id, shared, btn) {
  let nowShared = !!shared;
  if (shared === undefined || shared === null || typeof shared === "object") {
    // Studio flow: no cached state (or the button got passed through) —
    // look it up once. The snippets-list row flow skips this extra GET.
    if (btn === undefined && shared && shared.tagName) btn = shared;
    try {
      const data = await api("/snippets", "GET", null, true);
      const s = (data.snippets || []).find(x => x.id === id);
      nowShared = !!(s && s.share_token && s.is_public);
    } catch (e) {}
  }
  setLoading(btn, true);
  try {
    const res = await api("/snippets/share", "POST", { id, share: !nowShared }, true);
    if (res.share && res.url) {
      // BUGFIX: origin only — never origin+pathname — or the broken
      // /code/s/<tok> link happens when publishing from the Code tab.
      const full = window.location.origin + res.url;
      showPubBar(full);   // visible, tappable link — never silently clipboard-only
      try { await navigator.clipboard.writeText(full); } catch (e) { /* bar already shows the link */ }
      toast("Published! Your page is live", "success");
      logEvent("success", "Snippet shared", "Standalone page published");
    } else {
      toast("Unpublished — the page is no longer live.", "info");
      logEvent("warning", "Snippet unshared", "");
    }
    await loadSnippets();
  } catch (err) { toast(err.message, "error"); }
  finally { setLoading(btn, false); }
}


async function copySnippetCode(id) {
  try {
    const data = await api("/snippets", "GET", null, true);
    const s = (data.snippets || []).find(x => x.id === id);
    if (!s) return;
    await navigator.clipboard.writeText(s.content || "");
    toast("Code copied!", "success");
  } catch (e) { toast("Copy failed", "error"); }
}

/* Draggable split divider between editor and preview. */
/* Preview panel toggle — slides open/closed from the right.
   For runnable languages (html/css/js/md): shows live preview.
   For other languages: runs real code execution (Python etc). */
function togglePreviewPanel(open) {
  const zone = document.getElementById("idePreview");
  if (!zone) return;
  if (open === undefined) open = !zone.classList.contains("open");
  if (open) {
    zone.classList.add("open");
    const lang = document.getElementById("snippetLanguage").value;
    if (_RUNNABLE_LANGS[(lang||"").toLowerCase()]) {
      runLivePreview();
    } else {
      executeCode(); // real execution for Python/C/etc
    }
  } else {
    zone.classList.remove("open");
  }
}

/* ================== INTEGRATED TERMINAL (real code execution) ==================
   Results render in the bottom terminal panel (#ahTerm). Shows stdout AND
   stderr, and — crucially — EVERY backend/config/timeout error is printed as
   a visible red block, so failures never disappear silently again. */

// --- tiny terminal helpers ---
function _termOpen() {
  const t = document.getElementById("ahTerm");
  if (t) t.classList.add("open"); // smooth max-height transition, no layout jerk
}
function _termClear() {
  const b = document.getElementById("ahTermBody");
  if (b) b.innerHTML = "";
}
function _termLine(text, cls) {
  const b = document.getElementById("ahTermBody");
  if (!b) return null;
  const ln = document.createElement("div");
  ln.className = "t-line " + (cls || "t-out");
  ln.textContent = text;
  b.appendChild(ln);
  b.scrollTop = b.scrollHeight;
  return ln;
}
function _termBadge(txt, ok) {
  const badge = document.getElementById("ahTermBadge");
  if (!badge) return;
  badge.textContent = txt || "";
  badge.className = "ah-term-badge" + (ok === true ? " ok" : ok === false ? " bad" : "");
}
function _termTitle(lang) {
  const t = document.getElementById("ahTermTitle");
  if (t) t.textContent = "user@ahad-co: ~ — " + (lang || "bash");
}

/* Execute code on the backend runner service — real output, not preview. */
async function executeCode() {
  const lang = document.getElementById("snippetLanguage").value;
  const code = cmEditor ? cmEditor.getValue() : document.getElementById("snippetContent").value;
  if (!code.trim()) { toast("Nothing to run!", "error"); return; }

  _termOpen(); _termClear(); _termTitle(lang); _termBadge("running…");

  // Prompt line — feels like a real shell
  _termLine("user@ahad-co:~$ run " + lang, "t-prompt");

  // Animated multi-stage waiting line — the runner scans imports, auto
  // installs libraries, then executes. Keep the user entertained meanwhile.
  const waitMsgs = [
    "code scan hocche — kon kon library lagbe…",
    "dorkari library auto-install hocche…",
    "code run hocche…",
  ];
  let waitIdx = 0;
  const spinner = _termLine(waitMsgs[0], "t-sys");
  const spinnerTimer = setInterval(() => {
    waitIdx = (waitIdx + 1) % waitMsgs.length;
    if (spinner && spinner.isConnected) spinner.textContent = waitMsgs[waitIdx];
  }, 2200);

  try {
    const result = await api("/api/execute", "POST", { language: lang, code: code }, true);
    clearInterval(spinnerTimer);
    if (spinner) spinner.remove();

    let shown = 0;
    // stdout — normal terminal text
    if (result.stdout) {
      result.stdout.replace(/\n+$/, "").split("\n").forEach(function(line) {
        _termLine(line, "t-out"); shown++;
      });
    }
    // stderr — red text (programs can legitimately write to BOTH streams)
    if (result.stderr) {
      result.stderr.replace(/\n+$/, "").split("\n").forEach(function(line) {
        _termLine(line, "t-err"); shown++;
      });
    }
    // runner-level error (compile failed, timeout, unsupported language...)
    if (result.error) {
      _termLine("✗ " + result.error, "t-err"); shown++;
    }
    if (!shown) _termLine("(no output)", "t-sys");

    const ok = result.success === true;
    const codeTxt = result.exit_code !== undefined ? result.exit_code : (ok ? 0 : "!");
    const ms = result.execution_time_ms !== undefined ? result.execution_time_ms : "?";
    _termLine("process exited · code " + codeTxt + " · " + ms + " ms", "t-foot " + (ok ? "ok" : "bad"));
    _termBadge(ok ? "exit 0" : ("exit " + codeTxt), ok);
  } catch (err) {
    clearInterval(spinnerTimer);
    if (spinner) spinner.remove();
    // HTTP-level failure (runner not configured, unreachable, timed out, ...)
    // — printed loudly instead of vanishing.
    _termLine("✗ " + (err && err.message ? err.message : "Request failed"), "t-err");
    _termLine("hint: check RUNNER_SERVICE_URL & RUNNER_SERVICE_SECRET on the main service", "t-sys");
    _termBadge("error", false);
  }
}

/* Update line-number gutter */
function updateGutter() {
  const ta = document.getElementById("snippetContent");
  const gutter = document.getElementById("csGutter");
  if (!ta || !gutter) return;
  const lines = ta.value.split("\n").length;
  let nums = "";
  for (let i = 1; i <= lines; i++) nums += i + "\n";
  gutter.textContent = nums;
}

/* Sync gutter scroll with textarea scroll */
function initGutterScroll() {
  const ta = document.getElementById("snippetContent");
  const gutter = document.getElementById("csGutter");
  if (!ta || !gutter) return;
  ta.addEventListener("scroll", () => { gutter.scrollTop = ta.scrollTop; });
}

/* initIdeDivider is now the close button for the preview panel */
function initIdeDivider() {
  const closeBtn = document.getElementById("ideDivider");
  if (closeBtn) closeBtn.addEventListener("click", () => togglePreviewPanel(false));
}

/* ==================== HELPERS ==================== */
async function copyText(t) {
  try { await navigator.clipboard.writeText(t || ""); toast("Copied!", "success"); }
  catch (e) { toast("Copy failed", "error"); }
}

/* ==================== COMMAND PALETTE / SEARCH ==================== */
const _KIND_META = {
  snippet: ["code", "code"], runspace: ["rocket", "jobs"],
};
let _cmdTimer = null, _cmdResults = [], _cmdIndex = -1;

function openCommandPalette() {
  document.getElementById("cmdOverlay").classList.remove("hidden");
  const inp = document.getElementById("cmdInput");
  inp.value = ""; inp.focus();
  document.getElementById("cmdResults").innerHTML = `<div class="cmd-empty">Start typing to search everything you've saved…</div>`;
  _cmdResults = [];
}
function closeCommandPalette() { document.getElementById("cmdOverlay").classList.add("hidden"); }

async function runCommandSearch(q) {
  if (!q.trim()) { document.getElementById("cmdResults").innerHTML = `<div class="cmd-empty">Start typing to search everything you've saved…</div>`; _cmdResults = []; return; }
  try {
    const data = await api("/search?q=" + encodeURIComponent(q), "GET", null, true);
    _cmdResults = data.results || [];
    _cmdIndex = -1;
    renderCommandResults();
  } catch (err) { document.getElementById("cmdResults").innerHTML = `<div class="cmd-empty">Search failed: ${escapeHtml(err.message)}</div>`; }
}
function renderCommandResults() {
  const box = document.getElementById("cmdResults");
  if (!_cmdResults.length) { box.innerHTML = `<div class="cmd-empty">No results</div>`; return; }
  box.innerHTML = _cmdResults.map((r, i) => {
    const meta = _KIND_META[r.kind] || ["file", "overview"];
    return `<div class="cmd-item ${i === _cmdIndex ? "sel" : ""}" data-i="${i}" onclick="openSearchResult(${i})">
      <span class="cmd-ic">${ic(meta[0], "premium")}</span>
      <div class="cmd-text"><div class="cmd-title">${escapeHtml(r.title)}</div>${r.sub ? `<div class="cmd-sub">${escapeHtml(r.sub)}</div>` : ""}</div>
      <span class="cmd-kind">${r.kind}</span>
    </div>`;
  }).join("");
}
function openSearchResult(i) {
  const r = _cmdResults[i];
  if (!r) return;
  const tab = (_KIND_META[r.kind] || ["", "overview"])[1];
  closeCommandPalette();
  switchTab(tab);
}

/* ==================== THEME ==================== */
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const slot = document.getElementById("themeIcon");
  if (slot) slot.innerHTML = ic(theme === "light" ? "sun" : "moon");
  try { localStorage.setItem("ahad_theme", theme); } catch (e) {}
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  applyTheme(cur === "light" ? "dark" : "light");
}
(function initTheme() {
  try { applyTheme(localStorage.getItem("ahad_theme") || "light"); } catch (e) { applyTheme("light"); }
})();

/* ==================== PROFILE ==================== */
async function saveProfile() {
  const phone = document.getElementById("profilePhone").value.trim();
  const custom_code = document.getElementById("profileCode").value.trim();
  try { await api("/profile/update", "POST", { phone, custom_code }, true); toast("Profile saved!", "success"); }
  catch (err) { toast(err.message, "error"); }
}

/* ==================== MODAL SHELL (shared) ==================== */
function openModal(id) {
  const m = document.getElementById(id);
  if (!m) return;
  m.classList.remove("hidden");
  m.classList.add("open");
}
function closeModal(elOrId) {
  const m = typeof elOrId === "string" ? document.getElementById(elOrId) : elOrId;
  if (!m) return;
  m.classList.remove("open");
  setTimeout(() => m.classList.add("hidden"), 160);
}
// overlay click + [data-close] buttons close any ah-modal; Esc closes the top one
document.addEventListener("click", e => {
  if (e.target.classList && e.target.classList.contains("ah-modal")) closeModal(e.target);
  const closer = e.target.closest && e.target.closest("[data-close]");
  if (closer) { const m = closer.closest(".ah-modal"); if (m) closeModal(m); }
});
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (document.querySelector(".cs-canvas.full")) return; // fullscreen owns Esc
  const open = document.querySelector(".ah-modal.open");
  if (open) closeModal(open);
});

async function deleteAccount() {
  const c1 = confirm("Are you sure you want to DELETE your account permanently? This CANNOT be undone!");
  if (!c1) return;
  const password = prompt("Enter your password to confirm deletion:");
  if (!password) return;
  try {
    await api("/account/delete", "POST", { password }, true);
    toast("Account deleted. Goodbye.", "success");
    authToken = null;
    localStorage.removeItem("ahad_token");
    setTimeout(() => window.location.reload(), 2000);
  } catch (err) { toast(err.message, "error"); }
}

/* ==================== 2FA — GUIDED SETUP WIZARD + MANAGE ==================== */
let _tfa = { secret: "", qr: "", codes: [] };

function _tfaSetStep(n) {
  document.querySelectorAll("#tfaSteps .ah-dot").forEach(d => {
    d.classList.toggle("on", +d.dataset.s <= n);
  });
}
function _tfaStepsVisible(v) { document.getElementById("tfaSteps").style.display = v ? "" : "none"; }
function _tfaBody(html) { document.getElementById("tfaBody").innerHTML = html; }

async function manage2FA() {
  try {
    const st = await api("/2fa/status", "GET", null, true);
    openModal("tfaModal");
    if (st.enabled) _tfaShowManage(st); else _tfaShowStep1();
  } catch (err) { toast(err.message, "error"); }
}

/* ---- STEP 1: what 2FA is ---- */
function _tfaShowStep1() {
  _tfaStepsVisible(true); _tfaSetStep(1);
  document.getElementById("tfaTitle").textContent = "Set up two-factor authentication";
  _tfaBody(`
    <div class="tfa-hero">${ic("shield")}</div>
    <p class="tfa-p">Two-factor authentication asks for a <b>6-digit code</b> from an authenticator app
    (Google Authenticator, Authy…) every time you sign in — so a stolen password alone can't get into your account.</p>
    <button class="btn-primary block" onclick="_tfaStartSetup()">Get started</button>`);
}

/* ---- STEP 2: QR + manual key ---- */
async function _tfaStartSetup() {
  try {
    const data = await api("/2fa/setup", "POST", { enable: true }, true);
    _tfa = { secret: data.secret, qr: data.qr_code, codes: [] };
    _tfaSetStep(2);
    _tfaBody(`
      <p class="tfa-p"><b>1.</b> Scan this QR code with your authenticator app:</p>
      <div class="tfa-qr"><img src="${data.qr_code}" alt="Authenticator QR code"></div>
      <p class="tfa-p"><b>Can't scan?</b> Enter this key in the app by hand:</p>
      <div class="tfa-manual"><code>${data.secret}</code>
        <button class="xbtn" onclick="navigator.clipboard.writeText('${data.secret}').then(()=>toast('Secret key copied','success'))">${ic("copy")} Copy</button>
      </div>
      <button class="btn-primary block" onclick="_tfaShowVerify()">Next — verify code</button>`);
  } catch (err) { toast(err.message, "error"); }
}

/* ---- STEP 3: confirm a live code (segmented boxes, like the auth screens) ---- */
function _tfaShowVerify() {
  _tfaSetStep(3);
  _tfaBody(`
    <p class="tfa-p"><b>2.</b> Enter the <b>6-digit code</b> now showing in your authenticator app:</p>
    <div class="otp-boxes tfa-otp" id="tfaOtpBoxes">
      <input type="text" maxlength="1" inputmode="numeric"><input type="text" maxlength="1" inputmode="numeric"><input type="text" maxlength="1" inputmode="numeric"><input type="text" maxlength="1" inputmode="numeric"><input type="text" maxlength="1" inputmode="numeric"><input type="text" maxlength="1" inputmode="numeric">
    </div>
    <p class="tfa-err" id="tfaErr"></p>
    <button class="btn-primary block" id="tfaVerifyBtn" onclick="_tfaVerify()">Verify &amp; enable</button>`);
  const boxes = document.querySelectorAll("#tfaOtpBoxes input");
  if (boxes[0]) boxes[0].focus();
  setupOtpBoxes("tfaOtpBoxes", _tfaVerify);
}
async function _tfaVerify() {
  const code = getOtpValue("tfaOtpBoxes");
  if (code.length !== 6) { toast("Enter the full 6-digit code", "error"); return; }
  const errEl = document.getElementById("tfaErr");
  try {
    const btn = document.getElementById("tfaVerifyBtn"); if (btn) { btn.disabled = true; btn.textContent = "Verifying…"; }
    const r = await api("/2fa/verify-setup", "POST", { code }, true);
    _tfa.codes = r.backup_codes || [];
    _tfaShowBackupCodes(true);
    refreshSecurityPanel();
  } catch (err) {
    if (errEl) { errEl.textContent = err.message; }
    document.getElementById("tfaOtpBoxes").classList.add("otp-err");
    setTimeout(() => document.getElementById("tfaOtpBoxes").classList.remove("otp-err"), 400);
    clearOtpBoxes("tfaOtpBoxes");
    const boxes = document.querySelectorAll("#tfaOtpBoxes input"); if (boxes[0]) boxes[0].focus();
    const btn = document.getElementById("tfaVerifyBtn"); if (btn) { btn.disabled = false; btn.textContent = "Verify & enable"; }
  }
}

/* ---- STEP 4: single-use backup codes ---- */
function _tfaShowBackupCodes(freshlyEnabled) {
  _tfaSetStep(4);
  document.getElementById("tfaTitle").textContent = freshlyEnabled ? "2FA is on — save your backup codes" : "New backup codes";
  const codes = _tfa.codes;
  _tfaBody(`
    ${freshlyEnabled ? `<p class="tfa-ok">${ic("check")} Two-factor authentication enabled.</p>` : ""}
    <p class="tfa-p">Save these <b>${codes.length} backup codes</b> somewhere safe — <b>each works once</b> if you lose access to your authenticator app.</p>
    <div class="bc-grid">${codes.map(c => `<code>${c}</code>`).join("")}</div>
    <div class="bc-actions">
      <button class="xbtn" onclick="_tfaDownloadCodes()">${ic("download")} Download codes</button>
      <button class="xbtn" onclick="navigator.clipboard.writeText(_tfa.codes.join('\\n')).then(()=>toast('All codes copied','success'))">${ic("copy")} Copy all</button>
    </div>
    <label class="bc-confirm"><input type="checkbox" id="tfaSavedChk"> I've saved these codes somewhere safe</label>
    <button class="btn-primary block" id="tfaDoneBtn" disabled onclick="closeModal('tfaModal');refreshSecurityPanel()">Done</button>`);
  const chk = document.getElementById("tfaSavedChk");
  const done = document.getElementById("tfaDoneBtn");
  chk.addEventListener("change", () => { done.disabled = !chk.checked; });
}
function _tfaDownloadCodes() {
  const txt = "Ahad Co — 2FA backup codes\nSave these somewhere safe. Each code works ONCE.\n\n" + _tfa.codes.join("\n") + "\n";
  _downloadBlob(new Blob([txt], { type: "text/plain;charset=utf-8" }),
    `ahadco-backup-codes-${new Date().toISOString().split("T")[0]}.txt`);
}

/* ---- MANAGE VIEW (when already enabled) ---- */
function _tfaShowManage(st) {
  _tfaStepsVisible(false);
  document.getElementById("tfaTitle").textContent = "Two-factor authentication";
  _tfaBody(`
    <div class="tfa-status">
      <span class="chip on">Enabled</span>
      <span class="tfa-meta">${st.backup_codes_count} backup code${st.backup_codes_count === 1 ? "" : "s"} left</span>
    </div>
    <div class="tfa-man-block">
      <h4>Regenerate backup codes</h4>
      <p>Old codes stop working. Confirm with your password + a current authenticator code.</p>
      <button class="btn-secondary block" onclick="_tfaMiniForm('regen')">Regenerate</button>
      <div class="tfa-mini hidden" id="tfaMiniRegen">
        <input type="password" id="regen_pw" class="input-text" placeholder="Password" autocomplete="current-password">
        <input type="text" id="regen_code" class="input-text" placeholder="6-digit authenticator code" inputmode="numeric" maxlength="6">
        <button class="btn-primary block" onclick="_tfaRegen()">Confirm &amp; regenerate</button>
      </div>
    </div>
    <div class="tfa-man-block danger-lite">
      <h4>Disable two-factor authentication</h4>
      <p>Your account will be protected by password only.</p>
      <button class="btn-ghost block tfa-dis-btn" onclick="_tfaMiniForm('disable')">Disable 2FA</button>
      <div class="tfa-mini hidden" id="tfaMiniDisable">
        <input type="password" id="dis_pw" class="input-text" placeholder="Password" autocomplete="current-password">
        <input type="text" id="dis_code" class="input-text" placeholder="6-digit or backup code" inputmode="text">
        <button class="btn-danger block" onclick="_tfaDisable()">Confirm disable</button>
      </div>
    </div>`);
}
function _tfaMiniForm(which) {
  const el = document.getElementById(which === "regen" ? "tfaMiniRegen" : "tfaMiniDisable");
  if (el) el.classList.toggle("hidden");
}
async function _tfaRegen() {
  const password = document.getElementById("regen_pw").value;
  const code = document.getElementById("regen_code").value.trim();
  if (!password || !code) { toast("Enter your password and code", "error"); return; }
  try {
    const r = await api("/2fa/backup-codes", "POST", { password, code }, true);
    _tfa.codes = r.backup_codes || [];
    _tfaStepsVisible(true);
    _tfaShowBackupCodes(false);
  } catch (err) { toast(err.message, "error"); }
}
async function _tfaDisable() {
  const password = document.getElementById("dis_pw").value;
  const code = document.getElementById("dis_code").value.trim();
  if (!password || !code) { toast("Enter your password and code", "error"); return; }
  try {
    await api("/2fa/disable", "POST", { password, code }, true);
    toast("Two-factor authentication disabled", "success");
    closeModal("tfaModal");
    refreshSecurityPanel();
  } catch (err) { toast(err.message, "error"); }
}

/* ==================== SETTINGS: CHANGE PASSWORD ==================== */
async function openChangePassword() {
  ["cp_current", "cp_new", "cp_confirm", "cp_totp"].forEach(id => { const e = document.getElementById(id); if (e) e.value = ""; });
  const lbl = document.getElementById("strengthLabel4"), fill = document.getElementById("strengthFill4");
  if (lbl) lbl.textContent = ""; if (fill) fill.style.width = "0";
  openModal("pwModal");
  // Show the 2FA field only when the account actually has 2FA on
  try {
    const st = await api("/2fa/status", "GET", null, true);
    document.getElementById("cpTotpRow").classList.toggle("hidden", !st.enabled);
  } catch (e) {}
}
async function submitChangePassword() {
  const current = document.getElementById("cp_current").value;
  const next = document.getElementById("cp_new").value;
  const conf = document.getElementById("cp_confirm").value;
  const totp = document.getElementById("cp_totp").value.trim();
  if (!current || !next) { toast("Fill in your current and new password", "error"); return; }
  if (next.length < 6) { toast("New password must be at least 6 characters", "error"); return; }
  if (next !== conf) { toast("New passwords don't match", "error"); return; }
  const btn = document.getElementById("cpSubmit");
  if (btn) { btn.disabled = true; btn.textContent = "Updating…"; }
  try {
    const r = await api("/account/change-password", "POST", {
      current_password: current, new_password: next, totp_code: totp || null,
    }, true);
    logEvent("success", "Password changed", `${r.other_sessions_revoked} other device(s) signed out`);
    document.getElementById("pwBody").innerHTML = `
      <div class="pw-done">
        <div class="success-ring">✓</div>
        <h3>Password updated</h3>
        <p class="auth-hint">For your security, <b>all other devices have been signed out</b>${r.other_sessions_revoked ? ` (${r.other_sessions_revoked} session${r.other_sessions_revoked === 1 ? "" : "s"})` : ""}.</p>
        <button class="btn-primary block" onclick="closeModal('pwModal')">Done</button>
      </div>`;
    refreshSecurityPanel();
  } catch (err) {
    toast(err.message, "error");
    if (btn) { btn.disabled = false; btn.textContent = "Update password"; }
  }
}

/* ==================== SETTINGS: ACTIVE SESSIONS ==================== */
function _sessDevice(ua) {
  const s = (ua || "").toLowerCase();
  const isMobile = /mobile|android|iphone|ipod/.test(s);
  const icon = isMobile ? "phone" : "server";
  let os = "Device";
  if (/windows/.test(s)) os = "Windows";
  else if (/android/.test(s)) os = "Android";
  else if (/iphone|ipad|ios/.test(s)) os = "iPhone / iPad";
  else if (/mac os|macintosh/.test(s)) os = "Mac";
  else if (/linux/.test(s)) os = "Linux";
  let br = "";
  if (/edg\//.test(s)) br = "Edge";
  else if (/chrome\//.test(s)) br = "Chrome";
  else if (/firefox\//.test(s)) br = "Firefox";
  else if (/safari\//.test(s) && !/chrome/.test(s)) br = "Safari";
  return { icon, label: br ? `${br} on ${os}` : os };
}
async function loadSessionsList() {
  const box = document.getElementById("sessList");
  if (!box) return;
  try {
    const data = await api("/sessions", "GET", null, true);
    const rows = data.sessions || [];
    if (!rows.length) { box.innerHTML = `<div class="muted" style="font-size:13px">No active sessions.</div>`; return; }
    box.innerHTML = rows.map(r => {
      const d = _sessDevice(r.device_info);
      const seen = r.last_seen ? new Date(r.last_seen).toLocaleString() : "";
      return `<div class="sess-row">
        <span class="sess-ic">${ic(d.icon)}</span>
        <div class="sess-tx">
          <b>${escapeHtml(d.label)}${r.is_current ? ' <span class="chip on sm">This device</span>' : ""}</b>
          <small>${escapeHtml(r.ip_address || "unknown ip")} · last active ${escapeHtml(seen)}</small>
        </div>
        ${r.is_current ? "" : `<button class="xbtn danger" onclick="revokeSession(${r.id})" title="Sign this device out">${ic("log-out")} Revoke</button>`}
      </div>`;
    }).join("");
  } catch (err) { box.innerHTML = `<div class="muted" style="font-size:13px">Couldn't load sessions.</div>`; }
}
async function revokeSession(id) {
  try {
    await api("/sessions/revoke", "POST", { session_id: id }, true);
    toast("Device signed out", "success");
    logEvent("warning", "Session revoked", "A device was signed out from Settings");
    loadSessionsList();
  } catch (err) { toast(err.message, "error"); }
}

/* ==================== SECURITY PANEL REFRESH ==================== */
async function refreshSecurityPanel() {
  try {
    const st = await api("/2fa/status", "GET", null, true);
    const chip = document.getElementById("tfaChip");
    if (chip) {
      chip.textContent = st.enabled ? "Enabled" : "Disabled";
      chip.className = "chip" + (st.enabled ? " on" : "");
    }
    const meta = document.getElementById("tfaMeta");
    if (meta) meta.textContent = st.enabled ? `Backup codes: ${st.backup_codes_count} left` : "Adds a second lock on top of your password.";
  } catch (e) {}
  const pw = document.getElementById("pwChangedAt");
  if (pw && _lastProfile) {
    const when = _lastProfile.password_changed_at || _lastProfile.created_at;
    if (when) pw.textContent = "Last changed: " + new Date(when).toLocaleDateString();
  }
}
let _lastProfile = null;

/* ==================== STATS ==================== */
async function loadStats() {
  try {
    const data = await api("/stats", "GET", null, true);
    const sj = document.getElementById("statJobs"); if (sj) sj.textContent = data.jobs_total || 0;
    const sl = document.getElementById("statLive"); if (sl) sl.textContent = data.jobs_deployed || 0;
    const ss = document.getElementById("statSnippets"); if (ss) ss.textContent = data.snippets || 0;
    const sp = document.getElementById("statPublished"); if (sp) sp.textContent = data.published || 0;
  } catch (err) { console.error("Load stats error:", err); }
}

/* ==================== INIT & EVENT WIRING ==================== */

// Re-establish the correct screen based on the CURRENT token. Used at boot and
// when the page is restored from the browser's back/forward cache (bfcache),
// which otherwise can resurrect a stale auth screen with the user's old form
// data still in it.
/* ==================== CLIENT-SIDE ROUTING ====================
   Every section has a REAL URL (/code, /jobs …) — like a proper SaaS:
     • switchTab pushes the path → browser back/forward walk sections
     • refresh on /jobs boots straight into RunSpace (no bounce to dashboard)
     • links can be bookmarked/shared; logged-out visits to protected paths
       bounce to /sign-in and RETURN after successful login. */
const ROUTES = {
  "/dashboard": "overview", "/code": "code",
  "/runspace": "jobs", "/jobs": "jobs",
  "/admin": "admin", "/profile": "profile",
};
const TAB_PATHS = {};
Object.keys(ROUTES).forEach(p => { if (!TAB_PATHS[ROUTES[p]]) TAB_PATHS[ROUTES[p]] = p; });
const AUTH_ROUTES = {
  "/sign-in": "screen-signin", "/login": "screen-signin",
  "/sign-up": "screen-signup", "/forgot": "screen-forgot1",
};
let _routeNav = false;   // guard: a popstate-driven switchTab must not re-push

function _clientPath() {
  let p = (window.location.pathname || "/").replace(/\/+$/, "");
  return p || "/";
}

/* Apply the current browser URL to app state. Returns a truthy tag when the
   URL decided a screen (so callers don't fall back to the landing page). */
function routeFromUrl() {
  const p = _clientPath();
  const hasToken = !!localStorage.getItem("ahad_token");
  const _switch = (tab) => { _routeNav = true; switchTab(tab); _routeNav = false; };

  if (p === "/activity") {
    if (!hasToken) {
      try { sessionStorage.setItem("ahad_return_to", p); } catch (e2) {}
      history.replaceState({}, "", "/sign-in");
      showScreen("screen-signin");
      return "blocked";
    }
    showScreen("screen-dashboard");
    if (currentTab !== "overview") _switch("overview");
    if (typeof openActivityPanel === "function") openActivityPanel();
    return "tab";
  }
  if (ROUTES[p]) {                                  // protected section URL
    if (!hasToken) {                                // standard "return after login"
      try { sessionStorage.setItem("ahad_return_to", p); } catch (e2) {}
      history.replaceState({}, "", "/sign-in");
      showScreen("screen-signin");
      return "blocked";
    }
    showScreen("screen-dashboard");
    if (ROUTES[p] !== currentTab) _switch(ROUTES[p]);
    return "tab";
  }
  if (AUTH_ROUTES[p]) {
    if (hasToken) {                                 // signed-in users skip auth screens
      history.replaceState({}, "", "/dashboard");
      showScreen("screen-dashboard");
      if (currentTab !== "overview") _switch("overview");
      return "tab";
    }
    showScreen(AUTH_ROUTES[p]);
    return "auth";
  }
  if (p === "/" && hasToken) {                      // SaaS convention: / → /dashboard
    try { history.replaceState({}, "", "/dashboard"); } catch (e3) {}
    return "tab";
  }
  return null;
}

/* After successful login/verification: go back where the user wanted to be. */
function _consumeReturnTo() {
  let rt = null;
  try { rt = sessionStorage.getItem("ahad_return_to"); } catch (e) {}
  if (rt && (ROUTES[rt] || rt === "/activity")) {
    try { sessionStorage.removeItem("ahad_return_to"); } catch (e2) {}
    try { history.replaceState({}, "", rt); } catch (e3) {}
    _routeNav = true;
    switchTab(ROUTES[rt] || "overview");
    _routeNav = false;
    if (rt === "/activity" && typeof openActivityPanel === "function") openActivityPanel();
  } else {
    // Never clobber the address bar if the user already navigated into a
    // section while the dashboard was still loading (e.g. quick-click on
    // RunSpace right after sign-in) — the URL is the user's truth.
    const cur = _clientPath();
    if (!ROUTES[cur] && cur !== "/dashboard") {
      try { history.replaceState({}, "", "/dashboard"); } catch (e4) {}
    }
  }
}

// Browser back/forward: derive the visible screen purely from the URL.
window.addEventListener("popstate", () => { routeFromUrl(); });

function reconcileScreen() {
  const hasToken = !!localStorage.getItem("ahad_token");
  if (hasToken) {
    authToken = localStorage.getItem("ahad_token");
    showScreen("screen-dashboard");
    loadDashboard().catch(() => { /* loadDashboard handles its own errors */ });
    routeFromUrl();   // honor deep links (/code, /jobs…) after auth restore
  } else if (localStorage.getItem("ahad_signup_username")) {
    // A verification was in progress — keep them on the OTP screen.
    restoreOtpScreen();
  } else {
    authToken = null;
    if (!routeFromUrl()) showScreen("screen-landing");  // /sign-in / protected / plain
  }
}

window.addEventListener("pageshow", (event) => {
  // Page restored from bfcache (e.g. user pressed Back from another site).
  // Force the screen back in sync with the real auth state.
  if (event.persisted) {
    reconcileScreen();
    document.documentElement.classList.remove("booting");
    const splash = document.getElementById("bootSplash");
    if (splash) splash.style.display = "none";
  }
});

/* Fatal-error visibility: a silent exception used to leave buttons dead with
   no explanation. Surface it once so a real bug can never hide again.
   Errors DURING BOOT get a friendly full-screen "something went wrong —
   reload" page (our error boundary), so the app never renders half-dead. */
let _fatalToasts = 0;
let _bootOk = false;   // flips true once DOMContentLoaded wiring finishes

function _fatalOverlay(message) {
  if (document.getElementById("fatalOverlay")) return;
  const div = document.createElement("div");
  div.id = "fatalOverlay";
  div.className = "fatal-overlay";
  div.innerHTML =
    '<div class="fatal-card">' +
      '<div class="fatal-ic">' + ic("alert") + '</div>' +
      '<h1>Something went wrong</h1>' +
      '<p>The app hit an unexpected error while starting up. Reloading usually fixes it.</p>' +
      (message ? '<code>' + escapeHtml(String(message).slice(0, 200)) + '</code>' : '') +
      '<div class="fatal-btns"><button class="btn-primary" onclick="window.location.reload()">Reload</button>' +
      '<button class="btn-ghost" onclick="document.getElementById(\'fatalOverlay\').remove()">Keep trying</button></div>' +
    '</div>';
  document.body.appendChild(div);
}

window.addEventListener("error", (e) => {
  if (!e || !e.message) return;
  if (!_bootOk) { _fatalOverlay(e.message); return; }
  if (_fatalToasts >= 3) return;
  _fatalToasts += 1;
  toast("UI error: " + String(e.message).slice(0, 120), "error");
});

document.addEventListener("DOMContentLoaded", () => {
  // Logout
  const btnLogoutEl = document.getElementById("btnLogout");
  if (btnLogoutEl) btnLogoutEl.addEventListener("click", async () => {
    try { await api("/logout", "POST", null, true); } catch (e) {}
    logEvent("info", "Signed out", "Session ended");
    authToken = null;
    localStorage.removeItem("ahad_token");
    resetLocalActivity();   // next account on this device starts with a clean feed
    toast("Logged out", "success");
    showScreen("screen-landing");
  });

  // Code IDE wiring
  const btnSaveSnippet = document.getElementById("btnSaveSnippet");
  if (btnSaveSnippet) btnSaveSnippet.addEventListener("click", saveSnippet);
  const btnRunSnippet = document.getElementById("btnRunSnippet");
  if (btnRunSnippet) btnRunSnippet.addEventListener("click", () => { togglePreviewPanel(); });
  // ▶ Run button → real execution in the bottom terminal
  const btnRunCode = document.getElementById("btnRunCode");
  if (btnRunCode) btnRunCode.addEventListener("click", () => { executeCode(); });
  // terminal close button
  const ahTermClose = document.getElementById("ahTermClose");
  if (ahTermClose) ahTermClose.addEventListener("click", () => {
    const t = document.getElementById("ahTerm");
    if (t) t.classList.remove("open");
  });
  // ⚡ Always-On Jobs wiring
  const btnStartJob = document.getElementById("btnStartJob");
  if (btnStartJob) btnStartJob.addEventListener("click", startJob);
  const jobLogClose = document.getElementById("jobLogClose");
  if (jobLogClose) jobLogClose.addEventListener("click", closeJobLogs);
  const jobLogRefresh = document.getElementById("jobLogRefresh");
  if (jobLogRefresh) jobLogRefresh.addEventListener("click", refreshJobLogs);
  const jobLogCopy = document.getElementById("jobLogCopy");
  if (jobLogCopy) jobLogCopy.addEventListener("click", copyJobLogs);
  const jobLogFull = document.getElementById("jobLogFull");
  if (jobLogFull) jobLogFull.addEventListener("click", toggleFullLogs);
  const jobLogBody = document.getElementById("jobLogBody");
  if (jobLogBody) jobLogBody.addEventListener("scroll", _setLogFollow);
  // Ctrl+Enter inside the jobs code box = start the job (power-users' shortcut)
  const jobCodeEl = document.getElementById("jobCode");
  if (jobCodeEl) jobCodeEl.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); startJob(); }
  });
  const btnFormatSnippet = document.getElementById("btnFormatSnippet");
  if (btnFormatSnippet) btnFormatSnippet.addEventListener("click", formatSnippet);
  const btnShareSnippet = document.getElementById("btnShareSnippet");
  if (btnShareSnippet) btnShareSnippet.addEventListener("click", shareCurrentSnippet);
  const btnNewSnippet = document.getElementById("btnNewSnippet");
  if (btnNewSnippet) btnNewSnippet.addEventListener("click", newSnippetDraft);
  // editor live updates (debounced) + meta + language change + Tab key
  const snippetContent = document.getElementById("snippetContent");
  if (snippetContent) {
    snippetContent.addEventListener("input", () => {
      updateEditorMeta();
      updateGutter();
      clearTimeout(_livePreviewTimer);
      // Only web languages get live preview — re-writing the iframe on every
      // keystroke while typing Python/C/Java caused constant flicker.
      const l = (document.getElementById("snippetLanguage").value || "").toLowerCase();
      if (_RUNNABLE_LANGS[l]) _livePreviewTimer = setTimeout(runLivePreview, 400);
    });
    snippetContent.addEventListener("keydown", (e) => {
      if (e.key === "Tab") {
        e.preventDefault();
        const s = e.target, start = s.selectionStart, end = s.selectionEnd;
        s.value = s.value.substring(0, start) + "  " + s.value.substring(end);
        s.selectionStart = s.selectionEnd = start + 2;
        updateEditorMeta();
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        // Web languages (HTML/CSS/JS/MD) → live iframe preview.
        // Everything else (Python, C, Go...) → real run in the terminal.
        const curLang = (document.getElementById("snippetLanguage").value || "").toLowerCase();
        if (_RUNNABLE_LANGS[curLang]) { runLivePreview(); } else { executeCode(); }
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveSnippet(); }
    });
  }
  const snippetLanguage = document.getElementById("snippetLanguage");
  if (snippetLanguage) snippetLanguage.addEventListener("change", () => { updateCodeMirrorMode(); syncRunPreviewButtons(); runLivePreview(); });
  try { initCodeMirror(); } catch (e) { console.error("initCodeMirror:", e); }
  // These editor helpers must never block the wiring of the REST of the app.
  try { syncRunPreviewButtons(); } catch (e) { console.error("syncRunPreviewButtons:", e); }
  try { initIdeDivider(); } catch (e) { console.error("initIdeDivider:", e); }
  try { initGutterScroll(); } catch (e) { console.error("initGutterScroll:", e); }
  const btnEditorFull = document.getElementById("btnEditorFull");
  if (btnEditorFull) btnEditorFull.addEventListener("click", toggleEditorFullscreen);
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { const c = document.querySelector(".cs-canvas.full"); if (c) toggleEditorFullscreen(); }
  });
  // Tab click handlers (desktop)
  document.querySelectorAll(".dash-tab").forEach(tab => {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  });

  // Mobile bottom-nav items
  document.querySelectorAll(".bn-item").forEach(b => {
    b.addEventListener("click", () => switchTab(b.dataset.tab));
  });

  // Password strength
  const pw = document.getElementById("su_password");
  if (pw) pw.addEventListener("input", e => {
    checkStrength(e.target.value, document.getElementById("strengthFill"), document.getElementById("strengthLabel"));
  });
  const pw3 = document.getElementById("fp_newpass");
  if (pw3) pw3.addEventListener("input", e => {
    checkStrength(e.target.value, document.getElementById("strengthFill3"), document.getElementById("strengthLabel3"));
  });

  // Delete account wiring (2FA button uses inline onclick="manage2FA()")
  document.querySelectorAll(".btn-danger").forEach(b => {
    if (b.textContent.includes("Delete Account")) b.addEventListener("click", e => { e.preventDefault(); deleteAccount(); });
  });
  // Live strength meter inside the change-password modal
  const pw4 = document.getElementById("cp_new");
  if (pw4) pw4.addEventListener("input", e => {
    checkStrength(e.target.value, document.getElementById("strengthFill4"), document.getElementById("strengthLabel4"));
  });

  // Marketing mobile nav (burger -> sheet)
  const burger = document.getElementById("navBurger");
  const navSheet = document.getElementById("navSheet");
  if (burger && navSheet) burger.addEventListener("click", () => navSheet.classList.toggle("hidden"));

  // Mobile bottom nav "Menu" → left side drawer (full-height, standard)
  const bnMore = document.getElementById("bnMore");
  if (bnMore) bnMore.addEventListener("click", (e) => { e.preventDefault(); openSideMenu(); });
  const sideMenuBtn = document.getElementById("sideMenuBtn");
  if (sideMenuBtn) sideMenuBtn.addEventListener("click", openSideMenu);
  const sideOverlay = document.getElementById("sideOverlay");
  if (sideOverlay) sideOverlay.addEventListener("click", closeSideMenu);
  document.querySelectorAll(".dash-tabs .dash-tab").forEach(b => b.addEventListener("click", closeSideMenu));
  const btnActivitySide = document.getElementById("btnActivitySide");
  if (btnActivitySide) btnActivitySide.addEventListener("click", () => { closeSideMenu(); openActivityPanel(); });

  // Desktop sidebar collapse → icon-only (persisted)
  const dashRoot = document.querySelector(".dashboard");
  const sideCollapse = document.getElementById("sideCollapse");
  const _applySideMin = on => {
    if (dashRoot) dashRoot.classList.toggle("side-min", on);
    try { localStorage.setItem("ahad_side_min", on ? "1" : "0"); } catch (e) {}
  };
  if (sideCollapse) sideCollapse.addEventListener("click", () => {
    _applySideMin(!(dashRoot && dashRoot.classList.contains("side-min")));
  });
  try { if (localStorage.getItem("ahad_side_min") === "1") _applySideMin(true); } catch (e) {}
  const moreOverlay = document.getElementById("moreOverlay");
  if (moreOverlay) moreOverlay.addEventListener("click", closeMoreSheet);
  const bnLogout = document.getElementById("bnLogout");
  if (bnLogout) bnLogout.addEventListener("click", () => document.getElementById("btnLogout").click());
  // Mobile search button in bottom nav
  const bnSearch = document.getElementById("bnSearch");
  if (bnSearch) bnSearch.addEventListener("click", openCommandPalette);

  // Command palette wiring (Ctrl/Cmd+K, button, overlay, keyboard)
  const cmdBtn = document.getElementById("cmdBtn");
  if (cmdBtn) cmdBtn.addEventListener("click", openCommandPalette);
  const cmdOverlay = document.getElementById("cmdOverlay");
  if (cmdOverlay) cmdOverlay.addEventListener("click", (e) => { if (e.target === cmdOverlay) closeCommandPalette(); });
  const cmdInput = document.getElementById("cmdInput");
  if (cmdInput) {
    cmdInput.addEventListener("input", (e) => {
      clearTimeout(_cmdTimer);
      _cmdTimer = setTimeout(() => runCommandSearch(e.target.value), 220);
    });
    cmdInput.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { closeCommandPalette(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); if (_cmdIndex < _cmdResults.length - 1) { _cmdIndex++; renderCommandResults(); } }
      else if (e.key === "ArrowUp") { e.preventDefault(); if (_cmdIndex > 0) { _cmdIndex--; renderCommandResults(); } }
      else if (e.key === "Enter") { e.preventDefault(); if (_cmdIndex >= 0) openSearchResult(_cmdIndex); else if (_cmdResults.length) openSearchResult(0); }
    });
  }
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (document.getElementById("screen-dashboard").classList.contains("hidden")) return;
      if (cmdOverlay.classList.contains("hidden")) openCommandPalette(); else closeCommandPalette();
    } else if (e.key === "Escape" && cmdOverlay && !cmdOverlay.classList.contains("hidden")) {
      closeCommandPalette();
    }
  });

  // Theme toggle wiring
  const themeBtn = document.getElementById("themeBtn");
  if (themeBtn) themeBtn.addEventListener("click", toggleTheme);

  // Reveal-on-scroll (Stripe-style entrance animations)
  const revealEls = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && revealEls.length) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach(en => { if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); } });
    }, { threshold: 0.12 });
    revealEls.forEach(el => io.observe(el));
  } else {
    revealEls.forEach(el => el.classList.add("in"));
  }

  // ---- Activity log panel wiring (opened from Profile / More-sheet now) ----
  const apClose = document.getElementById("activityClose");
  if (apClose) apClose.addEventListener("click", closeActivityPanel);
  const apOverlay = document.getElementById("activityOverlay");
  if (apOverlay) apOverlay.addEventListener("click", closeActivityPanel);
  const apClear = document.getElementById("activityClear");
  if (apClear) apClear.addEventListener("click", () => {
    _saveActivity([]); renderActivity(); toast("Activity log cleared", "info");
  });
  renderActivity(); // draw any persisted events immediately

  // ---- OTP "Paste from clipboard" button ----
  const otpPasteBtn = document.getElementById("otpPasteBtn");
  if (otpPasteBtn) otpPasteBtn.addEventListener("click", pasteOtp);

  // ---- Boot: decide the screen SYNCHRONOUSLY (no flash) ----
  if (authToken) {
    showScreen("screen-dashboard");
    loadDashboard().catch(() => { /* infra-safe: banner + retry inside */ });
    routeFromUrl();          // direct hit on /jobs etc. → open that section
  } else if (localStorage.getItem("ahad_signup_username")) {
    // A verification was in progress (e.g. user switched to their mail app and
    // the page reloaded). Restore the OTP screen so they can finish verifying.
    restoreOtpScreen();
  } else {
    if (!routeFromUrl()) showScreen("screen-landing");  // deep link or plain visit
  }

  // Boot accomplished — a fatal error from here gets a toast, not the overlay.
  _bootOk = true;

  // Drop the boot splash now that a screen has been chosen.
  document.documentElement.classList.remove("booting");
  const splash = document.getElementById("bootSplash");
  if (splash) splash.style.display = "none";
});

/* Restore an in-progress verification screen from localStorage. */
function restoreOtpScreen() {
  const username = localStorage.getItem("ahad_signup_username") || "";
  const email = localStorage.getItem("ahad_signup_email") || "your email";
  signupUsername = username;
  clearOtpBoxes("otpBoxesSignup");
  document.getElementById("otpEmailNote").textContent =
    "Welcome back, " + username + "! Enter your 6-digit code to finish verifying. (Sent to " + email + ".)";
  showScreen("screen-otp");
  startResendTimer(10);
  logEvent("info", "Verification resumed", "Restored pending verification for " + username);
  toast("Pick up where you left off — enter your code.", "info");
}

/* Paste a copied 6-digit code from the clipboard into the OTP boxes. */
async function pasteOtp() {
  let code = "";
  try {
    code = (await navigator.clipboard.readText() || "").replace(/[^0-9]/g, "").slice(0, 6);
  } catch (e) {
    toast("Clipboard access blocked — paste manually (Ctrl/Cmd+V).", "warning");
    return;
  }
  if (code.length !== 6) {
    toast("Clipboard doesn't contain a 6-digit code. Paste it manually.", "warning");
    return;
  }
  const boxes = document.querySelectorAll("#otpBoxesSignup input");
  code.split("").forEach((ch, i) => { if (boxes[i]) boxes[i].value = ch; });
  toast("Code pasted!", "success");
  if (boxes[5]) boxes[5].focus();
}

/* Show server-side login history (from /login-history) in the activity panel. */
async function showLoginHistory() {
  try {
    const data = await api("/login-history", "GET", null, true);
    openActivityPanel();
    const list = data.history || [];
    list.forEach(h => {
      logEvent(h.success ? "success" : "error",
        h.success ? "Sign-in recorded" : "Failed sign-in",
        (h.location || "Email verification") + " · " + (h.ip_address || "") + " · " + (h.device_info || ""));
    });
    if (!list.length) toast("No login history yet.", "info");
  } catch (err) { toast(err.message, "error"); }
}

/* ==================== ⚡ ALWAYS-ON JOBS (24/7 tasks) ====================
   Frontend for /api/jobs — start/stop/restart persistent background tasks
   running on the runner service, with live logs. */
let _jobsTimer = null;
let _jobLogFor = null;
let _lastJobsSig = null; // change detection: skip re-render when nothing moved.
                         // MUST be null (not "") — the empty-list signature is "",
                         // and "" === "" would skip the very first render forever.

async function loadJobs() {
  const list = document.getElementById("jobsList");
  if (!list) return;
  if (_lastJobsSig === null) list.innerHTML = _skel(2);
  try {
    const data = await api("/api/jobs", "GET", null, true);
    // Flicker guard: rebuild the list ONLY when statuses actually changed
    // (uptime seconds ticking must not steal DOM/repaint every 7s).
    const jobs = (data && data.jobs) || [];
    const sig = jobs.map(j => [j.id, j.status, j.restarts, j.web ? 1 : 0, j.web_public === false ? 0 : 1].join(":")).join("|");
    if (sig !== _lastJobsSig) { _lastJobsSig = sig; renderJobs(jobs); }
  } catch (e) {
    const sig = "ERR:" + e.message;
    if (sig === _lastJobsSig) return;
    _lastJobsSig = sig;
    _loadErrorBox(list, "jobs", loadJobs, e);   // inline error + retry, never a stuck state
  }
}

function _fmtUptime(s) {
  s = s || 0;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return h + "h " + m + "m";
  if (m) return m + "m " + sec + "s";
  return sec + "s";
}

function renderJobs(jobs) {
  const list = document.getElementById("jobsList");
  if (!list) return;
  list.innerHTML = "";
  if (!jobs.length) {
    const div = document.createElement("div");
    div.className = "jobs-empty";
    div.innerHTML = ic("zap") + ' Nothing deployed yet — paste code above and press <b>Deploy 24/7</b>.';
    list.appendChild(div);
    return;
  }
  jobs.forEach(j => {
    const st = (j.status || "offline").toLowerCase();
    const card = document.createElement("div");
    card.className = "job-card";
    const isPub = j.web_public !== false;
    // Row 1: status dot + name (left) — action buttons (right)
    // Row 2: meta info, full width. Consistent alignment, no ragged wrap.
    // Row 3 (web jobs only): public URL + copy/open + access pill.
    card.innerHTML =
      '<div class="job-top">' +
        '<span class="job-dot ' + st + '"></span>' +
        '<span class="job-name">' + escapeHtml(j.name) + '</span>' +
        (j.web_url && st === "running" && j.web
          ? '<span class="job-pill ' + (isPub ? "pub" : "priv") + '">' + (isPub ? "Public" : "Private") + '</span>'
          : '') +
        '<span class="job-actions"></span>' +
      '</div>' +
      '<div class="job-meta">' + escapeHtml(j.language) + ' · ' +
        (st === "installing" ? 'installing libraries… <span class="mini-spinner"></span>' : escapeHtml(st)) +
        (j.uptime_s ? ' · up ' + _fmtUptime(j.uptime_s) : '') +
        (j.restarts ? ' · restarts ' + j.restarts : '') +
      '</div>';
    // Public URL row: ONLY when the runner actually detected a web listener.
    if (j.web_url && j.web && st === "running") {
      const shownUrl = isPub ? j.web_url : (j.web_private_url || j.web_url);
      const row = document.createElement("div");
      row.className = "job-url";
      row.innerHTML = ic("globe") + '<code title="' + escapeHtml(shownUrl) + '">' + escapeHtml(shownUrl) + '</code>';
      const mk2 = (label, title, fn) => {
        const b = document.createElement("button");
        b.className = "xbtn"; b.innerHTML = label; b.title = title;
        b.addEventListener("click", fn); row.appendChild(b);
      };
      mk2(ic("copy"), "Copy URL", () => copyText(shownUrl));
      mk2(ic("external"), "Open in new tab", () => window.open(shownUrl, "_blank", "noopener"));
      mk2(ic(isPub ? "lock" : "globe"), isPub ? "Make private (only you can open it)" : "Make public (anyone with the link)",
        () => toggleJobAccess(j.id, !isPub));
      card.appendChild(row);
    }
    const actions = card.querySelector(".job-actions");
    const mkBtn = (label, cls, fn) => {
      const b = document.createElement("button");
      b.className = "job-btn" + (cls ? " " + cls : "");
      b.innerHTML = label;
      b.addEventListener("click", fn);
      actions.appendChild(b);
    };
    mkBtn(ic("history") + " Logs", "", () => viewJobLogs(j.id, j.name));
    mkBtn(ic("refresh") + " Restart", "", () => restartJobById(j.id));
    mkBtn(ic("square") + " Stop", "", () => stopJobById(j.id));
    mkBtn(ic("trash"), "danger", (e) => deleteJobById(j.id, e.currentTarget));
    list.appendChild(card);
  });
}

async function toggleJobAccess(id, makePublic) {
  try {
    const info = await api(`/api/jobs/${id}/access`, "POST", { public: makePublic }, true);
    if (info && info.web_private_url) {
      toast("Private link ready — copied ✓", "success");
      copyText(info.web_private_url);
    } else {
      toast(makePublic ? "App URL is now PUBLIC" : "App URL is now PRIVATE", "info");
    }
    loadJobs();
  } catch (e) { toast(e.message, "error"); }
}

async function startJob() {
  const name = document.getElementById("jobName").value.trim();
  const language = document.getElementById("jobLang").value;
  const code = document.getElementById("jobCode").value;
  if (!name) { toast("Give the app a name!", "error"); return; }
  if (!code.trim()) { toast("Paste some code first!", "error"); return; }
  const btn = document.getElementById("btnStartJob");
  setLoading(btn, true);
  try {
    const info = await api("/api/jobs", "POST", { name, language, code }, true);
    toast("Deployed — running 24/7 now 🚀", "success");
    document.getElementById("jobName").value = "";
    document.getElementById("jobCode").value = "";
    await loadJobs();
    if (info && info.job_db_id) viewJobLogs(info.job_db_id, name);
  } catch (e) {
    toast(e.message, "error");
  } finally {
    setLoading(btn, false);
  }
}

async function stopJobById(id) {
  try {
    await api(`/api/jobs/${id}/stop`, "POST", null, true);
    toast("App stopped", "info");
    loadJobs();
  } catch (e) { toast(e.message, "error"); }
}

async function restartJobById(id) {
  try {
    await api(`/api/jobs/${id}/restart`, "POST", null, true);
    toast("App restarted 🚀", "success");
    loadJobs();
  } catch (e) { toast(e.message, "error"); }
}

async function deleteJobById(id, btn) {
  if (!confirm("Delete this app permanently?")) return;
  try {
    await api(`/api/jobs/${id}`, "DELETE", null, true);
    closeJobLogs();
    toast("App deleted", "info");
    await _rowOut(btn);
    loadJobs();
  } catch (e) { toast(e.message, "error"); }
}

let _jobLogTimer = null;
let _jobLogES = null;
let _logAutoScroll = true;

function _logBody() { return document.getElementById("jobLogBody"); }

function _setLogFollow() {
  const body = _logBody();
  if (!body) return;
  // Auto-scroll pauses while the user is scrolled up reading older lines,
  // and resumes the moment they return to the very bottom.
  _logAutoScroll = body.scrollTop + body.clientHeight >= body.scrollHeight - 40;
}

function _renderLogText(txt) {
  const body = _logBody();
  if (!body) return;
  body.textContent = (txt && txt.trim()) ? txt + "\n" : "(no output yet)";
  if (_logAutoScroll) body.scrollTop = body.scrollHeight;
}

async function viewJobLogs(id, name) {
  _jobLogFor = { id, name };
  document.getElementById("jobLogTitle").textContent = "📜 " + name;
  document.getElementById("jobLogBox").style.display = "block";
  _logAutoScroll = true;
  _renderLogText("connecting…");
  _openLogStream(id);
  if (_jobLogTimer) { clearInterval(_jobLogTimer); _jobLogTimer = null; }
}

function _openLogStream(id) {
  if (_jobLogES) { _jobLogES.close(); _jobLogES = null; }
  try {
    // Real-time logs over Server-Sent Events — lines appear as they happen,
    // no refresh button needed.
    const es = new EventSource(`/api/jobs/${id}/logs/stream?token=${encodeURIComponent(authToken || "")}`);
    _jobLogES = es;
    es.onmessage = (ev) => {
      try { _renderLogText(JSON.parse(ev.data).logs); } catch (e) {}
    };
    es.onerror = () => {
      // Stream blocked? Fall back to gentle polling so logs still move.
      es.close();
      if (_jobLogES === es) _jobLogES = null;
      if (_jobLogFor && !_jobLogTimer) _jobLogTimer = setInterval(refreshJobLogs, 2500);
    };
  } catch (e) {
    if (!_jobLogTimer) _jobLogTimer = setInterval(refreshJobLogs, 2500);
  }
}

async function refreshJobLogs() {
  if (!_jobLogFor) { if (_jobLogTimer) { clearInterval(_jobLogTimer); _jobLogTimer = null; } return; }
  try {
    const data = await api(`/api/jobs/${_jobLogFor.id}/logs`, "GET", null, true);
    _renderLogText(data.logs);
  } catch (e) {
    _renderLogText("✗ " + e.message);
  }
}

function copyJobLogs() {
  const body = _logBody();
  const btn = document.getElementById("jobLogCopy");
  const text = body ? body.textContent : "";
  const done = () => {
    if (!btn) return;
    btn.textContent = "Copied ✓";
    setTimeout(() => { btn.textContent = "⧉ Copy"; }, 1400);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(done);
  } else {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    ta.remove();
    done();
  }
}

function toggleFullLogs() {
  const box = document.getElementById("jobLogBox");
  if (!box) return;
  box.classList.toggle("full");
  const b = document.getElementById("jobLogFull");
  if (b) b.textContent = box.classList.contains("full") ? "⤡ Exit" : "⤢ Full";
}

function closeJobLogs() {
  if (_jobLogTimer) { clearInterval(_jobLogTimer); _jobLogTimer = null; }
  if (_jobLogES) { _jobLogES.close(); _jobLogES = null; }
  _jobLogFor = null;
  const box = document.getElementById("jobLogBox");
  if (box) { box.style.display = "none"; box.classList.remove("full"); }
  const b = document.getElementById("jobLogFull");
  if (b) b.textContent = "⤢ Full";
}

function startJobPolling() {
  loadJobs();
  if (_jobsTimer) clearInterval(_jobsTimer);
  _jobsTimer = setInterval(loadJobs, 7000);
}

function stopJobPolling() {
  if (_jobsTimer) { clearInterval(_jobsTimer); _jobsTimer = null; }
}

/* ==================== ADMIN CONSOLE (owner-only) ====================
   The sidebar button stays hidden until /profile says is_admin. The server
   answers 404 (not 403) for everybody else, so the panel's existence is
   never leaked. Destructive actions re-ask the admin's OWN 2FA code. */
let _admPending = null;   // { user_id, suspended } awaiting 2FA confirm

let _adminSectHtml = null;   // pristine copy so the panel can come BACK on
                             // this device when an actual admin signs in next
function applyAdminVisibility(profile) {
  const isAdm = !!(profile && profile.is_admin);
  const btn = document.getElementById("tabBtnAdmin");
  if (btn) btn.classList.toggle("hidden", !isAdm);
  let sect = document.getElementById("tab-admin");

  if (isAdm) {
    if (!sect && _adminSectHtml) {
      const host = document.querySelector(".dash-main");
      if (host) host.insertAdjacentHTML("beforeend", _adminSectHtml);
    }
    return;
  }

  // STEALTH for everyone else — the panel must not merely hide its DATA, it
  // must not EXIST: non-admins get "this page isn't here", exactly like the
  // server's 404. Remove the section from the DOM (switchTab then no-ops on
  // it), bounce anyone sitting on it, and scrub the /admin URL + any saved
  // deep-link so the address bar never advertises it either.
  if (sect && !_adminSectHtml) _adminSectHtml = sect.outerHTML;
  if (currentTab === "admin") switchTab("overview");
  if (sect) sect.remove();
  try {
    if (_clientPath() === "/admin") history.replaceState({}, "", "/dashboard");
    if (sessionStorage.getItem("ahad_return_to") === "/admin") sessionStorage.removeItem("ahad_return_to");
  } catch (e) {}
}

async function loadAdminPanel(force) {
  const stats = document.getElementById("admStats");
  if (!stats) return;
  if (force) delete stats.dataset.loaded;
  if (stats.dataset.loaded !== "1") {
    stats.innerHTML = '<div class="adm-stat"><b>…</b><span>loading</span></div>';
  }
  try {
    const [ov, usersR, jobsR, reportsR, auditR] = await Promise.all([
      api("/admin/overview", "GET", null, true),
      api("/admin/users", "GET", null, true),
      api("/admin/jobs", "GET", null, true),
      api("/admin/abuse-reports", "GET", null, true),
      api("/admin/audit-log", "GET", null, true),
    ]);
    renderAdminStats(ov || {});
    renderAdminSpark(ov || {});
    renderAdminJobs((jobsR && jobsR.jobs) || []);
    renderAdminUsers((usersR && usersR.users) || []);
    renderAdminReports((reportsR && reportsR.reports) || []);
    renderAdminAudit((auditR && auditR.audit) || []);
    stats.dataset.loaded = "1";
  } catch (e) {
    // 404 for non-admins — stay quiet and ambiguous, just like the server.
    stats.innerHTML = '<div class="adm-empty">Nothing here.</div>';
  }
}

function renderAdminStats(ov) {
  const el = document.getElementById("admStats");
  const chip = (label, val, cls) =>
    `<div class="adm-stat${cls ? " " + cls : ""}"><b>${val}</b><span>${label}</span></div>`;
  el.innerHTML =
    chip("users", ov.users ?? 0) +
    chip("verified", ov.verified ?? 0) +
    chip("suspended", ov.suspended ?? 0, ov.suspended ? "warn" : "") +
    chip("apps live", ov.jobs_deployed ?? 0) +
    chip("capacity used", `${ov.jobs_deployed ?? 0}/${ov.capacity_max ?? 0}`);
  const cap = document.getElementById("admCap");
  if (cap) cap.textContent =
    `capacity: ${ov.jobs_deployed ?? 0} of ${ov.capacity_max ?? 0} slots used (max ${ov.jobs_max_per_user ?? 3}/user)` +
    (ov.runner_capacity != null ? ` · runner: ${ov.runner_running ?? 0}/${ov.runner_capacity} busy` : "");
}

function renderAdminSpark(ov) {
  const el = document.getElementById("admSpark");
  if (!el) return;
  const byDay = {};
  (ov.signups_daily || []).forEach(r => { byDay[r.day] = r.count; });
  const days = [];
  for (let i = 13; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400000);
    const key = d.toISOString().slice(0, 10);
    days.push({ label: key.slice(5), count: byDay[key] || 0 });
  }
  const max = Math.max(1, ...days.map(d => d.count));
  el.innerHTML = days.map(d => {
    const h = Math.max(6, Math.round((d.count / max) * 56));
    return `<span class="adm-bar${d.count ? "" : " zero"}" style="height:${h}px" title="${d.label}: ${d.count} signup${d.count === 1 ? "" : "s"}"></span>`;
  }).join("");
}

function renderAdminJobs(jobs) {
  const el = document.getElementById("admJobs");
  if (!el) return;
  if (!jobs.length) { el.innerHTML = '<tr><td class="adm-empty">No RunSpace apps yet.</td></tr>'; return; }
  el.innerHTML = '<tr><th>App</th><th>Owner</th><th>Lang</th><th>Status</th><th>Uptime</th><th>Created</th></tr>' +
    jobs.map(j => {
      const st = (j.live_status || (j.runner_job_id ? "offline" : "stopped")).toLowerCase();
      const live = st === "running";
      return `<tr><td><b>${escapeHtml(j.name)}</b></td>` +
      `<td>${escapeHtml(j.owner)}${j.owner_suspended ? ' <span class="adm-pill warn">suspended</span>' : ""}</td>` +
      `<td>${escapeHtml(j.language)}</td>` +
      `<td><span class="adm-pill${live ? " ok" : ""}">${escapeHtml(st)}</span></td>` +
      `<td>${j.uptime_s ? _fmtUptime(j.uptime_s) : "—"}</td>` +
      `<td>${escapeHtml((j.created_at || "").slice(0, 10))}</td></tr>`;
    }).join("");
}

function renderAdminUsers(users) {
  const el = document.getElementById("admUsers");
  if (!el) return;
  if (!users.length) { el.innerHTML = '<tr><td class="adm-empty">No users yet.</td></tr>'; return; }
  const meId = _lastProfile && _lastProfile.id;
  el.innerHTML = '<tr><th>User</th><th>Joined</th><th>Apps</th><th>Status</th><th></th></tr>' +
    users.map(u => {
      const isMe = meId && u.id === meId;
      const state = u.is_suspended
        ? '<span class="adm-pill warn">suspended</span>'
        : (u.is_verified ? '<span class="adm-pill ok">active</span>' : '<span class="adm-pill">unverified</span>');
      const act = isMe
        ? '<span class="adm-hint">you</span>'
        : `<button class="adm-act${u.is_suspended ? " ok" : ""}" onclick="askSuspend(${u.id}, ${u.is_suspended ? 0 : 1}, this)">${u.is_suspended ? "Reactivate" : "Suspend"}</button>`;
      return `<tr><td><b>${escapeHtml(u.username)}</b><small>${escapeHtml(u.email)}</small></td>` +
        `<td>${escapeHtml((u.created_at || "").slice(0, 10))}</td>` +
        `<td>${u.job_count}</td><td>${state}</td><td>${act}</td></tr>`;
    }).join("");
}

function renderAdminReports(reports) {
  const el = document.getElementById("admReports");
  if (!el) return;
  if (!reports.length) { el.innerHTML = '<tr><td class="adm-empty">No abuse reports — all quiet. 🎉</td></tr>'; return; }
  el.innerHTML = '<tr><th>When</th><th>Reported URL</th><th>Reason</th><th>Status</th></tr>' +
    reports.map(r =>
      `<tr><td>${escapeHtml((r.created_at || "").slice(0, 16))}</td>` +
      `<td><a class="adm-link" href="${escapeHtml(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.url.length > 48 ? r.url.slice(0, 48) + "…" : r.url)}</a></td>` +
      `<td>${escapeHtml(r.reason || "—")}</td>` +
      `<td><span class="adm-pill${r.status === "open" ? " warn" : ""}">${escapeHtml(r.status)}</span></td></tr>`
    ).join("");
}

function renderAdminAudit(audit) {
  const el = document.getElementById("admAudit");
  if (!el) return;
  if (!audit.length) { el.innerHTML = '<tr><td class="adm-empty">No admin actions recorded yet.</td></tr>'; return; }
  el.innerHTML = '<tr><th>When</th><th>Admin</th><th>Action</th><th>Target</th></tr>' +
    audit.map(a =>
      `<tr><td>${escapeHtml((a.created_at || "").slice(0, 16))}</td>` +
      `<td>${escapeHtml(a.admin_name || "—")}</td>` +
      `<td><span class="adm-pill">${escapeHtml(a.action)}</span></td>` +
      `<td>${escapeHtml(a.target || "")}</td></tr>`
    ).join("");
}

function askSuspend(userId, suspend, btn) {
  const row = btn.closest("tr");
  const uname = row ? (row.querySelector("b") || {}).textContent || "this user" : "this user";
  _admPending = { user_id: userId, suspended: !!suspend };
  document.getElementById("adminModalTitle").textContent = suspend ? `Suspend ${uname}?` : `Reactivate ${uname}?`;
  document.getElementById("adminModalText").textContent = suspend
    ? `${uname} will be signed out on every device and their RunSpace apps will stop. You can reactivate anytime. Confirm with YOUR authenticator code.`
    : `${uname} gets their access back immediately. Confirm with YOUR authenticator code.`;
  document.getElementById("adminTfaCode").value = "";
  openModal("adminModal");
  setTimeout(() => { const i = document.getElementById("adminTfaCode"); if (i) i.focus(); }, 80);
}

async function confirmAdminAction() {
  if (!_admPending) { closeModal("adminModal"); return; }
  const btn = document.getElementById("adminModalGo");
  const code = document.getElementById("adminTfaCode").value.trim();
  if (!code) { toast("Enter your 6-digit authenticator code.", "error"); return; }
  setLoading(btn, true);
  try {
    const res = await api("/admin/users/set-suspended", "POST",
      { user_id: _admPending.user_id, suspended: _admPending.suspended, code }, true);
    _admPending = null;
    closeModal("adminModal");
    toast((res && res.message) || "Done.", "success");
    loadAdminPanel(true);
  } catch (e) {
    toast(e.message, "error");
  } finally {
    setLoading(btn, false);
  }
}

// Enter inside the admin 2FA box = confirm. (Wired once at boot.)
(function () {
  const box = document.getElementById("adminTfaCode");
  if (box) box.addEventListener("keydown", (e) => { if (e.key === "Enter") confirmAdminAction(); });
})();
