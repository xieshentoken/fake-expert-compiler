"""Synthetic crop-only protocol worker. No Paddle import, engine or real OCR."""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import sys

# The test creates this worker with a separate module search path file. It never
# uses this fixture for a user PDF or exposes a production fake-backend switch.
sys.path.insert(0, Path(__file__).with_suffix(".scripts").read_text())
from compiler_version import PADDLE_OCR_RESULT_PROTOCOL, PADDLE_OCR_RESULT_SCHEMA
from paddle_runtime_contract import DEFAULT_PADDLE_DISTRIBUTIONS, normalize_ppocrv6_result, sha256_json

inventory = {"python": sys.version, "executable": sys.executable,
             "distributions": {name: importlib.metadata.version(name) for name in DEFAULT_PADDLE_DISTRIBUTIONS}}
inventory["inventory_sha256"] = sha256_json(inventory)
FAIL_PAGE = None
for line in sys.stdin:
    request = json.loads(line)
    assert request["input_kind"] == "raster-crop"
    assert not {"source_pdf", "source_path", "pdf_path", "pdf_bytes"}.intersection(request)
    with Path(__file__).with_suffix(".calls").open("a") as log:
        log.write(request["request_id"] + "\n")
    if request.get("physical_page") == FAIL_PAGE:
        print(json.dumps({"schema_version": request["schema_version"], "request_id": request["request_id"],
                          "backend_id": request["backend_id"], "runtime_contract_sha256": request["runtime_contract_sha256"],
                          "input_sha256": request["input_sha256"], "status": "error", "error_code": "synthetic_input_failure",
                          "model_load_calls": 1}), flush=True)
        continue
    raw = [{"rec_texts": ["Synthetic bounded OCR system"], "rec_scores": [0.99], "rec_boxes": [[5, 5, 30, 15]]}]
    observations = normalize_ppocrv6_result(raw)
    adapter = {"schema_version": PADDLE_OCR_RESULT_SCHEMA, "protocol": PADDLE_OCR_RESULT_PROTOCOL,
               "result_shape": "paddleocr-3.7-ocr-result-rec-fields-v1", "raw_result": raw,
               "raw_result_sha256": sha256_json(raw), "normalized_result_sha256": sha256_json(observations),
               "observations": observations, "empty_text_candidate_count": 0,
               "candidate_only": True, "promotion": False, "verified_gold": False}
    print(json.dumps({"schema_version": request["schema_version"], "request_id": request["request_id"],
                      "backend_id": request["backend_id"], "runtime_contract_sha256": request["runtime_contract_sha256"],
                      "input_sha256": request["input_sha256"], "status": "ok", "observations": observations,
                      "engine": "synthetic protocol fixture; no model", "engine_version": "synthetic",
                      "result_adapter": adapter, "raw_result_sha256": adapter["raw_result_sha256"],
                      "normalized_result_sha256": adapter["normalized_result_sha256"],
                      "output_sha256": adapter["normalized_result_sha256"], "runtime_inventory": inventory,
                      "model_load_calls": 1, "candidate_only": True, "promotion": False}), flush=True)
