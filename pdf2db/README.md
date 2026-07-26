# Corpus — how to run it

Turns a folder of PDFs into a queryable database. Point it at an archive; it
reads the documents, drafts a table from them, and fills one row per record
using your internal LLM gateway. Runs entirely inside the network — no
internet, no downloads, no telemetry.

`README_TRANSFER.md` (same folder) is the full reference: schema format,
every output file, tuning knobs, scale numbers, known limits.

---

## 1. Provision (once, ahead of time)

Python 3.10 or newer, plus these from the internal PyPI channel:

```
pymupdf==1.28.0     # required
flask==3.1.3        # web console only
waitress==3.0.2     # web console only (production server)
```

Everything else is Python standard library.

## 2. Check the transfer arrived intact

```bash
python make_eft_package.py --verify <the-package>.zip
```
Prints a SHA-256 result per file. Then unzip it anywhere you can write.

## 3. Prove it works — offline, no data, no network

```bash
python pdf2db.py --selftest      # engine        — expect "selftest PASSED"
python webapp_selftest.py        # web console   — expect "61/61 checks passed"
```
Both fabricate their own tiny test archive and use a built-in mock model.
If these pass, the installation is sound.

## 4. Start the console

```bash
PDF2DB_WEB_HOST=0.0.0.0 python webapp.py          # Linux / macOS
```
```bat
set PDF2DB_WEB_HOST=0.0.0.0 && python webapp.py   :: Windows
```
Then open `http://<this-server>:8080` in a browser. It should print
`serving with waitress` — if it says it fell back to the Flask dev server,
`waitress` was not provisioned; fine for testing, not for shared use.

| Environment variable | Default | What it does |
|---|---|---|
| `PDF2DB_WEB_HOST` | `127.0.0.1` | set `0.0.0.0` to serve the intranet |
| `PDF2DB_WEB_PORT` | `8080` | listening port |
| `PDF2DB_WEB_DATA` | `./webapp_data` | where corpora are stored — back this up |
| `PDF2DB_ALLOWED_ROOTS` | (unset) | restrict server-path ingestion, e.g. `/data/archives:/mnt/x` |

## 5. First use, in the browser

1. **Settings** → enter the internal gateway's **Base URL** (ends in `/v1`),
   the **model name**, and an **API key** if the gateway needs one →
   **Test connection**. Until you do this it runs in `mock` mode and produces
   fake placeholder values.
2. **New corpus** → step 1, give it the archive:
   - *Folder on this server* — paste an absolute path. Read in place, nothing
     is copied. **Use this for large archives.**
   - *Upload a folder* — fine up to a few thousand files, or drop a `.zip`.

   It reports what is in there before anything runs: PDF count, how many
   records each grouping would make, images, folder depth, and whether the
   documents actually contain selectable text.
3. Step 2 — it samples 3 of your documents and **drafts the table**. Edit any
   field, delete what you do not need, then **Build corpus**.
4. Step 3 — watch it run, then open the corpus: browse and search records,
   review the ones it flagged, download the database.

## 6. Very large archives: run it headless

The console and the CLI are the same engine. For tens of thousands of
documents, or to run overnight:

```bash
export PDF2DB_API_KEY=...              # if the gateway needs a key

# text extraction only — check how many documents are scans before
# spending any LLM calls (read out/text_report.csv)
python pdf2db.py --root /path/to/archive --schema myschema.json \
    --out out/ --stages discover,extract

# full run, resumable: rerun with --resume after any interruption
python pdf2db.py --root /path/to/archive --schema myschema.json --out out/ \
    --llm-base-url http://GATEWAY:PORT/v1 --llm-model MODELNAME --resume
```
Save a schema from the console (**Save**) and it lands in
`webapp_data/schemas/` ready to pass as `--schema`.

## What you get

In the console's **Export** tab, or in `out/`:

| File | What it is |
|---|---|
| `<table>.db` | SQLite: `records`, `pdfs`, `images`, `review_queue`, with foreign keys and indexes |
| `records.csv` | one row per record — your fields plus status, confidence, provenance |
| `images.csv` | images found in and beside the PDFs, keyed to records by `record_id` |
| `review_queue.csv` | what a human should check, and what they decided |
| `issues.csv` | every file skipped or failed, with the reason — **read this** |
| `run_summary.json` | counts, timings and the exact config used (key redacted) |

## If something goes wrong

- **Nothing extracts, everything says `no_text`** — the PDFs are scans. This
  tool reads text only; it flags scans rather than guessing. OCR would need
  tesseract provisioned separately.
- **The console starts but pages look stale after an update** — assets are
  versioned automatically; a normal browser refresh is enough.
- **A run stopped halfway** (server restart) — open the corpus and press
  **Retry failed**; records already extracted are skipped.
- **Anything unexplained** — `issues.csv` and `out/raw_llm/` hold the full
  audit trail: every skipped file and every raw model reply.
