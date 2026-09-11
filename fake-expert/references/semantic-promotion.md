# Semantic promotion and assurance v0.7

Read this reference before turning PDF IR segments into knowledge proposals or
reviewing a proposal bundle.

## Trust boundary

Use four separate stages:

1. build an ephemeral local workpack;
2. let one or more agents submit proposals only;
3. obtain independent semantic and visual review bound to current input hashes;
4. let a deterministic promoter write canonical knowledge.

Never let the proposer assign `verified`, `human-reviewed`, publication status, final
confidence, or executable capability. A structurally valid proposal is not semantic
proof. Keep the existing Gold package outside the proposal agent's context; compare
against Gold only after independent review.

## Phase 7A visual semantics

For bounded native-text pages, `pypdf==6.10.0` remains the sole canonical native
evidence parser and a local `pdftoppm` render receipt is the canonical visual base.
`visual_semantics.py` normalizes coordinates, creates stable candidate-only
`tkc.visual-object/v0.1` rows, relations, table grids/cells, and a conflict ledger.
Every object binds source SHA, physical page, canonical render SHA/DPI/rotation,
crop commitment, bbox/polygon, and parser/backend/model/config receipts. The
JSON/JSONL files use canonical serialization and stable SHA-256 commitments.

The zero-model reviewer-locator/confirmed-bbox path is the baseline. Docling and
PyMuPDF adapters can add candidates only; PaddleOCR PP-StructureV3 is an explicit
scanned-page route and requires an installed model root. Missing optional packages
or model roots are unavailable/fail-closed. No adapter downloads, uses a network,
silently falls back, or promotes a visual fact.

The adapter boundary includes a pure mapping function for Docling table/formula/
picture/chart, PyMuPDF table/image/drawing, and PaddleOCR table/formula/chart/
picture candidates. It requires source/page/render/provenance receipts, normalizes
coordinate-space bbox/polygon values, and emits candidate locators for the native
extractor; it does not claim model precision. PyMuPDF/fitz is AGPL-licensed with a
commercial license option, so hosts must complete that license review before use;
the private release does not bundle it.

Phase 7A review fragments contain external `tkc.visual-review-attestation/v0.2`
rows. They bind exact visual object, table-cell, and series IDs and full-page,
crop, render, bbox, and input hashes. The compiler cannot create review conclusions.
They also bind every in-scope relation and table-grid ID/hash. The v0.2 assurance
manifest freezes the plan/session, registered reviewers, required reviewer count,
and accepted attestation/reviewer IDs; runtime validation never infers the count
from surviving accepted rows. Missing required objects, reviewers, sessions,
differences, or relation/grid bindings fail closed.
Presence/localization needs one independent visual reviewer; values read from a
plot/table and visual formula conclusions need two. Page-only visual support never
promotes a fact. The existing formula AST/unit/dimension/independent-test/process/
seal gates remain required.

## Build the workpack

```bash
python3 scripts/semantic_workpack.py build \
  --source /absolute/path/to/source.pdf \
  --ir /absolute/path/to/pdf-ir \
  --output /absolute/path/to/empty-workpack \
  --pdftoppm /absolute/path/to/pdftoppm
```

The builder must:

- match the source SHA-256 and Phase 1 source ID;
- require an eligible native-text scope with zero OCR-routed pages;
- uniquely locate every reconstructed heading;
- create each node's own-content slice from the end of its heading to the beginning
  of the next heading;
- keep previous/next context read-only and outside extractable spans;
- generate equation, glyph, figure-presence, table, image, and math-page tasks;
- render routed pages when a renderer is supplied;
- write the workpack with local-only permissions and `shareable=false`.
- declare `atomic-support-review-v2` and create candidate-only paths for atomic
  assertions, the support matrix, selected-scope coverage, and external review
  attestations. These files are materialized deterministically after all drafts
  exist; the builder does not invent semantic content.

The workpack contains temporary source text and page images. Keep it under an ignored
local workspace, never embed it in the output Skill, and do not send it to an external
model without explicit user authorization.

## Phase 7C cross-source candidate boundary

The Semantic Composer is not a promotion shortcut. It creates a separate,
source-attributed candidate sidecar from immutable package views and a bounded
candidate-source input. A Phase 5B/legacy package with evidence anchors but no
current Phase 6A `semantic-assertions`/`support-matrix` is explicitly
`legacy-source-support` / evidence-only and remains `atomic_support_required`.
It must not be relabelled `atomic-supported` or promoted through the current
atomic gate. Candidate-source refs likewise remain candidate-only.

The Composer may deterministically normalize typed labels/aliases/terms, compare
definition/assumption/applicability/failure boundaries, propose relation kinds,
and create dimension-compatible unit conversion candidates. It cannot infer
semantic equivalence from name/token overlap, resolve contradictions, generate
consensus, rewrite a child module, author an attestation, or raise capability.
All rows use `preserve-separate-no-consensus`. High-risk formula/numeric/unit/
symbol/conflict/applicability/failure candidates require two independent review
slots; ordinary concept/alias candidates require one. The review plan is a
request for external review, not review evidence.

For PDF candidates, stable evidence binds source SHA, physical page, printed
label candidate, bbox/locator/content hash, pypdf version, actual page geometry,
and page-specific render/crop hashes. A missing bbox is fail-closed unless the
input explicitly selects `full-page-candidate`, records non-exact/full-page
precision, and requires visual review. A frozen IR uses a canonical component
inventory and must be replayed by `verify_pdf_ir` with network disabled; its
candidate scope must be inside the normalized IR scope. No printed page label
is silently filled from a visual observation, and no candidate is called
verified without the required independent review.

## Write unit drafts

Write exactly one file at `drafts/<unit-id>.json` for each unit. Follow
`assets/schemas/semantic-draft.schema.json`.

Choose one candidate disposition:

- `promote`: the unit yields at least one object, conflict, or gap;
- `context-only`: it supports navigation or another unit but yields no object;
- `non-knowledge`: bibliography, index, or structural content;
- `reject`: unusable or misleading extraction, with a reason.

For each proposed object:

- use only its unit's `focus_spans` as evidence;
- return page and Unicode `[start,end)` offsets plus the span SHA-256;
- do not return source excerpts, quotes, raw text, page text, or self-review fields;
- classify origin as `source-explicit`, `source-paraphrase`, or
  `compiler-derived`;
- state assumptions, valid conditions, failure conditions, bounded physical pages,
  and excluded conclusions explicitly;
- link every Equation to an equation, glyph, or math-page visual task;
- keep source notation, normalized notation, and derived notation separate;
- create a conflict proposal for an inconsistent formula or incompatible claims.

Treat every proposed gap as a **unit-local observation**, not a package-level fact.
Bind it to one or more exact focus spans that demonstrate what the unit does and does
not supply. A proposer cannot declare a gap unresolved across the package.

Use object references `<unit-id>:<local-id>` in cross-unit relations and conflicts.
Do not propose a Procedure or DecisionRule unless the source scope truly defines the
complete contract; Phase 2 still forbids executable capability.

## Validate drafts

```bash
python3 scripts/semantic_workpack.py validate \
  --source /absolute/path/to/source.pdf \
  --workpack /absolute/path/to/workpack
```

Validation recomputes source, page, focus, and evidence-span hashes. It fails on
context citation, source-text leakage, unresolved relations, silent formula mutation,
unrecorded formula conflicts, missing visual tasks, incomplete unit coverage, and
capability escalation.

Passing this gate means only `source-bound proposal bundle`. It does not mean the
claims are semantically correct or ready for an Expert Skill.

## Review and promote

Freeze the proposal bundle before review:

```bash
python3 scripts/semantic_workpack.py prepare-review \
  --source /absolute/path/to/source.pdf \
  --workpack /absolute/path/to/workpack \
  --reviewer-instance <host-recorded-reviewer-a> \
  --reviewer-instance <host-recorded-reviewer-b>
```

The orchestrator writes `review-queue.jsonl`, `visual-review-queue.jsonl`, and a
`review-plan.json` that binds the source, proposal/dependency hashes, render hashes,
proposer instances, and reviewer registry. This registry is explicitly
`host-orchestrator-recorded-not-cryptographic`; it prevents accidental self-review
and stale receipts but is not a human identity credential.

The plan also carries `gap_resolution_policy=explicit-package-scope-v1` and a public
SHA-256 fingerprint over its bounded metadata. The fingerprint detects accidental or
untracked plan changes; because anyone with the plan can recompute it, it is not a
signature, authorization token, or reviewer-identity proof.

Legacy v0.4 workpacks carry
`review_protocol=separate-source-render-review-v1` and a bound `review_session_id`.
Every semantic record must declare `review_method=fresh-source-inspection` and a
`review_checks` object with `evidence_span_checked`, `support_completeness_checked`,
`scope_and_applicability_checked`, and `conflict_checked` set to `true`. Formula
objects also require `formula_checked=true`. Every visual receipt must declare
`review_method=fresh-render-inspection` and set `page_asset_opened` and
`task_resolutions_checked` to `true`. These are host-orchestrated workflow receipts,
not cryptographic identity evidence; they prevent incomplete or blanket review
scaffolds from being presented as a finished review.

New workpacks default to `review_protocol=atomic-support-review-v2`. Before the
review plan is frozen, the deterministic compiler decomposes structured proposals
into `tkc.semantic-assertion/v0.2` rows covering propositions, numeric tokens, units,
symbols, assumptions, applicability, failure conditions, derivation/AST nodes, and
visual facts. It emits exactly one `tkc.support-matrix/v0.1` row per assertion and
one `tkc.coverage-ledger/v0.1` row per selected semantic unit. A missing or changed
atom, support edge, formula-node path, or coverage disposition fails closed.

External semantic fragments for the new protocol contain
`tkc.review-attestation/v0.1`, not the legacy single review row. Definitions and
ordinary concepts with no higher-risk atom need one independent reviewer. Equations,
relations, conflicts, applicability, failure conditions, derivations, and visual
facts need two distinct reviewers. Each attestation binds the exact plan, item,
assertion bundle, source/span/render hashes inspected, and contains one explicit
difference or no-difference observation per assertion. The merge writes all compact
attestations plus a deterministic lead `tkc.semantic-review/v0.1` projection for
legacy audit/runtime compatibility. Different reviewer strings remain host-recorded
separation, not cryptographic identity proof.

For legacy workpacks only, write exactly one `tkc.semantic-review/v0.1` record per
semantic queue item. Both protocols require one
`tkc.visual-receipt/v0.1` record per routed page. Accepted semantic items require
full evidence support, independent extraction/interpretation confidence, and a
non-empty rationale. Rejected or quarantined items require stable issue codes. Every
visual receipt must cover every task on its page, and every accepted visual-dependent
claim must link only to supported tasks on a page whose receipt verdict is verified.
When reviewers write disjoint fragment files, merge them mechanically instead of
concatenating them by hand:

```bash
python3 scripts/semantic_workpack.py merge-review \
  --source /absolute/path/to/source.pdf \
  --workpack /absolute/path/to/workpack \
  --semantic-fragment /absolute/path/to/reviewer-a-semantic.jsonl \
  --visual-fragment /absolute/path/to/reviewer-a-visual.jsonl
```

Repeat both fragment flags as needed. Merge fails on missing/duplicate queue items,
unregistered or self-reviewing identities, stale fingerprints, or incomplete visual
task coverage, and restores the previous central receipts if the reviewed gate fails.

Since v0.3.2, fragment files must live **outside** the workpack directory. The merge
copies each fragment into `review-fragments/` keyed by content SHA-256, writes a
`review-fragments.json` provenance manifest (`tkc.review-fragments/v0.1`) binding
fragment digests, record counts, and reviewer instances, then writes the central
`review-records.jsonl` / `visual-receipts.jsonl`. Validation requires that manifest:
any direct write of review material without it fails with
`review_fragments_manifest_missing`, and any post-merge edit of the central files
that is no longer attributable to the recorded fragments fails with
`review_fragments_manifest_mismatch`. This closes the batch-promotion hole where a
proposal script wrote central receipts directly.

The merge and the reviewed gate also detect template-style blanket patterns: when
two or more accepted items share a single rationale (or one shared confidence), or
two or more verified pages share a single observation, validation fails with
`review_blanket_pattern` / `visual_review_blanket_pattern` and the review material is
moved into `quarantine/` under a receipt. This is fail-closed behavior: a blanket
merge writes nothing, and a direct blanket write is quarantined on validation.

### Authoring batch review fragments (v0.3.3)

Batch-generated review is allowed, but every record must describe the item it
reviews; the anti-blanket gate is a checklist, not a style rule. Follow these
rules to avoid a fail-closed quarantine:

- For `atomic-support-review-v2`, write the exact number of
  `tkc.review-attestation/v0.1` lines declared by each queue row's
  `required_reviewer_count`; use `schema-help --kind review-attestation`. For the
  explicit legacy mode only, write one `tkc.semantic-review/v0.1` line. Visual
  fragments always use `tkc.visual-receipt/v0.1`. Include the plan's
  bound `review_session_id`, `review_method`, and the required
  `review_checks`/`page_asset_opened`/`task_resolutions_checked` inspection flags.
- Use a **per-item rationale** that quotes the item's evidence span, the accepted
  support/conflict content, or the specific rejected issue code; never reuse one
  rationale string (or one confidence value) across all items. Batch hosts can
  vary the wording per item programmatically and still be honest.
- Write one **per-page observation** per visual receipt naming the resolved tasks
  and any anomaly found on that page; never reuse one observation across pages.
- The gate fires on a shared rationale across all records or across the accepted
  subset, an identical confidence shared by all accepted items, and a shared
  visual observation; it does not require artificial per-item uniqueness beyond
  those signals, so honest batch reviews that vary wording and confidence pass.
- Merge fragments only once per review session; re-merging a corrected fragment
  rewrites `review-fragments/` and the manifest, so keep one authoritative copy
  of each fragment outside the workpack.

A reviewer that loops over the queue and fills a single shared rationale is an
invalid adapter workflow even when every other field is present; fix the
fragment content and re-merge rather than editing the central files.

For every gap review, add `gap_resolution`:

- `unresolved_in_package` with no resolver refs requires an accepted review and is
  promoted as an unresolved package gap;
- `resolved_elsewhere` requires a rejected review with
  `knowledge_gap_resolved_elsewhere` and one or more accepted, full-support object
  refs from different units.

The reviewer decides semantic closure. Deterministic validation only proves that each
declared resolver exists in the same frozen bundle, is independently accepted, and
has valid source/visual evidence.

Validate current receipts immediately before promotion:

```bash
python3 scripts/semantic_workpack.py validate \
  --source /absolute/path/to/source.pdf \
  --workpack /absolute/path/to/workpack \
  --level reviewed
```

Any post-plan change to a proposal, dependency, source span, or render invalidates
the old receipt. A model-written `human` or `independent-agent` string is not accepted
as identity proof.

The deterministic promoter must whitelist output fields, assign stable object,
relation, conflict, gap, and anchor IDs, create exact bidirectional evidence links,
preserve unresolved conflicts and package-level gaps, exclude all workpack
text/images/absolute paths, force `executable=false`, and emit a complete promotion
report. Every promoted entity must retain a one-to-one binding to its accepted review;
deleting non-object review records must make structural validation fail. Publication
remains a later gate requiring runtime references, competency tests, sealing, and
external-source verification.

For a new-protocol package, promotion also writes the canonical source-free subtree:

- `references/semantic/semantic-assertions.jsonl`
- `references/semantic/support-matrix.jsonl`
- `references/semantic/coverage-ledger.jsonl`
- `references/semantic/assurance-manifest.json`
- `references/reviews/review-attestations.jsonl`

The assurance manifest binds every file, the selected-scope coverage claim, the
original review-plan hash, and the item-to-promoted-entity map. It explicitly sets
`whole_book_completeness_claimed=false`. Legacy packages remain valid under their
original protocol but cannot be relabeled v0.5 without rebuilding and fresh review.

Use `--legacy-semantic-review` on `semantic_workpack.py build` only for compatibility
tests or an intentional legacy continuation. That output cannot claim v0.5 semantic
assurance.

```bash
python3 scripts/semantic_workpack.py promote \
  --source /absolute/path/to/source.pdf \
  --workpack /absolute/path/to/workpack \
  --output /absolute/path/to/empty-draft-skill \
  --name <domain-expert> \
  --display-name "<Domain Expert>" \
  --domain <domain-slug>
```

Promotion copies neither PDF text slices nor page renders. It preserves sanitized
semantic/visual review records and their fingerprints for audit, blocks 21-word
verbatim source windows, and outputs a source-required, reference-only package with
`status=draft`, `decision_support=false`, and `executable=false`. Do not change those
capabilities merely to make a publication gate pass.
