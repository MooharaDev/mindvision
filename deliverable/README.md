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

**Only if your archive contains scanned documents**, also request these from
the internal *OS package* channel (they are not pip packages):

```
tesseract-ocr        the OCR engine
tesseract-ocr-eng    English data (add other languages as needed)
```

Then set `TESSDATA_PREFIX` to the directory holding the `.traineddata` files.
Nothing is downloaded and no vision model is involved — see §7. Without this,
scanned pages are reported as `no_text` and everything else works normally.

## 2. Check the transfer arrived intact

```bash
python make_eft_package.py --verify <the-package>.zip
```
Prints a SHA-256 result per file. Then unzip it anywhere you can write.

## 3. Prove it works — offline, no data, no network

```bash
python pdf2db.py --selftest      # engine        — expect "selftest PASSED"
python webapp_selftest.py        # web console   — expect "76/76 checks passed"
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

1. **Configure the extraction endpoint — on the server, not in the browser.**
   Edit `webapp_data/settings.json` (create it next to where the server runs,
   or under `PDF2DB_WEB_DATA`) as the user that runs the app:

   ```json
   {
    "llm_base_url": "https://gateway.internal/v1",
    "llm_model": "internal-model",
    "llm_api_key": "the-key-if-the-gateway-needs-one"
   }
   ```

   Then `chmod 600 webapp_data/settings.json`. The file is read on every
   request — no restart needed. The UI never shows, accepts, or transmits the
   URL or key; only the operator who can read this file sees them. Until the
   file exists it runs in `mock` mode and produces fake placeholder values.
2. **New corpus** → step 1, give it the archive:
   - *Folder on this server* — paste an absolute path. Read in place, nothing
     is copied. **Use this for large archives.**
   - *Upload a folder* — fine up to a few thousand files, or drop a `.zip`.

   It reports what is in there before anything runs: PDF count, how many
   records **each grouping** would make, images, folder depth, files skipped as
   lock/hidden files, and whether the documents contain selectable text.

   Two things to set here:

   - **What counts as one record.** Pick the grouping whose record count
     matches what you know about the archive — the list shows the real number
     for each. If your paths carry an incident number, choose the pattern
     option (or *Custom pattern…* and type your own); the count and example
     record names update as you type. See §7.
   - **Whether to OCR scans**, if the server has Tesseract. Offered only when
     it is installed, and pre-selected when the sampled documents look scanned.

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

## 7. Two things worth understanding

### What counts as one record

Archives are rarely tidy, so grouping is described rather than assumed. Set it
in the console on step 1, or with `--record-unit` on the CLI.

| Setting | One record is | Use when |
|---|---|---|
| `pdf` | one PDF file | every document stands alone |
| `folder` | a top-level subfolder, PDFs merged | `INC-2231/report.pdf` |
| `parent` | the folder a PDF sits directly in | `2003/Q1/INC-2231/report.pdf` |
| `depth:N` | the folder N levels below the root | you want to group by year or quarter |
| `regex:PATTERN` | whatever the pattern matches in the path | the id is in the **file name**, or the depth is inconsistent |

`regex:` is the one that rescues a messy archive. Everything whose path
contains the same match becomes one record, no matter where it sits:

```bash
--record-unit 'regex:(?P<id>INC-\d+)'
```

Name the group `id`, or just use the first bracketed group. Paths that do not
match still become their own record — nothing is ever dropped silently — and
each one is logged to `issues.csv` as `no_regex_match`.

**Getting this wrong is expensive**: too coarse fuses unrelated incidents into
one row; too fine splits one incident across several. The console shows the
record count and example names for every option before you commit — check that
the number matches what you expect from the archive.

Also on by default during discovery: editor lock files (`~$…`) and hidden files
are skipped, and byte-identical duplicates inside one record (`report copy
2.pdf`) are read once. Both are reported in `issues.csv`. Tune with
`--include`, `--exclude`, `--set max_depth=`, `--set dedupe_identical=false`.

### Reading scanned documents

Old archives are full of scans, which hold no selectable text. With Tesseract
provisioned (§1) the pipeline can read them — **locally, in-process, with no
model download, no vision model and no network call.**

| `--ocr` | What it does |
|---|---|
| `off` (default) | never OCR; scans are honestly reported as `no_text` |
| `auto` | OCR only pages with no text layer — digital pages untouched |
| `augment` | every page, image regions only: keeps the digital text and adds text from scanned figures pasted into typed reports |
| `force` | every page as an image, ignoring any text layer |

`auto` is the right answer for most archives. Use `augment` when typed reports
have scanned figures with captions worth reading.

```bash
python pdf2db.py --root ARCHIVE --schema S.json --out out/ --ocr auto
```

Expect roughly **1-3 seconds per scanned page** — check `text_report.csv` for
how many pages actually needed it, and raise `--set extract_workers=4` if OCR
dominates the runtime. Records where OCR was used carry `ocr_pages` and are
always routed to review with the reason `ocr_used`: OCR text is never as
trustworthy as a real text layer, so a human should confirm it.

If `--ocr` is set and Tesseract cannot be found, the run **stops immediately**
with instructions rather than silently producing empty records.

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

- **Nothing extracts, everything says `no_text`** — the PDFs are scans. Turn
  OCR on (§7): `--ocr auto`, or the control on step 1 of the console. It needs
  the `tesseract` OS package provisioned (§1); until then the tool flags scans
  rather than guessing at them.
- **`ocr_mode=... but Tesseract's tessdata directory was not found`** — the
  package is missing, or `TESSDATA_PREFIX` does not point at the folder holding
  the `.traineddata` files. The console's **Settings** page shows what the
  server found.
- **The record count is not what you expected** — the grouping is wrong for
  this archive, not the tool. Re-check §7 against the counts the preview shows;
  `--record-unit 'regex:...'` handles layouts the folder modes cannot.
- **The console starts but pages look stale after an update** — assets are
  versioned automatically; a normal browser refresh is enough.
- **A run stopped halfway** (server restart) — open the corpus and press
  **Retry failed**; records already extracted are skipped.
- **Anything unexplained** — `issues.csv` and `out/raw_llm/` hold the full
  audit trail: every skipped file and every raw model reply.
