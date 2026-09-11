#!/usr/bin/env python3
"""Report the local prerequisites for the fake-expert PDF compiler.

This command is intentionally read-only.  It never installs packages, opens a
source PDF beyond its metadata/native-text preflight, or writes a cache.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


PINNED_PYPDF = "6.10.0"
MINIMUM_PYTHON = (3, 11)
PROFILES = ("native-text", "scan-text", "scan-structure", "visual")

# These are diagnostic declarations, not backend selection.  In particular,
# a missing optional package never causes this module to install, download, or
# silently choose a different parser.
PROFILE_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "native-text": {
        "required_checks": ["python", "pypdf", "pdftoppm"],
        "route": "pypdf-native-text",
        "backends": [],
    },
    "scan-text": {
        "required_checks": ["python", "pypdf", "pdftoppm"],
        "route": "explicit-local-scan-text-backend",
        "backends": ["paddleocr-ppocrv6", "pymupdf-tesseract", "docling"],
    },
    "scan-structure": {
        "required_checks": ["python", "pypdf", "pdftoppm"],
        "route": "explicit-local-structured-scan-backend",
        "backends": ["paddleocr-ppstructure-v3"],
    },
    "visual": {
        "required_checks": ["python", "pypdf", "pdftoppm"],
        "route": "explicit-local-visual-review",
        "backends": ["pdftoppm", "paddleocr-ppstructure-v3", "docling"],
    },
}

BACKEND_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "pypdf-native": (),
    "pdftoppm": ("pdftoppm",),
    "paddleocr-ppocrv6": ("paddleocr", "paddle"),
    "paddleocr-ppstructure-v3": ("paddleocr", "paddle"),
    "pymupdf-tesseract": ("pymupdf", "pytesseract"),
    "docling": ("docling",),
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _check_python() -> dict[str, Any]:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    passed = sys.version_info[:2] >= MINIMUM_PYTHON
    return {
        "name": "python",
        "code": "python_version_ok" if passed else "python_version_unsupported",
        "passed": passed,
        "version": version,
        "required": f">={MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}",
        "remediation": [] if passed else ["Use Python 3.11 or newer."],
        "meaning": "The interpreter satisfies the compiler runtime contract." if passed else "The interpreter is older than the pinned compiler runtime.",
        "next_action": "continue" if passed else "use_python_3_11_or_newer",
    }


def _check_pypdf() -> dict[str, Any]:
    try:
        module = importlib.import_module("pypdf")
        version = str(getattr(module, "__version__", "unknown"))
        passed = version == PINNED_PYPDF
        remediation = [] if passed else [f"Install the compiler pin: pypdf=={PINNED_PYPDF}."]
    except Exception as error:  # pragma: no cover - message is host-dependent
        version = None
        passed = False
        remediation = [f"Install the compiler pin: pypdf=={PINNED_PYPDF}."]
        error_text = str(error)
    result: dict[str, Any] = {
        "name": "pypdf",
        "code": "pypdf_pin_ok" if passed else "pypdf_pin_mismatch",
        "passed": passed,
        "version": version,
        "required": PINNED_PYPDF,
        "remediation": remediation,
        "meaning": "Canonical native-text extraction uses the pinned pypdf version." if passed else "Canonical evidence extraction cannot be trusted with a different pypdf version.",
        "next_action": "continue" if passed else "install_pypdf_pin_in_existing_environment",
    }
    if "error_text" in locals():
        result["error"] = error_text
    return result


def _check_renderer(command: str) -> dict[str, Any]:
    resolved = Path(command).expanduser() if Path(command).expanduser().is_file() else None
    if resolved is None:
        found = shutil.which(command)
        resolved = Path(found).resolve() if found else None
    if resolved is None or not resolved.is_file():
        return {
            "name": "pdftoppm",
            "code": "renderer_missing",
            "passed": False,
            "path": None,
            "version": None,
            "remediation": ["Install Poppler and expose the pdftoppm executable, or pass --pdftoppm /absolute/path/to/pdftoppm."],
            "meaning": "The canonical local visual renderer is unavailable.",
            "next_action": "provide_existing_pdftoppm",
        }
    try:
        completed = subprocess.run(
            [str(resolved), "-v"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        version_text = (completed.stderr or completed.stdout).splitlines()[0]
        passed = completed.returncode == 0
    except (OSError, subprocess.SubprocessError) as error:
        version_text = None
        passed = False
        error_text = str(error)
    result: dict[str, Any] = {
        "name": "pdftoppm",
        "code": "renderer_ok" if passed else "renderer_unusable",
        "passed": passed,
        "path": str(resolved),
        "version": version_text,
        "remediation": [] if passed else ["Use a runnable Poppler pdftoppm binary."],
        "meaning": "The canonical local visual renderer is runnable." if passed else "The renderer exists but did not pass a local version probe.",
        "next_action": "continue" if passed else "provide_runnable_pdftoppm",
    }
    if "error_text" in locals():
        result["error"] = error_text
    return result


def _check_optional_module(
    name: str,
    import_name: str | None = None,
    *,
    applies_to: list[str] | None = None,
    blocking_when_selected: list[str] | None = None,
    recommendation: str = "Use only when this backend is explicitly selected and locally qualified.",
) -> dict[str, Any]:
    import_name = import_name or name
    try:
        module = importlib.import_module(import_name)
        version = str(getattr(module, "__version__", "installed"))
        return {
            "name": name,
            "code": "optional_backend_available",
            "passed": True,
            "version": version,
            "optional": True,
            "applies_to": applies_to or [],
            "blocks_profiles": blocking_when_selected or [],
            "blocking_when_selected": blocking_when_selected or [],
            "meaning": "The optional adapter is importable; this is not a runtime or accuracy qualification.",
            "next_action": "explicitly_select_and_qualify_if_needed",
            "recommendation": recommendation,
        }
    except Exception:
        return {
            "name": name,
            "code": "optional_backend_unavailable",
            "passed": False,
            "version": None,
            "optional": True,
            "applies_to": applies_to or [],
            "blocks_profiles": blocking_when_selected or [],
            "blocking_when_selected": blocking_when_selected or [],
            "meaning": "The optional adapter is unavailable in this environment.",
            "next_action": "keep_route_paused_or_provide_existing_local_dependency",
            "recommendation": recommendation,
        }


def _check_source(path_value: str, minimum_native_characters: int) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "signature": False,
        "sha256": None,
        "page_count": None,
        "native_text_pages": None,
        "pages_needing_ocr": [],
        "scan_candidate_route": "native-text-first",
        "manual_review_pages": [],
        "passed": False,
        "code": "source_missing",
        "meaning": "An existing local PDF is required for source preflight.",
        "next_action": "provide_existing_pdf",
        "remediation": [],
    }
    if not path.is_file():
        result["remediation"] = ["Provide an existing PDF path."]
        return result
    result["signature"] = path.read_bytes()[:5] == b"%PDF-"
    result["sha256"] = _sha256_file(path)
    if not result["signature"]:
        result["code"] = "source_not_pdf"
        result["meaning"] = "The supplied file does not have a PDF signature."
        result["next_action"] = "provide_valid_pdf"
        result["remediation"] = ["The input does not have a PDF signature."]
        return result
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            result["code"] = "source_encrypted"
            result["meaning"] = "The PDF is encrypted and cannot enter canonical native-text preflight."
            result["next_action"] = "decrypt_pdf_in_authorized_local_workflow"
            result["remediation"] = ["Decrypt the PDF using an authorized local workflow before intake."]
            return result
        native_pages = 0
        pages_needing_ocr: list[int] = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            normalized = _normalize(text)
            alphanumeric = sum(character.isalnum() for character in normalized)
            if alphanumeric >= minimum_native_characters:
                native_pages += 1
            else:
                pages_needing_ocr.append(page_number)
        result["page_count"] = len(reader.pages)
        result["native_text_pages"] = native_pages
        result["pages_needing_ocr"] = pages_needing_ocr
        result["manual_review_pages"] = [
            page_number
            for page_number, page in enumerate(reader.pages, start=1)
            if not (page.extract_text() or "").strip() and not page.get("/Resources")
        ]
        result["passed"] = not pages_needing_ocr
        if pages_needing_ocr:
            result["code"] = "source_requires_ocr"
            result["meaning"] = "One or more pages lack enough native text for the native-text route."
            result["next_action"] = "route_explicit_scan_or_ocr_candidate"
            result["remediation"] = [
                "Route image-only or low-text pages through the planned OCR adapter; current native-text compilation remains fail-closed."
            ]
        else:
            result["code"] = "source_native_text_ok"
            result["meaning"] = "All pages satisfy the configured native-text threshold."
            result["next_action"] = "continue"
    except Exception as error:  # pragma: no cover - pypdf host errors vary
        result["code"] = "source_preflight_error"
        result["meaning"] = "The pinned pypdf runtime could not complete source preflight."
        result["next_action"] = "rerun_with_pinned_pypdf"
        result["remediation"] = ["Re-run with the pinned pypdf runtime and inspect the local parser environment."]
    return result


def build_report(
    *,
    renderer: str,
    source: str | None,
    minimum_native_characters: int,
    profile: str = "native-text",
    backend: str | None = None,
) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"profile_invalid:{profile}")
    allowed_backends = tuple(PROFILE_REQUIREMENTS[profile]["backends"])
    if profile == "native-text":
        selected_backend = "pypdf-native" if backend is None else backend
        backend_selection_valid = selected_backend == "pypdf-native"
        backend_selection_code = "backend_native_fixed" if backend_selection_valid else "backend_invalid_for_profile"
    elif backend is None:
        selected_backend = None
        backend_selection_valid = False
        backend_selection_code = "backend_selection_required"
    else:
        selected_backend = str(backend)
        backend_selection_valid = selected_backend in allowed_backends
        backend_selection_code = "backend_selected" if backend_selection_valid else "backend_invalid_for_profile"
    checks = [
        _check_python(),
        _check_pypdf(),
        _check_renderer(renderer),
        _check_optional_module(
            "pymupdf", "pymupdf", applies_to=["scan-text", "visual"],
            blocking_when_selected=["scan-text", "visual"],
            recommendation="Use the existing local PyMuPDF/Tesseract route only after explicitly freezing it in a job.",
        ),
        _check_optional_module(
            "docling", applies_to=["scan-text", "scan-structure", "visual"],
            blocking_when_selected=["scan-text", "scan-structure", "visual"],
            recommendation="Use Docling only as an explicitly selected local candidate adapter; never treat it as canonical truth.",
        ),
        _check_optional_module(
            "paddleocr", applies_to=["scan-text", "scan-structure", "visual"],
            blocking_when_selected=["scan-text", "scan-structure", "visual"],
            recommendation="Provide an existing locally qualified PaddleOCR environment only when the selected route requires it; do not download models.",
        ),
        _check_optional_module(
            "paddle", applies_to=["scan-text", "scan-structure", "visual"],
            blocking_when_selected=["scan-text", "scan-structure", "visual"],
            recommendation="Paddle is an optional runtime dependency, not a selection signal or qualification receipt.",
        ),
        _check_optional_module(
            "pytesseract", applies_to=["scan-text", "visual"],
            blocking_when_selected=["scan-text", "visual"],
            recommendation="Use the existing local Tesseract route only when explicitly selected and separately bound.",
        ),
    ]
    source_report = _check_source(source, minimum_native_characters) if source else None
    required_names = set(PROFILE_REQUIREMENTS[profile]["required_checks"])
    backend_check_names = BACKEND_REQUIREMENTS.get(selected_backend, ()) if backend_selection_valid and selected_backend is not None else ()
    required_names.update(backend_check_names)
    backend_prerequisites_passed = bool(backend_selection_valid) and all(
        check["passed"] for check in checks if check.get("name") in set(backend_check_names)
    )
    required_passed = all(
        check["passed"] for check in checks if check.get("name") in required_names
    )
    if not backend_selection_valid:
        required_passed = False
    if source_report is not None:
        required_passed = required_passed and bool(source_report["passed"])
    missing_checks = sorted(
        check["name"] for check in checks
        if check.get("name") in required_names and not check.get("passed")
    )
    backend_missing_checks = sorted(
        check["name"] for check in checks
        if check.get("name") in set(backend_check_names) and not check.get("passed")
    )
    profile_passed = required_passed and profile == "native-text"
    blocking: list[str] = []
    if not backend_selection_valid:
        blocking.append(backend_selection_code)
    blocking.extend(f"missing_required_check:{name}" for name in missing_checks)
    if source_report is not None and not source_report["passed"]:
        blocking.append("source_preflight_failed")
    if profile != "native-text" and backend_selection_valid and backend_prerequisites_passed:
        blocking.append("runtime_qualification_required")
    if not backend_selection_valid:
        backend_code = backend_selection_code
        backend_next_action = "select_backend_explicitly" if profile != "native-text" else "use_pypdf_native_text"
    elif not backend_prerequisites_passed:
        backend_code = "backend_prerequisites_missing"
        backend_next_action = "provide_selected_backend_dependencies"
    elif profile != "native-text":
        backend_code = "backend_runtime_qualification_required"
        backend_next_action = "run_existing_specialist_runtime_qualification"
    else:
        backend_code = "backend_native_ready"
        backend_next_action = "use_pypdf_native_text"
    backend_status = {
        "selected": selected_backend,
        "allowed": list(allowed_backends) if allowed_backends else ["pypdf-native"],
        "required_checks": list(backend_check_names),
        "selection_valid": backend_selection_valid,
        "prerequisites_passed": backend_prerequisites_passed,
        "qualification_required": profile != "native-text",
        "qualification_status": "not-applicable" if profile == "native-text" else "not-checked",
        "code": backend_code,
        "next_action": backend_next_action,
        "missing_checks": backend_missing_checks,
    }
    return {
        "schema_version": "tkc.environment-preflight/v0.1",
        "profile": profile,
        "profiles": list(PROFILES),
        "profile_requirements": PROFILE_REQUIREMENTS[profile],
        "passed": profile_passed,
        "profile_ready": profile_passed,
        "blocking": blocking,
        "backend": backend_status,
        "compiler_policy": {
            "pypdf": f"{PINNED_PYPDF}",
            "native_text_only": True,
            "network_required": False,
            "scan_ocr_network": "disabled",
            "model_download": False,
            "primary_scan_backend": "paddleocr-ppocrv6",
            "structured_scan_backend": "paddleocr-ppstructure-v3",
            "challenger_scan_backend": "docling",
            "baseline_scan_backend": "pymupdf-tesseract",
            "silent_backend_fallback": False,
            "bytecode_cache_recommended": False,
            "backend_selection": "explicit-only",
            "auto_install": False,
            "auto_download": False,
            "auto_fallback": False,
        },
        "checks": checks,
        "source": source_report,
        "remediation": [
            remediation
            for check in checks
            for remediation in check.get("remediation", [])
        ]
        + ([] if source_report is None else source_report.get("remediation", [])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdftoppm", default="pdftoppm", help="Renderer name or absolute path.")
    parser.add_argument("--source", type=Path, help="Optional PDF to include in the read-only source preflight.")
    parser.add_argument("--minimum-native-characters", type=int, default=40)
    parser.add_argument("--profile", choices=PROFILES, default="native-text")
    parser.add_argument("--backend", help="Explicit backend identity; required for non-native profiles.")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    report = build_report(
        renderer=args.pdftoppm,
        source=str(args.source) if args.source else None,
        minimum_native_characters=args.minimum_native_characters,
        profile=args.profile,
        backend=args.backend,
    )
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{'PASS' if report['passed'] else 'FAIL'} environment-preflight profile={report['profile']}")
        for check in report["checks"]:
            suffix = " (optional)" if check.get("optional") else ""
            print(f"{'PASS' if check['passed'] else 'FAIL'} {check['name']}{suffix}: {check.get('version') or 'unavailable'}")
        if report.get("source") is not None:
            source = report["source"]
            print(f"{'PASS' if source['passed'] else 'FAIL'} source: pages={source.get('page_count')} ocr_pages={len(source.get('pages_needing_ocr', []))}")
        for remediation in report["remediation"]:
            print(f"REMEDIATION {remediation}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
