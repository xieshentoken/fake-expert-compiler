# Phase 7A — Visual Semantics implementation outline (v0.7.0)

## Purpose

Phase 7A adds a parser-independent, candidate-only visual contract for bounded
native-text pages. It makes figures, captions, plots, table grids/cells, display
equations, and their relations addressable without treating a page-level visual
receipt as a verified fact. The release remains private, source-free, and
offline-first.

## Contract and evidence boundary

The canonical native evidence path remains `pypdf==6.10.0`; `pdftoppm` is the
canonical visual renderer and its source/page/render/DPI/rotation receipt is
bound into every candidate. `tkc.visual-object/v0.1` records stable IDs, type and
subtype, parent, source/page, canonical render and crop hashes, normalized
geometry, and parser/backend/model/config receipts. Relations, table grids, the
conflict ledger, external `tkc.visual-review-attestation/v0.2` records, and the
assurance manifest have independent schemas and canonical JSON/JSONL hashes.

The reviewer-locator/confirmed-bbox baseline uses no model. Docling and PyMuPDF
are explicit candidate adapters; PaddleOCR PP-StructureV3 is only an explicit
scanned-page route. Missing extras or model roots are reported as unavailable or
fail closed. No adapter can promote a fact, download a model, use the network, or
silently replace native evidence.

The pure mapping boundary maps Docling table/formula/picture/chart, PyMuPDF
table/image/drawing, and PaddleOCR table/formula/chart/picture rows into confirmed
candidate locators with page/bbox/polygon/kind/content hash/coordinate and all
parser/backend/model/config receipts. It does not run optional packages or certify
their accuracy. PyMuPDF/fitz is AGPL-licensed with a commercial license option;
hosts must complete that license review before use, and the source-free release
does not bundle it.

## Review and promotion

Candidate extraction is deterministic and deduplicates into a conflict ledger.
Coordinates are normalized to top-left PDF points with explicit rotation and
render-pixel conversion receipts. A visual fact must reference exact
`visual_object_id`, `table_cell_id`, or `series_id` values plus render/crop/bbox
hashes. A page-level visual task cannot promote an object. Presence/localization
needs one independent visual reviewer; plot/table values and visual equations need
two independent reviewers. Attestations are external fragments merged by the
workpack; the compiler never authors review conclusions. Attestation validation
iterates every required object, rejects wrong sessions/unregistered or self-review,
invalid confidence/difference/checks, duplicate/replay rows, and stale relation or
table-grid commitments. The assurance manifest freezes the review plan/session,
reviewer registry, required reviewer counts, and accepted attestation/reviewer IDs;
runtime never derives those counts from surviving accepted rows. Formula execution keeps
the existing AST, unit, dimension, independent-test, process, and seal gates.

The orchestrator may plan, report, resume, and stop at visual review. It cannot
launch a model, write an attestation, auto-promote, seal, execute, or set
`whole_book_complete_claimed=true`.

## Acceptance and risks

ReportLab fixtures cover figures/captions, tables and spans, display equations,
simple plot axes/legends/series, rotation/crop/hash tampering, duplicate/conflict
and missing objects. The p662 reference-only fixture proves that prose mentions
of Table 16.1/16.2 do not create a table. Local Chapter 16 candidate extraction
checks physical pages 653, 660–663; these observations are not independent review
and are not promoted knowledge.

Known risks are native-PDF layout ambiguity, renderer/version differences,
unavailable optional adapters, and unvalidated model accuracy. Crop values are
hash commitments only; the distributed compiler contains no render or crop
pixels. The implementation does not claim full-book visual routing, scanned-page
A/B accuracy, cross-page tables, automatic chart digitization, incremental DAGs,
Composer, signing, UI, encryption, or cryptographic reviewer identity.
