#!/usr/bin/env python3
"""Build a deterministic intermediate representation for a born-digital PDF scope."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pdf_structure import (
    PdfPipelineError,
    compile_pdf_intermediate_representation,
    write_intermediate_representation,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Born-digital source PDF.")
    parser.add_argument("--start-page", type=int, default=1, help="First physical PDF page (1-based).")
    parser.add_argument("--end-page", type=int, help="Last physical PDF page (1-based, inclusive).")
    parser.add_argument("--output", required=True, type=Path, help="Empty output directory.")
    parser.add_argument(
        "--minimum-native-characters",
        type=int,
        default=40,
        help="Minimum alphanumeric characters for usable native text.",
    )
    parser.add_argument(
        "--anchor-strategy",
        choices=("leaf", "all"),
        default="all",
        help="Generate candidates for leaf segments or all reconstructed segments.",
    )
    parser.add_argument(
        "--layout-parser",
        choices=("pypdf", "pymupdf", "docling"),
        default="pypdf",
        help=(
            "Optional local layout parser. It supplies only hash-bound coordinate "
            "candidates; pypdf remains the canonical evidence text extractor."
        ),
    )
    parser.add_argument(
        "--docling-formulas",
        action="store_true",
        help=(
            "Enable Docling formula enrichment only when --layout-parser=docling. "
            "When omitted, EFIREBLE_DOCLING_FORMULAS (0/false/off disables, anything "
            "else enables) is honored; the effective value is recorded in the parser receipt."
        ),
    )
    parser.add_argument(
        "--docling-allow-network",
        action="store_true",
        help=(
            "Permit Docling model-network access. Use only after separate source and "
            "network authorization; the default forces common model hubs offline."
        ),
    )
    parser.add_argument(
        "--heading-overrides",
        type=Path,
        help=(
            "JSONL of bounded heading corrections (tkc.heading-override/v0.1) applied "
            "deterministically before the IR is hash-locked."
        ),
    )
    parser.add_argument(
        "--heading-locator-overrides",
        type=Path,
        help=(
            "JSONL of hash-bound occurrence-selection overrides "
            "(tkc.heading-locator-override/v0.2). This does not correct the title."
        ),
    )
    parser.add_argument(
        "--pdftoppm",
        type=Path,
        default=Path("pdftoppm"),
        help="Local renderer used to bind a PNG SHA-256 receipt for every scoped page.",
    )
    parser.add_argument(
        "--render-dpi",
        type=int,
        default=150,
        help="DPI for deterministic PDF-IR render receipts (36-1200).",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = compile_pdf_intermediate_representation(
            args.source,
            args.start_page,
            args.end_page,
            args.minimum_native_characters,
            args.anchor_strategy,
            args.layout_parser,
            args.pdftoppm,
            args.render_dpi,
            args.docling_formulas,
            args.docling_allow_network,
            args.heading_overrides,
            args.heading_locator_overrides,
        )
        write_intermediate_representation(args.output, result)
    except PdfPipelineError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    summary = {
        "passed": True,
        "source_id": result["source"]["source_id"],
        "source_sha256": result["source"]["sha256"],
        "scope": result["preflight"]["scope"],
        "classification": result["preflight"]["scope_classification"],
        "eligible_for_current_compiler": result["preflight"]["eligible_for_current_compiler"],
        "pages_needing_ocr": result["preflight"]["pages_needing_ocr"],
        "pages_needing_visual_verification": result["preflight"][
            "pages_needing_visual_verification"
        ],
        "segment_count": len(result["document_map"]["segments"]),
        "anchor_candidate_count": len(result["anchor_candidates"]),
        "layout_parser": result["parser_receipt"]["layout_parser"],
        "layout_candidate_count": len(result["layout_candidates"]),
        "render_receipt_count": len(result["render_receipts"]),
        "heading_override_count": len(result["heading_overrides"]),
        "heading_candidate_count": len(result.get("heading_candidates", [])),
        "heading_resolution_count": len(result.get("heading_resolutions", [])),
        "heading_resolution_required": sum(
            row.get("status") == "heading_resolution_required"
            for row in result.get("heading_resolutions", [])
            if isinstance(row, dict)
        ),
        "output": str(args.output.expanduser().resolve()),
    }
    if args.json_output:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "PASS "
            f"source={summary['source_id']} "
            f"classification={summary['classification']} "
            f"segments={summary['segment_count']} "
            f"anchors={summary['anchor_candidate_count']} "
            f"layout_parser={summary['layout_parser']['id']} "
            f"layout_candidates={summary['layout_candidate_count']} "
            f"render_receipts={summary['render_receipt_count']} "
            f"ocr_pages={len(summary['pages_needing_ocr'])} "
            f"visual_pages={len(summary['pages_needing_visual_verification'])} "
            f"heading_resolution_required={summary['heading_resolution_required']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
