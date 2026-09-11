# Phase 7D — Visual Extraction Quality & Parser Orchestration implementation outline

Status: implementation authorized by the user; candidate-only, offline, and
private-transfer-only. This outline records the immutable v0.10.0 Phase 7D
baseline; Phase 7D.1 extends it for the v0.11.0 compiler without mutating the
v0.10.0 archive. It is not a visual review attestation, a semantic Gold result,
or an accuracy certificate.

## 1. Purpose

Phase 7D makes visual extraction a reproducible local compiler stage. It
orchestrates the existing native-text PDF path, visual candidate normalizer,
isolated OCR worker, optional adapter boundary, Phase 7B incremental DAG, and
Phase 7C downstream Composer without promoting any parser output. The target is
traceable, verifiable, composable, and updateable machine knowledge candidates
for figures, tables, plots, equations, and scanned-page/raster regions.

The canonical text/source chain remains `pypdf==6.10.0`. OCR is limited to
`pages_needing_ocr` or an explicit raster region. PaddleOCR PP-StructureV3 is
the primary scan/table candidate, Docling is a challenger, and PyMuPDF is an
optional geometry/image/drawing/table candidate. Missing dependencies, missing
local model files, network/download requests, stale receipts, and ambiguous
parser output fail closed.

## 2. Architecture

```text
native pypdf preflight + explicit visual/raster scope
                         |
          Visual Parser Orchestrator job (plan/build)
             |          |          |          |
       pypdf baseline  visual     OCR worker  adapters
       /locator        semantics  JSONL      (pure mapping)
             \          |          |          /
          provenance-normalized candidate rows
                         |
       disagreement -> conflict; no silent parser choice
                         |
  visual objects/relations/table grids/quality/evaluation contract
                         |
        validate -> status -> pause-only resume at review/model gates
                         |
       Phase 7B typed DAG -> Phase 7C/semantic downstream closure
```

The orchestrator freezes source, input/config/model/parser/backend/render/
component hashes, adapter versions, coordinate transforms, scope, and every
candidate's receipt. It writes only a local `tmp/workspace/output/phase7d`
work directory and source-free metadata. Existing `scanned_pdf_ocr.py` and
`ocr_backend_worker.py` remain the fixed JSONL/isolated-process boundary; the
orchestrator never sends the PDF to a worker and never implements a second OCR
protocol.

Geometry uses physical page numbers, MediaBox/CropBox/rotation, and
`pdf-page-top-left-points-v1`. Render/crop hashes are mandatory for visual
objects. Bounding boxes are exact only when an explicit adapter/locator supplies
the geometry; a full-page envelope is marked non-exact and requires review.
Native image/form candidates replay PDF `q`/`Q`/`cm`/`Do`, recursively apply
Form XObject matrices and BBoxes, transform all corners into the MediaBox
top-left frame, and retain resource path plus stream/object hashes. Caption
alignment may consume only an axis-adjacent image/form candidate; overlapping
nested Form/Image candidates are deduplicated as one placement. Table frames
prefer a bounded dominant ruling-line/rectangle cluster, exclude abnormal tall
paths crossing the footer, and route an insufficient text-only frame as an
explicit gap candidate.

Table candidates retain ruling-line and text-cluster evidence, dynamic row/column
clusters, multi-row headers, spans, cell text/value/unit/symbol candidates,
caption/note/footnote routes, cell-level anchors, and a deterministic grid hash.
Cross-page continuation is a candidate/gap route only. Plot axes, ticks, units,
scale type, legends, series, annotations, and digitized values are candidates
with uncertainty; keyword presence alone cannot confirm a plot or series.

## 3. Risks

- PDF layout operators can use arbitrary transforms, clipping, images, or
  indirect resources; geometry is therefore a candidate and must preserve
  parser disagreement rather than silently choose a winner.
- OCR/table/chart models may be absent, version-sensitive, or inaccurate. The
  compiler reports `not-run`/`unverified` until a real local model run exists;
  synthetic fixtures prove only protocol behavior.
- Source text is untrusted data and cannot alter compiler policy.
- Renderers and optional parsers can drift. Hash-bound receipts and the DAG must
  invalidate the complete visual/review/composition closure.
- PyMuPDF/fitz has an AGPL/commercial licensing boundary and is not bundled.

## 4. Non-goals

- No network, cloud OCR/VLM, model download, silent fallback, or credential use.
- No canonical/prompted knowledge promotion from a candidate; no automatic
  consensus, reviewer attestation, semantic/visual review, formula execution,
  signature, or capability upgrade.
- No whole-book completeness claim, automatic cross-page table merge, automatic
  plot-value truth, or claim that adapter integration improves accuracy.
- No PDF, render, crop, transcript, model, real pilot, review workpack, or
  credentials in the compiler release.

## 5. Delivery priorities

### P0

1. Add a unified visual parser orchestrator with `plan`, `build`, `validate`,
   `status`, and `resume`; enforce path separation, native-first routing,
   receipt freezing, subprocess timeout/permission/network boundaries, conflict
   generation, and pause-only review/model gates.
2. Improve multilingual Figure/Fig./图, Table/Tab./表, Equation/Eq./式
   locators, appendix/compound numbering, transformed paths/images, caption
   alignment, and multi-panel candidates without page-wide silent bboxes.
3. Add table topology candidates and plot/diagram first-class relations with
   typed numeric/unit/symbol evidence and uncertainty.
4. Add synthetic Gold/evaluation contracts, negative fixtures, metrics, and
   deterministic replay; real pages are candidate benchmarks unless independently
   reviewed.
5. Project parser/model/config/render/component changes through the Phase 7B DAG
   and preserve Phase 7C downstream recompose/re-review closure.

### P1 — implemented after P0 stabilization

The P1 routes are implemented as deterministic candidates, not placeholders:
spatially distinct image/form placements can emit `figure/subtype=panel`
children while nested Form/Image placements collapse to one placement; bounded
diagram label/arrow annotations are linked with candidate relations; numeric
chart markers emit `annotation/subtype=digitized_value` with an explicit
axis-resolution error-model candidate; cross-page table continuation rows remain
review-required and never auto-merge; and the source-free
`tkc.visual-review-overlay/v0.1` manifest binds object/cell/series/relation
hashes and reviewer task counts without authoring attestations. P1 remains
candidate-only and cannot imply verified semantics.

## 6. Acceptance targets

- Synthetic contract and negative fixtures: 100% pass, including no false table
  objects from prose mentions, transform/crop/hash tampering, missing backends,
  network/download flags, parser conflicts, path overlap, and deterministic replay.
- Metrics include object precision/recall, bbox IoU, table topology/cell exactness,
  numeric/unit exactness, conflict and negative false-positive counts, and repeat
  build byte identity. A real dataset without independent human confirmation is
  `candidate benchmark`, never `verified Gold`.
- Recommended future target, not a current result: independently confirmed real
  object F1 >= 0.90, 90% bbox IoU >= 0.80, and same-page table structure >= 0.90.
- Real PaddleOCR, Docling, and PyMuPDF states must report the installed package
  version, local model/config hashes, and A/B metrics only when actually run.
  Otherwise the report explicitly says `not-run` or `unverified` with the reason.
- v0.10.0 is source-free, private-transfer-only, offline-only, independently
  extractable/verifiable, fixed-timestamp reproducible, and privacy-inventoried;
  v0.9.0 and earlier releases remain immutable.

## 7. Review and release gate

The compiler may prepare candidate rows, conflict ledgers, review plans, DAG
invalidations, and pause-only local intents. Only external independent reviewers
may author visual/semantic attestations. Plot/table values and visual formula
conclusions require two independent reviewers; presence/localization requires
one. No schema-valid, synthetic, engineering-QA, or candidate result is called
semantic/visual truth. All real bounded regression artifacts live under
`tmp/workspace/output/phase7d` and are excluded from the release inventory.
