# Phase 7D.1 — Local PaddleOCR Runtime Qualification & Raster/Table Mapping

Status: implemented and independently revalidated for private compiler release
`v0.11.0`; see `docs/phase-7d1-local-paddle-runtime-v0.11.0.md` for evidence and
the explicitly retained limitations.

## Purpose

Phase 7D.1 adds a bounded, local-only PaddleOCR path for visual candidates. It
qualifies the externally installed Paddle runtime without pretending that the
compiler's own virtual environment contains Paddle, turns an explicitly bound
PDF region into a real temporary raster crop, maps OCR observations back through
the PDF/render/crop coordinate chain, and defines a strict PP-StructureV3 table
contract. Every result remains a candidate and carries enough immutable receipt
material for later independent review and Phase 7B invalidation.

The only external runtime in the current qualification is the read-only
installation supplied by the host at `<external-paddle-runtime-root>`. Its interpreter,
distribution versions, worker source, model inventory, and model-file manifest
are inputs to the contract. The implementation never starts or stops its MCP
service and never uses localhost MCP HTTP as the canonical compilation path.

## Design

1. **External runtime contract.** A job declares an allowlisted, resolved worker
   interpreter, worker source, backend ID, exact distribution inventory, model
   root and model-file manifest, configuration, network/download policy, and
   output workspace. Resolved containment and symlink checks reject path escape,
   input/output overlap, runtime/model drift, unknown versions, download flags,
   and network-enabled execution. The worker protocol is fixed JSONL with a
   minimal environment, a private temporary work directory, a bounded timeout,
   read-only runtime/model/input paths, and byte/hash receipts for request and
   response. macOS kernel/process network isolation is not claimed unless it is
   explicitly verified by the host; an unverified host gate remains visible.

2. **Raster-region OCR.** A canonical page render is bound to the source page,
   render receipt, page geometry, rotation, CropBox/MediaBox policy, and source
   SHA. An explicit top-left PDF-point bbox is transformed to render pixels,
   cropped into a private Phase 7D.1 temporary workspace, hashed, and checked
   against the expected crop SHA before the worker sees it. The inverse transform
   is tested at corners and maps every observation from crop-local pixels back to
   physical-page coordinates. The source PDF is never passed to the worker.

3. **Accurate backend identity and normalization.** PP-OCRv6 text recognition
   uses `paddleocr-ppocrv6`; it is not called `paddleocr-ppstructure`. The
   actual PaddleOCR 3.7 `predict` result fields (`rec_texts`, `rec_scores`,
   `rec_polys`/`rec_boxes`) are normalized only when their shape and lengths are
   known. Unknown result shapes fail closed. PP-StructureV3 remains a separate
   candidate backend. Its table/cell normalizer requires table identity, row and
   column indices, positive spans, cell geometry, text/value/unit/symbol and
   confidence/source anchors, and rejects overlap, invalid spans, and tamper.
   Missing local structure/layout models produce `not-run`/`paused`, never a
   download and never a table inferred from PP-OCR line text.

4. **Candidate and review boundaries.** Native, Paddle, and Docling candidates
   are retained as typed conflicts when they disagree; the compiler does not
   select a winner. OCR text is transcript/coverage candidate material unless a
   pre-existing explicit visual object contract binds it. Numeric values, units,
   formulas, table cells, plots and series retain existing independent-review
   requirements. Review overlays add Paddle objects/cells/series and all new
   receipt hashes but do not author attestations, Gold, consensus, promotion,
   signatures, or capability upgrades.

5. **Incremental invalidation.** Runtime, worker, model inventory, crop/render
   binding, OCR observation and table-grid/cell identities participate in the
   Phase 7B typed DAG. A changed receipt invalidates the dependent OCR/object,
   review, recomposition and repackaging closure; an exact immutable snapshot
   remains reusable.

## Risks and fail-closed responses

- Paddle packages or cached models drift: reject the contract and record the
  expected/observed receipt difference.
- A crop is geometrically ambiguous, out of bounds, rounded inconsistently, or
  has a different render/crop hash: stop before OCR.
- A worker can observe the source PDF, write into the input tree, download, or
  call MCP HTTP: reject the request and report the corresponding isolation gate.
- Paddle changes its result object shape: return an unknown-shape gate instead
  of guessing fields.
- PP-StructureV3 assets are absent: report a real not-run/paused result with
  `verified_gold=false`; do not substitute text lines for topology.
- Candidate observations disagree: emit a typed conflict and route review; do
  not auto-promote or silently choose a parser.
- Real benchmarks have no independently reviewed Gold: report engineering
  observations only, with metrics `not-run` and `verified_gold=false`.

## Non-goals

- No network access, cloud OCR, model download, MCP HTTP dependency, or service
  lifecycle operation.
- No modification of `<external-paddle-runtime-root>`, its virtualenv,
  cache, scripts, images, processes, logs, or service state.
- No claim that PP-OCR text recognition is PP-Structure table extraction.
- No accuracy, F1, semantic truth, independent review, Gold, executable status,
  promotion, signature, or capability-upgrade claim.
- No inclusion of real PDFs, page renders, crops, OCR transcripts, models,
  source paths/titles, candidate workpacks, or host transcripts in `v0.11.0`.
- No mutation of `v0.10.0` or earlier release artifacts.

## Acceptance criteria

- [x] Runtime/interpreter/worker/backend/config/model inventory and manifest
      hashes are explicit, allowlisted, drift-checked, and source-free in the
      release contract.
- [x] Worker JSONL, timeout, private workspace, read-only paths, output hash,
      network/download and MCP-bypass gates are tested.
- [x] PDF top-left points ↔ rotated render pixels ↔ crop-local pixels ↔ page
      coordinates round-trip tests pass, including CropBox/MediaBox, bounds,
      hash and rounding failure cases.
- [x] A bounded local PP-OCRv6 smoke either runs against cached models and
      records an observation, or is explicitly not-run; it never downloads and
      never reports an accuracy claim.
- [x] PP-StructureV3 normalization and table-grid/span/overlap/tamper synthetic
      contract tests pass; missing real structure assets remain not-run/paused.
- [x] Native/Paddle/Docling conflicts, Phase 7B invalidation closure, review
      overlay receipt propagation and no-op reuse are covered by tests.
- [x] Full unittest, quick validation, CLI/verifier/schema/privacy checks,
      independent safe extraction, representative crop render inspection and
      two-build byte determinism pass with old regressions preserved.
- [x] A newly built private `v0.11.0` source-free release is distinct from and
      leaves the immutable `v0.10.0` SHA-256 unchanged.

## Evidence classification

The final Phase 7D.1 report separates: actual PP-OCRv6 execution;
PP-StructureV3 actual execution or missing-model not-run; synthetic table/crop
contract evidence; candidate benchmark observations; verified Gold status;
actual OS/process isolation level; and objects, cells, numeric values, units,
formulas, plots and series still requiring independent review.
