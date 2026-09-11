# Phase 7D.2 — First-class visual objects, tables, and PP-StructureV3 adapter

Status: implemented for private compiler release `v0.12.0`. This is a
candidate-only, offline implementation outline and acceptance contract. It is
not a visual Gold record, reviewer attestation, accuracy certificate, or
permission to mutate the immutable `v0.11.0` release.

## Scope and non-goals

Figures, tables, layout blocks, chart/equation/annotation candidates, table
grids, cells, gaps, conflicts, and review routing are first-class records. Each
record carries a stable ID, 1-based physical page, exact or explicitly
non-exact geometry, source/render/crop/parser/backend/model/config/runtime/
worker/request/response hashes, candidate status, and review requirements.
OCR `kind=text` remains transcript/coverage evidence; it is never silently
converted into a visual object or table topology.

No model result is promoted, attested, signed, executable, called Gold, or
called visual truth by this phase. Presence/localization requires one
independent visual reviewer. Table values, chart values, and visual formulas
require two. The compiler may emit a review overlay and a pause-only DAG plan,
but cannot author reviewer identity, verdict, consensus, or promotion.

## Canonical architecture

```text
PDF -> pypdf native preflight / pdftoppm render
    -> explicit page bbox -> deterministic crop-only JSONL worker
    -> runtime profile + Paddle/PaddleX result receipt
    -> safe JSON/NumPy/result-object adapter
    -> visual candidates + gaps/conflicts + table-grid/cells
    -> source-free review overlay + Coverage/Support/Review routing
    -> Phase 7B typed DAG -> pause-only downstream Composer/repackage
```

The worker receives only a temporary crop and complete binding metadata. It does
not receive a PDF or source path and is not an MCP service. MCP remains an
optional challenger/performance adapter; its output cannot enter canonical
evidence without an equivalent independently verifiable receipt protocol.

## Explicit runtime profiles

`technical-text-v1` binds local PP-OCRv6 detector/recognizer directories.
`technical-table-v1` binds:

- `PP-DocLayout_plus-L` layout detection;
- `PP-OCRv6_medium_det` and `PP-OCRv6_medium_rec` text detection/recognition;
- `PP-LCNet_x1_0_table_cls` table classification;
- `SLANeXt_wired` and `SLANet_plus` wired/wireless structure recognition;
- `RT-DETR-L_wired_table_cell_det` and
  `RT-DETR-L_wireless_table_cell_det` wired/wireless cell detection.

Both constructor and predict dictionaries are frozen. The table profile sets
`use_table_recognition=true` and explicitly sets chart/formula/seal/region,
document-orientation, unwarping, text-line-orientation, table-orientation and
e2e switches to their intended values. A model directory is not sufficient:
PaddleX 3.7 model names are frozen alongside the directories, including
`PP-OCRv6_medium_det` and `PP-OCRv6_medium_rec`; this blocks the PP-StructureV3
default `PP-OCRv5_server_det` resolver from changing the selected text model.
each enabled component must have a bound directory with a non-empty, per-file
size/SHA-256 manifest. Stable model identity excludes mutable cache/lock files;
cache metadata is recorded separately. Any missing component, drift, implicit
download, or unbound switch pauses/fails closed.

## Result adapter contract

The adapter accepts the observed PaddleOCR/PaddleX 3.7 PP-StructureV3 result
fields `parsing_res_list`, `layout_det_res`, `overall_ocr_res`, and
`table_res_list`. It additionally handles table `pred_html`, `cell_box_list`,
and `table_ocr_pred`. Official result objects and NumPy-like arrays are converted
through a bounded JSON/mapping representation. Unknown object/field shapes are
errors, never guesses. The worker reports constructor, predict, and
result-adapter failures as stable stage/error codes without exception detail;
original failure receipts remain private and are not replaced by diagnostics.

HTML is parsed into deterministic row/column slots with `rowspan`/`colspan`.
Cell boxes must match HTML cell count, lie inside the table bbox, not overlap,
and align with OCR boxes. HTML/OCR text disagreement, unmatched OCR, topology
gaps, count mismatch, outside-table cells, and overlap emit explicit gap/conflict
rows. No value, unit, symbol, bbox, or cell identity is fabricated to make a
grid pass. Every normalized result stores `raw_result_sha256` and
`normalized_result_sha256`.

## IDs, provenance, and review routes

The canonical visual object ID is a canonical-JSON hash over source hash,
physical page, render hash/DPI/rotation, mapped PDF geometry, crop hash,
parser/result-adapter receipt, backend/runtime/worker receipt, a normalized
request commitment, stable response-content commitment, model receipt, and
config receipt. Crop-local `candidate_id`/`visual_object_id` values are never
accepted as canonical overrides; they remain adapter attribution fields. Table
grid and cell rekeying changes the table object ID, grid ID, cell IDs, and every
source anchor/binding atomically. Result adapters,
candidate rows, gaps, conflicts, cells, grids, overlays, runtime contracts,
worker responses and backend receipts all remain hash-addressed. A geometry is
`exact-from-runtime-result` only when the adapter provides it; otherwise it is
declared non-exact and review-required.

Ephemeral worker elapsed/byte measurements and temporary filesystem paths remain
private audit data and are excluded from semantic identity. The raw request and
raw response hashes are retained in receipts; the canonical request/response
commitments are path-independent and stable for the same crop and configuration.

Layout types are handled separately for table, figure, picture, chart, equation,
annotation, caption, and diagram. OCR text is routed to transcript/coverage.
The review overlay contains IDs, pages, geometry, and receipt hashes only. It
does not contain pixels, reviewer identity, verdicts, attestations, Gold, or
promotion.

## Phase 7B projection and invalidation

The visual parser snapshot projects explicit `runtime_result`,
`visual_candidate`, `visual_gap`, `visual_conflict`, `table_cell`, and
`review_overlay` nodes. Inputs bind runtime/model/config/raw result/normalized
result/render/crop/worker/receipt/object/grid/cell/conflict/overlay artifacts.
Changing a runtime/model/config/result/crop/geometry/topology/review input must
produce the full typed closure: `re-extract`, `re-review`, `rebuild`,
`recompose`, and `repackage` where applicable. Exact unchanged dependencies are
eligible for deterministic no-op reuse. Resume remains pause-only.

## Evidence classification and acceptance

Evidence is reported in separate buckets:

1. actual local runtime execution, with elapsed time, resource observations,
   versions, model/config/result hashes and process/OS isolation level;
2. synthetic/protocol behavior, including HTML topology, unknown-shape, worker,
   model-drift and DAG tests;
3. candidate benchmark output from authorized local crops, never an accuracy or
   Gold claim;
4. independent Gold (`false` until a separate reviewed Gold set exists);
5. pending review/coverage/support routes and unresolved gaps/conflicts.

The release is private/source-free. Real PDFs, renders, crops, OCR output,
model receipts and workpacks stay under ignored Phase 7D.2 scratch roots. No
network, cloud OCR, credential propagation, model download, PDF externalization,
or unverified macOS kernel network-isolation claim is permitted.
