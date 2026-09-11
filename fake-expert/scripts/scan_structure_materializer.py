#!/usr/bin/env python3
"""Project an accepted external scan-structure review into a fresh Scan IR.

This utility never invokes OCR, a model, MCP, or a network route.  It preserves
the immutable OCR components, replays the existing strict structure validator,
and writes a new hash-bound IR beside (never over) the input IR.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping

from compiler_version import (
    SCAN_STRUCTURE_MATERIALIZER_COMPILER_VERSION,
    SCAN_STRUCTURE_MATERIALIZER_PROTOCOL,
)
from scanned_pdf_ocr import (
    ScanOcrError,
    _build_scan_anchor_candidates,
    _ensure_private_empty_output,
    _load_json,
    _load_jsonl,
    _load_scan_ir,
    _paths_overlap,
    build_scan_structure_review,
    scan_input_fingerprint,
    sha256_file,
    sha256_json,
    verify_scanned_pdf_ir,
    write_scanned_pdf_ir,
)


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _tool_sha256() -> str:
    return sha256_file(Path(__file__).resolve())


def _require_input_path(path: Path, *, directory: bool = False) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ScanOcrError(f"materializer_symlink_input_forbidden:{expanded}")
    resolved = expanded.resolve()
    if directory:
        if not resolved.is_dir():
            raise ScanOcrError(f"materializer_input_directory_missing:{resolved}")
    elif not resolved.is_file():
        raise ScanOcrError(f"materializer_input_file_missing:{resolved}")
    return resolved


def _native_components(root: Path) -> dict[str, Any]:
    return {
        "source": _load_json(root / "source.json"),
        "preflight": _load_json(root / "preflight.json"),
        "document_map": _load_json(root / "document-map.json"),
        "anchor_candidates": _load_jsonl(root / "anchor-candidates.jsonl"),
        "visual_review": _load_json(root / "visual-review.json"),
        "parser_receipt": _load_json(root / "parser-receipt.json"),
        "layout_candidates": _load_jsonl(root / "layout-candidates.jsonl"),
        "render_receipts": _load_jsonl(root / "render-receipts.jsonl"),
        "heading_candidates": _load_jsonl(root / "heading-candidates.jsonl"),
        "heading_resolutions": _load_jsonl(root / "heading-resolutions.jsonl"),
        "heading_locator_overrides": (
            _load_jsonl(root / "heading-locator-overrides.jsonl")
            if (root / "heading-locator-overrides.jsonl").is_file()
            else []
        ),
        "heading_overrides": (
            _load_jsonl(root / "heading-overrides.jsonl")
            if (root / "heading-overrides.jsonl").is_file()
            else []
        ),
    }


def materialize_reviewed_scan_structure(
    source_path: Path,
    ir_root: Path,
    proposal_path: Path,
    external_fragment_path: Path,
    output: Path,
    *,
    pdftoppm: Path | str = "pdftoppm",
) -> dict[str, Any]:
    """Write a fresh reviewed Scan IR after exact external-fragment replay."""

    source_path = _require_input_path(source_path)
    ir_root = _require_input_path(ir_root, directory=True)
    proposal_path = _require_input_path(proposal_path)
    external_fragment_path = _require_input_path(external_fragment_path)
    output_expanded = output.expanduser()
    if output_expanded.is_symlink():
        raise ScanOcrError(f"materializer_symlink_output_forbidden:{output_expanded}")
    output = output_expanded.resolve()
    if any(
        _paths_overlap(output, path)
        for path in (source_path, ir_root, proposal_path, external_fragment_path)
    ):
        raise ScanOcrError("materializer_input_output_overlap")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ScanOcrError(f"output_not_empty: {output}")

    verification = verify_scanned_pdf_ir(source_path, ir_root, pdftoppm=pdftoppm)
    if not verification.get("passed"):
        failures = ",".join(str(value) for value in verification.get("failures", []))
        raise ScanOcrError(f"scan_structure_materialization_input_invalid:{failures}")

    loaded = _load_scan_ir(ir_root)
    scan_ir = loaded["scan_ir"]
    existing_structure = scan_ir.get("reviewed_scan_structure")
    if isinstance(existing_structure, Mapping) and existing_structure.get("status") == "accepted":
        raise ScanOcrError("scan_structure_materialization_input_already_reviewed")
    proposal = _load_json(proposal_path)
    if not isinstance(proposal, Mapping) or set(proposal) != {"segments"}:
        raise ScanOcrError("scan_structure_materialization_proposal_invalid")
    fragment = _load_json(external_fragment_path)
    attestation = fragment.get("attestation") if isinstance(fragment, Mapping) else None
    proposer_instance = attestation.get("proposer_instance") if isinstance(attestation, Mapping) else None
    if not isinstance(proposer_instance, str) or not proposer_instance.strip():
        raise ScanOcrError("scan_structure_materialization_proposer_missing")

    native = _native_components(ir_root)
    source_record = native["source"]
    source_sha256 = sha256_file(source_path)
    if source_record.get("sha256") != source_sha256 or scan_ir.get("source_sha256") != source_sha256:
        raise ScanOcrError("scan_structure_materialization_source_binding_invalid")
    render_receipts = native["render_receipts"]
    render_by_page = {
        int(row["physical_page"]): row
        for row in render_receipts
        if isinstance(row, Mapping) and isinstance(row.get("physical_page"), int)
    }
    ocr_pages = [int(page) for page in scan_ir["ocr_pages"]]
    if len(render_by_page) != len(render_receipts) or not set(ocr_pages).issubset(render_by_page):
        raise ScanOcrError("scan_structure_materialization_render_set_invalid")

    request = {
        **_copy(proposal),
        "proposer_instance": proposer_instance.strip(),
        "review": {
            "external_fragment": {
                "path": str(external_fragment_path),
                "sha256": sha256_file(external_fragment_path),
                "provenance": "external-review-fragment",
            }
        },
    }
    structure_result = build_scan_structure_review(
        source_id=str(source_record["source_id"]),
        source_sha256=source_sha256,
        scope=scan_ir["scope"],
        primary_backend=str(scan_ir["backend_selection"]["primary"]),
        observations=loaded["observations"],
        transcripts=loaded["transcripts"],
        transforms=loaded["transforms"],
        backend_receipts=loaded["backend_receipts"],
        render_receipts=render_receipts,
        request=request,
    )
    structure = structure_result["structure"]
    if structure.get("status") != "accepted":
        raise ScanOcrError("scan_structure_materialization_review_not_accepted")

    proposed_segments = [
        {
            **segment,
            "has_children": False,
            "printed_page_label_candidates": [str(page) for page in segment["physical_pages"]],
            "heading_locator_resolution_id": next(
                row["resolution_id"]
                for row in structure_result["resolutions"]
                if row["segment_id"] == segment["id"]
            ),
        }
        for segment in structure_result["segments"]
    ]
    document_map = _copy(native["document_map"])
    document_map["structure_source"] = "bounded-scan-ocr-reviewed"
    document_map["segments"] = proposed_segments
    anchor_candidates = _build_scan_anchor_candidates(
        str(source_record["source_id"]),
        proposed_segments,
        structure_result["page_texts"],
        reviewed=True,
    )
    heading_candidates = structure_result["candidates"]
    heading_resolutions = structure_result["resolutions"]
    heading_locator_overrides = structure_result["overrides"]

    source_output = _copy(source_record)
    source_receipts = dict(source_output.get("pdf_ir_receipts") or {})
    source_receipts.update(
        {
            "document_map_sha256": sha256_json(document_map),
            "anchor_candidates_sha256": sha256_json(anchor_candidates),
            "heading_candidates_sha256": sha256_json(heading_candidates),
            "heading_resolutions_sha256": sha256_json(heading_resolutions),
            "scan_structure_review_sha256": structure["structure_sha256"],
        }
    )
    if heading_locator_overrides:
        source_receipts["heading_locator_overrides_sha256"] = sha256_json(heading_locator_overrides)
    source_output["pdf_ir_receipts"] = source_receipts

    output_scan_ir = _copy(scan_ir)
    output_scan_ir["reviewed_scan_structure"] = structure
    output_scan_ir["input_fingerprint"] = scan_input_fingerprint(
        source_sha256=source_sha256,
        render_receipts=[render_by_page[page] for page in ocr_pages],
        backend_receipts=loaded["backend_receipts"],
        coordinate_transforms=loaded["transforms"],
        raster_regions=[row for row in scan_ir.get("raster_regions", []) if isinstance(row, Mapping)],
        runtime_contracts=[row for row in scan_ir.get("runtime_contracts", []) if isinstance(row, Mapping)],
        table_grids=[row for row in scan_ir.get("table_grids", []) if isinstance(row, Mapping)],
        table_grid_bindings=[row for row in scan_ir.get("table_grid_bindings", []) if isinstance(row, Mapping)],
        visual_candidates=loaded["visual_candidates"],
        visual_gaps=loaded["visual_gaps"],
        visual_conflicts=loaded["visual_conflicts"],
        result_adapters=loaded["result_adapters"],
        spread_split_contracts=[row for row in scan_ir.get("spread_split_contracts", []) if isinstance(row, Mapping)],
        reviewed_structure=structure,
    )

    result = {
        **native,
        "source": source_output,
        "document_map": document_map,
        "anchor_candidates": anchor_candidates,
        "heading_candidates": heading_candidates,
        "heading_resolutions": heading_resolutions,
        "heading_locator_overrides": heading_locator_overrides,
        "scan_ir": output_scan_ir,
        "scan_checkpoint": loaded["checkpoint"],
        "ocr_observations": loaded["observations"],
        "ocr_transcripts": loaded["transcripts"],
        "ocr_backend_receipts": loaded["backend_receipts"],
        "coordinate_transforms": loaded["transforms"],
        "ocr_conflicts": loaded["conflicts"],
        "ocr_quality_routing": loaded["quality"],
        "ocr_worker_responses": loaded["worker_responses"],
        "ocr_worker_errors": (
            ["preserve-partial-worker-error-status"]
            if source_record.get("pdf_ir_receipts", {}).get("scan_receipt_status") == "partial-worker-errors"
            else []
        ),
        "visual_candidates": loaded["visual_candidates"],
        "visual_gaps": loaded["visual_gaps"],
        "visual_conflicts": loaded["visual_conflicts"],
        "result_adapters": loaded["result_adapters"],
    }
    _ensure_private_empty_output(output)
    write_scanned_pdf_ir(output, result)
    for path in output.iterdir():
        if path.is_file():
            os.chmod(path, 0o600)
    output_verification = verify_scanned_pdf_ir(source_path, output, pdftoppm=pdftoppm)
    if not output_verification.get("passed"):
        failures = ",".join(str(value) for value in output_verification.get("failures", []))
        raise ScanOcrError(f"scan_structure_materialization_output_invalid:{failures}")

    fragment_binding = structure.get("reviewer") if isinstance(structure.get("reviewer"), Mapping) else {}
    receipt = {
        "compiler_version": SCAN_STRUCTURE_MATERIALIZER_COMPILER_VERSION,
        "protocol": SCAN_STRUCTURE_MATERIALIZER_PROTOCOL,
        "tool_sha256": _tool_sha256(),
        "passed": True,
        "status": "reviewed-scan-ir-materialized",
        "source_sha256": source_sha256,
        "input_scan_ir_sha256": sha256_file(ir_root / "scan-ir.json"),
        "output_scan_ir_sha256": sha256_file(output / "scan-ir.json"),
        "structure_status": structure["status"],
        "structure_sha256": structure["structure_sha256"],
        "external_fragment_sha256": sha256_file(external_fragment_path),
        "attestation_sha256": fragment_binding.get("attestation_sha256"),
        "segment_count": len(proposed_segments),
        "ocr_page_count": len(ocr_pages),
        "observation_count": len(loaded["observations"]),
        "verification_level": output_verification["verification_level"],
        "ocr_invoked": False,
        "model_invoked": False,
        "mcp_invoked": False,
        "network_used": False,
        "promotable": False,
        "executable": False,
        "output": str(output),
    }
    receipt_path = output / "materialization-receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(receipt_path, 0o600)
    return {
        **receipt,
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256_file(receipt_path),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("materialize",))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--ir", required=True, type=Path)
    parser.add_argument("--proposal", required=True, type=Path)
    parser.add_argument("--external-fragment", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pdftoppm", default="pdftoppm")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        receipt = materialize_reviewed_scan_structure(
            args.source,
            args.ir,
            args.proposal,
            args.external_fragment,
            args.output,
            pdftoppm=args.pdftoppm,
        )
    except (ScanOcrError, OSError, ValueError) as error:
        if args.json:
            print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"ERROR: {error}", file=os.sys.stderr)
        return 2
    if args.json:
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(receipt["output"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
