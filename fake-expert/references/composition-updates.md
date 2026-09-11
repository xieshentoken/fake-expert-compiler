# Composition and incremental updates

## Contents

- [Composition boundary](#composition-boundary)
- [Compose immutable modules](#compose-immutable-modules)
- [Validate and query](#validate-and-query)
- [Generate an update report](#generate-an-update-report)
- [Incremental Build DAG](#incremental-build-dag)
- [Current limitations](#current-limitations)

## Composition boundary

Phase 5 composition is a federated reference layer, not a semantic merge. Accept at
least two published `source-required` Expert Skills whose capabilities are exactly
`reference=true`, `decision_support=false`, and `executable=false`. Copy each child
package byte-for-byte under `modules/<package-id>/`, preserve its own manifest and
seal, and seal the complete composite tree separately.

Require globally unique object, evidence, relation, conflict, and gap IDs. Require
globally unique module IDs. Reject same-source scopes that overlap or omit a bounded
physical-page scope; an independent semantic review must resolve those cases before
composition. Reject symlinks, non-regular files, embedded PDFs, unsealed children,
and child capability escalation.

The composition policy is `preserve-separate-no-consensus`. A query may route to
multiple modules, but the consuming Agent must attribute each claim to the returned
package, object, and evidence IDs. It must not rewrite module claims into a new
source-free consensus.

## Compose immutable modules

Use an explicit timestamp so the build is reproducible:

```bash
python3 scripts/compose_expert_skills.py \
  --package /absolute/path/to/ready-reference-a \
  --package /absolute/path/to/ready-reference-b \
  --name composed-domain-expert \
  --display-name "Composed Domain Expert" \
  --domain composed-domain \
  --version 0.5.0 \
  --created-at 2026-08-13T12:00:00+00:00 \
  --output /absolute/path/to/empty-composite
```

The output uses `tkc.composite-skill/v0.1`. `composition-lock.json` binds every
child package ID, version, manifest SHA-256, integrity SHA-256, module registry,
source registry, capability map, and relative path. The top manifest binds the lock
and every regular file below the composite root, including child manifests.

## Validate and query

```bash
python3 scripts/validate_composite_skill.py \
  /absolute/path/to/composite --level publish

python3 /absolute/path/to/composite/scripts/query_composite.py \
  --query "bounded technical question"
```

Treat the generated query script as read-only retrieval infrastructure, not an
executable domain procedure. A returned `cross_module_consensus_forbidden` reason
means that multiple modules matched; read and report those claims separately.

## Generate an update report

Compare two versions of the same canonical Expert Skill without modifying either:

```bash
python3 scripts/diff_expert_skills.py \
  --old /absolute/path/to/old-ready-skill \
  --new /absolute/path/to/new-ready-skill \
  --generated-at 2026-08-13T13:00:00+00:00 \
  --output /absolute/path/to/update-report.json
```

The report emits added, changed, removed, and unchanged IDs for sources, modules,
objects, evidence, relations, conflicts, gaps, procedures, decisions, and competency
tests. It also emits unresolved conflicted object IDs, affected objects/modules, and
the runtime, evaluation, seal, and host-certification artifacts that must be rebuilt.

Fail `safe_for_incremental_rebuild` when a stable ID silently changes its semantic
or evidence identity, or when a capability changes from false to true. Adding and
removing correctly re-identified objects is safe to schedule, but the report does
not itself rebuild, seal, source-verify, or recertify the new package.

## Current limitations

- The deterministic composer cannot discover semantic contradictions between
  different sources. Module isolation prevents silent reconciliation but does not
  replace independent cross-source semantic review.
- Same-source overlapping scopes are rejected, not automatically deduplicated.
- Phase 5 currently composes only reference-only, `source-required` packages.
- Incremental rebuild execution remains a later gate; the current differ computes
  the invalidation closure and detects stable-ID violations.
- A child host certificate does not certify the composite package or its adapter.

## Incremental Build DAG

Phase 7B (`0.8.0-incremental-build-dag`) extends, rather than replaces, the
Phase 5 differ. `diff_expert_skills.py` still emits the compatible
`tkc.incremental-update-report/v0.1`; the DAG absorbs that classification as an
advisory compatibility view and adds content-addressed nodes and typed edges for
source/page/segment/module/parser/model/config/render/locator, visual objects,
relations/grids, semantic assertions/support, review plan/session/attestation,
formula AST/tests, runtime/competency, seal, host certificate, and composite
layers.

Build or compare only immutable old/new snapshots. A valid DAG directory binds
`dag-manifest.json`, `nodes.jsonl`, `edges.jsonl`, and their canonical hashes.
`change-set.json` records root causes and every downstream path; each
`invalidation-plan.json` row includes required actions and its final gate. A
node is reusable only when content, semantic/evidence identity, protocol,
inputs, dependencies, reviewer requirement, capability map, and receipts are
identical. Missing nodes/receipts, tampering/replay, dangling/cyclic edges,
source identity changes, reviewer narrowing, and capability escalation fail
closed.

The orchestrator's `incremental-plan/status/resume` commands bind both snapshot
DAG hashes. Resume may record only local deterministic extraction/rebuild
receipts. OCR, model launch, structure/visual/semantic review, competency,
sealing, execution, host certification, signing, and Composer promotion remain
pause-only gates. The DAG is a rebuild plan and tamper-evident audit record; it
does not prove semantic truth, write reviews, sign artifacts, or authorize an
in-place update.

## Phase 7C source-attributed Semantic Composer

The Phase 7C Composer is intentionally adjacent to the federated Phase 5
composer. It consumes immutable reference-package views and a bounded
candidate-source spec, then writes a sidecar/workpack rather than a composite
package. Concept refs retain source-local labels, aliases, explicitly typed
symbols/numbers/units, definition and boundary hashes, evidence bindings, and
the source assurance level. A legacy package without the current Phase 6A
assertion/support graph is labelled `legacy-source-support` and can contribute
only evidence-level candidates; it cannot satisfy an atomic-support promotion
gate.

Every alignment/difference/conflict/unit-map identity includes the two source
hashes, package/workpack identities, local concept-ref IDs and hashes, exact
evidence binding IDs, and any atomic assertion/support hashes. Candidate PDF
evidence additionally carries the 1-based physical page, printed-label
candidate, content/page hashes, parser/version, page geometry, top-left PDF
coordinate-space bbox, and page-specific render/crop hashes. A frozen IR is
replayed from safe role-labelled relative paths and verified with
`verify_pdf_ir(..., docling_allow_network=False)`.

Deterministic normalization may rank explicit alias/term signals and perform
dimension-compatible unit conversion candidates. It may not turn lexical
overlap into equivalence, extract technical numbers from administrative page
ranges or prose, infer symbols from ordinary words, resolve contradictions, or
write a source-free consensus. Differences/conflicts/review items are computed
only for eligible explanatory pairs, not the full cross-product. Their evidence
binding sets must equal the exact union of both source-local concept refs.

The standalone `semantic_composer.py` supports `build`, `validate`, `plan`,
`status`, and `resume`. Status/resume can replay the same package/source/spec/
IR/render bindings as validation; with structural and source replay intact but
reviews missing they return a pending/paused state, never `ready`. The review
plan is not an attestation: the compiler records no reviewer conclusion. The
runtime can detect unregistered/self-review, hash mismatch, stale/replayed
attestations, and blanket patterns, but reviewer identity remains
`host-orchestrator-recorded-not-cryptographic`.
