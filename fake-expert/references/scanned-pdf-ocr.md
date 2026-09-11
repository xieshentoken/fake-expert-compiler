# Scanned-PDF OCR adapter contract v0.14.0-scan-pdf

This reference defines the current v0.14.0-scan-pdf source-candidate intake boundary.
It extends the existing
native-text PDF IR; it does not replace `pypdf==6.10.0`, native evidence offsets, or
the existing semantic-promotion and publication gates.

## Phase 7D.3 visual policy and body-OCR separation

Visual selection is a separate policy from the scanned-PDF text route. New visual
jobs use `visual-mode=auto`; `off` records `not-inspected-by-policy` and performs
no Census/render/PP-Structure/chart call, but it does not disable the ordinary
PP-OCRv6 `pages_needing_ocr` route when that backend is explicitly selected.
`auto` runs the no-model/no-render Census and sends only candidate page/crop
regions to a heavy route; `full` routes the selected visual scope. The closed
visual kinds are `table`, `figure`, `chart`, `diagram`, and `equation`. Budgets
pause with `paused-budget-exhausted` and a coverage gap; they never silently
skip a page. The default `visual-context=index` is source-free and excludes raw
image/crop bytes, complete OCR, absolute source paths, credentials, and complete
worker responses. Read
`phase-7d3-selective-visual-pipeline-implementation-outline.md` for the visual
sidecar/DAG contract. None of these visual policy states changes native-text-first
OCR provenance or promotes an OCR transcript.

When a visual route selects raster regions, the orchestrator renders and binds
all requested crops before making one bounded worker-session call for that
backend/profile. The session accepts only a finite fixed-JSONL crop request list:
an empty list makes zero worker/model calls, the model is constructed at most
once, ordinary item errors do not discard other results, and the receipt reports
the actual `model_load_calls` value. A parent hard total timeout can terminate
the isolated worker; the worker applies a hard per-request timeout and pauses
remaining work after an unsafe timeout. Each request and response is bound to
the frozen runtime inventory/contract hash. PDF objects, PDF paths, source paths,
and source bytes are rejected at the boundary.

The optional chart route is `paddleocr-chart-parsing` using the official local
`paddleocr.ChartParsing` API with `PP-Chart2Table` and no default model resolver.
The adapter accepts only the observed `{result: <pipe-delimited text>}` shape,
skips Markdown separator rows, emits strict numeric value candidates, and keeps
ordinary cells as text candidates. Axis/tick/legend/series/unit/annotation
fields are populated only when explicitly labelled; otherwise a gap and review
route is retained. Missing model/runtime is fail-closed before rendering, while
post-render worker/shape/empty failures use their corresponding paused chart
statuses. No chart quality claim follows from this protocol.

## Phase 7D.4 runtime qualification before heavy visual routes

New v0.14 PP-StructureV3 and Chart2Table visual routes require a matching
host-local `tkc.paddle-runtime-qualification/v0.1` receipt before render or
inference. The receipt binds the exact runtime/model/profile/config/worker and
three repeated crop-only requests in one short-lived process. A missing,
rejected, stale, warning-blocked, or differently bound receipt pauses as
`paused-runtime-unqualified`. It is availability/repeatability evidence only,
never Gold or accuracy.

`technical-chart-v2` selects the direct `PP-Chart2Table` Paddle model with CPU,
`enable_hpi=false`, `engine=paddle_dynamic`, and batch size 1. The v0.13
`technical-chart-v1` profile remains read-only compatibility input. The
safetensors route is not selected. Read
`phase-7d4-runtime-qualification-implementation-outline.md` for commands,
resource policy, warning blockers, and the DAG closure.

The scan MVP also permits a separate `paddleocr-ppocrv6` text profile. It is limited
to direct crop/page-image requests and exactly three serial repeats in one
short-lived local process. A qualified receipt must bind the runtime, model,
configuration, worker, input/output/response hashes, `model_load_calls=1`, and
false network/download/MCP flags. Missing models, warning drift, output-hash loss,
tool drift, or any policy mismatch fails closed. This proves bounded local
repeatability only; it is not CER, recognition accuracy, Gold, or semantic
completeness. The existing rejected Chart2Table receipt stays rejected and is not
the body-text OCR gate.

## Phase 7D.6C spread and checkpoint contracts

For a physical PDF page that visibly contains a rotated two-printed-page spread,
the compiler requires an explicit source-candidate
`tkc.scanned-pdf-spread-split/v0.1` contract. The physical PDF page remains the
canonical source page; a printed-page label is only a candidate or `null` until a
human confirms it. Orientation is never auto-detected. The selected content
rotation must be exactly one of clockwise `0`, `90`, `180`, or `270`, and the
contract binds the original render, rotated render, dimensions, pixel boundary,
region bboxes, fixed reading order, and per-region crop hashes/dimensions.

The split contract also binds the original and rotated image coordinate spaces to
`pdf-page-top-left-points-v1` through a reversible transform, plus source,
runtime/model/configuration/worker and qualification commitments. It rejects odd
or even dimension inconsistencies, out-of-bounds regions, overlap, gaps,
duplicate region IDs/orders, reading-order drift, label-policy violations,
transform/hash tampering, and runtime/configuration drift before inference. For
the Patankar scan fixture, CW90 followed by two left-to-right regions is a
candidate preprocessing choice; it is not a structure or accuracy claim.

The companion `tkc.scanned-pdf-checkpoint/v0.1` contract is page/region scoped.
Only a complete region with a matching binding may be reused. A failed or missing
region stays paused; resume revalidates source/render/split/rotation/tool/model/
config/runtime/worker/qualification hashes and the canonical region order, then
runs only missing regions. No resume path runs review, Gold, promotion, sealing,
signing, execution, or release. The split-bound runtime qualification identities
are separate from the legacy qualification schemas so an old receipt cannot
authorize a changed split/rotation route.

The same canonical-order rule applies to a multi-page full-page OCR route. Every
generated full-page raster region must carry one stable global `region_order`, and
that value must be copied unchanged into the raster binding and checkpoint row.
Missing, duplicated, page-local-reset, or reordered values fail verification; a
caller may not sort by region ID or hand-edit a checkpoint to recover. This is an
integrity/replay invariant only and does not make the OCR observations accurate,
reviewed, Gold, promotable, or executable.

## Phase 7D.1 external Paddle runtime and raster contract

The v0.14.0 route keeps the native-first policy and the explicit
`paddle-runtime-contract-v0.2` for the externally installed Paddle environment.
The compiler venv is never treated as the Paddle environment. A job must freeze
the raw external interpreter launcher, resolved runtime root, worker path and
SHA-256, exact distribution inventory, allowlisted model root, per-file model
manifest/identity hashes, effective configuration, backend ID, and network/MCP
policy. Runtime/model/worker paths are resolved inside explicit allowlists;
symlink and path escapes, input/output overlap, unknown versions, network or
download flags, and receipt drift fail closed.

The backend identities are intentionally separate: `paddleocr-ppocrv6` means
PP-OCRv6 text detection/recognition only; `paddleocr-ppstructure-v3` means the
structured layout/table/cell route. PP-OCR text lines cannot be used as a
PP-Structure table grid. The retained v0.11.0 workpack recorded a
`not-run-missing-models` pause; a host's local inventory may contain
the explicitly bound PP-StructureV3 components and was exercised on one
authorized crop. No download is attempted.

For an explicit raster region, the canonical page render is bound by SHA-256 and
the PDF bbox is checked against MediaBox/CropBox and rotation. The compiler emits
a deterministic crop from the render, binds the expected crop SHA-256, records a
reversible PDF-page ↔ render-pixel ↔ crop-local affine transform, and invokes a
fixed JSONL worker with only the crop. The worker never receives the source PDF.
Temporary crops, worker responses, transcripts, model receipts, real book paths,
and review workpacks stay under the private `tmp/workspace/output/phase7d2`
boundary and are not release inputs.

## Phase 7D.2 first-class visual result route (preserved by v0.14.0)

The `technical-table-v1` profile is explicit at both PP-StructureV3 constructor
and predict layers. It binds `PP-OCRv6_medium_det/rec`,
`PP-DocLayout_plus-L`, `PP-LCNet_x1_0_table_cls`, wired `SLANeXt_wired` plus
`RT-DETR-L_wired_table_cell_det`, and wireless `SLANet_plus` plus
`RT-DETR-L_wireless_table_cell_det`. Table recognition is enabled; chart,
formula, seal, document orientation, unwarping, text-line orientation, table
orientation, and e2e table flags are explicitly disabled. Missing or unbound
component directories/files pause with `not-run-missing-models`.
PaddleX 3.7 model names are also bound explicitly (`PP-OCRv6_medium_det/rec`
and the matching layout/table/cell names); this prevents the PP-StructureV3
default `PP-OCRv5_server_det` resolver from silently replacing the selected
PP-OCRv6 detector.

The PP-StructureV3 3.7 adapter accepts only the observed official result shape:
`parsing_res_list`, `layout_det_res`, `overall_ocr_res`, `table_res_list`,
`pred_html`, `cell_box_list`, and `table_ocr_pred`. It serializes result objects
and NumPy-like arrays through an allowlisted JSON/mapping conversion, records
raw and normalized result hashes, and rejects unknown shapes. HTML topology is
joined to cell boxes and OCR by deterministic order/geometry. Row/column spans,
overlap, gap, outside-table bbox, count mismatch, and HTML/OCR text disagreement
become explicit gap/conflict rows; no exact cell is fabricated.
Worker exceptions are returned as stage-scoped stable codes (`constructor`,
`predict`, or `result_adapter`) with exception detail stripped. A non-zero
child-process exit is exactly `worker_process_failed`; stderr is never appended
to the canonical/public exception. The original failure receipt remains in the
private workpack.

Every layout/table candidate has a canonical source/page/render/crop/parser/backend/
model/config/runtime/worker/request/response receipt chain. Its canonical ID is
recomputed from mapped page-space geometry and path-independent stable request /
response-content commitments; adapter-local IDs remain attribution only. Table
grid, cell, source-anchor, binding, conflict, overlay, and DAG references are
rekeyed atomically after page mapping. OCR `kind=text`
remains transcript/coverage evidence. Figure/table/chart/equation/annotation
objects are candidate-only, `promotion=false`, `verified_gold=false`, and route
to one-reviewer localization or two-reviewer value/formula review as applicable.
The visual review overlay is routing metadata, not an attestation or Gold record.

The Phase 7B projection includes runtime-result, visual-candidate, visual-gap,
visual-conflict, table-cell, and review-overlay nodes. A changed raw/normalized
result, model/config/runtime/worker/crop receipt or cell topology invalidates the
complete re-extract/re-review/rebuild/recompose/repackage closure; exact hashes
remain reusable.

## Native-first routing

Run the ordinary pypdf preflight first. Only physical pages in
`preflight.page_routing.ocr_required_pages` may enter the OCR worker. Usable native
pages stay native and are not sent through OCR. A blank or unusable page with no
image route remains `manual-review-required`; it is not silently OCR'd or dropped.
Mixed PDFs therefore keep one authoritative native path for native pages and one
candidate OCR path for scan pages.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/scanned_pdf_ocr.py build \
  /absolute/path/to/authorized-source.pdf \
  --output /absolute/path/to/empty-scanned-ir \
  --backend paddleocr-ppocrv6 \
  --model-root /absolute/path/to/preinstalled/local-model-root \
  --pdftoppm /absolute/path/to/pdftoppm
```

The worker receives temporary page renders, never the source PDF. It communicates
over a fixed JSONL protocol in an isolated subprocess. Ambient credentials and
network authority are not passed; common model hubs are forced offline. A selected
backend that is unavailable is an error. There is no silent PaddleOCR → Docling →
Tesseract fallback.

## Explicit backends

| ID | Role | Boundary |
| --- | --- | --- |
| `paddleocr-ppocrv6` | primary text route | PP-OCRv6 local text detection/recognition; emits transcript/coverage candidates, not table topology. Requires the cached text model directories and explicit external runtime contract for raster jobs. |
| `paddleocr-ppstructure-v3` | structured candidate route | PP-StructureV3 layout/table/cell normalization into `tkc.table-grid/v0.1`; requires all local layout/table/cell model directories. Missing assets are not-run/paused. |
| `paddleocr-ppstructure` | compatibility alias | Legacy structured-backend spelling accepted only as an explicit route; it never means PP-OCRv6 text recognition. |
| `docling` | challenger | Local structure, reading-order, table, and formula candidates for comparison; it never becomes canonical by itself. |
| `pymupdf-tesseract` | optional baseline | Local PyMuPDF/Tesseract comparison only; it is never an implicit fallback. |
| `fake` | test-only | Synthetic observations for contract, tamper, routing, and negative tests; it is not an OCR accuracy result. |

The release contains the adapter contract and worker dispatchers, not model files.
Real PaddleOCR/Docling A/B accuracy and target-machine performance remain an
explicit unfinished gate until authorized local models are installed and measured.
An actual Paddle run additionally requires backend configuration with explicit
local `model_dirs`; an actual Docling run requires a local `model_artifacts_path`.
Missing local model paths fail closed so a library constructor cannot fetch a model.

## Receipt chain

Every observation must bind all of the following:

- complete source SHA-256 and physical page number;
- canonical `pdftoppm` render SHA-256, renderer version, DPI, and PNG format;
- backend ID, engine/version, JSONL protocol version, and subprocess receipt;
- model ID plus every local model file's relative path, byte count, and SHA-256;
- complete effective configuration and configuration SHA-256;
- image-space polygon/bbox and PDF-space polygon/bbox;
- confidence, raw transcript, normalized transcript, and a Unicode-code-point span;
- a coordinate transform from preprocessed image pixels to
  `pdf-page-top-left-points-v1`, including preprocessing operations, forward and
  inverse 3×3 matrix, and round-trip tolerance.

The scan IR stores observations, transcript spans, backend/model/config receipts,
external runtime/worker receipts, raster crop and coordinate-transform receipts,
table grids/cell anchors where a real structured result exists, an engine
disagreement/duplicate ledger, and page quality routing. `source.json` hash-locks
every scan component and `scan-ir.json` binds the component hashes to one input
fingerprint. Changing source bytes, render version or DPI, model files, worker,
runtime inventory, configuration, crop, or transform invalidates the scan fingerprint.

## Bounded reviewed scan structure

OCR output may propose title and segment candidates, but it cannot resolve a
structure on its own. A bounded review must explicitly confirm the title, physical
page range, reading order, bbox, and evidence binding for every requested segment.
The attestation binds the candidate/observation/transcript span, source/render/
transcript/model/config hashes, coordinate transform scope, reviewer/session, and
review checks. Blank, ambiguous, overlapping, or stale proposals pause or reject.
An accepted review emits a `resolved` bounded structure resolution while the OCR
observation and reviewed scan locator remain `candidate_only`/`promotable=false`
until the existing independent semantic review, support matrix, coverage gate, and
promotion path pass. This route does not claim whole-book structure or author Gold.

## Phase 7D.6C-D Portable Scan Gold Review coverage repair

The next non-conflicting source-candidate line is
`0.14.7-scan-gold-coverage`; it does not change `RELEASE_VERSION` or the formal
v0.14.0 package. The split geometry repair is a separate hash identity:
`tkc.scanned-pdf-spread-split/v0.2` / `scanned-pdf-spread-split-v0.2`.
It makes the exact integer edges of the rotated raster authoritative, maps those
edges back to PDF top-left points, and records the forward/inverse transform. It
works for legal odd and even dimensions, including a real-ratio odd-size
regression, without changing the older v0.1 contract or its 7D.6B R4 evidence.

The private `scan_gold_review.py` workbench freezes only local Context and two
region renders per physical page. OCR Gold uses the complete two regions on up
to 12 deterministically stratified physical pages; this replaces the v0.1
two-narrow-strip sampling that could reduce a Gold item to a few words. It requires explicit
`reviewer_id`, `review_session_id`, and `proposer_instance`; the CLI never invents
an independent reviewer. The current handoff uses the previously evidenced
`external-reviewer-1` instance, while independence remains unverified until an
external attestation is supplied. The evaluator ID is also explicit and must not
equal the reviewer or proposer.

Structure Gold has one item per physical page and requires an explicit clockwise
rotation, split boundary, ordered regions, printed-page-label candidate/confirmed
state, readability, and abnormal status. Full-region OCR items are selected only
from the ordered physical-page scope and page/region geometry; no OCR result is prefilled.
Canonical text, `unreadable`, `not_text`, and `needs_review` decisions are human
inputs, and excluded/unreadable samples remain in the denominator with a reason.
Finalize and evaluation reread all canonical files/assets and require complete
item coverage, external reviewer identity/attestation, proposer separation, and
matching hashes. Any missing or drifting input produces a paused/not-run state and
null metrics; no automatic consensus, Gold verification, promotion, executable,
release, or Codex-certification state is emitted.

The deterministic evaluator computes only structure exact match, OCR CER/WER,
numeric-token accuracy, sample coverage, and excluded counts. Thresholds are frozen
in the profile as `acceptance-target-candidate`; the overall verdict remains
`not-authorized` and test results cannot rewrite thresholds. The review page has no
source PDF, model output, prediction, confidence, calibration, test label, metric
threshold, network, or external resource. It supports local draft save, same-
workbench submission import/export, page navigation, clickable Region 0/1 cards
and tabs, zoom/pan, pixel/PDF BBox readout/drawing, and audited batch pass for
selected untouched structure pages.

The follow-up makes the UI and schema boundary explicit. Context BBox drawing is
performed on the complete rotated Context raster, with a stored pixel-edge to
PDF transform and four-corner envelope; a corrected box may extend beyond a
candidate region. `原始像素 100%` is intrinsic-pixel scale and is distinct from
window fit. Batch confirmation fails closed when any selected page differs in
frozen rotation, boundary, or order (the Patankar workpack has page 1=`1156`
and pages 2--40=`1153`), and records target-local region IDs. Rotation `0` is
valid. Batch pass uses each page's own candidate fields, requires a visible human
confirmation, refuses prior-edited pages, and leaves later manual corrections
valid while preserving the audit. Excluding a page/sample clears incompatible scorable fields, while
submission import validates exact reviewer/session/proposer, item and input
hashes, duplicate/unknown IDs, and audit shape before changing the draft.

The complete Context raster is MediaBox-scoped even when the source PDF has an
inset or offset CropBox. The original MediaBox and CropBox remain unchanged in
the frozen page geometry; pixel/PDF transforms use the full MediaBox render and
`pdf-page-top-left-points-v1`, rather than rejecting the PDF or relabelling
CropBox-local positions as page coordinates. Region form selects are explicitly
indexed (`data-region-index` plus `data-region-field`) so every change is written
to the selected region and survives the immediate form rerender.

The bundled scan-Gold and split-v0.2 JSON schemas now close their actual
top-level and key nested contracts with `additionalProperties=false`; the plan
uses the lightweight validator's local `oneOf` support for its two item shapes.
The profile's frozen flag is plural
`test_results_cannot_change_thresholds`. These schema checks are shape/const
checks only and do not create Gold or authorize evaluation.

## Candidate and review boundary

### Single physical pages versus two-page spreads

Do not apply the Phase 7D.6C spread/region workbench merely because a source is
scanned or image-backed. When each physical PDF page contains one page of book
content and upstream pagination is already accepted, use
`reviewer_workbench.py prepare-single-page` and review only the frozen
formula/figure/table/chart candidates. That route has no left/right split and no
region decision fields. Keep `scan_gold_review.py` for sources that genuinely
need structure/OCR Gold, rotated spread geometry, or per-region read-order
review. The two contracts remain separate and historical spread evidence is not
rewritten.

OCR transcripts and layout are candidates only. Ordinary `kind=text` output remains
transcript/coverage evidence and cannot directly create a visual object, canonical
anchor, knowledge object, formula AST, review receipt, or capability. Structured
table rows/cells are normalized only when the explicit PP-Structure contract
provides contiguous row/column indices, valid spans, non-overlapping bboxes,
typed value/unit/symbol candidates, and source/crop anchors.
Formula-like, unit-bearing, superscript/subscript, Greek-symbol, table, low
confidence, duplicate, and engine-disagreement observations receive visual-review
routing. A scan formula remains `executable=false` until the existing complete
visual review, source binding, formula-AST/unit/dimension checks, independent numeric
tests, and execution-tier/seal gates all pass.

Use the verifier after reconstruction and whenever a model/config/render changes:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/scanned_pdf_ocr.py verify \
  --source /absolute/path/to/authorized-source.pdf \
  --ir /absolute/path/to/scanned-ir \
  --pdftoppm /absolute/path/to/pdftoppm \
  --model-root /absolute/path/to/preinstalled/local-model-root
```

`verify` is an integrity/staleness check, not an OCR quality certificate. A passing
synthetic fake-backend or table-grid contract test proves only protocol/topology
behavior. A real PP-OCRv6 run proves that the bounded local worker executed with
the recorded environment and produced observations; it does not prove recognition
precision. PP-StructureV3 remains `not-run` when its local models are absent. No
F1/accuracy or table-quality claim is valid without an independent Gold set.

`verify_source_anchors.py` exposes explicit scan verification levels. Without a
matching local OCR runtime, `scan-integrity` checks source/render/page geometry,
bbox/transform, transcript and review bindings but deliberately reports no full
replay. With an exact runtime/model/config/worker contract, it may recompute the
transcript hash and report `scan-full-replay`; a legacy read-only IR reports
`legacy-readonly-integrity`. None of these levels is an accuracy, Gold, or Codex
certification result.

### Review an existing Scan IR structure without rerunning OCR

Use `scan_structure_review.py` after a bounded Scan IR has already passed its
integrity gate. `generate` loads that immutable IR, calls the existing
`build_scan_structure_review` candidate path, and locally re-renders only the
pages containing proposed headings. It does not invoke the OCR worker, resident
service, MCP, network, model download, semantic proposal, or promotion.

The browser reviewer confirms exactly five facts per segment: visible title,
page range, reading order, candidate BBox, and the bound render/transcript/
observation evidence. Candidate BBoxes are locked because the downstream core
accepts only exact Scan IR candidates. A wrong BBox must be rejected or marked
`needs_review` and regenerated; the reviewer cannot silently replace it with an
unbound coordinate.

The browser emits only `tkc.scan-structure-review-submission/v0.1`. A complete
submission without a separately supplied
`tkc.scan-structure-review-external-attestation/v0.1` still finalizes to a
paused revision with zero fragments. When both inputs pass, Finalize emits the
existing `tkc.scanned-pdf-structure-review-fragment/v0.1`; no second structure
truth protocol is introduced.

## Privacy and distribution

Keep source PDFs, temporary renders, OCR transcripts, model roots, worker output,
review fragments, and host transcripts in a local ignored work directory. The
private compiler release is source-free and contains none of them. Do not enable
model downloads, network access, cloud OCR/VLM, or third-party PDF transfer for this
adapter without a separate user authorization.
