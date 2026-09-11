# Phase 7B — Incremental Build DAG implementation outline (v0.8.0)

## Purpose

Phase 7B turns the Phase 5 stable-ID impact report into a deterministic,
content-addressed incremental build graph. It compares immutable old/new
snapshots from a Phase 6C plan, PDF-IR, workpack, package inventory, or explicit
source-free fixture; preserves the Phase 5 `tkc.incremental-update-report/v0.1`
classification as a compatibility view; and emits a typed invalidation and
rebuild plan with a reason, dependency path, final action, and gate for every
invalidated node.

The graph has explicit source, page, segment, module, parser, model, config,
render, locator, visual object/relation/grid, semantic assertion/support,
review plan/session/attestation, formula AST/test, runtime, competency, seal,
host certificate, and composite layers. Node IDs are derived from a declared
stable identity and kind. Content, semantic identity, evidence identity,
protocol, input hashes, receipts, and typed edges are separately bound.

## Protocol and outputs

`compiler_version.py` is the only identity registry. Phase 7B adds
`0.8.0-incremental-build-dag`, `incremental-build-dag-v0.1`, and the
`tkc.incremental-* /v0.1` schemas. A DAG directory contains canonical:

- `dag-manifest.json`, `nodes.jsonl`, and `edges.jsonl`;
- `change-set.json` with legacy diff absorption, root causes, typed paths, and
  reuse eligibility;
- `invalidation-plan.json` with ordered actions and explicit gates;
- `state.json` and `receipts.jsonl` for hash-bound, idempotent local resume.

All rows are sorted and serialized with canonical JSON. Manifest, plan, state,
node, edge, and receipt hashes are recomputable. Sealed inputs are never edited.

## Propagation and reuse

Comparison first validates both graphs for endpoint integrity, content hashes,
typed edges, acyclicity, required receipts, and stable identities. It then
classifies added/changed/removed/unchanged nodes and walks the union of old/new
typed downstream edges. Text, render, parser/model/config, locator, visual
relation/grid, assertion/support, review-plan/session, formula AST/test,
compiler, and schema changes therefore invalidate each dependent closure.

Reuse is permitted only for an unchanged node whose content hash, semantic and
evidence identity, protocol, input hashes, dependency closure, reviewer
requirement, capabilities, and required receipts all match. Stable-ID semantic
or evidence drift, source identity changes, capability escalation, narrowed
reviewer requirements, missing original nodes/receipts, dangling/cyclic edges,
tampered/replayed receipts, and input binding changes are fail-closed safety
conditions. A plan remains available as an audit artifact when it is fail-closed;
it is not authorization to promote or reuse data.

## Orchestration

`pipeline_orchestrator.py incremental-plan/status/resume` binds the old/new
snapshot DAG hashes into a job. `resume` may record only deterministic local
`re-extract` and `rebuild` receipts. Structure resolution, OCR, visual review,
semantic review, competency, seal, execution, host certification, signing, and
composer boundaries remain explicit pauses. The state never writes review
attestations or claims `passed`, `ready`, `sealed`, `certificate`, or
`executable=true`; repeating resume with unchanged inputs is byte-idempotent.

## Risks and non-goals

This is not semantic truth, automatic review, a signature, a cryptographic
reviewer identity, or a cross-source Composer. Hashes prove binding and
tamper-evidence only. It does not invoke OCR/models, render or copy a PDF,
author semantic/visual attestations, certify competency, seal/sign an artifact,
or execute a formula. A deterministic rebuild plan must still pass the existing
Phase 6A semantic, Phase 6C whole-book, Phase 7A visual, execution, host, and
private-release gates.

## Acceptance

Acceptance uses ReportLab or in-memory source-free fixtures for byte-determinism,
no-op reuse, page/render/locator/visual/assertion/support/review/formula/test
changes, fan-out, add/delete, cycle/dangling/tamper/replay, capability and
reviewer-policy escalation, pause-only resume, and unchanged-node reuse. Existing
real-book input is accepted only as a read-only no-op or bounded plan; no PDF,
page render, crop, OCR transcript, model, review material, or knowledge workpack
is copied into the compiler release. The prior v0.7.0 and earlier private
releases remain immutable and independently verifiable.
