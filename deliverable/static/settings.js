/* Corpus — settings: OCR defaults and a read-only view of the extraction
   endpoint. The endpoint, model and API key are configured ONLY in
   webapp_data/settings.json on the server (owner-only file, read on every
   request) — deliberately not editable, or even fully visible, from here. */
"use strict";

async function enterSettings() {
  const s = await refreshSettings();
  if (!s) {
    el("endpoint-body").innerHTML = callout("bad", "Could not load settings.");
    return;
  }
  el("endpoint-body").innerHTML =
    (s.is_mock
      ? callout("warn", "<strong>Test mode (mock) is active.</strong> Runs " +
          "produce fake placeholder values and no network call is made.")
      : callout("good", "Live endpoint — extracting with <b>" +
          esc(s.llm_model) + "</b>.")) +
    '<p class="hint" style="margin-top:var(--s3)">The endpoint URL, model ' +
    "name and API key live in <code>webapp_data/settings.json</code> on this " +
    "server — an owner-only file the operator edits directly. It is read on " +
    "every request, so a change takes effect without a restart and the key " +
    "is never shown in, or accepted from, this page.</p>";
  paintOcr(s);

  let health = {};
  try { health = await api("/healthz"); } catch { /* shown as unknown below */ }
  const row = (k, v) => '<div class="row"><span class="k">' + esc(k) +
    '</span><span class="v">' + v + "</span></div>";
  el("about-list").innerHTML =
    row("Version", esc(health.app || "Corpus") + " " + esc(health.version || "?")) +
    row("Runs in progress", health.running ?? "—") +
    row("Extraction mode", s.is_mock
      ? "mock — offline test values, no network call"
      : "live endpoint · " + esc(s.llm_model)) +
    row("Endpoint config", "<code>webapp_data/settings.json</code> on this " +
        "server — owner-only, never exposed through the API") +
    row("Network", "self-contained — the browser loads nothing from outside " +
        "this server") +
    row("Engine", "<code>pdf2db.py</code> — the same pipeline also runs headless " +
        "from the command line for very large archives");
}

/* OCR needs a binary this server may simply not have. Rather than offer a
   control that always fails, the panel states what is missing and how to fix
   it — the operator is inside an air gap and cannot go and look it up. */
function paintOcr(s) {
  el("set-ocr").value = s.ocr_mode || "off";
  el("set-ocr-lang").value = s.ocr_language || "eng";
  el("set-ocr-dpi").value = s.ocr_dpi || 300;
  const disabled = !s.ocr_available;
  ["set-ocr", "set-ocr-lang", "set-ocr-dpi"].forEach(id => {
    el(id).disabled = disabled;
  });
  el("set-ocr-msg").innerHTML = disabled
    ? callout("warn", "<strong>Tesseract is not installed on this server</strong>, " +
        "so scanned pages cannot be read. Ask for the <code>tesseract</code> package " +
        "and its language data to be provisioned, then set <code>TESSDATA_PREFIX</code> " +
        "(or <code>ocr_tessdata</code>) and restart. No model download and no network " +
        "access is involved.")
    : '<p class="hint">Language data found at <code>' + esc(s.ocr_tessdata) +
      "</code>.</p>";
}

async function saveOcr() {
  const body = { ocr_mode: el("set-ocr").value,
                 ocr_language: el("set-ocr-lang").value,
                 ocr_dpi: Number(el("set-ocr-dpi").value) };
  try {
    const s = await api("/api/settings", { method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    settingsCache = s;
    paintOcr(s);
    el("set-ocr-msg").innerHTML = callout("good",
      s.ocr_mode === "off" ? "Saved. New corpora will skip scanned pages."
        : "Saved. New corpora default to <b>" + esc(s.ocr_mode) + "</b> OCR.");
  } catch (e) {
    el("set-ocr-msg").innerHTML = callout("bad", esc(e.message));
  }
}
