# Compilation workflow

## 1. Register the source

Confirm the file is a PDF, compute SHA-256, record file size and media type, and
select a distribution mode. Treat the file as immutable input. Reject or quarantine
encrypted, malformed, or image-only sources until the relevant adapter exists.

## 2. Bound the Gold scope

For the first source, choose one coherent chapter or approximately 10–30 pages that
contains several of: definitions, equations, figures, procedures, assumptions,
exceptions, and decision rules. Record explicit start/end locators.

## 3. Reconstruct before interpreting

Recover reading order, heading hierarchy, paragraphs, equations, figures, tables,
captions, footnotes, and citations. Preserve physical PDF pages separately from
printed page labels. Assign stable document and segment IDs.

Run `scripts/reconstruct_pdf.py` with a local `pdftoppm` renderer to create the
deterministic first-pass PDF intermediate representation and render receipts. Follow
`references/pdf-intermediate-representation.md`. Treat candidate anchors as review
inputs, not final evidence. An explicitly selected PyMuPDF or Docling layout parser
may add only source-hash-bound coordinate candidates; pypdf remains the canonical
text stream for evidence offsets and hashes.

For Phase 7A bounded visual semantics, run `visual_semantics.py` after native
preflight. It emits candidate-only visual objects, relations, table grids/cells,
and conflict rows. Each object binds the pypdf source/page and pdftoppm
render/DPI/rotation receipt, normalized PDF-page geometry, crop commitment, and
parser/backend/model/config receipts. The reviewer-locator/confirmed-bbox baseline
needs no model. Docling and PyMuPDF are optional candidate adapters, while PaddleOCR
PP-StructureV3 is an explicit scanned-page route requiring a local model root;
missing dependencies fail closed and never trigger downloads or fallback.

For a whole-book inventory, use the v0.2 planner after PDF-IR intake:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/book_planner.py \
  /absolute/path/to/book.pdf \
  --output /absolute/path/to/empty-book-plan \
  --json
```

The planner reads all physical pages and emits `book-plan.json`,
`book-coverage-ledger.jsonl`, `module-plan.jsonl`, and `review-routing.jsonl`.
Every page and reconstructed segment receives a planning state, but OCR pages
remain candidate-only and the plan never claims whole-book semantic completeness.
Unresolved headings are structured gaps; they do not block creating a routing plan
and they do block semantic workpack construction for the unresolved segment.

## 4. Read semantically

Process semantic segments with previous context, limited forward preview, section
context, and known concepts. Do not summarize each page independently. Preserve the
page coordinates of every contributing segment.

Build the own-content slices with `scripts/semantic_workpack.py build` and follow
`references/semantic-promotion.md`. Treat focus spans as extractable evidence and
context spans as read-only. Require exact source coordinates instead of copied
excerpts in proposal drafts.

## 5. Atomize knowledge

Extract only claims supported by the selected source scope. Classify object type,
normalize the statement, and record assumptions, valid conditions, failure
conditions, uncertainty, and dependencies. Keep equations, figures, and tables as
first-class objects where they carry technical meaning.

Treat all atomized output as proposals. Freeze their hashes, obtain semantic review
from an instance not used for proposal generation, and obtain render-bound visual
receipts for every routed page. Reject or quarantine locally unsupported objects;
do not fail an otherwise usable source merely because one proposal is wrong.

For new workpacks, the compiler deterministically projects the structured drafts
into nine machine-addressable assertion kinds and an exact support matrix. Review
cardinality is derived from the highest-risk atom in each item: one reviewer for an
ordinary definition/concept, two for equations, relations, conflicts, applicability,
failure conditions, derivations, or visual facts. Every selected unit also receives
one coverage-ledger disposition. These deterministic records prove completeness of
declared bindings, not natural-language truth.

## 6. Promote, anchor, and link

Let only the deterministic promoter write canonical objects and evidence anchors.
Recompute receipt freshness first. Validate that source IDs resolve, exact text spans
remain inside focus scope, evidence hashes are present, and object/anchor links are
bidirectional. Record typed relations, explicit conflicts, rejected proposals, and
knowledge-gap dispositions in the promotion report. Promote a gap only after an
independent package-level review confirms that no accepted full-support object in the
frozen bundle resolves it. Phase 2 always emits a reference-only draft.

## 7. Synthesize runtime references

Generate concept, section, and chapter views from existing object IDs. A synthesis
must cite the objects it uses; it must not introduce source-free technical claims.
Build a machine runtime catalog as a deterministic projection of canonical object,
conflict, and gap metadata. Do not let search aliases or weights invent new claims.

## 8. Compile runtime behavior

Compile definitions and explanations as references. Compile procedures and decision
rules only when inputs, applicability, stop conditions, and evidence are explicit.
Default all extracted commands to non-executable reference text.

Visual facts must reference exact visual object, table-cell, or series IDs and
render/crop/bbox hashes; a page-level visual task is only a review route, never
sufficient support for promotion. External `tkc.visual-review-attestation/v0.2`
fragments are required: one independent reviewer for presence/localization and two
for plot/table values or visual formula conclusions. The compiler validates and
merges these fragments but cannot author their conclusions.

## 9. Test expertise

Create at least one test in every required category:

- `definition`
- `comparison`
- `equation`
- `decision`
- `failure-condition`
- `trace`
- `out-of-scope`

Run the deterministic package-only runtime against every test. Require evidence IDs
for in-scope retrieval and an explicit insufficiency response for unsupported
questions. Store responses and hash-bound receipts separately from test expectations;
test files may not self-declare their own result.

## 10. Seal, validate, and adapt

Run structural validation throughout compilation. When runtime responses and
competency receipts recompute without error, seal file hashes, mark the package
`ready`, run publication validation, and only then add or test host adapters. Re-seal
after any adapter or content change.

For a resumable local run, `scripts/pipeline_orchestrator.py plan|status|resume`
may build the workpack, freeze a review plan after drafts exist, and promote after
the complete reviewed gate passes. It pauses with an explicit `next_action` at
semantic proposal, independent review, competency, seal, and execution boundaries.
It cannot launch a model, author review fragments, download dependencies, set
`passed`/`ready`, or turn on executable capability.

Whole-book jobs use the separate book-level commands:

```bash
python3 scripts/pipeline_orchestrator.py book-plan \
  --job /absolute/path/to/book-job.json \
  --source /absolute/path/to/book.pdf \
  --plan-output /absolute/path/to/book-plan \
  --created-at 2026-08-21T12:00:00+08:00 \
  --json
python3 scripts/pipeline_orchestrator.py book-status --job /absolute/path/to/book-job.json --json
python3 scripts/pipeline_orchestrator.py book-resume --job /absolute/path/to/book-job.json --json
```

`book-resume` is idempotent and stops at structure resolution, OCR candidate
review, visual review, independent semantic review, competency, seal, or execution
as applicable. It never runs OCR or authors any of those gates.

## 11. Certify host behavior

Build user-like challenges without embedded results. Calibrate the protocol in a
separate run set, freeze a hidden-expectation hash, and then give each formal
challenge attempt to a fresh host-Agent instance with only the sealed Skill, the
challenge, and the response contract. Do not release expectations until all formal
runs finish.

Record the host/version/model identity reported by the orchestrator, raw final
response, available transcript, and exact package/adapter/runtime binding. Score only
against released expectations whose normalized hash matches the commitment. Store
the report and recomputable receipts in an external certification sidecar. Never
promote host certification into `decision_support` or `executable`, and repeat the
gate independently for every other host or model fingerprint.
