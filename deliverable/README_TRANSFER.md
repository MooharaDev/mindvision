# Corpus — schema-driven PDF → database pipeline (transfer notes)

Turns any directory tree of PDFs into a SQLite database + CSV exports, using a
schema and an **internal** OpenAI-compatible LLM endpoint for field extraction.
Folder layout and file names do not matter: PDFs are found by `%PDF-` magic
bytes anywhere under `--root`, even without a `.pdf` extension.

Two front doors on the same engine:
- **Corpus** (`webapp.py`) — the intranet web console. Point it at an archive,
  it scans the archive, drafts a schema from the documents themselves, and
  builds the database.
- **`pdf2db.py`** — the same pipeline headless, for very large archives and
  scheduled runs.

## Package contents (goes through EFT as one ZIP, well under 3 GB)
```
pdf2db.py                      # the whole pipeline engine, single file
webapp.py                      # Flask backend for the Corpus web console
webapp_selftest.py             # offline end-to-end test of the console
make_eft_package.py            # builds/verifies the checksummed EFT zip
templates/index.html           # console shell
static/                        # app.css, app.js, create.js, database.js,
                               #   settings.js — all self-contained, no CDN
schemas/failure_reports.json   # example schema (material-failure incidents)
requirements.txt               # pinned; provision internally, no runtime pip
README_TRANSFER.md             # this file
```
No pretrained weights are needed for this tool. No test data is included —
`--selftest` fabricates its own synthetic archive at runtime.

## Provision internally before running
- Python 3.10+ (developed/tested on 3.13)
- `pymupdf==1.28.0` — the only dependency of the CLI pipeline
- `flask==3.1.3` + `waitress==3.0.2` — only for the web console (`webapp.py`)
Everything else is Python stdlib.

**The wheels ride along in `wheels/`** — no PyPI request needed. Install fully
offline with:
```
python -m pip install --no-index --find-links wheels -r requirements.txt
```
Included platforms: Linux x86_64 (glibc 2.28+, i.e. RHEL 8+/Ubuntu 20.04+),
Windows amd64, macOS arm64 — each for Python 3.10–3.13. If the server is
something else (e.g. RHEL 7 or aarch64), request the packages through the
internal PyPI channel instead; `requirements.txt` carries the exact pins.

**OS package channel (NOT pip), only if scanned documents must be read:**
- `tesseract-ocr` + the language data (`tesseract-ocr-eng`, add `-ara` etc.)
- then set `TESSDATA_PREFIX` to the directory holding the `.traineddata` files,
  or set the `ocr_tessdata` config key.
This is optional. With `ocr_mode="off"` (the default) nothing above is needed
and scanned pages are reported as `no_text`, exactly as in earlier versions.
pymupdf calls the binary in-process: no download, no model file, no network,
and no vision/multimodal LLM anywhere in the path.

## Air-gap compliance
- Zero outbound traffic. The only network call is the LLM endpoint YOU
  configure via `--llm-base-url`; point it at the internal gateway.
  With `--llm-base-url mock` the pipeline runs fully offline (deterministic
  stand-in responses) — use that to smoke-test the plumbing anywhere.
- Proxy env vars (`http_proxy`/`HTTPS_PROXY`) are IGNORED by default, so
  ambient corporate proxy settings can never re-route LLM traffic
  (`--set llm_use_env_proxy=true` only if your gateway genuinely sits behind one).
- No downloads, no telemetry, no runtime pip.
- OCR, when enabled, is a local binary call. It adds no network surface.

## First run inside (suggested order)
```bash
# 1. prove the plumbing works on this machine, no data or network needed:
python pdf2db.py --selftest        # pipeline engine (43 checks; OCR checks are
                                   #   skipped with a clear note if tesseract
                                   #   is not installed — that is not a failure)
python webapp_selftest.py          # web console end-to-end (76 checks)

# 2. inventory + text extraction only (no LLM yet) — review text_report.csv
#    to see how many reports are scanned (low_yield) before spending LLM calls:
python pdf2db.py --root /path/to/archive --schema schemas/failure_reports.json \
    --out out/ --stages discover,extract

# 3. smoke the LLM on 10 records, then read out/raw_llm/ and records.csv:
export PDF2DB_API_KEY=...   # if the gateway needs a key
python pdf2db.py --root /path/to/archive --schema schemas/failure_reports.json \
    --out out/ --llm-base-url http://GATEWAY-HOST:PORT/v1 --llm-model MODELNAME \
    --stages llm,load --limit 10

# 4. full run; resumable — rerun with --resume after any interruption and
#    already-extracted records are skipped:
python pdf2db.py --root ... --schema ... --out out/ \
    --llm-base-url http://GATEWAY-HOST:PORT/v1 --llm-model MODELNAME --resume
```
Any CONFIG knob can be overridden with `--set key=value` (see `--help`) or a
`--config overrides.json` file kept next to your data.

## Measured scale (synthetic archive, 2026-07-25, laptop, mock LLM)

| Archive | Result |
|---|---|
| 2,400 PDFs / 2,000 records / 5,001 dirs, nested 4 deep, 188 MB | discover+extract+images **7 s**; load **1 s** |
| 24,000 PDFs / 20,000 records / 50,001 dirs, 1.8 GB | discover+extract+images **56 s**, peak RSS **83 MB**; load **5 s**, peak RSS **190 MB**; 17 MB SQLite |
| Console archive preview, 32,001 files | **5 s** (walk + magic-byte sniff + text probe) |
| Console API on a 2,000-record corpus | every endpoint **≤10 ms** (paging, search, status filter, stats, record open) |
| SQL on 20,000 records / 8,001 images | `images JOIN records` **1.5 ms**; record by id **<1 ms** |

Notes on the limits:
- **Memory grows with record count in the `load` stage only** (~9.5 KB/record:
  190 MB at 20k, so roughly 1 GB at 100k). discover/extract/images stream and
  stay flat. Everything else is served from SQLite, so the console does not
  care how big the corpus is.
- **Wall clock is dominated by the LLM**, not by the pipeline: at 2 s/record
  and `llm_workers=4`, 20,000 records is ~2.8 hours. Raise `llm_workers` if the
  gateway allows it; `--resume` makes interruptions cheap.
- **The console's archive preview stops walking at 50,000 files** and then
  reports every figure with a `+` to mark it a floor. The run itself always
  processes the whole archive.
- **Browser upload is the weak link at scale** — use server-path ingestion (or
  the CLI) for anything above a few thousand files.

## Scale controls
- `llm_workers` (default 4): parallel LLM requests. Raise to 8–16 if the
  gateway allows concurrency; set 1 for strictly sequential + politeness
  sleeps. This is the main wall-clock lever on large archives.
- `llm_ca_file`: path to a CA bundle when the internal gateway is HTTPS with
  a private CA (alternatively set the SSL_CERT_FILE env var).
- Images: the `images` stage exports every image embedded in a PDF **and**
  catalogues loose image files sitting beside the PDFs — jpeg, png, tif, gif
  and bmp, detected by magic bytes, not by extension. Loose files are
  catalogued in place (`origin` = path in the archive, `file` empty); embedded
  ones are exported to `out/images/<record>/` (`file` = that path). Both carry
  `record_id`, a reserved `modality` column, and dimensions. Skip the whole
  stage with `--set extract_images=false`.

  **The link is a declared foreign key.** `images.record_id`,
  `pdfs.record_id` and `review_queue.record_id` all
  `REFERENCES records(record_id)`, and each has an index, so the vision-phase
  training pairs are one indexed join:
  ```sql
  SELECT i.file, i.origin, r.<label>
  FROM images i JOIN records r ON i.record_id = r.record_id;
  ```
  (SQLite only *enforces* foreign keys when a connection sets
  `PRAGMA foreign_keys=ON`; the constraint is always declared, so the schema
  is self-describing to pandas, DBeaver and any other reader.)

  **Owning record.** In `record_unit=folder` an image belongs to the top-level
  folder it sits under. In `record_unit=pdf` a record is named after a PDF, so
  an image is attached to the PDF sharing its folder — but only when exactly
  one PDF is there. If the folder holds several PDFs or none, the image is
  still catalogued, its `record_id` is left NULL, and the reason is written to
  `issues.csv` as `image_owner_unresolved`. Images are never guessed into the
  wrong record and never silently dropped.

## Corpus web console (webapp.py) — for company use
```bash
PDF2DB_WEB_HOST=0.0.0.0 python webapp.py     # then open http://<server>:8080
```
Home is a ledger of every corpus (database) built so far — records, documents,
review debt, model and date, searchable and linkable (`#/corpus/<id>`).

**New corpus** is archive-first, in three steps:
1. **Archive** — upload a folder or a `.zip`, or give a server path. Corpus
   walks it read-only and reports what is there: PDF count, how many records
   each `record_unit` would produce (with example record ids), image count,
   folder depth, size, and a text-yield probe on a sample of the PDFs so
   scanned documents are visible *before* any LLM call is spent.
2. **Schema** — Corpus samples up to 3 records, sends their text to the
   configured model and comes back with a proposed table: field names, types,
   enums with the values actually seen, and the value it found in the first
   sample under each field. Everything stays editable; the schema is validated
   server-side before the run starts. Falls back to hand-building or a saved
   schema when the endpoint is mock/unavailable.
3. **Build** — live stage-by-stage progress, then straight into the corpus.

Each corpus has: a readout strip (records, documents, needs-review, issues),
records/review/images/documents/issues tables with status filter chips,
server-side search and pagination, distribution charts, a record view showing
each extracted value **next to the verbatim evidence quote** the model cited,
a review workspace (approve or correct, next/previous, progress counter;
corrections are schema-validated and written back to CSV + SQLite with the
record marked human-verified), retry for failed records, rename, and an export
panel that explains what every artifact is for.

The LLM endpoint, model and API key are configured ONLY by editing
`webapp_data/settings.json` on the server (chmod 600 — owner-only). The web
UI and API deliberately cannot set, or even read, the endpoint or key: the
settings API returns just the model name and mock/live state, a PUT carrying
endpoint fields is refused with 403, and a request-supplied endpoint on job
creation is ignored — so only the operator who can read that file ever sees
the configuration, and no browser user can point the stored key at a URL of
their choosing. The file is re-read on every request, so edits take effect
without a restart. Keys are excluded from run summaries and logs.
- Air-gap compliant: the page is fully self-contained (inline CSS/JS, system
  fonts, zero CDN/external requests). The only network traffic is the LLM
  endpoint configured server-side in `webapp_data/settings.json`.
- The stored API key lives only in `webapp_data/settings.json` (owner-only
  permissions); rotate it by editing that file, and treat it like a secret
  when backing up the data directory.
- Corpora persist under `./webapp_data/` (override with `PDF2DB_WEB_DATA`);
  finished runs reappear in the ledger after a server restart. An archive
  uploaded but never built stays in `webapp_data/staging/` and is swept
  automatically after 24 h.
- Default bind is 127.0.0.1; set `PDF2DB_WEB_HOST=0.0.0.0` for intranet use.
- **Server-path ingestion (recommended inside):** in step 1 choose "Folder on
  this server" and give the archive's absolute path — it is scanned and
  processed in place, nothing is uploaded or duplicated. Optionally restrict
  which paths are allowed with `PDF2DB_ALLOWED_ROOTS=/data/archives:/mnt/x`.
- Serving: `waitress` is used automatically when installed (production WSGI,
  `PDF2DB_WEB_THREADS` default 16); without it the app falls back to Flask's
  dev server (testing only). For always-on use, run it as a system service
  and note that a server restart marks in-flight runs failed — use the Retry
  button to resume them (already-extracted records are skipped).
- Tables are served straight from SQLite with pagination and full-table
  search, so 10k+ record databases stay fast in the browser.
- Browser upload is comfortable for hundreds of PDFs (or one .zip); for big
  archives use server-path ingestion or run `pdf2db.py` directly.

## Building the EFT package
```bash
python make_eft_package.py            # writes pdf2db_eft_v4.0_<date>.zip + SHA-256 manifest
python make_eft_package.py --verify pdf2db_eft_v4.0_<date>.zip   # after transfer
```

## Writing a schema

The console drafts one for you from your own documents (step 2 above); this is
the same structure, for hand-editing or for the CLI.
```json
{
  "table": "incidents",            // SQLite file + table naming
  "record_unit": "folder",         // how PDFs group into records — see the
                                   //   grammar table below. Overridable per
                                   //   run with --record-unit, so one schema
                                   //   serves differently-shaped archives.
  "task_description": "context given to the LLM about what a record is",
  "fields": [
    {"name": "failure_class", "type": "enum", "required": true,
     "options": ["corrosion", "..."], "description": "shown to the LLM"},
    {"name": "incident_date", "type": "date", "required": false, "description": "..."}
  ]
}
```
Types: `string`, `integer`, `number`, `boolean`, `enum` (needs `options`),
`date` (ISO). Field names must be lowercase identifiers and not collide with
the pipeline's provenance columns (the script rejects bad schemas loudly).
Beyond your fields, every LLM reply must include `_confidence` (0-1) and
`_evidence` (verbatim quotes per field) — these drive the review queue.

### `record_unit` grammar
Given a PDF's path *relative to `--root`*:

| Value | One record is | `2003/Q1/INC-2231/report.pdf` becomes |
|---|---|---|
| `pdf` | one PDF file | `2003/Q1/INC-2231/report` |
| `folder` | the top-level subfolder | `2003` |
| `parent` | the immediate parent folder | `2003/Q1/INC-2231` |
| `depth:N` | the folder N segments below root | `depth:2` → `2003/Q1` |
| `regex:PATTERN` | the first match in the path | `regex:(?P<id>INC-\d+)` → `INC-2231` |

`regex:` uses the named group `id` if present, else group 1, else the whole
match. A path that does not match still becomes its own record (nothing is
dropped) and is logged as `no_regex_match`; a path shallower than the requested
folder level is logged as `shallower_than_record_unit`. Bad specs (`depth:0`,
an unclosed bracket) are rejected when the schema loads, before any work.

The console scores every mode against your archive and shows record counts and
example ids before you commit; `POST /api/staging/<id>/grouping` does the same
for an arbitrary pattern.

### Discovery and OCR config
All settable via `--set key=value`, `--config FILE`, or the console.

| Key | Default | What it does |
|---|---|---|
| `record_unit` | `""` | overrides the schema's value; `""` = use the schema |
| `include_globs` | `""` | `;`-separated fnmatch patterns a path must match |
| `exclude_globs` | `~$*;*/~$*;.*;*/.*;__MACOSX/*` | skipped outright (Office lock files, dotfiles) |
| `max_depth` | `0` | only files at most N segments deep; 0 = no limit |
| `max_file_mb` | `0` | skip PDFs larger than this; 0 = no limit |
| `follow_symlinks` | `false` | walking symlinked dirs risks loops and escaping `--root` |
| `dedupe_identical` | `true` | byte-identical PDFs **inside one record** read once; only same-size files are hashed, shortest path wins |
| `file_order` | `path` | order within a record: `path`, `mtime` (oldest first), `size` (largest first) |
| `extract_workers` | `1` | parallel PDFs in the extract stage; raise when OCR dominates |
| `ocr_mode` | `off` | `off` / `auto` (pages with no text layer) / `augment` (image regions on every page, keeping digital text) / `force` (every page as an image) |
| `ocr_language` | `eng` | tesseract codes, e.g. `eng+ara` |
| `ocr_dpi` | `300` | rasterisation dpi; higher = slower, better on small print |
| `ocr_tessdata` | `""` | explicit tessdata dir; `""` = autodetect via `TESSDATA_PREFIX` |
| `ocr_page_char_threshold` | `50` | `auto`: a page with fewer native chars than this is OCR'd |
| `ocr_max_pages_per_pdf` | `0` | cap OCR pages per document; 0 = no cap |

Every enum and range above is validated **before** the run starts, so a typo
fails in the first second rather than three hours into an archive. Setting
`ocr_mode` without tesseract present is a startup failure, not a silent
fallback to empty records.

OCR costs roughly 1-3 s per scanned page. Check `text_report.csv`
(`ocr_pages`, `native_chars`, `ocr_chars`) to see what it actually did.

## What comes out (all in --out)
| Artifact | What it is |
|---|---|
| `pdf_inventory.csv` | every PDF found (magic-byte detection), sizes, mtimes, record grouping — post-filter and post-dedupe |
| `text_report.csv` | per-PDF pages/chars, `low_yield` scan flag, `ocr_pages`/`native_chars`/`ocr_chars`, per-file errors |
| `records_text.csv` + `text/` | per-record concatenated text sent to the LLM |
| `raw_llm/<record>.json` | every raw LLM attempt + validation errors (full audit) |
| `extractions.jsonl` | validated per-record JSON (append-only; powers `--resume`; an llm run without `--resume` first backs it up to `.bak`; a line torn by an interruption is dropped and re-extracted on resume) |
| `<table>.db` | SQLite: `records`, `pdfs`, `images`, `review_queue`; foreign keys to `records` + indexes on the join/status columns |
| `records.csv`, `pdfs.csv`, `images.csv`, `review_queue.csv` | CSV exports of those tables |
| `images/<record>/` | images extracted out of the PDFs (loose files stay where they are) |
| `issues.csv` | every skipped/failed/suspicious file with reason (read this!); rows from stages not re-run are preserved across invocations |
| `run_summary.json` | counts by status + the exact config used (key redacted) |

Record `status` values: `ok`, `needs_review`, `no_text` (scanned/empty —
below `min_record_chars`), `llm_failed` (transport), `validation_failed`
(bad JSON after retries), `pending_llm` (not sent yet, e.g. `--limit` runs).

## Review queue
`review_queue.csv` collects records with: `_confidence` below 0.7, a required
field null, scan-suspect source PDFs, truncated text, plus a seeded ~8 % random
audit sample of "ok" records (measure the LLM's real error rate on this sample
before trusting the labels for anything downstream). Fill `corrected_json`,
`reviewer`, `verdict` during review; those columns are yours.

## Known limitations (deliberate v1 scope)
- Scanned reports need `ocr_mode` on **and** the tesseract OS package. Left off,
  they are flagged `no_text` rather than guessed at. OCR quality on 1970s-80s
  microfiche-grade scans is untested — check `records.csv` for the `ocr_used`
  review reason and read a sample before trusting a decade wholesale.
- OCR reads characters, not layout: tables in scanned pages come back as loose
  text in reading order, not as rows and columns.
- Long documents are head+tail truncated at `max_record_chars` (default
  80 000 chars) rather than chunk-merged; truncated records are routed to
  review by default.
- A folder whose only PDF-named file fails the magic-byte check yields no
  records row — it is visible ONLY in issues.csv (`pdf_extension_but_not_pdf`).
