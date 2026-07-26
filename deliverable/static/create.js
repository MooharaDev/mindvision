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
let chosenFiles = [];      // [{ file: File, rel: "sub/dir/name.pdf" }] from click OR drop
let srcMode = "upload";
let recordUnit = "folder";
let ocrMode = "off";       // chosen on the intake step; "" is never sent
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
    // same shape as the drop path: the picker supplies the relative path itself
    chosenFiles = [...el("files").files]
      .map(f => ({ file: f, rel: f.webkitRelativePath || f.name }));
    uploadArchive();
  });
  zone.addEventListener("dragover", e => {
    e.preventDefault(); zone.classList.add("over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", e => {
    e.preventDefault();
    zone.classList.remove("over");
    /* A dropped FOLDER appears in dataTransfer.files as the directory itself,
       not its contents — uploading that fails at the network layer. The entry
       API is the only way to see inside. Entries must be taken synchronously:
       the item list is invalidated as soon as this handler returns. */
    const entries = [...e.dataTransfer.items]
      .filter(i => i.kind === "file")
      .map(i => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null))
      .filter(Boolean);
    const plain = [...e.dataTransfer.files];
    if (!entries.length) {                     // no entry API — loose files only
      chosenFiles = plain.map(f => ({ file: f, rel: f.name }));
      uploadArchive();
      return;
    }
    el("drop-title").textContent = "Reading folder…";
    el("drop-note").textContent = "Listing every file before anything is sent.";
    collectEntries(entries)
      .then(found => {
        if (!found.length) {
          el("scan-msg").innerHTML = callout("bad",
            "That folder appears to be empty.");
          resetIntake();
          return;
        }
        chosenFiles = found;
        uploadArchive();
      })
      .catch(err => {
        el("scan-msg").innerHTML = callout("bad",
          "Could not read that folder: " + esc(err.message || String(err)) +
          ". Use the click-to-choose option instead.");
        resetIntake();
      });
  });
});

/* Walk dropped directory entries into [{file, rel}]. readEntries() hands back
   at most 100 children per call and signals the end with an empty array, so it
   MUST be looped — reading it once silently truncates any folder over 100
   files, which is exactly the kind of quiet data loss this tool exists to
   avoid. */
function collectEntries(entries) {
  const out = [];
  const walk = (entry, prefix) => new Promise((resolve, reject) => {
    if (entry.isFile) {
      entry.file(f => { out.push({ file: f, rel: prefix + entry.name }); resolve(); },
                 reject);
      return;
    }
    const reader = entry.createReader();
    const batch = () => reader.readEntries(async kids => {
      if (!kids.length) { resolve(); return; }
      try {
        for (const kid of kids) await walk(kid, prefix + entry.name + "/");
      } catch (e) { reject(e); return; }
      batch();
    }, reject);
    batch();
  });
  return Promise.all(entries.map(e => walk(e, ""))).then(() => out);
}

/* The name of the single top-level folder everything sits in, or "" when the
   selection spans several (or is loose files / a zip). Also decides whether
   that name gets stripped below. */
function commonTopFolder() {
  let top = null;
  for (const { rel } of chosenFiles) {
    if (!rel.includes("/")) return "";       // something sits at the very top
    const first = rel.split("/")[0];
    if (top === null) top = first;
    else if (top !== first) return "";       // more than one root folder
  }
  return top || "";
}

function archiveName() {
  const top = commonTopFolder();
  if (top) return top;
  return chosenFiles.length === 1
    ? chosenFiles[0].rel.replace(/\.zip$/i, "") : "";
}

async function uploadArchive() {
  if (!chosenFiles.length) return;
  let bytes = 0;
  for (const { file } of chosenFiles) bytes += file.size;
  el("dropzone").classList.remove("loaded");
  el("drop-title").textContent = "Uploading " + chosenFiles.length + " files (" +
    fmtBytes(bytes) + ")…";
  el("drop-note").textContent = "Large archives take a moment.";
  el("scan-msg").innerHTML = "";

  // strip the chosen folder's OWN name so its subfolders become the records —
  // but only when there is exactly one, or dropping two folders would merge them
  const top = commonTopFolder();
  const fd = new FormData();
  fd.append("source_name", archiveName());
  for (const { file, rel } of chosenFiles) {
    fd.append("files", file, top ? rel.slice(top.length + 1) : rel);
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
  recordUnit = s.suggested_unit || "folder";
  const probe = s.probe;
  const warn = [];
  if (probe.n_low_yield) {
    const ocr = s.ocr || {};
    warn.push(callout("warn",
      "<strong>" + probe.n_low_yield + " of " + probe.n_probed +
      " sampled documents hold almost no selectable text.</strong> They are " +
      "probably scans. " + (ocr.available
        ? "This server has Tesseract, so you can switch <b>OCR</b> on below and " +
          "read them anyway — no model download, no network."
        : "The extraction model reads text only, so records like these are " +
          "flagged <em>no text</em> instead of being guessed at. To read them, " +
          "the <code>tesseract</code> package has to be provisioned on this server.")));
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
  if (s.n_filtered) {
    warn.push(callout("", "<b>" + fmtNum(s.n_filtered) + "</b> file" +
      (s.n_filtered === 1 ? "" : "s") + " that look like PDFs are being skipped " +
      "as editor lock files or hidden files (names starting <code>~$</code> or " +
      "<code>.</code>). They are listed in <code>issues.csv</code> after the run."));
  }
  if (s.depth > 2 && (s.id_patterns || []).length) {
    const p = s.id_patterns[0];
    warn.push(callout("", "Documents are nested up to <b>" + s.depth + "</b> folders " +
      "deep and the paths carry what looks like an identifier (" + esc(p.label) +
      "). Grouping by that pattern would make <b>" + fmtNum(p.n_records) +
      "</b> records — check the options below."));
  }

  const sample = probe.samples.find(x => x.preview) || probe.samples[0] || {};
  el("scan-result").innerHTML =
    '<div class="panel"><header><div><h2>What is in ' + esc(staging.source || "this archive") +
      '</h2><p class="sub">Read only — nothing has been processed yet.</p></div>' +
      '<button class="btn" data-act="reset-intake">Use a different archive</button>' +
    '</header>' +
    '<div class="body"><dl class="readout">' +
      readout("PDFs", atLeast(s.n_pdfs)) +
      readout("Records", atLeast(unitRecords(s, recordUnit)), "recordcount") +
      readout("Images", atLeast(s.n_images)) +
      readout("Folder depth", fmtNum(s.depth)) +
      readout("Size", fmtBytes(s.n_bytes) + (s.truncated ? "+" : "")) +
    '</dl></div>' +
    '<div class="body rowsep">' + warn.join("") +
      '<div class="field" style="max-width:560px;margin-bottom:0">' +
      '<label for="unit-pick">What should count as one record?</label>' +
      '<select id="unit-pick">' + unitOptions(s) + '</select>' +
      '<p class="hint" id="unit-hint">' + unitHint(s) + '</p></div>' +
      '<div class="field" id="unit-custom-wrap" hidden ' +
           'style="max-width:560px;margin:var(--s3) 0 0">' +
        '<label for="unit-custom">Pattern to read the identifier from each path</label>' +
        '<input type="text" id="unit-custom" spellcheck="false" autocapitalize="off" ' +
          'placeholder="(?P&lt;id&gt;INC-\\d+)">' +
        '<p class="hint" id="unit-custom-hint">Every document whose path contains the ' +
          'same match becomes one record. Name the group <code>id</code>, or use the ' +
          'first bracketed group.</p>' +
      '</div>' +
      ocrPickerHtml(s) +
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
    draftState = null;                       // a different unit needs a new draft
    const custom = e.target.value === "__custom__";
    el("unit-custom-wrap").hidden = !custom;
    if (custom) { el("unit-custom").focus(); previewCustomUnit(); return; }
    recordUnit = e.target.value;
    el("recordcount").innerHTML = atLeast(unitRecords(s, recordUnit));
    el("unit-hint").innerHTML = unitHint(s);
  });
  el("unit-custom").addEventListener("input", () => {
    draftState = null;
    clearTimeout(customTimer);
    customTimer = setTimeout(previewCustomUnit, 300);   // typing a regex is noisy
  });
  const ocrSel = el("ocr-pick");
  if (ocrSel) ocrSel.addEventListener("change", e => { ocrMode = e.target.value; });
}

/* A custom pattern is scored by the server against the real paths: the count
   and the example ids are the only honest way to know a regex did what you
   meant before spending a run on it. */
let customTimer = null;
function previewCustomUnit() {
  const raw = el("unit-custom").value.trim();
  const hint = el("unit-custom-hint");
  if (!raw) {
    hint.innerHTML = "Enter a pattern — e.g. <code>(?P&lt;id&gt;INC-\\d+)</code>.";
    hint.className = "hint";
    return;
  }
  const spec = "regex:" + raw;
  api("/api/staging/" + staging.staging_id + "/grouping",
      { method: "POST", body: JSON.stringify({ record_unit: spec }) })
    .then(r => {
      recordUnit = spec;
      el("recordcount").innerHTML = atLeast(r.n_records);
      const ex = r.examples.filter(Boolean).slice(0, 4)
        .map(e => "<code>" + esc(e) + "</code>").join(", ");
      hint.className = "hint";
      hint.innerHTML = "<b>" + fmtNum(r.n_records) + "</b> records from " +
        fmtNum(r.n_pdfs) + " PDFs" + (ex ? " — e.g. " + ex : "") +
        (r.n_unmatched ? ' <span class="st"><i class="dot warn"></i>' +
          fmtNum(r.n_unmatched) + " path(s) do not match; each becomes its own " +
          "record</span>" : "");
    })
    .catch(e => {
      hint.className = "hint err";
      hint.textContent = e.message || "that pattern is not valid";
    });
}

function unitRecords(s, unit) {
  return (s.units[unit] || {}).n_records
      ?? ((s.id_patterns || []).find(p => p.record_unit === unit) || {}).n_records ?? 0;
}

function ocrPickerHtml(s) {
  const ocr = s.ocr || {};
  if (!ocr.available) return "";
  ocrMode = ocr.recommended ? "auto" : (settingsCache && settingsCache.ocr_mode) || "off";
  const opt = (v, label) => '<option value="' + v + '"' +
    (v === ocrMode ? " selected" : "") + ">" + esc(label) + "</option>";
  return '<div class="field" style="max-width:560px;margin:var(--s3) 0 0">' +
    '<label for="ocr-pick">Read scanned pages with OCR?</label>' +
    '<select id="ocr-pick">' +
      opt("off", "No — skip scans and flag them as no text") +
      opt("auto", "Yes — OCR pages that have no text layer (recommended)") +
      opt("augment", "Yes — also read text inside figures on normal pages") +
      opt("force", "Yes — OCR every page, ignoring any existing text") +
    '</select>' +
    '<p class="hint">Runs on this server with Tesseract. Slower — roughly a ' +
      'second or two per scanned page — and OCR\'d records are always sent to ' +
      'review.</p></div>';
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

/* Every grouping the archive supports, each labelled with the number of records
   it would actually produce. The count is the decision, not the wording. */
function unitLabel(unit) {
  if (unit === "pdf") return "One record per PDF file";
  if (unit === "folder") return "One record per top-level subfolder — its PDFs are merged";
  if (unit === "parent") return "One record per folder of PDFs (the folder each PDF sits in)";
  if (unit.indexOf("depth:") === 0)
    return "One record per folder " + unit.split(":")[1] +
           " level(s) down — everything below it merged";
  if (unit.indexOf("regex:") === 0)
    return "One record per identifier in the path — " + unit.slice(6);
  return unit;
}

function unitOption(unit, s, labelOverride, nOverride) {
  const n = fmtNum(nOverride ?? unitRecords(s, unit)) + (s.truncated ? "+" : "");
  return '<option value="' + esc(unit) + '"' + (unit === recordUnit ? " selected" : "") +
         ">" + esc(labelOverride || unitLabel(unit)) + " (" + n + " records)</option>";
}

function unitOptions(s) {
  const order = ["folder", "parent", "pdf"];
  const depths = Object.keys(s.units).filter(u => u.indexOf("depth:") === 0).sort();
  let html = order.filter(u => s.units[u]).map(u => unitOption(u, s)).join("") +
             depths.map(u => unitOption(u, s)).join("");
  (s.id_patterns || []).forEach(p => {
    html += unitOption(p.record_unit, s,
      "One record per identifier in the path — " + p.label, p.n_records);
  });
  const known = recordUnit.indexOf("regex:") !== 0 ||
                (s.id_patterns || []).some(p => p.record_unit === recordUnit);
  html += '<option value="__custom__"' + (known ? "" : " selected") +
          ">Custom pattern…</option>";
  return html;
}

/* The schema step repeats the picker without the custom-pattern editor: a
   grouping typed on step 1 (or loaded from a saved schema) still has to appear
   here, so it is carried in as its own option rather than silently reset. */
function unitOptionsFor(s, current) {
  let html = unitOptions(s).replace(
    /<option value="__custom__"[^>]*>[^<]*<\/option>/, "");
  if (current && html.indexOf('value="' + esc(current) + '"') === -1) {
    html += '<option value="' + esc(current) + '" selected>' +
            esc(unitLabel(current)) + "</option>";
  }
  return html;
}

function unitHint(s) {
  const u = s.units[recordUnit] ||
            (s.id_patterns || []).find(p => p.record_unit === recordUnit);
  const ex = (u && u.examples) || [];
  const unmatched = u && u.n_unmatched
    ? ' <span class="st"><i class="dot warn"></i>' + fmtNum(u.n_unmatched) +
      " PDF(s) sit above this level and become their own record</span>" : "";
  return (ex.length ? "Records would be: " + ex.slice(0, 4).map(e =>
    "<code>" + esc(e) + "</code>").join(", ") + (ex.length > 4 ? " …" : "") : "") +
    unmatched;
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
  el("s-unit").innerHTML = unitOptionsFor(staging.scan, recordUnit);
  el("s-unit").value = recordUnit;
  unitChanged();
  if (settingsCache && settingsCache.is_mock) {
    draftState = "skipped";
    el("draft-panel").innerHTML = callout("warn",
        "<strong>Test mode is active</strong>, so the schema cannot be drafted " +
        "from your documents — the mock backend never reads them. The real " +
        "endpoint is configured on the server (<code>webapp_data/settings.json" +
        "</code>), or build the schema by hand below.") +
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
  if (el("s-unit").options.length) {
    // a saved schema may carry a grouping the picker does not list (e.g. a
    // custom regex from another archive) — add it rather than silently
    // building with whatever the select happened to show
    const want = s.record_unit || recordUnit;
    if (want && ![...el("s-unit").options].some(o => o.value === want)) {
      const o = document.createElement("option");
      o.value = want;
      o.textContent = unitLabel(want);
      el("s-unit").appendChild(o);
    }
    el("s-unit").value = want;
    unitChanged();
  }
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
  if (!(await validateSchema(false))) {
    el("build-msg").textContent = "The schema needs fixing — see above.";
    return;
  }

  const fd = new FormData();
  fd.append("staging_id", staging.staging_id);
  fd.append("schema", JSON.stringify(schema));
  fd.append("title", staging.source || schema.table);
  // how the archive is READ travels with the run, not with the schema, so the
  // same schema can be pointed at a differently-shaped archive later
  fd.append("record_unit", recordUnit);
  fd.append("ocr_mode", ocrMode);
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
