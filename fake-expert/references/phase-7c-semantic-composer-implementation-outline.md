# Phase 7C — Semantic Composer v0.9.0 implementation outline

Status: approved by the user's Phase 7C request; implementation record dated 2026-08-22

## 1. Purpose and confirmed boundaries

Phase 7C adds a deterministic, source-attributed semantic alignment candidate
layer across independently compiled technical-source modules. It is an
adjacent workpack/sidecar contract: it does not flatten child packages, rewrite
child objects, create a source-free consensus, resolve contradictions, author
review attestations, or increase runtime capability.

The real evidence run is deliberately outside this compiler tree. Its exact
source/package identities, bounded scope, visual locator, and counts belong in
the independent Phase 7C report under the repository `docs/` directory and in
ignored local workspaces. The compiler contract itself remains domain-neutral:
legacy source/evidence inputs are labelled `legacy-source-support` /
`evidence-only`, never `atomic-supported`, and never pass a current
atomic-support promotion gate. A failed applicability gate in any external
workpack remains fail-closed and is not bypassed.

## 2. Goals

1. Introduce a centralized v0.9.0 release/protocol registry and immutable new
   schema identities without changing historical protocol strings.
2. Emit a deterministic sidecar/workpack that keeps source-local concepts,
   aliases/terms/symbols/units, relation candidates, definition/assumption/
   applicability/failure-condition differences, numeric/unit/symbol conflicts,
   evidence/support bindings, review requirements, state, and receipts separate
   from child packages.
3. Bind every alignment identity to both source SHA-256 values, package and
   workpack identities, and exact object/atomic assertion/support/evidence IDs.
   New-PDF evidence additionally binds 1-based physical page, printed-label
   candidate, bbox/locator, content hash, parser/version, and visual
   render/crop hashes when used.
4. Provide deterministic normalization, explicit alias maps, dimension-safe unit
   conversion candidates, and explainable ranking. Lexical similarity is only a
   candidate signal; it never becomes semantic equivalence.
5. Preserve `preserve-separate-no-consensus`: all equivalence, broader/narrower,
   related, and contradiction rows remain proposed relationships until the
   required independent review and explicit conflict disposition occur.
6. Add build/validate and pause-only plan/status/resume surfaces, plus Phase 7B
   DAG node/edge propagation for Composer, alignment, review, contradiction, and
   unit-map changes.
7. Validate a local second-source pilot with native-text-first IR and a bounded
   candidate workpack, report exact evidence outside the compiler release, and
   stop at the independent-review gate when no real independent reviewer is
   available.

## 3. Non-goals

- No automatic semantic equivalence, contradiction resolution, merge, consensus,
  source-free synthesis, capability promotion, package sealing, or execution.
- No modification of child packages or any v0.8.0-or-earlier private release.
- No whole-book semantic completeness claim; a real pilot may use a deliberately
  bounded scope when its frozen native-text IR covers only that scope. Any
  exception to the ordinary 10–30-page planning range is recorded in the
  external pilot report, not embedded in the compiler release.
- No automatic reviewer attestation or reviewer identity proof. Validation can
  detect only unregistered/self-review, hash mismatch, stale input, replay, and
  blanket-pattern conditions; reviewer identity remains
  `host-orchestrator-recorded-not-cryptographic`.
- No model/cloud/network/OCR/dependency download, external upload, signing,
  credential access, or formula execution.
- No use of the legacy Ch6 package as a current atomic-support source.

## 4. Recommended architecture and data flow

```text
immutable child package + bounded PDF IR/workpack
                  |
       source/package identity audit
                  |
      normalized local concept records
                  |
       deterministic candidate composer
       /       |          |          \
  aliases  relations  differences  unit maps
       \       |          |          /
          evidence/support bindings
                  |
          review plan + receipts
                  |
        validate -> pause at review
```

The new `semantic_composer.py` is standard-library-first and does not depend on
the semantic promotion writer. It reads a child Expert Skill as an immutable
source-attributed view and reads a bounded new-book candidate workpack/JSONL
adapter. It writes only a Composer directory. A Composer artifact contains no
source text, page images, or child-package copies.

The existing Phase 5 `compose_expert_skills.py` remains the immutable-module
federated composer. Phase 7C alignment is a separate candidate layer and may
reference a future promoted composite, but cannot authorize or perform that
promotion.

## 5. Normative records and stable identity

Add these schema identities under `assets/schemas/`:

- `tkc.semantic-composer-workpack/v0.1` — manifest, source/package levels,
  policy, counts, state, receipts, and explicit promotion boundary;
- `tkc.semantic-concept-ref/v0.1` — source-local object/assertion/term/symbol/
  unit reference with exact package/workpack bindings;
- `tkc.semantic-alignment-candidate/v0.1` — candidate relation, score signals,
  normalized terms, and review requirement;
- `tkc.semantic-difference/v0.1` — definition/assumption/applicability/
  failure-condition/numeric/symbol/unit differences;
- `tkc.semantic-conflict/v0.2` — source-attributed numeric/unit/symbol/
  dimensional or applicability conflict candidate;
- `tkc.semantic-evidence-binding/v0.1` — exact source-local evidence/support
  bindings, including physical/printed page and visual receipts where present;
- `tkc.semantic-review-plan/v0.1` — required reviewer cardinality and pending
  independent review state; it is not an attestation;
- `tkc.semantic-composer-receipt/v0.1` — deterministic step/input/output
  receipt, never a review or promotion receipt.

Every cross-source candidate ID includes both source SHA-256 values, both
package/workpack identities, and the exact left/right local IDs. Local IDs are
never inferred from titles or row order. New-PDF evidence rows include physical
page, printed-label candidate, bbox/locator, content hash, parser/version, and
render/crop hashes when visual evidence is in scope.

## 6. Deterministic algorithms

- Normalize Unicode NFKC, case-fold, whitespace, punctuation, Greek/math aliases,
  and explicitly configured domain aliases. Store raw source notation only as a
  bounded local reference, not as a compiler-release payload.
- Generate alias/term/symbol/unit candidates with an explicit alias map and
  dimension table. Unit conversion is a candidate transformation only when both
  dimensions are known and compatible; incompatible dimensions emit a conflict.
- Generate relation candidates from exact normalized aliases, shared explicit
  symbols/units, compatible typed assertions, and bounded lexical similarity.
  Rank signal tuples deterministically by strength and stable IDs. Never turn a
  lexical match into `equivalent` automatically.
- Compare definition text hashes, assumptions, applicability, failure
  conditions, numeric tokens, unit dimensions, and symbols. Differences are
  source-attributed rows; conflicts are not auto-resolved.
- Deduplicate only identical candidate identities. Preserve same-name/different-
  meaning and different-name/same-meaning as distinct candidate cases.

## 7. Fail-closed and review policy

The validator must fail or isolate rows for missing source/package/workpack,
source drift, missing object/atomic/support/evidence bindings, missing new-PDF
locator/printed-page/parser/visual receipts, symbol collision, incompatible
dimension, applicability/assumption/failure mismatch, review omission/replay,
automatic consensus/merge, and capability escalation.

Legacy evidence-only inputs may produce concept refs, evidence bindings,
alignment candidates, differences, conflicts, and a review plan, but
`promotion_eligible=false` and `atomic_support_required=true` are mandatory.
Atomic-supported inputs still require fresh cross-source review before any future
promotion. Ordinary alias/concept candidates require one independent reviewer;
relations, conflicts, numeric/unit/symbol mappings, applicability, assumptions,
failure conditions, and any formula-related mapping require two. The compiler
can write the plan and pending slots only; it cannot write attestations.

## 8. Phase 7B integration

Extend the DAG node inventory with `composer`, `alignment_candidate`,
`semantic_difference`, `composer_conflict`, `unit_map`, `composer_review_plan`,
and `composer_receipt`. Add typed dependencies from both source/package graphs to
the Composer root and fan-out from source/object/assertion/support/evidence,
unit-map, review-plan/session/attestation, protocol, and capability changes.
Each such change must require recompose/re-align/re-review closure; no stale
alignment can be reused from surviving rows alone.

The standalone `semantic_composer.py` CLI provides `plan`, `status`, and `resume`
with the same replay arguments as `validate`; this is the Phase 7C orchestration
surface for this release. Resume may validate and record deterministic preparation
only, then pauses at `independent-structure-review-required`,
`independent-cross-source-review-required`, contradiction disposition, promotion,
seal, and capability gates. Pipeline-orchestrator integration is not required for
the v0.9.0 acceptance gate and is not claimed here.

## 9. Acceptance and evidence

- New and mutated JSON/JSONL records validate deterministically with stable issue
  codes; all required negative cases are covered by `unittest`.
- Source order permutations produce byte-identical outputs. Same-name/different-
  meaning, different-name/same-meaning, symbol collision, compatible conversion,
  incompatible dimension, applicability conflict, missing support, source drift,
  consensus attempt, review missing/unregistered/self-review/hash-mismatch/
  stale/replayed/blanket-pattern cases, and DAG propagation all fail closed or
  remain explicit candidates as specified.
- The external real-pilot report records source hash/page count, the exact
  normalized-IR scope, concept/alignment/difference/conflict/evidence/pending
  counts, visual locator status, and the precise independent-review gate. It
  never promotes or creates an executable/ready Skill from an unreviewed pilot.
- The new private compiler release is source-free/offline-only, independently
  extractable/verifiable, fixed-timestamp reproducible, and privacy-inventoried.
  v0.8.0 and earlier release hashes remain unchanged.

## 10. Implementation sequence

1. Confirm baseline, backup, and immutable old-release hashes.
2. Add schemas, version identities, and deterministic Composer core with negative
   tests first.
3. Add bounded PDF/package adapters, review plan/validator, CLI, and orchestrator
   pause-only commands.
4. Add Phase 7B DAG node/edge propagation and tests.
5. Update Skill routing, quality/promotion/composition/version docs, README, and
   AGENTS to match implemented behavior.
6. Run complete unittest, system skill quick_validate, release verifier,
   fixed-timestamp rebuild, and privacy inventory.
7. Run the real second-book candidate workflow and report its exact fail-closed
   gate; never add its PDF/workpack/content to the compiler release.
