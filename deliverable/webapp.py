#!/usr/bin/env python3
"""
webapp.py — Corpus: intranet web console for the pdf2db pipeline (backend).

Frontend lives in ./templates/index.html + ./static/* (plain HTML/CSS/JS,
fully self-contained: system fonts, inline SVG sprite, zero CDN/external
requests — air-gap safe). This file is the Flask API + file server.

Views served to the browser:
  Corpora    — ledger of every corpus (database) built from a PDF archive
  New corpus — archive first: drop the folder, the app scans it and drafts a
               schema from the documents themselves, you edit it, then build
  Settings   — LLM endpoint, model and API key stored server-side

Storage layout (PDF2DB_WEB_DATA, default ./webapp_data):
  settings.json   — endpoint config incl. API key (chmod 600, never logged,
                    never echoed back in full by the API)
  schemas/        — saved schema library
  staging/<id>/   — an uploaded archive that has been scanned but not yet
                    built; promoted (moved) into jobs/ when the run starts,
                    swept after STAGING_TTL_S if abandoned
  jobs/<id>/      — one per corpus: archive/ (uploaded PDFs), meta.json
                    (title/source), scan.json, schema.json, out/ (pipeline
                    artifacts + SQLite db)

Run:  python webapp.py          then open  http://127.0.0.1:8080
Env:  PDF2DB_WEB_HOST (default 127.0.0.1 — set 0.0.0.0 to serve the intranet)
      PDF2DB_WEB_PORT (default 8080)
      PDF2DB_WEB_DATA (default ./webapp_data)

Provision internally: flask (pinned in requirements.txt).

Design notes for maintainers:
- Status chart palette validated (dataviz six-checks) for light and dark
  surfaces; neutral gray is intentionally low-chroma and every bar carries a
  direct label + count. Bar order keeps amber and red non-adjacent (CVD).
- The stored API key is deliberately server-side per operator request
  (shared company use): settings.json is chmod 600, the API returns only a
  masked hint, and pdf2db excludes the key from run_summary.json.
"""

import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pdf2db  # noqa: E402  (the pipeline engine, same directory)

APP_NAME = "Corpus"
APP_VERSION = "4.0"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PDF2DB_WEB_DATA") or (Path.cwd() / "webapp_data"))
JOBS_DIR = DATA_DIR / "jobs"
STAGING_DIR = DATA_DIR / "staging"
SCHEMAS_DIR = DATA_DIR / "schemas"
SETTINGS_PATH = DATA_DIR / "settings.json"
EXAMPLE_SCHEMA = BASE_DIR / "schemas" / "failure_reports.json"

app = Flask(__name__, static_folder=str(BASE_DIR / "static"))
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 ** 3  # 4 GB upload ceiling

JOBS = {}  # job_id -> dict; see _new_job_state()
JOBS_LOCK = threading.Lock()
REVIEW_LOCK = threading.Lock()    # serializes read-modify-write of run outputs
SETTINGS_LOCK = threading.Lock()

DOWNLOADABLE = {"records.csv", "pdfs.csv", "review_queue.csv", "issues.csv",
                "images.csv", "run_summary.json", "extractions.jsonl"}
TABLES = {"records", "pdfs", "review_queue", "issues", "images"}
STAGE_PCT = {"queued": 0, "discover": 3, "extract": 8, "images": 12, "llm": 15,
             "load": 95, "finished": 100}
# optional guard for server-path ingestion: colon-separated list of directories
# jobs may read from (empty = any readable path; set it in company deployments)
ALLOWED_ROOTS = [Path(p).expanduser().resolve()
                 for p in os.environ.get("PDF2DB_ALLOWED_ROOTS", "").split(":")
                 if p.strip()]
SCHEMA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")
STATUS_ORDER = ["ok", "reviewed", "needs_review", "pending_llm", "no_text",
                "llm_failed", "validation_failed"]
MAX_ZIP_MEMBERS = 20000
MAX_ZIP_BYTES = 6 * 1024 ** 3
DEFAULT_SETTINGS = {"llm_base_url": "mock", "llm_model": "internal-model",
                    "llm_api_key": "",
                    # OCR is a property of the host (is the tesseract binary there?),
                    # so it lives in settings and every new corpus inherits it
                    "ocr_mode": "off", "ocr_language": "eng", "ocr_dpi": 300}

# pdf2db CONFIG keys the console is allowed to set per corpus. Everything else
# stays at the engine default: this is the whole surface the UI can touch, so a
# new knob has to be added here deliberately rather than by accident.
INGEST_KEYS = {
    "record_unit": str, "include_globs": str, "exclude_globs": str,
    "file_order": str, "dedupe_identical": bool, "max_depth": int,
    "max_file_mb": int, "sniff_all_files": bool,
    "ocr_mode": str, "ocr_language": str, "ocr_dpi": int,
}

# ---- archive preview + schema drafting ----
STAGING_TTL_S = 24 * 3600      # abandoned staging areas are swept at startup
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
SCAN_FILE_CAP = 50000          # stop walking a gigantic archive during preview
                               # (~4 s at 50k files; past it the scan reports
                               # partial counts and flags them as a floor —
                               # the run itself still processes everything)
PROBE_DOCS = 4                 # PDFs opened to measure text yield in the preview
SUGGEST_SAMPLES = 3            # records sent to the LLM when drafting a schema
SUGGEST_CHARS = 7000           # per-record text budget for the drafting prompt
ARTIFACT_HELP = {
    "records.csv": "One row per record: your schema's fields plus status, "
                   "confidence and provenance columns. The training table.",
    "review_queue.csv": "Records flagged for human eyes, with the reason, "
                        "the reviewer verdict and any corrections applied.",
    "pdfs.csv": "Every PDF found, its record, page count and text yield.",
    "images.csv": "Embedded and loose images keyed by record_id — join to "
                  "records for image/label training pairs.",
    "issues.csv": "Every file skipped or failed, with the reason. Nothing is "
                  "dropped silently.",
    "run_summary.json": "Counts, config and timings for the whole run.",
    "extractions.jsonl": "Raw per-record extraction results, one JSON per line.",
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ settings
def _load_settings():
    with SETTINGS_LOCK:
        if SETTINGS_PATH.exists():
            try:
                s = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                return {k: s.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
            except (json.JSONDecodeError, OSError):
                pass
        return dict(DEFAULT_SETTINGS)


def _save_settings(s):
    with SETTINGS_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        os.chmod(SETTINGS_PATH, 0o600)  # operator-only: file holds the API key


def _public_settings(s):
    key = s.get("llm_api_key") or ""
    tess = pdf2db.tessdata_dir(pdf2db.CONFIG)
    return {"llm_base_url": s["llm_base_url"], "llm_model": s["llm_model"],
            "has_key": bool(key),
            "key_hint": ("…" + key[-4:]) if len(key) >= 8 else ("set" if key else ""),
            "is_mock": s["llm_base_url"].strip() == "mock",
            "ocr_mode": s.get("ocr_mode", "off"),
            "ocr_language": s.get("ocr_language", "eng"),
            "ocr_dpi": s.get("ocr_dpi", 300),
            # the console must not offer OCR the host cannot actually perform
            "ocr_available": tess is not None,
            "ocr_tessdata": tess or ""}


# ------------------------------------------------------------------ job state
def _new_job_state(job_id, table, title="", source=""):
    return {"id": job_id, "table": table, "title": title or table,
            "source": source, "status": "running", "stage": "queued",
            "done": 0, "total": 0, "pct": 0, "error": None, "summary": None,
            "created": _now(), "n_files": 0,
            "endpoint": "", "model": "", "is_mock": False}


def _read_meta(job_dir):
    p = Path(job_dir) / "meta.json"
    if p.exists():
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(m, dict):
                return m
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_meta(job_dir, meta):
    (Path(job_dir) / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def _set(job_id, **kw):
    with JOBS_LOCK:
        JOBS[job_id].update(kw)


def _safe_relpath(name):
    """Sanitize a client-supplied relative path; returns None if unusable."""
    name = (name or "").replace("\\", "/").lstrip("/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    parts = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", p) for p in parts]
    return "/".join(parts)


def _extract_zip(zpath, dest):
    """Safely extract a client zip into dest. Returns (n_files, skipped)."""
    n, total, skipped = 0, 0, []
    with zipfile.ZipFile(zpath) as z:
        for m in z.infolist():
            if m.is_dir():
                continue
            rel = _safe_relpath(m.filename)
            if rel is None:
                skipped.append(m.filename)
                continue
            n += 1
            total += m.file_size
            if n > MAX_ZIP_MEMBERS or total > MAX_ZIP_BYTES:
                raise ValueError("zip exceeds extraction limits")
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return n, skipped


def _job_paths(job_id):
    job_dir = JOBS_DIR / job_id
    return job_dir, job_dir / "archive", job_dir / "schema.json", job_dir / "out"


def _job_root(job_id):
    """Where this job's documents live: an uploaded archive/ copy, or a
    server-side folder referenced by source.json (no copy)."""
    job_dir, archive, _, _ = _job_paths(job_id)
    src = job_dir / "source.json"
    if src.exists():
        try:
            p = json.loads(src.read_text(encoding="utf-8")).get("server_path")
            if p:
                return Path(p)
        except (json.JSONDecodeError, OSError):
            pass
    return archive


def _resolve_server_path(raw):
    """Validate a server-side ingest path. Returns (Path, None) or (None, err)."""
    if not raw or not str(raw).strip():
        return None, "no path given"
    try:
        path = Path(str(raw)).expanduser().resolve()
    except (OSError, RuntimeError):
        return None, "invalid path"
    if not path.is_dir():
        return None, "not a directory on the server"
    if ALLOWED_ROOTS and not any(path == r or r in path.parents
                                 for r in ALLOWED_ROOTS):
        return None, "path is outside PDF2DB_ALLOWED_ROOTS"
    return path, None


def _count_pdfs(path, cap=2000):
    """Quick magic-byte PDF count for path validation; stops at cap files."""
    n_pdf = n_seen = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for fn in sorted(filenames):
            n_seen += 1
            if n_seen > cap:
                return n_pdf, True
            try:
                with open(Path(dirpath) / fn, "rb") as fh:
                    if fh.read(5) == b"%PDF-":
                        n_pdf += 1
            except OSError:
                pass
    return n_pdf, False


# ------------------------------------------------- archive preview (scanning)
def _pick_spread(items, k):
    """Deterministic even spread of k items across a sorted list."""
    items = list(items)
    if len(items) <= k:
        return items
    step = len(items) / float(k)
    return [items[int(i * step)] for i in range(k)]


def _probe_pdf(root, rel):
    """Open one PDF and measure how much text it actually yields.
    Always a plain text-layer read: the preview must stay fast and must show
    what the archive contains BEFORE any OCR is switched on."""
    pages, err, _meta = pdf2db.extract_pdf_text(Path(root) / rel)
    if err:
        return {"rel_path": rel, "error": err, "n_pages": 0, "chars": 0,
                "chars_per_page": 0, "preview": ""}
    text = "\n".join(pages).strip()
    return {"rel_path": rel, "error": None, "n_pages": len(pages),
            "chars": len(text),
            "chars_per_page": round(len(text) / max(1, len(pages))),
            "preview": re.sub(r"\s+", " ", text)[:600]}


def _scan_archive(root):
    """Describe an archive without processing it: what is in there, how many
    records each record_unit would produce, and how much text the PDFs yield.
    Read-only — writes nothing, so the operator can look before committing."""
    root = Path(root)
    pdfs, n_images, n_other, n_bytes, n_filtered = [], 0, 0, 0, 0
    n_seen, truncated = 0, False
    # the SAME default filters the run will apply, so the preview cannot promise
    # documents that discovery then drops (lock files, dotfiles, macOS cruft)
    excludes = pdf2db.split_globs(pdf2db.CONFIG["exclude_globs"])
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            n_seen += 1
            if n_seen > SCAN_FILE_CAP:
                truncated = True
                break
            p = Path(dirpath) / fn
            try:
                with open(p, "rb") as fh:
                    magic = fh.read(5)
                size = p.stat().st_size
            except OSError:
                n_other += 1
                continue
            rel = p.relative_to(root).as_posix()
            if pdf2db.filter_reason(rel, size, [], excludes, 0, 0):
                n_filtered += int(magic == b"%PDF-")
                continue
            if magic == b"%PDF-":
                pdfs.append(rel)
                n_bytes += size
            elif p.suffix.lower() in IMAGE_EXTS:
                n_images += 1
            else:
                n_other += 1
        if truncated:
            break
    pdfs.sort()
    max_depth = max((r.count("/") + 1 for r in pdfs), default=0)
    units = _score_units(pdfs, max_depth)
    patterns = _id_patterns(pdfs)
    probed = [_probe_pdf(root, r) for r in _pick_spread(pdfs, PROBE_DOCS)]
    low = [s for s in probed
           if s["error"] or s["chars_per_page"] < pdf2db.CONFIG["scan_chars_per_page"]]
    return {
        "n_pdfs": len(pdfs), "n_images": n_images, "n_other": n_other,
        "n_bytes": n_bytes, "truncated": truncated,
        "depth": max_depth,
        "n_loose": sum(1 for r in pdfs if "/" not in r),
        "units": units,
        "suggested_unit": _suggest_unit(units, pdfs, patterns),
        "id_patterns": patterns,
        "n_filtered": n_filtered,
        "ocr": {"available": pdf2db.tessdata_dir(pdf2db.CONFIG) is not None,
                "recommended": bool(probed) and len(low) >= max(1, len(probed) // 2)},
        "probe": {"n_probed": len(probed), "n_low_yield": len(low),
                  "samples": probed},
        "_pdfs": pdfs,   # popped by the caller and stored separately
    }


# Identifier shapes worth offering as a regex grouping. Ordered most specific
# first; the preview shows how many records each would actually produce so the
# operator picks on evidence rather than on the label.
ID_PATTERN_CANDIDATES = [
    (r"(?P<id>[A-Z]{2,5}[-_]?\d{3,8})", "letters + digits, e.g. INC-2231"),
    (r"(?P<id>\d{4}[-_]\d{3,6})", "year-number, e.g. 2003-0147"),
    (r"(?P<id>\d{6,10})", "a long digit run, e.g. 00231847"),
]


def _score_units(pdfs, max_depth):
    """How many records each grouping mode would produce over this archive, so
    the choice is made against real counts instead of a guess."""
    units = {}
    specs = ["pdf", "folder", "parent"] + \
            [f"depth:{d}" for d in range(2, min(max_depth, 5))]
    for spec in specs:
        try:
            unit = pdf2db.parse_record_unit(spec)
        except ValueError:
            continue
        ids, unmatched = set(), 0
        for rel in pdfs:
            rid, warn = pdf2db.assign_record_id(rel, unit)
            ids.add(rid)
            unmatched += bool(warn)
        units[spec] = {"n_records": len(ids), "examples": sorted(ids)[:6],
                       "n_unmatched": unmatched}
    return units


def _id_patterns(pdfs):
    """Try each candidate id shape against the paths and report what it would
    do. Only patterns that match most of the archive are worth offering."""
    out = []
    for pattern, label in ID_PATTERN_CANDIDATES:
        spec = f"regex:{pattern}"
        unit = pdf2db.parse_record_unit(spec)
        ids, matched = set(), 0
        for rel in pdfs:
            rid, warn = pdf2db.assign_record_id(rel, unit)
            ids.add(rid)
            matched += not warn
        if matched >= max(1, int(len(pdfs) * 0.6)):
            out.append({"record_unit": spec, "label": label,
                        "n_records": len(ids), "n_matched": matched,
                        "coverage": round(100.0 * matched / max(1, len(pdfs))),
                        "examples": sorted(i for i in ids if i)[:6]})
    return out


def _suggest_unit(units, pdfs, patterns):
    """Recommend a grouping.

    Deliberately NOT "whichever makes the fewest records": over-merging silently
    fuses unrelated incidents into one row, which is far more damaging than
    splitting one incident into two. The order below is by strength of evidence:

      1. an identifier the archive itself puts in nearly every path — the
         archive is telling us what a record is
      2. the folder each PDF sits directly in — the usual "one case per folder"
      3. the top-level folder
      4. one record per file, which is always correct and never insightful
    """
    n = len(pdfs)
    if n == 0:
        return "folder"

    def sane(spec):
        u = units.get(spec) or {}
        return 1 < u.get("n_records", 0) < n and not u.get("n_unmatched")

    best = max((p for p in patterns if p["coverage"] >= 95 and 1 < p["n_records"] <= n),
               key=lambda p: (p["coverage"], -p["n_records"]), default=None)
    if best:
        return best["record_unit"]
    for spec in ("parent", "folder"):
        if sane(spec):
            return spec
    return "folder" if units.get("folder", {}).get("n_records", 0) > 1 else "pdf"


def _record_text(root, rel_paths, budget):
    """Concatenate a record's PDFs the same way the extract stage does, so the
    text a schema is drafted from matches the text it will be applied to."""
    chunks = []
    for rel in rel_paths:
        pages, err, _meta = pdf2db.extract_pdf_text(Path(root) / rel)
        if err:
            continue
        chunks.append(f"===== FILE: {rel} =====\n" + "\n".join(pages))
    text, _ = pdf2db.truncate_text("\n\n".join(chunks), budget, 0.7)
    return text.strip()


# --------------------------------------------------------- schema suggestion
SUGGEST_SYSTEM = ("You design database schemas for archives of documents. "
                  "Output exactly one JSON object and nothing else — no "
                  "markdown fences, no commentary.")

GENERIC_SCHEMA = {
    "table": "documents", "record_unit": "folder",
    "task_description": "Each record is one document from a mixed archive.",
    "fields": [
        {"name": "document_title", "type": "string", "required": True,
         "description": "Title or subject line of the document."},
        {"name": "document_date", "type": "date", "required": False,
         "description": "Date the document was issued (ISO)."},
        {"name": "reference_number", "type": "string", "required": False,
         "description": "Any report, job or case number printed on it."},
        {"name": "author", "type": "string", "required": False,
         "description": "Person or group that produced the document."},
        {"name": "summary", "type": "string", "required": False,
         "description": "One or two sentences describing what it covers."},
    ],
}


def _unit_phrase(unit):
    """Plain-English gloss of a record_unit, for the schema-drafting prompt and
    for the console. The model reads this to understand what a record IS."""
    unit = (unit or "folder").strip()
    if unit == "pdf":
        return "one PDF file"
    if unit == "folder":
        return "one top-level subfolder of the archive (its PDFs merged)"
    if unit == "parent":
        return "one folder of PDFs (the folder a document sits directly in)"
    if unit.startswith("depth:"):
        return (f"one folder {unit.split(':', 1)[1]} level(s) below the archive "
                f"root (all PDFs beneath it merged)")
    if unit.startswith("regex:"):
        return ("one identifier found in the file path (all documents sharing "
                "that identifier merged into a single record)")
    return "one record"


def _suggest_user_prompt(samples, unit, n_records):
    reserved = ", ".join(sorted(pdf2db.RESERVED_COLUMNS))
    docs = "\n\n".join(
        f"===== SAMPLE {i + 1} (record: {s['record_id']}) =====\n{s['text']}"
        for i, s in enumerate(samples))
    unit_word = _unit_phrase(unit)
    return f"""Below are {len(samples)} sample documents drawn from an archive \
of {n_records} records. One record = {unit_word}.

Design ONE database table that captures what an engineer would want to query \
across the WHOLE archive, not just these samples.

Return a JSON object shaped exactly like this:
{{
  "table": "<snake_case table name>",
  "task_description": "<1-2 sentences telling a later extraction model what \
these documents are>",
  "fields": [
    {{"name": "<snake_case>",
      "type": "string|integer|number|boolean|date|enum",
      "required": true,
      "options": ["only", "for", "enum"],
      "description": "<what to extract and where it appears in the document>",
      "example": "<the value found in SAMPLE 1, copied verbatim, or null>"}}
  ]
}}

Rules:
- Between 5 and 12 fields. Only fields most documents in the archive will have.
- "name" must match ^[a-z_][a-z0-9_]*$ and must NOT be one of these reserved \
pipeline columns: {reserved}
- Use "enum" only for a small closed set you can actually enumerate from the \
documents; list every value you saw and add "other".
- Use "date" for dates and "integer"/"number" for counts and measurements — \
put the unit in the description (e.g. "wall thickness in mm").
- Do not add confidence, evidence or source fields; the pipeline adds those.
- Never invent a field the documents do not contain, and never invent an \
"example" — copy it from SAMPLE 1 or use null.

{docs}"""


def _draft_schema(settings, samples, unit, n_records):
    """Ask the configured LLM for a schema over these samples. Returns
    (schema_or_None, examples, source, error)."""
    if settings["llm_base_url"].strip() == "mock":
        schema = json.loads(json.dumps(GENERIC_SCHEMA))
        schema["record_unit"] = unit
        return schema, {}, "generic", None
    cfg = dict(pdf2db.CONFIG)
    cfg.update({"llm_base_url": settings["llm_base_url"],
                "llm_model": settings["llm_model"],
                "llm_api_key": settings["llm_api_key"],
                "llm_timeout_s": 120, "llm_transport_retries": 2})
    messages = [{"role": "system", "content": SUGGEST_SYSTEM},
                {"role": "user",
                 "content": _suggest_user_prompt(samples, unit, n_records)}]
    last_err = "no attempt made"
    for _attempt in range(2):
        try:
            raw = pdf2db.call_openai_compat(cfg, messages)
        except pdf2db.LLMTransportError as e:
            return None, {}, "llm", f"the model endpoint could not be reached — {e}"
        obj, err = pdf2db.parse_json_response(raw)
        if obj is None:
            last_err = err
            messages += [{"role": "assistant", "content": raw[:2000]},
                         {"role": "user", "content": f"That failed: {err}. "
                          "Reply with the JSON object only."}]
            continue
        examples = {}
        if isinstance(obj.get("fields"), list):
            obj["fields"] = [f for f in obj["fields"] if isinstance(f, dict)][:16]
        for f in obj.get("fields") or []:
            ex = f.pop("example", None)   # UI-only hint; never part of the schema
            if ex not in (None, "") and isinstance(f.get("name"), str):
                examples[f["name"]] = str(ex)[:200]
            if f.get("type") != "enum":
                f.pop("options", None)    # models like to send options: null
        obj.setdefault("record_unit", unit)
        errs = pdf2db.validate_schema(obj) if isinstance(obj, dict) else \
            ["reply is not a JSON object"]
        if not errs:
            return obj, examples, "llm", None
        last_err = "; ".join(errs)
        messages += [{"role": "assistant", "content": raw[:2000]},
                     {"role": "user",
                      "content": "That schema is invalid:\n- " +
                                 "\n- ".join(errs) + "\nReturn a corrected "
                                 "JSON object only."}]
    return None, {}, "llm", last_err


def _ingest_cfg_from(source, settings=None):
    """Pull the ingest knobs out of a form/JSON body, coercing and validating
    every one. Returns (cfg_overrides, errors) — an invalid value is reported,
    never silently swapped for a default, because grouping quietly changing
    under the operator is exactly the failure this feature exists to remove."""
    over, errs = {}, []
    if settings:   # host-level OCR defaults; the per-corpus form may override
        for k in ("ocr_mode", "ocr_language", "ocr_dpi"):
            if k in settings:
                over[k] = settings[k]
    get = source.get
    for key, kind in INGEST_KEYS.items():
        raw = get(key)
        if raw is None or (isinstance(raw, str) and raw.strip() == ""
                           and key not in ("include_globs", "exclude_globs")):
            continue
        if kind is bool:
            over[key] = raw if isinstance(raw, bool) else \
                str(raw).strip().lower() in ("1", "true", "yes", "on")
        elif kind is int:
            try:
                over[key] = int(raw)
            except (TypeError, ValueError):
                errs.append(f"{key} must be a whole number, got {raw!r}")
        else:
            over[key] = str(raw).strip()
    probe = dict(pdf2db.CONFIG)
    probe.update(over)
    errs += pdf2db.validate_cfg(probe)
    return over, errs


def _run_job(job_id, cfg):
    def cb(stage, done=0, total=0):
        pct = STAGE_PCT.get(stage, 0)
        if stage == "llm" and total:
            pct = 15 + round(80 * done / total)
        _set(job_id, stage=stage, done=done, total=total, pct=pct)

    cfg["_progress_cb"] = cb
    try:
        summary = pdf2db.run_pipeline(cfg)
        _set(job_id, status="done", pct=100, stage="finished",
             summary={k: summary[k] for k in
                      ("n_records", "n_pdfs", "status_counts", "n_review_queue",
                       "n_issues")} if summary else None)
    except SystemExit:
        _set(job_id, status="failed", error="pipeline aborted — see server log")
    except Exception as e:  # surface anything to the UI; never kill the server
        _set(job_id, status="failed", error=f"{type(e).__name__}: {e}")


def _with_live_counts(job_id, job):
    """Overlay the CURRENT record statuses onto the run summary.

    `summary` is frozen when the pipeline finishes, but reviewing a record
    changes its status afterwards — so a header painted from the frozen numbers
    would keep claiming records need review long after a human cleared them.
    The `records` table is the thing the review endpoint actually writes, so it
    is the only honest source for these counts. Falls back to the frozen summary
    whenever the database is absent or mid-write.
    """
    summary = job.get("summary")
    if not summary or job.get("status") == "running":
        return job
    _, _, _, out_dir = _job_paths(job_id)
    db_path = out_dir / f"{job.get('table', 'records')}.db"
    if not db_path.exists():
        return job
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            counts = dict(con.execute(
                'SELECT "status", COUNT(*) FROM "records" GROUP BY "status"'))
        finally:
            con.close()
    except sqlite3.Error:
        return job          # mid-write: last known good numbers beat an error
    live = dict(summary)
    live["status_counts"] = {k: v for k, v in sorted(counts.items()) if k}
    live["n_review_queue"] = counts.get("needs_review", 0)
    live["n_records"] = sum(counts.values())
    job["summary"] = live
    return job


def _rescan_jobs():
    """Register jobs from previous server runs (read-only view)."""
    if not JOBS_DIR.is_dir():
        return
    for d in sorted(JOBS_DIR.iterdir()):
        if not d.is_dir() or d.name in JOBS:
            continue
        table = "?"
        schema_file = d / "schema.json"
        if schema_file.exists():
            try:
                table = json.loads(schema_file.read_text()).get("table", "?")
            except (json.JSONDecodeError, OSError):
                pass
        meta = _read_meta(d)
        state = _new_job_state(d.name, table, meta.get("title", ""),
                               meta.get("source", ""))
        summary_file = d / "out" / "run_summary.json"
        if summary_file.exists():
            try:
                s = json.loads(summary_file.read_text())
                conf = s.get("config", {})
                state.update(status="done", pct=100, stage="finished",
                             endpoint=conf.get("llm_base_url", ""),
                             model=conf.get("llm_model", ""),
                             is_mock=conf.get("llm_base_url") == "mock",
                             summary={k: s.get(k) for k in
                                      ("n_records", "n_pdfs", "status_counts",
                                       "n_review_queue", "n_issues")})
            except (json.JSONDecodeError, OSError):
                state.update(status="failed", error="unreadable run_summary.json")
        else:
            state.update(status="failed", error="interrupted before finishing "
                                                "(server restarted?)")
        with JOBS_LOCK:
            JOBS[d.name] = state


def _write_csv_rows(path, rows):
    if rows:
        pdf2db.write_csv(path, rows, list(rows[0].keys()))


# ---------------------------------------------------------------- API routes
@app.get("/healthz")
def healthz():
    with JOBS_LOCK:
        running = sum(1 for j in JOBS.values() if j["status"] == "running")
    return jsonify({"ok": True, "app": APP_NAME, "version": APP_VERSION,
                    "running": running})


@app.get("/api/settings")
def api_get_settings():
    return jsonify(_public_settings(_load_settings()))


@app.put("/api/settings")
def api_put_settings():
    body = request.get_json(force=True, silent=True) or {}
    s = _load_settings()
    if "llm_base_url" in body:
        s["llm_base_url"] = str(body["llm_base_url"]).strip() or "mock"
    if "llm_model" in body:
        s["llm_model"] = str(body["llm_model"]).strip() or "internal-model"
    if "llm_api_key" in body:  # absent = keep; "" = clear; value = replace
        s["llm_api_key"] = str(body["llm_api_key"])
    if "ocr_mode" in body:
        mode = str(body["ocr_mode"]).strip()
        if mode not in pdf2db.OCR_MODES:
            return jsonify({"error": f"ocr_mode must be one of "
                                     f"{list(pdf2db.OCR_MODES)}"}), 400
        if mode != "off" and pdf2db.tessdata_dir(pdf2db.CONFIG) is None:
            return jsonify({"error": "Tesseract is not installed on this server, so OCR "
                                     "cannot be switched on. Ask for the tesseract OS "
                                     "package (plus its language data) to be provisioned, "
                                     "then set TESSDATA_PREFIX."}), 400
        s["ocr_mode"] = mode
    if "ocr_language" in body:
        s["ocr_language"] = str(body["ocr_language"]).strip() or "eng"
    if "ocr_dpi" in body:
        try:
            dpi = int(body["ocr_dpi"])
        except (TypeError, ValueError):
            return jsonify({"error": "ocr_dpi must be a whole number"}), 400
        if not 72 <= dpi <= 1200:
            return jsonify({"error": "ocr_dpi must be between 72 and 1200"}), 400
        s["ocr_dpi"] = dpi
    _save_settings(s)
    return jsonify(_public_settings(s))


@app.post("/api/settings/test")
def api_test_settings():
    """Probe an endpoint (GET /models) without saving anything."""
    body = request.get_json(force=True, silent=True) or {}
    s = _load_settings()
    base = (body.get("llm_base_url") or s["llm_base_url"]).strip()
    key = body.get("llm_api_key")
    if key in (None, ""):
        key = s["llm_api_key"]
    if base == "mock":
        return jsonify({"ok": True, "detail": "mock backend — offline test mode "
                                              "(fake values, no network call)"})
    url = base.rstrip("/") + "/models"
    headers = {"User-Agent": "pdf2db/1.0"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("id") for m in data.get("data", [])][:50]
        return jsonify({"ok": True,
                        "detail": f"endpoint reachable — {len(models)} models "
                                  f"available", "models": models})
    except urllib.error.HTTPError as e:
        det = ""
        try:
            det = e.read()[:200].decode("utf-8", "replace")
        except OSError:
            pass
        return jsonify({"ok": False, "detail": f"HTTP {e.code}: {det}"})
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return jsonify({"ok": False, "detail": f"{type(e).__name__}: {e}"})


@app.post("/api/server-path/check")
def api_check_server_path():
    body = request.get_json(force=True, silent=True) or {}
    path, err = _resolve_server_path(body.get("path"))
    if err:
        return jsonify({"ok": False, "detail": err})
    n_pdf, partial = _count_pdfs(path)
    return jsonify({"ok": True, "n_pdfs": n_pdf, "partial": partial,
                    "detail": f"{n_pdf}{'+' if partial else ''} PDFs found"
                              + (" (large folder — stopped counting)" if partial else "")})


@app.get("/api/example-schema")
def api_example_schema():
    if not EXAMPLE_SCHEMA.exists():
        abort(404)
    return Response(EXAMPLE_SCHEMA.read_text(encoding="utf-8"),
                    mimetype="application/json")


@app.post("/api/schema/validate")
def api_validate_schema():
    try:
        schema = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "errors": ["schema is not valid JSON"]})
    if not isinstance(schema, dict):
        return jsonify({"ok": False, "errors": ["schema must be a JSON object"]})
    errors = pdf2db.validate_schema(schema)
    return jsonify({"ok": not errors, "errors": errors})


# ---- schema library ----
@app.get("/api/schemas")
def api_list_schemas():
    out = []
    if SCHEMAS_DIR.is_dir():
        for p in sorted(SCHEMAS_DIR.glob("*.json")):
            try:
                s = json.loads(p.read_text(encoding="utf-8"))
                out.append({"name": p.stem, "table": s.get("table", "?"),
                            "n_fields": len(s.get("fields", []))})
            except (json.JSONDecodeError, OSError):
                out.append({"name": p.stem, "table": "?", "n_fields": 0})
    return jsonify(out)


@app.post("/api/schemas")
def api_save_schema():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    schema = body.get("schema")
    if not SCHEMA_NAME_RE.match(name):
        return jsonify({"error": "name must be 1-60 chars: letters, digits, "
                                 "space, dot, dash, underscore"}), 400
    if not isinstance(schema, dict):
        return jsonify({"error": "schema must be a JSON object"}), 400
    errors = pdf2db.validate_schema(schema)
    if errors:
        return jsonify({"error": "invalid schema", "details": errors}), 400
    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
    (SCHEMAS_DIR / f"{name}.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=1), encoding="utf-8")
    return jsonify({"ok": True, "name": name})


@app.get("/api/schemas/<name>")
def api_get_schema(name):
    if not SCHEMA_NAME_RE.match(name):
        abort(404)
    p = SCHEMAS_DIR / f"{name}.json"
    if not p.exists():
        abort(404)
    return Response(p.read_text(encoding="utf-8"), mimetype="application/json")


@app.delete("/api/schemas/<name>")
def api_delete_schema(name):
    if not SCHEMA_NAME_RE.match(name):
        abort(404)
    p = SCHEMAS_DIR / f"{name}.json"
    if not p.exists():
        abort(404)
    p.unlink()
    return jsonify({"ok": True})


# ---- staging: upload and look at an archive before committing to a run ----
ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


def _new_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _staging_dir(sid):
    if not ID_RE.match(sid or ""):
        abort(404)
    d = STAGING_DIR / sid
    if not d.is_dir():
        abort(404)
    return d


def _staging_root(sdir):
    """Documents of a staging area: the uploaded copy, or a server folder."""
    src = sdir / "source.json"
    if src.exists():
        try:
            p = json.loads(src.read_text(encoding="utf-8")).get("server_path")
            if p:
                return Path(p)
        except (json.JSONDecodeError, OSError):
            pass
    return sdir / "archive"


def _sweep_staging():
    """Delete staging areas nobody turned into a corpus."""
    if not STAGING_DIR.is_dir():
        return
    cutoff = time.time() - STAGING_TTL_S
    for d in sorted(STAGING_DIR.iterdir()):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _save_uploaded_files(files, archive):
    """Write an uploaded folder/zip into archive/. Returns (n_saved, skipped)."""
    archive.mkdir(parents=True, exist_ok=True)
    n_saved, skipped, zips = 0, [], []
    for f in files:
        rel = _safe_relpath(f.filename)
        if rel is None:
            skipped.append(f.filename)
            continue
        dest = archive / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        f.save(dest)
        if rel.lower().endswith(".zip"):
            zips.append(dest)
        else:
            n_saved += 1
    for z in zips:  # a .zip upload stands in for the whole folder
        try:
            n, z_skipped = _extract_zip(z, archive)
            n_saved += n
            skipped.extend(z_skipped)
        except (zipfile.BadZipFile, ValueError, OSError) as e:
            skipped.append(f"{z.name} ({e})")
        finally:
            z.unlink(missing_ok=True)
    return n_saved, skipped


@app.post("/api/staging")
def api_create_staging():
    """Accept an archive (uploaded folder/.zip or a server path) and describe
    it. Nothing is processed and no corpus exists yet."""
    _sweep_staging()
    files = request.files.getlist("files")
    server_path_raw = (request.form.get("server_path") or "").strip()
    source_name = (request.form.get("source_name") or "").strip()[:120]
    if not files and not server_path_raw:
        return jsonify({"error": "no folder uploaded and no server path given"}), 400

    sid = _new_id()
    sdir = STAGING_DIR / sid
    sdir.mkdir(parents=True)
    skipped = []
    try:
        if server_path_raw:
            spath, err = _resolve_server_path(server_path_raw)
            if err:
                shutil.rmtree(sdir, ignore_errors=True)
                return jsonify({"error": f"server path rejected: {err}"}), 400
            (sdir / "source.json").write_text(
                json.dumps({"server_path": str(spath)}), encoding="utf-8")
            root, source_name = spath, source_name or spath.name
        else:
            n_saved, skipped = _save_uploaded_files(files, sdir / "archive")
            if n_saved == 0:
                shutil.rmtree(sdir, ignore_errors=True)
                return jsonify({"error": "no usable files in that upload",
                                "details": skipped[:10]}), 400
            root = sdir / "archive"
        scan = _scan_archive(root)
        pdf_list = scan.pop("_pdfs", [])
    except OSError as e:
        shutil.rmtree(sdir, ignore_errors=True)
        return jsonify({"error": f"could not read the archive: {e}"}), 400
    if scan["n_pdfs"] == 0:
        shutil.rmtree(sdir, ignore_errors=True)
        return jsonify({"error": "no PDFs found in that archive — files are "
                                 "detected by content, not by name, so a "
                                 "renamed non-PDF will not count"}), 400
    meta = {"source": source_name, "skipped": skipped[:50],
            "created": _now(), "server_path": server_path_raw or ""}
    _write_meta(sdir, meta)
    (sdir / "scan.json").write_text(json.dumps(scan, ensure_ascii=False,
                                               indent=1), encoding="utf-8")
    # the discovered paths, kept so a custom grouping can be previewed without
    # walking the archive again (the walk is the slow part, not the grouping)
    (sdir / "pdfs.json").write_text(json.dumps(pdf_list), encoding="utf-8")
    return jsonify({"staging_id": sid, "scan": scan, "source": source_name,
                    "skipped": skipped[:20]})


@app.post("/api/staging/<sid>/grouping")
def api_preview_grouping(sid):
    """Score one record_unit against the staged archive: how many records it
    makes, what they are called, and how many PDFs it fails to place. Lets the
    operator type a regex and see the consequence before committing."""
    sdir = _staging_dir(sid)
    p = sdir / "pdfs.json"
    if not p.exists():
        abort(404)
    body = request.get_json(force=True, silent=True) or {}
    spec = (body.get("record_unit") or "").strip()
    try:
        unit = pdf2db.parse_record_unit(spec)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    pdfs = json.loads(p.read_text())
    ids, unmatched = set(), 0
    for rel in pdfs:
        rid, warn = pdf2db.assign_record_id(rel, unit)
        ids.add(rid)
        unmatched += bool(warn)
    return jsonify({"ok": True, "record_unit": spec, "n_records": len(ids),
                    "n_pdfs": len(pdfs), "n_unmatched": unmatched,
                    "examples": sorted(ids)[:8],
                    "phrase": _unit_phrase(spec)})


@app.get("/api/staging/<sid>")
def api_get_staging(sid):
    sdir = _staging_dir(sid)
    p = sdir / "scan.json"
    if not p.exists():
        abort(404)
    return jsonify({"staging_id": sid, "scan": json.loads(p.read_text()),
                    "source": _read_meta(sdir).get("source", "")})


@app.delete("/api/staging/<sid>")
def api_delete_staging(sid):
    shutil.rmtree(_staging_dir(sid), ignore_errors=True)
    return jsonify({"ok": True})


@app.post("/api/staging/<sid>/suggest-schema")
def api_suggest_schema(sid):
    """Draft a schema from the documents themselves: sample a few records,
    send their text to the configured model, validate what comes back."""
    sdir = _staging_dir(sid)
    body = request.get_json(force=True, silent=True) or {}
    unit = (body.get("record_unit") or "folder").strip() or "folder"
    try:
        pdf2db.parse_record_unit(unit)
    except ValueError as e:
        return jsonify({"ok": False, "error": f"invalid grouping: {e}"}), 400
    scan_path = sdir / "scan.json"
    if not scan_path.exists():
        abort(404)
    scan = json.loads(scan_path.read_text())
    root = _staging_root(sdir)
    if not root.is_dir():
        return jsonify({"ok": False, "error": "the staged archive is gone from "
                                              "disk — upload it again"}), 409

    # group PDFs by the record they would belong to, then sample across them
    by_record = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            try:
                with open(p, "rb") as fh:
                    if fh.read(5) != b"%PDF-":
                        continue
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            by_record.setdefault(pdf2db.assign_record_id(rel, unit)[0],
                                 []).append(rel)
    samples = []
    for rid in _pick_spread(sorted(by_record), SUGGEST_SAMPLES):
        text = _record_text(root, sorted(by_record[rid]), SUGGEST_CHARS)
        if text:
            samples.append({"record_id": rid, "text": text})
    if not samples:
        return jsonify({"ok": False, "no_text": True,
                        "error": "none of the sampled documents contain "
                                 "extractable text — they are probably scans. "
                                 "Build the schema by hand or start from a "
                                 "saved one."}), 200

    settings = _load_settings()
    schema, examples, source, err = _draft_schema(
        settings, samples, unit,
        (scan.get("units", {}).get(unit) or {}).get("n_records", len(by_record)))
    if schema is None:
        return jsonify({"ok": False, "error": err, "source": source}), 200
    return jsonify({"ok": True, "schema": schema, "examples": examples,
                    "source": source, "model": settings["llm_model"],
                    "sampled": [s["record_id"] for s in samples]})


# ---- databases (jobs) ----
def _llm_cfg_from(form_or_json, settings):
    """LLM config for a run: stored settings, overridable per request."""
    get = form_or_json.get
    base = (get("llm_base_url") or settings["llm_base_url"]).strip()
    model = (get("llm_model") or settings["llm_model"]).strip()
    key = get("llm_api_key")
    if key in (None, ""):
        key = settings["llm_api_key"]
    return base, model, key


@app.post("/api/jobs")
def api_create_job():
    try:
        schema = json.loads(request.form.get("schema") or "")
    except json.JSONDecodeError:
        return jsonify({"error": "schema is not valid JSON"}), 400
    errors = pdf2db.validate_schema(schema) if isinstance(schema, dict) else \
        ["schema must be a JSON object"]
    if errors:
        return jsonify({"error": "invalid schema", "details": errors}), 400

    staging_id = (request.form.get("staging_id") or "").strip()
    files = request.files.getlist("files")
    server_path_raw = (request.form.get("server_path") or "").strip()
    if not staging_id and not files and not server_path_raw:
        return jsonify({"error": "no archive given — stage one first, upload "
                                 "files, or pass a server path"}), 400

    job_id = _new_id()
    job_dir, archive, schema_path, out_dir = _job_paths(job_id)
    n_saved, skipped, meta = 0, [], {}

    if staging_id:
        # promote a scanned staging area: move it, never copy it twice
        sdir = _staging_dir(staging_id)
        meta = _read_meta(sdir)
        try:
            shutil.move(str(sdir), str(job_dir))
        except OSError as e:
            return jsonify({"error": f"could not promote the staged archive: {e}"}), 500
        out_dir.mkdir(exist_ok=True)
        scan_path = job_dir / "scan.json"
        if scan_path.exists():
            try:
                n_saved = json.loads(scan_path.read_text()).get("n_pdfs", 0)
            except (json.JSONDecodeError, OSError):
                pass
        skipped = meta.get("skipped", [])
        root = _job_root(job_id)
    else:
        job_dir.mkdir(parents=True)
        out_dir.mkdir()
        if server_path_raw:
            # server-side ingestion: process in place, no upload, no extra copy
            spath, err = _resolve_server_path(server_path_raw)
            if err:
                shutil.rmtree(job_dir, ignore_errors=True)
                return jsonify({"error": f"server path rejected: {err}"}), 400
            (job_dir / "source.json").write_text(
                json.dumps({"server_path": str(spath)}), encoding="utf-8")
            root = spath
            meta["source"] = spath.name
            n_saved, _partial = _count_pdfs(spath)
            if n_saved == 0:
                shutil.rmtree(job_dir, ignore_errors=True)
                return jsonify({"error": "no PDFs found under that server path"}), 400
        else:
            n_saved, skipped = _save_uploaded_files(files, archive)
            if n_saved == 0:
                shutil.rmtree(job_dir, ignore_errors=True)
                return jsonify({"error": "no usable files in upload",
                                "details": skipped[:10]}), 400
            root = archive

    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    title = (request.form.get("title") or "").strip()[:120]
    meta.update({"title": title or meta.get("source") or schema.get("table", ""),
                 "source": meta.get("source", ""), "created": _now(),
                 "skipped": skipped[:50]})
    _write_meta(job_dir, meta)

    settings = _load_settings()
    base_url, model, key = _llm_cfg_from(request.form, settings)
    ingest, ing_errs = _ingest_cfg_from(request.form, settings)
    if ing_errs:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "invalid ingest options",
                        "details": ing_errs}), 400
    meta["ingest"] = ingest          # so Retry reproduces the same reading of the archive
    _write_meta(job_dir, meta)
    cfg = dict(pdf2db.CONFIG)
    cfg.update(ingest)
    cfg.update({
        "root": str(root), "schema": str(schema_path), "out": str(out_dir),
        "llm_base_url": base_url, "llm_model": model, "llm_api_key": key,
        "stages": "discover,extract,images,llm,load", "resume": False,
    })

    with JOBS_LOCK:
        JOBS[job_id] = _new_job_state(job_id, schema.get("table", "?"),
                                      meta["title"], meta.get("source", ""))
        JOBS[job_id].update(n_files=n_saved, endpoint=base_url, model=model,
                            is_mock=(base_url == "mock"))
    threading.Thread(target=_run_job, args=(job_id, cfg), daemon=True).start()
    return jsonify({"job_id": job_id, "n_files": n_saved, "skipped": skipped})


@app.post("/api/jobs/<job_id>/retry")
def api_retry_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            abort(404)
        if job["status"] == "running":
            return jsonify({"error": "database is already processing"}), 409
    job_dir, _, schema_path, out_dir = _job_paths(job_id)
    root = _job_root(job_id)
    if not schema_path.exists() or not root.is_dir():
        return jsonify({"error": "database artifacts are missing on disk"}), 409
    body = request.get_json(force=True, silent=True) or {}
    base_url, model, key = _llm_cfg_from(body, _load_settings())
    if (out_dir / "records_text.csv").exists():
        # backfill the images stage for databases created before it existed
        stages = "llm,load" if (out_dir / "images.csv").exists() \
            else "images,llm,load"
    else:
        stages = "discover,extract,images,llm,load"
    cfg = dict(pdf2db.CONFIG)
    # reuse the ingest options this corpus was built with, so a retry cannot
    # silently regroup or re-OCR the archive differently from the first pass
    cfg.update((_read_meta(job_dir) or {}).get("ingest") or {})
    cfg.update({"root": str(root), "schema": str(schema_path),
                "out": str(out_dir), "llm_base_url": base_url,
                "llm_model": model, "llm_api_key": key,
                "stages": stages, "resume": True})
    _set(job_id, status="running", stage="queued", done=0, total=0, pct=0,
         error=None, endpoint=base_url, model=model,
         is_mock=(base_url == "mock"))
    threading.Thread(target=_run_job, args=(job_id, cfg), daemon=True).start()
    return jsonify({"ok": True, "stages": stages})


@app.get("/api/jobs")
def api_list_jobs():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j["id"], reverse=True)
        jobs = [dict(j) for j in jobs]
    # same live counts as the corpus header, so the ledger's "needs review"
    # totals cannot disagree with the corpus you open from it
    return jsonify([_with_live_counts(j["id"], j) for j in jobs])


@app.get("/api/jobs/<job_id>/status")
def api_job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            abort(404)
        job = dict(job)
    return jsonify(_with_live_counts(job_id, job))


@app.delete("/api/jobs/<job_id>")
def api_delete_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            abort(404)
        if job["status"] == "running":
            return jsonify({"error": "cannot delete while processing"}), 409
        JOBS.pop(job_id)
    target = (JOBS_DIR / job_id).resolve()
    if target.is_dir() and target.parent == JOBS_DIR.resolve():
        shutil.rmtree(target)
    return jsonify({"ok": True})


@app.get("/api/jobs/<job_id>/schema")
def api_job_schema(job_id):
    if job_id not in JOBS:
        abort(404)
    p = JOBS_DIR / job_id / "schema.json"
    if not p.exists():
        abort(404)
    return Response(p.read_text(encoding="utf-8"), mimetype="application/json")


@app.patch("/api/jobs/<job_id>")
def api_rename_job(job_id):
    """Rename a corpus. The table name is baked into the SQLite file, so only
    the human-facing title and note change here."""
    with JOBS_LOCK:
        if job_id not in JOBS:
            abort(404)
    body = request.get_json(force=True, silent=True) or {}
    job_dir = JOBS_DIR / job_id
    if not job_dir.is_dir():
        return jsonify({"error": "this corpus has no folder on disk"}), 409
    meta = _read_meta(job_dir)
    if "title" in body:
        title = str(body["title"]).strip()[:120]
        if not title:
            return jsonify({"error": "title cannot be empty"}), 400
        meta["title"] = title
    if "description" in body:
        meta["description"] = str(body["description"]).strip()[:500]
    _write_meta(job_dir, meta)
    _set(job_id, title=meta.get("title", ""))
    return jsonify({"ok": True, "title": meta.get("title", ""),
                    "description": meta.get("description", "")})


@app.get("/api/jobs/<job_id>/meta")
def api_job_meta(job_id):
    """Title, note, source archive and the scan taken before the run."""
    with JOBS_LOCK:
        if job_id not in JOBS:
            abort(404)
    job_dir = JOBS_DIR / job_id
    scan = None
    scan_path = job_dir / "scan.json"
    if scan_path.exists():
        try:
            scan = json.loads(scan_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            scan = None
    return jsonify({"meta": _read_meta(job_dir), "scan": scan})


@app.get("/api/jobs/<job_id>/artifacts")
def api_job_artifacts(job_id):
    """Every file this run produced, with size and what it is for."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        abort(404)
    out_dir = JOBS_DIR / job_id / "out"
    db_name = f"{job['table']}.db"
    out = []
    for name in list(DOWNLOADABLE) + [db_name]:
        p = out_dir / name
        if not p.exists():
            continue
        help_text = ARTIFACT_HELP.get(
            name, "SQLite database — every table above, queryable with any "
                  "SQL client or pandas.read_sql.")
        out.append({"name": name, "bytes": p.stat().st_size, "help": help_text,
                    "kind": p.suffix.lstrip(".")})
    out.sort(key=lambda a: (a["kind"] != "db", a["name"]))
    return jsonify(out)


def _stats_from_db(db_path, schema):
    """Distributions as SQL aggregates — the overview stays fast on a large
    corpus instead of re-parsing records.csv on every request."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        n_records = con.execute('SELECT COUNT(*) FROM "records"').fetchone()[0]
        counts = dict(con.execute(
            'SELECT "status", COUNT(*) FROM "records" GROUP BY "status"'))
        ordered = [[s, counts[s]] for s in STATUS_ORDER if counts.get(s)]
        for s in sorted(counts):
            if s and s not in STATUS_ORDER:
                ordered.append([s, counts[s]])
        conf = [0] * 10
        for bucket, n in con.execute(
                'SELECT MIN(9, CAST("_confidence" * 10 AS INTEGER)), COUNT(*) '
                'FROM "records" WHERE "_confidence" IS NOT NULL GROUP BY 1'):
            if bucket is not None and 0 <= bucket <= 9:
                conf[bucket] += n
        enums = {}
        cols = {r[1] for r in con.execute('PRAGMA table_info("records")')}
        for f in schema.get("fields", []):
            if f.get("type") != "enum" or f["name"] not in cols:
                continue
            pairs = [[v, n] for v, n in con.execute(
                f'SELECT "{f["name"]}", COUNT(*) FROM "records" '
                f'WHERE "{f["name"]}" IS NOT NULL AND "{f["name"]}" != \'\' '
                f'GROUP BY 1 ORDER BY 2 DESC')]
            top, rest = pairs[:8], sum(n for _, n in pairs[8:])
            if rest:
                top.append(["(other)", rest])
            enums[f["name"]] = top
        return {"n_records": n_records, "statuses": ordered,
                "confidence": conf, "enums": enums}
    finally:
        con.close()


@app.get("/api/jobs/<job_id>/stats")
def api_job_stats(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        abort(404)
    _, _, schema_path, out_dir = _job_paths(job_id)
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        schema = {"fields": []}
    db_path = out_dir / f"{job['table']}.db"
    if db_path.exists():
        try:
            return jsonify(_stats_from_db(db_path, schema))
        except sqlite3.Error:
            pass  # mid-write — fall back to the CSV
    rec_path = out_dir / "records.csv"
    if not rec_path.exists():
        return jsonify({"n_records": 0, "statuses": [], "confidence": [0] * 10,
                        "enums": {}})
    rows = pdf2db.read_csv(rec_path)
    statuses = Counter(r.get("status", "") for r in rows)
    ordered = [[s, statuses[s]] for s in STATUS_ORDER if statuses.get(s)]
    for s in sorted(statuses):
        if s and s not in STATUS_ORDER:
            ordered.append([s, statuses[s]])
    conf = [0] * 10
    for r in rows:
        c = r.get("_confidence") or ""
        try:
            conf[min(9, int(float(c) * 10))] += 1
        except ValueError:
            pass
    enums = {}
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        schema = {"fields": []}
    for f in schema.get("fields", []):
        if f.get("type") != "enum":
            continue
        counts = Counter(r.get(f["name"]) for r in rows if r.get(f["name"]))
        top = counts.most_common(8)
        rest = sum(counts.values()) - sum(c for _, c in top)
        pairs = [[v, c] for v, c in top]
        if rest > 0:
            pairs.append(["(other)", rest])
        enums[f["name"]] = pairs
    return jsonify({"n_records": len(rows), "statuses": ordered,
                    "confidence": conf, "enums": enums})


@app.get("/api/jobs/<job_id>/record/<path:record_id>")
def api_job_record(job_id, record_id):
    """One record. Served from SQLite by primary key so opening a row stays
    instant on a corpus with tens of thousands of records; the CSV scan is
    only the fallback for a run whose database is missing or mid-write."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        abort(404)
    out_dir = JOBS_DIR / job_id / "out"
    db_path = out_dir / f"{job['table']}.db"
    if db_path.exists():
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cols = [r[1] for r in con.execute('PRAGMA table_info("records")')]
                row = con.execute('SELECT * FROM "records" WHERE "record_id" = ?',
                                  (record_id,)).fetchone()
            finally:
                con.close()
            if row:
                return jsonify({"columns": cols,
                                "row": ["" if v is None else v for v in row]})
        except sqlite3.Error:
            pass  # fall through to the CSV
    rec_path = out_dir / "records.csv"
    if not rec_path.exists():
        abort(404)
    for r in pdf2db.read_csv(rec_path):
        if r.get("record_id") == record_id:
            return jsonify({"columns": list(r.keys()), "row": list(r.values())})
    abort(404)


@app.post("/api/jobs/<job_id>/review")
def api_review(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        abort(404)
    body = request.get_json(force=True, silent=True) or {}
    record_id = body.get("record_id") or ""
    verdict = body.get("verdict") or ""
    reviewer = (body.get("reviewer") or "").strip()[:80]
    corrections = body.get("corrections") or {}
    if verdict not in ("approved", "corrected"):
        return jsonify({"error": "verdict must be 'approved' or 'corrected'"}), 400
    if not reviewer:
        return jsonify({"error": "reviewer name is required"}), 400

    _, _, schema_path, out_dir = _job_paths(job_id)
    rec_path = out_dir / "records.csv"
    if not rec_path.exists():
        return jsonify({"error": "records.csv not found for this database"}), 409
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return jsonify({"error": "schema.json unreadable for this database"}), 409
    fields = {f["name"]: f for f in schema.get("fields", [])}

    # validate + coerce corrections against the schema before touching anything
    errors, warnings, applied = [], [], {}
    for k, v in corrections.items():
        f = fields.get(k)
        if f is None:
            errors.append(f"unknown field {k!r}")
            continue
        if v is None or v == "":
            applied[k] = None
        else:
            applied[k] = pdf2db._coerce(f, v, errors, warnings)
    if errors:
        return jsonify({"error": "invalid corrections", "details": errors}), 400

    with REVIEW_LOCK:
        rows = pdf2db.read_csv(rec_path)
        row = next((r for r in rows if r.get("record_id") == record_id), None)
        if row is None:
            return jsonify({"error": f"record {record_id!r} not found"}), 404
        for k, v in applied.items():
            row[k] = "" if v is None else v
        row["status"] = "reviewed"
        _write_csv_rows(rec_path, rows)

        rq_path = out_dir / "review_queue.csv"
        if rq_path.exists():
            rq = pdf2db.read_csv(rq_path)
            for r in rq:
                if r.get("record_id") == record_id:
                    r["verdict"] = verdict
                    r["reviewer"] = reviewer
                    r["corrected_json"] = json.dumps(
                        applied, ensure_ascii=False, sort_keys=True,
                        default=str) if applied else ""
            _write_csv_rows(rq_path, rq)

        db_path = out_dir / f"{schema.get('table', 'records')}.db"
        if db_path.exists():
            import sqlite3
            con = sqlite3.connect(db_path)
            try:
                sets = ", ".join(f'"{k}" = ?' for k in applied) + \
                    (", " if applied else "") + '"status" = ?'
                vals = [int(v) if isinstance(v, bool) else v
                        for v in applied.values()] + ["reviewed", record_id]
                con.execute(f'UPDATE "records" SET {sets} WHERE "record_id" = ?',
                            vals)
                con.execute('UPDATE "review_queue" SET "verdict" = ?, '
                            '"reviewer" = ?, "corrected_json" = ? '
                            'WHERE "record_id" = ?',
                            [verdict, reviewer,
                             json.dumps(applied, ensure_ascii=False,
                                        sort_keys=True, default=str)
                             if applied else "", record_id])
                con.commit()
            finally:
                con.close()
    return jsonify({"ok": True, "warnings": warnings,
                    "applied": {k: v for k, v in applied.items()}})


def _table_from_db(db_path, name, offset, limit, q, status=""):
    """Paged, filtered read straight from the SQLite file — scales to large
    databases without re-parsing CSVs per request. Table names come from the
    TABLES whitelist, so identifier quoting is safe."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
        if not cols:
            raise sqlite3.Error(f"table {name} missing")
        clauses, params = [], []
        if q:
            clauses.append("(" + " OR ".join(
                f'CAST("{c}" AS TEXT) LIKE ?' for c in cols) + ")")
            params += [f"%{q}%"] * len(cols)
        if status and "status" in cols:
            clauses.append('"status" = ?')
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = con.execute(f'SELECT COUNT(*) FROM "{name}"{where}',
                            params).fetchone()[0]
        rows = con.execute(f'SELECT * FROM "{name}"{where} LIMIT ? OFFSET ?',
                           params + [limit, offset]).fetchall()
        return {"columns": cols,
                "rows": [["" if v is None else v for v in r] for r in rows],
                "total": total, "offset": offset}
    finally:
        con.close()


@app.get("/api/jobs/<job_id>/table/<name>")
def api_job_table(job_id, name):
    if name not in TABLES or job_id not in JOBS:
        abort(404)
    try:
        offset = max(0, int(request.args.get("offset", 0)))
        limit = min(500, max(1, int(request.args.get("limit", 200))))
    except ValueError:
        offset, limit = 0, 200
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()
    out_dir = JOBS_DIR / job_id / "out"
    with JOBS_LOCK:
        table = JOBS[job_id]["table"]
    db_path = out_dir / f"{table}.db"
    if name != "issues" and db_path.exists():  # issues live only in CSV
        try:
            return jsonify(_table_from_db(db_path, name, offset, limit, q,
                                          status))
        except sqlite3.Error:
            pass  # mid-write or missing table — fall back to the CSV
    path = out_dir / f"{name}.csv"
    if not path.exists():
        return jsonify({"columns": [], "rows": [], "total": 0, "offset": 0,
                        "note": f"{name}.csv not produced (yet)"})
    rows = pdf2db.read_csv(path)
    columns = list(rows[0].keys()) if rows else []
    if status and "status" in columns:
        rows = [r for r in rows if r.get("status") == status]
    if q:
        ql = q.lower()
        rows = [r for r in rows
                if any(ql in str(v).lower() for v in r.values())]
    return jsonify({"columns": columns,
                    "rows": [list(r.values()) for r in rows[offset:offset + limit]],
                    "total": len(rows), "offset": offset})


@app.get("/api/jobs/<job_id>/image/<path:relfile>")
def api_job_image(job_id, relfile):
    """Serve an exported embedded image (images/<record>/<file>) for previews."""
    if job_id not in JOBS:
        abort(404)
    base = (JOBS_DIR / job_id / "out").resolve()
    target = (base / relfile).resolve()
    if not str(target).startswith(str(base / "images") + os.sep) \
            or not target.is_file():
        abort(404)
    return send_file(target)


@app.get("/api/jobs/<job_id>/source-image/<path:relfile>")
def api_job_source_image(job_id, relfile):
    """Preview a loose image that lives in the archive itself (jpeg/png/tif
    beside the PDFs). Those are catalogued in place, never copied, so they are
    served from the archive root — only when the bytes really are an image, so
    this cannot be turned into a reader for the documents themselves."""
    if job_id not in JOBS:
        abort(404)
    root = _job_root(job_id).resolve()
    try:
        target = (root / relfile).resolve()
    except (OSError, RuntimeError):
        abort(404)
    if not str(target).startswith(str(root) + os.sep) or not target.is_file():
        abort(404)
    try:
        with open(target, "rb") as fh:
            head = fh.read(8)
    except OSError:
        abort(404)
    if not any(head.startswith(m) for m, _ in pdf2db.IMG_MAGICS):
        abort(404)
    return send_file(target)


@app.get("/api/jobs/<job_id>/download/<name>")
def api_job_download(job_id, name):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        abort(404)
    allowed = DOWNLOADABLE | {f"{job['table']}.db"}
    if name not in allowed:
        abort(404)
    path = JOBS_DIR / job_id / "out" / name
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=f"{job_id}-{name}")


def _asset_version():
    """Cache-buster for the static files: the app version plus the newest
    static mtime, so patching a file in place inside the air gap can never
    leave a stale copy in someone's browser."""
    newest = 0
    static = BASE_DIR / "static"
    if static.is_dir():
        for p in sorted(static.iterdir()):
            try:
                newest = max(newest, int(p.stat().st_mtime))
            except OSError:
                pass
    return f"{APP_VERSION}-{newest}"


@app.get("/")
def index():
    html = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    return Response(html.replace("?v=APPVER", "?v=" + _asset_version()),
                    mimetype="text/html")


if __name__ == "__main__":
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
    _rescan_jobs()
    _sweep_staging()
    host = os.environ.get("PDF2DB_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("PDF2DB_WEB_PORT", "8080"))
    print(f"[webapp] {APP_NAME} v{APP_VERSION}")
    print(f"[webapp] data dir: {DATA_DIR}")
    print(f"[webapp] open http://{host}:{port}")
    try:
        from waitress import serve
        threads = int(os.environ.get("PDF2DB_WEB_THREADS", "16"))
        print(f"[webapp] serving with waitress ({threads} threads)")
        serve(app, host=host, port=port, threads=threads,
              max_request_body_size=app.config["MAX_CONTENT_LENGTH"])
    except ImportError:
        print("[webapp] WARNING: waitress not installed — falling back to the "
              "Flask dev server (fine for testing, not for company use)")
        app.run(host=host, port=port, threaded=True, debug=False)
