#!/usr/bin/env python3
"""Isolated JSONL worker for explicitly selected local OCR backends.

The worker never receives the source PDF.  It receives a temporary rendered page
path plus hash-bound configuration and returns raw observation candidates.  A
missing optional backend is an explicit error; this module never falls back to a
different engine.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from compiler_version import (
    OCR_PROTOCOL_SCHEMA,
    PADDLE_OCR_RESULT_PROTOCOL,
    PADDLE_OCR_RESULT_SCHEMA,
)
from paddle_runtime_contract import (
    PADDLE_OCR_BACKEND,
    PADDLE_STRUCTURE_BACKEND,
    PADDLE_STRUCTURE_V3_BACKEND,
    PADDLE_CHART_BACKEND,
    PROFILE_MODEL_NAMES,
    PaddleRuntimeContractError,
    normalize_ppocrv6_result,
    normalize_ppstructure_v3_result,
    normalize_technical_chart_v1_result,
    profile_definition,
    normalize_ppstructure_v3_tables,
    sha256_file,
    sha256_json,
)

PROTOCOL_SCHEMA = OCR_PROTOCOL_SCHEMA


class _WorkerRequestTimeout(TimeoutError):
    """Hard per-request timeout raised by SIGALRM."""


def _version(distribution: str, fallback: str = "unknown") -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return fallback


def _error(request: dict[str, Any], code: str) -> dict[str, Any]:
    return {
        "schema_version": PROTOCOL_SCHEMA,
        "request_id": request.get("request_id"),
        "backend_id": request.get("backend_id"),
        "input_sha256": request.get("input_sha256"),
        "status": "error",
        "error_code": code,
    }


def _stage_error(
    request: dict[str, Any],
    stage: str,
    error: Exception,
    *,
    runtime_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable stage/error code and keep detail on private stderr only."""

    import re

    token = str(error).split(":", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        token = "exception"
    if token.startswith("paddle_"):
        token = token[len("paddle_") :]
    error_code = f"paddle_{stage}_{token or 'exception'}"
    # The parent qualification runner captures this stream into its explicit
    # 0600 private diagnostic file.  The JSONL response remains stable and
    # contains only the public error code plus the private safe event envelope.
    if os.environ.get("EFIREBLE_PRIVATE_DIAGNOSTIC") == "1":
        print(
            f"worker_exception stage={stage} type={type(error).__name__}: {error}",
            file=sys.stderr,
        )
    response = _error(request, error_code)
    if runtime_event is not None:
        response["_runtime_private_diagnostic"] = {
            **runtime_event,
            "status": "error",
            "stage": stage,
            "error_code": error_code,
            "error_category": re.sub(r"[^A-Za-z0-9_-]", "_", type(error).__name__) or "backend_exception",
        }
    return response


def _engine_instance_sha256(
    *,
    backend: str,
    profile_id: str,
    model_dirs: dict[str, str],
    constructor: dict[str, Any],
) -> str:
    """Hash the selected engine binding without exposing model paths."""

    return sha256_json(
        {
            "backend_id": backend,
            "profile_id": profile_id,
            "model_dirs": model_dirs,
            "constructor": constructor,
        }
    )


def _runtime_inventory() -> dict[str, Any]:
    names = ("paddleocr", "paddlepaddle", "paddlex", "opencv-contrib-python")
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    inventory = {
        "python": sys.version,
        "executable": str(sys.executable),
        "distributions": versions,
    }
    from paddle_runtime_contract import sha256_json

    inventory["inventory_sha256"] = sha256_json(inventory)
    return inventory


def _fake(request: dict[str, Any]) -> dict[str, Any]:
    observations = request.get("fixture_observations", [])
    if not isinstance(observations, list):
        return _error(request, "fake_fixture_observations_invalid")
    return {
        "schema_version": PROTOCOL_SCHEMA,
        "request_id": request["request_id"],
        "backend_id": "fake",
        "status": "ok",
        "engine": "synthetic-fake-backend",
        "engine_version": "fake-backend/1.0",
        "observations": observations,
    }


def _paddle(request: dict[str, Any], runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    model_root = request.get("model_root")
    if not isinstance(model_root, str) or not Path(model_root).is_dir():
        return _error(request, "paddle_model_root_missing")
    if any(key in request for key in ("source_path", "source_pdf", "pdf_path", "pdf_bytes")):
        return _error(request, "paddle_source_pdf_input_forbidden")
    image_path = request.get("image_path")
    if not isinstance(image_path, str) or not Path(image_path).is_file() or Path(image_path).is_symlink():
        return _error(request, "paddle_input_crop_missing")
    if request.get("input_kind") != "raster-crop":
        return _error(request, "paddle_input_kind_invalid")
    expected_input_sha = request.get("input_sha256")
    if not isinstance(expected_input_sha, str) or sha256_file(Path(image_path)) != expected_input_sha:
        return _error(request, "paddle_input_crop_hash_mismatch")
    try:
        import paddleocr  # type: ignore
    except Exception:
        return _error(request, "paddleocr_not_installed")
    options = request.get("configuration", {}).get("backend_options", {})
    if not isinstance(options, dict) or options.get("allow_model_download") is True:
        return _error(request, "paddle_model_download_forbidden")
    model_dirs = options.get("model_dirs")
    if not isinstance(model_dirs, dict) or not model_dirs:
        return _error(request, "paddle_model_dirs_required_for_offline_run")
    profile_id = options.get("profile_id")
    if not isinstance(profile_id, str):
        return _error(request, "paddle_profile_required")
    try:
        profile = profile_definition(profile_id)
    except PaddleRuntimeContractError as error:
        return _error(request, str(error))
    if options.get("constructor_options") != profile["constructor"]:
        return _error(request, "paddle_profile_constructor_switches_required")
    if options.get("predict_options") != profile["predict"]:
        return _error(request, "paddle_profile_predict_switches_required")
    if options.get("model_names") != PROFILE_MODEL_NAMES[profile_id]:
        return _error(request, "paddle_profile_model_names_required")
    if model_dirs != profile["model_dirs"]:
        return _error(request, "paddle_profile_model_bindings_mismatch")
    root = Path(model_root).resolve()
    resolved_model_dirs: dict[str, str] = {}
    for key, value in model_dirs.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return _error(request, "paddle_model_dir_config_invalid")
        raw_candidate = root / value
        if any(parent.is_symlink() for parent in (raw_candidate, *raw_candidate.parents) if parent != root):
            return _error(request, "paddle_model_dir_symlink_forbidden")
        candidate = raw_candidate.resolve()
        if root not in candidate.parents and candidate != root:
            return _error(request, "paddle_model_dir_escape")
        if candidate.is_symlink() or not candidate.is_dir():
            return _error(request, "paddle_model_dir_missing")
        resolved_model_dirs[key] = str(candidate)
    backend = str(request.get("backend_id"))
    api = options.get("api")
    if backend == PADDLE_OCR_BACKEND:
        if api != "paddleocr.predict":
            return _error(request, "paddle_api_must_be_explicit_paddleocr_predict")
        required = {"text_detection_model_dir", "text_recognition_model_dir"}
        if not required.issubset(resolved_model_dirs):
            return _error(request, "paddle_ppocrv6_models_missing")
    elif backend in {PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND}:
        if api != "ppstructure-v3":
            return _error(request, "paddle_api_must_be_explicit_ppstructure_v3")
        required = {
            "layout_detection_model_dir",
            "text_detection_model_dir",
            "text_recognition_model_dir",
            "table_classification_model_dir",
            "wired_table_structure_recognition_model_dir",
            "wireless_table_structure_recognition_model_dir",
            "wired_table_cells_detection_model_dir",
            "wireless_table_cells_detection_model_dir",
        }
        if not required.issubset(resolved_model_dirs):
            return _error(request, "paddle_ppstructure_models_missing")
    elif backend == PADDLE_CHART_BACKEND:
        if api != "paddleocr.chart_parsing":
            return _error(request, "paddle_api_must_be_explicit_chart_parsing")
        if not {"chart_model_dir"}.issubset(resolved_model_dirs):
            return _error(request, "paddle_chart_model_missing")
    else:
        return _error(request, "paddle_backend_identity_unknown")
    started = time.monotonic()
    runtime_cache = runtime if runtime is not None else {}
    cache_key = json.dumps(
        {
            "backend": backend,
            "profile_id": profile_id,
            "model_dirs": resolved_model_dirs,
            "device": str(options.get("device", "cpu")),
            "constructor": profile["constructor"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    engine = runtime_cache.get(cache_key)
    request_sequence = int(runtime_cache.get("_request_sequence", 0)) + 1
    runtime_cache["_request_sequence"] = request_sequence
    engine_reused = engine is not None
    engine_instance_sha256 = _engine_instance_sha256(
        backend=backend,
        profile_id=profile_id,
        model_dirs=resolved_model_dirs,
        constructor=profile["constructor"],
    )
    runtime_event: dict[str, Any] = {
        "sequence": request_sequence,
        "request_id": request.get("request_id"),
        "backend_id": backend,
        "profile_id": profile_id,
        "engine_instance_sha256": engine_instance_sha256,
        "engine_reused": engine_reused,
        "model_load_calls_before": int(runtime_cache.get("_model_load_calls", 0)),
        "predict_method": "predict",
        "predict_iter_available": False,
        "predict_iter_selected": False,
        "phase_trace": ["engine-reuse" if engine_reused else "engine-constructor"],
    }
    stage = "constructor"
    try:
        resolved_model_names = dict(PROFILE_MODEL_NAMES[profile_id])
        if engine is None:
            if backend == PADDLE_OCR_BACKEND:
                from paddleocr import PaddleOCR  # type: ignore

                engine = PaddleOCR(
                    **resolved_model_names,
                    text_detection_model_dir=resolved_model_dirs["text_detection_model_dir"],
                    text_recognition_model_dir=resolved_model_dirs["text_recognition_model_dir"],
                    device=str(options.get("device", "cpu")),
                    **profile["constructor"],
                )
            elif backend in {PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND}:
                from paddleocr import PPStructureV3  # type: ignore

                engine = PPStructureV3(
                    device=str(options.get("device", "cpu")),
                    **resolved_model_names,
                    **resolved_model_dirs,
                    **profile["constructor"],
                )
            else:
                from paddleocr import ChartParsing  # type: ignore

                engine = ChartParsing(
                    model_name=resolved_model_names["model_name"],
                    model_dir=resolved_model_dirs["chart_model_dir"],
                    **profile["constructor"],
                )
            runtime_cache[cache_key] = engine
            runtime_cache["_model_load_calls"] = int(runtime_cache.get("_model_load_calls", 0)) + 1
        runtime_event["model_load_calls_after"] = int(runtime_cache.get("_model_load_calls", 0))
        runtime_event["predict_iter_available"] = callable(getattr(engine, "predict_iter", None))
        if backend == PADDLE_OCR_BACKEND:
            stage = "predict"
            runtime_event["phase_trace"].append("predict")
            result = engine.predict(image_path, **profile["predict"])
            stage = "result_adapter"
            runtime_event["phase_trace"].append("result-adapter")
            observations = normalize_ppocrv6_result(
                result,
                require_mixed_language=bool(options.get("mixed_language_expected", False)),
            )
            raw_result = [
                _result_mapping(item) for item in result
            ] if isinstance(result, list) else _result_mapping(result)
            raw_pages = raw_result if isinstance(raw_result, list) else [raw_result]
            empty_text_candidate_count = sum(
                1
                for page in raw_pages
                if isinstance(page, dict)
                for text in page.get("rec_texts", [])
                if isinstance(page.get("rec_texts"), list)
                and isinstance(text, str)
                and not text.strip()
            )
            adapter_receipt = {
                "schema_version": PADDLE_OCR_RESULT_SCHEMA,
                "protocol": PADDLE_OCR_RESULT_PROTOCOL,
                "result_shape": "paddleocr-3.7-ocr-result-rec-fields-v1",
                "raw_result": raw_result,
                "raw_result_sha256": sha256_json(raw_result),
                "normalized_result_sha256": sha256_json(observations),
                "observations": observations,
                "empty_text_candidate_count": empty_text_candidate_count,
                "candidate_only": True,
                "promotion": False,
                "verified_gold": False,
            }
        elif backend in {PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND}:
            stage = "predict"
            runtime_event["phase_trace"].append("predict")
            result = engine.predict(image_path, **profile["predict"])
            stage = "result_adapter"
            runtime_event["phase_trace"].append("result-adapter")
            adapter_receipt = normalize_ppstructure_v3_result(
                result,
                source_sha256=str(request.get("source_sha256", "0" * 64)),
                physical_page=int(request.get("physical_page", 1)),
                table_visual_object_id=str(request.get("table_visual_object_id", "vo-" + "0" * 20)),
                crop_sha256=request.get("input_sha256"),
                image_dimensions=(request.get("render") or {}).get("image_dimensions_px"),
                require_mixed_language=bool(options.get("mixed_language_expected", False)),
            )
            observations = list(adapter_receipt.get("observations", []))
            tables = list(adapter_receipt.get("table_grids", []))
        else:
            stage = "predict"
            runtime_event["phase_trace"].append("predict")
            result = engine.predict({"image": image_path}, **profile["predict"])
            stage = "result_adapter"
            runtime_event["phase_trace"].append("result-adapter")
            adapter_receipt = normalize_technical_chart_v1_result(result)
            observations = []
            chart_rows = list(adapter_receipt.get("chart_rows", []))
    except PaddleRuntimeContractError as error:
        runtime_event["model_load_calls_after"] = int(runtime_cache.get("_model_load_calls", 0))
        return _stage_error(request, stage, error, runtime_event=runtime_event)
    except Exception as error:
        runtime_event["model_load_calls_after"] = int(runtime_cache.get("_model_load_calls", 0))
        return _stage_error(request, stage, error, runtime_event=runtime_event)
    runtime_event.update(
        {
            "status": "ok",
            "stage": "result_adapter",
            "model_load_calls_after": int(runtime_cache.get("_model_load_calls", 0)),
        }
    )
    return {
        "schema_version": PROTOCOL_SCHEMA,
        "request_id": request["request_id"],
        "backend_id": backend,
        "input_sha256": request.get("input_sha256"),
        "status": "ok",
        "engine": "PaddleOCR/PP-OCRv6" if backend == PADDLE_OCR_BACKEND else "PaddleOCR/PP-StructureV3" if backend in {PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND} else "PaddleOCR/ChartParsing",
        "engine_version": _version("paddleocr", str(getattr(paddleocr, "__version__", "unknown"))),
        "observations": observations,
        **({"table_grids": tables} if backend in {PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND} else {}),
        **({"visual_candidates": adapter_receipt.get("visual_candidates", []), "visual_gaps": adapter_receipt.get("gaps", []), "visual_conflicts": adapter_receipt.get("conflicts", [])} if backend in {PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND} else {}),
        **({"chart_candidates": adapter_receipt.get("chart_rows", []), "chart": adapter_receipt.get("chart", {}), "chart_gaps": adapter_receipt.get("gaps", []), "chart_conflicts": adapter_receipt.get("conflicts", [])} if backend == PADDLE_CHART_BACKEND else {}),
        "result_adapter": adapter_receipt,
        "raw_result_sha256": adapter_receipt.get("raw_result_sha256"),
        "normalized_result_sha256": adapter_receipt.get("normalized_result_sha256"),
        "output_sha256": adapter_receipt.get("normalized_result_sha256"),
        "profile_id": profile_id,
        "profile": profile,
        "runtime_inventory": _runtime_inventory(),
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        "candidate_only": True,
        "promotion": False,
        "_runtime_private_diagnostic": runtime_event,
    }


def _result_mapping(value: Any) -> dict[str, Any]:
    """Use the same safe result conversion for the OCR raw-result receipt."""

    from paddle_runtime_contract import _as_mapping

    return _as_mapping(value)


def _extract_paddle_observations(result: Any) -> list[dict[str, Any]]:
    """Compatibility name retained for callers; unknown shapes fail closed."""

    return normalize_ppocrv6_result(result)


def _docling(request: dict[str, Any]) -> dict[str, Any]:
    try:
        import docling  # type: ignore
        from docling.document_converter import DocumentConverter  # type: ignore
    except Exception:
        return _error(request, "docling_not_installed")
    options = request.get("configuration", {}).get("backend_options", {})
    if not isinstance(options, dict) or options.get("allow_model_download") is True:
        return _error(request, "docling_model_download_forbidden")
    if not isinstance(options.get("model_artifacts_path"), str):
        return _error(request, "docling_local_model_artifacts_required")
    artifacts = Path(options["model_artifacts_path"]).expanduser().resolve()
    model_root = request.get("model_root")
    if not isinstance(model_root, str) or not Path(model_root).is_dir():
        return _error(request, "docling_model_root_missing")
    root = Path(model_root).expanduser().resolve()
    if root not in artifacts.parents and artifacts != root:
        return _error(request, "docling_model_artifacts_escape")
    if not artifacts.is_dir():
        return _error(request, "docling_model_artifacts_missing")
    if options.get("api", "image-document-converter") != "image-document-converter":
        return _error(request, "docling_api_must_be_explicit_image_document_converter")
    try:
        converter = DocumentConverter(artifacts_path=str(artifacts))
        document = converter.convert(str(request["image_path"])).document
        observations = _extract_docling_observations(document)
    except Exception:
        return _error(request, "docling_execution_failed")
    return {
        "schema_version": PROTOCOL_SCHEMA,
        "request_id": request["request_id"],
        "backend_id": "docling",
        "status": "ok",
        "engine": "Docling",
        "engine_version": _version("docling", str(getattr(docling, "__version__", "unknown"))),
        "observations": observations,
    }


def _extract_docling_observations(document: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for collection_name in ("texts", "tables", "formulas"):
        for item in getattr(document, collection_name, []) or []:
            text = getattr(item, "text", None)
            if not isinstance(text, str) and collection_name == "tables":
                exporter = getattr(item, "export_to_markdown", None)
                text = exporter() if callable(exporter) else None
            if not isinstance(text, str) or not text.strip():
                continue
            for provenance in getattr(item, "prov", None) or []:
                bbox = getattr(provenance, "bbox", None)
                if bbox is None:
                    continue
                values = [getattr(bbox, key, None) for key in ("l", "t", "r", "b")]
                if any(value is None for value in values):
                    continue
                rows.append({"text": text, "bbox": values, "confidence": 0.0, "kind": collection_name.rstrip("s")})
    return rows


def _baseline(request: dict[str, Any]) -> dict[str, Any]:
    try:
        import pytesseract  # type: ignore
        import pymupdf  # type: ignore
    except Exception:
        return _error(request, "pymupdf_tesseract_not_installed")
    try:
        document = pymupdf.open(str(request["image_path"]))
        page = document[0]
        data = pytesseract.image_to_data(page.get_pixmap().pil_image(), output_type=pytesseract.Output.DICT)
        observations = []
        for index, text in enumerate(data.get("text", [])):
            if not str(text).strip():
                continue
            observations.append(
                {
                    "text": str(text),
                    "bbox": [data["left"][index], data["top"][index], data["left"][index] + data["width"][index], data["top"][index] + data["height"][index]],
                    "confidence": max(0.0, min(1.0, float(data["conf"][index]) / 100.0)),
                    "kind": "text",
                }
            )
        document.close()
    except Exception:
        return _error(request, "pymupdf_tesseract_execution_failed")
    return {
        "schema_version": PROTOCOL_SCHEMA,
        "request_id": request["request_id"],
        "backend_id": "pymupdf-tesseract",
        "status": "ok",
        "engine": "PyMuPDF/Tesseract",
        "engine_version": f"pymupdf={_version('PyMuPDF')};tesseract={_version('pytesseract')}",
        "observations": observations,
    }


def dispatch(backend: str, request: dict[str, Any], runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    if request.get("schema_version") != PROTOCOL_SCHEMA or request.get("backend_id") != backend:
        return _error(request, "protocol_request_invalid")
    if backend == "fake":
        return _fake(request)
    if backend in {PADDLE_OCR_BACKEND, PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND, PADDLE_CHART_BACKEND}:
        return _paddle(request, runtime)
    if backend == "docling":
        return _docling(request)
    if backend == "pymupdf-tesseract":
        return _baseline(request)
    return _error(request, "unsupported_backend")


def _close_runtime(runtime: dict[str, Any]) -> None:
    """Best-effort release of the single heavy model before worker exit."""

    for key, value in list(runtime.items()):
        if str(key).startswith("_"):
            continue
        close = getattr(value, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    runtime.clear()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True)
    args = parser.parse_args()
    runtime: dict[str, Any] = {}
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"schema_version": PROTOCOL_SCHEMA, "status": "error", "error_code": "protocol_json_invalid"}, sort_keys=True))
            continue
        request = request if isinstance(request, dict) else {}
        timeout_value = request.get("request_timeout_seconds")
        timed_out = False
        previous_handler = None
        previous_timer = (0.0, 0.0)
        try:
            timeout = float(timeout_value) if timeout_value is not None else 0.0
            if timeout < 0.0:
                raise ValueError
            if timeout == 0.0 and timeout_value is not None:
                timed_out = True
                response = _error(request, "worker_request_timeout")
            elif timeout > 0.0:
                previous_handler = signal.getsignal(signal.SIGALRM)
                previous_timer = signal.setitimer(signal.ITIMER_REAL, 0.0)
                def _alarm(_signum: int, _frame: Any) -> None:
                    raise _WorkerRequestTimeout()
                signal.signal(signal.SIGALRM, _alarm)
                signal.setitimer(signal.ITIMER_REAL, timeout)
                response = dispatch(args.backend, request, runtime)
            else:
                response = dispatch(args.backend, request, runtime)
        except _WorkerRequestTimeout:
            timed_out = True
            response = _error(request, "worker_request_timeout")
        except (AttributeError, OSError, ValueError):
            response = _error(request, "worker_hard_timeout_unavailable")
        finally:
            if previous_handler is not None:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
                signal.signal(signal.SIGALRM, previous_handler)
                if previous_timer[0] > 0.0:
                    signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])
        response.setdefault("model_load_calls", int(runtime.get("_model_load_calls", 0)))
        response.setdefault("runtime_contract_sha256", request.get("runtime_contract_sha256"))
        print(json.dumps(response, ensure_ascii=False, sort_keys=True), flush=True)
        if timed_out:
            _close_runtime(runtime)
            return 0
    _close_runtime(runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
