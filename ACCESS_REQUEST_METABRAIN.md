# Access request — Metabrain API endpoint (Corpus / PDF-to-database pipeline)

Draft. Replace every `[BRACKET]` before sending. Section 1 is the email/ticket
body; sections 2–4 are the technical annex — paste them in, or attach this file.

---

## 1. Email / ticket body

**To:** [Metabrain team distribution list / ITSM queue]
**Cc:** [Line manager], [Material Consulting Services sponsor]
**Subject:** API access request — Metabrain endpoint for material-failure report extraction (MCS)

Hello,

I am requesting programmatic access to the Metabrain API for a document
extraction pipeline being built for Material Consulting Services.

**What the work is.** MCS holds roughly 50 years of material-failure incident
reports as PDFs. We are turning that archive into a queryable database — one
row per incident, with the failure classification, dates, equipment and
material fields read out of the report text — so it can support failure-trend
analysis and, in a later phase, train a vision model to classify failures from
inspection imagery. The database build is the prerequisite for everything else.

**Why an LLM and not parsing rules.** The reports span five decades and have no
consistent template. Pattern matching on that corpus produces unusable labels.
Sending the report text to a model that returns a structured JSON record is the
only approach that survives the format drift, and every extracted value is
returned with the verbatim sentence it came from, so a human reviewer can
verify it against the source.

**Why Metabrain specifically.** The reports are internal Aramco technical
documents and cannot leave the network. Metabrain is the approved internal
option, so the tool was built against it: the pipeline is fully offline apart
from a single call to the endpoint we configure, and it has no other network
access of any kind — no external APIs, no downloads, no telemetry, no runtime
package installation.

**What I am asking for:**

1. A service account / API key for the Metabrain OpenAI-compatible API, usable
   from [SERVER HOSTNAME OR SUBNET] where the pipeline runs.
2. The endpoint base URL and the model name to specify.
3. Confirmation of the rate and concurrency limits on that account.
4. The answers in section 4 below, which determine how the tool must be
   configured before the first production run.

**Scale of use:** approximately [N] documents in the full archive, one request
per document, run as a small number of batch jobs rather than continuous
traffic. See section 3 for the concurrency profile. I would like to start with
a 10-document smoke test on a non-production key if that is easier to approve.

**Timeline:** the code is complete and tested against a mock endpoint; the
Metabrain call is the last unresolved dependency. Target for the first
production run is [DATE].

Happy to walk through the tool or run it in front of your team if that helps
the review.

Thank you,
[NAME]
[TITLE], [DEPARTMENT]
[BADGE / ID] · [PHONE] · [EMAIL]

---

## 1a. Business-case details (the intake fields Metabrain asked for)

Suggested answers below; every figure marked `[CONFIRM]` needs a real number
from you or the MCS sponsor before this goes out. Keep the arithmetic in the
submission — reviewers trust a visible formula over a bare claim.

**Use case name**
> Corpus — automated extraction of the Material Consulting Services (MCS)
> material-failure incident archive into a queryable engineering database.

**Task that the use case is providing**
> Converting unstructured PDF failure-investigation reports (~50 years of MCS
> incident records, no consistent template) into one structured database row
> per incident — failure mechanism, equipment, location, dates, costs — each
> value paired with the verbatim source sentence for human verification.
> Low-confidence records are routed to a human review queue. The database
> serves (a) day-to-day failure-history lookups by engineers, (b) failure-trend
> analysis across decades, and (c) the labelled training set for the planned
> phase-2 vision model on inspection imagery.

**Deployment date**
> The tool is already transferred through EFT and installed; the Metabrain key
> is the last dependency. Deployment: within [2 weeks — CONFIRM] of key
> issuance (smoke test of 10 documents on day 1, full archive backfill the
> same week, console available to engineers immediately after).

**Frequency of the task — before**
> Failure-history inquiries are answered manually today: [15/month — CONFIRM
> with MCS] archive searches, each meaning locating and reading the relevant
> PDF reports by hand. Full-archive extraction (the backfill this key enables)
> has effectively **never been performed** — at manual effort it is
> [N_REPORTS] × ~30 min ≈ [X,000] engineer-hours, which is why the archive is
> unqueryable today.

**Frequency of the task — after**
> The same [15/month] inquiries served from the database, plus newly issued
> reports extracted on arrival ([~10/month — CONFIRM]). The one-time backfill
> of [N_REPORTS — run `--stages discover` for the exact count] documents runs
> as a single batch (~1 request per document).

**Time spent on the task — before**
> [2–4 hours — CONFIRM] per inquiry (find the right incident folders, read
> several reports, transcribe values into a spreadsheet). Backfill equivalent:
> ~30 min of engineer reading + transcription per report.

**Time spent on the task — after**
> [5–10 minutes] per inquiry (query the console or SQL, check the cited
> evidence sentence against the source where it matters). Per new report:
> ~seconds of pipeline time, plus ~3 min human review for the subset
> (~15–20%) that lands in the review queue.

**Estimated cost savings** (show the formula, plug confirmed numbers)
> One-time backfill: [N_REPORTS] × 0.5 h avoided, minus review-queue time
> ([N_REPORTS] × 18% × 3 min) and operator time (~[40 h]).
> For example, at N = 10,000: 5,000 h avoided − ~90 h review − 40 h operator
> ≈ **4,870 engineer-hours one-time**.
> Recurring: [15 inquiries/month] × ([3 h] − [10 min]) ≈ **~42 engineer-hours
> per month**, plus [10 new reports/month] × ~25 min ≈ 4 h/month.
> Convert to SAR with the loaded engineer rate your finance contact prefers
> [RATE — CONFIRM]; at SAR [300]/h the backfill alone is ≈ SAR [1.46M].

**Expected number of users**
> [25–50 — CONFIRM] total: 2–3 operators (MCS) who define schemas and run
> extractions, 3–5 reviewers working the review queue, and the wider MCS /
> reliability-engineering audience consuming the tables and downloads
> read-only through the intranet console.

Note the honest asymmetry that makes this case strong: the *API usage* is a
short batch plus a trickle (Section 3), while the *value* accrues on every
subsequent query that never touches Metabrain — the key funds a one-time
conversion of 50 years of paper into an asset, not an ongoing per-query cost.

---

## 2. What the tool is and how it calls the API

`Corpus` — a self-contained Python tool (one pipeline engine plus an optional
Flask web console for reviewers). It runs entirely inside the network on
[SERVER / WORKSTATION], reads a folder of PDFs, and writes a SQLite database
plus CSV exports.

The Metabrain call is one stage of five. For each document the pipeline sends:

- the extracted text of that document (truncated at 80,000 characters), and
- a schema describing the fields to return,

and expects one JSON object back per document. It is a plain text-in / text-out
chat completion — no streaming, no tool use, no function calling, no
multimodal/vision input, no fine-tuning, no embeddings, no persistent
conversation state. Each request is independent and stateless.

**Endpoints used** (OpenAI-compatible paths, relative to the configured base URL):

| Path | Method | When |
|---|---|---|
| `/chat/completions` | POST | once per document, plus up to 2 retries if the reply is not valid JSON |
| `/models` | GET | only when an operator clicks "Test connection" in the console |

Nothing else is called. If Metabrain's paths differ from the OpenAI convention,
tell me and I will adapt the client — it is one function.

**Security posture, for the reviewer:**

- Zero outbound internet traffic. The Metabrain endpoint is the only network
  destination the tool ever contacts.
- Ambient `http_proxy` / `HTTPS_PROXY` environment variables are deliberately
  ignored, so corporate proxy settings cannot silently re-route the traffic.
- The API key is read from an environment variable, or stored server-side in a
  configuration file with owner-only (0600) permissions, masked in the UI, and
  redacted from every run summary and log file the tool writes.
- Prompts and raw responses are written to local audit files under the output
  directory so that any extraction can be traced back to what was actually sent
  and returned. These stay on the local disk.
- Private CA bundles are supported for HTTPS endpoints.

---

## 3. Expected load

| | |
|---|---|
| Documents in the full archive | [N] (currently estimated; being counted) |
| Requests per document | 1, plus up to 2 retries on invalid JSON (rare in testing) |
| Input size per request | typically [X]k characters, hard-capped at 80,000 characters |
| Output size per request | small — one JSON object, on the order of 500–2,000 tokens |
| Default parallelism | 4 concurrent requests; configurable 1–16 |
| Pattern | batch runs, resumable; an interrupted run restarts without re-sending completed documents |
| Estimated duration | at 2 s/document and 4 concurrent workers, 20,000 documents ≈ 3 hours |

The pipeline can be pinned to strictly sequential requests with politeness
delays if that suits the platform better — please tell us the concurrency you
want us to stay under and we will configure to it rather than discover the
limit by hitting it.

---

## 4. Questions we need answered before the first run

1. **Base URL** — the exact URL to configure, including any version path.
2. **Model name** — which model to specify, and whether the identifier is
   expected to change (so we can pin it in our run records for reproducibility).
3. **Authentication** — static API key in an `Authorization: Bearer` header, or
   a token that must be refreshed? If refresh is required, what is the flow and
   the token lifetime? *(This determines whether unattended overnight batch runs
   are possible.)*
4. **Rate and concurrency limits** — requests per minute and maximum concurrent
   requests for this account. Does the endpoint return HTTP 429 with a
   `Retry-After` header? *(The client already honours it if so.)*
5. **Context window and maximum output tokens** — so we can set the truncation
   limit correctly rather than relying on our 80,000-character default.
6. **Structured output** — is `response_format: {"type": "json_object"}`
   supported? If not, we fall back to prompt-enforced JSON with a validation
   and repair loop, which works but costs extra calls.
7. **TLS** — is the endpoint HTTPS with a private CA? If so, where is the CA
   bundle on internal machines?
8. **Network path** — is the endpoint reachable directly from
   [SERVER HOSTNAME / SUBNET], or is a firewall rule or proxy exception needed?
9. **Data handling** — are prompts and responses retained or logged on the
   platform side, and for how long? Any classification ceiling on what may be
   sent? The content is internal Aramco technical reports classified
   [CLASSIFICATION LEVEL]; please confirm that is in scope for this endpoint.
10. **Quota / chargeback** — is there a token budget or cost centre to charge?
    Ours is [COST CENTRE].
11. **Support** — the right channel to raise an issue during a long-running
    batch job.

---

## 5. Notes to self before sending (delete this section)

- Fill in the archive count `[N]` — the pipeline's `--stages discover` run gives
  the exact number in seconds; do that first so the request carries a real
  figure instead of an estimate.
- Confirm the report classification level with the MCS sponsor before question 9
  goes out.
- Decide the host: personal-issue workstation vs a proper internal server. Ask
  for access from the server if there is any chance the batch runs move there,
  so a second request is not needed.
- If Metabrain has a self-service onboarding portal or a standard intake form,
  use that instead of a free-text email and paste sections 2–4 into it.
