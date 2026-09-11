# Version matrix

Single registry for every version identity used by the fake-expert compiler Skill.
All scripts import these strings from `scripts/compiler_version.py`; no script may
maintain its own copy. Update this file and `scripts/compiler_version.py` together.

## Release version

| Constant | Value | Meaning |
|----------|-------|---------|
| `RELEASE_VERSION` | `1.2.0` | Formal private fake-expert compiler Skill product release. It retains source-free queryable `fake-*` Reference Drafts and adds a hash-bound mandatory-reading route, machine-checked documentation contract, unified UX hardening, and Codex negative certification. Historical v1.1.0 and earlier archives remain immutable. |
| `READONLY_COMPATIBLE_SOURCE_CANDIDATE_VERSIONS` | `0.14.1` | Historical source-candidate manifests without an artifact-stage field remain readable for audit only; they are never formal releases and are not newly emitted. |

The historical v1.2 UX candidate remains identifiable, while new jobs use the formal line:

| Constant | Value | Meaning |
|----------|-------|---------|
| `UX_SOURCE_CANDIDATE_VERSION` | `1.2.0-ux` | Read-only identity for the historical UX candidate; it is not emitted by the v1.2.0 unified CLI |
| `UX_COMPILER_VERSION` | `1.2.0` | Current unified compiler/job identity; it is a compiler product identity, not a knowledge/promotion identity |
| `UX_CLI_PROTOCOL` | `fake-expert-ux-cli-v0.2` | Human/agent-facing command and progress transport including explicit mandatory-reading output |
| `FAKE_EXPERT_JOB_SCHEMA` | `tkc.fake-expert-job/v0.2` | Closed canonical-JSON private job contract with source/workpack/tool/runtime hashes, explicit profile/backend selection, optional runtime receipt, false policy flags, and ordered hash-bound `required_reading` records |
| `FAKE_EXPERT_JOB_PROTOCOL` | `fake-expert-job-v0.2` | Hash-bound plan/compile/status/resume pause contract; v0.1 jobs require explicit re-plan and are never silently rewritten |

The historical `1.2.0-ux` candidate directories remain candidate-only. Only the
fresh release directory containing a `release-verified` manifest, checksums,
standalone verifier and exact-package Codex certification is formal v1.2.0.

Formal v1.2.0 emits `tkc.fake-expert-release/v0.4` and
`tkc.fake-expert-compiler-codex-certification/v0.1`. The release schema requires
an exact-archive Codex certification sidecar for `release-verified`. Historical
v1.1.0 emits `tkc.fake-expert-release/v0.3`. Both schemas require an explicit
artifact stage and closes the two valid shapes: candidates require
`source_candidate_identity` and forbid `mvp_acceptance`; formal releases require
`mvp_acceptance` and forbid `source_candidate_identity`. The acceptance scope
also records `unreviewed_output_usable=true`, explicit private
`draft-distributable` packaging, and the unchanged Review/promotion boundary.

The Phase 7D.6A implementation uses the next non-conflicting source-candidate
identity `0.14.4-scan-mvp-contract-hardening`. It is not a formal release and it
does not change `RELEASE_VERSION`; formal private v0.14.0 remains the immutable
release line.

## Protocol identities stamped into compiled artifacts

These strings identify the compiler protocol that produced a sealed artifact. They
are stable by design: validators compare manifest fields against them, so changing
a value invalidates every existing sealed package. A new protocol identity is
introduced only when the emitted artifact shape changes.

| Constant | Value | Stamped where |
|----------|-------|---------------|
| `DIRECT_REFERENCE_COMPILER_VERSION` | `1.1.0-direct-reference` | New source-free, queryable `fake-*` Reference Draft manifests and control sidecars; never a promotion identity |
| `DIRECT_REFERENCE_PROTOCOL` | `direct-reference-draft-v0.1` | Deterministic build/extend/hash-only-Gold lifecycle with Review deferred and decision/execution disabled |
| `DIRECT_REFERENCE_SCHEMA` | `tkc.direct-reference/v0.1` | Closed candidate assurance, workpack/source binding, additive update, and optional Gold sidecar contract |
| `PHASE3_COMPILER_VERSION` | `0.3.0-phase3` | Reference-tier package `build.compiler_version` (`reference_tier.py`), accepted by `expert_skill_contract.py` |
| `EXECUTION_COMPILER_VERSION` | `0.4.0-execution` | Executable-tier package `build.compiler_version` (`executable_formula_tier.py`) |
| `COMPOSITE_COMPILER_VERSION` | `0.5.0-phase5` | Composite lock and composite package `compiler_version` (`composite_contract.py`) |
| `SCAN_PDF_COMPILER_VERSION` | `0.14.0-scan-pdf` | Candidate-only scanned-PDF OCR adapter and v0.2 scan IR (`scanned_pdf_ocr.py`); host orchestration fields are excluded from the hash-bound engine configuration |
| `SEMANTIC_ASSURANCE_COMPILER_VERSION` | `0.5.0-semantic-assurance` | Newly promoted Book Expert Skill `build.compiler_version` when the atomic assertion/support/review/coverage gate passes |
| `SEMANTIC_ASSURANCE_PROTOCOL` | `atomic-support-review-v2` | Workpack, review plan, promoted assurance manifest, release runtime contract, and resumable pipeline review boundary |
| `PDF_IR_SCHEMA_VERSION` | `tkc.pdf-ir/v0.3` | Hash-locked PDF IR with heading candidate/resolution receipts; legacy v0.1/v0.2 inputs remain explicitly labeled compatibility inputs |
| `HEADING_LOCATOR_PROTOCOL` | `heading-locator-v0.2` | Candidate/resolution/ bounded-occurrence override protocol consumed directly by semantic workpacks |
| `WHOLE_BOOK_PLANNER_COMPILER_VERSION` | `0.6.0-whole-book-structure-coverage` | Whole-book page/segment/module planning compiler identity |
| `WHOLE_BOOK_PLANNER_PROTOCOL` | `whole-book-structure-coverage-v0.2` | Book plan, layered coverage ledger, deterministic module plan, review/OCR routing, and book-level status/resume |
| `VISUAL_SEMANTICS_COMPILER_VERSION` | `0.7.0-visual-semantics` | Candidate-only visual object/relation/table/conflict normalization and visual assurance runtime contract |
| `VISUAL_SEMANTICS_PROTOCOL` | `visual-semantics-v0.1` | Parser-independent visual object, relation, table-grid, conflict, provenance, and candidate status boundary |
| `VISUAL_OBJECT_SCHEMA` | `tkc.visual-object/v0.1` | Stable visual object JSONL rows |
| `VISUAL_RELATION_SCHEMA` | `tkc.visual-relation/v0.1` | Typed visual relation JSONL rows |
| `TABLE_GRID_SCHEMA` | `tkc.table-grid/v0.1` | Candidate table rows/columns/cells and binding candidates |
| `VISUAL_CONFLICT_SCHEMA` | `tkc.visual-conflict/v0.1` | Visual parser/locator/type/overlap/duplicate/missing conflict ledger |
| `VISUAL_REVIEW_ATTESTATION_SCHEMA` | `tkc.visual-review-attestation/v0.2` | External fragment-only object-level full-page/crop/render/bbox/input review attestation |
| `VISUAL_ASSURANCE_MANIFEST_SCHEMA` | `tkc.visual-assurance-manifest/v0.1` | Source-free visual artifact inventory with permanent whole-book false boundary |
| `VISUAL_REVIEW_PROTOCOL` | `visual-review-attestation-v0.2` | External visual reviewer fragment merge protocol |
| `INCREMENTAL_BUILD_DAG_COMPILER_VERSION` | `0.14.0-incremental-build-dag` | Content-addressed typed build DAG including selective visual and runtime-qualification nodes |
| `INCREMENTAL_BUILD_DAG_PROTOCOL` | `incremental-build-dag-v0.2` | Immutable snapshot comparison, typed dependency edges, reuse eligibility, invalidation receipts, and fail-closed gates |
| `INCREMENTAL_DAG_MANIFEST_SCHEMA` | `tkc.incremental-dag-manifest/v0.2` | DAG manifest and canonical node/edge inventory bindings |
| `INCREMENTAL_DAG_NODE_SCHEMA` | `tkc.incremental-dag-node/v0.2` | Content-addressed source/page/segment/module/parser/render/visual/semantic/runtime-result/review node rows |
| `INCREMENTAL_DAG_EDGE_SCHEMA` | `tkc.incremental-dag-edge/v0.2` | Typed dependency edge rows with endpoint hash identities |
| `INCREMENTAL_CHANGE_SET_SCHEMA` | `tkc.incremental-change-set/v0.2` | Root causes, typed closure paths, action/gate invalidations, reuse decisions, and safety violations |
| `INCREMENTAL_BUILD_PLAN_SCHEMA` | `tkc.incremental-build-plan/v0.2` | Hash-bound pause-only incremental rebuild plan |
| `INCREMENTAL_BUILD_STATE_SCHEMA` | `tkc.incremental-build-state/v0.2` | Idempotent local resume state and immutable plan binding |
| `INCREMENTAL_BUILD_RECEIPT_SCHEMA` | `tkc.incremental-build-receipt/v0.2` | Deterministic local-step receipt; never a review, seal, certificate, or executable claim |
| `SEMANTIC_COMPOSER_COMPILER_VERSION` | `0.9.0-semantic-composer` | Phase 7C source-attributed semantic alignment candidate compiler identity |
| `SEMANTIC_COMPOSER_PROTOCOL` | `semantic-composer-v0.1` | Cross-source candidate normalization, alignment, difference, conflict, unit-map, review-plan, and pause-only state boundary |
| `COMPOSER_WORKPACK_SCHEMA` | `tkc.semantic-composer-workpack/v0.1` | Source-attributed Composer sidecar/workpack manifest and artifact inventory |
| `COMPOSER_INPUT_SCHEMA` | `tkc.semantic-composer-input/v0.1` | Bounded local candidate-source concept/evidence input contract |
| `CONCEPT_REF_SCHEMA` | `tkc.semantic-concept-ref/v0.1` | Source-local package/workpack concept reference with exact object/assertion/support/evidence bindings |
| `ALIGNMENT_CANDIDATE_SCHEMA` | `tkc.semantic-alignment-candidate/v0.1` | Deterministic cross-source relationship candidate; never semantic equivalence proof |
| `DIFFERENCE_SCHEMA` | `tkc.semantic-difference/v0.1` | Source-attributed definition/assumption/applicability/failure/numeric/symbol/unit difference |
| `COMPOSER_CONFLICT_SCHEMA` | `tkc.semantic-conflict/v0.2` | Candidate numeric/unit/symbol/dimension/applicability conflict with preserve-separate policy |
| `EVIDENCE_BINDING_SCHEMA` | `tkc.semantic-evidence-binding/v0.1` | Exact package/workpack evidence and physical/printed/locator/render/crop bindings |
| `UNIT_MAP_SCHEMA` | `tkc.semantic-unit-map/v0.1` | Dimension-checked unit normalization/conversion candidate |
| `COMPOSER_REVIEW_PLAN_SCHEMA` | `tkc.semantic-review-plan/v0.1` | Required independent cross-source reviewer slots and pending state; no attestation |
| `COMPOSER_REVIEW_ATTESTATION_SCHEMA` | `tkc.semantic-composer-review-attestation/v0.1` | External-only candidate review attestation input; compiler cannot author it |
| `COMPOSER_RECEIPT_SCHEMA` | `tkc.semantic-composer-receipt/v0.1` | Deterministic Composer step/input/output receipt, never a review or promotion receipt |
| `COMPOSER_REVIEW_PROTOCOL` | `cross-source-review-v0.1` | Independent cross-source review boundary with host-recorded-not-cryptographic identity |
| `VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION` | `0.14.12-visual-parser-calibration` | Phase 7D.6D source-candidate identity; retains the v0.4 orchestrator contract and adds calibration-frozen concise chart-label evidence before native plot creation |
| `VISUAL_PARSER_ORCHESTRATOR_PROTOCOL` | `visual-parser-orchestrator-v0.4` | Plan/build/validate/status/resume boundary for native, selective visual, and explicitly routed local candidate parsers with runtime worker contracts |
| `VISUAL_PARSER_JOB_SCHEMA` | `tkc.visual-parser-job/v0.4` | Closed runtime job contract including hash-bound visual mode/kinds/budgets/context; v0.3 remains read-only compatibility input |
| `VISUAL_PARSER_MANIFEST_SCHEMA` | `tkc.visual-parser-manifest/v0.4` | Hash-bound source-free component and selective visual sidecar manifest; v0.3 remains read-only compatibility input |
| `VISUAL_EVALUATION_CONTRACT_SCHEMA` | `tkc.visual-evaluation-contract/v0.1` | Explicit synthetic-contract truth versus real candidate-benchmark evaluation semantics |
| `VISUAL_REVIEW_OVERLAY_SCHEMA` | `tkc.visual-review-overlay/v0.4` | Strict source-free candidate review routing overlay including first-class result/candidate/gap/conflict/cell receipt hashes; no attestation authoring or promotion |
| `PADDLE_RUNTIME_PROFILE_SCHEMA` | `tkc.paddle-runtime-profile/v0.2` | Explicit text/table profiles plus legacy chart-v1 and qualified chart-v2 constructor/predict bindings |
| `PADDLE_RUNTIME_QUALIFICATION_PLAN_SCHEMA` | `tkc.paddle-runtime-qualification-plan/v0.1` | Private host-local plan binding one crop fixture, exact runtime contract, resource limits, and timeout classes |
| `PADDLE_RUNTIME_QUALIFICATION_SCHEMA` | `tkc.paddle-runtime-qualification/v0.1` | Source-free host-local runtime/profile/repeatability qualification receipt; never Gold or review evidence |
| `PADDLE_RUNTIME_QUALIFICATION_PROTOCOL` | `paddle-runtime-qualification-v0.1` | Three-repeat, single-model-process, warning-blocked crop-only qualification protocol |
| `PADDLE_RUNTIME_QUALIFICATION_COMPILER_VERSION` | `0.14.0-paddle-runtime-qualification` | Qualification plan/receipt compiler identity; plans bind the qualification script SHA-256 and reject tool drift |
| `READONLY_COMPATIBLE_SCAN_PDF_COMPILER_VERSIONS` | `0.12.0-scan-pdf` | Explicit read-only legacy scan IR input; never silently rewritten or treated as the current producer identity |
| `READONLY_COMPATIBLE_QUALIFICATION_TOOL_SHA256` | `d1d9b23090e8a99d7104b8e4b978330c22eaddbb5d8f964fc14f66c8bb21d842` | One immutable prior qualification tool hash accepted only for validating preserved old receipts; new receipts use the current tool hash |
| `PADDLE_RESULT_ADAPTER_SCHEMA` | `tkc.ppstructure-v3-result/v0.1` | Fail-closed PP-StructureV3 3.7 result adapter with raw/normalized result hashes and table topology candidates |
| `PADDLE_RESULT_ADAPTER_PROTOCOL` | `ppstructure-v3-result-adapter-v0.1` | Official result-field normalization boundary for layout, OCR, table HTML, cell boxes, and conflict/gap routing |
| `PADDLE_OCR_RESULT_SCHEMA` | `tkc.ppocrv6-result/v0.1` | Strict PP-OCRv6 text result adapter with input/output/response commitments; always candidate-only |
| `PADDLE_OCR_RESULT_PROTOCOL` | `ppocrv6-result-adapter-v0.1` | Direct crop/page-image PP-OCRv6 text observation normalization boundary |
| `SCANNED_PDF_STRUCTURE_REVIEW_SCHEMA` | `tkc.scanned-pdf-structure-review/v0.1` | Bounded external review of title/page-range/order/bbox and exact scan-evidence bindings |
| `SCANNED_PDF_STRUCTURE_REVIEW_PROTOCOL` | `scanned-pdf-structure-review-v0.1` | Candidate → independent confirmation → resolved bounded scan structure; no whole-book or Gold claim |
| `SCAN_MVP_CONTRACT_HARDENING_COMPILER_VERSION` | `0.14.4-scan-mvp-contract-hardening` | Phase 7D.6A source-candidate identity for exact object-scoped scan evidence, runtime/worker commitments, independent review binding, and candidate/release stage disambiguation; not a formal release |
| `SCAN_ANCHOR_PROTOCOL` | `scan-anchor-v0.2` | Source-free per-knowledge-object scan anchor with exact evidence spans, per-page OCR row/observation/BBox/render/transform commitments, and exact runtime/worker binding |
| `SCAN_SPLIT_CHECKPOINT_COMPILER_VERSION` | `0.14.5-scan-split-checkpoint` | Phase 7D.6C source-candidate identity for explicit rotated-spread split/read-order contracts and page/region checkpoint reuse; not a formal release |
| `SCANNED_PDF_SPLIT_SCHEMA` | `tkc.scanned-pdf-spread-split/v0.1` | Hash-bound physical-page spread geometry, explicit content rotation, left-to-right region order, candidate/null printed-page labels, crop hashes/dimensions, and PDF-coordinate transforms |
| `SCANNED_PDF_SPLIT_PROTOCOL` | `scanned-pdf-spread-split-v0.1` | Fail-closed two-region spread split contract; no orientation auto-detection and no automatic printed-page confirmation |
| `SCANNED_PDF_CHECKPOINT_SCHEMA` | `tkc.scanned-pdf-checkpoint/v0.1` | Private page/region checkpoint containing complete reusable receipts or paused pending/error items plus exact source/render/split/runtime/config/qualification bindings |
| `SCANNED_PDF_CHECKPOINT_PROTOCOL` | `scanned-pdf-checkpoint-v0.1` | Deterministic checkpoint/resume protocol that revalidates every binding, reuses only complete regions, and never triggers review/promotion/Gold/seal/sign/release |
| `PADDLE_RUNTIME_SPLIT_QUALIFICATION_PLAN_SCHEMA` | `tkc.paddle-runtime-split-qualification-plan/v0.1` | 7D.6C qualification plan shape that adds explicit split-contract/core/region bindings while leaving the original qualification plan schema unchanged |
| `PADDLE_RUNTIME_SPLIT_QUALIFICATION_SCHEMA` | `tkc.paddle-runtime-split-qualification/v0.1` | 7D.6C source-free qualification receipt shape bound to the rotated spread contract; never Gold or review evidence |
| `PADDLE_RUNTIME_SPLIT_QUALIFICATION_PROTOCOL` | `paddle-runtime-split-qualification-v0.1` | Three-repeat crop-only qualification protocol with explicit split-contract binding; old v0.1 qualification receipts remain read-only compatible |
| `PADDLE_RUNTIME_SPLIT_QUALIFICATION_COMPILER_VERSION` | `0.14.5-scan-split-checkpoint` | 7D.6C compiler identity for split-bound qualification plans/receipts; the original qualification compiler identity remains unchanged for old receipts |
| `SCANNED_PDF_SPLIT_GEOMETRY_COMPILER_VERSION` | `0.14.6-scan-gold-review` | 7D.6C-C pixel-edge-first split mapping repair for legal odd/even raster dimensions; v0.1 split contracts remain read-only compatible |
| `SCANNED_PDF_SPLIT_GEOMETRY_SCHEMA` | `tkc.scanned-pdf-spread-split/v0.2` | Hash-bound split contract whose exact rotated-render pixel edges are authoritative and mapped back to PDF points without integer re-rounding; an inset source CropBox remains recorded while the physical-page Context and split use the complete MediaBox render |
| `SCANNED_PDF_SPLIT_GEOMETRY_PROTOCOL` | `scanned-pdf-spread-split-v0.2` | Deterministic pixel-edge-first split/reversible mapping protocol; no DPI workaround and no mutation of 7D.6B/R4 evidence |
| `SCAN_GOLD_REVIEW_COMPILER_VERSION` | `0.14.7-scan-gold-coverage` | Phase 7D.6C-D private Portable Scan Gold Review coverage repair and reviewer UI; candidate-only, not a formal release |
| `SCAN_GOLD_REVIEW_PROTOCOL` | `scan-gold-review-v0.2` | Explicit-identity, hash-bound independent structure/OCR Gold handoff with stratified full-region samples, audited batch pass, external attestation, and fail-closed finalize/validate/evaluate |
| `SCAN_GOLD_REVIEW_PROFILE_SCHEMA` | `tkc.scan-gold-review-profile/v0.2` | Frozen structure/full-region OCR sampling/evaluation/privacy profile; acceptance thresholds remain targets and cannot authorize accuracy |
| `SCAN_GOLD_REVIEW_PLAN_SCHEMA` | `tkc.scan-gold-review-plan/v0.2` | Private source/render/split/geometry/stratified-page/item/asset plan for a Portable Scan Gold Review workbench |
| `SCAN_GOLD_REVIEW_WORKBENCH_MANIFEST_SCHEMA` | `tkc.scan-gold-review-workbench-manifest/v0.2` | Immutable private workbench manifest with local assets, explicit reviewer/session/proposer identities, and no hidden-result/source-PDF policy |
| `SCAN_GOLD_REVIEW_SUBMISSION_SCHEMA` | `tkc.scan-gold-review-submission/v0.2` | Browser-exported human structure/full-region OCR decisions, coverage/exclusions, batch-confirm/batch-pass audit, and input hashes |
| `SCAN_GOLD_REVIEW_ATTESTATION_SCHEMA` | `tkc.scan-gold-review-attestation/v0.2` | External-only reviewer attestation bound to the complete workbench/submission/item/input hashes and proposer separation |
| `SCAN_GOLD_REVIEW_STRUCTURE_GOLD_SCHEMA` | `tkc.scan-gold-review-structure-gold/v0.2` | Private per-physical-page rotation/split/order/label/readability/abnormal Gold row; accepted rows remain unverified and release-excluded |
| `SCAN_GOLD_REVIEW_OCR_GOLD_SCHEMA` | `tkc.scan-gold-review-ocr-gold/v0.2` | Private full-region canonical text/excluded row with explicit denominator coverage; accepted rows remain unverified and release-excluded |
| `SCAN_GOLD_REVIEW_REVISION_SCHEMA` | `tkc.scan-gold-review-revision/v0.2` | Hash-bound private paused or accepted-for-evaluation revision; no automatic vote/merge/promotion/executable/release state |
| `SCAN_GOLD_REVIEW_PREDICTION_SCHEMA` | `tkc.scan-gold-review-prediction/v0.2` | Separate evaluator input contract bound to the same workbench/profile/revision; predictions are never shown to the reviewer |
| `SCAN_GOLD_REVIEW_EVALUATION_SCHEMA` | `tkc.scan-gold-review-evaluation/v0.2` | Deterministic structure exact/OCR CER-WER/numeric-token/coverage/excluded evaluation; incomplete or overlapping identities yield null metrics |
| `CONTROLLED_SCAN_DERIVATION_COMPILER_VERSION` | `0.14.13-controlled-scan-derivation` | Phase 7D.6E private qualification utility that converts an exact bounded full-MediaBox render set into a deterministic image-only PDF; not a production intake route or formal release |
| `CONTROLLED_SCAN_DERIVATION_PROTOCOL` | `controlled-image-only-scan-derivation-v0.1` | Source/page/render/embedded-pixel/text-layer/re-render-fidelity and optional reviewed-visual-evidence binding; never OCR, Gold rewriting, promotion, or MVP acceptance |
| `CONTROLLED_SCAN_DERIVATION_SCHEMA` | `tkc.controlled-scan-derivation/v0.1` | Private derivation manifest with original-to-derived physical-page map, identity BBox transform, exact embedded raster hashes, Poppler fidelity metrics, and fail-closed policy fields |
| `SCAN_STRUCTURE_REVIEW_WORKBENCH_COMPILER_VERSION` | `0.14.14-scan-structure-review-workbench` | Phase 7D.6E-C private offline transport over an existing Scan IR; it re-renders only candidate heading pages and never runs OCR or authors review |
| `SCAN_STRUCTURE_REVIEW_WORKBENCH_PROTOCOL` | `scan-structure-review-workbench-v0.1` | Browser submission, exact candidate/input/plan bindings, separate external attestation, and projection into the existing scanned-PDF structure fragment |
| `SCAN_STRUCTURE_REVIEW_WORKBENCH_SCHEMA` | `tkc.scan-structure-review-workbench/v0.1` | Hash-bound private workbench with structure candidates, page renders, reviewer/session/proposer identities, and permanent no-promotion policy |
| `SCAN_STRUCTURE_REVIEW_SUBMISSION_SCHEMA` | `tkc.scan-structure-review-submission/v0.1` | Browser-exported per-segment title/page/order/BBox/evidence confirmations plus rationale, difference observation, and declarations |
| `SCAN_STRUCTURE_REVIEW_ATTESTATION_SCHEMA` | `tkc.scan-structure-review-external-attestation/v0.1` | External-only attestation bound to the exact workbench plan, candidates, input hashes, proposer/reviewer separation, and inspected facts |
| `SCAN_STRUCTURE_REVIEW_REVISION_SCHEMA` | `tkc.scan-structure-review-revision/v0.1` | Paused zero-fragment or accepted external-fragment revision; never Gold, promotion, release, or MVP acceptance |
| `SCAN_STRUCTURE_MATERIALIZER_COMPILER_VERSION` | `0.14.15-scan-structure-materializer` | Phase 7D.6E-C offline projection utility that replays an accepted external structure fragment into a fresh reviewed Scan IR without changing the OCR compiler/checkpoint |
| `SCAN_STRUCTURE_MATERIALIZER_PROTOCOL` | `scan-structure-materializer-v0.1` | Exact source/Scan-IR/proposal/fragment replay; no OCR, model, MCP, network, promotion, release, or MVP acceptance |
| `SEMANTIC_REVIEW_WORKBENCH_COMPILER_VERSION` | `0.14.16-semantic-review-workbench` | Private offline UI/finalizer for the frozen atomic semantic-review queue; it never authors reviewer judgments or completes the multi-reviewer gate alone |
| `SEMANTIC_REVIEW_WORKBENCH_PROTOCOL` | `semantic-review-workbench-v0.1` | Per-reviewer isolation, answer-free evidence display, browser submission import/export, canonical validation, and projection into the existing semantic attestation fragment |
| `SEMANTIC_REVIEW_WORKBENCH_SCHEMA` | `tkc.semantic-review-workbench/v0.1` | Source/workpack/plan/reviewer/item/data/asset-bound private workbench manifest with explicit no-prediction/no-network policy |
| `SEMANTIC_REVIEW_WORKBENCH_SUBMISSION_SCHEMA` | `tkc.semantic-review-workbench-submission/v0.1` | Reviewer-owned verdict/support/rationale/confidence/check/difference form data bound to the exact workbench and frozen assertion bundle |
| `SEMANTIC_REVIEW_WORKBENCH_REVISION_SCHEMA` | `tkc.semantic-review-workbench-revision/v0.1` | Immutable single-reviewer fragment receipt; always records that the independent multi-reviewer gate remains incomplete until normal merge/replay |

For the 7D.6C-C follow-up, these existing identities retain their versions while
their bundled JSON contracts are tightened to the actual emitted fields:
top-level and key nested objects are closed with `additionalProperties=false`,
the plan's structure/OCR item alternatives are locally validated, and the
profile flag is the plural `test_results_cannot_change_thresholds`. The split
v0.2 schema is based on the strict v0.1 object shape and adds only the v0.2
identity/pixel-mapping policy; page geometry, render, split, region, and
coordinate-transform objects are not widened to untyped objects. This is schema
and fail-closed validation hardening, not a release-version change.
| `PADDLE_RUNTIME_PRIVATE_DIAGNOSTIC_SCHEMA` | `tkc.paddle-runtime-private-diagnostic/v0.1` | 0600 host-local exception/line-stderr diagnostics; never part of canonical receipts or releases |
| `PADDLE_RUNTIME_PROTOCOL` | `paddle-runtime-contract-v0.2` | Allowlisted external interpreter/runtime/model/worker contract, explicit profile, fixed JSONL crop-only execution, inventory and network gates |
| `PADDLE_RUNTIME_CONTRACT_SCHEMA` | `tkc.paddle-runtime-contract/v0.2` | Frozen external Paddle runtime/model inventory contract; private workpack metadata only |
| `SCAN_IR_SCHEMA` | `tkc.scanned-pdf-ir/v0.2` | Scan IR with first-class visual candidates, gaps/conflicts, result adapters, and table-grid bindings |
| `OCR_OBSERVATION_SCHEMA` | `tkc.pdf-ocr-observation/v0.2` | Candidate OCR observation with current scan IR binding; legacy v0.12 protocol inputs remain explicitly labeled |
| `OCR_BACKEND_RECEIPT_SCHEMA` | `tkc.pdf-ocr-backend-receipt/v0.2` | Backend/result/model/runtime receipt with raw and normalized result hashes |
| `OCR_PROTOCOL_SCHEMA` | `tkc.pdf-ocr-jsonl/v0.2` | Fixed crop-only request/response JSONL worker protocol |
| `SELECTIVE_VISUAL_COMPILER_VERSION` | `0.14.0-selective-visual` | Selective no-model census, bounded routing, budget/index, qualified chart candidate, and batch/session compiler identity |
| `SELECTIVE_VISUAL_PROTOCOL` | `selective-visual-pipeline-v0.1` | Closed `off|auto|full` policy, `table|figure|chart|diagram|equation` kinds, context, pause/gap, and candidate-only routing boundary |
| `VISUAL_POLICY_SCHEMA` | `tkc.visual-policy/v0.1` | Hash-bound mode/kind/budget/context policy record |
| `VISUAL_CENSUS_SCHEMA` | `tkc.visual-census/v0.1` | pypdf-native no-model/no-render candidate census |
| `VISUAL_ROUTING_SCHEMA` | `tkc.visual-routing/v0.1` | Explicit candidate/not-requested/not-inspected/budget route rows |
| `VISUAL_BUDGET_RECEIPT_SCHEMA` | `tkc.visual-budget-receipt/v0.1` | Auditable calls/pages/regions/elapsed/bytes/objects receipt with pause status |
| `VISUAL_INDEX_SCHEMA` | `tkc.visual-index/v0.1` | Source-free navigation/provenance index; no raw images/crops/full OCR/paths/credentials |
| `VISUAL_COVERAGE_SCHEMA` | `tkc.visual-coverage/v0.2` | Processed, not-requested, policy-gap, budget-gap, candidate/conflict/review coverage |
| `VISUAL_BATCH_RECEIPT_SCHEMA` | `tkc.visual-batch-receipt/v0.1` | One-model-load bounded crop session receipt and stable error isolation |
| `LEGACY_TECHNICAL_CHART_PROFILE_ID` | `technical-chart-v1` | Read-only v0.13 compatibility profile; never the default for a new v0.14 job |
| `TECHNICAL_CHART_PROFILE_ID` | `technical-chart-v2` | Qualified local PP-Chart2Table candidate route |
| `TECHNICAL_CHART_PROFILE_PROTOCOL` | `technical-chart-v2-local-pp-chart2table-paddle-dynamic` | Explicit CPU, no-HPI, `paddle_dynamic`, batch-size-1 ChartParsing profile |
| `TECHNICAL_CHART_BACKEND` | `paddleocr-chart-parsing` | Crop-only production chart worker backend |
| `TECHNICAL_CHART_API` | `paddleocr.ChartParsing` | Official local PaddleOCR 3.7 chart adapter entry point |
| `TECHNICAL_CHART_MODEL_NAME` / `TECHNICAL_CHART_MODEL_DIR` | `PP-Chart2Table` / `PP-Chart2Table` | Explicit local model name and directory; no default resolver/download |
| `TECHNICAL_CHART_RESULT_SCHEMA` | `tkc.technical-chart-result/v0.1` | Normalized candidate rows/axis/tick/legend/series/unit/annotation result |
| `TECHNICAL_CHART_RESULT_PROTOCOL` | `technical-chart-result-adapter-v0.1` | Observed `{result: <pipe-delimited text>}` adapter boundary |
| `TECHNICAL_CHART_CANDIDATE_SCHEMA` | `tkc.technical-chart-candidate/v0.2` | Source/render/crop/runtime-qualification-bound candidate status and review contract |
| `VISUAL_BENCHMARK_COMPILER_VERSION` | `0.14.1-visual-benchmark` | Phase 7D.5B private candidate benchmark Gold/import, frozen evaluation, and pause-only orchestration layer; no formal release created |
| `VISUAL_BENCHMARK_PROTOCOL` | `visual-benchmark-v0.1` | External-attested Gold handoff, current two-route qualification gate, calibration/test freeze, `off|auto|full` evaluation, and machine-readable plan/status/resume boundary |
| `VISUAL_BENCHMARK_PLAN_SCHEMA` | `tkc.visual-benchmark-plan/v0.1` | Hash-bound Phase 7D.5B job plan; stores only workpack/plan/config commitments and no PDF/render/crop payload |
| `VISUAL_BENCHMARK_GOLD_FRAGMENT_SCHEMA` | `tkc.visual-benchmark-gold-fragment/v0.1` | External reviewer-owned item Gold fragment; the compiler validates/imports it but cannot author it |
| `VISUAL_BENCHMARK_ATTESTATION_SCHEMA` | `tkc.visual-benchmark-review-attestation/v0.1` | External differentiated reviewer attestation bound to item/source/page/render/crop hashes |
| `VISUAL_BENCHMARK_PREDICTION_SCHEMA` | `tkc.visual-benchmark-prediction/v0.1` | Hash-bound private model-output row with separated evaluator identity and runtime cost fields |
| `VISUAL_BENCHMARK_THRESHOLD_CONFIG_SCHEMA` | `tkc.visual-benchmark-threshold-config/v0.1` | Calibration-derived frozen threshold configuration; test rows cannot rewrite it |
| `VISUAL_BENCHMARK_COST_LEDGER_SCHEMA` | `tkc.visual-benchmark-cost-ledger/v0.1` | Private per-mode/split elapsed, memory, model-load, render/crop/output ledger |
| `VISUAL_BENCHMARK_STATE_SCHEMA` | `tkc.visual-benchmark-state/v0.1` | Idempotent pause-only Phase 7D.5B state and gate/status projection |
| `VISUAL_BENCHMARK_EVALUATION_SCHEMA` | `tkc.visual-benchmark-evaluation/v0.1` | Frozen split/mode metric and private runtime/cost receipt; accuracy remains absent until all gates pass |
| `VISUAL_REVIEW_WORKBENCH_COMPILER_VERSION` | `0.14.1-visual-review-workbench` | Offline reviewer-facing instance generator and canonical finalize/validate adapter; private, release-excluded |
| `VISUAL_REVIEW_WORKBENCH_PROTOCOL` | `visual-review-workbench-v0.1` | Local context/crop form transport; it emits the existing Phase 7D.5B Gold fragment rows only after canonical revalidation |
| `VISUAL_REVIEW_WORKBENCH_MANIFEST_SCHEMA` | `tkc.visual-review-workbench-manifest/v0.1` | Locked reviewer instance identity, local assets, and no-split/no-model UI policy |
| `VISUAL_REVIEW_WORKBENCH_SUBMISSION_SCHEMA` | `tkc.visual-review-workbench-submission/v0.1` | Browser-exported human form data plus locked workpack identity; browser does not generate trust hashes |
| `VISUAL_REVIEW_WORKBENCH_REVIEWER_REGISTRY_SCHEMA` | `tkc.visual-review-workbench-reviewer-registry/v0.1` | Finalization registry and 31-item coverage projection for external reviewers |
| `VISUAL_REVIEW_WORKBENCH_ATTESTATION_BUNDLE_SCHEMA` | `tkc.visual-review-workbench-attestation-bundle/v0.1` | Finalization attestation/declaration bundle; not a Gold truth claim or signature |
| `VISUAL_REVIEW_WORKBENCH_REVIEW_MANIFEST_SCHEMA` | `tkc.visual-review-workbench-review-manifest/v0.1` | Immutable/revisioned finalization status and direct-import compatibility receipt |
| `PORTABLE_VISUAL_REVIEW_COMPILER_VERSION` | `0.14.8-single-page-visual-review` | Review-only source-candidate identity for variable-size, single-physical-page visual workpacks; not a formal release |
| `PORTABLE_VISUAL_REVIEW_WORKPACK_PROTOCOL` | `portable-visual-review-workpack-v0.1` | Local native render/crop handoff for actual formula/figure/table/chart candidates; no region split, model inference, Gold authoring, or benchmark authorization |
| `PORTABLE_VISUAL_REVIEW_WORKPACK_MANIFEST_SCHEMA` | `tkc.portable-visual-review-workpack-manifest/v0.1` | Hash-bound source/bundle/item/page identity for a variable candidate count; accepted only by the reviewer path, not the frozen 31-item evaluator |
| `PORTABLE_VISUAL_BENCHMARK_COMPILER_VERSION` | `0.14.9-portable-visual-benchmark` | Private candidate-only evaluator for variable-size portable review workpacks; not a formal release |
| `PORTABLE_VISUAL_BENCHMARK_PROTOCOL` | `portable-visual-benchmark-v0.1` | Replays pre-Gold native parser candidates against accepted Portable Gold while keeping whole-range recall unmeasured |
| `PORTABLE_VISUAL_BENCHMARK_PLAN_SCHEMA` | `tkc.portable-visual-benchmark-plan/v0.1` | Hash-binds workpack, Gold, parser job/backend/config, split, evaluator identity, and no-network/no-model policy |
| `PORTABLE_VISUAL_BENCHMARK_EVALUATION_SCHEMA` | `tkc.portable-visual-benchmark-evaluation/v0.1` | Calibration/test candidate correctness, type, BBox, and table-structure observations with explicit no-recall boundary |
| `PORTABLE_VISUAL_BENCHMARK_MANIFEST_SCHEMA` | `tkc.portable-visual-benchmark-manifest/v0.1` | Immutable private benchmark file hashes and candidate-only policy receipt |
| `PORTABLE_VISUAL_CALIBRATION_REPLAY_COMPILER_VERSION` | `0.14.12-visual-calibration-replay` | Phase 7D.6D additive replay identity: preserves the frozen calibration/test split, scores a changed candidate set, and separately replays bounded recall Gold without granting promotion or release |
| `PORTABLE_VISUAL_RECALL_COMPILER_VERSION` | `0.14.10-portable-visual-recall-review` | Full-page, candidate-blind, single-reviewer visual-object census for a bounded page range; source candidate only |
| `PORTABLE_VISUAL_RECALL_PROTOCOL` | `portable-visual-recall-review-v0.1` | Offline page render review and deterministic same-page/same-kind one-to-one IoU matching against a frozen candidate workpack |
| `PORTABLE_VISUAL_RECALL_WORKBENCH_SCHEMA` | `tkc.portable-visual-recall-workbench/v0.1` | Source/tool/job/workpack/render-bound private workbench paused before independent page census |
| `PORTABLE_VISUAL_RECALL_SUBMISSION_SCHEMA` | `tkc.portable-visual-recall-submission/v0.1` | One independent reviewer submission containing complete page census, object kind, BBox, and declarations |
| `PORTABLE_VISUAL_RECALL_REVISION_SCHEMA` | `tkc.portable-visual-recall-revision/v0.1` | Immutable bounded recall ledger with single-reviewer assurance and permanent no-promotion boundary |
| `PORTABLE_VISUAL_INCREMENTAL_RECALL_COMPILER_VERSION` | `0.14.11-portable-visual-incremental-recall` | Superseding missed-object workbench that locks accepted Portable Gold as baseline and migrates a prior reviewer submission |
| `PORTABLE_VISUAL_INCREMENTAL_RECALL_PROTOCOL` | `portable-visual-incremental-recall-v0.2` | One-reviewer gap check over full-page renders without exposing parser candidates, predictions, confidence, or split labels |
| `PORTABLE_VISUAL_INCREMENTAL_RECALL_WORKBENCH_SCHEMA` | `tkc.portable-visual-incremental-recall-workbench/v0.2` | Tool/source/job/workpack/baseline-Gold/legacy-submission/render-bound incremental workbench |
| `PORTABLE_VISUAL_INCREMENTAL_RECALL_SUBMISSION_SCHEMA` | `tkc.portable-visual-incremental-recall-submission/v0.2` | Reviewer reconfirmation preserving every locked baseline object plus any additional missed objects |
| `PORTABLE_VISUAL_INCREMENTAL_RECALL_REVISION_SCHEMA` | `tkc.portable-visual-incremental-recall-revision/v0.2` | Immutable bounded recall ledger combining accepted content Gold and a single-reviewer additional-object check |
| `VISUAL_REVIEW_CONVENTION_COMPILER_VERSION` | `0.14.2-review-convention` | Phase 7D.5B-R2 source-candidate identity only; does not change `RELEASE_VERSION` or create a release |
| `VISUAL_REVIEW_CONVENTION_PROTOCOL` | `visual-review-convention-v0.2` | Hashable caption/number, type-specific BBox scope, raw/canonical comparison, and fail-closed profile-aware receipt rules |
| `VISUAL_REVIEW_CONVENTION_PROFILE_SCHEMA` | `tkc.visual-review-convention-profile/v0.2` | Canonical JSON profile contract stored under `skills/fake-expert/references/`; its SHA-256 binds profile-aware artifacts |
| `VISUAL_REVIEW_WORKBENCH_PROFILE_PROTOCOL` | `visual-review-workbench-v0.2-profile-bound` | New generated workbench transport identity; reviewer submission export remains the compatible v0.1 schema/protocol |
| `VISUAL_REVIEW_WORKBENCH_PROFILE_MANIFEST_SCHEMA` | `tkc.visual-review-workbench-manifest/v0.2` | Profile-bound reviewer data/manifest with the profile identity; old v0.1 instances remain unbound-compatible |
| `VISUAL_REVIEW_COMPARISON_RECEIPT_SCHEMA` | `tkc.visual-review-convention-comparison-receipt/v0.1` | Private field-level raw/canonical comparison receipt; remaining conflicts stay paused and no Gold is authored |
| `VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION` | `0.14.1-visual-review-adjudication` | Private third-party dispute-set workbench and fail-closed resolution receipt; release-excluded |
| `VISUAL_REVIEW_ADJUDICATION_PROTOCOL` | `visual-review-adjudication-v0.1` | Independent adjudication of frozen reviewer disagreements; no prediction/split/score visibility and no automatic merge |
| `VISUAL_REVIEW_ADJUDICATION_MANIFEST_SCHEMA` | `tkc.visual-review-adjudication-manifest/v0.1` | Hash-bound six-item-or-current dispute workbench, source submission bindings, locked Context/Crop assets, and offline UI policy |
| `VISUAL_REVIEW_ADJUDICATION_SUBMISSION_SCHEMA` | `tkc.visual-review-adjudication-submission/v0.1` | Adjudicator-owned field-level resolution choices, manual values, rationale/observation, declarations, and exact dispute bindings |
| `VISUAL_REVIEW_ADJUDICATION_RECEIPT_SCHEMA` | `tkc.visual-review-adjudication-receipt/v0.1` | Finalization receipt binding the third-party identity, original submissions, dispute/payload hashes, and fail-closed import status |
| `PORTABLE_GOLD_REVIEW_COMPILER_VERSION` | `0.14.3-portable-gold-review` | Phase 7D Portable Gold Review source-candidate identity; it reuses the existing adjudication/Gold wire contracts and does not change `RELEASE_VERSION` |
| `PORTABLE_GOLD_REVIEW_PROTOCOL` | `portable-gold-review-v0.1` | Offline dual-reviewer plus human-curator finalization, bound to a verified Review Convention comparison receipt; no automatic consensus |
| `PORTABLE_GOLD_REVIEW_DECISION_SCHEMA` | `tkc.portable-gold-review-decision/v0.1` | Hash-only per-item curator decision sidecar preserving field source choices, rationale, observation, input bindings, and resolved payload hash |
| `PORTABLE_GOLD_REVIEW_REVISION_SCHEMA` | `tkc.portable-gold-review-revision/v0.1` | Immutable private revision manifest binding reviewer submissions, comparison/adjudication receipts, decision ledger, and import-compatible Gold fragments |

Phase 7D.2 repair note: the existing v0.1 visual-object shape remains
compatible, but canonical IDs are now derived from the full stable visual
commitment. Adapter-local IDs, ephemeral request paths, and runtime timing are
attribution/audit data only; stable request and response-content commitments
remain part of the ID. The repaired release does not reinterpret v0.11.0
artifacts in place.

`PHASE3_COMPILER_VERSION` remains the portable reference-runtime compiler identity.
When `reference_tier.py` consumes a v0.5 semantic-assured draft it preserves the
assurance subtree and package version `0.5.0`, then adds the legacy-compatible
Phase 3 runtime identity. Validators recompute both layers. This does not upgrade a
legacy v0.4-era package: only a workpack that regenerated the atomic artifacts and
obtained fresh risk-tiered attestations may carry the assurance manifest.

## Historical markers in documentation

Documentation may describe when a protocol feature was introduced. Keep such
references explicitly historical ("Since v0.3.2, ..." / "introduced in v0.3.2") and
never write a stale version as if it described the current protocol. Current-state
prose must not carry a release version at all; the protocol identity and the
`tkc.*/vX.Y` schema IDs are the versioned facts that matter.

## Changing versions

1. Edit `RELEASE_VERSION` in `scripts/compiler_version.py` and this table.
2. Never touch the protocol identities unless the artifact contract actually
   changes; if it does, bump the affected constant, update the validators that
   accept it, and document the migration in the phase report.
3. Re-run the full test suite and the release verifier
   (`verify_fake_expert_release.py`) before distributing.
