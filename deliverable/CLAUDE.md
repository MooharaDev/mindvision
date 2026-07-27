# Corpus (pdf2db) — project context

Schema-driven pipeline that turns a directory tree of PDF failure reports into
a SQLite database + CSV exports, with an OpenAI-compatible LLM doing field
extraction. Two front doors, one engine: `webapp.py` (intranet web console)
and `pdf2db.py` (headless CLI for large archives). `README.md` = how to run;
`README_TRANSFER.md` = full reference (schema format, outputs, tuning, scale
numbers); `PRODUCT.md` = who it is for; `DESIGN.md` = visual/interaction rules
for the console.

## Hard constraints — this code runs inside an air-gapped network
- Fully offline. The ONLY permitted network call is the internal LLM gateway,
  configured server-side in `webapp_data/settings.json` (console) or
  `--llm-base-url` (CLI). Never add any other outbound request: no package
  downloads, no runtime pip, no CDN links, no web fonts, no telemetry.
- Dependencies are exactly `requirements.txt` (pymupdf; flask+waitress for the
  console). New packages cannot be pip-installed here — they must be
  provisioned through the internal channel, so treat the dependency set as
  frozen unless that has been arranged.
- The web UI is deliberately framework-free (vanilla JS in `static/`, one
  template). All assets are local; keep it that way.
- `webapp_data/settings.json` holds the LLM API key (owner-only file). Never
  print it, log it, or move it into code. The web API must never accept or
  reveal endpoint/key values.

## Layout
- `pdf2db.py` — the whole engine; stages `discover,extract,images,llm,load`,
  each restartable (`--resume`), each writing auditable artifacts to `--out`.
- `webapp.py` — Flask backend; corpora live under `webapp_data/jobs/<id>/`.
- `templates/index.html` + `static/app.js|create.js|database.js|settings.js`
  — hash-routed single page (`#/corpora`, `#/corpus/<id>/<tab>`, `#/new`).
- `schemas/failure_reports.json` — example schema, served by
  `/api/example-schema`.

## Coding style (match it)
- Minimal, flat, readable; few standalone files; no unnecessary classes.
- Fail loudly: no bare `except`, no silent skips — every skipped or failed
  file gets a row in `issues.csv` with a reason.
- Every pipeline stage writes an auditable artifact (CSV/JSON); debugging
  starts from artifacts, not from a debugger.
- Deterministic: sorted directory walks, seeded sampling, stable ids.
- Config via the CONFIG block / `--set key=value` / `--config file.json` —
  validated before the run starts; a typo must fail in the first second.

## Fixed decisions (do not relitigate)
- Records group by `record_unit`; labels/fields live at record level.
- Train/test splits downstream must be by `record_id`, never by image or PDF
  (leakage — see the Export tab text in `database.js`).
- Low-confidence (<0.7), missing-required, OCR'd, truncated and a seeded ~8%
  audit sample of records go to the review queue; human review writes back to
  BOTH `records.csv` and the SQLite db and marks the record `reviewed`.
- Reserved pipeline columns (`status`, `_confidence`, `_evidence`, ...) are
  never schema fields; `validate_schema` enforces this.

## Testing and debugging here
- `python pdf2db.py --selftest` — 43 offline checks, fabricates its own tiny
  synthetic archive, needs no data and no network (OCR checks self-skip when
  tesseract is absent).
- `--llm-base-url mock` runs the full pipeline with deterministic fake
  extractions — use it to test plumbing without spending gateway calls.
- When a run misbehaves, read in order: `issues.csv`,
  `raw_llm/<record>.json` (every attempt + validation errors),
  `run_summary.json` (exact config used, key redacted).
- A fuller console test harness (`webapp_selftest.py`, 82 checks) exists in
  the source repository outside the network; it is not shipped in this
  package.
