/* Corpus — shell: helpers, hash router, theme, toast, action delegation.
   Loaded first; create.js / database.js / settings.js hang off it. */
"use strict";

/* ---------------- helpers ---------------- */
const el = id => document.getElementById(id);
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ESCAPES[c]);

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    throw new Error(j.error
      ? j.error + (j.details && j.details.length ? ": " + j.details.join("; ") : "")
      : "server error (HTTP " + r.status + ")");
  }
  return j;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/* run ids look like 20260724-183736-a1b2c3 */
function fmtWhen(id) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/.exec(id || "");
  if (!m) return "";
  return +m[3] + " " + MONTHS[+m[2] - 1] + " " + m[1] + ", " + m[4] + ":" + m[5];
}

function fmtBytes(n) {
  if (!n && n !== 0) return "—";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}

const fmtNum = n => (n === null || n === undefined || n === "") ? "—"
  : Number(n).toLocaleString("en-US");

function icon(name, size) {
  return '<svg width="' + (size || 15) + '" height="' + (size || 15) +
         '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
}

/* status word + colour mark; colour never carries the meaning alone */
const STATUS_WORDS = {
  ok: "ok", reviewed: "reviewed", needs_review: "needs review",
  pending_llm: "pending", no_text: "no text", llm_failed: "model failed",
  validation_failed: "invalid reply", done: "done", running: "running",
  failed: "failed", queued: "queued",
};
function statusHTML(s) {
  const key = String(s || "").trim();
  return '<span class="st st-' + esc(key) + '">' +
         esc(STATUS_WORDS[key] || key || "—") + "</span>";
}

let _toastT = null;
function toast(msg) {
  const t = el("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(_toastT);
  _toastT = setTimeout(() => t.classList.remove("show"), 3600);
}

function busy(button, on) {
  if (button) button.classList.toggle("busy", !!on);
}

function callout(kind, html, iconName) {
  return '<div class="callout ' + kind + '">' +
    icon(iconName || (kind === "bad" ? "warn" : kind === "warn" ? "warn"
         : kind === "good" ? "check" : "info"), 16) +
    "<div>" + html + "</div></div>";
}

/* ---------------- action delegation ---------------- */
const ACTS = {};
function onAct(name, fn) { ACTS[name] = fn; }

document.addEventListener("click", e => {
  const t = e.target.closest("[data-act]");
  if (!t) return;
  const fn = ACTS[t.dataset.act];
  if (!fn) return;
  e.preventDefault();
  fn(t.dataset, t, e);
});
document.addEventListener("keydown", e => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const t = e.target.closest("[data-act][tabindex]");
  if (!t || !ACTS[t.dataset.act]) return;
  e.preventDefault();
  ACTS[t.dataset.act](t.dataset, t, e);
});

/* ---------------- theme ---------------- */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  el("themelabel").textContent = t === "dark" ? "Light" : "Dark";
  el("themeicon").innerHTML = '<use href="#i-' + (t === "dark" ? "sun" : "moon") + '"/>';
}
function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem("corpus-theme", next);
  applyTheme(next);
}
applyTheme(localStorage.getItem("corpus-theme") ||
  (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));

/* ---------------- settings cache ---------------- */
let settingsCache = null;
async function refreshSettings() {
  try { settingsCache = await api("/api/settings"); }
  catch { settingsCache = null; }
  paintEndpoint();
  return settingsCache;
}
function paintEndpoint() {
  const s = settingsCache;
  const box = el("rail-endpoint-val");
  if (!s) { box.textContent = "unavailable"; return; }
  if (s.is_mock) {
    box.innerHTML = '<span class="st st-needs_review"></span>Test mode (mock)';
    el("rail-endpoint").title = "Mock backend — fake values, no network call. " +
      "The real endpoint is configured on the server.";
    return;
  }
  box.innerHTML = '<span class="st st-ok"></span>' + esc(s.llm_model);
  el("rail-endpoint").title = s.llm_model +
    " — configured on the server (webapp_data/settings.json)";
}

/* ---------------- slide-over drawer (records, schema library) ---------------- */
let drawerOpener = null;

function openDrawerHTML(title, bodyHTML, footHTML, navHTML) {
  drawerOpener = document.activeElement;
  el("drawer-title").textContent = title;
  el("drawer-body").innerHTML = bodyHTML;
  el("drawer-nav").innerHTML = navHTML || "";
  el("drawer-foot").innerHTML = footHTML || "";
  el("drawer-foot").hidden = !footHTML;
  el("drawer").classList.add("open");
  el("scrim").classList.add("open");
  el("drawer-body").scrollTop = 0;
  el("drawer-close").focus();
}

function closeDrawer() {
  if (!el("drawer").classList.contains("open")) return;
  el("drawer").classList.remove("open");
  el("scrim").classList.remove("open");
  if (drawerOpener && drawerOpener.isConnected && drawerOpener.focus) drawerOpener.focus();
  drawerOpener = null;
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeDrawer();
});

/* ---------------- router ---------------- */
const VIEWS = ["corpora", "corpus", "new", "settings"];

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function showView(name) {
  for (const v of VIEWS) el("view-" + v).hidden = (v !== name);
  const navFor = { corpora: "corpora", corpus: "corpora", new: "new",
                   settings: "settings" }[name];
  for (const n of ["corpora", "new", "settings"]) {
    const b = el("nav-" + n);
    if (n === navFor) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  }
  window.scrollTo(0, 0);
}

function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const head = parts[0] || "corpora";
  closeDrawer();
  if (head === "new") { showView("new"); enterCreate(); return; }
  if (head === "settings") { showView("settings"); enterSettings(); return; }
  if (head === "corpus" && parts[1]) {
    showView("corpus");
    openCorpus(parts[1], parts[2] || "records");
    return;
  }
  showView("corpora");
  loadLedger();
}

window.addEventListener("hashchange", route);

/* ---------------- chart tooltip ---------------- */
document.addEventListener("mouseover", e => {
  const src = e.target.closest("[data-tip]");
  const tip = el("tip");
  if (!src) { tip.style.opacity = 0; return; }
  tip.textContent = src.dataset.tip;
  tip.style.opacity = 1;
});
document.addEventListener("mousemove", e => {
  const tip = el("tip");
  if (tip.style.opacity === "1") {
    tip.style.left = Math.min(e.clientX + 12, innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = (e.clientY + 16) + "px";
  }
});

/* ---------------- boot ---------------- */
document.addEventListener("DOMContentLoaded", async () => {
  try {
    const h = await api("/healthz");
    el("railver").textContent = "v" + h.version;
  } catch { /* health is cosmetic here — the views report their own errors */ }
  await refreshSettings();
  route();
});
