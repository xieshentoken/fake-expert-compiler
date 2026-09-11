# Phase 7D.4 — real local Paddle runtime qualification and chart benchmark

Status: implementation authorized for private fake-expert v0.14.0. This phase
qualifies an exact installed runtime/profile route. It is not OCR accuracy,
independent Gold, visual review, semantic promotion, signing, or execution.

## 1. Purpose

Phase 7D.4 closes the gap between “model files exist” and “this exact local
runtime/profile can safely execute the crop-only protocol.” PP-StructureV3 and
Chart2Table must bind the interpreter, package inventory, selected model-file
identity, worker, engine configuration, fixture crop, warnings, timeouts, and
repeatability before a new v0.14 heavy route can run.

The qualification receipt is host-local and source-free. It is consumed by the
visual parser job and Phase 7B DAG, but is excluded from the distributable
compiler release. The release contains only the tool and schemas.

## 2. Closed profiles

- `technical-table-v1`: the existing PP-StructureV3 table-only profile with
  explicit PP-OCRv6, layout, classifier, wired/wireless structure, and cell
  model names/directories.
- `technical-chart-v2`: `paddleocr.ChartParsing`, model and directory
  `PP-Chart2Table`, CPU, `enable_hpi=false`, `engine=paddle_dynamic`, and
  `predict(batch_size=1)`.
- `technical-chart-v1`: read-only v0.13 compatibility only. It is not the
  default for a new v0.14 job and cannot satisfy the v2 qualification gate.

The `PP-Chart2Table_safetensors` route is not selected. A warning that weights
were newly initialized, require training, have key/shape mismatch, or attempt a
download/network access rejects qualification.

## 3. Qualification flow

```text
local PNG crop fixture
  -> freeze exact external runtime/model/profile/worker contract
  -> one short-lived worker process
  -> first request uses startup timeout
  -> later requests use per-request timeout
  -> three repeated requests, serial, model_load_calls == 1
  -> normalize result shape and compare normalized hashes
  -> reduce stderr to hash + allowlisted warning codes (never raw stderr)
  -> qualified or rejected host-local receipt
  -> visual route gate / Phase 7B runtime_qualification node
```

Only `raster-crop` input is accepted. A PDF path, PDF bytes, source path,
network/model-download flag, unmatched runtime contract, changed model/config/
worker hash, more than one resident model, nondeterministic normalized output,
or blocking warning fails closed.

## 4. Commands

Create one private plan per route:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/paddle_runtime_qualification.py plan \
  --backend paddleocr-ppstructure-v3 \
  --interpreter /absolute/local/venv/bin/python \
  --runtime-root /absolute/local/venv \
  --model-root /absolute/local/model-root \
  --fixture /absolute/local/table-crop.png \
  --output /absolute/private/table-plan.json \
  --planned-at 2026-08-24T00:00:00+08:00

PYTHONDONTWRITEBYTECODE=1 python3 scripts/paddle_runtime_qualification.py run \
  --plan /absolute/private/table-plan.json \
  --output /absolute/private/empty-table-qualification \
  --tested-at 2026-08-24T01:00:00+08:00 \
  --exclusive-heavy-model-confirmed

PYTHONDONTWRITEBYTECODE=1 python3 scripts/paddle_runtime_qualification.py verify \
  --plan /absolute/private/table-plan.json \
  --receipt /absolute/private/empty-table-qualification/qualification-receipt.json
```

Repeat with `--backend paddleocr-chart-parsing` and a chart crop. Pass the
verified receipt to a new visual job with `--runtime-qualification`. A missing,
rejected, stale, or differently bound receipt produces
`paused-runtime-unqualified`; it does not render a crop or launch the model.
The run confirmation flag is an operator assertion that no other heavy OCR/model
service is resident. The receipt also binds the qualification plan SHA-256,
qualification compiler version, script SHA-256, and explicit tested-at label.

## 5. Resource and privacy policy

- At most one heavy model process is started by the qualification runner.
- Requests are serial; the engine is constructed once and closed at process
  exit. The tool does not start or depend on an MCP HTTP service.
- Do not run direct qualification while another local OCR service holds a heavy
  model on a 16GB host. Stop/restart that service through an operator-controlled
  action; the compiler does not stop it automatically. The `run` command refuses
  to start without the explicit exclusive-heavy-model confirmation flag.
- Raw stderr, PDF bytes, source paths, model paths, worker responses, fixture
  paths, and results stay in the private plan/results directory. The receipt
  contains hashes, counts, safe codes, and policy flags only.

## 6. Evaluation and evidence labels

Qualification proves only that the exact route loaded once, returned the
allowlisted adapter shape, and repeated deterministically under the declared
limits. A real book crop without independent labels remains
`candidate-benchmark`, `verified_gold=false`, and `accuracy_claim=false`.

Chart evaluation adds row/column topology, cell exactness, numeric exactness,
and hallucinated-numeric counts. Those metrics are meaningful only with an
externally supplied bounded Gold set; otherwise they remain not-run.

## 7. DAG and update closure

The Phase 7B snapshot emits a `runtime_qualification` node keyed by receipt hash.
It depends on the exact runtime/config contract and feeds runtime results and
chart candidates. Runtime inventory, selected models, profile, worker,
configuration, warning status, fixture, or receipt drift invalidates the
re-extract -> re-review -> recompose -> repackage closure. It cannot authorize
review, promotion, sealing, signing, or execution.

## 8. Acceptance

1. Ordinary tests do not import or load Paddle models.
2. Schema/profile/warning/hash/tamper/resource/timeout/one-load tests pass.
3. Each real route, when resources are available, completes three repeated crop
   requests in one process with `model_load_calls=1`, one normalized result hash,
   no blocking warning, and a verified receipt.
4. A missing/rejected/stale receipt pauses before render/inference.
5. A changed qualification receipt appears as a typed DAG invalidation root.
6. The private v0.14.0 archive is deterministic, source-free, contains no
   qualification receipt/model/image/PDF/path, and v0.13.0 remains byte-immutable.
