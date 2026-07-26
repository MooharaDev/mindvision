/* Corpus — the ledger of corpora, one corpus in detail, its tables, charts,
   review workspace and export panel. */
"use strict";

/* ======================= LEDGER ======================= */
let ledgerJobs = [], ledgerTimer = null;

async function loadLedger() {
  try { ledgerJobs = await api("/api/jobs"); }
  catch { el("ledger-body").innerHTML = callout("bad",
    "Could not reach the server."); return; }
  el("nav-tally").textContent = ledgerJobs.length || "";
  el("ledger-banner").innerHTML = (settingsCache && settingsCache.is_mock)
    ? callout("warn", "<strong>Test mode is active.</strong> New corpora are " +
      "filled with fake placeholder values — the mock backend never reads your " +
      'PDFs. Point Corpus at your internal model in <a href="#/settings">' +
      "Settings</a>.")
    : "";
  renderLedger();
  clearTimeout(ledgerTimer);
  if (!el("view-corpora").hidden && ledgerJobs.some(j => j.status === "running"))
    ledgerTimer = setTimeout(loadLedger, 3000);
}

function renderLedger() {
  const q = (el("ledger-q").value || "").trim().toLowerCase();
  const rows = ledgerJobs.filter(j => !q ||
    (j.title + " " + j.source + " " + j.table).toLowerCase().includes(q));
  el("ledger-count").textContent = ledgerJobs.length
    ? (q ? rows.length + " of " + ledgerJobs.length : ledgerJobs.length) : "";

  let recs = 0, docs = 0, debt = 0;
  for (const j of ledgerJobs) {
    const s = j.summary || {};
    recs += s.n_records || 0;
    docs += s.n_pdfs || 0;
    debt += s.n_review_queue || 0;
  }
  el("ledger-sub").innerHTML = ledgerJobs.length
    ? fmtNum(ledgerJobs.length) + " corpora · " + fmtNum(recs) +
      " records extracted from " + fmtNum(docs) + " documents · " +
      (debt ? '<b class="num">' + fmtNum(debt) + "</b> awaiting review"
            : "nothing awaiting review")
    : "Every database built from an archive of documents.";

  if (!ledgerJobs.length) {
    el("ledger-body").innerHTML = '<div class="empty">' + icon("corpus", 34) +
      "<h3>No corpora yet</h3><p>Point Corpus at a folder of PDFs. It reads the " +
      "documents, drafts a table from what it finds, and builds a database you " +
      "can query, review and train on.</p>" +
      '<a class="btn primary lg" href="#/new">' + icon("plus", 15) +
      "Build your first corpus</a></div>";
    return;
  }
  if (!rows.length) {
    el("ledger-body").innerHTML = '<div class="empty"><h3>Nothing matches ' +
      esc(q) + "</h3><p>Corpora are matched on their name, source folder and " +
      "table name.</p></div>";
    return;
  }
  el("ledger-body").innerHTML =
    '<div class="tablewrap"><table class="tbl ledger"><thead><tr>' +
    '<th><span class="sortbtn">Corpus</span></th>' +
    '<th><span class="sortbtn">Status</span></th>' +
    '<th class="n"><span class="sortbtn">Records</span></th>' +
    '<th class="n"><span class="sortbtn">PDFs</span></th>' +
    '<th class="n"><span class="sortbtn">Review</span></th>' +
    '<th><span class="sortbtn">Model</span></th>' +
    '<th><span class="sortbtn">Built</span></th>' +
    "</tr></thead><tbody>" +
    rows.map(j => {
      const s = j.summary || {};
      const debt = s.n_review_queue || 0;
      return '<tr tabindex="0" data-act="open-corpus" data-id="' + esc(j.id) + '">' +
        '<td><span class="name">' + esc(j.title || j.table) + "</span>" +
        '<span class="src">' + esc(j.source ? j.source + "/" : "") +
        esc(j.table) + "</span></td>" +
        "<td>" + statusHTML(j.status) +
        (j.status === "running" ? ' <span class="hint num">' + j.pct + "%</span>" : "") +
        "</td>" +
        '<td class="n">' + fmtNum(s.n_records) + "</td>" +
        '<td class="n">' + fmtNum(s.n_pdfs ?? j.n_files) + "</td>" +
        '<td class="n">' + (debt ? '<span class="debt">' + debt + "</span>"
          : (j.status === "done" ? '<span class="hint">clear</span>' : "—")) + "</td>" +
        "<td>" + (j.is_mock ? '<span class="hint">mock</span>'
          : '<span class="hint">' + esc(j.model || "—") + "</span>") + "</td>" +
        '<td class="hint">' + esc(fmtWhen(j.id)) + "</td></tr>";
    }).join("") + "</tbody></table></div>";
}

onAct("open-corpus", d => go("#/corpus/" + d.id));

/* ======================= ONE CORPUS ======================= */
let cid = null, cjob = null, cmeta = null, cschema = null, ctab = "records";
let tableData = null, tOffset = 0, tQuery = "", tStatus = "", tAllCols = false;
let sortCol = -1, sortDir = 1, queryTimer = null, headerTimer = null;
const T_LIMIT = 200;

const TABLE_TABS = ["records", "review_queue", "images", "pdfs", "issues"];
const PIPELINE_COLS = ["source_paths", "n_pdfs", "n_pages", "chars_extracted",
  "truncated", "low_yield", "scanned_suspect", "review_reasons", "llm_model",
  "llm_attempts", "extracted_at", "n_images", "_evidence", "_notes"];

async function openCorpus(id, tab) {
  const fresh = id !== cid;
  cid = id;
  ctab = tab;
  if (fresh) {
    cjob = cmeta = cschema = tableData = null;
    tOffset = 0; tQuery = ""; tStatus = ""; sortCol = -1; tAllCols = false;
    el("c-q").value = "";
    el("c-title").textContent = "…";
    el("c-sub").textContent = "";
    el("c-readout").innerHTML = "";
    el("c-panelbody").innerHTML = "";
  }
  paintTabs();
  if (fresh && !(await loadCorpusHeader())) return;
  renderTab();
}

async function loadCorpusHeader() {
  clearTimeout(headerTimer);
  try {
    cjob = await api("/api/jobs/" + cid + "/status");
  } catch {
    el("c-title").textContent = "Corpus not found";
    el("c-panelbody").innerHTML = '<div class="empty"><h3>This corpus is gone</h3>' +
      "<p>It may have been deleted, or the server was restarted with a different " +
      'data directory.</p><a class="btn" href="#/corpora">Back to all corpora</a></div>';
    return false;
  }
  [cmeta, cschema] = await Promise.all([
    api("/api/jobs/" + cid + "/meta").catch(() => null),
    api("/api/jobs/" + cid + "/schema").catch(() => null),
  ]);
  paintHeader();
  if (cjob.status === "running") {
    headerTimer = setTimeout(() => {
      if (cid && !el("view-corpus").hidden) { loadCorpusHeader(); renderTab(); }
    }, 2500);
  }
  return true;
}

function paintHeader() {
  const j = cjob, s = j.summary || {};
  const title = j.title || j.table;
  el("c-crumb").textContent = title;
  el("c-title").textContent = title;
  document.title = title + " · Corpus";
  el("c-sub").innerHTML = statusHTML(j.status) +
    (j.status === "running" ? ' <span class="num">' + j.pct + "%</span>" : "") +
    ' <span class="hint">· table <code>' + esc(j.table) + "</code> · built " +
    esc(fmtWhen(j.id)) + (j.model ? " · " + esc(j.is_mock ? "mock backend" : j.model) : "") +
    "</span>";

  const debt = s.n_review_queue || 0;
  const canRetry = j.status !== "running" && (j.status === "failed" ||
    (s.status_counts && (s.status_counts.llm_failed ||
     s.status_counts.validation_failed || s.status_counts.pending_llm)));
  el("c-acts").innerHTML =
    (debt ? '<button class="btn primary" data-act="start-review">' +
      icon("check", 15) + "Review " + debt + " record" + (debt === 1 ? "" : "s") +
      "</button>" : "") +
    (canRetry ? '<button class="btn" data-act="retry-run">' + icon("retry", 14) +
      "Retry failed</button>" : "") +
    '<button class="iconbtn" data-act="rename" aria-label="Rename this corpus" ' +
    'title="Rename">' + icon("pencil", 16) + "</button>";

  el("c-readout").innerHTML = j.summary ? (
    readoutItem("Records", fmtNum(s.n_records)) +
    readoutItem("Documents", fmtNum(s.n_pdfs)) +
    readoutItem("Needs review", fmtNum(debt), debt ? "attn" : "") +
    readoutItem("Issues", fmtNum(s.n_issues), s.n_issues ? "attn" : "") +
    (s.status_counts ? readoutItem("Extracted cleanly",
      fmtNum((s.status_counts.ok || 0) + (s.status_counts.reviewed || 0)) +
      ' <small>of ' + fmtNum(s.n_records) + "</small>") : "")
  ) : '<div><dt>Status</dt><dd style="font-size:var(--t-md)">' +
      esc(j.error || "no summary yet") + "</dd></div>";

  const alerts = [];
  if (j.status === "failed" && j.error)
    alerts.push(callout("bad", "<strong>The run failed.</strong> " + esc(j.error)));
  if (j.is_mock)
    alerts.push(callout("warn", "<strong>Built in test mode.</strong> Every value " +
      "in this corpus is a mock placeholder, not something read from the PDFs."));
  el("c-alerts").innerHTML = alerts.join("");
}

function readoutItem(label, value, cls) {
  return "<div><dt>" + esc(label) + '</dt><dd class="' + (cls || "") + '">' +
         value + "</dd></div>";
}

function paintTabs() {
  for (const b of el("c-tabs").querySelectorAll("button"))
    b.setAttribute("aria-pressed", String(b.dataset.tab === ctab));
  el("c-searchbox").hidden = !TABLE_TABS.includes(ctab);
  el("c-filters").hidden = ctab !== "records";
}

function pickTab(tab) { go("#/corpus/" + cid + "/" + tab); }

function renderTab() {
  paintTabs();
  if (ctab === "overview") return renderOverview();
  if (ctab === "export") return renderExport();
  tOffset = 0;
  sortCol = -1;
  if (ctab !== "records") tStatus = "";
  renderFilters();
  loadTable();
}

/* ---------- status filter chips ---------- */
function renderFilters() {
  if (ctab !== "records" || !cjob || !cjob.summary) {
    el("c-filters").innerHTML = "";
    return;
  }
  const counts = cjob.summary.status_counts || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const chip = (key, label, n) =>
    '<button class="chip" data-act="filter-status" data-status="' + esc(key) +
    '" aria-pressed="' + (tStatus === key) + '">' +
    (key ? '<span class="st st-' + esc(key) + '" style="gap:0"></span>' : "") +
    esc(label) + ' <span class="n">' + n + "</span></button>";
  el("c-filters").innerHTML = '<div class="chips">' +
    chip("", "All", total) +
    Object.entries(counts).map(([k, n]) =>
      chip(k, STATUS_WORDS[k] || k, n)).join("") + "</div>";
}

onAct("filter-status", d => {
  tStatus = d.status === tStatus ? "" : d.status;
  tOffset = 0;
  renderFilters();
  loadTable();
});

function queryChanged() {
  clearTimeout(queryTimer);
  queryTimer = setTimeout(() => {
    tQuery = el("c-q").value.trim();
    tOffset = 0;
    loadTable();
  }, 280);
}

/* ---------- tables ---------- */
const EMPTY_TABLE = {
  records: ["No records", "This corpus has no rows yet."],
  review_queue: ["Review queue is clear",
    "Every record cleared the confidence threshold with all required fields " +
    "present. Nothing needs human eyes."],
  images: ["No images",
    "No embedded or loose images were found alongside these documents."],
  pdfs: ["No documents catalogued", "The discover stage found no PDFs."],
  issues: ["No issues",
    "Every file was readable and every record was processed. Nothing was " +
    "skipped silently."],
};

function skeletonTable() {
  el("c-panelbody").innerHTML = '<div class="body">' +
    Array.from({ length: 5 }, (_, i) =>
      '<div class="skel" style="margin:14px 0;width:' + (92 - i * 9) + '%"></div>')
      .join("") + "</div>";
}

async function loadTable() {
  if (!cid) return;
  skeletonTable();
  const url = "/api/jobs/" + cid + "/table/" + ctab + "?offset=" + tOffset +
    "&limit=" + T_LIMIT + (tQuery ? "&q=" + encodeURIComponent(tQuery) : "") +
    (tStatus ? "&status=" + encodeURIComponent(tStatus) : "");
  tableData = await api(url).catch(() => null);
  renderTable();
}

function visibleCols(cols) {
  if (tAllCols || ctab !== "records") return cols.map((_, i) => i);
  return cols.map((c, i) => PIPELINE_COLS.includes(c) ? -1 : i).filter(i => i >= 0);
}

function cellClass(col) {
  if (col === "record_id" || col === "rel_path" || col === "file" ||
      col === "source_paths") return "cell id";
  if (col === "_confidence" || col.startsWith("n_") || col === "size_bytes" ||
      col === "chars_extracted") return "n";
  return "cell";
}

function renderTable() {
  const t = tableData;
  if (!t) {
    el("c-panelbody").innerHTML = '<div class="empty"><h3>Could not load this ' +
      "table</h3><p>The run may still be writing it. Try again in a moment.</p></div>";
    return;
  }
  const [emptyTitle, emptyBody] = EMPTY_TABLE[ctab] || ["Empty", ""];
  if (!t.columns.length || !t.rows.length) {
    const filtered = tQuery || tStatus;
    el("c-panelbody").innerHTML = '<div class="empty">' + icon("table", 30) +
      "<h3>" + (filtered ? "Nothing matches this filter" : esc(emptyTitle)) +
      "</h3><p>" + (filtered
        ? "No rows match " + (tQuery ? "<code>" + esc(tQuery) + "</code>" : "") +
          (tQuery && tStatus ? " and " : "") +
          (tStatus ? "status <b>" + esc(STATUS_WORDS[tStatus] || tStatus) + "</b>" : "") + "."
        : esc(emptyBody) + (t.note ? " <code>" + esc(t.note) + "</code>" : "")) +
      "</p>" + (filtered ? '<button class="btn" data-act="clear-filters">' +
        "Clear filters</button>" : "") + "</div>";
    return;
  }

  const cols = visibleCols(t.columns);
  let order = t.rows.map((_, i) => i);
  if (sortCol >= 0) {
    const numeric = t.rows.every(r => r[sortCol] === "" ||
      /^-?\d+(\.\d+)?$/.test(String(r[sortCol])));
    order.sort((a, b) => {
      const x = t.rows[a][sortCol], y = t.rows[b][sortCol];
      return sortDir * (numeric ? (parseFloat(x) || 0) - (parseFloat(y) || 0)
                                : String(x).localeCompare(String(y)));
    });
  }
  const statusIdx = t.columns.indexOf("status");

  el("c-panelbody").innerHTML = '<div class="tablewrap"><table class="tbl">' +
    "<thead><tr>" + cols.map(i => {
      const c = t.columns[i];
      const num = cellClass(c) === "n";
      return '<th' + (num ? ' class="n"' : "") + ' aria-sort="' +
        (i === sortCol ? (sortDir > 0 ? "ascending" : "descending") : "none") +
        '"><button class="sortbtn" data-act="sort-col" data-i="' + i + '">' +
        esc(c) + (i === sortCol ? ' <span class="arrow">' +
          (sortDir > 0 ? "↑" : "↓") + "</span>" : "") + "</button></th>";
    }).join("") + "</tr></thead><tbody>" +
    order.map(ri => {
      const r = t.rows[ri];
      return '<tr tabindex="0" data-act="open-row" data-i="' + ri + '">' +
        cols.map(i => {
          const v = r[i];
          if (i === statusIdx) return "<td>" + statusHTML(v) + "</td>";
          return '<td class="' + cellClass(t.columns[i]) + '" title="' + esc(v) +
                 '">' + esc(v === "" ? "—" : v) + "</td>";
        }).join("") + "</tr>";
    }).join("") + "</tbody></table></div>" + tableFoot(t);
}

function tableFoot(t) {
  const from = t.total ? t.offset + 1 : 0;
  const to = t.offset + t.rows.length;
  const hidden = ctab === "records" && !tAllCols
    ? t.columns.length - visibleCols(t.columns).length : 0;
  return '<div class="tblfoot"><span class="num">' + fmtNum(from) + "–" +
    fmtNum(to) + " of " + fmtNum(t.total) + " rows</span>" +
    (hidden ? '<button class="btn sm" data-act="toggle-cols">Show ' + hidden +
      " pipeline columns</button>" : "") +
    (tAllCols && ctab === "records" ? '<button class="btn sm" data-act="toggle-cols">' +
      "Hide pipeline columns</button>" : "") +
    '<span class="hint">Click a row for the full record</span>' +
    '<span class="pager">' +
    '<button class="btn sm" data-act="page" data-d="-1"' +
      (t.offset <= 0 ? " disabled" : "") + " aria-label=\"Previous page\">" +
      icon("back", 13) + "</button>" +
    '<button class="btn sm" data-act="page" data-d="1"' +
      (to >= t.total ? " disabled" : "") + " aria-label=\"Next page\">" +
      icon("fwd", 13) + "</button></span></div>";
}

onAct("sort-col", d => {
  const i = +d.i;
  if (sortCol === i) sortDir = -sortDir; else { sortCol = i; sortDir = 1; }
  renderTable();
});
onAct("toggle-cols", () => { tAllCols = !tAllCols; renderTable(); });
onAct("page", d => {
  const next = tOffset + (+d.d) * T_LIMIT;
  if (next < 0 || next >= tableData.total) return;
  tOffset = next;
  loadTable();
});
onAct("clear-filters", () => {
  tQuery = ""; tStatus = ""; el("c-q").value = "";
  renderFilters();
  loadTable();
});

/* ---------- overview charts ---------- */
/* single-hue bars for magnitude, fixed status palette for state; every bar
   carries a direct label and its count, so colour is never the only channel */
const STATUS_COLOR = { ok: "var(--good)", reviewed: "var(--good)",
  needs_review: "var(--warn)", pending_llm: "var(--ink-3)",
  no_text: "var(--ink-3)", llm_failed: "var(--crit)",
  validation_failed: "var(--crit)" };

/* bars: [label, value, colour]. Every bar carries its own label and count, so
   colour is decoration on top of text rather than the only channel. */
function barChart(title, bars) {
  const max = Math.max(1, ...bars.map(b => b[1]));
  return '<div class="chart"><h3>' + esc(title) + "</h3>" + bars.map(b =>
    '<div class="crow" data-tip="' + esc(b[0]) + ": " + b[1] + '">' +
    '<span class="lb" title="' + esc(b[0]) + '">' + esc(b[0]) + "</span>" +
    '<span class="tr"><span class="fl" style="width:' +
      Math.max(2, Math.round(100 * b[1] / max)) + "%;background:" +
      (b[2] || "var(--series)") + '"></span></span>' +
    '<span class="vl">' + b[1] + "</span></div>").join("") + "</div>";
}

async function renderOverview() {
  el("c-panelbody").innerHTML = '<div class="body"><div class="skel" ' +
    'style="width:60%"></div></div>';
  let s;
  try { s = await api("/api/jobs/" + cid + "/stats"); }
  catch { el("c-panelbody").innerHTML = callout("bad", "Could not load stats."); return; }
  const parts = [];
  if (s.statuses.length)
    parts.push(barChart("Records by status", s.statuses.map(([k, v]) =>
      [STATUS_WORDS[k] || k, v, STATUS_COLOR[k]])));
  if (s.confidence.some(v => v)) {
    const max = Math.max(...s.confidence);
    parts.push('<div class="chart"><h3>Model confidence</h3><div class="hist">' +
      s.confidence.map((v, i) =>
        '<div class="col" data-tip="' + (i / 10).toFixed(1) + "–" +
        ((i + 1) / 10).toFixed(1) + ": " + v + " record" + (v === 1 ? "" : "s") +
        '"><span class="fl" style="height:' +
        (v ? Math.max(3, Math.round(100 * v / max)) : 0) + '%"></span></div>')
        .join("") + '</div><div class="haxis"><span>0.0</span><span>0.5</span>' +
      "<span>1.0</span></div></div>");
  }
  for (const [name, pairs] of Object.entries(s.enums))
    parts.push(barChart(name, pairs.map(([k, v]) => [k, v])));

  el("c-panelbody").innerHTML = parts.length
    ? '<div class="body"><div class="charts">' + parts.join("") + "</div></div>"
    : '<div class="empty">' + icon("info", 30) + "<h3>Nothing to plot yet</h3>" +
      "<p>Charts appear once records exist. Enum fields in your schema each get " +
      "their own distribution here.</p></div>";
}

/* ---------- export ---------- */
async function renderExport() {
  let files = [];
  try { files = await api("/api/jobs/" + cid + "/artifacts"); }
  catch { el("c-panelbody").innerHTML = callout("bad", "Could not list artifacts."); return; }
  if (!files.length) {
    el("c-panelbody").innerHTML = '<div class="empty">' + icon("dl", 30) +
      "<h3>Nothing to export yet</h3><p>Artifacts appear as the run writes " +
      "them.</p></div>";
    return;
  }
  const unit = cschema && cschema.record_unit === "pdf" ? "PDF" : "folder";
  el("c-panelbody").innerHTML =
    '<div class="body"><dl class="filelist">' + files.map(f =>
      '<div class="row"><div style="flex:1;min-width:0">' +
      '<b class="mono">' + esc(f.name) + "</b>" +
      '<p class="hint">' + esc(f.help) + "</p></div>" +
      '<span class="v hint">' + fmtBytes(f.bytes) + "</span>" +
      '<a class="btn sm" href="/api/jobs/' + esc(cid) + "/download/" +
      encodeURIComponent(f.name) + '">' + icon("dl", 13) + "Download</a></div>")
      .join("") + "</dl></div>" +
    '<div class="body rowsep">' +
    '<p class="micro" style="margin-bottom:8px">Using this corpus for training</p>' +
    '<p class="hint" style="max-width:74ch">One row of <code>records.csv</code> ' +
    "is one record (one " + unit + "), keyed by <code>record_id</code>. " +
    "<code>images.csv</code> carries the same key, so joining the two gives " +
    "image/label pairs. Split train and test <b>by <code>record_id</code></b>, " +
    "never by image — several images can come from one document, and splitting " +
    "by image leaks the label across the split. <code>status</code> tells you " +
    "which rows are model output (<code>ok</code>) and which a human confirmed " +
    "(<code>reviewed</code>); <code>_confidence</code> lets you train on a " +
    "high-confidence subset first.</p></div>";
}

/* ======================= RECORD DRAWER + REVIEW ======================= */
let reviewList = [], reviewIdx = -1;

onAct("open-row", d => {
  const t = tableData;
  if (!t) return;
  const row = t.rows[+d.i];
  if (ctab === "images") return openImageDrawer(t, row);
  const idCol = t.columns.indexOf("record_id");
  if (idCol < 0) return openRawDrawer(t, row);
  reviewList = t.rows.map(r => r[idCol]);
  reviewIdx = +d.i;
  openRecord(row[idCol]);
});

function openImageDrawer(t, row) {
  const cell = c => { const i = t.columns.indexOf(c); return i >= 0 ? row[i] : ""; };
  const file = cell("file");           // embedded: exported under out/images/
  const origin = cell("origin");       // loose: still in the source archive
  const src = file
    ? "/api/jobs/" + encodeURIComponent(cid) + "/image/" + encodeURI(file)
    : (origin ? "/api/jobs/" + encodeURIComponent(cid) + "/source-image/" +
                encodeURI(origin) : "");
  const head = src
    ? '<img class="imgprev" alt="' + esc(origin || file) + '" src="' + src + '">'
    : "";
  const note = cell("record_id")
    ? ""
    : callout("warn", "<strong>This image is not linked to a record.</strong> " +
      "It sits in a folder that holds no PDF, or several — see " +
      "<code>issues.csv</code> for the reason. It will not appear in a " +
      "training join.");
  openDrawerHTML(origin || file || "Image", head + note + dlFrom(t.columns, row));
}

function openRawDrawer(t, row) {
  openDrawerHTML(ctab === "issues" ? "Issue" : "Row", dlFrom(t.columns, row));
}

function dlFrom(columns, row) {
  return '<dl class="dl">' + columns.map((c, i) => {
    const v = row[i];
    const mono = ["record_id", "rel_path", "file", "source_paths"].includes(c);
    return '<div class="k">' + esc(c) + "</div>" +
      (c === "status" ? '<div class="v">' + statusHTML(v) + "</div>"
        : '<div class="v' + (mono ? " mono" : "") + (v === "" ? " none" : "") + '">' +
          esc(v === "" ? "—" : v) + "</div>");
  }).join("") + "</dl>";
}

async function openRecord(recordId, forceEdit) {
  let rec;
  try {
    rec = await api("/api/jobs/" + cid + "/record/" + encodeURIComponent(recordId));
  } catch {
    toast("That record is not in records.csv");
    return;
  }
  const val = {};
  rec.columns.forEach((c, i) => { val[c] = rec.row[i]; });
  let evidence = {};
  try { evidence = JSON.parse(val._evidence || "{}") || {}; } catch { evidence = {}; }

  const fields = (cschema && cschema.fields) || [];
  const status = val.status || "";
  const editable = fields.length &&
    ["needs_review", "ok", "reviewed"].includes(status);
  const edit = editable && (forceEdit !== undefined ? forceEdit
                                                    : status === "needs_review");

  let body = '<div class="dl">' +
    '<div class="v" style="display:flex;gap:var(--s4);align-items:center;' +
    'margin-bottom:var(--s4)">' + statusHTML(status) +
    (val._confidence !== "" && val._confidence !== undefined
      ? '<span class="hint num">confidence ' + esc(val._confidence) + "</span>" : "") +
    (val.review_reasons ? '<span class="hint">' + esc(val.review_reasons) +
      "</span>" : "") + "</div></div>";

  body += '<dl class="dl">' + fields.map(f => {
    const v = val[f.name] === undefined ? "" : val[f.name];
    const q = evidence[f.name];
    let input;
    if (!edit) {
      input = '<div class="v' + (v === "" ? " none" : "") + '">' +
              esc(v === "" ? "—" : v) + "</div>";
    } else if (f.type === "enum") {
      input = '<div class="v"><select class="rv" data-f="' + esc(f.name) + '">' +
        '<option value="">— none —</option>' +
        (f.options || []).map(o => "<option" + (String(o) === String(v) ? " selected" : "") +
          ">" + esc(o) + "</option>").join("") + "</select></div>";
    } else if (f.type === "boolean") {
      input = '<div class="v"><select class="rv" data-f="' + esc(f.name) + '">' +
        ["", "True", "False"].map(o => '<option value="' + o + '"' +
          (o === String(v) ? " selected" : "") + ">" + (o || "— none —") +
          "</option>").join("") + "</select></div>";
    } else {
      input = '<div class="v"><input type="text" class="rv" data-f="' + esc(f.name) +
        '" value="' + esc(v) + '"></div>';
    }
    return '<div class="k">' + esc(f.name) + (f.required ? " *" : "") + "</div>" +
      input + (q ? '<p class="quote" style="margin-top:6px">' + esc(q) + "</p>" : "");
  }).join("") + "</dl>";

  const prov = ["source_paths", "n_pdfs", "n_pages", "chars_extracted",
                "n_images", "llm_model", "llm_attempts", "extracted_at",
                "truncated", "low_yield", "scanned_suspect", "_notes"]
    .filter(c => val[c] !== undefined && val[c] !== "");
  if (prov.length) {
    const FLAGS = ["truncated", "low_yield", "scanned_suspect"];
    body += '<div class="micro sect">Provenance</div>' +
      '<dl class="dl">' + prov.map(c =>
        '<div class="k">' + esc(c) + "</div>" +
        '<div class="v' + (c === "source_paths" ? " mono" : "") + '">' +
        esc(FLAGS.includes(c) ? (String(val[c]) === "1" ? "yes" : "no") : val[c]) +
        "</div>").join("") + "</dl>";
  }

  let foot = "";
  if (editable && edit) {
    foot = '<input type="text" id="rv-reviewer" placeholder="Your name" ' +
      'aria-label="Reviewer name" style="max-width:150px" value="' +
      esc(localStorage.getItem("corpus-reviewer") || "") + '">' +
      '<button class="btn primary" data-act="save-review" data-id="' + esc(recordId) +
      '">' + icon("save", 14) + "Save corrections</button>" +
      '<button class="btn" data-act="approve-review" data-id="' + esc(recordId) +
      '">' + icon("check", 14) + "Approve as-is</button>";
  } else if (editable) {
    foot = '<button class="btn" data-act="edit-record" data-id="' + esc(recordId) +
      '">' + icon("pencil", 14) + "Correct these values</button>";
  }
  if (foot) foot += '<span class="hint" id="rv-msg" style="margin-left:auto"></span>';

  let nav = "";
  if (reviewIdx >= 0 && reviewList.length > 1) {
    nav = '<span class="hint num">' + (reviewIdx + 1) + " of " + reviewList.length +
      "</span>" +
      '<button class="btn sm" data-act="rec-step" data-d="-1"' +
      (reviewIdx <= 0 ? " disabled" : "") + ' aria-label="Previous record">' +
      icon("back", 13) + "</button>" +
      '<button class="btn sm" data-act="rec-step" data-d="1"' +
      (reviewIdx >= reviewList.length - 1 ? " disabled" : "") +
      ' aria-label="Next record">' + icon("fwd", 13) + "</button>";
  }

  window._recordBase = {};
  for (const f of fields) window._recordBase[f.name] = String(val[f.name] ?? "");
  openDrawerHTML(recordId, body, foot, nav);
}

onAct("edit-record", d => openRecord(d.id, true));
onAct("rec-step", d => {
  const next = reviewIdx + (+d.d);
  if (next < 0 || next >= reviewList.length) return;
  reviewIdx = next;
  openRecord(reviewList[next]);
});

async function submitReview(recordId, withCorrections) {
  const nameBox = el("rv-reviewer");
  const reviewer = (nameBox ? nameBox.value : "").trim();
  const msg = el("rv-msg");
  if (!reviewer) {
    msg.innerHTML = '<span style="color:var(--crit-ink)">Enter your name first — ' +
      "reviews are attributed.</span>";
    if (nameBox) nameBox.focus();
    return;
  }
  localStorage.setItem("corpus-reviewer", reviewer);
  const corrections = {};
  if (withCorrections) {
    for (const node of document.querySelectorAll(".rv")) {
      if (String(node.value) !== (window._recordBase[node.dataset.f] ?? ""))
        corrections[node.dataset.f] = node.value;
    }
    if (!Object.keys(corrections).length) {
      msg.innerHTML = "Nothing was changed — use <b>Approve as-is</b> instead.";
      return;
    }
  }
  try {
    await api("/api/jobs/" + cid + "/review", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ record_id: recordId, reviewer,
        verdict: withCorrections ? "corrected" : "approved", corrections }) });
  } catch (e) {
    msg.innerHTML = '<span style="color:var(--crit-ink)">' + esc(e.message) + "</span>";
    return;
  }
  toast(withCorrections ? "Corrections saved — marked reviewed"
                        : "Approved — marked reviewed");
  const more = reviewIdx >= 0 && reviewIdx < reviewList.length - 1;
  if (more) { reviewIdx += 1; openRecord(reviewList[reviewIdx]); }
  else closeDrawer();
  loadCorpusHeader().then(() => { if (ctab !== "overview") loadTable(); });
}

onAct("save-review", d => submitReview(d.id, true));
onAct("approve-review", d => submitReview(d.id, false));

/* ---------- review workspace ---------- */
onAct("start-review", async () => {
  let t;
  try {
    t = await api("/api/jobs/" + cid +
      "/table/records?status=needs_review&limit=500");
  } catch { toast("Could not load the review queue"); return; }
  const idCol = t.columns.indexOf("record_id");
  if (idCol < 0 || !t.rows.length) { toast("Nothing is waiting for review"); return; }
  reviewList = t.rows.map(r => r[idCol]);
  reviewIdx = 0;
  openRecord(reviewList[0], true);
});

/* ---------- corpus-level actions ---------- */
onAct("rename", async () => {
  const next = prompt("Name this corpus:", cjob.title || cjob.table);
  if (!next || next.trim() === (cjob.title || "")) return;
  try {
    await api("/api/jobs/" + cid, { method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: next.trim() }) });
  } catch (e) { toast(e.message); return; }
  cjob.title = next.trim();
  paintHeader();
  toast("Renamed");
});

onAct("retry-run", async () => {
  try {
    const r = await api("/api/jobs/" + cid + "/retry", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    toast("Retrying: " + r.stages);
    loadCorpusHeader();
  } catch (e) { toast(e.message); }
});

async function deleteCorpus() {
  if (!confirm("Delete this corpus and every file it produced?\n\n" +
               "This cannot be undone.")) return;
  try { await api("/api/jobs/" + cid, { method: "DELETE" }); }
  catch (e) { toast(e.message); return; }
  cid = null;
  toast("Corpus deleted");
  go("#/corpora");
}
