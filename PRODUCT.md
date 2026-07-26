# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: a broad engineering audience inside an air-gapped corporate network
(Saudi Aramco) who **browse and consume extraction results** — tables, record
details, downloads. Secondary: a small number of operators (Material
Consulting Services / Mohammed) who define schemas and launch processing runs.
Tertiary: reviewers who approve or correct low-confidence records
(human-verified data-entry loop). Confirmed by user 2026-07-24.

## Product Purpose

Turn any folder tree of PDF documents into a structured, queryable database —
the archive comes first, and the app drafts the schema from the documents
themselves before a single field is defined by hand. An internal LLM fills one
row per record. Success = engineers trust and use the resulting tables
(records + evidence + review status) instead of reading PDFs by hand, and the
tables are ready to train on.

## Positioning

Schema-driven and fully generic (any document domain), fully air-gapped
(zero external requests, self-contained single-file deployables), and
audit-first: every skipped file, raw LLM reply, and confidence score is
inspectable. The differentiator is that nobody has to know their schema up
front: point it at an archive, see what is in there, and edit a draft rather
than face a blank table. Neighboring tools (cloud ETL/OCR SaaS) cannot run
inside this network at all.

## Operating Context

- Deployed on an intranet host inside an isolated network; reached by browser.
- Code arrives via secure file transfer (EFT, ZIP ≤3 GB); no runtime internet.
- LLM = internal OpenAI-compatible gateway; "mock" backend for offline tests.
- Pipelines also run headless via the CLI (`pdf2db.py`) for huge archives.
- Debugging happens without external help — artifacts must self-explain.

## Capabilities and Constraints

- Flask + stdlib + pymupdf only; single-file app, inline CSS/JS/SVG.
- No CDN, no external fonts (system font stack), no telemetry. Hard rule.
- No auth layer (trusted intranet); destructive actions need confirmation.
- Table/schema vocabulary: record, field, run, review queue, evidence,
  confidence, mock vs real endpoint.
- Undecided: GPU availability inside; final top-5 class list (placeholder).

## Brand Commitments

Neutral tool identity — deliberately not branded as corporate/official.
Named **Corpus** (2026-07-24, user asked for a neutral product name rather
than the script name "pdf2db console"); the vocabulary follows from it:
corpus → records → fields. The engine keeps the name `pdf2db.py`.

## Evidence on Hand

Real test: 3 corrosion incident reports (INC-2231) extracted correctly via
Groq llama-3.3-70b (all fields, verbatim evidence quotes). Synthetic scale
test: 102 PDFs nested 7 deep → 42 records, 400-page PDF truncated + routed
to review. No customer quotes/benchmarks exist — do not fabricate any.

## Product Principles

1. Trust through transparency: every value traceable to evidence and status.
2. Generic before specific: nothing hardcoded to one schema or domain.
3. Air-gap absolutism: self-contained beats convenient, always.
4. Read-first: browsing results is the main job; operating runs is secondary.
5. Fail loudly, recover gracefully: errors are visible, retryable, auditable.

## Accessibility & Inclusion

Internal requirement inferred (not user-stated): keyboard operability,
WCAG AA contrast, reduced-motion support — engineers on varied hardware.
