/* Corpus — settings: the extraction endpoint, model and stored API key. */
"use strict";

async function enterSettings() {
  const s = await refreshSettings();
  if (!s) {
    el("set-msg").innerHTML = callout("bad", "Could not load settings.");
    return;
  }
  el("set-url").value = s.llm_base_url;
  el("set-model").value = s.llm_model;
  el("set-key").value = "";
  paintKeyHint(s);
  el("set-msg").innerHTML = "";

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
    row("Network", "self-contained — the browser loads nothing from outside " +
        "this server") +
    row("Engine", "<code>pdf2db.py</code> — the same pipeline also runs headless " +
        "from the command line for very large archives");
}

function paintKeyHint(s) {
  el("set-keyhint").textContent = s.has_key
    ? "— a key is stored (" + s.key_hint + ")" : "— none stored";
}

async function saveSettings() {
  const body = { llm_base_url: el("set-url").value,
                 llm_model: el("set-model").value };
  if (el("set-key").value) body.llm_api_key = el("set-key").value;
  try {
    const s = await api("/api/settings", { method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    settingsCache = s;
    paintEndpoint();
    el("set-key").value = "";
    paintKeyHint(s);
    el("set-msg").innerHTML = callout("good", "Saved. Every new run uses this" +
      (s.is_mock ? " — note that <b>mock test mode</b> is still active." : "."));
  } catch (e) {
    el("set-msg").innerHTML = callout("bad", esc(e.message));
  }
}

async function clearKey() {
  if (!confirm("Remove the stored API key?")) return;
  try {
    const s = await api("/api/settings", { method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ llm_api_key: "" }) });
    settingsCache = s;
    paintKeyHint(s);
    toast("Stored key removed");
  } catch (e) { toast(e.message); }
}

async function testSettings() {
  el("set-msg").innerHTML = '<p class="hint">Testing the endpoint…</p>';
  const body = { llm_base_url: el("set-url").value };
  if (el("set-key").value) body.llm_api_key = el("set-key").value;
  try {
    const r = await api("/api/settings/test", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    const models = r.models || [];
    const note = models.length
      ? '<br><span class="hint">models include ' +
        esc(models.slice(0, 6).join(", ")) + (models.length > 6 ? ", …" : "") +
        "</span>" : "";
    el("set-msg").innerHTML = r.ok
      ? callout("good", esc(r.detail) + note)
      : callout("bad", "<strong>Connection failed.</strong> " + esc(r.detail));
  } catch (e) {
    el("set-msg").innerHTML = callout("bad", esc(e.message));
  }
}
