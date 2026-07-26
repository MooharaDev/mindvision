# Mindvision — Material Failure Classification (Saudi Aramco)

## What this project is
A computer-vision project classifying material failure types from a 50-year
incident archive. Incident folders (INC001, INC002, ...) each contain a PDF
failure report plus PNG/JPEG images (some embedded in the PDF, some loose).
The data (provided by Material Consulting Services) mixes TWO modalities
inside each incident: regular field/equipment photos AND lab micrographs at
multiple magnifications. Design consequence: track a `modality` flag in the
manifest, report model metrics per modality, and watch for the
modality-class shortcut (a class that is always/never micrograph-backed).
Scope per management directive: the 5 most common failure classes + "other".
Root-cause prediction is deferred to a later phase.

## THE AIR GAP — the most important constraint in this repo
All deliverable code is authored HERE (personal laptop) but EXECUTED inside
Aramco's isolated network. Code goes in through approved channels; nothing
comes out. Therefore every deliverable script MUST:

- Run fully offline. NO external API calls of any kind at runtime — no
  Anthropic API, no OpenAI, no Roboflow API, no HuggingFace Hub downloads,
  no `pip install` at runtime, no telemetry, no URL fetches.
- Use only: Python stdlib + common approved packages (pandas, numpy,
  torch/torchvision, scikit-learn, Pillow, opencv-python, pymupdf/pdfplumber,
  matplotlib, tqdm). Beyond that: individual packages CAN be provisioned
  from PyPI through Aramco's internal channel (NOT runtime pip — packages
  are staged ahead of time). Any extra package must be pinned in
  requirements.txt AND called out in README_TRANSFER.md as "provision
  internally before running", so Mohammed can request it. Keep extras few.
- Internal web hosting exists: Flask is available and approved for
  internal-facing tools (viewer/data-entry/dashboard). Web deliverables
  still follow air-gap rules: serve on the intranet only, zero outbound
  requests, ALL static assets (JS/CSS/fonts) vendored locally — never CDN
  links or Google Fonts.
- Transfer mechanism: finished code goes in as a ZIP through EFT (secure
  file-transfer channel), hard limit 3 GB per package. Package contents =
  deliverable code + pinned requirements + pretrained weights files (no
  downloads exist inside; weights are the one non-code artifact that MUST
  ride along). Never include the public stand-in data — selftests fabricate
  their own tiny synthetic input at runtime.
- Assume pretrained weights are provided as LOCAL FILES (e.g.
  `weights/resnet50.pth` loaded via `torch.load` +
  `model.load_state_dict`), never `weights=ResNet50_Weights.DEFAULT`
  or any auto-download path. Emit a one-line comment noting which weights
  file must be transferred alongside the script.
- Take all paths/config via a CONFIG block at the top of the file or CLI
  args — never hardcode personal-laptop paths.
- Prefer few flat files over package structure; each script runnable
  standalone with `python script.py`. Transfer-friendly = fewer files.

If a task can't be done air-gap-safe, say so explicitly instead of quietly
adding an online dependency.

## Two lanes of work — never mix them
1. **Deliverable lane** (`deliverable/`): code destined for Aramco internal
   systems. Air-gap rules above apply absolutely. Test locally with synthetic
   or public stand-in data structured like the real archive.
2. **Prototype lane** (`prototype/`): personal-laptop experiments with PUBLIC
   data only (NEU Surface Defect DB, Severstal Kaggle). Roboflow MCP may be
   used here. NEVER place real Aramco data, filenames, incident numbers,
   report text, or images in this lane or in Roboflow. If asked to, refuse
   and remind why.

## Coding style
- Minimal, flat, readable. No unnecessary classes/abstraction layers.
- Fail loudly: no bare except, no silent skips — log every skipped/failed
  file to a report the user can review.
- Every pipeline stage writes an auditable artifact (CSV/JSON), because
  debugging happens inside the air gap where Claude isn't available.
- Deterministic where possible: seed everything, sort directory listings.

## Fixed architectural decisions (do not relitigate)
- Train/test split by `incident_id`, never by image (leakage).
- Labels live at incident level, inherited by images; images flagged
  `usable` in the manifest are the training set.
- Model: ResNet-50 transfer learning; output = ranked class probabilities
  with confidence, aggregated to incident level (mean softmax).
- Classes: top-5 by empirical frequency from report extraction + "other".
- Metrics: per-class recall + confusion matrix; never headline accuracy
  alone (corrosion will dominate the class balance).
- Era/decade is a confounder (old grainy photos correlate with era-specific
  failure modes) — augmentation simulates degradation; validate distribution
  shift across decades.
