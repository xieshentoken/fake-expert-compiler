#!/usr/bin/env python3
"""Recompute PDF-IR parser and render receipts without exposing source text."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pdf_parser_adapters import (
    ParserAdapterError,
    build_parser_receipt,
    build_render_receipts,
    sha256_json,
)
from pdf_structure import PdfPipelineError, normalize_text, page_dimensions, sha256_file
from paddle_runtime_contract import PADDLE_OCR_BACKEND, PADDLE_STRUCTURE_V3_BACKEND
from heading_locator import (
    HEADING_LOCATOR_PROTOCOL,
    HeadingLocatorError,
    build_heading_locator_records,
    load_locator_overrides,
)
from scanned_pdf_ocr import ScanOcrError, verify_scanned_pdf_ir


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _check(actual: Any, expected: Any, name: str, failures: list[str]) -> None:
    if actual != expected:
        failures.append(f"{name}_mismatch")


def verify_pdf_ir(
    source_path: Path,
    ir_root: Path,
    *,
    pdftoppm: Path | str | None,
    docling_allow_network: bool,
    model_roots: dict[str, Path | None] | None = None,
    backend_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ir_root = ir_root.expanduser().resolve()
    files = {
        "source": "source.json",
        "preflight": "preflight.json",
        "document_map": "document-map.json",
        "visual_review": "visual-review.json",
        "anchor_candidates": "anchor-candidates.jsonl",
        "parser_receipt": "parser-receipt.json",
        "layout_candidates": "layout-candidates.jsonl",
        "render_receipts": "render-receipts.jsonl",
    }
    missing = [name for name in files.values() if not (ir_root / name).is_file()]
    if missing:
        raise PdfPipelineError(f"ir_missing_files: {missing}")
    source = _load_json(ir_root / files["source"])
    preflight = _load_json(ir_root / files["preflight"])
    document_map = _load_json(ir_root / files["document_map"])
    visual_review = _load_json(ir_root / files["visual_review"])
    anchor_candidates = _load_jsonl(ir_root / files["anchor_candidates"])
    receipt = _load_json(ir_root / files["parser_receipt"])
    candidates = _load_jsonl(ir_root / files["layout_candidates"])
    renders = _load_jsonl(ir_root / files["render_receipts"])
    heading_candidates_path = ir_root / "heading-candidates.jsonl"
    heading_resolutions_path = ir_root / "heading-resolutions.jsonl"
    heading_candidates = _load_jsonl(heading_candidates_path) if heading_candidates_path.is_file() else None
    heading_resolutions = _load_jsonl(heading_resolutions_path) if heading_resolutions_path.is_file() else None
    overrides_path = ir_root / "heading-overrides.jsonl"
    overrides = _load_jsonl(overrides_path) if overrides_path.is_file() else []
    locator_overrides_path = ir_root / "heading-locator-overrides.jsonl"
    locator_overrides = _load_jsonl(locator_overrides_path) if locator_overrides_path.is_file() else []
    source_path = source_path.expanduser().resolve()
    source_hash = sha256_file(source_path)
    scope = receipt.get("scope") if isinstance(receipt, dict) else None
    parser = receipt.get("layout_parser") if isinstance(receipt, dict) else None
    if (
        not isinstance(scope, dict)
        or not isinstance(scope.get("start_page"), int)
        or not isinstance(scope.get("end_page"), int)
        or not isinstance(parser, dict)
        or not isinstance(parser.get("id"), str)
        or not isinstance(receipt.get("canonical_text_extractor"), str)
    ):
        raise PdfPipelineError("ir_parser_receipt_invalid")

    try:
        from pypdf import PdfReader, __version__ as pypdf_version
    except ModuleNotFoundError as error:
        raise PdfPipelineError("pypdf is required to verify PDF IR") from error
    reader = PdfReader(str(source_path))
    start_page, end_page = scope["start_page"], scope["end_page"]
    if not 1 <= start_page <= end_page <= len(reader.pages):
        raise PdfPipelineError("ir_scope_out_of_source")
    dimensions = {
        page: page_dimensions(reader.pages[page - 1])
        for page in range(start_page, end_page + 1)
    }
    config = parser.get("configuration") if isinstance(parser.get("configuration"), dict) else {}
    try:
        expected_receipt, expected_candidates = build_parser_receipt(
            source_path,
            source_hash,
            start_page,
            end_page,
            dimensions,
            f"pypdf-{pypdf_version}",
            parser["id"],
            docling_formulas=config.get("formula_enrichment") is True,
            docling_allow_network=docling_allow_network,
        )
    except ParserAdapterError as error:
        raise PdfPipelineError(str(error)) from error

    failures: list[str] = []
    _check(source.get("sha256"), source_hash, "source_sha256", failures)
    _check(receipt, expected_receipt, "parser_receipt", failures)
    _check(candidates, expected_candidates, "layout_candidates", failures)
    receipts = source.get("pdf_ir_receipts") if isinstance(source, dict) else None
    if not isinstance(receipts, dict):
        failures.append("source_receipt_binding_missing")
    else:
        _check(receipts.get("parser_receipt_sha256"), sha256_json(receipt), "parser_receipt_hash", failures)
        _check(receipts.get("layout_candidates_sha256"), sha256_json(candidates), "layout_candidate_hash", failures)
        _check(receipts.get("render_receipts_sha256"), sha256_json(renders), "render_receipt_hash", failures)
        _check(receipts.get("preflight_sha256"), sha256_json(preflight), "preflight_hash", failures)
        _check(receipts.get("document_map_sha256"), sha256_json(document_map), "document_map_hash", failures)
        _check(receipts.get("visual_review_sha256"), sha256_json(visual_review), "visual_review_hash", failures)
        _check(receipts.get("anchor_candidates_sha256"), sha256_json(anchor_candidates), "anchor_candidate_hash", failures)
        if overrides:
            _check(
                receipts.get("heading_overrides_sha256"),
                sha256_json(overrides),
                "heading_overrides_hash",
                failures,
            )
            bound = {
                (
                    segment.get("title"),
                    segment.get("heading_override", {}).get("before_title"),
                )
                for segment in document_map.get("segments", [])
                if isinstance(segment, dict)
                and isinstance(segment.get("heading_override"), dict)
            }
            expected = {
                (row.get("new_title"), row.get("before_title")) for row in overrides
            }
            if bound != expected:
                failures.append("heading_override_unbound")
        elif receipts.get("heading_overrides_sha256") is not None:
            failures.append("heading_overrides_hash_mismatch")

        if source.get("schema_version") == "tkc.pdf-ir/v0.3":
            if heading_candidates is None:
                failures.append("heading_candidates_missing")
            if heading_resolutions is None:
                failures.append("heading_resolutions_missing")
            if heading_candidates is not None:
                _check(
                    receipts.get("heading_candidates_sha256"),
                    sha256_json(heading_candidates),
                    "heading_candidate_hash",
                    failures,
                )
            if heading_resolutions is not None:
                _check(
                    receipts.get("heading_resolutions_sha256"),
                    sha256_json(heading_resolutions),
                    "heading_resolution_hash",
                    failures,
                )
            if locator_overrides:
                _check(
                    receipts.get("heading_locator_overrides_sha256"),
                    sha256_json(locator_overrides),
                    "heading_locator_overrides_hash",
                    failures,
                )
            elif receipts.get("heading_locator_overrides_sha256") is not None:
                failures.append("heading_locator_overrides_hash_mismatch")

    if source.get("schema_version") == "tkc.pdf-ir/v0.3" and heading_candidates is not None and heading_resolutions is not None:
        raw_page_texts = {
            page: reader.pages[page - 1].extract_text() or ""
            for page in range(start_page, end_page + 1)
        }
        page_texts = {page: normalize_text(text) for page, text in raw_page_texts.items()}
        try:
            expected_heading_candidates, expected_heading_resolutions, _ = build_heading_locator_records(
                source_sha256=source_hash,
                segments=document_map.get("segments", []),
                raw_page_texts=raw_page_texts,
                page_texts=page_texts,
                parser_receipt=expected_receipt,
                layout_candidates=expected_candidates,
                render_receipts=renders,
                scope=(start_page, end_page),
                overrides=locator_overrides,
            )
        except HeadingLocatorError as error:
            raise PdfPipelineError(str(error)) from error
        _check(heading_candidates, expected_heading_candidates, "heading_candidates", failures)
        _check(heading_resolutions, expected_heading_resolutions, "heading_resolutions", failures)

    if renders:
        if pdftoppm is None:
            raise PdfPipelineError("pdftoppm_required_for_render_receipt_verification")
        first_renderer = renders[0].get("renderer") if isinstance(renders[0], dict) else None
        if not isinstance(first_renderer, dict) or not isinstance(first_renderer.get("dpi"), int):
            raise PdfPipelineError("ir_render_receipt_invalid")
        try:
            _, expected_renders = build_render_receipts(
                source_path,
                source_hash,
                range(start_page, end_page + 1),
                pdftoppm,
                dpi=first_renderer["dpi"],
            )
        except ParserAdapterError as error:
            raise PdfPipelineError(str(error)) from error
        _check(renders, expected_renders, "render_receipts", failures)
    elif receipts and receipts.get("render_receipt_status") != "deferred":
        failures.append("render_receipt_status")

    try:
        scan_result = verify_scanned_pdf_ir(
            source_path,
            ir_root,
            pdftoppm=pdftoppm,
            model_roots=model_roots,
            backend_config=backend_config,
        )
    except (ScanOcrError, OSError, ValueError) as error:
        scan_result = {
            "passed": False,
            "present": True,
            "failures": [f"scan_verifier_failed:{error}"],
            "stale_reasons": ["scan-ir"],
        }
    failures.extend(scan_result.get("failures", []))
    return {
        "passed": not failures,
        "source_sha256": source_hash,
        "scope": {"start_page": start_page, "end_page": end_page},
        "layout_parser": {"id": parser["id"], "version": parser.get("version")},
        "render_receipt_count": len(renders),
        "scan": scan_result,
        "failures": failures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--ir", required=True, type=Path)
    parser.add_argument("--pdftoppm", type=Path, default=Path("pdftoppm"))
    parser.add_argument("--docling-allow-network", action="store_true")
    parser.add_argument("--model-root", type=Path, help="Local primary scan model root for stale-receipt verification.")
    parser.add_argument("--challenger-model-root", type=Path)
    parser.add_argument("--baseline-model-root", type=Path)
    parser.add_argument("--backend-config", type=Path, help="Current scan backend JSON configuration for stale verification.")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = verify_pdf_ir(
            args.source,
            args.ir,
            pdftoppm=args.pdftoppm,
            docling_allow_network=args.docling_allow_network,
            model_roots={
                PADDLE_OCR_BACKEND: args.model_root,
                PADDLE_STRUCTURE_V3_BACKEND: args.model_root,
                "docling": args.challenger_model_root,
                "pymupdf-tesseract": args.baseline_model_root,
            },
            backend_config=(
                json.loads(args.backend_config.read_text(encoding="utf-8"))
                if args.backend_config is not None
                else None
            ),
        )
    except (PdfPipelineError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("PASS" if result["passed"] else "FAIL")
        for failure in result["failures"]:
            print(f"ERROR {failure}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
