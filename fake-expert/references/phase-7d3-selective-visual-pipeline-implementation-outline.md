# Phase 7D.3 selective visual pipeline (v0.13.0)

This reference is the implementation contract for sparse technical-PDF visual
inspection. It extends the Phase 7D/7D.2 candidate boundary and does not
reinterpret a v0.12.0 job, manifest, visual object, or release in place.

## 1. Policy and context

New visual-parser jobs use `visual-mode=auto` unless the caller explicitly
selects `off` or `full`. The closed `visual-kinds` set is `table`, `figure`,
`chart`, `diagram`, and `equation`; unknown values fail closed. A policy is
canonical JSON with `mode`, sorted kinds, `max_visual_pages`,
`max_visual_regions`, `max_visual_seconds`, and `visual_context` (`none`,
`index`, or `on-demand`). Its SHA-256 is copied into the job, manifest, DAG,
census, routing, budget receipt, index, coverage, and chart records.

`off` performs no visual census, visual render, PP-Structure, or chart-worker
call. Every selected page is explicitly recorded as
`not-inspected-by-policy`; this is a policy gap, not evidence that the page has
no visual content. If the job separately selects the PP-OCRv6 text backend,
the existing native-text-first `pages_needing_ocr` route remains available and
is written under `scan-ir`; visual policy does not disable body OCR.

`auto` runs the no-model, no-render Visual Census first. Only census candidates
are sent to the bounded visual route. An empty candidate page is
`not-requested` and cannot start a heavy worker. `full` routes every selected
page and requested kind, still using page/crop-only workers. Neither mode turns
a census hint into a visual fact or Gold result.

The default Agent context is `index`: the visual index contains navigation,
status, candidate IDs, hashes, and review routing only. It excludes raw image
bytes, crop bytes, full OCR text, absolute source paths, credentials, and full
worker responses. `none` and `on-demand` are explicit host choices; source
content remains outside the default context boundary.

## 2. Census, routing, and coverage

The Census uses only `pypdf==6.10.0` native page objects, Form/Image XObject
counts, path operators, bounded text cues, captions, and alignment signals. It
records `model_invocations=0` and `render_invocations=0`. Its regions are
candidate envelopes, not confirmed bounding boxes.

Routing is explicit and closed: `not-inspected-by-policy`, `not-requested`,
`native-visual-only`, `table-candidate`, `chart-candidate`, `manual-review`,
and `budget-exhausted` are distinct states. Coverage distinguishes processed,
not-requested, not-inspected-by-policy, budget-exhausted, candidate, conflict,
and review-required. Candidate, conflict, and review-required rows remain
candidate-only and require external review before any promotion path.

Budget exhaustion is a pause: the receipt status is
`paused-budget-exhausted`, the state has a `budget-exhausted` pause gate, and
the affected pages/regions carry a gap. It is never reported as success or
silently omitted. Receipts record calls, pages, regions, elapsed seconds,
bytes, and object counts. Token estimates are host-adapter data only and are
not emitted as universal savings claims.

## 3. Stable objects and bounded execution

The canonical visual object ID commits source SHA, physical page, render/DPI/
rotation, normalized PDF geometry, crop hash, object kind, parser/backend/
runtime/model/config receipts, and path-independent request/response content.
Mode, scheduling order, budget, wall-clock, and temporary paths are excluded.
Consequently the same source/render/crop/model/config object has the same ID in
`auto` and `full`, while the policy and DAG nodes remain different.

The production path uses one bounded external worker session for a visual job,
backend, and profile. It receives a finite JSONL request list containing only
rendered crop bindings, constructs the model once (the receipt reports actual
`model_load_calls` 0 or 1), processes crops sequentially, enforces maximum
requests plus a parent hard total timeout and a worker hard per-request timeout,
rejects PDF/source paths, isolates ordinary errors, and emits one hash-bound
`tkc.visual-batch-receipt/v0.1`. An empty request list makes zero worker/model
calls. Timeouts terminate the isolated worker and pause remaining work; they are
not post-hoc elapsed checks. The worker never starts a hidden daemon, receives a
PDF, uses cloud OCR, downloads a model, or drops a receipt. Runtime inventory
and each request's contract hash must match the frozen contract.

## 4. Technical charts

`technical-chart-v1` binds the local official `paddleocr.ChartParsing` API,
model name/directory `PP-Chart2Table`, constructor options
`{"device":"cpu","enable_hpi":false}`, empty predict options, the local
model-file inventory, and the `tkc.technical-chart-result/v0.1` adapter hash. It
is eligible only when `chart` is in `visual-kinds`, mode is `auto` or `full`, the
Census route is `chart-candidate`, the local model inventory is complete, and
budget permits. Missing models emit `not-run-missing-models`; missing runtime
emits `not-run-runtime-unavailable` before rendering; rendered worker failures
use `paused-chart-runtime-error` or a shape/empty pause. There is no download or
silent fallback. Results expose axis, ticks, legend, series, units, annotations,
and strictly parsed numeric value candidates plus gaps/conflicts and a review
route. Ordinary text cells remain text candidates. Values require two
independent reviewers. Chart records always carry `candidate_only=true`,
`promotion=false`, and `executable=false`.

## 5. Jobs, DAG, migration, and resume

v0.13 jobs/manifests use `tkc.visual-parser-job/v0.4`,
`tkc.visual-parser-manifest/v0.4`, and the selective sidecars. v0.3 is read-only
compatibility input and is never rewritten as v0.4. Phase 7B projects policy,
census, routing, budget, index, coverage, and chart-candidate nodes. Policy,
kinds, budgets, and context invalidate only their visual/review/composer/
repackage closure; native正文 artifacts remain reusable.

`resume` is pause-only. It emits intent and keeps review execution, promotion,
sealing, signing, and executable output forbidden. A source-free release may
contain schemas, scripts, references, and tests only; it must not contain PDFs,
images, renders, crops, OCR output, models, real source titles or hashes,
absolute host paths, credentials, review workpacks, or session transcripts.

## 6. Evidence boundaries and non-goals

Synthetic contract tests prove schema, negative, determinism, privacy, budget,
batch, and migration behavior. A bounded local candidate run is engineering
observation only. Without independent Gold and two independent value reviewers,
the compiler must not claim chart accuracy, routing recall, visual accuracy,
performance targets, promotion, signing, executable status, macOS kernel
isolation, or M1 performance.
