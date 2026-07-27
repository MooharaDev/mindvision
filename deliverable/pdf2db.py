#!/usr/bin/env python3
r"""
pdf2db.py — general-purpose, schema-driven PDF -> database extraction pipeline.

Point it at ANY directory tree containing PDFs (any folder layout, any file
naming — PDFs are detected by magic bytes, not names), give it a schema file
describing the fields you want, and it:

  1. discover : inventories every PDF under --root (recursive, deterministic),
                applying include/exclude globs, depth and size limits, and
                dropping byte-identical duplicates inside a record. How PDFs
                group into records is the `record_unit` grammar:
                  pdf | folder | parent | depth:N | regex:PATTERN
                so nested, flat and id-in-the-filename archives all work
                without reorganising anything on disk.
  2. extract  : pulls text per PDF (pymupdf), flags probable scans (low text
                yield), concatenates per record, writes text artifacts.
                OPTIONALLY runs Tesseract on pages with no text layer
                (ocr_mode=auto|augment|force) — in-process via pymupdf, no
                vision model, no download; needs the tesseract OS package.
  3. llm      : sends each record's text + the schema to an OpenAI-compatible
                LLM endpoint (e.g. an internal/intranet deployment) which
                returns one validated JSON object per record; invalid replies
                get a bounded repair loop; every raw response is logged
  4. load     : builds a SQLite database (+ CSV exports of every table),
                including a human review queue (low confidence, null required
                fields, scan-suspect, truncated, seeded random audit sample)

AIR-GAP SAFE: python stdlib + pymupdf only. The ONLY network traffic is the
LLM endpoint you configure (set it to your internal gateway; set it to "mock"
for a fully offline deterministic stand-in). No downloads, no telemetry.
No pretrained-weights file is needed for this script. OCR, when enabled, uses
the locally installed tesseract binary — still zero network.

Run:
  python pdf2db.py --root ARCHIVE_DIR --schema schemas/failure_reports.json \
      --out out/ --llm-base-url http://internal-gw:8000/v1 --llm-model NAME
  python pdf2db.py --selftest        # offline end-to-end test, no inputs needed

  # one record per incident id found anywhere in the path, OCR the scans:
  python pdf2db.py --root ARCHIVE --schema S.json --out out/ \
      --record-unit 'regex:(?P<id>INC-\d+)' --ocr auto

Every stage reads/writes flat auditable artifacts in --out:
  pdf_inventory.csv, text_report.csv, records_text.csv, text/<record>.txt,
  raw_llm/<record>.json, extractions.jsonl, issues.csv,
  <table>.db, records.csv, pdfs.csv, review_queue.csv, run_summary.json
"""

import argparse
import concurrent.futures
import csv
from collections import Counter
import fnmatch
import hashlib
import json
import os
import re
import ssl
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

try:
    import pymupdf  # pymupdf >= 1.24 module name
except ImportError:  # older installs expose only the legacy name
    import fitz as pymupdf

# --------------------------------------------------------------------------
# CONFIG — every knob lives here; override via --config FILE and/or --set k=v
# --------------------------------------------------------------------------
CONFIG = {
    # paths
    "root": "",                     # archive root directory (required unless --selftest)
    "schema": "",                   # schema JSON file (required unless --selftest)
    "out": "out",                   # output directory
    # discovery — which files take part, and how they group into records
    "sniff_all_files": True,        # detect PDFs by %PDF- magic in ANY file, regardless of name
    "record_unit": "",              # "" = use the schema's record_unit; anything else overrides it.
                                    # pdf | folder | parent | depth:N | regex:PATTERN (see
                                    # parse_record_unit for the full grammar)
    "include_globs": "",            # ";"-separated fnmatch patterns on the relative path; "" = every file
    "exclude_globs": "~$*;*/~$*;.*;*/.*;__MACOSX/*",  # Office lock files, dotfiles, macOS zip cruft
    "max_file_mb": 0,               # skip PDFs larger than this; 0 = no limit
    "max_depth": 0,                 # only files at most N path segments deep; 0 = no limit
    "follow_symlinks": False,       # walking symlinked dirs risks loops and escaping --root
    "dedupe_identical": True,       # byte-identical PDFs inside ONE record are extracted once
                                    # ("report copy 2.pdf"); only same-size files get hashed
    "file_order": "path",           # order of a record's PDFs before concatenation:
                                    # path | mtime (oldest first) | size (largest first)
    # text extraction
    "extract_workers": 1,           # parallel PDFs in the extract stage. 1 = sequential (default,
                                    # always safe). Raise it when OCR dominates the runtime; each
                                    # worker opens its own document and shares nothing.
    "scan_chars_per_page": 200,     # below this avg chars/page a PDF is flagged low_yield (probable scan)
    "min_record_chars": 300,        # records with less usable text than this are status=no_text
    "max_record_chars": 80000,      # truncate longer concatenated text (head+tail) before the LLM
    "truncate_head_frac": 0.7,      # fraction of the budget spent on the head; rest on the tail
    # OCR — recovers text from scanned pages using Tesseract through pymupdf.
    # NO vision model and no network: the Tesseract BINARY plus its tessdata must be
    # provisioned inside the network (an OS package, not a pip install). With ocr_mode
    # left "off" nothing here is touched and scans are reported as no_text, as before.
    "ocr_mode": "off",              # off     — never OCR (default)
                                    # auto    — OCR pages whose text layer is below the threshold
                                    #           below, as a full-page image; digital pages untouched
                                    # augment — every page, image regions only: keeps the digital
                                    #           text and adds text from scanned figures pasted in
                                    # force   — every page as an image, ignoring any text layer
    "ocr_language": "eng",          # tesseract language code(s), e.g. "eng" or "eng+ara"
    "ocr_dpi": 300,                 # rasterisation dpi; 300 suits body text, 400+ for small print
    "ocr_tessdata": "",             # path to the tessdata directory; "" = autodetect
    "ocr_page_char_threshold": 50,  # auto mode: pages with fewer native chars than this get OCR'd
    "ocr_max_pages_per_pdf": 0,     # 0 = no cap; stops one 400-page scan from eating the whole run
    # image extraction (for the vision phase: images link to records by record_id)
    "extract_images": True,         # export embedded PDF images + catalog loose image files
    "image_min_px": 64,             # skip embedded images smaller than this on either side
    "image_min_bytes": 1024,        # skip embedded images smaller than this (logos, rules)
    # llm endpoint (OpenAI-compatible /chat/completions). "mock" = offline deterministic stand-in.
    "llm_base_url": "mock",
    "llm_model": "internal-model",
    "llm_api_key": "",              # discouraged; prefer the env var below
    "llm_api_key_env": "PDF2DB_API_KEY",
    # OAuth2 client-credentials (e.g. Metabrain via Keycloak SSO). When
    # llm_auth_url is set, a bearer token is fetched from it and renewed
    # automatically before expiry — llm_api_key is then ignored.
    "llm_auth_url": "",             # token endpoint URL; "" = static key auth
    "llm_client_id": "",
    "llm_client_secret": "",        # discouraged; prefer the env var below
    "llm_client_secret_env": "PDF2DB_CLIENT_SECRET",
    "llm_temperature": 0.0,
    "llm_force_json": False,        # send response_format={"type":"json_object"} (not all gateways support it)
    "llm_use_env_proxy": False,     # False = ignore http_proxy/HTTPS_PROXY env vars (air-gap default)
    "llm_ca_file": "",              # custom CA bundle for an HTTPS gateway with a private CA
    "llm_workers": 4,               # parallel LLM requests (1 = strictly sequential)
    "llm_timeout_s": 180,
    "llm_transport_retries": 3,
    "llm_retry_backoff_s": 2.0,
    "llm_validation_retries": 2,    # extra attempts when the reply fails schema validation
    "llm_sleep_between_calls_s": 0.0,
    "log_full_prompts": False,      # if True, raw_llm/ logs include full prompt text (large)
    # mock behaviour (selftest uses this)
    "mock_fail_first_attempt": False,  # first mock reply per record is broken, to exercise the repair loop
    # review routing
    "review_confidence_threshold": 0.7,
    "review_on_truncation": True,
    "review_sample_rate": 0.08,     # seeded random audit sample of otherwise-ok records
    "seed": 0,
    # run control
    "stages": "discover,extract,images,llm,load",
    "resume": False,                # keep prior extractions.jsonl; skip records already extracted ok
    "limit": 0,                     # >0: only send the first N pending records to the LLM (smoke runs)
}

RESERVED_COLUMNS = {
    "record_id", "source_paths", "n_pdfs", "n_pages", "chars_extracted",
    "truncated", "low_yield", "scanned_suspect", "status", "review_reasons",
    "llm_model", "llm_attempts", "extracted_at", "n_images",
    "ocr_pages", "native_chars", "ocr_chars",
    "_confidence", "_evidence", "_notes",
}
RECORD_UNIT_KINDS = ("pdf", "folder", "parent", "depth:N", "regex:PATTERN")
OCR_MODES = ("off", "auto", "augment", "force")
FILE_ORDERS = ("path", "mtime", "size")
IMG_MAGICS = [(b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF8", "gif"),
              (b"II*\x00", "tif"), (b"MM\x00*", "tif"), (b"BM", "bmp")]
FIELD_TYPES = {"string", "integer", "number", "boolean", "enum", "date"}
IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
SQL_TYPE = {"string": "TEXT", "date": "TEXT", "enum": "TEXT",
            "integer": "INTEGER", "number": "REAL", "boolean": "INTEGER"}


def log(msg):
    print(f"[pdf2db] {msg}", flush=True)


def die(msg):
    print(f"[pdf2db] FATAL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fieldnames})


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def require_artifact(path, stage, hint):
    """Fail with a clear operator message when a prerequisite artifact is
    missing (e.g. running --stages llm,load in a fresh --out directory)."""
    if not Path(path).exists():
        die(f"{stage}: required artifact {path} is missing — {hint}")


def read_jsonl_tolerant(path, what):
    """Read a .jsonl file, tolerating a truncated FINAL line (interrupted run).
    Returns (rows, truncated_final_line). A corrupt NON-final line aborts with
    the exact location — that is damage worth a human look, not auto-repair."""
    rows, truncated = [], False
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            if i == len(lines) - 1:
                log(f"WARNING: last line of {path} is truncated (interrupted run?) — "
                    f"ignored; that record will be re-extracted")
                truncated = True
            else:
                die(f"{what}: corrupt JSON at {path} line {i + 1}: {e}")
    return rows, truncated


def safe_filename(record_id):
    """Deterministic collision-proof filename for a record id."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", record_id)[:80]
    digest = hashlib.sha1(record_id.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
def validate_schema(schema):
    """Validate (and normalize in place) a schema dict. Returns a list of
    error strings — empty when valid. Safe to call from other tools (webapp)."""
    errs = []
    table = schema.get("table", "")
    if not IDENT_RE.match(table or ""):
        errs.append(f"table {table!r} must match {IDENT_RE.pattern}")
    try:
        parse_record_unit(schema.get("record_unit", "folder"))
    except ValueError as e:
        errs.append(f"record_unit: {e}")
    schema.setdefault("record_unit", "folder")
    schema.setdefault("task_description", "")
    fields = schema.get("fields") or []
    if not fields:
        errs.append("schema needs a non-empty 'fields' list")
    seen = set()
    for f in fields:
        name = f.get("name", "")
        if not IDENT_RE.match(name):
            errs.append(f"field name {name!r} must match {IDENT_RE.pattern}")
        if name in RESERVED_COLUMNS:
            errs.append(f"field name {name!r} is reserved for pipeline columns")
        if name in seen:
            errs.append(f"duplicate field name {name!r}")
        seen.add(name)
        if f.get("type") not in FIELD_TYPES:
            errs.append(f"field {name!r}: type must be one of {sorted(FIELD_TYPES)}")
        if f.get("type") == "enum" and not f.get("options"):
            errs.append(f"enum field {name!r} needs non-empty 'options'")
        f.setdefault("required", False)
        f.setdefault("description", "")
    return errs


def load_schema(path):
    with open(path, encoding="utf-8") as fh:
        schema = json.load(fh)
    errs = validate_schema(schema)
    if errs:
        die("invalid schema:\n  - " + "\n  - ".join(errs))
    return schema


def resolve_record_unit(cfg, schema):
    """CONFIG wins over the schema, so one schema can be re-pointed at an
    archive with a different layout without editing the file."""
    return (cfg.get("record_unit") or "").strip() or schema.get("record_unit", "folder")


def validate_cfg(cfg, schema=None):
    """Check every enum/range knob BEFORE any work starts, so a typo fails in
    the first second of a run rather than three hours into an archive."""
    errs = []
    if cfg["ocr_mode"] not in OCR_MODES:
        errs.append(f"ocr_mode must be one of {list(OCR_MODES)}, got {cfg['ocr_mode']!r}")
    if cfg["file_order"] not in FILE_ORDERS:
        errs.append(f"file_order must be one of {list(FILE_ORDERS)}, got {cfg['file_order']!r}")
    unit = resolve_record_unit(cfg, schema or {})
    try:
        parse_record_unit(unit)
    except ValueError as e:
        errs.append(f"record_unit: {e}")
    for key in ("extract_workers", "llm_workers", "ocr_dpi"):
        if int(cfg[key]) < 1:
            errs.append(f"{key} must be >= 1, got {cfg[key]!r}")
    for key in ("max_file_mb", "max_depth", "ocr_max_pages_per_pdf",
                "ocr_page_char_threshold", "limit"):
        if int(cfg[key]) < 0:
            errs.append(f"{key} must be >= 0, got {cfg[key]!r}")
    if not 0.0 <= float(cfg["truncate_head_frac"]) <= 1.0:
        errs.append(f"truncate_head_frac must be between 0 and 1, got {cfg['truncate_head_frac']!r}")
    if not 0.0 <= float(cfg["review_sample_rate"]) <= 1.0:
        errs.append(f"review_sample_rate must be between 0 and 1, got {cfg['review_sample_rate']!r}")
    if cfg["ocr_mode"] != "off" and tessdata_dir(cfg) is None:
        errs.append(
            f"ocr_mode={cfg['ocr_mode']!r} but Tesseract's tessdata directory was not found. "
            f"Install the tesseract OS package inside the network, then either set "
            f"TESSDATA_PREFIX or point ocr_tessdata at the directory holding *.traineddata. "
            f"Set ocr_mode=off to run without OCR.")
    if cfg.get("llm_auth_url") and cfg["llm_base_url"] != "mock":
        if not cfg.get("llm_client_id"):
            errs.append("llm_auth_url is set but llm_client_id is empty")
        if not (cfg.get("llm_client_secret")
                or os.environ.get(cfg.get("llm_client_secret_env") or "", "")):
            errs.append("llm_auth_url is set but no client secret is available "
                        f"(set llm_client_secret or the "
                        f"{cfg.get('llm_client_secret_env') or 'PDF2DB_CLIENT_SECRET'} "
                        f"env var)")
    return errs


def tessdata_dir(cfg):
    """Locate Tesseract's language data. Explicit config wins; otherwise ask
    pymupdf to autodetect (it reads TESSDATA_PREFIX). None = OCR unavailable."""
    explicit = (cfg.get("ocr_tessdata") or "").strip()
    if explicit:
        return explicit if Path(explicit).is_dir() else None
    try:
        found = pymupdf.get_tessdata()
    except Exception:   # pymupdf raises assorted types when mupdf lacks OCR support
        return None
    return found if found and Path(str(found)).is_dir() else None


def _progress(cfg, stage, done=0, total=0):
    """Optional progress hook: set cfg['_progress_cb'] to a callable to get
    (stage, done, total) updates. Used by webapp.py; no-op from the CLI."""
    cb = cfg.get("_progress_cb")
    if cb:
        cb(stage, done, total)


# --------------------------------------------------------------------------
# Stage 1: discover
# --------------------------------------------------------------------------
def parse_record_unit(spec):
    """Parse a record_unit spec into a (kind, param) pair, or raise ValueError.

    Real archives are not tidy, so grouping has to be describable rather than
    guessed. Given a PDF's path relative to the archive root:

      pdf            one record per PDF file
      folder         the top-level folder under root      (identical to depth:1)
      parent         the PDF's immediate parent folder    ("2003/Q1/INC-2231")
      depth:N        the folder N segments below root     (depth:2 -> "2003/Q1")
      regex:PATTERN  the first match anywhere in the path; the named group "id"
                     if the pattern defines one, else group 1, else the whole
                     match. Handles flat archives that carry the id in the file
                     name and nested ones that carry it in a directory:
                       regex:(?P<id>INC-\\d+)
    """
    spec = (spec or "folder").strip()
    if spec in ("pdf", "folder", "parent"):
        return spec, None
    if spec.startswith("depth:"):
        raw = spec[len("depth:"):].strip()
        if not raw.isdigit() or int(raw) < 1:
            raise ValueError(f"depth: needs a positive integer, got {raw!r}")
        return "depth", int(raw)
    if spec.startswith("regex:"):
        pattern = spec[len("regex:"):]
        if not pattern:
            raise ValueError("regex: needs a pattern after the colon")
        try:
            return "regex", re.compile(pattern)
        except re.error as e:
            raise ValueError(f"invalid regex {pattern!r}: {e}") from None
    raise ValueError(f"unknown record_unit {spec!r}; expected one of "
                     f"{', '.join(RECORD_UNIT_KINDS)}")


def assign_record_id(rel_path, record_unit):
    """Map one PDF path onto its record id.

    Returns (record_id, warning) where warning is None or a short reason the
    caller should log — a path that cannot satisfy the requested grouping still
    becomes a record (never silently dropped), just an unexpectedly shaped one.
    Accepts either a raw spec string or an already-parsed (kind, param) pair, so
    the hot loop can parse once.
    """
    kind, param = record_unit if isinstance(record_unit, tuple) \
        else parse_record_unit(record_unit)
    parts = rel_path.split("/")
    stem = rel_path[:-4] if rel_path.lower().endswith(".pdf") else rel_path
    if kind == "pdf":
        return stem, None
    if kind == "regex":
        m = param.search(rel_path)
        if not m:
            return stem, "no_regex_match"
        rid = m.group("id") if "id" in param.groupindex \
            else (m.group(1) if param.groups else m.group(0))
        return (rid, None) if rid else (stem, "empty_regex_match")
    # folder-shaped kinds: take the first n path segments
    n = {"folder": 1, "depth": param, "parent": len(parts) - 1}[kind]
    if n < 1 or len(parts) - 1 < n:
        return stem, "shallower_than_record_unit"   # PDF sits above the grouping level
    return "/".join(parts[:n]), None


def split_globs(spec):
    """';'-separated fnmatch patterns. ';' rather than ',' because commas are
    legal in file names and semicolons effectively are not."""
    return [g.strip() for g in (spec or "").split(";") if g.strip()]


def filter_reason(rel, size_bytes, includes, excludes, max_bytes, max_depth):
    """Why this path is not eligible for the inventory, or None if it is."""
    if max_depth and rel.count("/") + 1 > max_depth:
        return "max_depth"
    for g in excludes:
        if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(PurePosixPath(rel).name, g):
            return "exclude_globs"
    if includes and not any(fnmatch.fnmatch(rel, g)
                            or fnmatch.fnmatch(PurePosixPath(rel).name, g) for g in includes):
        return "include_globs"
    if max_bytes and size_bytes > max_bytes:
        return "max_file_mb"
    return None


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dedupe_inventory(root, inventory, issues):
    """Drop byte-identical PDFs that land in the SAME record — real archives are
    full of 'report copy 2.pdf'. Only files matching on (record, size) are
    hashed, so the common case costs nothing. Duplicates across different
    records are left alone: those are separate records that happen to share a
    document, which is a fact about the archive, not an error."""
    groups = {}
    for row in inventory:
        groups.setdefault((row["record_id"], row["size_bytes"]), []).append(row)
    dropped = set()
    for (rid, _size), rows in sorted(groups.items()):
        if len(rows) < 2:
            continue
        kept_by_digest = {}
        # shortest path first, then lexicographic: a copy's name is almost always
        # its original plus something ("report.pdf" beats "report copy 2.pdf"),
        # and the tie-break stays deterministic either way
        for row in sorted(rows, key=lambda r: (len(r["rel_path"]), r["rel_path"])):
            try:
                digest = _file_sha256(root / row["rel_path"])
            except OSError as e:
                issues.append({"severity": "warning", "stage": "discover",
                               "rel_path": row["rel_path"], "record_id": rid,
                               "reason": "dedupe_hash_failed", "detail": str(e)[:300]})
                continue
            first = kept_by_digest.setdefault(digest, row["rel_path"])
            if first != row["rel_path"]:
                dropped.add(row["rel_path"])
                issues.append({"severity": "warning", "stage": "discover",
                               "rel_path": row["rel_path"], "record_id": rid,
                               "reason": "duplicate_pdf_in_record",
                               "detail": f"byte-identical to {first}; extracted once"})
    return [r for r in inventory if r["rel_path"] not in dropped], len(dropped)


def stage_discover(cfg, schema, outdir, issues):
    root = Path(cfg["root"]).resolve()
    if not root.is_dir():
        die(f"--root {root} is not a directory")
    unit = parse_record_unit(resolve_record_unit(cfg, schema))
    includes, excludes = split_globs(cfg["include_globs"]), split_globs(cfg["exclude_globs"])
    max_bytes = int(cfg["max_file_mb"]) * 1024 * 1024
    skipped = Counter()
    examples = {}
    inventory, n_ignored = [], 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=cfg["follow_symlinks"]):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            try:
                stat = p.stat()
                with open(p, "rb") as fh:
                    magic = fh.read(5)
            except OSError as e:
                issues.append({"severity": "error", "stage": "discover", "rel_path": rel,
                               "record_id": "", "reason": "unreadable_file", "detail": str(e)[:300]})
                continue
            is_pdf = magic == b"%PDF-"
            has_pdf_ext = p.suffix.lower() == ".pdf"
            skip = filter_reason(rel, stat.st_size, includes, excludes, max_bytes, cfg["max_depth"])
            if skip:
                # only worth reporting for files that would otherwise have been input
                if is_pdf or has_pdf_ext:
                    skipped[skip] += 1
                    examples.setdefault(skip, []).append(rel)
                else:
                    n_ignored += 1
                continue
            if not is_pdf:
                if has_pdf_ext:
                    rid, _ = assign_record_id(rel, unit)
                    issues.append({"severity": "error", "stage": "discover", "rel_path": rel,
                                   "record_id": rid, "reason": "pdf_extension_but_not_pdf",
                                   "detail": f"magic bytes {magic!r}; if its folder has no other "
                                             f"PDFs, this record exists ONLY in issues.csv"})
                else:
                    n_ignored += 1  # images etc. — expected, not an issue
                continue
            if not has_pdf_ext and not cfg["sniff_all_files"]:
                n_ignored += 1
                continue
            rid, warn = assign_record_id(rel, unit)
            if warn:
                issues.append({"severity": "warning", "stage": "discover", "rel_path": rel,
                               "record_id": rid, "reason": warn,
                               "detail": f"record_unit={resolve_record_unit(cfg, schema)!r} does not "
                                         f"apply to this path; it becomes its own record {rid!r}"})
            inventory.append({"record_id": rid, "rel_path": rel, "size_bytes": stat.st_size,
                              "mtime": int(stat.st_mtime), "had_pdf_ext": int(has_pdf_ext)})
    # one bounded issues row per filter reason: auditable without flooding the file
    for reason, count in sorted(skipped.items()):
        shown = examples[reason][:5]
        issues.append({"severity": "warning", "stage": "discover", "rel_path": "", "record_id": "",
                       "reason": f"filtered_{reason}",
                       "detail": f"{count} PDF(s) excluded by {reason}; e.g. " + ", ".join(shown)})
    n_dupes = 0
    if cfg["dedupe_identical"]:
        inventory, n_dupes = dedupe_inventory(root, inventory, issues)
    inventory.sort(key=lambda r: (r["record_id"], r["rel_path"]))
    write_csv(outdir / "pdf_inventory.csv", inventory,
              ["record_id", "rel_path", "size_bytes", "mtime", "had_pdf_ext"])
    log(f"discover: {len(inventory)} PDFs across "
        f"{len({r['record_id'] for r in inventory})} records "
        f"(record_unit={resolve_record_unit(cfg, schema)!r}); {n_ignored} non-PDF files ignored"
        + (f"; {sum(skipped.values())} filtered out ("
           + ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())) + ")" if skipped else "")
        + (f"; {n_dupes} duplicate PDFs dropped" if n_dupes else ""))
    return inventory


# --------------------------------------------------------------------------
# Stage 2: extract text
# --------------------------------------------------------------------------
def extract_pdf_text(path, cfg=None):
    """Returns (page_texts, error, meta). error is None on success.

    With cfg["ocr_mode"] != "off" each page can additionally go through
    Tesseract (via pymupdf, in-process — no vision model, no network call).
    A page whose OCR fails keeps whatever native text it had and the failure is
    reported in meta["ocr_errors"]: one unreadable page must not lose the other
    399. Pass cfg=None for a plain text-layer read (probing, schema drafting).
    """
    meta = {"ocr_pages": 0, "native_chars": 0, "ocr_chars": 0, "ocr_errors": [], "ocr_capped": 0}
    mode = (cfg or {}).get("ocr_mode", "off")
    try:
        doc = pymupdf.open(path)
    except Exception as e:  # pymupdf raises several types; one file must not kill the run
        return None, f"open_error:{type(e).__name__}: {str(e)[:200]}", meta
    try:
        if doc.is_encrypted and not doc.authenticate(""):
            return None, "encrypted", meta
        tessdata = tessdata_dir(cfg) if mode != "off" else None
        cap = int((cfg or {}).get("ocr_max_pages_per_pdf", 0))
        pages = []
        for page in doc:
            native = page.get_text("text")
            meta["native_chars"] += len(native)
            if mode == "off":
                pages.append(native)
                continue
            # auto only pays for pages that have essentially no text layer;
            # augment keeps the digital text and adds scanned figures on top
            want = (mode in ("force", "augment")
                    or len(native.strip()) < int(cfg["ocr_page_char_threshold"]))
            if want and cap and meta["ocr_pages"] >= cap:
                meta["ocr_capped"] += 1
                pages.append(native)
                continue
            if not want:
                pages.append(native)
                continue
            try:
                tp = page.get_textpage_ocr(language=cfg["ocr_language"], dpi=int(cfg["ocr_dpi"]),
                                           full=(mode != "augment"), tessdata=tessdata)
                text = page.get_text("text", textpage=tp)
            except Exception as e:   # missing binary, bad language pack, corrupt page image
                meta["ocr_errors"].append(f"page {page.number + 1}: "
                                          f"{type(e).__name__}: {str(e)[:120]}")
                pages.append(native)
                continue
            meta["ocr_pages"] += 1
            meta["ocr_chars"] += max(0, len(text) - len(native))
            pages.append(text)
        return pages, None, meta
    except Exception as e:
        return None, f"read_error:{type(e).__name__}: {str(e)[:200]}", meta
    finally:
        doc.close()


def truncate_text(text, max_chars, head_frac):
    if len(text) <= max_chars:
        return text, False
    head = int(max_chars * head_frac)
    tail = max_chars - head
    omitted = len(text) - head - tail
    return (text[:head] + f"\n\n[... TRUNCATED: {omitted} characters omitted ...]\n\n"
            + text[-tail:]), True


def record_file_order(cfg):
    """Order a record's PDFs before their text is concatenated. Deterministic in
    every mode: rel_path is always the final tie-break."""
    mode = cfg["file_order"]
    if mode == "mtime":     # oldest first — usually the original report, then addenda
        return lambda r: (int(r.get("mtime") or 0), r["rel_path"])
    if mode == "size":      # largest first — the main report usually outweighs its attachments
        return lambda r: (-int(r.get("size_bytes") or 0), r["rel_path"])
    return lambda r: r["rel_path"]


def stage_extract(cfg, schema, outdir, issues):
    require_artifact(outdir / "pdf_inventory.csv", "extract",
                     "run the discover stage into this --out first")
    root = Path(cfg["root"]).resolve()
    inventory = read_csv(outdir / "pdf_inventory.csv")
    textdir = outdir / "text"
    textdir.mkdir(exist_ok=True)
    by_record = {}
    for row in inventory:
        by_record.setdefault(row["record_id"], []).append(row)
    order = record_file_order(cfg)

    # Read every PDF first (optionally in parallel — OCR is ~1-3 s/page and
    # dominates everything else), then assemble records in sorted order so the
    # artifacts are byte-identical regardless of worker count.
    def _read(rel):
        return rel, extract_pdf_text(root / rel, cfg)

    rels = [row["rel_path"] for row in inventory]
    results = {}
    workers = max(1, int(cfg["extract_workers"]))
    _progress(cfg, "extract", 0, len(rels))
    if workers == 1:
        for i, rel in enumerate(rels, 1):
            results[rel] = _read(rel)[1]
            _progress(cfg, "extract", i, len(rels))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for i, (rel, res) in enumerate(pool.map(_read, rels), 1):
                results[rel] = res
                _progress(cfg, "extract", i, len(rels))

    pdf_rows, record_rows = [], []
    for rid in sorted(by_record):
        chunks, tot_pages, tot_chars, n_ok = [], 0, 0, 0
        tot_ocr_pages, tot_native, tot_ocr_chars = 0, 0, 0
        any_low_yield = False
        for row in sorted(by_record[rid], key=order):
            rel = row["rel_path"]
            pages, err, meta = results[rel]
            for detail in meta["ocr_errors"]:
                issues.append({"severity": "warning", "stage": "extract", "rel_path": rel,
                               "record_id": rid, "reason": "ocr_page_failed", "detail": detail})
            if meta["ocr_capped"]:
                issues.append({"severity": "warning", "stage": "extract", "rel_path": rel,
                               "record_id": rid, "reason": "ocr_page_cap_reached",
                               "detail": f"{meta['ocr_capped']} page(s) left un-OCR'd by "
                                         f"ocr_max_pages_per_pdf={cfg['ocr_max_pages_per_pdf']}"})
            if err is not None:
                reason = err.split(":", 1)[0]
                issues.append({"severity": "error", "stage": "extract", "rel_path": rel,
                               "record_id": rid, "reason": reason, "detail": err})
                pdf_rows.append({"record_id": rid, "rel_path": rel, "n_pages": 0,
                                 "chars": 0, "chars_per_page": 0.0, "low_yield": 0,
                                 "ocr_pages": 0, "native_chars": 0, "ocr_chars": 0, "error": err})
                continue
            chars = sum(len(t) for t in pages)
            cpp = round(chars / len(pages), 1) if pages else 0.0
            low = int(len(pages) > 0 and cpp < cfg["scan_chars_per_page"])
            any_low_yield = any_low_yield or bool(low)
            pdf_rows.append({"record_id": rid, "rel_path": rel, "n_pages": len(pages),
                             "chars": chars, "chars_per_page": cpp, "low_yield": low,
                             "ocr_pages": meta["ocr_pages"], "native_chars": meta["native_chars"],
                             "ocr_chars": meta["ocr_chars"], "error": ""})
            chunks.append(f"===== FILE: {rel} =====\n" + "".join(pages))
            tot_pages += len(pages)
            tot_chars += chars
            tot_ocr_pages += meta["ocr_pages"]
            tot_native += meta["native_chars"]
            tot_ocr_chars += meta["ocr_chars"]
            n_ok += 1
        text = "\n\n".join(chunks)
        text, truncated = truncate_text(text, cfg["max_record_chars"], cfg["truncate_head_frac"])
        fname = safe_filename(rid) + ".txt"
        with open(textdir / fname, "w", encoding="utf-8") as fh:
            fh.write(text)
        record_rows.append({
            "record_id": rid, "n_pdfs": len(by_record[rid]), "n_pdfs_readable": n_ok,
            "n_pages": tot_pages, "chars": tot_chars, "truncated": int(truncated),
            "scanned_suspect": int(any_low_yield),
            "ocr_pages": tot_ocr_pages, "native_chars": tot_native, "ocr_chars": tot_ocr_chars,
            "usable": int(tot_chars >= cfg["min_record_chars"]),
            "text_file": f"text/{fname}",
            "source_paths": ";".join(sorted(r["rel_path"] for r in by_record[rid])),
        })
    write_csv(outdir / "text_report.csv", pdf_rows,
              ["record_id", "rel_path", "n_pages", "chars", "chars_per_page", "low_yield",
               "ocr_pages", "native_chars", "ocr_chars", "error"])
    write_csv(outdir / "records_text.csv", record_rows,
              ["record_id", "n_pdfs", "n_pdfs_readable", "n_pages", "chars", "truncated",
               "scanned_suspect", "ocr_pages", "native_chars", "ocr_chars", "usable",
               "text_file", "source_paths"])
    n_unusable = sum(1 for r in record_rows if not r["usable"])
    n_ocr = sum(r["ocr_pages"] for r in record_rows)
    log(f"extract: {len(record_rows)} records; {n_unusable} below min_record_chars "
        f"({cfg['min_record_chars']}); {sum(r['scanned_suspect'] for r in record_rows)} scan-suspect"
        + (f"; OCR recovered {sum(r['ocr_chars'] for r in record_rows)} chars from "
           f"{n_ocr} pages (mode={cfg['ocr_mode']})" if n_ocr else ""))
    return record_rows


# --------------------------------------------------------------------------
# Stage 2b: images — export embedded PDF images + catalog loose image files.
# Every image row carries record_id, so images join to extracted labels for
# the vision phase; `modality` is reserved (filled later by human/model).
# --------------------------------------------------------------------------
IMAGES_FIELDS = ["image_id", "record_id", "source", "origin", "page",
                 "file", "width", "height", "bytes", "format", "modality"]


def stage_images(cfg, schema, outdir, issues):
    imgs_csv = outdir / "images.csv"
    if not cfg["extract_images"]:
        write_csv(imgs_csv, [], IMAGES_FIELDS)
        log("images: skipped (extract_images=false)")
        return []
    require_artifact(outdir / "pdf_inventory.csv", "images",
                     "run the discover stage first")
    unit = parse_record_unit(resolve_record_unit(cfg, schema))
    root = Path(cfg["root"]).resolve()
    imgdir = outdir / "images"
    imgdir.mkdir(exist_ok=True)
    rows = []

    # 1. images embedded inside the PDFs
    for row in sorted(read_csv(outdir / "pdf_inventory.csv"),
                      key=lambda r: (r["record_id"], r["rel_path"])):
        rid, rel = row["record_id"], row["rel_path"]
        try:
            doc = pymupdf.open(root / rel)
            if doc.is_encrypted and not doc.authenticate(""):
                doc.close()
                continue  # already reported by the extract stage
            seen = set()
            for pno in range(doc.page_count):
                for info in doc.get_page_images(pno, full=True):
                    xref = info[0]
                    if xref in seen:
                        continue
                    seen.add(xref)
                    try:
                        img = doc.extract_image(xref)
                    except Exception:  # damaged/unsupported object — skip it
                        continue
                    w, h = img.get("width", 0), img.get("height", 0)
                    data = img.get("image", b"")
                    if (w < cfg["image_min_px"] or h < cfg["image_min_px"]
                            or len(data) < cfg["image_min_bytes"]):
                        continue
                    sub = imgdir / safe_filename(rid)
                    sub.mkdir(exist_ok=True)
                    ext = img.get("ext", "png")
                    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(rel).stem)[:60]
                    fname = f"{stem}_p{pno + 1}_x{xref}.{ext}"
                    (sub / fname).write_bytes(data)
                    rows.append({"record_id": rid, "source": "embedded",
                                 "origin": rel, "page": pno + 1,
                                 "file": f"images/{safe_filename(rid)}/{fname}",
                                 "width": w, "height": h, "bytes": len(data),
                                 "format": ext, "modality": ""})
            doc.close()
        except Exception as e:  # one bad PDF must not kill the stage
            issues.append({"severity": "warning", "stage": "images",
                           "rel_path": rel, "record_id": rid,
                           "reason": "image_extract_error",
                           "detail": f"{type(e).__name__}: {str(e)[:200]}"})

    # 2. loose image files (jpeg/png/tif/...) sitting beside the PDFs.
    #    These are catalogued in place, never copied. Ownership: the record id
    #    a loose file's own path implies is only meaningful in folder mode — in
    #    pdf mode every record is named after a PDF, so an image next to it
    #    would resolve to a record that does not exist. When the implied id is
    #    not a real record, fall back to the PDFs sharing the image's folder,
    #    and only claim ownership when exactly one PDF sits there. Anything
    #    ambiguous is reported in issues.csv with an empty record_id rather
    #    than guessed at.
    inventory = read_csv(outdir / "pdf_inventory.csv")
    known_ids = {r["record_id"] for r in inventory}
    ids_by_dir = {}
    for r in inventory:
        ids_by_dir.setdefault(str(PurePosixPath(r["rel_path"]).parent),
                              set()).add(r["record_id"])

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            try:
                with open(p, "rb") as fh:
                    head = fh.read(8)
            except OSError:
                continue  # unreadable files are reported by discover
            fmt = next((f for m, f in IMG_MAGICS if head.startswith(m)), None)
            if fmt is None:
                continue
            rid, _ = assign_record_id(rel, unit)
            if rid not in known_ids:
                owners = ids_by_dir.get(str(PurePosixPath(rel).parent), set())
                if len(owners) == 1:
                    rid = next(iter(owners))
                else:
                    issues.append({
                        "severity": "warning", "stage": "images",
                        "rel_path": rel, "record_id": "",
                        "reason": "image_owner_unresolved",
                        "detail": f"{len(owners)} PDFs share this image's folder"
                                  f"{' (' + ', '.join(sorted(owners)[:3]) + ')' if owners else ''}"
                                  " — the image is catalogued but joins to no "
                                  "record; move it beside a single PDF or use "
                                  "record_unit=folder"})
                    rid = ""
            w = h = 0
            try:
                pm = pymupdf.Pixmap(str(p))
                w, h = pm.width, pm.height
            except Exception:  # pymupdf raises FzError* — catalog it anyway
                pass  # dimensions stay unknown (0)
            rows.append({"record_id": rid, "source": "loose", "origin": rel,
                         "page": "", "file": "", "width": w, "height": h,
                         "bytes": p.stat().st_size, "format": fmt,
                         "modality": ""})

    rows.sort(key=lambda r: (r["record_id"], r["source"], r["origin"],
                             str(r["page"])))
    for i, r in enumerate(rows, 1):
        r["image_id"] = f"img-{i:06d}"
    write_csv(imgs_csv, rows, IMAGES_FIELDS)
    n_emb = sum(1 for r in rows if r["source"] == "embedded")
    log(f"images: {len(rows)} catalogued ({n_emb} embedded exported, "
        f"{len(rows) - n_emb} loose files)")
    return rows


# --------------------------------------------------------------------------
# Stage 3: LLM extraction
# --------------------------------------------------------------------------
class LLMTransportError(Exception):
    pass


def build_prompts(schema, record_id, text):
    lines = []
    for f in schema["fields"]:
        req = "required" if f["required"] else "optional"
        opts = f" Allowed values: {json.dumps(f['options'])}." if f["type"] == "enum" else ""
        lines.append(f'- "{f["name"]}" ({f["type"]}; {req}): {f["description"]}{opts}')
    system = ("You are a precise data-extraction engine. "
              "Output exactly one JSON object and nothing else — no markdown fences, no commentary.")
    user = f"""Task: extract structured fields from the document below.
{schema['task_description']}

Return a JSON object with exactly these keys:
{chr(10).join(lines)}
- "_confidence" (number; required): overall confidence 0.0-1.0 that the extracted values are correct.
- "_evidence" (object; required): maps field names to short VERBATIM quotes from the document \
supporting the value; include an entry for every field you filled; use {{}} if none.
- "_notes" (string; optional): anything ambiguous or noteworthy.

Rules:
- Use null for any field the document does not support. Never guess.
- Enum fields: use exactly one of the allowed values, or null.
- Dates: ISO format (YYYY-MM-DD, or YYYY-MM / YYYY if the document is less precise).
- The document may concatenate several files; "===== FILE: ... =====" lines mark boundaries.
- A "[... TRUNCATED ...]" marker means middle content was omitted for length.

DOCUMENT (id: {record_id}):
<<<BEGIN DOCUMENT
{text}
END DOCUMENT>>>"""
    return system, user


def parse_json_response(raw):
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return None, "response contains no JSON object"
    try:
        obj = json.loads(s[start:end + 1])
    except json.JSONDecodeError as e:
        return None, f"response is not valid JSON: {e}"
    if not isinstance(obj, dict):
        return None, "response JSON is not an object"
    return obj, None


def _coerce(f, value, errors, warnings):
    """Type-check/coerce one schema field value. Returns possibly-coerced value."""
    name, ftype = f["name"], f["type"]
    if value is None:
        return None
    if ftype == "string":
        if not isinstance(value, str):
            warnings.append(f"{name}: coerced {type(value).__name__} to string")
            return str(value)
        return value
    if ftype == "integer":
        if isinstance(value, bool):
            errors.append(f"{name}: expected integer, got boolean")
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            warnings.append(f"{name}: coerced string to integer")
            return int(value.strip())
        errors.append(f"{name}: expected integer, got {value!r}")
        return None
    if ftype == "number":
        if isinstance(value, bool):
            errors.append(f"{name}: expected number, got boolean")
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                warnings.append(f"{name}: coerced string to number")
                return float(value.strip())
            except ValueError:
                pass
        errors.append(f"{name}: expected number, got {value!r}")
        return None
    if ftype == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            warnings.append(f"{name}: coerced string to boolean")
            return value.strip().lower() == "true"
        errors.append(f"{name}: expected boolean, got {value!r}")
        return None
    if ftype == "enum":
        if not isinstance(value, str):
            errors.append(f"{name}: enum value must be a string, got {type(value).__name__}")
            return None
        if value in f["options"]:
            return value
        lowered = {o.lower(): o for o in f["options"]}
        if value.lower() in lowered:
            warnings.append(f"{name}: case-corrected enum value {value!r}")
            return lowered[value.lower()]
        errors.append(f"{name}: {value!r} is not one of {f['options']}")
        return None
    if ftype == "date":
        if not isinstance(value, str):
            errors.append(f"{name}: date must be a string, got {type(value).__name__}")
            return None
        if not DATE_RE.match(value.strip()):
            warnings.append(f"{name}: date {value!r} not ISO (YYYY[-MM[-DD]]); kept as-is")
        return value.strip()
    errors.append(f"{name}: unhandled type {ftype}")  # unreachable if schema validated
    return None


def validate_and_coerce(schema, obj, text):
    errors, warnings = [], []
    clean = {}
    known = {f["name"] for f in schema["fields"]}
    for key in obj:
        if key not in known and key not in ("_confidence", "_evidence", "_notes"):
            warnings.append(f"unknown key {key!r} dropped")
    for f in schema["fields"]:
        name = f["name"]
        if name not in obj:
            errors.append(f"missing key {name!r} (use null if the document does not support it)")
            continue
        clean[name] = _coerce(f, obj[name], errors, warnings)
    conf = obj.get("_confidence")
    if isinstance(conf, str):
        try:
            conf = float(conf)
        except ValueError:
            conf = None
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= float(conf) <= 1.0:
        errors.append('missing or invalid "_confidence" (number between 0 and 1)')
    else:
        clean["_confidence"] = float(conf)
    ev = obj.get("_evidence")
    if not isinstance(ev, dict):
        errors.append('missing or invalid "_evidence" (object mapping field names to quotes)')
    else:
        norm_text = " ".join(text.split())
        for k, q in ev.items():
            if isinstance(q, str) and q and " ".join(q.split()) not in norm_text:
                warnings.append(f"evidence for {k!r} is not a verbatim quote")
        clean["_evidence"] = json.dumps(ev, ensure_ascii=False, sort_keys=True)
    notes = obj.get("_notes")
    clean["_notes"] = notes if isinstance(notes, str) else None
    return errors, warnings, clean


def mock_llm_response(cfg, schema, record_id, text, attempt):
    """Deterministic offline stand-in for the internal LLM (also used by --selftest)."""
    if cfg["mock_fail_first_attempt"] and attempt == 0:
        return 'Sure! Here is the JSON you asked for: {"broken": '
    def h(s):
        return int(hashlib.sha256(f"{record_id}:{s}".encode()).hexdigest(), 16)
    obj = {}
    ev = {}
    quote = text[:60].strip()
    for f in schema["fields"]:
        n, t = f["name"], f["type"]
        if t == "enum":
            obj[n] = f["options"][h(n) % len(f["options"])]
        elif t == "integer":
            obj[n] = h(n) % 100
        elif t == "number":
            obj[n] = round((h(n) % 1000) / 10.0, 1)
        elif t == "boolean":
            obj[n] = h(n) % 2 == 0
        elif t == "date":
            obj[n] = f"{1970 + h(n) % 56}-01-15"
        else:
            obj[n] = f"MOCK {n} {h(n) % 1000}"
        if quote:
            ev[n] = quote
    obj["_confidence"] = round(min(0.95, max(0.15, len(text) / 4000.0)), 2)
    obj["_evidence"] = ev
    obj["_notes"] = "mock backend"
    return json.dumps(obj, ensure_ascii=False)


def _llm_opener(cfg):
    handlers = [] if cfg["llm_use_env_proxy"] else \
        [urllib.request.ProxyHandler({})]  # ambient proxies must never re-route
    if cfg.get("llm_ca_file"):  # HTTPS gateway signed by an internal/private CA
        ctx = ssl.create_default_context(cafile=cfg["llm_ca_file"])
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


# OAuth2 client-credentials token cache. Tokens from SSO gateways (Metabrain/
# Keycloak) expire — typically after 3600 s with no refresh token — so long
# batch runs must re-fetch. One cache per (endpoint, client), shared across
# the llm worker threads and the web console's request threads.
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE = {}  # (auth_url, client_id) -> (token, renew_after_epoch)


def _oauth_token(cfg, opener, force=False):
    """Bearer token via the client-credentials grant, renewed 120 s before
    expiry so a token can never lapse mid-request. force=True drops the cached
    token first (used once after an unexpected 401)."""
    cache_key = (cfg["llm_auth_url"], cfg.get("llm_client_id", ""))
    with _TOKEN_LOCK:
        cached = None if force else _TOKEN_CACHE.get(cache_key)
        if cached and time.time() < cached[1]:
            return cached[0]
        secret = os.environ.get(cfg.get("llm_client_secret_env") or "", "") \
            or cfg.get("llm_client_secret", "")
        form = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": cfg.get("llm_client_id", ""),
            "client_secret": secret,
        }).encode("ascii")
        req = urllib.request.Request(
            cfg["llm_auth_url"], data=form, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "pdf2db/1.0"})
        try:
            with opener.open(req, timeout=cfg["llm_timeout_s"]) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = e.read()[:300]
            except OSError:
                detail = b""
            raise LLMTransportError(
                f"token endpoint HTTP {e.code}: {detail!r} — check "
                f"llm_client_id / client secret") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMTransportError(f"token endpoint unreachable: {e}") from e
        token = body.get("access_token")
        if not token:
            raise LLMTransportError("token endpoint returned no access_token "
                                    f"(keys: {sorted(body)[:8]})")
        try:
            ttl = float(body.get("expires_in", 300))
        except (TypeError, ValueError):
            ttl = 300.0
        _TOKEN_CACHE[cache_key] = (token, time.time() + max(30.0, ttl - 120.0))
        return token


def call_openai_compat(cfg, messages):
    url = cfg["llm_base_url"].rstrip("/") + "/chat/completions"
    payload = {"model": cfg["llm_model"], "messages": messages,
               "temperature": cfg["llm_temperature"]}
    if cfg["llm_force_json"]:
        payload["response_format"] = {"type": "json_object"}
    # explicit User-Agent: python-urllib's default gets blocked by bot filters
    # (e.g. Cloudflare error 1010) in front of some OpenAI-compatible providers
    headers = {"Content-Type": "application/json", "User-Agent": "pdf2db/1.0"}
    opener = _llm_opener(cfg)
    use_oauth = bool(cfg.get("llm_auth_url"))
    if use_oauth:
        headers["Authorization"] = "Bearer " + _oauth_token(cfg, opener)
    else:
        key = os.environ.get(cfg["llm_api_key_env"] or "", "") or cfg["llm_api_key"]
        if key:
            headers["Authorization"] = f"Bearer {key}"
    last_err = "no attempt made"
    refreshed_after_401 = False
    for i in range(cfg["llm_transport_retries"]):
        delay = cfg["llm_retry_backoff_s"] * (2 ** i)
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers=headers, method="POST")
            with opener.open(req, timeout=cfg["llm_timeout_s"]) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            try:
                detail = e.read()[:500]
            except OSError:
                detail = b""
            last_err = f"HTTP {e.code}: {detail!r}"
            if e.code == 401 and use_oauth and not refreshed_after_401:
                # token may have been revoked or expired early — one fresh
                # token, then continue the normal retry budget
                refreshed_after_401 = True
                headers["Authorization"] = "Bearer " + \
                    _oauth_token(cfg, opener, force=True)
                continue
            if e.code in (400, 401, 403, 404, 422):
                break  # config problem; retrying will not help
            if e.code == 429:  # rate limited — honor the server's Retry-After
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    delay = max(delay, min(float(ra), 60.0))
                except (TypeError, ValueError):
                    pass
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
        except (KeyError, IndexError, TypeError, ValueError) as e:
            last_err = f"unexpected response shape: {type(e).__name__}: {e}"
        time.sleep(delay)
    raise LLMTransportError(last_err)


def extract_record(cfg, schema, record_id, text):
    """Returns (clean_data_or_None, warnings, attempts_log, fail_reason_or_None)."""
    system, user = build_prompts(schema, record_id, text)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    attempts = []
    for attempt in range(cfg["llm_validation_retries"] + 1):
        if cfg["llm_base_url"] == "mock":
            raw = mock_llm_response(cfg, schema, record_id, text, attempt)
        else:
            try:
                raw = call_openai_compat(cfg, messages)
            except LLMTransportError as e:
                attempts.append({"attempt": attempt, "error": f"transport: {e}"})
                return None, [], attempts, f"transport: {e}"
        obj, perr = parse_json_response(raw)
        if perr:
            errors, warnings, clean = [perr], [], None
        else:
            errors, warnings, clean = validate_and_coerce(schema, obj, text)
        attempts.append({"attempt": attempt, "response": raw,
                         "errors": errors, "warnings": warnings})
        if not errors:
            return clean, warnings, attempts, None
        repair = ("Your previous response failed validation with these errors:\n- "
                  + "\n- ".join(errors)
                  + "\nReturn the corrected JSON object only — no commentary.")
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": repair})
    return None, [], attempts, "validation failed after retries: " + "; ".join(attempts[-1]["errors"])


def stage_llm(cfg, schema, outdir, issues):
    require_artifact(outdir / "records_text.csv", "llm",
                     "run the discover+extract stages into this --out first")
    records = read_csv(outdir / "records_text.csv")
    rawdir = outdir / "raw_llm"
    rawdir.mkdir(exist_ok=True)
    jl_path = outdir / "extractions.jsonl"
    done = {}
    if jl_path.exists():
        if cfg["resume"]:
            rows, truncated = read_jsonl_tolerant(jl_path, "resume")
            for row in rows:
                done[row["record_id"]] = row["status"]
            if truncated:  # drop the torn line so the file stays parseable after we append
                with open(jl_path, "w", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        elif jl_path.stat().st_size > 0:
            bak = Path(str(jl_path) + ".bak")
            bak.write_bytes(jl_path.read_bytes())
            log(f"WARNING: prior {jl_path.name} saved to {bak.name} — "
                f"running the llm stage without --resume starts extraction over")
    mode = "a" if cfg["resume"] else "w"
    todo, n_skipped = [], 0
    for r in sorted(records, key=lambda r: r["record_id"]):
        if not int(r["usable"]):
            continue  # load stage will mark it no_text
        if done.get(r["record_id"]) == "ok":
            n_skipped += 1
            continue
        todo.append(r)
    if cfg["limit"] and len(todo) > cfg["limit"]:
        log(f"llm: --limit {cfg['limit']}: {len(todo) - cfg['limit']} records stay pending_llm")
        todo = todo[:cfg["limit"]]
    n_sent = n_ok = n_failed = 0
    _progress(cfg, "llm", 0, len(todo))

    def _process(r):
        """Worker: LLM round-trips only. All file writes stay on the main
        thread so artifacts can never interleave."""
        rid = r["record_id"]
        with open(outdir / r["text_file"], encoding="utf-8") as fh:
            text = fh.read()
        clean, warnings, attempts, fail = extract_record(cfg, schema, rid, text)
        return r, text, clean, warnings, attempts, fail

    workers = max(1, int(cfg["llm_workers"]))
    with open(jl_path, mode, encoding="utf-8") as out:
        def _emit(i, r, text, clean, warnings, attempts, fail):
            nonlocal n_ok, n_failed
            rid = r["record_id"]
            rawlog = {"record_id": rid, "model": cfg["llm_model"],
                      "chars_sent": len(text), "attempts": attempts}
            if cfg["log_full_prompts"]:
                sys_p, user_p = build_prompts(schema, rid, text)
                rawlog["system_prompt"], rawlog["user_prompt"] = sys_p, user_p
            with open(rawdir / (safe_filename(rid) + ".json"), "w",
                      encoding="utf-8") as fh:
                json.dump(rawlog, fh, ensure_ascii=False, indent=1)
            if clean is None:
                status = "llm_failed" if (fail or "").startswith("transport") \
                    else "validation_failed"
                issues.append({"severity": "error", "stage": "llm",
                               "rel_path": r["text_file"], "record_id": rid,
                               "reason": status, "detail": (fail or "")[:300]})
                out.write(json.dumps({"record_id": rid, "status": status,
                                      "data": None, "warnings": [], "error": fail,
                                      "attempts": len(attempts),
                                      "model": cfg["llm_model"],
                                      "extracted_at": now_iso()},
                                     ensure_ascii=False) + "\n")
                n_failed += 1
            else:
                out.write(json.dumps({"record_id": rid, "status": "ok",
                                      "data": clean, "warnings": warnings,
                                      "error": None, "attempts": len(attempts),
                                      "model": cfg["llm_model"],
                                      "extracted_at": now_iso()},
                                     ensure_ascii=False) + "\n")
                n_ok += 1
            log(f"llm [{i}/{len(todo)}] {rid}: "
                f"{'ok' if clean is not None else 'FAILED'} (attempts={len(attempts)})")
            _progress(cfg, "llm", i, len(todo))
            out.flush()

        if workers == 1:
            for i, r in enumerate(todo, 1):
                n_sent += 1
                res = _process(r)
                _emit(i, res[0], res[1], res[2], res[3], res[4], res[5])
                if cfg["llm_sleep_between_calls_s"]:
                    time.sleep(cfg["llm_sleep_between_calls_s"])
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_process, r) for r in todo]
                n_sent = len(futures)
                for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                    res = fut.result()
                    _emit(i, res[0], res[1], res[2], res[3], res[4], res[5])
    log(f"llm: sent {n_sent} records ({n_ok} ok, {n_failed} failed, "
        f"{n_skipped} resumed-skip, workers={workers})")


# --------------------------------------------------------------------------
# Stage 4: load into SQLite + CSV exports + review queue
# --------------------------------------------------------------------------
def in_audit_sample(cfg, record_id):
    h = int(hashlib.sha256(f"{cfg['seed']}:{record_id}".encode()).hexdigest(), 16)
    return (h % 10000) < cfg["review_sample_rate"] * 10000


def stage_load(cfg, schema, outdir, issues):
    require_artifact(outdir / "records_text.csv", "load",
                     "run the discover+extract stages into this --out first")
    records_text = {r["record_id"]: r for r in read_csv(outdir / "records_text.csv")}
    inventory = read_csv(outdir / "pdf_inventory.csv")
    pdf_rows = read_csv(outdir / "text_report.csv")
    size_by_path = {r["rel_path"]: r["size_bytes"] for r in inventory}
    for p in pdf_rows:
        p["size_bytes"] = size_by_path.get(p["rel_path"], "")
    extractions = {}
    jl_path = outdir / "extractions.jsonl"
    if jl_path.exists():
        rows, _truncated = read_jsonl_tolerant(jl_path, "load")
        for row in rows:
            extractions[row["record_id"]] = row  # later lines win (resume appends)
    images = read_csv(outdir / "images.csv") if (outdir / "images.csv").exists() else []
    n_images_by_record = Counter(i["record_id"] for i in images)

    field_names = [f["name"] for f in schema["fields"]]
    required = {f["name"] for f in schema["fields"] if f["required"]}
    out_rows, review_rows = [], []
    for rid in sorted(records_text):
        rt = records_text[rid]
        ex = extractions.get(rid)
        row = {name: None for name in field_names}
        row.update({
            "record_id": rid,
            "_confidence": None, "_evidence": None, "_notes": None,
            "source_paths": rt["source_paths"], "n_pdfs": int(rt["n_pdfs"]),
            "n_images": n_images_by_record.get(rid, 0),
            "n_pages": int(rt["n_pages"]), "chars_extracted": int(rt["chars"]),
            "truncated": int(rt["truncated"]), "scanned_suspect": int(rt["scanned_suspect"]),
            "ocr_pages": int(rt.get("ocr_pages") or 0),
            "llm_model": None, "llm_attempts": None, "extracted_at": None,
        })
        reasons = []
        if not int(rt["usable"]):
            status = "no_text"
            reasons.append("below_min_record_chars")
        elif ex is None:
            status = "pending_llm"
        elif ex["status"] != "ok":
            status = ex["status"]
            row["llm_model"], row["llm_attempts"] = ex["model"], ex["attempts"]
            row["extracted_at"] = ex["extracted_at"]
        else:
            data = ex["data"]
            for name in field_names:
                row[name] = data.get(name)
            row["_confidence"] = data.get("_confidence")
            row["_evidence"] = data.get("_evidence")
            row["_notes"] = data.get("_notes")
            row["llm_model"], row["llm_attempts"] = ex["model"], ex["attempts"]
            row["extracted_at"] = ex["extracted_at"]
            if row["_confidence"] is not None and row["_confidence"] < cfg["review_confidence_threshold"]:
                reasons.append("low_confidence")
            null_req = sorted(n for n in required if data.get(n) is None)
            if null_req:
                reasons.append("required_null:" + ",".join(null_req))
            if int(rt["scanned_suspect"]):
                reasons.append("scanned_suspect")
            if int(rt.get("ocr_pages") or 0):
                # OCR output is never as trustworthy as a real text layer
                reasons.append("ocr_used")
            if cfg["review_on_truncation"] and int(rt["truncated"]):
                reasons.append("truncated")
            if not reasons and in_audit_sample(cfg, rid):
                reasons.append("random_audit")
            status = "needs_review" if reasons else "ok"
        row["status"] = status
        row["review_reasons"] = ";".join(reasons)
        out_rows.append(row)
        if status == "needs_review":
            review_rows.append({
                "record_id": rid, "review_reasons": row["review_reasons"],
                "_confidence": row["_confidence"], "_evidence": row["_evidence"],
                "text_file": rt["text_file"],
                "extracted_json": json.dumps({n: row[n] for n in field_names},
                                             ensure_ascii=False, sort_keys=True),
                "corrected_json": "", "reviewer": "", "verdict": "",
            })

    # ---- SQLite ----
    db_path = outdir / f"{schema['table']}.db"
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        for t in ("records", "pdfs", "review_queue", "images"):
            cur.execute(f'DROP TABLE IF EXISTS "{t}"')
        rec_cols = (['"record_id" TEXT PRIMARY KEY']
                    + [f'"{f["name"]}" {SQL_TYPE[f["type"]]}' for f in schema["fields"]]
                    + ['"_confidence" REAL', '"_evidence" TEXT', '"_notes" TEXT',
                       '"source_paths" TEXT', '"n_pdfs" INTEGER',
                       '"n_images" INTEGER', '"n_pages" INTEGER',
                       '"chars_extracted" INTEGER', '"truncated" INTEGER',
                       '"scanned_suspect" INTEGER', '"ocr_pages" INTEGER',
                       '"status" TEXT', '"review_reasons" TEXT',
                       '"llm_model" TEXT', '"llm_attempts" INTEGER', '"extracted_at" TEXT'])
        cur.execute(f'CREATE TABLE "records" ({", ".join(rec_cols)})')
        rec_fieldnames = (["record_id"] + field_names
                          + ["_confidence", "_evidence", "_notes", "source_paths", "n_pdfs",
                             "n_images", "n_pages", "chars_extracted", "truncated",
                             "scanned_suspect", "ocr_pages", "status", "review_reasons",
                             "llm_model", "llm_attempts", "extracted_at"])
        placeholders = ", ".join("?" for _ in rec_fieldnames)
        quoted = ", ".join(f'"{c}"' for c in rec_fieldnames)
        for row in out_rows:
            vals = [row[c] if not isinstance(row[c], bool) else int(row[c])
                    for c in rec_fieldnames]
            cur.execute(f'INSERT INTO "records" ({quoted}) VALUES ({placeholders})', vals)
        cur.execute('CREATE TABLE "pdfs" ("record_id" TEXT '
                    'REFERENCES "records"("record_id"), "rel_path" TEXT, '
                    '"size_bytes" INTEGER, "n_pages" INTEGER, "chars" INTEGER, '
                    '"chars_per_page" REAL, "low_yield" INTEGER, "ocr_pages" INTEGER, '
                    '"native_chars" INTEGER, "ocr_chars" INTEGER, "error" TEXT)')
        for p in pdf_rows:
            cur.execute('INSERT INTO "pdfs" VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        [p["record_id"], p["rel_path"], p["size_bytes"] or None, p["n_pages"],
                         p["chars"], p["chars_per_page"], p["low_yield"], p["ocr_pages"],
                         p["native_chars"], p["ocr_chars"], p["error"]])
        # images table: a declared foreign key to records — this is the link
        # the vision phase trains on (label columns live on records). An image
        # whose owning record could not be resolved is stored with a NULL
        # record_id so the key stays honest instead of pointing nowhere.
        cur.execute('CREATE TABLE "images" ("image_id" TEXT PRIMARY KEY, '
                    '"record_id" TEXT REFERENCES "records"("record_id"), '
                    '"source" TEXT, "origin" TEXT, '
                    '"page" INTEGER, "file" TEXT, "width" INTEGER, '
                    '"height" INTEGER, "bytes" INTEGER, "format" TEXT, '
                    '"modality" TEXT)')
        for im in images:
            cur.execute('INSERT INTO "images" VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        [im["image_id"], im["record_id"] or None, im["source"],
                         im["origin"], int(im["page"]) if im["page"] else None,
                         im["file"], int(im["width"] or 0), int(im["height"] or 0),
                         int(im["bytes"] or 0), im["format"], im["modality"]])
        rq_cols = ["record_id", "review_reasons", "_confidence", "_evidence", "text_file",
                   "extracted_json", "corrected_json", "reviewer", "verdict"]
        cur.execute('CREATE TABLE "review_queue" ("record_id" TEXT '
                    'REFERENCES "records"("record_id"), '
                    + ", ".join(f'"{c}" TEXT' for c in rq_cols[1:]) + ")")
        for r in review_rows:
            cur.execute(f'INSERT INTO "review_queue" VALUES ({", ".join("?" for _ in rq_cols)})',
                        [r[c] for c in rq_cols])
        # indexes for the joins and filters this database exists to serve:
        # images/pdfs/review rows by record, and records by status (the
        # review-queue filter). Dropping a table drops its indexes, so these
        # are simply recreated on every load.
        cur.execute('CREATE INDEX "ix_images_record" ON "images"("record_id")')
        cur.execute('CREATE INDEX "ix_pdfs_record" ON "pdfs"("record_id")')
        cur.execute('CREATE INDEX "ix_review_record" ON "review_queue"("record_id")')
        cur.execute('CREATE INDEX "ix_records_status" ON "records"("status")')
        con.commit()
    finally:
        con.close()

    # ---- CSV exports ----
    write_csv(outdir / "records.csv", out_rows, rec_fieldnames)
    write_csv(outdir / "pdfs.csv", pdf_rows,
              ["record_id", "rel_path", "size_bytes", "n_pages", "chars", "chars_per_page",
               "low_yield", "ocr_pages", "native_chars", "ocr_chars", "error"])
    write_csv(outdir / "review_queue.csv", review_rows, rq_cols)

    counts = {}
    for row in out_rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    summary = {
        "finished_at": now_iso(),
        "table": schema["table"], "record_unit": resolve_record_unit(cfg, schema),
        "n_records": len(out_rows), "n_pdfs": len(pdf_rows),
        "ocr_mode": cfg["ocr_mode"],
        "n_ocr_pages": sum(int(r.get("ocr_pages") or 0) for r in records_text.values()),
        "status_counts": dict(sorted(counts.items())),
        "n_review_queue": len(review_rows),
        "n_issues": len(issues),
        "config": {k: v for k, v in cfg.items()
                   if k not in ("llm_api_key", "llm_client_secret")
                   and not k.startswith("_")},
    }
    with open(outdir / "run_summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
    log(f"load: {len(out_rows)} records -> {db_path.name}; statuses {summary['status_counts']}; "
        f"{len(review_rows)} in review queue")
    return summary


# --------------------------------------------------------------------------
# Pipeline driver
# --------------------------------------------------------------------------
def run_pipeline(cfg):
    schema = load_schema(cfg["schema"])
    errs = validate_cfg(cfg, schema)
    if errs:
        die("invalid config:\n  - " + "\n  - ".join(errs))
    outdir = Path(cfg["out"])
    outdir.mkdir(parents=True, exist_ok=True)
    issues = []
    stages = [s.strip() for s in cfg["stages"].split(",") if s.strip()]
    unknown = set(stages) - {"discover", "extract", "images", "llm", "load"}
    if unknown:
        die(f"unknown stages: {sorted(unknown)}")
    if cfg["ocr_mode"] != "off":
        log(f"OCR enabled (mode={cfg['ocr_mode']}, language={cfg['ocr_language']}, "
            f"dpi={cfg['ocr_dpi']}, tessdata={tessdata_dir(cfg)})")
    if "discover" in stages:
        _progress(cfg, "discover")
        stage_discover(cfg, schema, outdir, issues)
    if "extract" in stages:
        _progress(cfg, "extract")
        stage_extract(cfg, schema, outdir, issues)
    if "images" in stages:
        _progress(cfg, "images")
        stage_images(cfg, schema, outdir, issues)
    if "llm" in stages:
        stage_llm(cfg, schema, outdir, issues)
    if "load" in stages:
        _progress(cfg, "load")
        summary = stage_load(cfg, schema, outdir, issues)
    else:
        summary = None
    _progress(cfg, "finished")
    # merge: keep issue rows from stages NOT re-run in this invocation, so split-stage
    # workflows (--stages discover,extract now; llm,load later) never lose the record
    issues_path = outdir / "issues.csv"
    preserved = []
    if issues_path.exists():
        preserved = [r for r in read_csv(issues_path) if r.get("stage") not in set(stages)]
    all_issues = preserved + issues
    write_csv(issues_path, all_issues,
              ["severity", "stage", "rel_path", "record_id", "reason", "detail"])
    if all_issues:
        log(f"ATTENTION: {len(all_issues)} issues in issues.csv "
            f"({sum(1 for i in all_issues if i['severity'] == 'error')} errors)")
    return summary


# --------------------------------------------------------------------------
# Selftest — fabricates a tiny synthetic archive, runs everything offline
# --------------------------------------------------------------------------
SELFTEST_SCHEMA = {
    "table": "test_records",
    "record_unit": "folder",
    "task_description": "Each record is a small synthetic test document.",
    "fields": [
        {"name": "title", "type": "string", "required": True, "description": "Document title."},
        {"name": "category", "type": "enum", "required": True,
         "options": ["alpha", "beta", "gamma"], "description": "Test category."},
        {"name": "year", "type": "integer", "required": False, "description": "Year mentioned."},
        {"name": "approved", "type": "boolean", "required": False, "description": "Approval flag."},
        {"name": "event_date", "type": "date", "required": False, "description": "Event date."},
    ],
}


# Lines rasterised into the OCR fixture. Deliberately plain: the test proves the
# OCR path is wired up and returns the page's words, not that Tesseract is accurate.
OCR_FIXTURE_LINES = [
    "INCIDENT REPORT INC-9001",
    "Category: beta classification applies here",
    "The pipeline section failed during routine inspection",
    "Observed wall thinning across the lower weld seam",
    "Maintenance crew documented every finding in detail",
    "Recommended replacement of the affected spool piece",
    "Follow up inspection scheduled for the next shutdown",
    "No further action required at this time by the team",
]


def _make_pdf(path, text, pages=None):
    doc = pymupdf.open()
    chunks = pages if pages is not None else [text[i:i + 1500] for i in range(0, len(text), 1500)] or [""]
    for chunk in chunks:
        page = doc.new_page()
        if chunk:
            page.insert_textbox(pymupdf.Rect(36, 36, 559, 806), chunk, fontsize=9)
    doc.save(path)
    doc.close()


def _make_encrypted_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(36, 36, 559, 806), "secret content " * 50, fontsize=9)
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret")
    doc.close()


def build_synthetic_archive(root):
    root = Path(root)
    filler = ("The quick brown fox jumps over the lazy dog near the riverbank while "
              "engineers document every observation in meticulous detail. ")
    # A: normal folder, one PDF with rich text (odd filename + uppercase ext), plus a loose PNG
    a = root / "Folder A (2021)"
    a.mkdir(parents=True)
    _make_pdf(a / "Final REPORT v2 (copy).PDF", "Incident summary. " + filler * 40)  # ~4.7k chars
    (a / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\nfakeimagedata")
    # B: two PDFs, one extensionless (magic-byte discovery test)
    b = root / "B_multi"
    b.mkdir()
    _make_pdf(b / "part1.pdf", "Part one of the record. " + filler * 15)
    _make_pdf(b / "part2_no_ext", "Part two continues. " + filler * 15)
    (b / "part2_no_ext").rename(b / "notes")  # no extension at all
    # C: scan-like PDF — pages with no extractable text
    c = root / "C_scanned"
    c.mkdir()
    _make_pdf(c / "old_scan.pdf", "", pages=["", "", ""])
    # D: short text -> low mock confidence -> needs_review
    d = root / "D_short"
    d.mkdir()
    _make_pdf(d / "brief.pdf", "A short memo about the beta category test. " + filler * 3)  # ~450 chars
    # E: file with .pdf extension that is not a PDF
    e = root / "E_fake"
    e.mkdir()
    (e / "scam.pdf").write_text("This is just a text file pretending.", encoding="utf-8")
    # F: encrypted PDF
    f = root / "F_locked"
    f.mkdir()
    _make_encrypted_pdf(f / "locked.pdf")
    # G: PDF with an embedded image + a loose real PNG (images stage test)
    g = root / "G_images"
    g.mkdir()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(36, 36, 559, 400),
                        "Report with figures. " + filler * 8, fontsize=9)
    pm = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 96, 96))
    pm.clear_with(120)
    page.insert_image(pymupdf.Rect(50, 420, 250, 620), stream=pm.tobytes("png"))
    doc.save(g / "figures.pdf")
    doc.close()
    (g / "site_photo.png").write_bytes(pm.tobytes("png"))
    # loose PDF directly in root (folder mode edge case)
    _make_pdf(root / "loose_report.pdf", "Loose report at root level. " + filler * 30)
    # H: deeply nested, id-bearing folders — the layout that breaks naive
    # top-level grouping (every 2003 incident would collapse into one record)
    for inc in ("INC-2231", "INC-2232"):
        h = root / "H_nested" / "2003" / "Q1" / inc
        h.mkdir(parents=True)
        _make_pdf(h / "report.pdf", f"Failure report for {inc}. " + filler * 20)
    # an Office lock file that IS a real PDF — must be excluded by default
    (a / "~$Final REPORT v2 (copy).PDF").write_bytes((a / "Final REPORT v2 (copy).PDF").read_bytes())
    # a byte-identical duplicate inside one record — must be deduped away
    (b / "part1 copy.pdf").write_bytes((b / "part1.pdf").read_bytes())
    # I: an image-only PDF (text rasterised away) — unreadable without OCR
    i_dir = root / "I_scan_ocr"
    i_dir.mkdir()
    src = pymupdf.open()
    page = src.new_page()
    for n, line in enumerate(OCR_FIXTURE_LINES):
        page.insert_text((54, 90 + n * 26), line, fontsize=13)
    pix = page.get_pixmap(dpi=200)
    scan = pymupdf.open()
    sp = scan.new_page(width=page.rect.width, height=page.rect.height)
    sp.insert_image(sp.rect, pixmap=pix)
    scan.save(i_dir / "scanned_memo.pdf")
    scan.close()
    src.close()


def selftest(base_cfg):
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="pdf2db_selftest_"))
    log(f"selftest: working in {tmp}")
    archive = tmp / "archive"
    build_synthetic_archive(archive)
    schema_path = tmp / "schema.json"
    with open(schema_path, "w", encoding="utf-8") as fh:
        json.dump(SELFTEST_SCHEMA, fh)

    failures = []

    def check(cond, label):
        (log(f"  PASS {label}") if cond else failures.append(label))
        if not cond:
            log(f"  FAIL {label}")

    def run_once(outdir):
        cfg = dict(base_cfg)
        cfg.update({"root": str(archive), "schema": str(schema_path), "out": str(outdir),
                    "llm_base_url": "mock", "mock_fail_first_attempt": True,
                    "stages": "discover,extract,images,llm,load", "resume": False, "limit": 0,
                    "seed": 0, "review_sample_rate": 0.25,
                    "llm_workers": 3, "image_min_bytes": 100})
        return run_pipeline(cfg), cfg

    cfg = run_once(tmp / "out1")[1]
    out = tmp / "out1"
    records = {r["record_id"]: r for r in read_csv(out / "records.csv")}
    issues = read_csv(out / "issues.csv")
    reasons = {r["record_id"]: r["review_reasons"] for r in records.values()}

    check(len(records) == 9, f"9 records found (got {sorted(records)})")
    check(records.get("B_multi", {}).get("n_pdfs") == "2",
          "extensionless PDF discovered by magic bytes (B_multi has 2 PDFs)")
    check(records.get("Folder A (2021)", {}).get("status") == "ok"
          or "random_audit" in reasons.get("Folder A (2021)", ""),
          "rich-text record extracted ok (or only random_audit)")
    check(records.get("C_scanned", {}).get("status") == "no_text",
          "scan-like record flagged no_text")
    check(records.get("D_short", {}).get("status") == "needs_review"
          and "low_confidence" in reasons.get("D_short", ""),
          "short record routed to review for low_confidence")
    check(records.get("F_locked", {}).get("status") == "no_text",
          "encrypted-PDF record ends up no_text")
    check(any(i["reason"] == "encrypted" for i in issues), "encrypted PDF logged in issues.csv")
    check(any(i["reason"] == "pdf_extension_but_not_pdf" for i in issues),
          "fake .pdf logged in issues.csv")
    check(any(i["reason"] == "shallower_than_record_unit" for i in issues),
          "loose root PDF warning logged")

    # ---- grouping grammar (pure functions: the whole matrix, no PDFs needed) ----
    nested = "H_nested/2003/Q1/INC-2231/report.pdf"
    grouping = [
        (nested, "pdf", "H_nested/2003/Q1/INC-2231/report"),
        (nested, "folder", "H_nested"),
        (nested, "depth:1", "H_nested"),
        (nested, "depth:3", "H_nested/2003/Q1"),
        (nested, "parent", "H_nested/2003/Q1/INC-2231"),
        (nested, r"regex:(?P<id>INC-\d+)", "INC-2231"),
        (nested, r"regex:(\d{4})", "2003"),          # unnamed group 1
        (nested, r"regex:INC-\d+", "INC-2231"),      # no group at all -> whole match
        ("flat/INC-4444-report.pdf", r"regex:(?P<id>INC-\d+)", "INC-4444"),
        ("top.pdf", "parent", "top"),                # shallower than the unit
        ("top.pdf", "depth:2", "top"),
        ("a/b.pdf", r"regex:(?P<id>ZZZ-\d+)", "a/b"),  # no match -> falls back to the path
    ]
    bad = [(p, u, assign_record_id(p, u)[0]) for p, u, want in grouping
           if assign_record_id(p, u)[0] != want]
    check(not bad, f"record_unit grammar maps every layout correctly (mismatches: {bad})")
    check(assign_record_id("top.pdf", "parent")[1] == "shallower_than_record_unit"
          and assign_record_id("a/b.pdf", r"regex:(?P<id>ZZZ-\d+)")[1] == "no_regex_match",
          "unmatched paths still become records AND carry a warning")
    rejected = []
    for spec in ("nonsense", "depth:0", "depth:x", "regex:", "regex:(unclosed"):
        try:
            parse_record_unit(spec)
        except ValueError:
            rejected.append(spec)
    check(len(rejected) == 5, f"invalid record_unit specs rejected at parse time ({rejected})")

    # ---- discovery filters ----
    inv = read_csv(out / "pdf_inventory.csv")
    check(not any("~$" in r["rel_path"] for r in inv),
          "Office lock file (~$...) excluded by the default exclude_globs")
    kept = {r["rel_path"] for r in inv if r["record_id"] == "B_multi"}
    check(any(i["reason"] == "duplicate_pdf_in_record" for i in issues)
          and "B_multi/part1.pdf" in kept and "B_multi/part1 copy.pdf" not in kept,
          f"byte-identical duplicate deduped away, ORIGINAL kept (got {sorted(kept)})")
    check(records.get("B_multi", {}).get("n_pdfs") == "2",
          "dedupe leaves the record's real PDF count intact")

    # ---- end-to-end with a regex record_unit over the nested tree ----
    cfg_rx = dict(base_cfg)
    cfg_rx.update({"root": str(archive), "schema": str(schema_path),
                   "out": str(tmp / "out_regex"), "llm_base_url": "mock",
                   "stages": "discover,extract", "resume": False,
                   "record_unit": r"regex:(?P<id>INC-\d+)"})
    run_pipeline(cfg_rx)
    rx_ids = {r["record_id"] for r in read_csv(tmp / "out_regex" / "records_text.csv")}
    check({"INC-2231", "INC-2232"} <= rx_ids,
          f"regex grouping splits the nested tree into per-incident records ({sorted(rx_ids)[:4]})")
    cfg_parent = dict(cfg_rx)
    cfg_parent.update({"out": str(tmp / "out_parent"), "record_unit": "parent"})
    run_pipeline(cfg_parent)
    par_ids = {r["record_id"] for r in read_csv(tmp / "out_parent" / "records_text.csv")}
    check("H_nested/2003/Q1/INC-2231" in par_ids,
          f"parent grouping keys on the immediate folder ({sorted(par_ids)[:3]})")
    check(records.get("H_nested", {}) != {} and "H_nested" not in rx_ids,
          "default folder grouping would have collapsed the nested tree — regex does not")

    # ---- OCR (needs the tesseract binary; skipped cleanly when absent) ----
    ocr_cfg = dict(base_cfg)
    ocr_cfg.update({"ocr_mode": "auto"})
    if tessdata_dir(ocr_cfg) is None:
        log("  SKIP OCR checks — no tessdata found (install tesseract to exercise them)")
    else:
        ocr_cfg.update({"root": str(archive), "schema": str(schema_path),
                        "out": str(tmp / "out_ocr"), "llm_base_url": "mock",
                        "stages": "discover,extract,llm,load", "resume": False, "seed": 0})
        run_pipeline(ocr_cfg)
        o_out = tmp / "out_ocr"
        o_recs = {r["record_id"]: r for r in read_csv(o_out / "records.csv")}
        scan_txt = (o_out / "text" / (safe_filename("I_scan_ocr") + ".txt")).read_text(encoding="utf-8")
        hits = sum(1 for line in OCR_FIXTURE_LINES if line.split()[0].lower() in scan_txt.lower())
        check("INC-9001" in scan_txt,
              f"OCR recovered the incident id from an image-only PDF ({scan_txt[:60]!r})")
        check(hits >= len(OCR_FIXTURE_LINES) - 2,
              f"OCR recovered most of the rasterised lines ({hits}/{len(OCR_FIXTURE_LINES)})")
        check(int(o_recs.get("I_scan_ocr", {}).get("ocr_pages") or 0) == 1,
              "ocr_pages recorded on the record")
        check(o_recs.get("I_scan_ocr", {}).get("status") != "no_text",
              "the scan is no longer no_text once OCR is on")
        check("ocr_used" in o_recs.get("I_scan_ocr", {}).get("review_reasons", ""),
              "OCR'd records are routed to human review")
        check(int(o_recs.get("Folder A (2021)", {}).get("ocr_pages") or 0) == 0,
              "auto mode leaves pages that already have a text layer alone")
        # the same archive with OCR off must still report the scan as unreadable
        check(records.get("I_scan_ocr", {}).get("status") == "no_text",
              "with ocr_mode=off the same scan is honestly reported as no_text")

    # ---- config validation fails loudly, before any work ----
    bad_cfgs = [{"ocr_mode": "sometimes"}, {"file_order": "random"},
                {"record_unit": "depth:0"}, {"extract_workers": 0},
                {"truncate_head_frac": 1.5}]
    caught = sum(1 for over in bad_cfgs if validate_cfg({**base_cfg, **over}, SELFTEST_SCHEMA))
    check(caught == len(bad_cfgs), f"invalid config values rejected up front ({caught}/{len(bad_cfgs)})")
    ok_rec = records.get("Folder A (2021)", {})
    check(ok_rec.get("llm_attempts") == "2",
          "repair loop exercised (2 attempts after broken first reply)")
    check(ok_rec.get("category") in SELFTEST_SCHEMA["fields"][1]["options"],
          "enum value valid in records.csv")
    # images stage: embedded image exported + loose PNG catalogued, linked by record_id
    imgs = read_csv(out / "images.csv")
    g_imgs = [i for i in imgs if i["record_id"] == "G_images"]
    check(any(i["source"] == "embedded" and i["file"] for i in g_imgs),
          "embedded PDF image exported and catalogued")
    check(any(i["source"] == "loose" and i["origin"].endswith("site_photo.png")
              for i in g_imgs), "loose PNG catalogued for its record")
    emb = next(i for i in g_imgs if i["source"] == "embedded")
    check((out / emb["file"]).exists() and emb["width"] == "96",
          "exported image file exists with correct dimensions")
    check(records.get("G_images", {}).get("n_images") == str(len(g_imgs)),
          "records.n_images links to images table")
    con = sqlite3.connect(out / "test_records.db")
    try:
        n_db = con.execute('SELECT COUNT(*) FROM "records"').fetchone()[0]
        n_rq = con.execute('SELECT COUNT(*) FROM "review_queue"').fetchone()[0]
        n_im = con.execute('SELECT COUNT(*) FROM "images"').fetchone()[0]
        joined = con.execute(
            'SELECT COUNT(*) FROM "images" i JOIN "records" r '
            'ON i."record_id" = r."record_id"').fetchone()[0]
        fks = {t: [(r[2], r[3], r[4])
                   for r in con.execute(f'PRAGMA foreign_key_list("{t}")')]
               for t in ("images", "pdfs", "review_queue")}
        idx = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        con.close()
    check(n_db == len(records), "SQLite records row count matches CSV")
    check(n_im == len(imgs) and joined == len(imgs),
          "SQLite images table populated and joinable to records")
    check(all(fks[t] == [("records", "record_id", "record_id")]
              for t in ("images", "pdfs", "review_queue")),
          f"images/pdfs/review_queue declare a foreign key to records ({fks})")
    check({"ix_images_record", "ix_pdfs_record", "ix_review_record",
           "ix_records_status"} <= idx,
          f"join and status indexes created ({sorted(idx)})")
    rq_csv = read_csv(out / "review_queue.csv")
    check(n_rq == len(rq_csv) and any(r["record_id"] == "D_short" for r in rq_csv),
          "review queue consistent and contains D_short")

    # record_unit=pdf: a loose image's own path implies a record id that does
    # not exist in that mode, so ownership falls back to the PDFs beside it.
    # The invariant: every image either joins to a real record or is reported.
    pdf_schema_path = tmp / "schema_pdf.json"
    pdf_schema = json.loads(json.dumps(SELFTEST_SCHEMA))
    pdf_schema["record_unit"] = "pdf"
    with open(pdf_schema_path, "w", encoding="utf-8") as fh:
        json.dump(pdf_schema, fh)
    cfg_pdf = dict(base_cfg)
    cfg_pdf.update({"root": str(archive), "schema": str(pdf_schema_path),
                    "out": str(tmp / "out_pdf"), "llm_base_url": "mock",
                    "stages": "discover,extract,images,llm,load", "resume": False,
                    "seed": 0, "image_min_bytes": 100})
    run_pipeline(cfg_pdf)
    p_out = tmp / "out_pdf"
    p_ids = {r["record_id"] for r in read_csv(p_out / "records.csv")}
    p_imgs = read_csv(p_out / "images.csv")
    p_flagged = {i["rel_path"] for i in read_csv(p_out / "issues.csv")
                 if i["reason"] == "image_owner_unresolved"}
    silent = [i for i in p_imgs if i["source"] == "loose"
              and i["record_id"] not in p_ids
              and i["origin"] not in p_flagged]
    check(not silent,
          f"record_unit=pdf: no loose image is silently orphaned ({silent[:2]})")
    check(all(i["record_id"] in p_ids for i in p_imgs if i["source"] == "embedded"),
          "record_unit=pdf: embedded images still link to their PDF's record")

    # determinism: run again into a fresh outdir; compare minus volatile columns
    run_once(tmp / "out2")
    r1 = read_csv(out / "records.csv")
    r2 = read_csv(tmp / "out2" / "records.csv")
    strip = lambda rows: [{k: v for k, v in r.items() if k != "extracted_at"} for r in rows]
    check(strip(r1) == strip(r2), "two runs produce identical records (minus timestamps)")

    # interrupted-run recovery: a torn final jsonl line must not break --resume
    jl = out / "extractions.jsonl"
    with open(jl, "a", encoding="utf-8") as fh:
        fh.write('{"record_id": "torn", "status": "ok", "data"')  # kill -9 mid-write
    cfg_resume = dict(cfg)
    cfg_resume.update({"resume": True, "stages": "llm,load"})
    run_pipeline(cfg_resume)
    rec_r = {r["record_id"]: r for r in read_csv(out / "records.csv")}
    check("torn" not in rec_r and rec_r.get("D_short", {}).get("status") == "needs_review",
          "--resume survives a torn final jsonl line")
    # running the llm stage again WITHOUT --resume must back up prior extractions
    cfg_nores = dict(cfg)
    cfg_nores.update({"resume": False, "stages": "llm,load"})
    run_pipeline(cfg_nores)
    check((out / "extractions.jsonl.bak").exists(),
          "no-resume rerun backs up extractions.jsonl to .bak")
    issues_after = read_csv(out / "issues.csv")
    check(any(i["stage"] == "discover" for i in issues_after),
          "discover-stage issues survive later llm,load-only invocations")

    if failures:
        die(f"selftest FAILED ({len(failures)}): " + "; ".join(failures))
    log(f"selftest PASSED — artifacts kept in {tmp} for inspection")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args(argv):
    p = argparse.ArgumentParser(description="Schema-driven PDF -> SQLite extraction pipeline "
                                            "(air-gap safe; see module docstring).")
    p.add_argument("--root", help="archive root directory")
    p.add_argument("--schema", help="schema JSON file")
    p.add_argument("--out", help="output directory")
    p.add_argument("--llm-base-url", dest="llm_base_url",
                   help='OpenAI-compatible base URL ending in /v1, or "mock"')
    p.add_argument("--llm-model", dest="llm_model", help="model name at the endpoint")
    p.add_argument("--stages", help="comma list from: discover,extract,images,llm,load")
    p.add_argument("--record-unit", dest="record_unit",
                   help="override the schema's grouping: pdf | folder | parent | depth:N | "
                        "regex:PATTERN (e.g. 'regex:(?P<id>INC-\\d+)')")
    p.add_argument("--include", dest="include_globs",
                   help="';'-separated fnmatch patterns a path must match to be read")
    p.add_argument("--exclude", dest="exclude_globs",
                   help="';'-separated fnmatch patterns to skip (default drops lock files/dotfiles)")
    p.add_argument("--ocr", dest="ocr_mode", choices=OCR_MODES,
                   help="off (default) | auto (scanned pages only) | augment (add scanned "
                        "figures to digital pages) | force (every page). Needs the Tesseract "
                        "binary + tessdata provisioned; no vision model is involved")
    p.add_argument("--ocr-language", dest="ocr_language",
                   help='tesseract language code(s), e.g. "eng" or "eng+ara"')
    p.add_argument("--resume", action="store_true", default=None,
                   help="keep prior extractions.jsonl; skip records already ok")
    p.add_argument("--limit", type=int, help="send at most N records to the LLM (smoke run)")
    p.add_argument("--seed", type=int, help="seed for the audit-sample hash")
    p.add_argument("--config", help="JSON file overriding CONFIG defaults")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override any CONFIG key (repeatable), e.g. --set llm_timeout_s=300")
    p.add_argument("--selftest", action="store_true",
                   help="run the offline end-to-end selftest and exit")
    return p.parse_args(argv)


def build_cfg(args):
    cfg = dict(CONFIG)
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            overrides = json.load(fh)
        bad = set(overrides) - set(cfg)
        if bad:
            die(f"--config has unknown keys: {sorted(bad)}")
        cfg.update(overrides)
    for kv in args.set:
        if "=" not in kv:
            die(f"--set expects KEY=VALUE, got {kv!r}")
        k, v = kv.split("=", 1)
        if k not in cfg:
            die(f"--set: unknown CONFIG key {k!r}")
        default = CONFIG[k]
        if isinstance(default, bool):
            cfg[k] = v.strip().lower() in ("1", "true", "yes")
        elif isinstance(default, int):
            cfg[k] = int(v)
        elif isinstance(default, float):
            cfg[k] = float(v)
        else:
            cfg[k] = v
    for k in ("root", "schema", "out", "llm_base_url", "llm_model", "stages",
              "record_unit", "include_globs", "exclude_globs", "ocr_mode", "ocr_language"):
        v = getattr(args, k, None)
        if v is not None:
            cfg[k] = v
    if args.resume is not None:
        cfg["resume"] = args.resume
    if args.limit is not None:
        cfg["limit"] = args.limit
    if args.seed is not None:
        cfg["seed"] = args.seed
    return cfg


def main(argv=None):
    args = parse_args(argv)
    cfg = build_cfg(args)
    if args.selftest:
        selftest(cfg)
        return
    if not cfg["root"] or not cfg["schema"]:
        die("--root and --schema are required (or use --selftest)")
    run_pipeline(cfg)


if __name__ == "__main__":
    main()
