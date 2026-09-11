# Phase 6A — Semantic Assurance v0.5.0 implementation outline

Status: approved for implementation on 2026-08-21  
Implementation session: `<host-recorded-session>`  
Rollback snapshot: `.backups/20260821-145555-phase6a-semantic-assurance-prechange`

Read this document before changing semantic proposal, review, promotion, package
validation, versioning, or private release behavior. This is the approved Phase 6A
scope. Extend the current pipeline; do not replace the existing contracts with an
unrelated compiler, graph framework, or packaging workflow.

## 1. Change purpose

`fake-expert` v0.4.0 already proves deterministic PDF intake, source/render/parser
binding, independent review fragments, reference-tier publication, same-source
composition, update-impact reporting, formula-only execution gates, and private
source-free release verification. Its remaining P0 weakness is semantic assurance:
the current review record accepts or rejects a complete object/relation/conflict/gap
as one item, but does not prove that every numeric value, unit, symbol, assumption,
applicability boundary, failure condition, derivation step, visual fact, or formula
AST node is individually traceable and reviewed.

Phase 6A shall make those semantic atoms machine-addressable and fail-closed without
claiming that hashes or schemas replace independent source inspection. The release
must continue to describe reviewer identity as
`host-orchestrator-recorded-not-cryptographic` until a later signing phase.

The target outcome is a private `fake-expert` v0.5.0 compiler Skill that can produce
new Book Expert Skills with:

- atomic semantic assertions;
- assertion-to-evidence support matrices;
- risk-tiered reviewer attestations;
- explicit scope coverage accounting;
- deterministic promotion and validation of those artifacts;
- a resumable local orchestration surface that cannot bypass human/agent review;
- legacy verification for existing v0.4.0-era packages without silently upgrading
  their semantic assurance level.

## 2. Scope and non-goals

### In scope

1. A breaking semantic-assurance protocol for newly promoted packages.
2. New JSON/JSONL schemas, deterministic validators, stable issue codes, fixtures,
   and negative tests.
3. Integration into semantic workpack preparation, review merge, deterministic
   promotion, package validation, reference-tier build, execution eligibility, and
   private compiler release inventory.
4. A coverage ledger for every semantic unit in the selected compilation scope.
5. A local `plan`, `status`, and `resume` orchestrator for deterministic stages. It
   must pause at proposal/review boundaries and must never author or auto-accept a
   review.
6. Explicit legacy/new protocol detection. Existing sealed Chapter 6/7 packages and
   v0.4.0 release artifacts remain immutable.
7. Documentation, version-matrix, `AGENTS.md`, `README.md`, Skill routing, full tests,
   deterministic v0.5.0 packaging, and standalone release verification.

### Out of scope

- Cross-book semantic consensus or automatic contradiction resolution.
- Whole-book completeness claims. Phase 6A accounts for a selected scope only.
- General shell/Python/kernel execution or `decision_support=true`.
- Cryptographic reviewer identity, CA integration, or signed in-toto layouts.
- New cloud services, network OCR/VLM, model downloads, or data egress.
- Real PaddleOCR/Docling quality certification. Keep the existing
  `uncertified-real-model-quality` limitation unless a separately authorized local
  model benchmark is actually run.
- Editing any sealed/certified artifact already under `output/`.

## 3. Normative artifact contracts

The final field layout may be refined during implementation when required by an
existing invariant, but the following semantics are mandatory. All JSON hashes use
the project's canonical JSON normalization. IDs must be deterministic and collision
checked.

### 3.1 `semantic-assertions.jsonl`

Introduce `tkc.semantic-assertion/v0.2`. Each row represents one atomic semantic
claim and binds it to its parent proposal or promoted entity.

Required assertion kinds:

- `proposition`
- `numeric`
- `unit`
- `symbol`
- `assumption`
- `applicability`
- `failure_condition`
- `derivation_step`
- `visual_fact`

Each assertion must carry, directly or through immutable references:

- deterministic assertion ID and parent item reference;
- origin: `source-explicit`, `source-paraphrase`, or `compiler-derived`;
- normalized semantic payload while preserving source notation where applicable;
- evidence/source reference IDs;
- dependency assertion IDs for derived claims;
- risk tier;
- content hash and current proposal/workpack binding.

Do not store unrestricted source excerpts in canonical source-required Skills.
Ephemeral workpacks may retain bounded text/crops under the existing local-only and
non-shareable rules.

### 3.2 `support-matrix.jsonl`

Introduce `tkc.support-matrix/v0.1`. It must provide a deterministic, complete map
from each assertion to the evidence required to support it. Supported bindings
include:

- evidence anchor/span;
- physical page and span/bbox/crop/render hashes when applicable;
- numeric token and unit token positions;
- symbol declaration/usage;
- assumption, applicability, and failure-condition source support;
- formula AST node paths;
- derivation dependencies;
- visual task/receipt references.

The matrix must distinguish at least `explicit`, `paraphrase`, `derived`, `visual`,
and `contradicted` support. A derived assertion requires a complete derivation chain;
it must never be relabeled as source-explicit. `partial` support may remain in a
draft/quarantine record but cannot satisfy promotion.

### 3.3 `review-attestations.jsonl`

Introduce `tkc.review-attestation/v0.1`. Attestations are created only from external
review fragments and must bind:

- review session and review-plan hashes;
- reviewer and proposer instances;
- reviewed item/assertion IDs and their exact hashes;
- source/render artifacts actually inspected;
- inspection checks and per-item observations;
- differences or corrections found, including an explicit no-difference record;
- verdict, stable issue codes, rationale, and confidence;
- attestation level fixed to
  `host-orchestrator-recorded-not-cryptographic`.

Risk-tier coverage rules:

- Definitions and ordinary concepts require at least one independent reviewer.
- Equations, relations, conflicts, applicability, failure conditions, derivations,
  and visual facts require at least two distinct independent reviewers.
- An executable formula additionally requires an independent math-test author who
  differs from the proposer and all semantic reviewers.

Distinct reviewer strings alone do not prove a human identity. Validation proves
only host-recorded separation, fresh inputs, exact coverage, and non-replay.

### 3.4 `coverage-ledger.jsonl`

Introduce `tkc.coverage-ledger/v0.1`. Every semantic unit/segment in the selected
scope must have exactly one disposition:

- `promoted`
- `context-only`
- `non-knowledge`
- `rejected`
- `gap`
- `quarantined`

Rows bind the source scope, physical pages, unit/segment IDs, proposal hash, linked
object/assertion/gap IDs, and rationale. The gate requires 100% `accounted` units,
not 100% promoted text and not a whole-book completeness claim.

### 3.5 Canonical generated-Skill layout

Newly promoted v0.5.0-protocol Book Expert Skills must carry sanitized, source-free
semantic assurance artifacts under a stable `references/` subtree. Choose one
canonical layout and enforce it in schemas, promoter, validator, runtime catalog,
seal, package, and tests. Workpack-only text, page renders, crops, reviewer fragment
source files, hidden expectations, and absolute paths must not enter the package.

## 4. Deterministic validation and issue codes

Add deterministic recomputation rather than trusting self-declared booleans. At
minimum, implement and test these stable hard failures where applicable:

- `assertion_untraced`
- `numeric_token_untraced`
- `unit_untraced`
- `symbol_untraced`
- `assumption_added`
- `assumption_omitted`
- `applicability_overbroad`
- `failure_condition_omitted`
- `formula_node_untraced`
- `quote_source_mismatch`
- `quote_hash_mismatch`
- `bbox_page_mismatch`
- `crop_hash_mismatch`
- `support_matrix_incomplete`
- `review_difference_missing`
- `review_attestation_invalid`
- `review_attestation_replay`
- `reviewer_not_independent`
- `reviewer_coverage_insufficient`
- `coverage_ledger_incomplete`
- `legacy_semantic_protocol_not_promotable`

If an existing stable code already expresses the same failure, reuse it rather than
creating a synonym. Do not validate semantic truth with regex alone: deterministic
checks establish traceability, completeness, internal consistency, and bounded
scope; reviewer attestations remain responsible for natural-language entailment.

## 5. Integration plan

### 5.1 Schemas and version registry

- Add the new schemas under `assets/schemas/`.
- Centralize all new release/protocol strings in `scripts/compiler_version.py` and
  document them in `references/version-matrix.md`.
- Bump the private compiler release to `0.5.0` only after the complete gate passes.
- Do not silently replace `tkc.semantic-review/v0.1`; detect legacy packages and
  validate them under their original contract.

### 5.2 Semantic workpack and review flow

- Extend schema help for all new record kinds.
- Generate assertion/support/coverage scaffolds without inventing semantic content.
- Freeze their hashes in the review plan.
- Extend external fragment merge and provenance manifests for multiple reviewers.
- Enforce risk-tier cardinality and exact assertion coverage.
- Preserve current anti-blanket, fresh-source, fresh-render, quarantine, and rollback
  behavior.

### 5.3 Promotion and package validation

- Promote only fully supported assertions whose required reviewer coverage passes.
- Maintain exact bidirectional object/assertion/evidence/review mappings.
- Copy only sanitized compact attestations into the generated Skill.
- Force `decision_support=false` and `executable=false` unless the existing dedicated
  execution tier independently proves the executable formula contract.
- Extend structural/publication/source-verification gates to recompute new files and
  reject deletion, substitution, stale hashes, orphan rows, or capability escalation.

### 5.4 Coverage and orchestration

- Add a resumable local orchestrator with explicit `plan`, `status`, and `resume`
  operations and a deterministic job manifest.
- It may call or describe existing deterministic stages, but must stop with a clear
  `next_action` when proposal or independent review is required.
- It must not launch paid/live model runs, write reviewer fragments, set `ready`, set
  `passed`, set `executable=true`, or download/install dependencies.
- Re-running a completed deterministic stage with unchanged inputs must be
  byte-identical or report that the stage is already complete.

### 5.5 Legacy compatibility

- Existing v0.4.0 compiler release, Chapter 6/7 packages, composite, certifications,
  and test fixtures remain immutable.
- Current validators must continue to verify supported legacy sealed artifacts.
- A legacy package cannot be represented as v0.5.0-semantic-assured without an
  explicit migration that regenerates assertions/matrices, obtains fresh required
  reviews, re-runs competency, and reseals.

## 6. Expected code and documentation surface

Inspect callers before editing; the exact split may change, but expect work in:

- `assets/schemas/semantic-assertion.schema.json`
- `assets/schemas/support-matrix.schema.json`
- `assets/schemas/review-attestation.schema.json`
- `assets/schemas/coverage-ledger.schema.json`
- a focused semantic-assurance helper under `scripts/`
- a resumable pipeline orchestrator under `scripts/`
- `scripts/semantic_workpack.py`
- `scripts/semantic_promotion.py`
- `scripts/expert_skill_contract.py`
- `scripts/executable_formula_tier.py`
- `scripts/reference_tier.py` and sealing/packaging code where integrity coverage
  changes
- `scripts/compiler_version.py`
- new focused unittest files and adversarial fixtures
- `SKILL.md`, `references/semantic-promotion.md`, `references/quality-gates.md`,
  `references/compilation-workflow.md`, `references/version-matrix.md`
- project `README.md`, `AGENTS.md`, and a Phase 6A implementation report under
  `docs/`

Do not merge or replace the project with Claude's `compile.py`, `package.py`,
`grade.py`, or `executable.py`.

## 7. Test strategy

Use `unittest`, not pytest. Keep the existing suite passing and add positive,
negative, deterministic, and tamper tests.

### Positive fixtures

- At least 10–20 representative assertion records spanning all assertion kinds.
- Definition/concept with one reviewer.
- Equation/relation/applicability/failure/derivation/visual facts with two reviewers.
- A formula whose independent test author is distinct from proposer and reviewers.
- Complete selected-scope coverage ledger.
- Legacy sealed package verification.
- Orchestrator pause/resume/idempotence.

Gold fixtures may reuse sanitized, already approved Chapter 6/7 package identities
and anchors, but tests must not edit those output packages or falsely claim a fresh
human review. Synthetic/adversarial fixtures must be labeled as such.

### Negative fixtures

Mutate at least:

- numeric value, sign, exponent, unit, symbol;
- assumption, applicability, failure condition;
- formula AST node/path;
- source span/page/bbox/crop/render hash;
- assertion/support/review deletion or substitution;
- duplicated reviewer, self-review, missing second reviewer, replayed attestation;
- missing/duplicated coverage unit;
- legacy package presented as new-protocol assured;
- orchestrator attempt to skip the review pause.

Each mutation must fail closed with a stable, asserted issue code. Avoid tests that
only match documentation text.

## 8. Main risks and controls

### False semantic confidence

Risk: a valid hash graph may be described as proof that a claim is true.  
Control: separate deterministic assurance from reviewer entailment; preserve honest
attestation labels and explicit limitations in manifests, reports, and Skill text.

### Atomicization drift

Risk: propositions are split too finely or too coarsely, changing stable identity.  
Control: deterministic canonicalization, Gold fixtures, explicit supersession rather
than silently retaining an ID after semantic drift.

### Reviewer theater

Risk: different strings or blanket fragments simulate independence.  
Control: bound sessions/input hashes, external fragment provenance, differentiated
observations, exact per-risk coverage, replay checks, and proposer/reviewer/test-author
separation.

### Compatibility break

Risk: a new schema invalidates v0.4.0-era real packages.  
Control: explicit legacy dispatch, immutable old outputs, regression verification,
and no implicit assurance upgrade.

### Source leakage

Risk: new matrices or attestations copy book text/crops into distributed Skills.  
Control: allow raw evidence only in ignored local workpacks; run source-free inventory
and verbatim-window checks on promoted packages and compiler releases.

### Dependency and portability growth

Risk: adding graph/validation frameworks prevents reuse on Hermes, WorkBuddy, or
Claude Code.  
Control: keep the normative verifier standard-library-first; do not add mandatory
RDF, graph database, DVC, cloud, or OCR-model dependencies.

## 9. Acceptance targets

Phase 6A is complete only when all of the following are demonstrated:

1. The current v0.4.0 standalone private-release verifier still reports
   `status=verified`.
2. All existing 114 tests pass unchanged or with justified compatibility updates;
   all new Phase 6A tests also pass.
3. Every promoted new-protocol object/relation/conflict/gap has complete atomic
   assertion, support, review, and reverse-evidence coverage.
4. Unbound numeric, unit, symbol, assumption, applicability, failure-condition, and
   formula-node counts are zero for accepted Gold fixtures.
5. Risk-tier reviewer cardinality and executable test-author independence are
   recomputed, not trusted from self-declared fields.
6. All listed adversarial mutations fail closed with stable issue codes.
7. The selected-scope coverage ledger is 100% accounted, with gaps/quarantine kept
   explicit and no whole-book completeness claim.
8. Identical inputs and explicit timestamps produce byte-identical deterministic
   artifacts.
9. The orchestrator pauses at proposal/review boundaries, resumes safely, and cannot
   bypass promotion, competency, seal, or execution gates.
10. Existing sealed outputs remain byte-identical and valid under their legacy
    protocol.
11. The v0.5.0 private compiler release is source-free, deterministic, safely
    extractable, fully inventory-locked, and verified by its standalone verifier.
12. `AGENTS.md`, `README.md`, version matrix, Skill routing, Phase 6A report, and
    receiver documentation state actual verified behavior and all remaining gaps.

If any acceptance item is unproven, do not label the release complete. Report the
specific blocked gate and keep the release candidate outside the final v0.5.0 path.

## 10. Required implementation sequence

1. Confirm the rollback snapshot and re-run the v0.4.0 verifier plus full baseline
   tests before editing.
2. Freeze schemas, protocol identities, legacy-dispatch policy, and hard issue codes.
3. Write negative tests first, then implement deterministic assertion/support
   validation.
4. Implement reviewer attestation coverage and workpack fragment integration.
5. Integrate deterministic promotion and generated-Skill validation.
6. Implement coverage ledger and the review-stopping orchestrator.
7. Run focused tests, full tests, legacy artifact verification, source-free inventory,
   and deterministic rebuild checks.
8. Update documentation only to match observed results.
9. Build `output/private-releases/fake-expert/v0.5.0` without overwriting prior
   releases and run its standalone verifier.
10. Return a concise implementation report listing changed files, issue codes, test
    totals, release inventory/hash, remaining uncertified capabilities, and rollback
    location.
