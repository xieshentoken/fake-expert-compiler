# PDF intermediate representation v0.3

Read this reference before running or changing native PDF intake and structure
reconstruction.

## Command

```bash
python3 scripts/reconstruct_pdf.py /absolute/path/to/source.pdf \
  --start-page <physical-start> \
  --end-page <physical-end> \
  --layout-parser pypdf \
  --pdftoppm /absolute/path/to/pdftoppm \
  --output /absolute/path/to/empty-run-directory
```

Run with an environment that provides `pypdf` and a local `pdftoppm`. The command
refuses a non-PDF, encrypted PDF, invalid page range, non-empty output directory,
unavailable selected parser, or failed render. It renders each scoped physical page
only into a temporary directory, records its PNG hash, then removes the image.

Before intake on a new host, run the read-only prerequisite check:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/environment_preflight.py --json
```

## Outputs

- `source.json`: immutable source fingerprint, file identity, page count, and PDF
  metadata. It records the original filename but does not copy the source or page
  text.
- `preflight.json`: native-text quality, page hashes, fonts, images, equation/figure
  signals, OCR routing, explicit `page_role`/`page_disposition` values, visual-
  verification routing, and PDF-label versus visible-label comparison.
- `document-map.json`: reconstructed outline/heading hierarchy, bounded heading
  candidates/resolution receipts, and stable segment IDs. An outline destination is
  never silently treated as the body heading when a nearby body candidate is better.
  Segments are ordered deterministically by (first physical page, final-title
  offset within that page), so a bookmark tree whose order differs from physical
  page order still produces a map that passes the workpack `heading_order_invalid`
  check; the ordering is computed **after** heading overrides are applied so the
  offsets use the renamed titles the workpack locator will search for, and it is
  baked into the hash lock, never repaired by hand.
- `anchor-candidates.jsonl`: full-page normalized-text hashes tied to candidate
  segments. These are not final evidence anchors and have empty `supports` arrays.
- `visual-review.json`: pages and reason codes that must be rendered and checked.
- `parser-receipt.json`: source-SHA-bound parser identity/version/configuration,
  physical-page coverage, page dimensions, coordinate availability, and per-page
  candidate hashes.
- `layout-candidates.jsonl`: noncanonical, page-attributed layout candidates. Each
  candidate contains a physical page, normalized PDF-space bbox, parser identity,
  and a content hash; it never contains source text or an evidence decision.
- `render-receipts.jsonl`: per-page `pdftoppm` PNG SHA-256 receipts, bound to the
  source SHA, physical page, renderer version, DPI, and format. It contains no PNG.
- `heading-overrides.jsonl`: present only when a `--heading-overrides` file was
  supplied. Each row (`tkc.heading-override/v0.1`) binds an original segment title
  by hash to its corrected title, reviewer instance, rationale, and the physical
  page where the corrected text was seen.
- `heading-candidates.jsonl`: v0.2 hash-locked heading occurrences. Each row binds
  the source, physical page, raw occurrence, matching-only normalization, canonical
  page/span/line hashes, parser/version, optional render/layout bindings, structural
  constraints, method, score, and selection reason.
- `heading-resolutions.jsonl`: v0.2 deterministic resolution rows. Only a unique
  eligible candidate or a candidate above the fixed margin is selected; all other
  rows are `heading_resolution_required` and remain a structured pause.
- `heading-locator-overrides.jsonl`: optional v0.2 bounded occurrence selections,
  each bound to candidate/resolution before hashes, reviewer instance, and rationale.

The PDF-IR container files use schema ID `tkc.pdf-ir/v0.3`. Parser, layout, and
render receipt files have their own `tkc.pdf-*-receipt/v0.1` schema IDs. All
intentionally omit full extracted page text.

Since v0.3.2, `source.json` hash-locks the complete IR container: `preflight.json`,
`document-map.json`, `visual-review.json`, `anchor-candidates.jsonl`, heading
candidate/resolution files, and (when present) override files are all covered by SHA-256 receipts in
`pdf_ir_receipts`. The receipts are written only after every derived file has been
serialized, so editing any IR file after reconstruction invalidates the lock. This
replaces the historical practice of editing `document-map.json` directly: segment
titles are corrected by rebuilding with a bounded override file, never by hand.

## Parser boundary

`pypdf==6.10.0` is always the canonical text extractor for native-text status,
normalized evidence offsets, segment IDs, candidate anchor hashes, and later source
verification. `--layout-parser` is an explicit local opt-in and has no silent
fallback:

- `pypdf` (default) records page dimensions and an honest `coordinate_status` of
  `unavailable`; it does not invent bboxes.
- `pymupdf` records hashed word-level layout candidates in
  `pdf-page-top-left-points-v1` coordinates. Install its optional local adapter
  first.
- `docling` records hashed text/table/formula layout candidates only when every
  item has an attributable physical page and normalizable bbox. Formula enrichment
  is disabled unless `--docling-formulas` is explicit (or `EFIREBLE_DOCLING_FORMULAS`
  enables it; any value other than `0`/`false`/`off` is on). The adapter selects an
  accelerator device (`mps` → `cuda` → `cpu`) and passes the `AcceleratorDevice`
  enum to Docling ≥ 2.x (string fallback for older APIs); the effective
  formula-enrichment source and accelerator are recorded in the parser receipt so
  accelerated runs stay auditable. Use only preinstalled local
  Docling models unless separate network authorization exists. The default sets the
  common Hugging Face/Transformers offline flags; `--docling-allow-network` is a
  separate, explicit authorization surface.

No parser candidate may become an evidence anchor, normalized formula, knowledge
object, visual receipt, review verdict, or promoted capability by itself. The workpack
still regenerates fresh render assets and the independent semantic/visual review
remains mandatory.

The workpack accepts a complete historical `tkc.pdf-ir/v0.1` directory only as
`legacy-unbound`: it retains its original evidence and review gates, but has no parser
or intake-render receipt. Reconstruct it again before relying on coordinate candidates
or comparing parser/render receipts.

## Routing rules

Treat OCR and visual verification as separate decisions:

- Route missing or very low native text without a confirmed-blank proof to `pages_needing_ocr`. The current compiler
  must stop when this list is non-empty.
- `page_role=image-only-or-scanned` and `page_disposition=ocr-required` identify
  pages that a future OCR adapter may reprocess. `blank-or-unusable` pages are
  routed to manual review; they are not silently discarded.
- `confirmed-blank` / `preserve-blank` retains the original physical page in
  `confirmed_blank_pages` and the source scope, without OCR/visual tasks, body spans
  or anchor pages. An entirely blank scope stays ineligible. A blank-only scoped
  bookmark segment remains in `document-map.json`, but produces no semantic unit
  and needs no body-title locator; segments with any content keep the locator gate.
  Whole-book plans list blank pages as `non-knowledge` without downgrading chapters
  that contain native body text. Mixed scan quality routes also forbid blank anchors.
- `page_diagnostic` (`tkc.native-page-diagnostic/v0.1`) always gives a classification,
  reason codes and proof/null. `page_summary` and `confirmed_blank_pages` are explicit
  in whole-source, reconstruct and workpack summaries. The old `pages_needing_ocr`
  remains a native-profile hard-stop superset; automatic OCR budgets use only
  `classification=needs_ocr`, while uncertain/short-text/vector pages need review.
- The shared `native-blank-page-v0.1` rule requires empty normalized native text,
  an explicit non-painting operator allowlist, no annotations/unsupported page
  features, and a strict-white full MediaBox render at 150 DPI using local
  `pdftoppm`. Unused shared image resources are allowed; any actual image/Form
  invocation, stroke, shading, text showing,
  unknown operations, render warning/failure, or non-white pixel prevents exemption.
  No fuzzy image threshold, OCR, model or network is used. Without a usable renderer
  the ordinary OCR/manual gate remains.
- `native-white-vector-blank-v0.1` additionally accepts finite, bounded rectangles
  filled with exact DeviceGray/RGB/CMYK white, using restricted graphics state and
  the same complete strict-white render. q/Q restores fill and matrix, including
  the default black fill. Every operation must qualify: black content later
  covered in white, near-white that rasterizes to 255, thin strokes, opacity/gs,
  clipping, unknown colors and geometry remain content/review. A pure-white raster
  image is still an invoked image, not an automatic blank proof. No drawing-count
  or near-white percentage can authorize blankness.
- Each `blank_page_proof` binds source SHA-256, physical page, content-stream hash,
  pypdf version, full MediaBox/rotation/pixel bounds, and renderer version/binary
  SHA-256 plus full RGB PPM render SHA-256. Temporary render payloads are removed.
  These proofs are part of the preflight hash and native extraction bindings;
  `verify_pdf_ir.py` replays them from the source even if component hashes were
  recomputed. Semantic workpacks preserve proofs in `page-index.jsonl` and replay
  them on validation; duplicated/misplaced/changed/missing proofs fail closed.
- Route equations, math fonts, suspicious decoded glyph names, figures, tables, and
  raster images to `pages_needing_visual_verification`, even when native text is
  otherwise usable.
- A text-based classification means only that native extraction is usable. It does
  not certify formula, figure, table, or reading-order fidelity.

The detector is conservative. Semantic review may remove a false-positive visual
route, but must not silently accept a formula or diagram merely because extraction
succeeded.

## Structure and page boundaries

Prefer the PDF outline, then inspect a bounded window around each destination. The
v0.2 locator records exact, Unicode-equivalent, dehyphenated, and compact-boundary
title candidates with role labels, hashed line evidence, structural constraints,
and optional layout bboxes. Running headers/footers are excluded. If the top
candidates tie below the deterministic margin, the resolution stays
`heading_resolution_required`; the planner may continue routing, but semantic
workpack construction cannot silently choose an occurrence. A bounded locator
override must bind before hashes and a reviewer instance. Missing outline entries
may still be planned as explicit `gap`/resolution tasks rather than being guessed.

Keep these locators separate:

- physical PDF page number, 1-based;
- PDF page-label-tree value;
- visible printed page-number candidate inferred from headers or footers.

For full-page evidence hashes, use the page-envelope policy
`include-next-heading-page`. A section that ends partway through the page on which the
next heading begins must retain that boundary page, because a whole-page hash cannot
separate text above and below the heading. Adjacent candidate envelopes may therefore
overlap. Preserve this policy in the segment record.

Normalize each page with Unicode NFKC, collapse whitespace, and join multiple pages
with form feed (`\f`) before SHA-256. This must remain identical to
`verify_source_anchors.py`.

Coordinates use physical PDF pages (1-based) and `pdf-page-top-left-points-v1`; do
not substitute visible printed labels. A render receipt is complete only when its
physical-page set equals the selected scope and its source SHA equals `source.json`.
Changing a parser, parser version/configuration, renderer version/DPI, source bytes,
or layout candidate invalidates the corresponding receipt hash and requires a new IR.
The lock is complete: `verify_pdf_ir.py` recomputes the parser and render receipts,
the container-file receipts, and the complete candidate/resolution bundle. It
rejects any IR whose container files were modified after reconstruction (failure
codes end in `_mismatch`, e.g. `document_map_hash_mismatch`). A heading override that does not resolve to exactly
one existing segment title fails reconstruction (`heading_override_unresolved`,
`heading_override_duplicate`, `heading_override_binding_invalid`, or
`heading_override_page_out_of_scope`). Repeated titles (for example a `Problems`
section in every chapter) are disambiguated with the optional `segment_id`, or
with the required `physical_page` matching the selected segment's first physical
page; when both are supplied they must agree, otherwise the override fails closed.
An IR whose override copy was edited away
fails verification with `ir_heading_override_unbound`. Before semantic
interpretation, a host can independently recompute these receipts without printing
page text:

```bash
python3 scripts/verify_pdf_ir.py \
  --source /absolute/path/to/source.pdf \
  --ir /absolute/path/to/pdf-ir \
  --pdftoppm /absolute/path/to/pdftoppm
```

## Generic replay for Phase 7C Composer inputs

When a Composer input freezes `normalized_pdf_ir`, the compiler records a
canonical role-labelled inventory rather than trusting an opaque aggregate hash.
The inventory uses safe relative paths for exactly these roles: `source`,
`document_map`, `parser_receipt`, `heading_resolutions`, and `structure_task`;
each path and component SHA-256 is frozen, and the sorted map is hashed with
`pdf-ir-canonical-inventory-v1`. The IR root is supplied only through
`--candidate-ir-root`; the compiler contains no book-specific directory
assumption. The source component must bind the candidate PDF SHA, the parser
receipt must expose the physical scope, and the structure task must bind its
heading task ID/hash and remain `pending-independent-structure-review`.

Build and replay call the local `verify_pdf_ir` routine with
`docling_allow_network=False`. Component-map equality alone is insufficient:
receipt/parser/render/heading consistency and the candidate-scope-inside-IR
scope condition are separately checked. Missing root, path escape/role
mismatch, component tamper, source drift, stale heading task, opaque hash, or
verification failure is `unverifiable`/fail-closed. Candidate visual evidence
uses actual MediaBox/CropBox/rotation geometry and
`pdf-page-top-left-points-v1`; a full-page candidate bbox is never presented as
an exact locator.

## Promotion boundary

Do not copy candidate anchors directly into a ready Expert Skill. Before promotion:

1. render every routed page and resolve formula/figure/glyph risks;
2. select, split, or merge candidates around actual knowledge claims;
3. create typed knowledge objects;
4. populate bidirectional `supports` and `evidence_ids` links;
5. record conflicts or gaps instead of repairing source content silently;
6. verify final anchor hashes against the external source.

## 原生页面处置证明

`native-outside-print-marks-v0.1` 扩展自动判空：每个已绘制路径及包含 CTM、线宽、端帽和
miter 的保守描边包络必须严格在完整 MediaBox 外，并且完整 MediaBox 的 150 DPI
Poppler RGB 渲染逐通道全 255、无警告。PDF 合法负宽高矩形可作受限裁剪路径；裁剪/白像素
本身不能掩盖页内内容。未知操作、外部 opacity/混合状态、图像、文字、页内细线和被白色覆盖
的黑内容不获得自动空白豁免。旧 nonpainting/opaque-white proofs 保留兼容回放。

`native-outline-structure-page-v0.1` 只保留低文本 Part/Chapter 结构页：原生文字与同物理页
outline 目的标题精确紧凑匹配，文字几何/可见状态可核对，裁剪覆盖标题 glyph，非文字绘制只能
通过受限页外标记证明。页内图形、图像、隐藏/白色文字、未知操作留复核。页面保留原生 text
和物理页，不降低正文 40 字符阈值，不生成该页的知识 focus span/证据锚。
仅该精确证明可定位其自身结构父节点，不豁免其他 heading gate。

实际操作人可用独立入口准备待填任务，外部完成后验证、导入新收据：

```bash
python3 scripts/native_page_disposition.py prepare --source /path/book.pdf \
  --pages 12 13 --output /path/pending-submission.json
# 操作人亲自填写 operator_id、operator_attestation、每页 role 与 reason 后：
python3 scripts/native_page_disposition.py validate --source /path/book.pdf \
  --submission /path/completed-submission.json --operator-id ACTUAL_OPERATOR
python3 scripts/native_page_disposition.py import --source /path/book.pdf \
  --submission /path/completed-submission.json --operator-id ACTUAL_OPERATOR \
  --output /path/new-receipt.json
python3 scripts/fake_expert.py plan --source /path/book.pdf --start-page 10 --end-page 30 \
  --page-dispositions /path/new-receipt.json --workspace /path/fresh-workspace --json
```

编译器不填写身份、理由、同意或 attestation。收据是 **operator-provided transport**，不提供
身份认证、签名、独立知识 Review 或 Gold。`blank` 还要求无原生文字且完整渲染全白；有可见
内容的页面只能由操作人明确选择 `structural/context/non-knowledge`。所有处置都保留原页，
不产生该页正文知识证据。绑定 source/content/text/render/renderer/pypdf/geometry/rule；
重复页、错源、改渲染、缺身份/理由、缺证明或规则漂移失败。

IR、workpack page-index 与 manifest commitments 逐级保留/回放证明。Draft 的
`references/runtime/direct-reference.json` 在每个 workpack binding 下保留
`page_disposition_protocol`、实际 `native_minimum_characters` 阈值和逐物理页 role/proof_protocol/proof_sha256，不复制完整人工收据、
操作人身份或来源标题。`extend` 保留所有旧绑定并新增新范围绑定，连续性规则不变。

```bash
python3 scripts/verify_source_anchors.py /path/to/fake-reference --source /path/book.pdf \
  --page-dispositions /path/new-receipt.json --json
```

自动 blank/structure 可用原件重算；人工处置必须显式提供匹配收据，可重复传入多个。
缺失收据报告 `page_disposition_receipt_required`、`full_replay=false`，不会虚称已回放。
匹配回放仅证明处置输入和绑定完整性，不认证操作人判断或知识准确率。
