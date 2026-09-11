# Quality gates

## Structural gate

Use during drafting. Require parseable metadata, canonical paths, valid source
fingerprints, module declarations, and cross-file identifiers. Empty knowledge is
allowed, so passing this gate does not demonstrate expertise.

## Publication gate

Require all structural checks plus:

1. package status is `ready`;
2. every module exports at least one knowledge object;
3. every exported object exists and has evidence;
4. every evidence anchor resolves to a declared source and supported object;
5. all seven competency categories exist;
6. every shareable package file is included in the integrity seal and hashes match;
7. declared decision/executable capabilities have matching contracts;
8. no unresolved error-level validation issue remains.
9. every conflict has independently traceable claims and an explicit runtime status.
10. Phase 3 competency responses and receipts recompute from the sealed runtime,
    catalog, and test suite; a test cannot self-declare a passing result.

For `source-required`, also run `scripts/verify_source_anchors.py` with the matching
external PDF. Package publication and external-source verification are separate gates;
passing one does not imply the other.

## Host-forward certification gate

Run this gate only after publication and keep its artifacts outside the sealed Skill.
Require:

1. leak-free challenges contain no answer, verdict, or expected-result fields;
2. a normalized hidden-expectation bundle is hash-committed before formal runs and
   matches the exact released bundle after all runs finish;
3. calibration instances and records are excluded from certification;
4. each challenge has the declared number of attempts and every run uses a unique
   fresh Agent instance;
5. all runs share one declared host ID, host version, and model ID;
6. run and receipt hashes bind the current sealed manifest, integrity map, host
   adapter, runtime, challenge, raw response, and available transcript;
7. returned object, evidence, conflict, and gap identifiers resolve, stay within the
   expectation allowlists, and preserve evidence-to-object coherence;
8. required conflicts, gaps, scope refusals, and capability boundaries pass without
   forbidden claims or invented IDs;
9. every receipt and the certification report recompute exactly; deletion,
   substitution, reuse, or stale commitments fail closed;
10. the certificate retains the canonical capability map and explicitly declares
    transcript, model-identity, semantic-scoring, and non-cryptographic-attestation
    limitations.

This gate certifies observed behavior for an exact host/package fingerprint. It does
not upgrade canonical capabilities, authenticate the host, prove human review, or
generalize to untested hosts and models.

## Semantic-promotion gate

Run this gate between PDF IR and the structural package gate. Require:

1. every structural candidate has an explicit disposition;
2. every proposal span resolves inside its unit's extractable focus and all hashes
   recompute against the declared source;
3. equations keep source, normalized, and derived forms distinct and link visual
   tasks on the same physical page as their evidence spans;
4. each structured proposal is deterministically decomposed into atomic
   propositions, numeric values, units, symbols, assumptions, applicability,
   failure conditions, derivation/AST nodes, and visual facts without inventing
   new semantic content;
5. every atomic assertion has exactly one full support-matrix row, with derived
   content kept distinct from source-explicit support and every formula node path
   covered;
6. every selected-scope semantic unit has exactly one hash-bound coverage row and
   one of `promoted`, `context-only`, `non-knowledge`, `rejected`, `gap`, or
   `quarantined`; this is selected-scope accounting, not whole-book completeness;
7. an orchestrator-issued `atomic-support-review-v2` plan binds the current source,
   proposals, atomic/support/coverage bundles, dependencies, rendered pages,
   reviewer registry, and explicit package-gap policy;
8. definitions and ordinary concepts without higher-risk atoms have at least one
   independent attestation; equations, relations, conflicts, applicability,
   failure conditions, derivations, and visual facts have two distinct reviewer
   attestations;
9. each `tkc.review-attestation/v0.1` binds the source/span/render artifacts actually
   inspected, exact assertion IDs and hashes, plan/session, proposer/reviewer, and
   one differentiated difference/no-difference observation per assertion;
10. reviewer instances differ from proposer instances, replayed attestations fail,
    and an executable formula's independent test author differs from the compiler,
    proposer, and every semantic reviewer;
11. every visual page has one fresh render-bound receipt, and all semantic/visual
    rows are attributable to external fragment files through
    `review-fragments.json`; direct central-file writes and post-merge edits fail;
12. accepted items have full evidence support, rationale, confidence, accepted
    dependencies, and supported visual tasks where applicable;
13. rejected or quarantined items retain unique snake-case issue codes;
14. each unit-local gap is independently classified as unresolved in the package or
   resolved by accepted full-support objects from other units;
15. rendered assets are CRC-valid, decompressible PNGs, not signature-only files;
16. no blanket pattern survives validation: shared single rationales/confidences
    across accepted items or a shared single observation across verified pages fail
    as `review_blanket_pattern` / `visual_review_blanket_pattern`, and review
    material is quarantined rather than accepted;
17. every promoted object, relation, conflict, and package gap maps one-to-one to an
    accepted review plus its full assertion/support/attestation set, and every
    reverse evidence edge is exact;
18. deterministic promotion excludes source text/renders, emits the compact
    source-free assurance subtree, and forces decision/executable capability off.

This gate establishes a host-recorded independent-agent audit trail, not
cryptographic reviewer identity and not human domain-expert approval. The review-plan
SHA-256 is a publicly recomputable tamper-evidence fingerprint; it cannot authenticate
who controlled a reviewer instance. Legacy `separate-source-render-review-v1`
packages remain verifiable under their original contract but cannot be relabeled as
v0.5 semantic-assured packages.

### Phase 7A visual-semantics gate

For a visual-enabled workpack, additionally require:

1. every candidate object, relation, grid/cell, and conflict validates against its
   `tkc.*` schema and canonical hash, with source/page/render/DPI/rotation,
   coordinate, crop, and parser/backend/model/config provenance;
2. `pypdf==6.10.0` is the native evidence parser and the `pdftoppm` receipt is the
   canonical visual base; optional adapters are explicit candidate-only routes and
   unavailable dependencies fail closed;
3. duplicate candidates are normalized into a conflict ledger and no parent,
   relation endpoint, grid, cell, unit, or series binding is dangling;
4. external `tkc.visual-review-attestation/v0.2` fragments bind exact object,
   table-cell, or series IDs and full-page/crop/render/bbox/input hashes; the
   compiler cannot generate an attestation or reviewer conclusion;
5. presence/localization has one independent visual reviewer and plot/table values
   or visual formula conclusions have two independent reviewers; page-only support
   cannot resolve or promote a visual fact;
6. output visual rows are explicitly promoted only after those attestations, the
   assurance manifest recomputes, and source-free runtime metadata carries the exact
   IDs and hashes. `whole_book_complete_claimed` remains false.

These checks prove deterministic binding and review workflow state. They do not
certify model precision, scanned-page accuracy, chart digitization, human expertise,
or cryptographic reviewer identity.

### Phase 7D.3 selective visual gate

For a v0.4 visual-parser job, additionally require:

1. the mode, closed visual-kind set, context, and each nonnegative visual budget
   validate and the canonical policy SHA is identical in the job, manifest, DAG,
   census, routing, budget, index, coverage, and chart receipts;
2. `off` has zero visual census/render/PP-Structure/chart calls and records every
   page as `not-inspected-by-policy`; separately selected PP-OCRv6 body OCR may
   still route only `pages_needing_ocr`;
3. `auto` has a no-model/no-render Census and starts heavy visual work only for
   candidate regions; `full` remains crop-only and bounded;
4. budget exhaustion is `paused-budget-exhausted` with explicit page/region gaps,
   never a successful completion or silent skip;
5. the visual index contains no raw image/crop bytes, full OCR, absolute source
   path, credential, or complete worker response;
6. unchanged source/render/crop/model/config inputs produce the same canonical
   visual object ID in auto and full, while policy/DAG identities differ;
7. a chart candidate is eligible only for a chart route with a complete local
   `paddleocr.ChartParsing`/PP-Chart2Table inventory and the frozen
   `technical-chart-v1` adapter options; missing models/runtime are
   `not-run-missing-models`/`not-run-runtime-unavailable`, and every chart row
   remains `candidate_only=true`, `promotion=false`, `executable=false`; and
8. batch/session receipts bind bounded request counts, total/per-request timeouts,
   model-load-once behavior, stable error codes, failure isolation, and core
   calls/pages/regions/elapsed/bytes/objects counts.

These are deterministic routing and privacy gates, not Gold, visual accuracy,
routing recall, token-saving, host-isolation, or performance certification.

### Phase 7D.4 local runtime qualification gate

Before a new v0.14 PP-StructureV3 or Chart2Table route may render/infer, require:

1. a host-local `tkc.paddle-runtime-qualification/v0.1` receipt bound to the
   exact runtime contract, package inventory, selected profile/model identity,
   configuration, worker, and crop fixture hash;
2. `technical-table-v1` for PP-StructureV3 or `technical-chart-v2` for
   Chart2Table. The latter fixes CPU, no HPI, `paddle_dynamic`, batch size 1,
   and the direct `PP-Chart2Table` directory; v1 is compatibility-only;
3. three serial repeated crop requests in one short-lived process,
   `model_load_calls=1`, one normalized-result SHA-256, a completed batch, and
   explicit startup/per-request/total timeout values;
4. no weight-newly-initialized, training-required, key/shape mismatch, or
   download/network warning code. Raw stderr is excluded; only its SHA-256,
   byte count, and stable codes may enter the receipt;
5. `source_pdf_passed=false`, network/model download/MCP dependency false,
   candidate-only true, and accuracy/Gold/promotion false;
6. a missing, rejected, stale, or differently bound receipt pauses as
   `paused-runtime-unqualified` before render/inference; and
7. a Phase 7B `runtime_qualification` node feeding runtime results/chart
   candidates so receipt/profile/warning drift invalidates the complete
   re-extract/re-review/recompose/repackage closure.

This gate proves exact-route availability and repeatability only. It does not
prove OCR/chart accuracy, table semantics, visual truth, reviewer independence,
performance on a different host, signing, promotion, or execution.

## Whole-book structure and coverage gate

Before calling a book plan usable, require:

1. the source SHA-256 and complete physical page count match the local PDF;
2. every physical page has exactly one planning ledger row and every reconstructed
   segment has exactly one separate ledger row;
3. planning state is one of `compile`, `context-only`, `non-knowledge`,
   `ocr-candidate`, `visual-review`, `gap`, or `quarantined`, and promotion status
   is tracked separately;
4. every page, segment, and module has a deterministic review-routing row, with
   OCR explicitly candidate-only and visual review explicitly required where routed;
5. heading candidates and resolutions recompute from source/parser/render receipts;
   unresolved structure produces a structured task and never a guessed occurrence;
6. module IDs, page sets, segment membership, dependencies, routes, and input hashes
   are stable across two builds with identical input;
7. `whole_book_complete_claimed` is false until semantic completeness, real OCR
   quality, Visual Semantics, independent review, competency, seal, and execution
   gates have all been separately satisfied. The planner itself can never set it
   true.

The book-level orchestrator may build and validate these routing artifacts, but it
must pause at every named gate and must not launch OCR/models, author reviews, seal,
or enable execution.

## Composition and update gate

For a Phase 5 composite require:

1. at least two independently publishable, source-required, reference-only children;
2. exact lock bindings for child package/version, manifest, integrity, modules,
   sources, capabilities, and canonical relative path;
3. no global entity-ID or module-ID collision;
4. no same-source page overlap or unknown same-source scope;
5. no symlink, non-regular file, embedded PDF, child mutation, or unsealed file;
6. a recomputable aggregate catalog with package and module attribution;
7. `preserve-separate-no-consensus` routing and no decision/executable escalation;
8. exact top-level integrity coverage, including every child manifest.

A draft-stage composite (`--status draft`) is allowed only from reviewed draft
children: it is structure-valid and queryable but not publishable, its lock binds
child manifest hashes (unsealed children bind the deterministic hash of `null`
integrity), and `seal_expert_skill.py --mark-ready` refuses to promote it until
every child is sealed and ready. Sealing a composite restamps the build moment so
`build.created_at`, `lock.created_at`, and `integrity.sealed_at` stay equal.

For an incremental update require a report binding the old and new package hashes.
Reject a stable ID whose semantic/evidence identity changed, a false-to-true
capability transition, or a missing invalidation of runtime, evaluation, seal, and
host-certification artifacts. The report is a rebuild plan, not evidence that the
new package has been rebuilt, source-verified, sealed, or host-certified.

### Phase 7B incremental build DAG gate

For an incremental plan, additionally require:

1. old and new immutable snapshots bind to explicit source/plan/workpack/package
   fingerprints; no plan may infer reuse from only the rows that remain in a
   mutable output directory;
2. every node has a content-addressed kind/stable identity, protocol, content
   hash, semantic/evidence identity hashes, and required receipt binding;
3. every typed edge has hash-derived endpoints, known node IDs, and a required
   edge type; dangling and cyclic graphs fail closed;
4. source/page/segment/module/parser/model/config/render/locator, external
   runtime/worker/raster-crop/OCR-observation/result-adapter,
   visual candidate/gap/conflict object/relation/grid/cell/overlay,
   semantic assertion/support, review plan/session, formula AST/test,
   runtime/competency, seal, host certificate, and composite changes propagate
   through the complete downstream closure;
5. every invalidation records a root cause, one or more typed paths, required
   action, final action, and final gate; a global invalidated-artifacts list is
   not sufficient evidence;
6. reuse requires exact content, protocol, dependency/input, reviewer
   requirement, capability, and receipt agreement. Stable-ID semantic/evidence
   drift, source identity changes, capability escalation, narrowed reviewer
   requirements, missing original nodes/receipts, tampered/replayed receipts,
   and input binding changes are fail-closed;
7. DAG manifest, node/edge inventory, change set, plan, state, and receipts are
   canonical JSON/JSONL with recomputable hashes. Recomputed manifests cannot
   authorize deletion or receipt loss because the job binds both old and new
   frozen DAG hashes;
8. `incremental-plan/status/resume` is idempotent and pause-only. Resume may
   record deterministic local extraction/rebuild receipts but must stop before
   structure/OCR/model, visual or semantic review, competency, seal, execution,
   host-certification, signing, and Composer gates;
9. the DAG is explicitly not semantic truth, automatic review, a signature,
   cryptographic reviewer identity, or cross-source Composer authorization.

### Phase 7C Semantic Composer gate

For a Composer candidate workpack additionally require:

1. every child package and candidate source binds an immutable SHA-256 identity;
   legacy source/evidence input is explicitly `legacy-source-support`, while
   current atomic support requires non-empty assertion IDs and exact support
   SHA-256 bindings; all Composer rows remain `promotion_eligible=false`;
2. concept refs, evidence bindings, alignments, differences, conflicts, unit
   maps, review items, and receipts have closed schemas, recomputable hashes,
   source/package/workpack/local identities, and exact evidence/support links;
3. candidate PDF evidence has a 1-based physical page, printed-label candidate,
   page/content hash, pypdf version, actual MediaBox/CropBox/rotation geometry,
   and the project `pdf-page-top-left-points-v1` coordinate space. Missing bbox
   fails closed unless explicitly marked `full-page-candidate`, non-exact, and
   visually review-required. A single render is bound only to its mapped page;
4. a frozen normalized PDF IR has safe role-labelled relative component paths,
   component hashes, canonical inventory hash, heading-task identity, and a
   candidate scope fully covered by the IR scope. Build/replay runs the local
   `verify_pdf_ir` verifier with network disabled; missing or opaque IR input is
   unverifiable;
5. eligibility is established before differences/conflicts/unit maps are
   computed. Labels, aliases, and explicit normalized terms are source-local
   typed phrases; Chinese single characters and prose-derived numbers/symbols
   cannot create semantic pairs. Lexical overlap remains a related/ambiguous
   candidate unless a proposer relation explicitly requests another relation;
6. numeric, unit, symbol, applicability, assumption, failure-condition, and
   contradiction risks require two independent reviewer slots; ordinary alias/
   concept candidates require one. The compiler may write a review plan but no
   reviewer attestation. Validation may detect registration/self-review/hash,
   stale/replay, evidence, and blanket-pattern failures only; identity remains
   host-recorded-not-cryptographic;
7. missing review returns `pending-independent-review` / pause-only state when
   structural and source replay gates pass. `review_integrity_issue_count` and
   `pending_semantic_review_items` are separate buckets and are not double
   counted. Any contract/source/IR failure is `blocked` and requires inspection;
8. Phase 7B includes Composer, alignment, difference, conflict, unit-map,
   review, receipt, parser, and locator nodes. Any source/object/support/unit/
   reviewer/protocol drift invalidates the downstream recompose/re-review
   closure. Composer output never authorizes merge, consensus, capability,
   signing, sealing, formula execution, or a ready release.

### Phase 7D visual extraction and parser orchestration gate

For a visual-parser job or visual parser snapshot, additionally require:

1. the unified `plan|build|validate|status|resume` orchestrator uses the fixed
   pypdf/renderer/isolated-worker/adapter boundaries, freezes source, page,
   render, parser, backend, model, config, transform, component, and job hashes,
   and records the provenance of every candidate;
2. native text remains first. OCR is allowed only for `pages_needing_ocr` or a
   fully bound explicit raster region containing physical page, PDF bbox,
   CropBox/rotation geometry, source anchor, render SHA-256, and crop SHA-256.
   The region is rendered and cropped with a reversible transform; the worker
   receives only the crop and expected crop hash, never the source PDF. Missing
   runtime/model/worker evidence fails closed and never widens a region into
   silent whole-page OCR. Non-contiguous selections are rejected unless each
   selected page is an explicit job scope;
3. `paddleocr-ppocrv6` is the explicit local text-candidate route and
   `paddleocr-ppstructure-v3` is the separate structured table/layout route.
   Docling is a challenger, and PyMuPDF is an optional geometry/image/drawing/
   table candidate with its AGPL/commercial licensing boundary. Missing
   dependencies/models, network/download flags, path overlap, stale runtime or
   model inventories, worker drift, and parser schema drift fail closed. PP-OCR
   text is never promoted to table topology. The `technical-table-v1` profile
   must bind PP-OCRv6 det/rec, layout, table-class, wired/wireless structure,
   and wired/wireless cell models, with constructor and predict switches and
   matching PaddleX model names explicitly supplied; chart/formula/seal/orientation/unwarping flags are not
   inherited from Paddle defaults;
   the separate PP-OCRv6 text qualification is limited to exactly three serial
   crop/page-image repeats in one short-lived local process, one model load, stable
   input/output/response hashes, and false network/download/MCP flags. Missing
   output commitments, warning drift, runtime/model/worker drift, or any policy
   violation rejects the receipt. This is repeatability evidence, not CER,
   accuracy, Gold, or semantic completeness;
4. fake backends are accepted only with explicit synthetic test-mode and
   test-only authorization markers plus a synthetic fixture/source. Their
   receipts and manifest say `synthetic_only`; real source/benchmark/release
   mixing is rejected, and synthetic rows cannot enter DAG, Composer, promotion,
   or downstream real-candidate routing;
5. visual objects use physical-page MediaBox/CropBox/rotation geometry in
   `mediabox-top-left` coordinates. Table grids bind rows/columns/cells,
   spans, typed numeric/unit/symbol candidates, caption/note/footnote routes,
   cell bbox/crop/source anchors, and grid hashes. Plot axes/ticks/units/
   legends/series/annotations/digitized values remain uncertain candidates;
   keyword presence alone is not visual confirmation. Native image/form
   geometry must replay PDF `q`/`Q`/`cm`/`Do`, recursively apply Form XObject
   matrices/BBoxes, transform corners into the page frame, and bind resource,
   stream, and object hashes. Caption matching may consume only an axis-adjacent
   image/form candidate; overlapping nested Form/Image candidates must not be
   emitted twice. Table frames prefer a bounded dominant ruling-line/rectangle
   cluster and reject abnormal tall paths that cross the footer; a text-only
   fallback is explicitly a gap candidate, not an exact frame;
   canonical visual IDs are canonical-JSON commitments over source/page,
   render/DPI/rotation, mapped PDF geometry, crop, parser/result-adapter,
   backend/runtime/worker, path-independent request/response-content, model,
   and config receipts. Adapter-local IDs cannot override them; table grids,
   cells, anchors, bindings, conflicts, overlays, and DAG aliases must be
   rekeyed together. Runtime elapsed/byte measurements and temporary crop paths
   are audit-only and must not perturb semantic reuse;
6. every disagreement is a conflict, ordinary OCR `kind=text` remains scan
   transcript/coverage evidence and creates no visual object, while an unknown
   visual kind fails closed. Structured tables require explicit row/column/cell
   indices, spans, bboxes, typed value/unit/symbol candidates, source anchors,
   and overlap-free `tkc.table-grid/v0.1` topology. PP-StructureV3 3.7
   `pred_html`/`cell_box_list`/`table_ocr_pred` joins must be exact or emit a
   gap/conflict; unknown result fields fail closed. Cross-page table continuity
   is a candidate/gap route, never an automatic confirmed merge;
7. the bundled strict JSON Schemas and runtime validators reject unknown nested
   fields, missing geometry/uncertainty/provenance, invalid bbox/rotation,
   mismatched source/crop/grid hashes, and tampered manifest/job/component/
   bundle bytes. Component paths must resolve inside the snapshot root and
   symlink components are forbidden;
8. DAG edges point from source/page/render/parser/config/model to visual object,
   from runtime/worker to raw result adapter, from adapter to visual candidate,
   gap/conflict, table cell and grid, and from all review-overlay inputs to the
   package/Composer intent. Runtime/worker/raster-crop/OCR-observation/result-
   adapter changes are typed prerequisites. Mutation tests must show model/
   config/render/crop/OCR/result/object/cell/relation/grid/overlay changes
   invalidate the complete typed downstream closure without cycles. A
   non-zero worker process exposes only `worker_process_failed`; stderr and
   exception details never enter canonical errors, release files, or public
   evidence;
   bounded scan structure is a separate review gate: title, physical page range,
   reading order, bbox, coordinate scope, observation/transcript span, source/
   render/transcript/model/config hashes, and an external review attestation must
   match exactly before a `resolved` structure is emitted. Blank, ambiguous,
   overlapping, or stale proposals remain paused/rejected and the accepted
   structure remains candidate-only;
   `verify_source_anchors.py` reports only scan-integrity without a matching local
   OCR runtime. Full transcript replay requires the exact runtime/model/config/
   worker contract and a matching transcript hash;
   scan transcript/structure/review mutations must invalidate re-extract, re-review,
   rebuild, recompose, and repackage through typed DAG closure;
9. evaluation labels distinguish deterministic `synthetic-gold` contract truth
   from a real `candidate-benchmark`. `verified_gold` and
   `independent_real_gold` remain false until an external independent review
   exists. Real backend accuracy is reported only after an actual local model
   run with versions/model/config hashes; otherwise it is `not-run`/`unverified`.
   Recommended F1/IoU/table targets are future acceptance targets, not current
   results.
10. P1 candidate routes are real and hash-bound: spatially distinct panel
    placements, diagram labels/arrows, chart digitization/error candidates,
    review-required cross-page continuation rows, and the closed
    `tkc.visual-review-overlay/v0.3` manifest with runtime/worker/model/crop/
    raw-result/normalized-result/cell
    receipt hashes. Nested Form/Image placements,
    overlay replay, self-parent, and self-relation cases must fail closed or
    normalize to one valid placement; none may author a reviewer verdict.

These gates establish reproducible candidate extraction and tamper-evident
routing. They do not establish semantic truth, visual correctness, reviewer
independence, model accuracy, automatic consensus, promotion, signing, formula
execution, or a capability upgrade.

## Capability truthfulness

- `reference` means the package can explain and trace at least one bounded topic.
- `decision_support` means decision rules include inputs, applicability, alternatives,
  uncertainty, and stop/escalation conditions.
- `executable` means deterministic procedures exist and declare permissions, risk,
  review, and failure behavior.

Do not infer a higher tier from polished prose.

For reference-only Phase 3 packages, the `decision` competency category must abstain
or escalate, and `out-of-scope` must abstain. A read-only retrieval script does not
turn on executable domain capability.

## Verification labels

Use one of:

- `external-source-required`
- `evidence-pack-verifiable`
- `self-contained-source-verifiable`

Do not label `source-required` packages self-contained.

## Failure reporting

Return stable issue codes, affected paths/IDs, and a concrete remediation. Do not
silently create evidence, lower a safety tier, or remove a failed competency test to
make validation pass.
