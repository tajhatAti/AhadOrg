/* =========================================================================
   PIVOT E2E — jsdom harness driving the REAL app served by driver.py
   -------------------------------------------------------------------------
   The browser is simulated honestly: the real index.html is parsed by jsdom,
   the real static/pro.js is evaluated inside it, forms are filled and buttons
   clicked — against the real FastAPI server (driver.py) with real HTTP.

   Hard-won jsdom facts (do NOT "simplify" these):
     1. window.eval(proSrc) MUST run synchronously right after construction —
        before the first await. jsdom fires DOMContentLoaded on the next task;
        if we await in between, pro.js's init listener never registers
        (this was the "bnSearch palette flake" root cause).
     2. No resource loader → <link>/<script src> are stripped; CSS is inlined
        as <style> so class-based visibility behaves like a browser.
     3. Top-level `let authToken` is NOT reachable as window.authToken — log
        in through the real form (#si_username/#si_password/#btnSignin).
     4. fetch / EventSource / clipboard / window.open are stubbed in
        beforeParse (node http → the real server).

   Run:  node tests/e2e/test-pivot.js            (driver must already be up)
         EXPECT_LANDING=1 node tests/e2e/test-pivot.js   (also assert the S3
         landing sections — until Step 3 ships those checks stay gated off)
   ========================================================================= */
"use strict";

const fs = require("fs");
const path = require("path");
const http = require("http");
const crypto = require("crypto");

const PORT = parseInt(process.env.PORT || "8931", 10);
const BASE = `http://127.0.0.1:${PORT}`;
const EXPECT_LANDING = process.env.EXPECT_LANDING === "1";

const ROOT = path.join(__dirname, "..", "..");
const htmlPath = path.join(ROOT, "index.html");
const proPath = path.join(ROOT, "static", "pro.js");
const cssPath = path.join(ROOT, "static", "classic.css");

let passed = 0, failed = 0;
function check(name, cond, extra) {
  const ok = !!cond;
  if (ok) passed++; else failed++;
  console.log(`${ok ? "✓" : "✗ FAIL"} ${name}${ok ? "" : (extra ? " — " + extra : "")}`);
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function waitFor(fn, desc, timeout = 7000) {
  const t0 = Date.now();
  let last;
  while (Date.now() - t0 < timeout) {
    try { const v = await fn(); if (v) return v; } catch (e) { last = e; }
    await sleep(60);
  }
  throw new Error(`timeout waiting for: ${desc}${last ? " (" + last.message + ")" : ""}`);
}

/* ---------- raw HTTP helper (Node side, for API-level assertions) ---------- */
const reqLog = [];   // last N requests — printed if the harness dies mid-flight
function rawFetch(method, url, { headers = {}, body = null } = {}) {
  return new Promise((resolve, reject) => {
    const full = url.startsWith("http") ? url : BASE + url;
    const data = body == null ? null : (typeof body === "string" ? body : JSON.stringify(body));
    const req = http.request(full, { method, headers: { "Content-Type": "application/json", ...headers } }, (res) => {
      let buf = "";
      res.on("data", c => buf += c);
      res.on("end", () => {
        reqLog.push(`${method} ${url} -> ${res.statusCode}`);
        if (reqLog.length > 400) reqLog.shift();
        resolve({ status: res.statusCode, text: buf, json: () => { try { return JSON.parse(buf); } catch { return {}; } } });
      });
    });
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}

// A page-side unhandled rejection must not nuke the runner — record it as a
// page error (checked at the end) instead of a fatal crash.
process.on("uncaughtException", (e) => { pageErrorsSink.push("uncaught(page): " + e.message); });
process.on("unhandledRejection", (e) => { pageErrorsSink.push("unhandled(page): " + (e && e.message || e)); });
const pageErrorsSink = [];

/* ---------- TOTP (for the admin 2FA re-confirmation checks) ---------- */
function base32decode(s) {
  const A = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0, val = 0; const out = [];
  for (const c of s.replace(/=+$/, "").toUpperCase()) {
    const i = A.indexOf(c); if (i < 0) continue;
    val = (val << 5) | i; bits += 5;
    if (bits >= 8) { out.push((val >> (bits - 8)) & 0xff); bits -= 8; }
  }
  return Buffer.from(out);
}
function totp(secret, when = Date.now()) {
  const key = base32decode(secret);
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(Math.floor(when / 1000 / 30)));
  const h = crypto.createHmac("sha1", key).update(buf).digest();
  const off = h[h.length - 1] & 0xf;
  return ((h.readUInt32BE(off) & 0x7fffffff) % 1e6).toString().padStart(6, "0");
}

/* ============================ boot the DOM ============================ */
async function main() {
  const { JSDOM, VirtualConsole } = require("jsdom");

  let html = fs.readFileSync(htmlPath, "utf8");
  const proSrc = fs.readFileSync(proPath, "utf8");
  const cssSrc = fs.readFileSync(cssPath, "utf8");

  // no resource loader: drop external tags, inline the CSS
  html = html
    .replace(/<script[^>]*src=["'][^"']*static[^"']*["'][^>]*>\s*<\/script>/g, "")
    .replace(/<link[^>]*rel=["']stylesheet["'][^>]*>/g, "");
  html = html.replace("</head>", `<style>\n${cssSrc}\n</style>\n</head>`);

  const vc = new VirtualConsole();
  const pageErrors = pageErrorsSink;
  vc.on("jsdomError", (e) => {
    const m = String((e && e.message) || e);
    if (m.includes("Could not parse CSS") || m.includes("Could not load")) return;  // resource-loader noise
    if (m.includes("Not implemented")) return;  // window.open etc. — stubbed where it matters
    pageErrors.push(m);
  });
  vc.on("error", (m) => pageErrors.push("console.error: " + m));

  const dom = new JSDOM(html, {
    url: BASE + "/",
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(window) {
      window.__errs = pageErrors;
      window.addEventListener("error", (e) => pageErrors.push("window.onerror: " + e.message));
      window.addEventListener("unhandledrejection", (e) => pageErrors.push("unhandledrejection: " + (e.reason && e.reason.message || e.reason)));
      window.fetch = (url, opts = {}) => rawFetch(opts.method || "GET", url, { headers: opts.headers || {}, body: opts.body })
        .then(r => ({ status: r.status, ok: r.status >= 200 && r.status < 300, json: async () => r.json(), text: async () => r.text,
                      headers: { get: () => null } }));
      window.open = () => null;
      window.scrollTo = () => {};
      window.HTMLElement.prototype.scrollIntoView = function () {};
      try { Object.defineProperty(window.navigator, "clipboard", { value: { writeText: async () => {} }, configurable: true }); } catch (e) {}
      window.EventSource = class {
        constructor(url) { this.url = url; this.readyState = 1; setTimeout(() => this.onopen && this.onopen(), 5); }
        addEventListener() {} removeEventListener() {} close() { this.readyState = 2; }
      };
      if (!window.matchMedia) window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
    },
  });

  const { window } = dom;
  const { document } = window;
  // FACT #1: eval synchronously, before ANY await.
  window.eval(proSrc);

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const visible = (el) => !!el && !el.classList.contains("hidden") && el.style.display !== "none";
  const txt = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");
  const bodyTxt = () => txt(document.body);
  const toastTxt = () => txt($("#toastContainer"));
  const dispatch = (el, type, opts = {}) => el.dispatchEvent(new window.Event(type, { bubbles: true, ...opts }));

  check("boot: zero page errors during parse+boot", pageErrors.length === 0, pageErrors[0]);

  /* ---------------- 1. LANDING (signed out) ---------------- */
  await waitFor(() => visible($("#screen-landing")), "landing screen after boot");
  check("landing screen shown when signed out", visible($("#screen-landing")));
  check("top nav visible", visible($(".nav")));

  const heroH1 = txt($(".hero h1") || $("h1"));
  const heroSub = txt($(".hero .sub, .hero p, .hero-sub") || $(".hero"));
  check("hero headline sells the pitch", /telegram bot/i.test(heroH1), heroH1);
  check("hero says free", /free/i.test(heroH1 + " " + heroSub));
  check("sub explains the flow (paste code → deploy → URL)", /paste/i.test(heroSub) && /url/i.test(heroSub), heroSub.slice(0, 140));

  const landingTxt = bodyTxt();
  check("trust: live URL in seconds", /live url in seconds/i.test(landingTxt));
  check("trust: libraries auto-install", /libraries auto-install/i.test(landingTxt));
  check("trust: no lock-in", /no lock-in/i.test(landingTxt));
  check("NO vault-era wording anywhere", !/vault|password manager|seed phrase|wifi|bookmark/i.test(landingTxt));
  check("CTA: create free account", /create free account/i.test(landingTxt));
  check("CTA: sign in", /sign in/i.test(landingTxt));

  if (EXPECT_LANDING) {
    check("sub names the real languages (Python/JS/Bash/Ruby/PHP)",
      /python/i.test(heroSub) && /javascript/i.test(heroSub) && /bash/i.test(heroSub) && /ruby/i.test(heroSub) && /php/i.test(heroSub), heroSub.slice(0, 160));
    const featCards = $$(".section .fc, .section .feat-card, #features .fc").length;
    check("landing: exactly 6 feature cards", featCards === 6, `found ${featCards}`);
    const chat = $(".demo-chat");
    check("landing: demo chat panel exists", !!chat);
    check("landing: demo chat has 6 bubbles", chat ? $$(".demo-chat .msg, .demo-chat .bubble, .demo-chat .dc-msg").length === 6 : false);
    check("landing: typing indicator in demo", !!$(".dc-typing"));
    check("landing: demo shows a /live/ URL", /\/live\//.test(txt($(".dc-url")) || landingTxt));
    check("landing: 3 how-it-works steps", $$(".how-step").length === 3, `found ${$$(".how-step").length}`);
    const price = txt($(".gs-price"));
    check("landing: free-tier card states 3 running apps", /3 running apps\s*·\s*always on/i.test(price), price);
    check("landing: no card required", /no card/i.test(price));
    check("landing: 'Start building free' CTA", /start building free/i.test(landingTxt));
  } else {
    console.log("   … landing-detail checks S3-pending (EXPECT_LANDING=1 to enable)");
  }

  /* ---------------- 2. AUTH UI ---------------- */
  const signinLink = $$("a, button").find(b => /sign in/i.test(txt(b)) && visible(b));
  signinLink.click();
  await waitFor(() => visible($("#screen-signin")), "sign-in screen");
  check("nav Sign in → sign-in screen", visible($("#screen-signin")));

  check("signup screen has ToS checkbox", !!$("#su_terms"));
  check("signup ToS links to /terms", !!$$("#screen-signup a").find(a => (a.getAttribute("href") || "").includes("/terms")));

  const av = await rawFetch("POST", "/auth/check-availability", { body: { username: "boss", email: "boss@t.dev" } });
  check("check-availability flags taken username+email", av.json().username_taken === true && av.json().email_taken === true);

  // wrong password → visible error, still on sign-in
  $("#si_username").value = "regular1";
  $("#si_password").value = "wrong-pass-999";
  $("#btnSignin").click();
  await waitFor(() => /incorrect/i.test(toastTxt()), "wrong-password toast");
  check("wrong password → error toast", /incorrect/i.test(toastTxt()));
  check("wrong password → still on sign-in", visible($("#screen-signin")));

  // good login through the REAL form
  $("#si_username").value = "regular1";
  $("#si_password").value = "pass-123";
  $("#btnSignin").click();
  await waitFor(() => visible($("#screen-dashboard")), "dashboard after login");
  check("login regular1 → dashboard", visible($("#screen-dashboard")));
  check("token stored", !!window.localStorage.getItem("ahad_token"));
  const regularToken = window.localStorage.getItem("ahad_token");

  /* ---------------- 3. DASHBOARD STRUCTURE ---------------- */
  check("sidebar .dash-tabs", !!$(".dash-tabs"));
  const groups = $$(".dash-tabs .tab-group").map(txt);
  check("sidebar group: Create", groups.some(g => /create/i.test(g)));
  check("sidebar group: Account", groups.some(g => /account/i.test(g)));
  check("sidebar tab: overview", !!$('.dash-tab[data-tab="overview"]'));
  check("sidebar tab: code", !!$('.dash-tab[data-tab="code"]'));
  check("sidebar tab: jobs (RunSpace)", !!$('.dash-tab[data-tab="jobs"]'));
  check("sidebar separator .tab-sep", !!$(".dash-tabs .tab-sep"));
  check("sidebar: Activity launcher", !!$("#btnActivitySide"));
  check("sidebar tab: profile", !!$('.dash-tab[data-tab="profile"]'));
  check("admin tab hidden for regular user", $("#tabBtnAdmin") && $("#tabBtnAdmin").classList.contains("hidden"));
  check("admin panel section does NOT exist in DOM for regular user", !$("#tab-admin"));
  window.eval("switchTab('admin')");
  check("switchTab('admin') no-ops for regular user (stealth)", $("#tab-overview").classList.contains("active"));

  const bnTabs = $$("#bottomNav .bn-item").map(b => b.dataset.tab || b.id);
  check("bottom-nav exists", !!$("#bottomNav"));
  check("bottom-nav: overview", bnTabs.includes("overview"));
  check("bottom-nav: code", bnTabs.includes("code"));
  check("bottom-nav: search button", !!$("#bnSearch"));
  check("bottom-nav: jobs", bnTabs.includes("jobs"));
  check("bottom-nav: more", bnTabs.includes("more"));

  ["statJobs", "statLive", "statSnippets", "statPublished"].forEach(id =>
    check(`stat card #${id}`, !!document.getElementById(id)));
  check("stat labels: hosting product (Total apps / Published pages)", /total apps/i.test(bodyTxt()) && /published pages/i.test(bodyTxt()));
  ["Code Studio", "Deploy 24/7", "Publish a page", "Activity log"].forEach(q =>
    check(`quick action: ${q}`, bodyTxt().includes(q)));
  const featCards = $$("#tab-overview .feat-card");
  check("overview: exactly 2 feature cards (Code Studio + RunSpace)", featCards.length === 2, `found ${featCards.length}`);
  await waitFor(() => /^\d+$/.test(txt($("#statJobs"))), "stats loaded");
  check("stats counters numeric", /^\d+$/.test(txt($("#statJobs"))) && /^\d+$/.test(txt($("#statLive"))));

  /* ---------------- 4. CODE STUDIO ---------------- */
  $$('.dash-tab[data-tab="code"]')[0].click();
  await sleep(120);
  check("Code tab active", $("#tab-code").classList.contains("active"));
  check("URL pushed to /code", window.location.pathname === "/code", window.location.pathname);
  check("editor title input", !!$("#snippetTitle"));
  check("editor code area / CodeMirror", !!$("#snippetContent"));
  check("editor language select", !!$("#snippetLanguage"));
  check("RunSpace terminal panel (#ahTerm)", !!$("#ahTerm"));
  check("RunSpace save button (#btnSaveSnippet)", !!$("#btnSaveSnippet"));
  check("RunSpace run button (#btnRunCode)", !!$("#btnRunCode"));

  $("#snippetTitle").value = "e2e-demo-page";
  $("#snippetLanguage").value = "html";
  $("#snippetContent").value = "<h1>e2e-marker-42</h1><p>hello from the harness</p>";
  $("#btnSaveSnippet").click();
  // wait for the REAL row (not the _skel placeholders) before asserting
  await waitFor(() => $$("#snippetsList .snippet-item").length >= 1, "snippet row listed", 8000);
  check("snippet appears in library list", $$("#snippetsList .snippet-item").length >= 1);
  check("library shows the title", /e2e-demo-page/.test(txt($("#snippetsList"))), txt($("#snippetsList")).slice(0, 100));

  $("#btnShareSnippet").click();
  await waitFor(() => $("#pubBar") && $("#pubBar").style.display !== "none", "publish bar");
  const pubHref = ($(".pub-link") || {}).href || txt($(".pub-link"));
  check("publish → visible pub bar with /s/ link", /\/s\//.test(pubHref), pubHref);
  const pubTok = (pubHref.match(/\/s\/([A-Za-z0-9_\-]+)/) || [])[1];
  const pubPage = await rawFetch("GET", `/s/${pubTok}`);
  check("published page reachable with NO auth", pubPage.status === 200 && /e2e-marker-42/.test(pubPage.text));

  /* ---------------- 5. SEARCH PALETTE ---------------- */
  $("#bnSearch").click();
  await waitFor(() => !$("#cmdOverlay").classList.contains("hidden"), "palette opens");
  check("command palette opens", !$("#cmdOverlay").classList.contains("hidden"));
  $("#cmdInput").value = "e2e-demo";
  dispatch($("#cmdInput"), "input");
  await waitFor(() => /e2e-demo-page/.test(txt($("#cmdResults") || $("#cmdOverlay"))), "palette results", 8000);
  check("palette finds the saved snippet", /e2e-demo-page/.test(txt($("#cmdOverlay"))));
  dispatch($("#cmdInput"), "keydown", { key: "Escape" });
  const kd = new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true });
  $("#cmdInput").dispatchEvent(kd);
  await waitFor(() => $("#cmdOverlay").classList.contains("hidden"), "palette closes", 4000);
  check("Escape closes the palette", $("#cmdOverlay").classList.contains("hidden"));

  /* ---------------- 6. RUNSPACE TAB ---------------- */
  $$('.dash-tab[data-tab="jobs"]')[0].click();
  await sleep(150);
  check("RunSpace tab active", $("#tab-jobs").classList.contains("active"));
  const startBtn = $("#btnStartJob");
  check("Deploy 24/7 button present", !!startBtn && /deploy 24\/7/i.test(txt(startBtn)));
  await waitFor(() => /nothing deployed/i.test(txt($("#jobsList"))) || $$("#jobsList .job-card").length > 0, "jobs list settles", 8000);
  check("regular1 sees empty jobs state", /nothing deployed/i.test(txt($("#jobsList"))), txt($("#jobsList")).slice(0, 80));

  /* ---------------- 7. PROFILE + ACTIVITY + LOGOUT ---------------- */
  $$('.dash-tab[data-tab="profile"]')[0].click();
  await sleep(150);
  check("Profile tab active", $("#tab-profile").classList.contains("active"));
  const tfaStatus = await rawFetch("GET", "/2fa/status", { headers: { Authorization: "Bearer " + regularToken } });
  check("regular1 2FA status = disabled", tfaStatus.json().enabled === false);

  $("#btnActivitySide").click();
  await sleep(150);
  check("activity panel opens", $("#activityPanel").classList.contains("open"));
  check("activity overlay shown", !$("#activityOverlay").classList.contains("hidden"));
  $("#activityClose").click();

  // admin stealth — while regular1's token is still alive
  const adm404 = await rawFetch("GET", "/admin/overview", { headers: { Authorization: "Bearer " + regularToken } });
  check("non-admin /admin/overview → 404 (stealth)", adm404.status === 404);

  // CGNAT regression: hammer /api/execute from ONE ip — an IP-keyed bucket
  // would 429 after 6 calls; per-account limit must let a normal user work.
  let hammer429 = 0, hammerOther = 0;
  for (let i = 0; i < 12; i++) {
    const r = await rawFetch("POST", "/api/execute",
      { headers: { Authorization: "Bearer " + regularToken }, body: { language: "python", code: "print(1)" } });
    if (r.status === 429) hammer429++; else hammerOther++;
  }
  check("12 rapid execs from one IP are NOT IP-limited (CGNAT fix)", hammer429 === 0, `${hammer429}×429`);

  $("#btnLogout").click();
  await waitFor(() => visible($("#screen-landing")), "landing after logout");
  check("logout → landing + token cleared", visible($("#screen-landing")) && !window.localStorage.getItem("ahad_token"));
  const reLogout = await rawFetch("POST", "/logout", { headers: { Authorization: "Bearer " + regularToken } });
  check("logout is idempotent (dead token → 200, no 401 wall)", reLogout.status === 200, `got ${reLogout.status}`);

  /* ---------------- 8. ADMIN: panel, 2FA-guarded actions ---------------- */

  // boss logs in through the REAL form too
  $$("a, button").find(b => /sign in/i.test(txt(b)) && visible(b)).click();
  await waitFor(() => visible($("#screen-signin")), "sign-in screen for boss");
  $("#si_username").value = "boss";
  $("#si_password").value = "pass-123";
  $("#btnSignin").click();
  await waitFor(() => visible($("#screen-dashboard")), "boss dashboard");
  await waitFor(() => !$("#tabBtnAdmin").classList.contains("hidden"), "admin tab visible");
  check("boss sees the Admin tab (granted via ADMIN_EMAILS)", !$("#tabBtnAdmin").classList.contains("hidden"));
  check("boss: admin panel section EXISTS in DOM", !!$("#tab-admin"));
  const bossToken = window.localStorage.getItem("ahad_token");
  check("boss token differs from regular1", !!bossToken && bossToken !== regularToken);

  await waitFor(() => txt($("#statJobs")) === "2", "boss stats loaded");
  check("boss stats: 2 total apps", txt($("#statJobs")) === "2");
  check("boss stats: 1 deployed (rid-abc)", txt($("#statLive")) === "1");

  $$('.dash-tab[data-tab="jobs"]')[0].click();
  await waitFor(() => $$("#jobsList .job-card").length === 2, "boss's 2 job cards", 8000);
  const jobsTxt = txt($("#jobsList"));
  check("boss sees shilo-bot + draft-api", /shilo-bot/.test(jobsTxt) && /draft-api/.test(jobsTxt));
  check("job cards leak NO code", !/print\(|never deployed'/.test(jobsTxt));

  const apiJobs = await rawFetch("GET", "/api/jobs", { headers: { Authorization: "Bearer " + bossToken } });
  const jobRows = apiJobs.json().jobs || [];
  check("GET /api/jobs → 2 rows, code field stripped", jobRows.length === 2 && jobRows.every(j => !("code" in j)));

  const draftRow = jobRows.find(j => j.name === "draft-api");
  const logs = await rawFetch("GET", `/api/jobs/${draftRow.id}/logs`, { headers: { Authorization: "Bearer " + bossToken } });
  check("never-started job logs read '(never started)'", /never started/.test(logs.json().logs || ""));

  // admin panel UI
  $("#tabBtnAdmin").click();
  await waitFor(() => $$("#admStats .adm-stat").length >= 5, "admin stats chips", 9000);
  check("admin: 5 stat chips (users/verified/suspended/live/capacity)", $$("#admStats .adm-stat").length >= 5);
  check("admin: capacity line (max per user)", /capacity:/.test(txt($("#admCap"))) && /\/user/.test(txt($("#admCap"))), txt($("#admCap")));
  check("admin: 14-day signup spark", $$("#admSpark .adm-bar").length === 14, `${$$("#admSpark .adm-bar").length} bars`);
  check("admin: user table lists boss + regular1", /boss/.test(txt($("#admUsers"))) && /regular1/.test(txt($("#admUsers"))));
  check("admin: job table metadata only — NO code", /shilo-bot/.test(txt($("#admJobs"))) && !/print\(/.test($("#admJobs").innerHTML));

  // 2FA-guarded suspend via API (same contract the modal uses)
  const users = (await rawFetch("GET", "/admin/users", { headers: { Authorization: "Bearer " + bossToken } })).json().users || [];
  const reg = users.find(u => u.username === "regular1");
  const badTfa = await rawFetch("POST", "/admin/users/set-suspended",
    { headers: { Authorization: "Bearer " + bossToken }, body: { user_id: reg.id, suspended: true, code: "000000" } });
  check("suspend without valid 2FA → refused", badTfa.status === 400);
  const sus = await rawFetch("POST", "/admin/users/set-suspended",
    { headers: { Authorization: "Bearer " + bossToken }, body: { user_id: reg.id, suspended: true, code: totp("JBSWY3DPEHPK3PXP") } });
  check("suspend WITH own TOTP → 200", sus.status === 200, sus.text.slice(0, 120));
  const blocked = await rawFetch("POST", "/login", { body: { username: "regular1", password: "pass-123" } });
  check("suspended account cannot sign in (403)", blocked.status === 403, `got ${blocked.status}`);
  const audit = (await rawFetch("GET", "/admin/audit-log", { headers: { Authorization: "Bearer " + bossToken } })).json().audit || [];
  check("audit trail recorded the suspend", audit.some(a => /suspend/i.test(a.action || "")));
  const unsus = await rawFetch("POST", "/admin/users/set-suspended",
    { headers: { Authorization: "Bearer " + bossToken }, body: { user_id: reg.id, suspended: false, code: totp("JBSWY3DPEHPK3PXP") } });
  check("reactivate WITH TOTP → 200", unsus.status === 200, unsus.text.slice(0, 120));
  const reLogin = await rawFetch("POST", "/login", { body: { username: "regular1", password: "pass-123" } });
  check("reactivated account signs in again", reLogin.status === 200);

  /* ---------------- 9. VAULT-ERA URLS ARE GONE + MISC ---------------- */
  for (const dead of ["/vault", "/notes", "/bookmarks", "/notifications", "/export-data", "/qr"]) {
    const r = await rawFetch("GET", dead, { headers: { Authorization: "Bearer " + bossToken } });
    check(`dead endpoint ${dead} → 404 even authed`, r.status === 404);
  }
  const terms = await rawFetch("GET", "/terms");
  check("/terms serves the Terms of Use", terms.status === 200 && /terms/i.test(terms.text));
  const health = await rawFetch("GET", "/health");
  check("/health 200", health.status === 200);

  /* ---------------- 10. FINAL: nothing broke under our feet ---------------- */
  check("zero page errors end-to-end", pageErrors.length === 0, pageErrors.slice(0, 3).join(" | "));

  console.log(`\n${passed} passed, ${failed} failed (${passed + failed} checks)`);
  const hit401 = reqLog.filter(l => l.endsWith("-> 401"));
  if (hit401.length) console.log("requests that got 401:\n  " + hit401.join("\n  "));
  if (failed) console.log("last requests:\n  " + reqLog.slice(-15).join("\n  "));
  window.close();
  process.exit(failed ? 1 : 0);
}

main().catch(e => { console.error("HARNESS CRASH:", e); process.exit(2); });
