"""Single version matrix for the fake-expert compiler Skill.

Every script imports its identity strings from here instead of maintaining a
private copy, so release/protocol/compiler versions cannot drift independently.
See `references/version-matrix.md` for the full documentation of each string and
where it is stamped.
"""

from __future__ import annotations

RELEASE_VERSION = "1.2.0"
# Retained only so historical v1.2.0-ux candidate manifests remain
# identifiable. Newly created unified jobs use UX_COMPILER_VERSION.
UX_SOURCE_CANDIDATE_VERSION = "1.2.0-ux"
UX_COMPILER_VERSION = "1.2.0"
UX_CLI_PROTOCOL = "fake-expert-ux-cli-v0.2"
FAKE_EXPERT_JOB_SCHEMA = "tkc.fake-expert-job/v0.2"
FAKE_EXPERT_JOB_PROTOCOL = "fake-expert-job-v0.2"
# Source-free, queryable candidate Reference Skills.  This identity is not a
# promotion identity: it keeps Review deferred, Gold optional, lifecycle at
# draft, and all decision/execution capabilities disabled.
DIRECT_REFERENCE_COMPILER_VERSION = "1.1.0-direct-reference"
DIRECT_REFERENCE_PROTOCOL = "direct-reference-draft-v0.1"
DIRECT_REFERENCE_SCHEMA = "tkc.direct-reference/v0.1"
DIRECT_REFERENCE_RUNTIME_SHA256 = "4373790b353306851ca92debab4dc3e1b5a2c8c64178ade857b79cd99f34f19b"
PORTABLE_REFERENCE_RUNTIME_SHA256 = "50de12e92c840806121d898d1f44d36c57d80e81ad13ef82e08b4417c6ba5581"
# Historical source-candidate manifests without an explicit artifact stage are
# readable for audit only; they are never formal releases.
READONLY_COMPATIBLE_SOURCE_CANDIDATE_VERSIONS = ("0.14.1",)
PHASE3_COMPILER_VERSION = "0.3.0-phase3"
EXECUTION_COMPILER_VERSION = "0.4.0-execution"
COMPOSITE_COMPILER_VERSION = "0.5.0-phase5"
SCAN_PDF_COMPILER_VERSION = "0.14.0-scan-pdf"
# The old scan IR identity is accepted only by the explicit read-only loader.
# It is never emitted, rewritten, or treated as the current formal schema.
READONLY_COMPATIBLE_SCAN_PDF_COMPILER_VERSIONS = ("0.12.0-scan-pdf",)
# The frozen Phase 7D.5A qualification receipts were produced before the
# additive PP-OCRv6 contract extension.  Accept this exact hash only when
# replaying those immutable receipts; newly emitted receipts bind the current
# script hash.
READONLY_COMPATIBLE_QUALIFICATION_TOOL_SHA256 = (
    "d1d9b23090e8a99d7104b8e4b978330c22eaddbb5d8f964fc14f66c8bb21d842",
)
SCANNED_PDF_STRUCTURE_REVIEW_SCHEMA = "tkc.scanned-pdf-structure-review/v0.1"
SCANNED_PDF_STRUCTURE_REVIEW_PROTOCOL = "scanned-pdf-structure-review-v0.1"
SEMANTIC_ASSURANCE_COMPILER_VERSION = "0.5.0-semantic-assurance"
SEMANTIC_ASSURANCE_PROTOCOL = "atomic-support-review-v2"
PDF_IR_SCHEMA_VERSION = "tkc.pdf-ir/v0.3"
HEADING_LOCATOR_PROTOCOL = "heading-locator-v0.2"
WHOLE_BOOK_PLANNER_COMPILER_VERSION = "0.6.0-whole-book-structure-coverage"
WHOLE_BOOK_PLANNER_PROTOCOL = "whole-book-structure-coverage-v0.2"
VISUAL_SEMANTICS_COMPILER_VERSION = "0.7.0-visual-semantics"
VISUAL_SEMANTICS_PROTOCOL = "visual-semantics-v0.1"
VISUAL_OBJECT_SCHEMA = "tkc.visual-object/v0.1"
VISUAL_RELATION_SCHEMA = "tkc.visual-relation/v0.1"
TABLE_GRID_SCHEMA = "tkc.table-grid/v0.1"
VISUAL_CONFLICT_SCHEMA = "tkc.visual-conflict/v0.1"
VISUAL_REVIEW_ATTESTATION_SCHEMA = "tkc.visual-review-attestation/v0.2"
VISUAL_ASSURANCE_MANIFEST_SCHEMA = "tkc.visual-assurance-manifest/v0.1"
VISUAL_REVIEW_PROTOCOL = "visual-review-attestation-v0.2"
INCREMENTAL_BUILD_DAG_COMPILER_VERSION = "0.14.0-incremental-build-dag"
INCREMENTAL_BUILD_DAG_PROTOCOL = "incremental-build-dag-v0.2"
INCREMENTAL_DAG_MANIFEST_SCHEMA = "tkc.incremental-dag-manifest/v0.2"
INCREMENTAL_DAG_NODE_SCHEMA = "tkc.incremental-dag-node/v0.2"
INCREMENTAL_DAG_EDGE_SCHEMA = "tkc.incremental-dag-edge/v0.2"
INCREMENTAL_CHANGE_SET_SCHEMA = "tkc.incremental-change-set/v0.2"
INCREMENTAL_BUILD_PLAN_SCHEMA = "tkc.incremental-build-plan/v0.2"
INCREMENTAL_BUILD_STATE_SCHEMA = "tkc.incremental-build-state/v0.2"
INCREMENTAL_BUILD_RECEIPT_SCHEMA = "tkc.incremental-build-receipt/v0.2"
SEMANTIC_COMPOSER_COMPILER_VERSION = "0.9.0-semantic-composer"
SEMANTIC_COMPOSER_PROTOCOL = "semantic-composer-v0.1"
COMPOSER_WORKPACK_SCHEMA = "tkc.semantic-composer-workpack/v0.1"
COMPOSER_INPUT_SCHEMA = "tkc.semantic-composer-input/v0.1"
CONCEPT_REF_SCHEMA = "tkc.semantic-concept-ref/v0.1"
ALIGNMENT_CANDIDATE_SCHEMA = "tkc.semantic-alignment-candidate/v0.1"
DIFFERENCE_SCHEMA = "tkc.semantic-difference/v0.1"
COMPOSER_CONFLICT_SCHEMA = "tkc.semantic-conflict/v0.2"
EVIDENCE_BINDING_SCHEMA = "tkc.semantic-evidence-binding/v0.1"
UNIT_MAP_SCHEMA = "tkc.semantic-unit-map/v0.1"
COMPOSER_REVIEW_PLAN_SCHEMA = "tkc.semantic-review-plan/v0.1"
COMPOSER_REVIEW_ATTESTATION_SCHEMA = "tkc.semantic-composer-review-attestation/v0.1"
COMPOSER_RECEIPT_SCHEMA = "tkc.semantic-composer-receipt/v0.1"
COMPOSER_REVIEW_PROTOCOL = "cross-source-review-v0.1"
VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION = "0.14.12-visual-parser-calibration"
VISUAL_PARSER_ORCHESTRATOR_PROTOCOL = "visual-parser-orchestrator-v0.4"
VISUAL_PARSER_JOB_SCHEMA = "tkc.visual-parser-job/v0.4"
VISUAL_PARSER_MANIFEST_SCHEMA = "tkc.visual-parser-manifest/v0.4"
VISUAL_EVALUATION_CONTRACT_SCHEMA = "tkc.visual-evaluation-contract/v0.1"
VISUAL_REVIEW_OVERLAY_SCHEMA = "tkc.visual-review-overlay/v0.4"
PADDLE_RUNTIME_PROFILE_SCHEMA = "tkc.paddle-runtime-profile/v0.2"
PADDLE_RUNTIME_QUALIFICATION_PLAN_SCHEMA = "tkc.paddle-runtime-qualification-plan/v0.1"
PADDLE_RUNTIME_QUALIFICATION_SCHEMA = "tkc.paddle-runtime-qualification/v0.1"
PADDLE_RUNTIME_QUALIFICATION_PROTOCOL = "paddle-runtime-qualification-v0.1"
PADDLE_RUNTIME_QUALIFICATION_COMPILER_VERSION = "0.14.0-paddle-runtime-qualification"
PADDLE_RESULT_ADAPTER_SCHEMA = "tkc.ppstructure-v3-result/v0.1"
PADDLE_RESULT_ADAPTER_PROTOCOL = "ppstructure-v3-result-adapter-v0.1"
PADDLE_OCR_RESULT_SCHEMA = "tkc.ppocrv6-result/v0.1"
PADDLE_OCR_RESULT_PROTOCOL = "ppocrv6-result-adapter-v0.1"
PADDLE_RUNTIME_PRIVATE_DIAGNOSTIC_SCHEMA = "tkc.paddle-runtime-private-diagnostic/v0.1"
PADDLE_RUNTIME_CONTRACT_SCHEMA = "tkc.paddle-runtime-contract/v0.2"
PADDLE_RUNTIME_PROTOCOL = "paddle-runtime-contract-v0.2"
SCAN_IR_SCHEMA = "tkc.scanned-pdf-ir/v0.2"
OCR_OBSERVATION_SCHEMA = "tkc.pdf-ocr-observation/v0.2"
OCR_BACKEND_RECEIPT_SCHEMA = "tkc.pdf-ocr-backend-receipt/v0.2"
OCR_PROTOCOL_SCHEMA = "tkc.pdf-ocr-jsonl/v0.2"
OCR_PROTOCOL_VERSION = "0.2"

# Phase 7D.3 selective visual routing.  These identities are deliberately
# separate from the v0.12 first-class object schemas: v0.12 jobs remain
# read-only compatibility inputs and are never reinterpreted in place.
SELECTIVE_VISUAL_COMPILER_VERSION = "0.14.0-selective-visual"
SELECTIVE_VISUAL_PROTOCOL = "selective-visual-pipeline-v0.1"
VISUAL_POLICY_SCHEMA = "tkc.visual-policy/v0.1"
VISUAL_CENSUS_SCHEMA = "tkc.visual-census/v0.1"
VISUAL_ROUTING_SCHEMA = "tkc.visual-routing/v0.1"
VISUAL_BUDGET_RECEIPT_SCHEMA = "tkc.visual-budget-receipt/v0.1"
VISUAL_INDEX_SCHEMA = "tkc.visual-index/v0.1"
VISUAL_COVERAGE_SCHEMA = "tkc.visual-coverage/v0.2"
VISUAL_BATCH_RECEIPT_SCHEMA = "tkc.visual-batch-receipt/v0.1"
LEGACY_TECHNICAL_CHART_PROFILE_ID = "technical-chart-v1"
LEGACY_TECHNICAL_CHART_PROFILE_PROTOCOL = "technical-chart-v1-local-pp-chart2table"
TECHNICAL_CHART_PROFILE_ID = "technical-chart-v2"
TECHNICAL_CHART_PROFILE_PROTOCOL = "technical-chart-v2-local-pp-chart2table-paddle-dynamic"
TECHNICAL_CHART_BACKEND = "paddleocr-chart-parsing"
TECHNICAL_CHART_API = "paddleocr.ChartParsing"
TECHNICAL_CHART_MODEL_NAME = "PP-Chart2Table"
TECHNICAL_CHART_MODEL_DIR = "PP-Chart2Table"
TECHNICAL_CHART_RESULT_SCHEMA = "tkc.technical-chart-result/v0.1"
TECHNICAL_CHART_RESULT_PROTOCOL = "technical-chart-result-adapter-v0.1"
TECHNICAL_CHART_CANDIDATE_SCHEMA = "tkc.technical-chart-candidate/v0.2"

# Phase 7D.5B private candidate benchmark execution/evaluation.  These
# identities describe the source-candidate orchestration layer only; they do
# not change the formal v0.14.0 release identity and are never sufficient for
# a Gold, accuracy, promotion, or release claim.
VISUAL_BENCHMARK_COMPILER_VERSION = "0.14.1-visual-benchmark"
VISUAL_BENCHMARK_PROTOCOL = "visual-benchmark-v0.1"
VISUAL_BENCHMARK_PLAN_SCHEMA = "tkc.visual-benchmark-plan/v0.1"
VISUAL_BENCHMARK_GOLD_FRAGMENT_SCHEMA = "tkc.visual-benchmark-gold-fragment/v0.1"
VISUAL_BENCHMARK_ATTESTATION_SCHEMA = "tkc.visual-benchmark-review-attestation/v0.1"
VISUAL_BENCHMARK_PREDICTION_SCHEMA = "tkc.visual-benchmark-prediction/v0.1"
VISUAL_BENCHMARK_THRESHOLD_CONFIG_SCHEMA = "tkc.visual-benchmark-threshold-config/v0.1"
VISUAL_BENCHMARK_COST_LEDGER_SCHEMA = "tkc.visual-benchmark-cost-ledger/v0.1"
VISUAL_BENCHMARK_STATE_SCHEMA = "tkc.visual-benchmark-state/v0.1"
VISUAL_BENCHMARK_EVALUATION_SCHEMA = "tkc.visual-benchmark-evaluation/v0.1"

# Phase 7D.5B reviewer-facing transport.  These are deliberately separate
# from the Gold fragment/attestation schemas above: the workbench stores only
# locked candidate identity plus human form data, and emits the existing Gold
# rows only after the canonical workpack is re-read and all review gates pass.
VISUAL_REVIEW_WORKBENCH_COMPILER_VERSION = "0.14.1-visual-review-workbench"
VISUAL_REVIEW_WORKBENCH_PROTOCOL = "visual-review-workbench-v0.1"
VISUAL_REVIEW_WORKBENCH_MANIFEST_SCHEMA = "tkc.visual-review-workbench-manifest/v0.1"
VISUAL_REVIEW_WORKBENCH_SUBMISSION_SCHEMA = "tkc.visual-review-workbench-submission/v0.1"
VISUAL_REVIEW_WORKBENCH_REVIEWER_REGISTRY_SCHEMA = "tkc.visual-review-workbench-reviewer-registry/v0.1"
VISUAL_REVIEW_WORKBENCH_ATTESTATION_BUNDLE_SCHEMA = "tkc.visual-review-workbench-attestation-bundle/v0.1"
VISUAL_REVIEW_WORKBENCH_REVIEW_MANIFEST_SCHEMA = "tkc.visual-review-workbench-review-manifest/v0.1"

# Variable-size, single-physical-page visual review workpacks.  This transport
# reuses the established human reviewer UI but is deliberately not accepted by
# the frozen 31-item benchmark evaluator unless the caller explicitly opts in
# for review-only loading.
PORTABLE_VISUAL_REVIEW_COMPILER_VERSION = "0.14.8-single-page-visual-review"
PORTABLE_VISUAL_REVIEW_WORKPACK_PROTOCOL = "portable-visual-review-workpack-v0.1"
PORTABLE_VISUAL_REVIEW_WORKPACK_MANIFEST_SCHEMA = "tkc.portable-visual-review-workpack-manifest/v0.1"

# Candidate-correctness evaluation for variable-size portable visual review
# workpacks.  This is deliberately separate from the frozen 31-item/GPT visual
# benchmark: it replays only already-frozen native parser candidates and never
# claims whole-range detection recall.
PORTABLE_VISUAL_BENCHMARK_COMPILER_VERSION = "0.14.9-portable-visual-benchmark"
PORTABLE_VISUAL_BENCHMARK_PROTOCOL = "portable-visual-benchmark-v0.1"
PORTABLE_VISUAL_BENCHMARK_PLAN_SCHEMA = "tkc.portable-visual-benchmark-plan/v0.1"
PORTABLE_VISUAL_BENCHMARK_EVALUATION_SCHEMA = "tkc.portable-visual-benchmark-evaluation/v0.1"
PORTABLE_VISUAL_BENCHMARK_MANIFEST_SCHEMA = "tkc.portable-visual-benchmark-manifest/v0.1"
PORTABLE_VISUAL_CALIBRATION_REPLAY_COMPILER_VERSION = "0.14.12-visual-calibration-replay"

# Bounded full-page visual-object census used to measure missed-object recall.
# This remains a source candidate identity, not a formal v0.14.x release.
PORTABLE_VISUAL_RECALL_COMPILER_VERSION = "0.14.10-portable-visual-recall-review"
PORTABLE_VISUAL_RECALL_PROTOCOL = "portable-visual-recall-review-v0.1"
PORTABLE_VISUAL_RECALL_WORKBENCH_SCHEMA = "tkc.portable-visual-recall-workbench/v0.1"
PORTABLE_VISUAL_RECALL_SUBMISSION_SCHEMA = "tkc.portable-visual-recall-submission/v0.1"
PORTABLE_VISUAL_RECALL_REVISION_SCHEMA = "tkc.portable-visual-recall-revision/v0.1"

# Incremental missed-object review reuses accepted content Gold as a locked
# baseline, so the reviewer only records additional objects. It supersedes the
# v0.1 full-census handoff without rewriting that historical artifact.
PORTABLE_VISUAL_INCREMENTAL_RECALL_COMPILER_VERSION = "0.14.11-portable-visual-incremental-recall"
PORTABLE_VISUAL_INCREMENTAL_RECALL_PROTOCOL = "portable-visual-incremental-recall-v0.2"
PORTABLE_VISUAL_INCREMENTAL_RECALL_WORKBENCH_SCHEMA = "tkc.portable-visual-incremental-recall-workbench/v0.2"
PORTABLE_VISUAL_INCREMENTAL_RECALL_SUBMISSION_SCHEMA = "tkc.portable-visual-incremental-recall-submission/v0.2"
PORTABLE_VISUAL_INCREMENTAL_RECALL_REVISION_SCHEMA = "tkc.portable-visual-incremental-recall-revision/v0.2"

# Phase 7D.5B-R2 review convention profile.  This source-candidate identity
# is deliberately separate from RELEASE_VERSION and from the legacy v0.1
# reviewer submission transport.  A profile-bound workbench records the
# canonical profile hash; changing the profile makes that workbench stale.
VISUAL_REVIEW_CONVENTION_COMPILER_VERSION = "0.14.2-review-convention"
VISUAL_REVIEW_CONVENTION_PROTOCOL = "visual-review-convention-v0.2"
VISUAL_REVIEW_CONVENTION_PROFILE_SCHEMA = "tkc.visual-review-convention-profile/v0.2"
VISUAL_REVIEW_WORKBENCH_PROFILE_PROTOCOL = "visual-review-workbench-v0.2-profile-bound"
VISUAL_REVIEW_WORKBENCH_PROFILE_MANIFEST_SCHEMA = "tkc.visual-review-workbench-manifest/v0.2"
VISUAL_REVIEW_COMPARISON_RECEIPT_SCHEMA = "tkc.visual-review-convention-comparison-receipt/v0.1"

# Phase 7D.5B independent adjudication.  These identities are deliberately
# separate from the reviewer workbench and Gold contracts: adjudication may
# resolve only a frozen disagreement set and never authors reviewer evidence.
VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION = "0.14.1-visual-review-adjudication"
VISUAL_REVIEW_ADJUDICATION_PROTOCOL = "visual-review-adjudication-v0.1"
VISUAL_REVIEW_ADJUDICATION_MANIFEST_SCHEMA = "tkc.visual-review-adjudication-manifest/v0.1"
VISUAL_REVIEW_ADJUDICATION_SUBMISSION_SCHEMA = "tkc.visual-review-adjudication-submission/v0.1"
VISUAL_REVIEW_ADJUDICATION_RECEIPT_SCHEMA = "tkc.visual-review-adjudication-receipt/v0.1"

# Phase 7D portable Gold review finalization.  This is an additive private
# review/revision sidecar over the existing adjudication and Gold fragment
# contracts; it does not change RELEASE_VERSION or reinterpret old revisions.
PORTABLE_GOLD_REVIEW_COMPILER_VERSION = "0.14.3-portable-gold-review"
PORTABLE_GOLD_REVIEW_PROTOCOL = "portable-gold-review-v0.1"
PORTABLE_GOLD_REVIEW_REVISION_SCHEMA = "tkc.portable-gold-review-revision/v0.1"
PORTABLE_GOLD_REVIEW_DECISION_SCHEMA = "tkc.portable-gold-review-decision/v0.1"

# Phase 7D.6A scan contract hardening.  This is a source-candidate identity,
# not a formal release and not a replacement for RELEASE_VERSION.
SCAN_MVP_CONTRACT_HARDENING_COMPILER_VERSION = "0.14.4-scan-mvp-contract-hardening"
SCAN_ANCHOR_PROTOCOL = "scan-anchor-v0.2"

# Phase 7D.6C explicit spread split and page/region checkpoint sidecars.  These
# are candidate-only protocol identities; they do not change RELEASE_VERSION or
# reinterpret the immutable 7D.6B artifacts.
SCAN_SPLIT_CHECKPOINT_COMPILER_VERSION = "0.14.5-scan-split-checkpoint"
SCANNED_PDF_SPLIT_SCHEMA = "tkc.scanned-pdf-spread-split/v0.1"
SCANNED_PDF_SPLIT_PROTOCOL = "scanned-pdf-spread-split-v0.1"
# Phase 7D.6C-C keeps the v0.1 split contracts read-only and binds the
# pixel-edge-first odd-dimension mapping repair to the new Gold-review line.
SCANNED_PDF_SPLIT_GEOMETRY_COMPILER_VERSION = "0.14.6-scan-gold-review"
SCANNED_PDF_SPLIT_GEOMETRY_SCHEMA = "tkc.scanned-pdf-spread-split/v0.2"
SCANNED_PDF_SPLIT_GEOMETRY_PROTOCOL = "scanned-pdf-spread-split-v0.2"
SCANNED_PDF_CHECKPOINT_SCHEMA = "tkc.scanned-pdf-checkpoint/v0.1"
SCANNED_PDF_CHECKPOINT_PROTOCOL = "scanned-pdf-checkpoint-v0.1"
PADDLE_RUNTIME_SPLIT_QUALIFICATION_PLAN_SCHEMA = "tkc.paddle-runtime-split-qualification-plan/v0.1"
PADDLE_RUNTIME_SPLIT_QUALIFICATION_SCHEMA = "tkc.paddle-runtime-split-qualification/v0.1"
PADDLE_RUNTIME_SPLIT_QUALIFICATION_PROTOCOL = "paddle-runtime-split-qualification-v0.1"
PADDLE_RUNTIME_SPLIT_QUALIFICATION_COMPILER_VERSION = "0.14.5-scan-split-checkpoint"

# Phase 7D.6C-C portable Scan Gold Review.  These identities are private,
# candidate-only contracts over frozen local renders/crops.  They do not
# change RELEASE_VERSION and never authorize OCR, promotion, or release.
SCAN_GOLD_REVIEW_COMPILER_VERSION = "0.14.7-scan-gold-coverage"
SCAN_GOLD_REVIEW_PROTOCOL = "scan-gold-review-v0.2"
SCAN_GOLD_REVIEW_PROFILE_SCHEMA = "tkc.scan-gold-review-profile/v0.2"
SCAN_GOLD_REVIEW_PLAN_SCHEMA = "tkc.scan-gold-review-plan/v0.2"
SCAN_GOLD_REVIEW_WORKBENCH_MANIFEST_SCHEMA = "tkc.scan-gold-review-workbench-manifest/v0.2"
SCAN_GOLD_REVIEW_SUBMISSION_SCHEMA = "tkc.scan-gold-review-submission/v0.2"
SCAN_GOLD_REVIEW_ATTESTATION_SCHEMA = "tkc.scan-gold-review-attestation/v0.2"
SCAN_GOLD_REVIEW_STRUCTURE_GOLD_SCHEMA = "tkc.scan-gold-review-structure-gold/v0.2"
SCAN_GOLD_REVIEW_OCR_GOLD_SCHEMA = "tkc.scan-gold-review-ocr-gold/v0.2"
SCAN_GOLD_REVIEW_REVISION_SCHEMA = "tkc.scan-gold-review-revision/v0.2"
SCAN_GOLD_REVIEW_PREDICTION_SCHEMA = "tkc.scan-gold-review-prediction/v0.2"
SCAN_GOLD_REVIEW_EVALUATION_SCHEMA = "tkc.scan-gold-review-evaluation/v0.2"

# Phase 7D.6E private controlled-scan derivation.  This creates an image-only
# qualification source from exact, locally rendered pages and binds any
# existing reviewed visual evidence by page/render identity.  It is not an OCR
# result, Gold rewrite, formal release, or MVP acceptance.
CONTROLLED_SCAN_DERIVATION_COMPILER_VERSION = "0.14.13-controlled-scan-derivation"
CONTROLLED_SCAN_DERIVATION_PROTOCOL = "controlled-image-only-scan-derivation-v0.1"
CONTROLLED_SCAN_DERIVATION_SCHEMA = "tkc.controlled-scan-derivation/v0.1"

# Phase 7D.6E-C private scan-structure review transport.  It consumes an
# existing Scan IR and re-renders only the bound heading pages; it never runs
# OCR, authors reviewer attestations, promotes knowledge, or changes a release.
SCAN_STRUCTURE_REVIEW_WORKBENCH_COMPILER_VERSION = "0.14.14-scan-structure-review-workbench"
SCAN_STRUCTURE_REVIEW_WORKBENCH_PROTOCOL = "scan-structure-review-workbench-v0.1"
SCAN_STRUCTURE_REVIEW_WORKBENCH_SCHEMA = "tkc.scan-structure-review-workbench/v0.1"
SCAN_STRUCTURE_REVIEW_SUBMISSION_SCHEMA = "tkc.scan-structure-review-submission/v0.1"
SCAN_STRUCTURE_REVIEW_ATTESTATION_SCHEMA = "tkc.scan-structure-review-external-attestation/v0.1"
SCAN_STRUCTURE_REVIEW_REVISION_SCHEMA = "tkc.scan-structure-review-revision/v0.1"

# Phase 7D.6E-C offline projection of an accepted external structure fragment
# into a fresh reviewed Scan IR.  This identity is deliberately separate from
# the OCR compiler so an immutable OCR checkpoint does not become stale merely
# because the projection utility is added or updated.
SCAN_STRUCTURE_MATERIALIZER_COMPILER_VERSION = "0.14.15-scan-structure-materializer"
SCAN_STRUCTURE_MATERIALIZER_PROTOCOL = "scan-structure-materializer-v0.1"

# Private, answer-free transport for risk-tiered semantic assurance review.
# It projects reviewer-owned browser submissions into the existing attestation
# schema and never completes the multi-reviewer gate by itself.
SEMANTIC_REVIEW_WORKBENCH_COMPILER_VERSION = "0.14.16-semantic-review-workbench"
SEMANTIC_REVIEW_WORKBENCH_PROTOCOL = "semantic-review-workbench-v0.1"
SEMANTIC_REVIEW_WORKBENCH_SCHEMA = "tkc.semantic-review-workbench/v0.1"
SEMANTIC_REVIEW_WORKBENCH_SUBMISSION_SCHEMA = "tkc.semantic-review-workbench-submission/v0.1"
SEMANTIC_REVIEW_WORKBENCH_REVISION_SCHEMA = "tkc.semantic-review-workbench-revision/v0.1"

__all__ = [
    "RELEASE_VERSION",
    "UX_SOURCE_CANDIDATE_VERSION",
    "UX_COMPILER_VERSION",
    "UX_CLI_PROTOCOL",
    "FAKE_EXPERT_JOB_SCHEMA",
    "FAKE_EXPERT_JOB_PROTOCOL",
    "DIRECT_REFERENCE_COMPILER_VERSION",
    "DIRECT_REFERENCE_PROTOCOL",
    "DIRECT_REFERENCE_SCHEMA",
    "DIRECT_REFERENCE_RUNTIME_SHA256",
    "PORTABLE_REFERENCE_RUNTIME_SHA256",
    "READONLY_COMPATIBLE_SOURCE_CANDIDATE_VERSIONS",
    "PHASE3_COMPILER_VERSION",
    "EXECUTION_COMPILER_VERSION",
    "COMPOSITE_COMPILER_VERSION",
    "SCAN_PDF_COMPILER_VERSION",
    "READONLY_COMPATIBLE_SCAN_PDF_COMPILER_VERSIONS",
    "READONLY_COMPATIBLE_QUALIFICATION_TOOL_SHA256",
    "SCANNED_PDF_STRUCTURE_REVIEW_SCHEMA",
    "SCANNED_PDF_STRUCTURE_REVIEW_PROTOCOL",
    "SEMANTIC_ASSURANCE_COMPILER_VERSION",
    "SEMANTIC_ASSURANCE_PROTOCOL",
    "PDF_IR_SCHEMA_VERSION",
    "HEADING_LOCATOR_PROTOCOL",
    "WHOLE_BOOK_PLANNER_COMPILER_VERSION",
    "WHOLE_BOOK_PLANNER_PROTOCOL",
    "VISUAL_SEMANTICS_COMPILER_VERSION",
    "VISUAL_SEMANTICS_PROTOCOL",
    "VISUAL_OBJECT_SCHEMA",
    "VISUAL_RELATION_SCHEMA",
    "TABLE_GRID_SCHEMA",
    "VISUAL_CONFLICT_SCHEMA",
    "VISUAL_REVIEW_ATTESTATION_SCHEMA",
    "VISUAL_ASSURANCE_MANIFEST_SCHEMA",
    "VISUAL_REVIEW_PROTOCOL",
    "INCREMENTAL_BUILD_DAG_COMPILER_VERSION",
    "INCREMENTAL_BUILD_DAG_PROTOCOL",
    "INCREMENTAL_DAG_MANIFEST_SCHEMA",
    "INCREMENTAL_DAG_NODE_SCHEMA",
    "INCREMENTAL_DAG_EDGE_SCHEMA",
    "INCREMENTAL_CHANGE_SET_SCHEMA",
    "INCREMENTAL_BUILD_PLAN_SCHEMA",
    "INCREMENTAL_BUILD_STATE_SCHEMA",
    "INCREMENTAL_BUILD_RECEIPT_SCHEMA",
    "SEMANTIC_COMPOSER_COMPILER_VERSION",
    "SEMANTIC_COMPOSER_PROTOCOL",
    "COMPOSER_WORKPACK_SCHEMA",
    "COMPOSER_INPUT_SCHEMA",
    "CONCEPT_REF_SCHEMA",
    "ALIGNMENT_CANDIDATE_SCHEMA",
    "DIFFERENCE_SCHEMA",
    "COMPOSER_CONFLICT_SCHEMA",
    "EVIDENCE_BINDING_SCHEMA",
    "UNIT_MAP_SCHEMA",
    "COMPOSER_REVIEW_PLAN_SCHEMA",
    "COMPOSER_REVIEW_ATTESTATION_SCHEMA",
    "COMPOSER_RECEIPT_SCHEMA",
    "COMPOSER_REVIEW_PROTOCOL",
    "VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION",
    "VISUAL_PARSER_ORCHESTRATOR_PROTOCOL",
    "VISUAL_PARSER_JOB_SCHEMA",
    "VISUAL_PARSER_MANIFEST_SCHEMA",
    "VISUAL_EVALUATION_CONTRACT_SCHEMA",
    "VISUAL_REVIEW_OVERLAY_SCHEMA",
    "PADDLE_RUNTIME_PROFILE_SCHEMA",
    "PADDLE_RUNTIME_QUALIFICATION_PLAN_SCHEMA",
    "PADDLE_RUNTIME_QUALIFICATION_SCHEMA",
    "PADDLE_RUNTIME_QUALIFICATION_PROTOCOL",
    "PADDLE_RUNTIME_QUALIFICATION_COMPILER_VERSION",
    "PADDLE_RESULT_ADAPTER_SCHEMA",
    "PADDLE_RESULT_ADAPTER_PROTOCOL",
    "PADDLE_OCR_RESULT_SCHEMA",
    "PADDLE_OCR_RESULT_PROTOCOL",
    "PADDLE_RUNTIME_PRIVATE_DIAGNOSTIC_SCHEMA",
    "PADDLE_RUNTIME_CONTRACT_SCHEMA",
    "PADDLE_RUNTIME_PROTOCOL",
    "SCAN_IR_SCHEMA",
    "OCR_OBSERVATION_SCHEMA",
    "OCR_BACKEND_RECEIPT_SCHEMA",
    "OCR_PROTOCOL_SCHEMA",
    "OCR_PROTOCOL_VERSION",
    "SELECTIVE_VISUAL_COMPILER_VERSION",
    "SELECTIVE_VISUAL_PROTOCOL",
    "VISUAL_POLICY_SCHEMA",
    "VISUAL_CENSUS_SCHEMA",
    "VISUAL_ROUTING_SCHEMA",
    "VISUAL_BUDGET_RECEIPT_SCHEMA",
    "VISUAL_INDEX_SCHEMA",
    "VISUAL_COVERAGE_SCHEMA",
    "VISUAL_BATCH_RECEIPT_SCHEMA",
    "LEGACY_TECHNICAL_CHART_PROFILE_ID",
    "LEGACY_TECHNICAL_CHART_PROFILE_PROTOCOL",
    "TECHNICAL_CHART_PROFILE_ID",
    "TECHNICAL_CHART_PROFILE_PROTOCOL",
    "TECHNICAL_CHART_BACKEND",
    "TECHNICAL_CHART_API",
    "TECHNICAL_CHART_MODEL_NAME",
    "TECHNICAL_CHART_MODEL_DIR",
    "TECHNICAL_CHART_RESULT_SCHEMA",
    "TECHNICAL_CHART_RESULT_PROTOCOL",
    "TECHNICAL_CHART_CANDIDATE_SCHEMA",
    "VISUAL_BENCHMARK_COMPILER_VERSION",
    "VISUAL_BENCHMARK_PROTOCOL",
    "VISUAL_BENCHMARK_PLAN_SCHEMA",
    "VISUAL_BENCHMARK_GOLD_FRAGMENT_SCHEMA",
    "VISUAL_BENCHMARK_ATTESTATION_SCHEMA",
    "VISUAL_BENCHMARK_PREDICTION_SCHEMA",
    "VISUAL_BENCHMARK_THRESHOLD_CONFIG_SCHEMA",
    "VISUAL_BENCHMARK_COST_LEDGER_SCHEMA",
    "VISUAL_BENCHMARK_STATE_SCHEMA",
    "VISUAL_BENCHMARK_EVALUATION_SCHEMA",
    "VISUAL_REVIEW_WORKBENCH_COMPILER_VERSION",
    "VISUAL_REVIEW_WORKBENCH_PROTOCOL",
    "VISUAL_REVIEW_WORKBENCH_MANIFEST_SCHEMA",
    "VISUAL_REVIEW_WORKBENCH_SUBMISSION_SCHEMA",
    "VISUAL_REVIEW_WORKBENCH_REVIEWER_REGISTRY_SCHEMA",
    "VISUAL_REVIEW_WORKBENCH_ATTESTATION_BUNDLE_SCHEMA",
    "VISUAL_REVIEW_WORKBENCH_REVIEW_MANIFEST_SCHEMA",
    "VISUAL_REVIEW_CONVENTION_COMPILER_VERSION",
    "VISUAL_REVIEW_CONVENTION_PROTOCOL",
    "VISUAL_REVIEW_CONVENTION_PROFILE_SCHEMA",
    "VISUAL_REVIEW_WORKBENCH_PROFILE_PROTOCOL",
    "VISUAL_REVIEW_WORKBENCH_PROFILE_MANIFEST_SCHEMA",
    "VISUAL_REVIEW_COMPARISON_RECEIPT_SCHEMA",
    "VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION",
    "VISUAL_REVIEW_ADJUDICATION_PROTOCOL",
    "VISUAL_REVIEW_ADJUDICATION_MANIFEST_SCHEMA",
    "VISUAL_REVIEW_ADJUDICATION_SUBMISSION_SCHEMA",
    "VISUAL_REVIEW_ADJUDICATION_RECEIPT_SCHEMA",
    "PORTABLE_GOLD_REVIEW_COMPILER_VERSION",
    "PORTABLE_GOLD_REVIEW_PROTOCOL",
    "PORTABLE_GOLD_REVIEW_REVISION_SCHEMA",
    "PORTABLE_GOLD_REVIEW_DECISION_SCHEMA",
    "SCAN_MVP_CONTRACT_HARDENING_COMPILER_VERSION",
    "SCAN_ANCHOR_PROTOCOL",
    "SCAN_SPLIT_CHECKPOINT_COMPILER_VERSION",
    "SCANNED_PDF_SPLIT_SCHEMA",
    "SCANNED_PDF_SPLIT_PROTOCOL",
    "SCANNED_PDF_SPLIT_GEOMETRY_COMPILER_VERSION",
    "SCANNED_PDF_SPLIT_GEOMETRY_SCHEMA",
    "SCANNED_PDF_SPLIT_GEOMETRY_PROTOCOL",
    "SCANNED_PDF_CHECKPOINT_SCHEMA",
    "SCANNED_PDF_CHECKPOINT_PROTOCOL",
    "PADDLE_RUNTIME_SPLIT_QUALIFICATION_PLAN_SCHEMA",
    "PADDLE_RUNTIME_SPLIT_QUALIFICATION_SCHEMA",
    "PADDLE_RUNTIME_SPLIT_QUALIFICATION_PROTOCOL",
    "PADDLE_RUNTIME_SPLIT_QUALIFICATION_COMPILER_VERSION",
    "SCAN_GOLD_REVIEW_COMPILER_VERSION",
    "SCAN_GOLD_REVIEW_PROTOCOL",
    "SCAN_GOLD_REVIEW_PROFILE_SCHEMA",
    "SCAN_GOLD_REVIEW_PLAN_SCHEMA",
    "SCAN_GOLD_REVIEW_WORKBENCH_MANIFEST_SCHEMA",
    "SCAN_GOLD_REVIEW_SUBMISSION_SCHEMA",
    "SCAN_GOLD_REVIEW_ATTESTATION_SCHEMA",
    "SCAN_GOLD_REVIEW_STRUCTURE_GOLD_SCHEMA",
    "SCAN_GOLD_REVIEW_OCR_GOLD_SCHEMA",
    "SCAN_GOLD_REVIEW_REVISION_SCHEMA",
    "SCAN_GOLD_REVIEW_PREDICTION_SCHEMA",
    "SCAN_GOLD_REVIEW_EVALUATION_SCHEMA",
    "CONTROLLED_SCAN_DERIVATION_COMPILER_VERSION",
    "CONTROLLED_SCAN_DERIVATION_PROTOCOL",
    "CONTROLLED_SCAN_DERIVATION_SCHEMA",
    "SCAN_STRUCTURE_REVIEW_WORKBENCH_COMPILER_VERSION",
    "SCAN_STRUCTURE_REVIEW_WORKBENCH_PROTOCOL",
    "SCAN_STRUCTURE_REVIEW_WORKBENCH_SCHEMA",
    "SCAN_STRUCTURE_REVIEW_SUBMISSION_SCHEMA",
    "SCAN_STRUCTURE_REVIEW_ATTESTATION_SCHEMA",
    "SCAN_STRUCTURE_REVIEW_REVISION_SCHEMA",
    "SCAN_STRUCTURE_MATERIALIZER_COMPILER_VERSION",
    "SCAN_STRUCTURE_MATERIALIZER_PROTOCOL",
    "SEMANTIC_REVIEW_WORKBENCH_COMPILER_VERSION",
    "SEMANTIC_REVIEW_WORKBENCH_PROTOCOL",
    "SEMANTIC_REVIEW_WORKBENCH_SCHEMA",
    "SEMANTIC_REVIEW_WORKBENCH_SUBMISSION_SCHEMA",
    "SEMANTIC_REVIEW_WORKBENCH_REVISION_SCHEMA",
]
