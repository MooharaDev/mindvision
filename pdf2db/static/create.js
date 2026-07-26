/* Corpus — the intake flow: archive first, then a schema drafted from the
   documents themselves, then the build.

   Step 1 uploads (or points at) an archive and gets a read-only scan back:
   nothing is processed until the operator has seen what is in there. Step 2
   asks the configured model to draft a schema from a sample of the real
   documents; every field stays editable and is validated server-side before
   anything runs. */
"use strict";

const TYPES = ["string", "enum", "integer", "number", "boolean", "date"];

let staging = null;        // { staging_id, scan, source }
let chosenFiles = [];
let srcMode = "upload";
let recordUnit = "folder";
let draftExamples = {};    // field name -> value seen in the first sample
let draftState = null;     // null | "pending" | "done" | "skipped"
let wizStep = 1;

/* ======================= step machine ======================= */
function enterCreate() {
  wizGoto(staging ? wizStep : 1);
}

function wizGoto(n) {
  wizStep = n;
  for (let i = 1; i <= 3; i++) {
    el("wiz-" + i).hidden = i !== n;
    el("step-" + i).dataset.state = i < n ? "done" : i === n ? "on" : "";
  }
  if (n === 2 && draftState === null) autoDraft();
}

function resetIntake() {
  staging = null; chosenFiles = []; draftExamples = {}; draftState = null;
  el("scan-result").innerHTML = "";
  el("scan-msg").innerHTML = "";
  el("draft-panel").innerHTML = "";
  el("dropzone").classList.remove("loaded");
  el("drop-title").textContent = "Choose a folder of PDFs";
  el("drop-note").textContent = "Subfolders are included — or drop a .zip of " +
    "the whole archive here";
  el("s-fields").innerHTML = "";
  el("s-msg").innerHTML = "";
  el("build-result").innerHTML = "";
  wizGoto(1);
}

/* ======================= step 1: the archive ======================= */
function setSource(m) {
  srcMode = m;
  el("src-upload").setAttribute("aria-pressed", String(m === "upload"));
  el("src-server").setAttribute("aria-pressed", String(m === "server"));
  el("src-upload-block").hidden = m !== "upload";
  el("src-server-block").hidden = m !== "server";
}

document.addEventListener("DOMContentLoaded", () => {
  const zone = el("dropzone");
  el("files").addEventListener("change", () => {
    chosenFiles = [...el("files").files];
    uploadArchive();
  });
  zone.addEventListener("dragover", e => {
    e.preventDefault(); zone.classList.add("over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", e => {
    e.preventDefault();
    zone.classList.remove("over");
    chosenFiles = [...e.dataTransfer.files];
    uploadArchive();
  });
});

function archiveName() {
  for (const f of chosenFiles) {
    const rel = f.webkitRelativePath || "";
    if (rel.includes("/")) return rel.split("/")[0];
  }
  return chosenFiles.length === 1 ? chosenFiles[0].name.replace(/\.zip$/i, "") : "";
}

async function uploadArchive() {
  if (!chosenFiles.length) return;
  let bytes = 0;
  for (const f of chosenFiles) bytes += f.size;
  el("dropzone").classList.remove("loaded");
  el("drop-title").textContent = "Uploading " + chosenFiles.length + " files (" +
    fmtBytes(bytes) + ")…";
  el("drop-note").textContent = "Large archives take a moment.";
  el("scan-msg").innerHTML = "";

  const fd = new FormData();
  fd.append("source_name", archiveName());
  for (const f of chosenFiles) {
    // drop the selected folder's own name so its subfolders become records
    let rel = f.webkitRelativePath || f.name;
    const parts = rel.split("/");
    if (parts.length > 1) rel = parts.slice(1).join("/");
    fd.append("files", f, rel);
  }
  await sendStaging(fd);
}

async function scanServerPath() {
  const p = el("spath").value.trim();
  if (!p) { el("scan-msg").innerHTML = callout("bad", "Enter a path first."); return; }
  const fd = new FormData();
  fd.append("server_path", p);
  await sendStaging(fd);
}

async function sendStaging(fd) {
  el("scan-msg").innerHTML = '<p class="hint">Reading the archive…</p>';
  try {
    staging = await api("/api/staging", { method: "POST", body: fd });
  } catch (e) {
    staging = null;
    el("scan-msg").innerHTML = callout("bad", esc(e.message));
    el("drop-title").textContent = "Choose a folder of PDFs";
    el("drop-note").textContent = "Subfolders are included — or drop a .zip of " +
      "the whole archive here";
    return;
  }
  draftState = null;
  draftExamples = {};
  el("scan-msg").innerHTML = "";
  const zone = el("dropzone");
  zone.classList.add("loaded");
  el("drop-title").textContent = (staging.source || "Archive") + " — loaded";
  el("drop-note").textContent = "Click to choose a different folder";
  renderScan();
}

function renderScan() {
  const s = staging.scan;
  recordUnit = s.units.folder.n_records > 1 &&
               s.units.folder.n_records < s.units.pdf.n_records ? "folder" : "pdf";
  const probe = s.probe;
  const warn = [];
  if (probe.n_low_yield) {
    warn.push(callout("warn",
      "<strong>" + probe.n_low_yield + " of " + probe.n_probed +
      " sampled documents hold almost no selectable text.</strong> They are " +
      "probably scans. The extraction model reads text only, so records like " +
      "these are flagged <em>no text</em> instead of being guessed at."));
  }
  if (s.truncated) {
    warn.push(callout("warn", "<strong>Very large archive.</strong> The preview " +
      "stopped counting partway through, so every figure above is a " +
      "<em>minimum</em> — that is what the <b>+</b> means. The run itself " +
      "processes the whole archive."));
  }
  if (s.n_loose && s.units.folder.n_records !== s.units.pdf.n_records) {
    warn.push(callout("", s.n_loose + " PDF" + (s.n_loose === 1 ? " sits" : "s sit") +
      " directly in the top level rather than in a subfolder. Each becomes its " +
      "own record."));
  }

  const sample = probe.samples.find(x => x.preview) || probe.samples[0] || {};
  el("scan-result").innerHTML =
    '<div class="panel"><header><div><h2>What is in ' + esc(staging.source || "this archive") +
      '</h2><p class="sub">Read only — nothing has been processed yet.</p></div>' +
      '<button class="btn" data-act="reset-intake">Use a different archive</button>' +
    '</header>' +
    '<div class="body"><dl class="readout">' +
      readout("PDFs", atLeast(s.n_pdfs)) +
      readout("Records", atLeast(s.units[recordUnit].n_records), "recordcount") +
      readout("Images", atLeast(s.n_images)) +
      readout("Folder depth", fmtNum(s.depth)) +
      readout("Size", fmtBytes(s.n_bytes) + (s.truncated ? "+" : "")) +
    '</dl></div>' +
    '<div class="body rowsep">' + warn.join("") +
      '<div class="field" style="max-width:520px;margin-bottom:0">' +
      '<label for="unit-pick">What should count as one record?</label>' +
      '<select id="unit-pick" data-act-change="unit">' +
        unitOption("folder", s) + unitOption("pdf", s) +
      '</select>' +
      '<p class="hint" id="unit-hint">' + unitHint(s) + '</p></div>' +
    '</div>' +
    (sample.preview ? '<div class="body rowsep">' +
      '<p class="micro" style="margin-bottom:8px">Text read from ' +
      esc(sample.rel_path) + '</p>' +
      '<div class="sample">' + esc(sample.preview) + '</div></div>' : "") +
    '<div class="body rowsep" style="display:flex;gap:var(--s3);align-items:center">' +
      '<button class="btn primary lg" data-act="to-schema">Continue to schema ' +
      icon("fwd", 14) + '</button>' +
      '<span class="hint">Next: Corpus reads a few of these documents and drafts ' +
      'the table for you.</span>' +
    '</div></div>';

  el("unit-pick").addEventListener("change", e => {
    recordUnit = e.target.value;
    draftState = null;                       // a different unit needs a new draft
    el("recordcount").innerHTML = atLeast(s.units[recordUnit].n_records);
    el("unit-hint").innerHTML = unitHint(s);
  });
}

function readout(label, value, id) {
  return "<div><dt>" + esc(label) + "</dt><dd" + (id ? ' id="' + id + '"' : "") +
         ">" + value + "</dd></div>";
}

/* past the walk cap every count is a floor, and the UI must say so */
function atLeast(n) {
  return fmtNum(n) + (staging && staging.scan.truncated
    ? '<small title="the preview stopped counting; the real number is higher">' +
      "+</small>" : "");
}

function unitOption(unit, s) {
  const n = fmtNum(s.units[unit].n_records) + (s.truncated ? "+" : "");
  const label = unit === "folder"
    ? "One record per top-level subfolder — its PDFs are merged"
    : "One record per PDF file";
  return '<option value="' + unit + '"' + (unit === recordUnit ? " selected" : "") +
         ">" + esc(label) + " (" + n + " records)</option>";
}

function unitHint(s) {
  const ex = s.units[recordUnit].examples;
  return ex.length ? "Records would be: " + ex.slice(0, 4).map(e =>
    "<code>" + esc(e) + "</code>").join(", ") + (ex.length > 4 ? " …" : "") : "";
}

onAct("reset-intake", () => {
  if (staging) api("/api/staging/" + staging.staging_id, { method: "DELETE" })
    .catch(() => { /* the server sweeps abandoned staging areas anyway */ });
  resetIntake();
});
onAct("to-schema", () => wizGoto(2));

/* ======================= step 2: the schema ======================= */
function autoDraft() {
  if (!staging) { wizGoto(1); return; }
  el("s-unit").innerHTML = unitOption("folder", staging.scan) +
                           unitOption("pdf", staging.scan);
  el("s-unit").value = recordUnit;
  unitChanged();
  if (settingsCache && settingsCache.is_mock) {
    draftState = "skipped";
    el("draft-panel").innerHTML = callout("warn",
        "<strong>Test mode is active</strong>, so the schema cannot be drafted " +
        "from your documents — the mock backend never reads them. Set a real " +
        'endpoint in <a href="#/settings">Settings</a>, or build the schema by ' +
        "hand below.") +
      '<div class="btnrow" style="margin-bottom:var(--s5)">' +
      '<button class="btn" data-act="load-example">Load the example schema' +
      "</button></div>";
    if (!el("s-fields").children.length) { addField(); addField(); addField(); }
    return;
  }
  draftSchema();
}

async function draftSchema() {
  draftState = "pending";
  el("draft-panel").innerHTML =
    '<div class="callout" style="align-items:center"><span class="spin"></span>' +
    '<div><strong>Reading your documents…</strong>' +
    '<p class="hint">Sampling up to 3 records and asking ' +
    esc((settingsCache && settingsCache.llm_model) || "the model") +
    " to propose the table. This takes a few seconds.</p></div></div>";
  let r;
  try {
    r = await api("/api/staging/" + staging.staging_id + "/suggest-schema", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ record_unit: recordUnit }) });
  } catch (e) {
    r = { ok: false, error: e.message };
  }
  if (!r.ok) {
    draftState = "skipped";
    el("draft-panel").innerHTML = callout("warn",
        "<strong>Could not draft a schema.</strong> " + esc(r.error || "") +
        "<br>Build the table by hand below — everything still works.") +
      '<div class="btnrow" style="margin-bottom:var(--s5)">' +
      '<button class="btn" data-act="redraft">' + icon("retry", 14) +
      "Try again</button>" +
      '<button class="btn" data-act="load-example">Load the example schema' +
      "</button></div>";
    if (!el("s-fields").children.length) { addField(); addField(); addField(); }
    return;
  }
  draftState = "done";
  draftExamples = r.examples || {};
  fillBuilder(r.schema);
  const generic = r.source === "generic";
  el("draft-panel").innerHTML =
    callout("good", "<strong>Schema drafted from " + r.sampled.length +
      " of your documents.</strong> " +
      (generic ? "A generic starter — the endpoint could not read the files."
               : "The value under each field is what " + esc(r.model) +
                 " found in <code>" + esc(r.sampled[0]) + "</code>. Edit " +
                 "anything that looks wrong, delete what you do not need, " +
                 "then build.")) +
    '<div class="btnrow" style="margin-bottom:var(--s5)">' +
    '<button class="btn" data-act="redraft">' + icon("retry", 14) +
    "Draft again</button>" +
    '<span class="hint">Sampled ' +
    r.sampled.map(x => "<code>" + esc(x) + "</code>").join(", ") + "</span></div>";
}

onAct("redraft", () => draftSchema());
onAct("load-example", async () => {
  fillBuilder(await api("/api/example-schema"));
  toast("Example schema loaded");
});

function unitChanged() {
  const u = el("s-unit").value;
  recordUnit = u;
  el("s-unit-hint").innerHTML = staging ? unitHint(staging.scan) : "";
}

/* ---------- schema editor ---------- */
function addField(f) {
  f = f || { name: "", type: "string", required: false, options: "", description: "" };
  const row = document.createElement("div");
  row.className = "fieldrow";
  const ex = draftExamples[f.name];
  row.innerHTML =
    '<div><input type="text" class="f-name" spellcheck="false" aria-label="Field name" ' +
      'placeholder="field_name" value="' + esc(f.name) + '"></div>' +
    '<div><select class="f-type" aria-label="Field type" data-act-type="1">' +
      TYPES.map(t => '<option' + (t === f.type ? " selected" : "") + ">" + t +
        "</option>").join("") + "</select></div>" +
    '<div class="req"><input type="checkbox" class="f-req" aria-label="Required"' +
      (f.required ? " checked" : "") + "></div>" +
    '<div><input type="text" class="f-desc" aria-label="What to extract" ' +
      'placeholder="What should the model look for?" value="' + esc(f.description) + '">' +
      '<input type="text" class="f-opts" aria-label="Allowed values" ' +
      'style="margin-top:6px' + (f.type === "enum" ? "" : ";display:none") + '" ' +
      'placeholder="allowed, values, comma separated" value="' + esc(f.options) + '"></div>' +
    '<div><button class="iconbtn dangerous" data-act="drop-field" ' +
      'aria-label="Remove field" title="Remove field">' + icon("trash", 15) +
      "</button></div>" +
    (ex ? '<div class="ex"><span>found:</span><code>' + esc(ex) + "</code></div>" : "");
  row.querySelector(".f-type").addEventListener("change", e => {
    const opts = row.querySelector(".f-opts");
    const isEnum = e.target.value === "enum";
    opts.style.display = isEnum ? "" : "none";
    if (!isEnum) opts.value = "";
  });
  el("s-fields").appendChild(row);
}

onAct("drop-field", (_d, node) => node.closest(".fieldrow").remove());

function fillBuilder(s) {
  el("s-table").value = s.table || "records";
  if (el("s-unit").options.length) el("s-unit").value = s.record_unit || recordUnit;
  el("s-desc").value = s.task_description || "";
  el("s-fields").innerHTML = "";
  for (const f of (s.fields || [])) {
    addField({ name: f.name, type: f.type, required: !!f.required,
               options: (f.options || []).join(", "),
               description: f.description || "" });
  }
  el("s-msg").innerHTML = "";
}

function buildSchema() {
  const fields = [];
  for (const row of el("s-fields").querySelectorAll(".fieldrow")) {
    const name = row.querySelector(".f-name").value.trim();
    if (!name) continue;
    const fld = { name, type: row.querySelector(".f-type").value,
                  required: row.querySelector(".f-req").checked,
                  description: row.querySelector(".f-desc").value.trim() };
    if (fld.type === "enum") {
      fld.options = row.querySelector(".f-opts").value.split(",")
        .map(s => s.trim()).filter(Boolean);
    }
    fields.push(fld);
  }
  return { table: el("s-table").value.trim(),
           record_unit: el("s-unit").value || recordUnit,
           task_description: el("s-desc").value.trim(), fields };
}

async function validateSchema(loud) {
  let j;
  try {
    j = await api("/api/schema/validate", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildSchema()) });
  } catch (e) {
    el("s-msg").innerHTML = callout("bad", esc(e.message));
    return false;
  }
  if (j.ok) {
    el("s-msg").innerHTML = loud
      ? callout("good", "Schema is valid — " + buildSchema().fields.length +
                        " fields.") : "";
  } else {
    el("s-msg").innerHTML = callout("bad", "<strong>Fix these before building:" +
      "</strong><ul style='margin:6px 0 0;padding-left:18px'>" +
      j.errors.map(e => "<li>" + esc(e) + "</li>").join("") + "</ul>");
  }
  return j.ok;
}

/* ---------- saved schema library ---------- */
async function saveSchema() {
  if (!(await validateSchema(false))) return;
  const name = prompt("Save this schema as:", el("s-table").value);
  if (!name) return;
  try {
    await api("/api/schemas", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, schema: buildSchema() }) });
    toast("Saved to the schema library: " + name);
  } catch (e) { toast(e.message); }
}

async function openLibrary() {
  let list = [];
  try { list = await api("/api/schemas"); } catch (e) { toast(e.message); return; }
  const body = list.length
    ? '<dl class="filelist">' + list.map(s =>
        '<div class="row"><div style="flex:1"><b>' + esc(s.name) + "</b>" +
        '<p class="hint">table <code>' + esc(s.table) + "</code> · " +
        s.n_fields + " fields</p></div>" +
        '<button class="btn sm" data-act="load-schema" data-name="' + esc(s.name) +
        '">Load</button>' +
        '<button class="iconbtn dangerous" data-act="del-schema" data-name="' +
        esc(s.name) + '" aria-label="Delete ' + esc(s.name) + '">' +
        icon("trash", 15) + "</button></div>").join("") + "</dl>"
    : '<div class="empty">' + icon("save", 30) +
      "<h3>No saved schemas yet</h3><p>When a schema works well, save it here " +
      "so the next archive of the same kind starts from it instead of from a " +
      "blank table.</p></div>";
  openDrawerHTML("Schema library", body);
}

onAct("open-library", () => openLibrary());
onAct("load-schema", async d => {
  fillBuilder(await api("/api/schemas/" + encodeURIComponent(d.name)));
  draftExamples = {};
  closeDrawer();
  toast("Loaded: " + d.name);
});
onAct("del-schema", async d => {
  if (!confirm('Delete the saved schema "' + d.name + '"?')) return;
  await api("/api/schemas/" + encodeURIComponent(d.name), { method: "DELETE" });
  openLibrary();
});

/* ======================= step 3: build ======================= */
let pollTimer = null, buildJob = null, buildStart = 0;
const STAGE_ORDER = ["discover", "extract", "images", "llm", "load"];

async function startBuild() {
  el("build-msg").textContent = "";
  if (!staging) { el("build-msg").textContent = "Load an archive first."; return; }
  const schema = buildSchema();
  if (!schema.fields.length) {
    el("build-msg").textContent = "Add at least one field.";
    return;
  }
  if (!(await validateSchema(false))) return;

  const fd = new FormData();
  fd.append("staging_id", staging.staging_id);
  fd.append("schema", JSON.stringify(schema));
  fd.append("title", staging.source || schema.table);
  const btn = el("build-btn");
  busy(btn, true);
  let j;
  try {
    j = await api("/api/jobs", { method: "POST", body: fd });
  } catch (e) {
    el("build-msg").textContent = e.message;
    busy(btn, false);
    return;
  }
  busy(btn, false);
  buildJob = j.job_id;
  buildStart = Date.now();
  staging = null;                    // promoted into the corpus; no longer staged
  el("build-title").textContent = "Building " + schema.table;
  el("build-result").innerHTML = "";
  if (j.skipped && j.skipped.length) toast(j.skipped.length + " file(s) skipped");
  wizGoto(3);
  clearInterval(pollTimer);
  pollTimer = setInterval(pollBuild, 1000);
  pollBuild();
}

async function pollBuild() {
  let j;
  try { j = await api("/api/jobs/" + buildJob + "/status"); } catch { return; }
  el("build-bar").style.transform = "scaleX(" + (j.pct / 100) + ")";
  el("build-elapsed").textContent =
    Math.round((Date.now() - buildStart) / 1000) + "s · " + j.pct + "%";
  const idx = STAGE_ORDER.indexOf(j.stage);
  for (const li of el("build-stages").querySelectorAll("li")) {
    const i = STAGE_ORDER.indexOf(li.dataset.s);
    const done = j.status === "done" || j.stage === "finished" || i < idx;
    li.dataset.state = done ? "done" : (i === idx ? "on" : "");
    li.querySelector(".mark").innerHTML = done
      ? '<svg width="14" height="14" style="color:var(--good)"><use href="#i-check"/></svg>'
      : (i === idx ? '<span class="spin"></span>' : '<span class="dot"></span>');
  }
  el("build-llm").textContent =
    (j.stage === "llm" && j.total) ? "record " + j.done + " of " + j.total : "";

  if (j.status === "running") return;
  clearInterval(pollTimer);
  if (j.status === "failed") {
    el("build-result").innerHTML =
      callout("bad", "<strong>The run failed.</strong> " + esc(j.error || "")) +
      '<div class="btnrow"><button class="btn" data-act="back-to-schema">' +
      "Back to the schema</button></div>";
    return;
  }
  const s = j.summary || {};
  el("build-title").textContent = "Built " + j.table;
  el("build-result").innerHTML =
    callout("good", "<strong>" + fmtNum(s.n_records) + " records</strong> from " +
      fmtNum(s.n_pdfs) + " PDFs. " +
      (s.n_review_queue ? fmtNum(s.n_review_queue) + " need a human check."
                        : "Nothing was flagged for review.")) +
    '<div class="btnrow"><a class="btn primary lg" href="#/corpus/' + buildJob + '">' +
    "Open the corpus " + icon("fwd", 14) + "</a>" +
    '<button class="btn" data-act="new-again">Build another</button></div>';
}

onAct("back-to-schema", () => wizGoto(2));
onAct("new-again", () => { resetIntake(); go("#/new"); });
