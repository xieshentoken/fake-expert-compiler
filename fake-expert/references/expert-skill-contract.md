# Expert Skill contract v0.1

## Canonical layout

```text
<domain>-expert/
├── SKILL.md
├── manifest.json
├── agents/
│   └── openai.yaml                 # optional Codex adapter
├── adapters/                       # optional non-Codex adapters
├── references/
│   ├── knowledge/
│   │   ├── index.json
│   │   ├── relations.jsonl
│   │   └── objects/*.json
│   ├── evidence/
│   │   ├── source-manifest.json
│   │   ├── anchors.jsonl
│   │   └── promotion-report.json   # when produced by semantic promotion
│   ├── reviews/                    # sanitized hash-bound review audit
│   │   ├── review-plan.json
│   │   ├── semantic-records.jsonl
│   │   └── visual-receipts.jsonl
│   ├── runtime/
│   │   ├── catalog.json            # deterministic object/conflict/gap projection
│   │   └── reference-map.md        # human-readable ID router
│   ├── evaluations/
│   │   ├── evaluation-plan.json
│   │   ├── runtime-responses.jsonl
│   │   └── competency-receipts.jsonl
│   ├── procedures/index.json
│   ├── decisions/index.json
│   ├── conflicts.jsonl
│   ├── knowledge-gaps.jsonl
│   └── competency-tests.jsonl
├── scripts/                        # optional executable capability
└── assets/                         # optional licensed source/evidence assets
```

The portable `scripts/query_reference.py` is read-only retrieval infrastructure, not
a domain procedure and not evidence of `executable=true`.

Keep the root `SKILL.md` concise. Route the agent to the smallest relevant reference
instead of loading an entire book or knowledge module.

## Manifest identity

Use schema ID `tkc.expert-skill/v0.1`. Declare:

- package ID, display name, semantic version, domain, and lifecycle status;
- source distribution and verification level;
- source IDs and SHA-256 fingerprints;
- module IDs, versions, exports, and dependencies;
- runtime capabilities;
- compiler/schema versions and input fingerprint;
- integrity hashes for every shareable file except `manifest.json` itself.

Use lifecycle status `draft`, `ready`, `deprecated`, or `revoked`. Only `ready` may
pass the publication gate.

## Stable identity

Derive source IDs from source SHA-256. Derive segment and object IDs from normalized
source identity, semantic locator, object type, and normalized content. Preserve IDs
across updates only when both meaning and evidence scope remain equivalent.

Never use a mutable title or array position as the sole identity input.

## Source modes

| Mode | Package contains | Verification consequence |
| --- | --- | --- |
| `source-required` | source hash and locators | Full verification requires the matching external source. |
| `evidence-pack` | source hash plus licensed excerpts/crops | Included claims can be verified from the pack. |
| `full` | complete source with explicit rights | Full package verification is self-contained. |

Default to `source-required`. Record usability and verification separately: an agent
may use compiled knowledge without the PDF, but must not claim independent source
verification when the matching PDF is unavailable.

## Knowledge objects

Require each exported object to contain:

- stable `id`, `type`, `title`, and normalized `statement`;
- `source_ids` and one or more `evidence_ids`;
- assumptions, validity conditions, and failure conditions when applicable;
- extraction and interpretation confidence;
- review status.

Phase 2 promoted objects may also carry proposal origin, structured applicability,
separated formula representations, and a hash-bound review record. The review
attestation label must remain `host-orchestrator-recorded-not-cryptographic` unless a
future identity provider supplies stronger evidence.

Phase 2 relations, conflicts, and package-level gaps also carry stable IDs and the
same review-binding fields. `promotion-report.json` must map every accepted object,
relation, conflict, and gap review to exactly one canonical entity, while retaining
rejected and quarantined records. Removing or swapping any of those mappings is a
structural error.

Use typed objects such as `Definition`, `Concept`, `Claim`, `Equation`, `Assumption`,
`Exception`, `Method`, `Procedure`, and `DecisionRule`. Add domain-specific types only
through a versioned schema extension.

## Evidence anchors

Require each anchor to include:

- anchor ID and source ID;
- physical PDF page index and printed page label when available;
- semantic segment ID;
- structural locator such as chapter/section/equation/figure label;
- SHA-256 of the normalized evidence text or bounded asset;
- IDs of supported knowledge objects.

A page number alone is not sufficient evidence identity.

For a source-required Phase 2 package, declare the bounded physical/printed source
scope in the manifest. Exact object-anchor links must be bidirectional in both
directions. Package-level gaps use their own exact, externally re-verifiable source
spans and must remain `status=unresolved`; unit-local observations resolved elsewhere
stay in the review audit and do not enter `knowledge-gaps.jsonl`.

## Composition

Treat one source compilation as a knowledge module. Compose modules by explicit
exports and dependency requirements. Namespace IDs globally, retain each module's
source lineage, and create a conflict object when claims cannot be reconciled.

Phase 5 uses a federated composite layout instead of flattening child records:

```text
<composite-expert>/
├── SKILL.md
├── manifest.json                 # tkc.composite-skill/v0.1
├── composition-lock.json         # exact child manifest/integrity bindings
├── agents/openai.yaml
├── references/runtime/catalog.json
├── scripts/query_composite.py
└── modules/<package-id>/          # byte-preserved sealed Expert Skill
```

Require at least two ready, `source-required`, reference-only children. Reject
global identifier collisions, duplicate module IDs, unreviewed same-source overlap,
symlinks, non-regular files, embedded PDFs, and child seal drift. The composite's
top-level seal covers child manifests and all other regular files; each child seal
also remains independently valid.

Do not rewrite multiple claims into a source-free consensus statement.
Record conflicting source statements or internal equation inconsistencies in
`references/conflicts.jsonl`, keep each claim's evidence IDs, and state whether the
runtime action is unresolved, mitigated, or resolved.

## Update semantics

Record source hash, compiler version, schema version, prompt version, model identity,
and build time. On update:

1. compare fingerprints;
2. identify affected source segments;
3. invalidate dependent objects, syntheses, tests, and procedures;
4. rebuild only the affected dependency closure;
5. emit added, changed, removed, conflicted, and unchanged IDs;
6. seal and rerun publication validation.

Use `tkc.incremental-update-report/v0.1` to bind both manifest and integrity hashes,
classify source/module/object/evidence/relation/conflict/gap/procedure/decision/test
IDs, report unresolved conflicted objects, and enumerate the invalidated dependency
closure. Treat a semantic or evidence change under an unchanged stable ID, and every
false-to-true capability change, as unsafe for incremental rebuild.

## Executable capability

Keep reference knowledge and executable code separate. An executable procedure must
declare inputs, outputs, preconditions, permissions, risk tier, deterministic entry
point, failure behavior, evidence IDs, and whether human review is required.

Never convert source code or shell commands into auto-executing behavior merely
because they appeared in a technical document.

## Reference runtime competency

For compiler version `0.3.0-phase3` (see `version-matrix.md` for the identity
string registry), require `tkc.competency-test/v0.2` tests with no
embedded result. Bind the exact test suite, runtime catalog, canonical query script,
stored responses, and deterministic evaluator version in an evaluation plan. Require
one recomputable response and receipt per test. Publication must fail when a test,
catalog, response, runtime script, or receipt is missing, stale, duplicated, or
self-declared.

Machine-readable schemas are stored in `assets/schemas/`.

## Host certification sidecar

Do not place host-forward certification inside the canonical layout or integrity
seal. Store it as an adjacent distribution artifact:

```text
output/certifications/<package-id>/<host-certificate>/
├── certification.json
├── expectation-commitment.json
├── challenges.jsonl
├── expectations.jsonl
├── runs.jsonl
└── receipts.jsonl
```

Use the `tkc.host-*/v0.1` schemas and bind the exact package manifest, integrity map,
adapter, and runtime hashes. A certification sidecar is invalid if any file is added,
removed, stale, or fails deterministic recomputation. Its attestation level remains
`host-orchestrator-recorded-not-cryptographic` unless a future identity provider
supplies stronger evidence.
