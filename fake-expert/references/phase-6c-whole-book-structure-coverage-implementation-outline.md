# Phase 6C Whole-book Structure & Coverage Planner v0.2 implementation outline

Status: implemented in the private checkout as `fake-expert v0.6.0`.

This file records the approved Phase 6C implementation route. It is an
implementation record, not a new design-approval gate.

## Purpose and boundaries

Phase 6C makes whole-book scope explicit before semantic compilation. The
planner reads the complete local PDF through the pinned pypdf PDF-IR boundary,
locks heading candidates and resolutions, assigns every physical page and
every reconstructed segment to a planning state, groups stable modules, and
emits review/OCR/gate routes. It does not create knowledge objects, call OCR,
launch a model, author a review, assert competency, seal a package, or enable
execution.

Canonical evidence text remains the existing normalized pypdf text stream. The
heading matching normalization is a locator aid only and is never used to
rewrite evidence text or change the canonical evidence fingerprint.

## Data contracts

The v0.3 PDF-IR emits `heading-candidates.jsonl` and
`heading-resolutions.jsonl`, with optional
`heading-locator-overrides.jsonl`. Candidate rows bind source SHA-256,
physical page, raw title occurrence, matching normalization, character span,
page/span/line hashes, parser/version, optional render SHA, optional layout
coordinates and layout candidate IDs, structural constraints, method, score,
and selection reason. Resolution rows bind the complete candidate ID/hash set,
selected occurrence, deterministic margin, title correction metadata, failure
code, structure order, and their own hash.

The schemas are:

- `heading-candidate.schema.json`
- `heading-resolution.schema.json`
- `heading-locator-override.schema.json`
- `book-plan.schema.json`
- `book-coverage-ledger.schema.json`
- `module-plan.schema.json`
- `review-routing.schema.json`

`book-plan.json` carries the source identity, page/segment/module/route
counts, structure status, planning states, artifact hashes, and explicit false
completion/policy flags. `book-coverage-ledger.jsonl` has one row for every
physical page and every segment. Its `planning_status` is separate from
`promotion_status`; a candidate route cannot be represented as promoted.
`module-plan.jsonl` binds stable module IDs, structural segment membership,
page ranges, dependencies, route decisions, planning/promotion status, and
input hashes. `review-routing.jsonl` gives page, segment, and module routes for
structure, semantic review, OCR candidate handling, visual review, competency,
seal, and execution.

## Algorithms

### Heading Locator v0.2

1. Generate page-local candidates from canonical pypdf text without storing
   source text in a release. Matching covers normalized exact, Unicode/NFKC
   and ligature equivalence, dehyphenated line matches, compact token and line
   boundaries, outline distance, parent/neighbor IDs, segment-envelope
   membership, running-header/footer exclusion, and optional hash-only layout
   bounding boxes.
2. Keep title correction separate from occurrence selection. Existing bounded
   `heading-override/v0.1` rows are recorded as `title_correction`; new
   `heading-locator-override/v0.2` rows select a candidate by candidate ID and
   before candidate/resolution hashes, reviewer instance, and rationale.
3. Resolve only a unique eligible candidate or a candidate whose score exceeds
   the next candidate by the fixed deterministic margin. Otherwise emit
   `heading_resolution_required` with candidate context and do not guess.
4. Verify PDF-IR receipts by recomputing candidates/resolutions from the source
   and parser/render receipts. Semantic workpack construction consumes the
   selected resolution span directly and never performs a second title search.

### Whole-book planner

1. Compile the complete physical range in memory with `pdftoppm=None` and the
   existing pypdf adapter. OCR pages are identified by preflight only; no OCR
   backend is invoked.
2. Assign each page and segment one of `compile`, `context-only`,
   `non-knowledge`, `ocr-candidate`, `visual-review`, `gap`, or `quarantined`.
   Page/segment status is planning state; promotion remains not-started,
   candidate-only, or blocked.
3. Group segments by their nearest structural root and add deterministic
   50-page context modules for pages outside structural envelopes. Module IDs
   are derived from source SHA, title, segment IDs, and page set. All module
   inputs and routes are hash-bound.
4. Write sorted, canonical JSON/JSONL artifacts. The plan ID and row IDs do
   not depend on output paths, timestamps, or filesystem traversal order.

### Book-level orchestrator

`pipeline_orchestrator.py book-plan` creates a hash-bound book job and builds
the plan. `book-status` validates the source and all plan artifacts and reports
module/status aggregation. `book-resume` is idempotent: it builds a missing
plan, then stops at the first explicit structure/OCR/visual/semantic gate.
The job policy permanently records network/model/OCR/review/competency/seal/
execution as disallowed. Existing scoped `plan/status/resume` behavior remains
unchanged.

## Risks and fail-closed behavior

- Whole-book structure can remain unresolved; the plan is usable for routing
  but `whole_book_complete_claimed` stays false.
- OCR pages are candidate routes only. No transcript is promoted by planning.
- Visual review is a required route when preflight says so; native text does
  not substitute for rendered inspection.
- Semantic review, competency, seal, and execution remain independent gates.
- Hash or source/parser/render/locator tampering stops verification or
  workpack construction.
- A legacy v0.1 PDF-IR remains usable under its existing unbound compatibility
  label; new v0.3 IR requires the locator receipts.

## Migration

The version source is `scripts/compiler_version.py`. v0.5.0 artifacts are not
rewritten. New IR writers add the two locator files, and scanned-PDF proposal
writers preserve the same native locator files while keeping OCR candidate
artifacts separate. Workpacks generated from v0.3 consume resolutions; legacy
IRs retain the old string locator only when explicitly identified as legacy.
The release runtime contract adds the locator and whole-book planner protocol
without changing `pypdf==6.10.0` or canonical evidence hashes.

## Test matrix

The unittest matrix uses reportlab-only synthetic fixtures for new behavior:

- repeated titles with running headers and body occurrence selection;
- cross-line hyphenation, Unicode/NFKC ligatures and punctuation;
- extraction/layout order differences and outline/page constraints;
- missing titles and structured resolution tasks;
- bounded override before-hash and candidate/resolution tamper rejection;
- source/parser/render/locator receipt tamper paths;
- page and segment ledger coverage, stable module IDs, gate routes;
- two identical builds compared byte-for-byte;
- book-level status/resume with no automatic OCR/review/seal/execution.

The complete repository suite remains `unittest`, never pytest.

## Acceptance gates

Phase 6C is accepted only when the full unittest suite passes; the fixed-hash
the bounded real-book plan has a row for every page and segment; selected chapters
produce resolved workpacks or hash-bound structured resolution tasks; Chapter
16 still stops at independent review; two real plans are byte-identical;
v0.6.0 passes its standalone verifier and fixed-timestamp rebuild comparison;
and v0.5.0 still verifies. No public publication, external transfer, real
independent review, reviewer attestation, or signing is part of this gate.
