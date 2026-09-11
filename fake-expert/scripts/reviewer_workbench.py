#!/usr/bin/env python3
"""Offline Phase 7D.5B independent visual Gold reviewer workbench.

The workbench is a human-form transport layer, not a second Gold protocol.
It copies only the already-frozen page render and crop into a private reviewer
instance, embeds locked candidate identity, and exports ordinary form data.
``finalize`` re-reads the canonical 7D.5A workpack, verifies every identity and
provenance binding, and emits the existing visual-benchmark Gold rows only when
the external review gate is complete.  The explicit ``prepare-single-page``
command may render and crop a bounded PDF locally; every other command only
copies frozen assets.  No command invokes a model, reads a prediction, uses the
network, or authors Gold.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import struct
import sys
import unicodedata
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from compiler_version import (
    PORTABLE_VISUAL_REVIEW_COMPILER_VERSION,
    PORTABLE_VISUAL_REVIEW_WORKPACK_MANIFEST_SCHEMA,
    PORTABLE_VISUAL_REVIEW_WORKPACK_PROTOCOL,
    PORTABLE_GOLD_REVIEW_COMPILER_VERSION,
    PORTABLE_GOLD_REVIEW_DECISION_SCHEMA,
    PORTABLE_GOLD_REVIEW_PROTOCOL,
    PORTABLE_GOLD_REVIEW_REVISION_SCHEMA,
    VISUAL_BENCHMARK_ATTESTATION_SCHEMA,
    VISUAL_BENCHMARK_GOLD_FRAGMENT_SCHEMA,
    VISUAL_BENCHMARK_PROTOCOL,
    VISUAL_REVIEW_WORKBENCH_ATTESTATION_BUNDLE_SCHEMA,
    VISUAL_REVIEW_WORKBENCH_COMPILER_VERSION,
    VISUAL_REVIEW_WORKBENCH_MANIFEST_SCHEMA,
    VISUAL_REVIEW_WORKBENCH_PROTOCOL,
    VISUAL_REVIEW_WORKBENCH_REVIEWER_REGISTRY_SCHEMA,
    VISUAL_REVIEW_WORKBENCH_REVIEW_MANIFEST_SCHEMA,
    VISUAL_REVIEW_WORKBENCH_SUBMISSION_SCHEMA,
    VISUAL_REVIEW_CONVENTION_COMPILER_VERSION,
    VISUAL_REVIEW_CONVENTION_PROTOCOL,
    VISUAL_REVIEW_CONVENTION_PROFILE_SCHEMA,
    VISUAL_REVIEW_WORKBENCH_PROFILE_PROTOCOL,
    VISUAL_REVIEW_WORKBENCH_PROFILE_MANIFEST_SCHEMA,
    VISUAL_REVIEW_COMPARISON_RECEIPT_SCHEMA,
    VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION,
    VISUAL_REVIEW_ADJUDICATION_MANIFEST_SCHEMA,
    VISUAL_REVIEW_ADJUDICATION_PROTOCOL,
    VISUAL_REVIEW_ADJUDICATION_RECEIPT_SCHEMA,
    VISUAL_REVIEW_ADJUDICATION_SUBMISSION_SCHEMA,
)
from expert_skill_contract import sha256_file
from incremental_build_dag import sha256_json
from visual_benchmark import (
    VisualBenchmarkError,
    _attestation_hash,
    _fragment_hash,
    _gold_input_hashes,
    load_phase7d5a_workpack,
    validate_external_gold_fragments,
)
from visual_semantics import _render_page
from paddle_runtime_contract import crop_render_png


EXPECTED_ITEM_COUNT = 31
ALLOWED_KINDS = {"equation", "figure", "table", "chart"}
DIFFERENCE_OBSERVATION_PLACEHOLDERS = frozenset({"none", "n/a", "na", "null", "-", "无"})
COORDINATE_SPACE = "pdf-page-top-left-points-v1"
COORDINATE_MAPPING_SCHEMA = "tkc.visual-review-context-coordinate-mapping/v0.1"
REVIEW_CONVENTION_PROFILE_PATH = Path(__file__).resolve().parents[1] / "references" / "review-convention-profile-v0.2.json"
REVIEW_CONVENTION_PROFILE_VERSION = "0.2"
REVIEW_STATUSES = {"complete", "uncertain", "needs_adjudication", "unreadable", "candidate_incorrect"}
UNRESOLVED_STATUSES = {"uncertain", "needs_adjudication", "unreadable"}
TYPE_MISMATCH_VERDICTS = {"other", "not_this_type"}
PRESENCE_VALUES = {"present", "absent", "uncertain"}
CONFIDENCE_VALUES = {"low", "medium", "high"}
BBOX_VERDICTS = {"correct", "too_large", "too_small", "wrong", "adjusted", "incorrect", "not_applicable"}
CORRECTED_BBOX_VERDICTS = {"too_large", "too_small", "wrong", "adjusted", "incorrect"}
VIEW_ZOOM_MIN = 0.1
VIEW_ZOOM_MAX = 4.0
VIEW_ZOOM_STEP = 1.25
LATEX_DELIMITER_PATTERNS = (
    ("double-dollar", re.compile(r"\$\$")),
    ("display-bracket", re.compile(r"\\\[|\\\]")),
    ("inline-parenthesis", re.compile(r"\\\(|\\\)")),
    ("single-dollar", re.compile(r"(^|[^\\])\$(?!\$)")),
)
DECLARATION_KEYS = (
    "qualified_reviewer",
    "reviewed_specified_source_evidence",
    "did_not_view_predictions",
    "did_not_change_split",
    "reviewed_specified_render_crop",
    "not_evaluator",
    "independent_from_proposer",
)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
HASH_RE = set("0123456789abcdef")
SYSTEM_PATH_ALIAS_SYMLINKS = {Path("/var")}
FORBIDDEN_REVIEW_KEYS = {
    "prediction",
    "predictions",
    "expected_answer",
    "expected_answers",
    "model_output",
    "model_confidence",
    "inference_output",
    "ppstructure",
    "chart2table",
    "docling",
}
TABLE_PASTE_FORMATS = ("tsv", "csv", "markdown")
TABLE_MARKDOWN_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")


def _load_review_workpack(path: Path) -> dict[str, Any]:
    """Load either the frozen 31-item benchmark or an explicit review-only workpack."""

    return load_phase7d5a_workpack(path, allow_portable_review=True)
ADJUDICATION_SOURCE_VALUES = {"reviewer-1", "reviewer-2", "manual", "unresolved"}
ADJUDICATION_DECLARATION_KEYS = (
    "qualified_adjudicator",
    "reviewed_specified_context_crop",
    "reviewed_two_reviewer_results",
    "did_not_view_predictions",
    "did_not_view_split_or_scores",
    "independent_from_reviewers",
    "independent_from_proposer",
    "not_evaluator",
)
ADJUDICATION_FIELD_PATH_RE = re.compile(r"^(?:presence|type_verdict|bbox_verdict|bbox|corrected_bbox|bbox_source|bbox_coordinate_space|kind|details\.[A-Za-z0-9_]+)$")


class ReviewerWorkbenchError(RuntimeError):
    """Stable fail-closed workbench error."""


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HASH_RE


def _hash_without(value: Mapping[str, Any], key: str) -> str:
    return sha256_json({name: nested for name, nested in value.items() if name != key})


def _read_json(path: Path, label: str = "json") -> dict[str, Any]:
    _secure_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReviewerWorkbenchError(f"{label}_invalid") from error
    if not isinstance(value, dict):
        raise ReviewerWorkbenchError(f"{label}_object_required")
    return value


def _validate_review_convention_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the small, source-controlled R2 profile contract."""

    required = {
        "schema_version": VISUAL_REVIEW_CONVENTION_PROFILE_SCHEMA,
        "profile_id": "visual-review-convention-v0.2",
        "profile_version": REVIEW_CONVENTION_PROFILE_VERSION,
        "compiler_version": VISUAL_REVIEW_CONVENTION_COMPILER_VERSION,
        "protocol": VISUAL_REVIEW_CONVENTION_PROTOCOL,
    }
    if any(profile.get(key) != expected for key, expected in required.items()):
        raise ReviewerWorkbenchError("review_convention_profile_identity_invalid")
    if "profile_sha256" in profile:
        raise ReviewerWorkbenchError("review_convention_profile_must_not_self_hash")
    bbox_scope = profile.get("bbox_scope")
    if not isinstance(bbox_scope, Mapping) or set(bbox_scope) != set(ALLOWED_KINDS):
        raise ReviewerWorkbenchError("review_convention_profile_bbox_scope_invalid")
    caption_rules = profile.get("caption_number_split")
    if not isinstance(caption_rules, Mapping) or caption_rules.get("exact_prefix_only") is not True or caption_rules.get("no_guess_or_broad_prefix_deletion") is not True:
        raise ReviewerWorkbenchError("review_convention_profile_caption_rules_invalid")
    text_rules = profile.get("text_comparison")
    if not isinstance(text_rules, Mapping) or text_rules.get("raw_transcription_preserved") is not True or text_rules.get("canonical_comparison_only") is not True:
        raise ReviewerWorkbenchError("review_convention_profile_text_rules_invalid")
    allowed_rules = tuple(text_rules.get("allowed_rules", ()))
    expected_allowed = ("unicode-nfc", "line-ending-unify", "line-edge-trim", "caption-exact-number-split")
    if allowed_rules != expected_allowed:
        raise ReviewerWorkbenchError("review_convention_profile_allowed_rules_invalid")
    forbidden_rules = set(text_rules.get("forbidden_rules", ()))
    if not {"unicode-nfkc", "case-fold", "technical-identifier-fuzzy", "formula-or-unit-fuzzy", "numeric-lexical-coercion", "character-auto-correction", "punctuation-or-hyphen-fuzzy"} <= forbidden_rules:
        raise ReviewerWorkbenchError("review_convention_profile_forbidden_rules_invalid")
    bbox_rules = profile.get("bbox_equivalence")
    if not isinstance(bbox_rules, Mapping) or bbox_rules.get("iou_min") != 0.95 or bbox_rules.get("edge_delta_pdf_points_max") != 3.0 or bbox_rules.get("render_pixel_edge_max") != 2.0:
        raise ReviewerWorkbenchError("review_convention_profile_bbox_equivalence_invalid")
    if any(bbox_rules.get(key) is not True for key in ("same_scope_required", "same_coordinate_space_required", "same_object_required", "bbox_verdict_conflict_substantive", "raw_bboxes_preserved")):
        raise ReviewerWorkbenchError("review_convention_profile_bbox_policy_invalid")
    if any(bbox_rules.get(key) is not False for key in ("allow_average", "allow_union", "allow_intersection")):
        raise ReviewerWorkbenchError("review_convention_profile_bbox_merge_policy_invalid")
    return deepcopy(dict(profile))


def load_review_convention_profile(path: Path | None = None) -> dict[str, Any]:
    """Load and validate the R2 profile without resolving any external input."""

    profile_path = Path(path).expanduser() if path is not None else REVIEW_CONVENTION_PROFILE_PATH
    return _validate_review_convention_profile(_read_json(profile_path, "review_convention_profile"))


def review_convention_profile_identity(profile: Mapping[str, Any] | None = None, *, path: Path | None = None) -> dict[str, Any]:
    """Return the deterministic identity bound to profile-aware artifacts."""

    value = _validate_review_convention_profile(profile) if profile is not None else load_review_convention_profile(path)
    return {
        "schema_version": value["schema_version"],
        "profile_id": value["profile_id"],
        "profile_version": value["profile_version"],
        "compiler_version": value["compiler_version"],
        "protocol": value["protocol"],
        "profile_sha256": sha256_json(value),
    }


def _read_jsonl(path: Path, label: str = "jsonl") -> list[dict[str, Any]]:
    _secure_file(path, label)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ReviewerWorkbenchError(f"{label}_read_failed") from error
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ReviewerWorkbenchError(f"{label}_invalid:{number}") from error
        if not isinstance(value, dict):
            raise ReviewerWorkbenchError(f"{label}_object_required:{number}")
        rows.append(value)
    return rows


def _write_json(path: Path, value: Any) -> None:
    _assert_no_symlink_components(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists() and path.is_symlink():
        raise ReviewerWorkbenchError("refuse_symlink_overwrite")
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    _assert_no_symlink_components(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists() and path.is_symlink():
        raise ReviewerWorkbenchError("refuse_symlink_overwrite")
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    os.chmod(path, 0o600)


def _assert_no_symlink_components(path: Path, *, stop_at: Path | None = None) -> None:
    candidate = Path(path)
    if stop_at is None:
        # macOS exposes /var as a system symlink to /private/var.  Parent
        # symlinks outside the scoped work directory are allowed; callers that
        # validate a workbench-relative path pass the workbench root explicitly
        # and get the stricter component walk below.
        if candidate.is_symlink():
            raise ReviewerWorkbenchError("symlink_path_forbidden")
        return
    for part in (candidate, *candidate.parents):
        if stop_at is not None and part == Path(stop_at):
            break
        if part.is_symlink() and part not in SYSTEM_PATH_ALIAS_SYMLINKS:
            raise ReviewerWorkbenchError("symlink_path_forbidden")


def _secure_file(path: Path, label: str = "file") -> Path:
    _assert_no_symlink_components(path)
    if not path.is_file() or path.is_symlink():
        raise ReviewerWorkbenchError(f"{label}_missing_or_symlink")
    return path


def _secure_directory(path: Path, *, must_be_empty: bool = False) -> Path:
    _assert_no_symlink_components(path)
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ReviewerWorkbenchError("directory_invalid")
        if must_be_empty and any(path.iterdir()):
            raise ReviewerWorkbenchError("output_not_empty")
    else:
        path.mkdir(parents=True, exist_ok=False)
    os.chmod(path, 0o700)
    return path


def _relative_instance_file(root: Path, relative: Any, label: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ReviewerWorkbenchError(f"{label}_path_not_relative")
    relative_path = Path(relative)
    if ".." in relative_path.parts:
        raise ReviewerWorkbenchError(f"{label}_path_traversal")
    path = root / relative_path
    _assert_no_symlink_components(path, stop_at=root)
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or not path.is_file() or path.is_symlink():
        raise ReviewerWorkbenchError(f"{label}_path_invalid")
    return resolved


def _safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ReviewerWorkbenchError(f"{label}_invalid")
    return value


def _copy_asset(source: Path, destination: Path) -> None:
    _secure_file(source, "asset_source")
    _assert_no_symlink_components(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    if destination.exists():
        raise ReviewerWorkbenchError("asset_destination_already_exists")
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o600)
    if sha256_file(destination) != sha256_file(source):
        raise ReviewerWorkbenchError("asset_copy_hash_mismatch")


def _strip_terminal_line_break(value: str) -> str:
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith(("\r", "\n")):
        return value[:-1]
    return value


def _markdown_pipe_row(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|") and not value.endswith("\\|"):
        value = value[:-1]
    cells: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value) and value[index + 1] == "|":
            current.append("|")
            index += 2
            continue
        if character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        index += 1
    cells.append("".join(current).strip())
    return cells


def _is_markdown_separator(cells: Sequence[str]) -> bool:
    return bool(cells) and all(TABLE_MARKDOWN_SEPARATOR_RE.fullmatch(cell.strip()) for cell in cells)


def _looks_like_markdown_pipe_table(value: str) -> bool:
    lines = [line for line in value.splitlines() if line.strip()]
    return len(lines) >= 2 and any("|" in line for line in lines) and any(_is_markdown_separator(_markdown_pipe_row(line)) for line in lines[1:])


def _parse_delimited_table(value: str, delimiter: str) -> list[list[str]]:
    source = _strip_terminal_line_break(value.lstrip("\ufeff"))
    if not source:
        raise ReviewerWorkbenchError("table_paste_parse_failed:未识别到数据行")
    try:
        rows = list(csv.reader(io.StringIO(source, newline=""), delimiter=delimiter, quotechar='"', doublequote=True, strict=True))
    except csv.Error as error:
        raise ReviewerWorkbenchError(f"table_paste_parse_failed:引号或分隔符格式错误:{error}") from error
    if not rows or any(not row for row in rows):
        raise ReviewerWorkbenchError("table_paste_parse_failed:存在空行，无法确定表格尺寸")
    width = len(rows[0])
    if width < 1 or any(len(row) != width for row in rows):
        raise ReviewerWorkbenchError("table_paste_parse_failed:每行列数不一致")
    return rows


def parse_table_paste(value: str) -> tuple[str, list[list[str]]]:
    """Parse a reviewer clipboard table while preserving empty cells."""

    if not isinstance(value, str):
        raise ReviewerWorkbenchError("table_paste_parse_failed:粘贴内容必须是文字")
    source = value.lstrip("\ufeff")
    if _looks_like_markdown_pipe_table(source):
        rows: list[list[str]] = []
        for line in source.splitlines():
            if not line.strip():
                continue
            cells = _markdown_pipe_row(line)
            if _is_markdown_separator(cells):
                continue
            rows.append(cells)
        if not rows:
            raise ReviewerWorkbenchError("table_paste_parse_failed:Markdown 表格没有数据行")
        width = len(rows[0])
        if width < 1 or any(len(row) != width for row in rows):
            raise ReviewerWorkbenchError("table_paste_parse_failed:Markdown 每行列数不一致")
        return "markdown", rows
    if "\t" in source:
        return "tsv", _parse_delimited_table(source, "\t")
    if "," in source:
        return "csv", _parse_delimited_table(source, ",")
    return "tsv", _parse_delimited_table(source, "\t")


def table_cells_from_paste(value: str, expected_rows: int, expected_columns: int) -> tuple[str, list[dict[str, Any]]]:
    """Return the exact zero-based row-major cell contract after a dimension check."""

    if type(expected_rows) is not int or type(expected_columns) is not int or expected_rows < 1 or expected_columns < 1:
        raise ReviewerWorkbenchError("table_paste_expected_dimensions_invalid:请先提供正整数行数和列数")
    format_name, rows = parse_table_paste(value)
    actual_rows = len(rows)
    actual_columns = len(rows[0]) if rows else 0
    if (actual_rows, actual_columns) != (expected_rows, expected_columns):
        raise ReviewerWorkbenchError(f"table_paste_dimension_mismatch:实际 {actual_rows}×{actual_columns} / 期望 {expected_rows}×{expected_columns}")
    cells = [{"row": row, "column": column, "text": rows[row][column]} for row in range(expected_rows) for column in range(expected_columns)]
    return format_name, cells


def _canonical_identity(facts: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    root = Path(str(facts["root"])).resolve()
    manifest_path = _secure_file(root / "manifest.json", "phase7d5a_manifest")
    split_path = _secure_file(root / "split-commitment.json", "phase7d5a_split")
    review_plan_path = _secure_file(root / "independent-review-plan.json", "phase7d5a_review_plan")
    bindings: list[dict[str, Any]] = []
    for item_id in sorted(str(value) for value in facts["candidate_ids"]):
        item = facts["candidate_by_id"][item_id]
        task = facts["task_by_item"][item_id]
        source = item.get("source") if isinstance(item.get("source"), Mapping) else {}
        geometry = item.get("geometry") if isinstance(item.get("geometry"), Mapping) else {}
        render = item.get("render") if isinstance(item.get("render"), Mapping) else {}
        crop = item.get("crop") if isinstance(item.get("crop"), Mapping) else {}
        bbox_sha = task.get("bbox_sha256")
        values = [source.get("source_sha256"), render.get("sha256"), crop.get("sha256"), bbox_sha, item.get("item_sha256")]
        if not all(_is_hash(value) for value in values) or not isinstance(source.get("physical_page"), int):
            raise ReviewerWorkbenchError(f"canonical_identity_invalid:{item_id}")
        bindings.append({
            "item_id": item_id,
            "item_sha256": item["item_sha256"],
            "source_sha256": source["source_sha256"],
            "physical_page": source["physical_page"],
            "bbox_sha256": bbox_sha,
            "render_sha256": render["sha256"],
            "crop_sha256": crop["sha256"],
            "source_visual_object_id": source.get("source_visual_object_id"),
            "candidate_bbox_pdf": geometry.get("candidate_bbox_pdf"),
        })
    identity_core = {
        "phase7d5a_manifest_sha256": str(facts["manifest_sha256"]),
        "phase7d5a_manifest_file_sha256": sha256_file(manifest_path),
        "candidate_items_file_sha256": str(facts["candidate_items_sha256"]),
        "split_commitment_sha256": str(facts["split"]["commitment_sha256"]),
        "split_file_sha256": str(facts["split_sha256"]),
        "review_plan_sha256": str(facts["review_plan_sha256"]),
        "candidate_set_sha256": sha256_json([(row["item_id"], row["item_sha256"]) for row in bindings]),
        "item_count": len(bindings),
        "item_bindings": bindings,
    }
    return {"workpack_sha256": sha256_json(identity_core), **identity_core}, sha256_json(identity_core)


def _reviewer_instance_id(workpack_hash: str, reviewer_id: str, session_id: str) -> str:
    return "reviewer-instance-" + sha256_json({"workpack_sha256": workpack_hash, "reviewer_instance": reviewer_id, "review_session_id": session_id})[:32]


def _asset_name(prefix: str, book_id: str, physical_page: int, source_path: str) -> str:
    safe_book = re.sub(r"[^A-Za-z0-9._-]+", "-", str(book_id)).strip("-") or "source"
    return f"{prefix}-{safe_book}-page-{physical_page:03d}-{sha256_json(source_path)[:10]}.png"


def _finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _bbox_numbers(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if not all(_finite_number(nested) for nested in value):
        return None
    return [float(nested) for nested in value]


def _bbox_within_page(value: Any, page_width: Any, page_height: Any) -> bool:
    numbers = _bbox_numbers(value)
    if numbers is None or not _finite_number(page_width) or not _finite_number(page_height):
        return False
    width = float(page_width)
    height = float(page_height)
    return width > 0 and height > 0 and 0 <= numbers[0] < numbers[2] <= width and 0 <= numbers[1] < numbers[3] <= height


def _bbox_equal(left: Any, right: Any, *, tolerance: float = 1e-6) -> bool:
    left_numbers = _bbox_numbers(left)
    right_numbers = _bbox_numbers(right)
    return left_numbers is not None and right_numbers is not None and all(abs(a - b) <= tolerance for a, b in zip(left_numbers, right_numbers))


def _aspect_matches(width_a: float, height_a: float, width_b: float, height_b: float) -> bool:
    if min(width_a, height_a, width_b, height_b) <= 0:
        return False
    return abs((width_a / height_a) - (width_b / height_b)) <= 0.02


def _context_coordinate_mapping(item: Mapping[str, Any]) -> dict[str, Any]:
    """Build the only browser-approved Context-pixel -> canonical PDF mapping.

    The current 7D.5A assets are full-page pdftoppm renders.  We record that
    fact explicitly as provenance instead of letting the browser infer it from
    an image's CSS size.  Future cropped page contexts must provide an explicit
    ``context_pdf_bbox`` in the frozen candidate geometry; otherwise the UI is
    deliberately disabled for correction.
    """

    geometry = item.get("geometry") if isinstance(item.get("geometry"), Mapping) else {}
    render = item.get("render") if isinstance(item.get("render"), Mapping) else {}
    page_width = geometry.get("page_width_points")
    page_height = geometry.get("page_height_points")
    dimensions = render.get("dimensions_px") if isinstance(render.get("dimensions_px"), Mapping) else {}
    render_width = dimensions.get("width")
    render_height = dimensions.get("height")
    mapping: dict[str, Any] = {
        "schema_version": COORDINATE_MAPPING_SCHEMA,
        "coordinate_space": COORDINATE_SPACE,
        "coordinate_origin": "page-top-left",
        "status": "unverifiable",
        "page_width_points": page_width,
        "page_height_points": page_height,
        "canonical_page_bbox": [0.0, 0.0, page_width, page_height] if _finite_number(page_width) and _finite_number(page_height) else None,
        "context_pdf_bbox": None,
        "context_is_full_page": False,
        "render_dimensions_px": {"width": render_width, "height": render_height},
        "render_rotation": render.get("rotation"),
        "mapping_basis": "missing-or-invalid-canonical-context-mapping",
        "unsupported_reason": "canonical page/context geometry is unavailable",
    }
    if not (_finite_number(page_width) and _finite_number(page_height) and _finite_number(render_width) and _finite_number(render_height)):
        return mapping
    page_width_f = float(page_width)
    page_height_f = float(page_height)
    render_width_i = int(render_width)
    render_height_i = int(render_height)
    explicit_context = geometry.get("context_pdf_bbox")
    if explicit_context is None and isinstance(render.get("context_pdf_bbox"), (list, tuple)):
        explicit_context = render.get("context_pdf_bbox")
    if explicit_context is not None:
        context_bbox = _bbox_numbers(explicit_context)
        basis = "frozen-context-pdf-bbox"
        full_page = _bbox_equal(context_bbox, [0.0, 0.0, page_width_f, page_height_f])
    elif _aspect_matches(float(render_width_i), float(render_height_i), page_width_f, page_height_f) and str(render.get("path", "")).startswith("source-renders/"):
        context_bbox = [0.0, 0.0, page_width_f, page_height_f]
        basis = "frozen-canonical-full-page-render"
        full_page = True
    else:
        context_bbox = None
        basis = "missing-context-pdf-bbox-for-non-full-page-render"
        full_page = False
    mapping["mapping_basis"] = basis
    if context_bbox is None or not _bbox_within_page(context_bbox, page_width_f, page_height_f):
        mapping["unsupported_reason"] = "context PDF bbox is absent or outside canonical page bounds"
        return mapping
    context_width = context_bbox[2] - context_bbox[0]
    context_height = context_bbox[3] - context_bbox[1]
    if not _aspect_matches(float(render_width_i), float(render_height_i), context_width, context_height):
        mapping["unsupported_reason"] = "render pixel aspect does not match context PDF bbox"
        return mapping
    if int(render.get("rotation", 0) or 0) not in {0}:
        mapping["unsupported_reason"] = "render rotation needs an explicit canonical transform"
        return mapping
    mapping.update({
        "status": "verified",
        "context_pdf_bbox": context_bbox,
        "context_is_full_page": full_page,
        "unsupported_reason": None,
    })
    return mapping


def _context_pixel_to_pdf(mapping: Mapping[str, Any], pixel_x: float, pixel_y: float, natural_width: float, natural_height: float) -> list[float]:
    """Map natural render pixels to canonical top-left PDF points for tests/tools."""

    if mapping.get("status") != "verified":
        raise ReviewerWorkbenchError("coordinate_mapping_unverifiable")
    context_bbox = _bbox_numbers(mapping.get("context_pdf_bbox"))
    if context_bbox is None or not _finite_number(natural_width) or not _finite_number(natural_height) or float(natural_width) <= 0 or float(natural_height) <= 0:
        raise ReviewerWorkbenchError("coordinate_mapping_invalid")
    x_ratio = min(1.0, max(0.0, float(pixel_x) / float(natural_width)))
    y_ratio = min(1.0, max(0.0, float(pixel_y) / float(natural_height)))
    return [context_bbox[0] + x_ratio * (context_bbox[2] - context_bbox[0]), context_bbox[1] + y_ratio * (context_bbox[3] - context_bbox[1])]


def _reviewer_item(item: Mapping[str, Any], task: Mapping[str, Any], context_asset: str, crop_asset: str) -> dict[str, Any]:
    source = item["source"]
    geometry = item["geometry"]
    render = item["render"]
    crop = item["crop"]
    coordinate_mapping = _context_coordinate_mapping(item)
    return {
        "item_id": item["item_id"],
        "item_sha256": item["item_sha256"],
        "review_task_id": task["review_task_id"],
        "candidate_type": item["kind_candidate"],
        "book_id": item.get("book_id"),
        "physical_page": source["physical_page"],
        "candidate_bbox_pdf": geometry["candidate_bbox_pdf"],
        "bbox_sha256": task["bbox_sha256"],
        "source_sha256": source["source_sha256"],
        "source_visual_object_id": source["source_visual_object_id"],
        "page_geometry": source.get("page_geometry"),
        "context": {"asset": context_asset, "sha256": render["sha256"], "dpi": render["dpi"], "rotation": render["rotation"], "dimensions_px": render.get("dimensions_px"), "coordinate_mapping": coordinate_mapping},
        "crop": {"asset": crop_asset, "sha256": crop["sha256"], "dimensions_px": crop.get("dimensions_px")},
        "review_scope": list(task.get("review_scope", [])),
    }


def _reviewer_data(facts: Mapping[str, Any], identity: Mapping[str, Any], reviewer_id: str, session_id: str, instance_id: str, destination: Path, profile_identity: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(str(facts["root"])).resolve()
    context_assets: dict[str, str] = {}
    assets: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for index, item_id in enumerate(sorted(str(value) for value in facts["candidate_ids"]), 1):
        item = facts["candidate_by_id"][item_id]
        task = facts["task_by_item"][item_id]
        render = item["render"]
        crop = item["crop"]
        render_source = root / render["path"]
        crop_source = root / crop["path"]
        context_key = str(render["path"])
        if context_key not in context_assets:
            relative = f"assets/context/{_asset_name('context', str(item.get('book_id', 'source')), int(item['source']['physical_page']), context_key)}"
            context_assets[context_key] = relative
            destination_path = destination / relative
            _copy_asset(render_source, destination_path)
            assets.append({"path": relative, "kind": "context", "sha256": render["sha256"], "source_hash": render["sha256"]})
        crop_relative = f"assets/crops/crop-{index:02d}-{_safe_id(item_id, 'item_id')}.png"
        _copy_asset(crop_source, destination / crop_relative)
        assets.append({"path": crop_relative, "kind": "crop", "sha256": crop["sha256"], "source_hash": crop["sha256"], "item_id": item_id})
        items.append(_reviewer_item(item, task, context_assets[context_key], crop_relative))

    profile_bound = profile_identity is not None
    data = {
        "schema_version": VISUAL_REVIEW_WORKBENCH_PROFILE_MANIFEST_SCHEMA if profile_bound else VISUAL_REVIEW_WORKBENCH_MANIFEST_SCHEMA,
        "protocol": VISUAL_REVIEW_WORKBENCH_PROFILE_PROTOCOL if profile_bound else VISUAL_REVIEW_WORKBENCH_PROTOCOL,
        "compiler_version": VISUAL_REVIEW_CONVENTION_COMPILER_VERSION if profile_bound else VISUAL_REVIEW_WORKBENCH_COMPILER_VERSION,
        "workbench_instance_id": instance_id,
        "reviewer": {"reviewer_instance": reviewer_id, "review_session_id": session_id, "role": "external-independent"},
        "workpack_identity": dict(identity),
        "item_count": len(items),
        "items": items,
        "assets": assets,
        "ui_policy": {
            "network_enabled": False,
            "external_resources": False,
            "split_labels_visible": False,
            "prediction_or_model_fields_present": False,
            "autosave_namespace": f"tkc.visual-review-workbench:{identity['workpack_sha256']}:{reviewer_id}",
        },
    }
    if profile_bound:
        data["profile"] = dict(profile_identity)
    return data, {"context_assets": context_assets}


ADJUDICATION_SCALAR_FIELDS = (
    "presence",
    "type_verdict",
    "bbox_verdict",
    "bbox",
    "corrected_bbox",
    "bbox_source",
    "bbox_coordinate_space",
    "kind",
)


def _json_exact_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without treating 2 and 2.0 as interchangeable."""

    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return set(left) == set(right) and all(_json_exact_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_exact_equal(a, b) for a, b in zip(left, right))
    return left == right


def _json_transport_identity_equal(left: Any, right: Any) -> bool:
    """Compare browser-round-tripped identity JSON without weakening content.

    JavaScript has one JSON Number type, so ``JSON.stringify`` serializes a
    locked geometry value such as ``144.0`` as ``144``.  The transport identity
    is still bound by its source/item/hash fields; accepting that one numeric
    representation change here avoids rejecting a genuine browser export.
    Scorable reviewer values continue to use ``_json_exact_equal`` so ``2`` and
    ``2.0`` remain distinct technical content everywhere else.
    """

    left_number = isinstance(left, (int, float)) and not isinstance(left, bool)
    right_number = isinstance(right, (int, float)) and not isinstance(right, bool)
    if left_number or right_number:
        return left_number and right_number and left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return set(left) == set(right) and all(_json_transport_identity_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_transport_identity_equal(a, b) for a, b in zip(left, right))
    return left == right


def _adjudication_field_values(payload: Mapping[str, Any]) -> dict[str, Any]:
    values = {key: deepcopy(payload.get(key)) for key in ADJUDICATION_SCALAR_FIELDS}
    details = payload.get("details")
    if not isinstance(details, Mapping):
        raise ReviewerWorkbenchError("adjudication_payload_details_invalid")
    values.update({f"details.{key}": deepcopy(value) for key, value in details.items()})
    return values


def _profile_json_type(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _profile_canonical_text(value: str, field_path: str, kind: str, details: Mapping[str, Any]) -> tuple[str, list[str]]:
    rules: list[str] = []
    canonical = value
    nfc = unicodedata.normalize("NFC", canonical)
    if nfc != canonical:
        canonical = nfc
        rules.append("unicode-nfc")
    unified = canonical.replace("\r\n", "\n").replace("\r", "\n")
    if unified != canonical:
        canonical = unified
        rules.append("line-ending-unify")
    trimmed = "\n".join(line.strip() for line in canonical.split("\n"))
    if trimmed != canonical:
        canonical = trimmed
        rules.append("line-edge-trim")
    if field_path == "details.caption" and kind in {"table", "figure"}:
        number_field = "table_number" if kind == "table" else "figure_number"
        number = details.get(number_field)
        missing_markers = {"none", "n/a", "na", "null", "unreadable", "无"}
        if isinstance(number, str) and number.strip() and number.strip().casefold() not in missing_markers:
            number_canonical = unicodedata.normalize("NFC", number).replace("\r\n", "\n").replace("\r", "\n")
            number_canonical = "\n".join(line.strip() for line in number_canonical.split("\n"))
            if canonical.startswith(number_canonical):
                remainder = canonical[len(number_canonical):]
                separator = re.match(r"^[ \t]+", remainder)
                if separator is not None:
                    canonical = remainder[separator.end():]
                    rules.append("caption-exact-number-split")
    return canonical, sorted(set(rules))


def _profile_canonical_value(value: Any, field_path: str, kind: str, details: Mapping[str, Any]) -> tuple[Any, list[str]]:
    if isinstance(value, str):
        return _profile_canonical_text(value, field_path, kind, details)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        rules: list[str] = []
        for key, nested in value.items():
            nested_value, nested_rules = _profile_canonical_value(nested, f"{field_path}.{key}", kind, details)
            result[str(key)] = nested_value
            rules.extend(nested_rules)
        return result, sorted(set(rules))
    if isinstance(value, list):
        result: list[Any] = []
        rules: list[str] = []
        for index, nested in enumerate(value):
            nested_value, nested_rules = _profile_canonical_value(nested, f"{field_path}[{index}]", kind, details)
            result.append(nested_value)
            rules.extend(nested_rules)
        return result, sorted(set(rules))
    return deepcopy(value), []


def _bbox_render_tolerance(item: Mapping[str, Any]) -> tuple[float, float] | None:
    geometry = item.get("geometry") if isinstance(item.get("geometry"), Mapping) else item
    render = item.get("render") if isinstance(item.get("render"), Mapping) else {}
    page_width = geometry.get("page_width_points")
    page_height = geometry.get("page_height_points")
    dimensions = render.get("dimensions_px") if isinstance(render.get("dimensions_px"), Mapping) else {}
    render_width = dimensions.get("width")
    render_height = dimensions.get("height")
    if not all(_finite_number(value) for value in (page_width, page_height, render_width, render_height)):
        return None
    page_width_f = float(page_width)
    page_height_f = float(page_height)
    render_width_f = float(render_width)
    render_height_f = float(render_height)
    if min(page_width_f, page_height_f, render_width_f, render_height_f) <= 0:
        return None
    mapping = _context_coordinate_mapping(item) if isinstance(item.get("geometry"), Mapping) else None
    context_bbox = mapping.get("context_pdf_bbox") if isinstance(mapping, Mapping) and mapping.get("status") == "verified" else None
    if _bbox_numbers(context_bbox) is not None:
        context = _bbox_numbers(context_bbox)
        assert context is not None
        x_scale = (context[2] - context[0]) / render_width_f
        y_scale = (context[3] - context[1]) / render_height_f
    else:
        if not _aspect_matches(render_width_f, render_height_f, page_width_f, page_height_f):
            return None
        x_scale = page_width_f / render_width_f
        y_scale = page_height_f / render_height_f
    return max(3.0, 2.0 * x_scale), max(3.0, 2.0 * y_scale)


def bbox_iou(left: Any, right: Any) -> float | None:
    left_numbers = _bbox_numbers(left)
    right_numbers = _bbox_numbers(right)
    if left_numbers is None or right_numbers is None or left_numbers[2] <= left_numbers[0] or left_numbers[3] <= left_numbers[1] or right_numbers[2] <= right_numbers[0] or right_numbers[3] <= right_numbers[1]:
        return None
    x0, y0 = max(left_numbers[0], right_numbers[0]), max(left_numbers[1], right_numbers[1])
    x1, y1 = min(left_numbers[2], right_numbers[2]), min(left_numbers[3], right_numbers[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    left_area = (left_numbers[2] - left_numbers[0]) * (left_numbers[3] - left_numbers[1])
    right_area = (right_numbers[2] - right_numbers[0]) * (right_numbers[3] - right_numbers[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else None


def bbox_equivalence(left: Any, right: Any, item: Mapping[str, Any], *, left_scope: str | None = None, right_scope: str | None = None, left_coordinate_space: str | None = None, right_coordinate_space: str | None = None, left_object_id: str | None = None, right_object_id: str | None = None) -> dict[str, Any]:
    """Apply only the R2 geometric equivalence rule; never derive a new box."""

    kind = str(item.get("kind_candidate", item.get("kind", "")))
    scope_left = left_scope or kind
    scope_right = right_scope or kind
    coordinate_left = left_coordinate_space or COORDINATE_SPACE
    coordinate_right = right_coordinate_space or COORDINATE_SPACE
    object_left = left_object_id or str(item.get("item_id", ""))
    object_right = right_object_id or str(item.get("item_id", ""))
    geometry = {
        "same_scope": scope_left == scope_right and scope_left in ALLOWED_KINDS,
        "same_coordinate_space": coordinate_left == coordinate_right == COORDINATE_SPACE,
        "same_object": object_left == object_right and bool(object_left),
        "iou": bbox_iou(left, right),
        "edge_deltas_pdf_points": None,
        "edge_tolerance_pdf_points": None,
    }
    left_numbers = _bbox_numbers(left)
    right_numbers = _bbox_numbers(right)
    if left_numbers is not None and right_numbers is not None:
        geometry["edge_deltas_pdf_points"] = [abs(a - b) for a, b in zip(left_numbers, right_numbers)]
    tolerance = _bbox_render_tolerance(item)
    if tolerance is not None:
        geometry["edge_tolerance_pdf_points"] = [tolerance[0], tolerance[1], tolerance[0], tolerance[1]]
    checks = [geometry["same_scope"], geometry["same_coordinate_space"], geometry["same_object"], geometry["iou"] is not None, tolerance is not None]
    if geometry["edge_deltas_pdf_points"] is not None and geometry["edge_tolerance_pdf_points"] is not None:
        checks.append(all(delta <= limit for delta, limit in zip(geometry["edge_deltas_pdf_points"], geometry["edge_tolerance_pdf_points"])))
    checks.append(geometry["iou"] is not None and geometry["iou"] >= 0.95)
    geometry["equivalent"] = all(checks)
    if not geometry["same_scope"]:
        geometry["reason"] = "bbox_scope_mismatch"
    elif not geometry["same_coordinate_space"]:
        geometry["reason"] = "bbox_coordinate_space_mismatch"
    elif not geometry["same_object"]:
        geometry["reason"] = "bbox_object_mismatch"
    elif geometry["iou"] is None:
        geometry["reason"] = "bbox_invalid"
    elif tolerance is None:
        geometry["reason"] = "bbox_render_mapping_unavailable"
    elif geometry["iou"] < 0.95:
        geometry["reason"] = "bbox_iou_below_threshold"
    elif geometry["edge_deltas_pdf_points"] is None or any(delta > limit for delta, limit in zip(geometry["edge_deltas_pdf_points"], geometry["edge_tolerance_pdf_points"])):
        geometry["reason"] = "bbox_edge_delta_above_threshold"
    else:
        geometry["reason"] = "geometric_equivalent"
    return geometry


def _profile_difference_reason(field_path: str, left: Any, right: Any) -> tuple[str, list[str]]:
    if isinstance(left, str) and isinstance(right, str):
        blocked: set[str] = {"unicode-nfkc", "case-fold"}
        if unicodedata.normalize("NFKC", left) == right or unicodedata.normalize("NFKC", right) == left:
            reason = "unicode_nfkc_difference_preserved"
        elif left.casefold() == right.casefold():
            reason = "case_difference_preserved"
        else:
            reason = "raw_text_difference_preserved"
        numeric_pattern = re.compile(r"^[ \t]*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[ \t]*$")
        if numeric_pattern.fullmatch(left) and numeric_pattern.fullmatch(right):
            return "numeric_lexical_difference_preserved", sorted(blocked | {"numeric-lexical-coercion"})
        if any(token in field_path.casefold() for token in ("cell", "header", "legend", "series", "formula", "axis", "identifier")):
            return "technical_identifier_or_formula_difference_preserved", sorted(blocked | {"technical-identifier-fuzzy", "formula-or-unit-fuzzy"})
        return reason, sorted(blocked | {"character-auto-correction", "punctuation-or-hyphen-fuzzy"})
    return "raw_value_difference_preserved", []


def _profile_field_comparison(item: Mapping[str, Any], kind: str, field_path: str, left: Any, right: Any, left_payload: Mapping[str, Any], right_payload: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_equal = _json_exact_equal(left, right)
    left_details = left_payload.get("details") if isinstance(left_payload.get("details"), Mapping) else {}
    right_details = right_payload.get("details") if isinstance(right_payload.get("details"), Mapping) else {}
    left_canonical, left_rules = _profile_canonical_value(left, field_path, kind, left_details)
    right_canonical, right_rules = _profile_canonical_value(right, field_path, kind, right_details)
    equivalence: dict[str, Any] | None = None
    canonical_source = "profile-safe-rules"
    verdict: str
    reason: str | None = None
    blocked_rules: list[str] = []
    if field_path in {"bbox", "corrected_bbox"}:
        if left is not None or right is not None:
            equivalence = bbox_equivalence(left, right, item)
        left_canonical, right_canonical = deepcopy(left), deepcopy(right)
        left_rules, right_rules = [], []
        if field_path == "bbox" and equivalence and equivalence.get("equivalent"):
            canonical_item = item.get("geometry") if isinstance(item.get("geometry"), Mapping) else {}
            candidate = canonical_item.get("candidate_bbox_pdf")
            left_candidate = bbox_equivalence(left, candidate, item)
            right_candidate = bbox_equivalence(right, candidate, item)
            if left_candidate.get("equivalent") and right_candidate.get("equivalent"):
                left_canonical = deepcopy(candidate)
                right_canonical = deepcopy(candidate)
                left_rules = ["bbox-frozen-candidate-source"]
                right_rules = ["bbox-frozen-candidate-source"]
                canonical_source = "frozen-candidate"
                verdict = "eliminated-by-profile" if not raw_equal else "same-after-profile"
            else:
                canonical_source = "raw-preserved"
                verdict = "remaining-substantive"
                reason = "geometric_equivalent_but_frozen_candidate_source_unavailable"
        elif raw_equal:
            verdict = "same-after-profile"
        else:
            verdict = "remaining-substantive"
            reason = equivalence.get("reason") if equivalence else "bbox_difference_preserved"
    elif field_path == "bbox_verdict" and not raw_equal:
        verdict = "remaining-substantive"
        reason = "bbox_verdict_conflict_substantive"
        canonical_source = "raw-preserved"
    else:
        canonical_equal = _json_exact_equal(left_canonical, right_canonical)
        if canonical_equal:
            verdict = "same-after-profile" if raw_equal else "eliminated-by-profile"
        elif raw_equal:
            verdict = "same-after-profile"
        else:
            verdict = "remaining-substantive"
            reason, blocked_rules = _profile_difference_reason(field_path, left, right)
    transformation_applied = bool(left_rules or right_rules) and (not _json_exact_equal(left, left_canonical) or not _json_exact_equal(right, right_canonical))
    if raw_equal and not transformation_applied and equivalence is None:
        return None
    if raw_equal and verdict == "same-after-profile" and not left_rules and not right_rules and equivalence is None:
        return None
    rule_ids = sorted(set(left_rules + right_rules + (["bbox-geometric-equivalence"] if equivalence and equivalence.get("equivalent") else [])))
    return {
        "field_path": field_path,
        "raw_equal": raw_equal,
        "raw": {
            "reviewer_1_sha256": sha256_json(left),
            "reviewer_2_sha256": sha256_json(right),
            "reviewer_1_type": _profile_json_type(left),
            "reviewer_2_type": _profile_json_type(right),
        },
        "canonical": {
            "reviewer_1_sha256": sha256_json(left_canonical),
            "reviewer_2_sha256": sha256_json(right_canonical),
            "reviewer_1_type": _profile_json_type(left_canonical),
            "reviewer_2_type": _profile_json_type(right_canonical),
        },
        "rule_ids": rule_ids,
        "rules_by_reviewer": {"reviewer_1": sorted(set(left_rules)), "reviewer_2": sorted(set(right_rules))},
        "blocked_rule_ids": sorted(set(blocked_rules)),
        "transformation_applied": transformation_applied,
        "verdict": verdict,
        "canonical_source": canonical_source,
        "substantive_reason": reason,
        "equivalence": equivalence,
    }


def _profile_item_comparison(item: Mapping[str, Any], dispute: Mapping[str, Any]) -> dict[str, Any]:
    payload_1 = dispute["reviewer_1"]["payload"]
    payload_2 = dispute["reviewer_2"]["payload"]
    values_1 = _adjudication_field_values(payload_1)
    values_2 = _adjudication_field_values(payload_2)
    field_comparisons = []
    for field_path in sorted(set(values_1) | set(values_2)):
        comparison = _profile_field_comparison(item, str(item["kind_candidate"]), field_path, values_1.get(field_path), values_2.get(field_path), payload_1, payload_2)
        if comparison is not None:
            field_comparisons.append(comparison)
    eliminated = [row for row in field_comparisons if row["verdict"] == "eliminated-by-profile"]
    remaining = [row for row in field_comparisons if row["verdict"] == "remaining-substantive"]
    return {
        "item_id": dispute["item_id"],
        "item_sha256": dispute["item_sha256"],
        "review_task_id": dispute["review_task_id"],
        "kind_candidate": dispute["kind_candidate"],
        "input_hashes": deepcopy(dispute["input_hashes"]),
        "raw_payload_sha256s": [dispute["reviewer_1"]["payload_sha256"], dispute["reviewer_2"]["payload_sha256"]],
        "raw_differing_fields": list(dispute["differing_fields"]),
        "field_comparisons": field_comparisons,
        "eliminated_fields": [{"field_path": row["field_path"], "rule_ids": row["rule_ids"]} for row in eliminated],
        "remaining_substantive_disagreements": [{"field_path": row["field_path"], "reason": row["substantive_reason"], "rule_ids": row["rule_ids"], "blocked_rule_ids": row["blocked_rule_ids"]} for row in remaining],
        "remaining_substantive": bool(remaining),
    }


def _workbench_profile_binding(reviewer_instance: Path) -> dict[str, Any]:
    root = Path(reviewer_instance).expanduser()
    manifest = _read_json(root / "workbench-manifest.json", "workbench_manifest")
    binding = {"workbench_instance_id": manifest.get("workbench_instance_id"), "manifest_sha256": manifest.get("manifest_sha256")}
    if manifest.get("schema_version") == VISUAL_REVIEW_WORKBENCH_PROFILE_MANIFEST_SCHEMA:
        profile = review_convention_profile_identity()
        if not _json_exact_equal(manifest.get("profile"), profile):
            raise ReviewerWorkbenchError("comparison_workbench_profile_drift")
        binding.update({"status": "profile-bound", "profile": profile})
    elif manifest.get("schema_version") == VISUAL_REVIEW_WORKBENCH_MANIFEST_SCHEMA:
        binding.update({"status": "unbound-legacy", "profile": None})
    else:
        raise ReviewerWorkbenchError("comparison_workbench_schema_invalid")
    return binding


def _build_profile_comparison_receipt(workpack: Path, reviewer_instance: Path, submissions: Sequence[Path], *, created_at: str) -> tuple[dict[str, Any], dict[str, Any]]:
    facts, identity, parsed, source = _source_adjudication_inputs(workpack, reviewer_instance, submissions, allow_empty=True)
    profile = review_convention_profile_identity()
    workbench_binding = _workbench_profile_binding(reviewer_instance)
    items = [_profile_item_comparison(facts["candidate_by_id"][dispute["item_id"]], dispute) for dispute in source["disputes"]]
    items.sort(key=lambda row: str(row["item_id"]))
    raw_field_count = sum(len(row["raw_differing_fields"]) for row in items)
    eliminated_fields = [field for row in items for field in row["eliminated_fields"]]
    remaining_fields = [field for row in items for field in row["remaining_substantive_disagreements"]]
    remaining_items = [row["item_id"] for row in items if row["remaining_substantive"]]
    source_submissions = _adjudication_source_submissions(parsed)
    raw_dispute_set_sha256 = _adjudication_dispute_set_hash(source["disputes"]) if source["disputes"] else sha256_json([])
    receipt = {
        "schema_version": VISUAL_REVIEW_COMPARISON_RECEIPT_SCHEMA,
        "protocol": VISUAL_REVIEW_CONVENTION_PROTOCOL,
        "compiler_version": VISUAL_REVIEW_CONVENTION_COMPILER_VERSION,
        "created_at": created_at,
        "status": "paused-profile-comparison",
        "profile": profile,
        "workbench": workbench_binding,
        "workpack_identity": deepcopy(identity),
        "source_submissions": source_submissions,
        "raw_dispute_set_sha256": raw_dispute_set_sha256,
        "raw_item_count": len(items),
        "items": items,
        "summary": {
            "raw_disagreement_item_count": len(items),
            "raw_disagreement_field_count": raw_field_count,
            "profile_eliminated_field_count": len(eliminated_fields),
            "profile_eliminated_item_count": sum(1 for row in items if not row["remaining_substantive"]),
            "remaining_substantive_item_count": len(remaining_items),
            "remaining_substantive_field_count": len(remaining_fields),
            "remaining_item_ids": remaining_items,
            "gold_rows": 0,
        },
        "gold_rows": 0,
        "release_included": False,
    }
    receipt["receipt_sha256"] = _hash_without(receipt, "receipt_sha256")
    return receipt, {"facts": facts, "identity": identity, "parsed": parsed, "source": source}


def compare_submissions_with_profile(workpack: Path, reviewer_instance: Path, submissions: Sequence[Path], output: Path | None = None, *, created_at: str = "2026-08-27T00:00:00+08:00") -> dict[str, Any]:
    receipt, _ = _build_profile_comparison_receipt(Path(workpack).expanduser().resolve(), Path(reviewer_instance).expanduser(), submissions, created_at=created_at)
    result = {"status": receipt["status"], "profile": receipt["profile"], "raw_disagreement_item_count": receipt["summary"]["raw_disagreement_item_count"], "raw_disagreement_field_count": receipt["summary"]["raw_disagreement_field_count"], "profile_eliminated_field_count": receipt["summary"]["profile_eliminated_field_count"], "profile_eliminated_item_count": receipt["summary"]["profile_eliminated_item_count"], "remaining_substantive_item_count": receipt["summary"]["remaining_substantive_item_count"], "remaining_substantive_field_count": receipt["summary"]["remaining_substantive_field_count"], "remaining_item_ids": receipt["summary"]["remaining_item_ids"], "gold_rows": 0, "release_included": False}
    if output is not None:
        destination = _secure_directory(Path(output).expanduser(), must_be_empty=True)
        receipt_path = destination / "review-convention-comparison-receipt.json"
        _write_json(receipt_path, receipt)
        result["receipt"] = str(receipt_path.resolve())
    return result


compare_review_submissions_with_profile = compare_submissions_with_profile


def validate_comparison_receipt(receipt_path: Path, workpack: Path, reviewer_instance: Path, submissions: Sequence[Path]) -> dict[str, Any]:
    receipt_path = _secure_file(Path(receipt_path).expanduser(), "comparison_receipt")
    receipt = _read_json(receipt_path, "comparison_receipt")
    if receipt.get("receipt_sha256") != _hash_without(receipt, "receipt_sha256"):
        raise ReviewerWorkbenchError("comparison_receipt_hash_mismatch")
    current_profile = review_convention_profile_identity()
    if not _json_exact_equal(receipt.get("profile"), current_profile):
        raise ReviewerWorkbenchError("comparison_profile_drift")
    expected, _ = _build_profile_comparison_receipt(Path(workpack).expanduser().resolve(), Path(reviewer_instance).expanduser(), submissions, created_at=str(receipt.get("created_at", "")))
    if not _json_exact_equal(receipt, expected):
        raise ReviewerWorkbenchError("comparison_receipt_binding_invalid")
    return {"status": "verified", "receipt": str(receipt_path.resolve()), "profile": current_profile, "raw_disagreement_item_count": receipt["summary"]["raw_disagreement_item_count"], "profile_eliminated_field_count": receipt["summary"]["profile_eliminated_field_count"], "remaining_substantive_item_count": receipt["summary"]["remaining_substantive_item_count"], "remaining_item_ids": receipt["summary"]["remaining_item_ids"], "gold_rows": 0, "release_included": False}


def _profile_filtered_adjudication_disputes(
    disputes: Sequence[Mapping[str, Any]],
    comparison_receipt: Path,
    workpack: Path,
    reviewer_instance: Path,
    submissions: Sequence[Path],
) -> list[dict[str, Any]]:
    """Bind a verified profile receipt and keep only its final review fields.

    The receipt never chooses a value.  Its hash, profile hash, reasons, and
    exact remaining field paths are committed into each dispute record so the
    downstream dispute-set hash remains sufficient to detect drift.
    """

    receipt_path = _secure_file(Path(comparison_receipt).expanduser(), "comparison_receipt")
    validate_comparison_receipt(receipt_path, workpack, reviewer_instance, submissions)
    receipt = _read_json(receipt_path, "comparison_receipt")
    receipt_items = {
        str(row["item_id"]): row
        for row in receipt.get("items", [])
        if isinstance(row, Mapping) and isinstance(row.get("item_id"), str)
    }
    raw_by_id = {str(row["item_id"]): row for row in disputes}
    if set(receipt_items) != set(raw_by_id):
        raise ReviewerWorkbenchError("portable_gold_comparison_item_set_mismatch")
    result: list[dict[str, Any]] = []
    for item_id in sorted(raw_by_id):
        raw = deepcopy(dict(raw_by_id[item_id]))
        compared = receipt_items[item_id]
        remaining = compared.get("remaining_substantive_disagreements")
        if not isinstance(remaining, list):
            raise ReviewerWorkbenchError(f"portable_gold_comparison_fields_invalid:{item_id}")
        field_paths = [str(row.get("field_path")) for row in remaining if isinstance(row, Mapping)]
        if len(field_paths) != len(remaining) or len(set(field_paths)) != len(field_paths):
            raise ReviewerWorkbenchError(f"portable_gold_comparison_fields_invalid:{item_id}")
        if not set(field_paths).issubset(set(str(value) for value in raw["differing_fields"])):
            raise ReviewerWorkbenchError(f"portable_gold_comparison_unknown_field:{item_id}")
        expected_payload_hashes = [raw["reviewer_1"]["payload_sha256"], raw["reviewer_2"]["payload_sha256"]]
        if compared.get("raw_payload_sha256s") != expected_payload_hashes:
            raise ReviewerWorkbenchError(f"portable_gold_comparison_payload_hash_mismatch:{item_id}")
        raw["differing_fields"] = sorted(field_paths)
        raw["comparison_receipt_sha256"] = receipt["receipt_sha256"]
        raw["comparison_profile_sha256"] = receipt["profile"]["profile_sha256"]
        raw["comparison_reasons"] = {
            str(row["field_path"]): {
                "reason": row.get("reason"),
                "rule_ids": deepcopy(row.get("rule_ids", [])),
                "blocked_rule_ids": deepcopy(row.get("blocked_rule_ids", [])),
            }
            for row in remaining
        }
        result.append(raw)
    return result


def _adjudication_record_core(record: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("item_id", "item_sha256", "review_task_id", "kind_candidate", "input_hashes", "differing_fields", "reviewer_1", "reviewer_2")
    if any(key not in record for key in keys):
        raise ReviewerWorkbenchError("adjudication_dispute_record_incomplete")
    result = {key: deepcopy(record[key]) for key in keys}
    optional = ("comparison_receipt_sha256", "comparison_profile_sha256", "comparison_reasons")
    for key in optional:
        if key in record:
            result[key] = deepcopy(record[key])
    return result


def _adjudication_dispute_set_hash(disputes: Sequence[Mapping[str, Any]]) -> str:
    return sha256_json([_adjudication_record_core(record) for record in disputes])


def _role_instances(facts: Mapping[str, Any], role: str) -> set[str]:
    values: set[str] = set()
    plan = facts.get("review_plan") if isinstance(facts.get("review_plan"), Mapping) else {}
    for key, value in plan.items():
        if role not in str(key).casefold():
            continue
        if isinstance(value, str):
            values.add(value)
        elif isinstance(value, (list, tuple, set)):
            values.update(str(nested) for nested in value if isinstance(nested, str))
    return values


def _validate_adjudicator_identity(adjudicator_id: str, session_id: str, facts: Mapping[str, Any], reviewer_ids: set[str]) -> tuple[str, str]:
    adjudicator_id = _safe_id(adjudicator_id, "adjudicator_instance")
    session_id = _safe_id(session_id, "adjudication_session_id")
    if adjudicator_id in reviewer_ids:
        raise ReviewerWorkbenchError("adjudicator_reviewer_overlap")
    if adjudicator_id in set(str(value) for value in facts.get("review_plan", {}).get("proposer_instances", []) if isinstance(value, str)):
        raise ReviewerWorkbenchError("adjudicator_proposer_overlap")
    if adjudicator_id in _role_instances(facts, "evaluator"):
        raise ReviewerWorkbenchError("adjudicator_evaluator_overlap")
    return adjudicator_id, session_id


def _source_adjudication_inputs(
    workpack: Path,
    reviewer_instance: Path,
    submissions: Sequence[Path],
    *,
    allow_empty: bool = False,
    comparison_receipt: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    workpack = Path(workpack).expanduser().resolve()
    reviewer_instance = Path(reviewer_instance).expanduser()
    if len(submissions) != 2:
        raise ReviewerWorkbenchError("adjudication_requires_exactly_two_reviewer_submissions")
    validate_instance(reviewer_instance, workpack)
    facts = _load_review_workpack(workpack)
    identity, _ = _canonical_identity(facts)
    parsed = [_load_submission(Path(path), identity, facts) for path in submissions]
    reviewer_ids = [str(row["reviewer_instance"]) for row in parsed]
    if len(set(reviewer_ids)) != 2:
        raise ReviewerWorkbenchError("adjudication_reviewer_identity_duplicate")
    if set(reviewer_ids) & set(str(value) for value in facts.get("review_plan", {}).get("proposer_instances", []) if isinstance(value, str)):
        raise ReviewerWorkbenchError("adjudication_reviewer_not_independent")
    if any(row["content_issues"] for row in parsed):
        raise ReviewerWorkbenchError("adjudication_source_submission_incomplete")
    data = _read_json(reviewer_instance / "data" / "reviewer-workpack.json", "reviewer_data")
    data_items = {str(row["item_id"]): row for row in data.get("items", []) if isinstance(row, Mapping) and isinstance(row.get("item_id"), str)}
    disputes: list[dict[str, Any]] = []
    for item_id in sorted(str(value) for value in facts["candidate_ids"]):
        item = facts["candidate_by_id"][item_id]
        task = facts["task_by_item"][item_id]
        # The canonical reviewer finalize contract requires two independent
        # reviewers only for table/chart items.  A second optional review of an
        # equation/figure is not a blocking disagreement and must not enter a
        # Phase 7D.5B adjudication set.
        if int(task.get("required_independent_reviewers", 0)) != 2:
            continue
        source_rows = [row["items"].get(item_id) for row in sorted(parsed, key=lambda value: str(value["reviewer_instance"]))]
        if any(not isinstance(row, Mapping) for row in source_rows):
            raise ReviewerWorkbenchError(f"adjudication_source_item_missing:{item_id}")
        reviews = [row["review"] for row in source_rows]
        payloads = [_scorable_payload(str(item["kind_candidate"]), review, item) for review in reviews]
        payload_hashes = [sha256_json(payload) for payload in payloads]
        if _json_exact_equal(payloads[0], payloads[1]):
            continue
        fields_1 = _adjudication_field_values(payloads[0])
        fields_2 = _adjudication_field_values(payloads[1])
        differing_fields = sorted(key for key in set(fields_1) | set(fields_2) if not _json_exact_equal(fields_1.get(key), fields_2.get(key)))
        if not differing_fields:
            raise ReviewerWorkbenchError(f"adjudication_payload_hash_or_field_drift:{item_id}")
        if item_id not in data_items:
            raise ReviewerWorkbenchError(f"adjudication_context_item_missing:{item_id}")
        source_item = data_items[item_id]
        disputes.append({
            "item_id": item_id,
            "item_sha256": item["item_sha256"],
            "review_task_id": task["review_task_id"],
            "kind_candidate": item["kind_candidate"],
            "input_hashes": _gold_input_hashes(item, task),
            "differing_fields": differing_fields,
            "reviewer_1": {"reviewer_instance": parsed[0]["reviewer_instance"], "review_session_id": parsed[0]["review_session_id"], "payload_sha256": payload_hashes[0], "payload": payloads[0]},
            "reviewer_2": {"reviewer_instance": parsed[1]["reviewer_instance"], "review_session_id": parsed[1]["review_session_id"], "payload_sha256": payload_hashes[1], "payload": payloads[1]},
            "context": deepcopy(source_item.get("context")),
            "crop": deepcopy(source_item.get("crop")),
        })
    disputes.sort(key=lambda row: str(row["item_id"]))
    if comparison_receipt is not None:
        disputes = _profile_filtered_adjudication_disputes(
            disputes,
            Path(comparison_receipt),
            workpack,
            reviewer_instance,
            submissions,
        )
    if not disputes and not allow_empty:
        raise ReviewerWorkbenchError("adjudication_dispute_set_empty")
    comparison_hash = disputes[0].get("comparison_receipt_sha256") if disputes else None
    return facts, identity, parsed, {"disputes": disputes, "data_items": data_items, "reviewer_ids": reviewer_ids, "comparison_receipt_sha256": comparison_hash}


def _adjudication_workbench_id(identity: Mapping[str, Any], source_submissions: Sequence[Mapping[str, Any]], dispute_set_sha256: str, adjudicator_id: str, session_id: str) -> str:
    return "adjudication-workbench-" + sha256_json({
        "workpack_sha256": identity["workpack_sha256"],
        "source_submission_sha256s": sorted(str(row["submission_sha256"]) for row in source_submissions),
        "dispute_set_sha256": dispute_set_sha256,
        "adjudicator_instance": adjudicator_id,
        "adjudication_session_id": session_id,
    })[:32]


def _adjudication_source_submissions(parsed: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "reviewer_instance": row["reviewer_instance"],
        "review_session_id": row["review_session_id"],
        "workbench_instance_id": row["workbench_instance_id"],
        "submission_sha256": row["file_sha256"],
    } for row in sorted(parsed, key=lambda value: str(value["reviewer_instance"]))]


def _adjudication_dispute_items(disputes: Sequence[Mapping[str, Any]], source_root: Path, destination: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    context_assets: dict[str, str] = {}
    assets: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for index, dispute in enumerate(disputes, 1):
        item = deepcopy(dict(dispute))
        context = item.get("context") if isinstance(item.get("context"), Mapping) else {}
        crop = item.get("crop") if isinstance(item.get("crop"), Mapping) else {}
        source_context = _relative_instance_file(source_root, context.get("asset"), "adjudication_context_asset")
        source_crop = _relative_instance_file(source_root, crop.get("asset"), "adjudication_crop_asset")
        context_key = str(context["asset"])
        if context_key not in context_assets:
            relative = f"assets/context/{_asset_name('context', item['item_id'], index, context_key)}"
            context_assets[context_key] = relative
            _copy_asset(source_context, destination / relative)
            assets.append({"path": relative, "kind": "context", "sha256": context["sha256"], "source_hash": context["sha256"]})
        crop_relative = f"assets/crops/crop-{index:02d}-{_safe_id(str(item['item_id']), 'item_id')}.png"
        _copy_asset(source_crop, destination / crop_relative)
        assets.append({"path": crop_relative, "kind": "crop", "sha256": crop["sha256"], "source_hash": crop["sha256"], "item_id": item["item_id"]})
        item["context"] = deepcopy(dict(context))
        item["context"]["asset"] = context_assets[context_key]
        item["crop"] = deepcopy(dict(crop))
        item["crop"]["asset"] = crop_relative
        items.append(item)
    return items, assets


def _png_dimensions_bytes(payload: bytes) -> dict[str, int]:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise ReviewerWorkbenchError("portable_review_render_not_png")
    width, height = struct.unpack(">II", payload[16:24])
    if width < 1 or height < 1:
        raise ReviewerWorkbenchError("portable_review_render_dimensions_invalid")
    return {"width": int(width), "height": int(height)}


def _portable_review_scope(kind: str) -> list[str]:
    if kind == "table":
        return ["presence", "localization", "row_column_topology", "span_topology", "cell_geometry", "numeric_and_unit_candidates"]
    if kind == "chart":
        return ["presence", "localization", "axis_candidates", "tick_candidates", "legend_candidates", "series_candidates", "value_candidates"]
    if kind == "figure":
        return ["presence", "localization", "caption_association_candidate"]
    return ["presence", "localization", "latex_display_body"]


def _portable_empty_gold(item: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "tkc.phase-7d5a-empty-gold-fragment/v0.1",
        "protocol": PORTABLE_VISUAL_REVIEW_WORKPACK_PROTOCOL,
        "release_included": False,
        "item_id": item["item_id"],
        "item_sha256": item["item_sha256"],
        "review_task_id": task["review_task_id"],
        "reviewer_instance": None,
        "attestation_id": None,
        "verdict": None,
        "difference_observation": None,
        "evidence_checked": None,
        "gold_payload": {},
        "verified_gold": False,
        "independent_real_gold": False,
        "promotion_allowed": False,
        "status": "empty-awaiting-independent-review",
    }


def prepare_single_page_visual_review_workpack(
    source: Path,
    visual_bundle: Path,
    output: Path,
    *,
    pdftoppm: Path,
    book_id: str,
    created_at: str,
    dpi: int = 120,
) -> dict[str, Any]:
    """Freeze actual single-page visual candidates for the existing reviewer UI.

    This command performs native rendering and deterministic cropping only.  It
    never invokes OCR/models, authors Gold, changes pagination, or creates
    artificial candidates to reach a benchmark count.
    """

    source = _secure_file(Path(source).expanduser().resolve(), "portable_review_source")
    bundle_path = _secure_file(Path(visual_bundle).expanduser().resolve(), "portable_review_visual_bundle")
    destination = _secure_directory(Path(output).expanduser(), must_be_empty=True)
    book_id = _safe_id(book_id, "book_id")
    if dpi != 120:
        raise ReviewerWorkbenchError("portable_review_dpi_must_be_120")
    bundle = _read_json(bundle_path, "portable_review_visual_bundle")
    source_sha = sha256_file(source)
    if bundle.get("source_sha256") != source_sha or bundle.get("candidate_only") is not True:
        raise ReviewerWorkbenchError("portable_review_source_or_candidate_policy_mismatch")
    objects = bundle.get("objects")
    if not isinstance(objects, list):
        raise ReviewerWorkbenchError("portable_review_objects_missing")
    kind_map = {"figure": "figure", "table": "table", "plot": "chart", "equation_display": "equation"}
    selected: list[dict[str, Any]] = []
    for raw in objects:
        if not isinstance(raw, Mapping) or raw.get("parent") is not None:
            continue
        source_type = str(raw.get("type", ""))
        kind = kind_map.get(source_type)
        if kind is None or (source_type == "figure" and raw.get("subtype") == "panel"):
            continue
        bbox = _bbox_numbers(raw.get("bbox"))
        page = raw.get("physical_page")
        width = raw.get("page_width")
        height = raw.get("page_height")
        if type(page) is not int or bbox is None or not _bbox_within_page(bbox, width, height):
            raise ReviewerWorkbenchError(f"portable_review_candidate_geometry_invalid:{raw.get('visual_object_id')}")
        selected.append({"object": dict(raw), "kind": kind, "bbox": bbox, "page": page, "width": float(width), "height": float(height)})
    # A full-page raster may contain a real numbered figure even when no
    # bounded image XObject exists.  Preserve that explicit parser conflict as
    # a review candidate so a human can draw the correct box.  Free prose such
    # as "Figure e.g" is intentionally excluded by the numbered-label rule.
    existing_labels = {
        (int(row["page"]), str(row["kind"]), str(row["object"].get("label", "")).strip().casefold())
        for row in selected
    }
    captions = [raw for raw in objects if isinstance(raw, Mapping) and raw.get("type") == "caption"]
    numbered_label = re.compile(r"^(?:figure|fig\.|table)\s+\d+(?:\.\d+)*\b", re.IGNORECASE)
    for conflict in bundle.get("conflicts", []) if isinstance(bundle.get("conflicts"), list) else []:
        if not isinstance(conflict, Mapping):
            continue
        details = conflict.get("details") if isinstance(conflict.get("details"), Mapping) else {}
        candidate_name = str(details.get("candidate", ""))
        kind = {"figure": "figure", "table": "table"}.get(candidate_name)
        label = str(details.get("label", "")).strip()
        page = conflict.get("physical_page")
        if kind is None or type(page) is not int or not numbered_label.fullmatch(label):
            continue
        key = (page, kind, label.casefold())
        if key in existing_labels:
            continue
        caption = next((raw for raw in captions if raw.get("physical_page") == page and str(raw.get("label", "")).strip().casefold() == label.casefold()), None)
        if not isinstance(caption, Mapping):
            continue
        caption_bbox = _bbox_numbers(caption.get("bbox"))
        width = caption.get("page_width")
        height = caption.get("page_height")
        if caption_bbox is None or not _bbox_within_page(caption_bbox, width, height):
            continue
        width_f, height_f = float(width), float(height)
        page_geometry = caption.get("page_geometry") if isinstance(caption.get("page_geometry"), Mapping) else {}
        crop_box = _bbox_numbers(page_geometry.get("crop_box"))
        left = crop_box[0] if crop_box is not None else 0.0
        right = crop_box[2] if crop_box is not None else width_f
        if kind == "figure":
            bottom = caption_bbox[1]
            top = max(crop_box[1] if crop_box is not None else 0.0, bottom - min(260.0, height_f * 0.38))
            estimated_bbox = [left, top, right, bottom]
        else:
            top = caption_bbox[3]
            bottom = min(crop_box[3] if crop_box is not None else height_f, top + min(300.0, height_f * 0.42))
            estimated_bbox = [left, top, right, bottom]
        pseudo = dict(caption)
        pseudo.update({
            "visual_object_id": conflict.get("visual_conflict_id"),
            "object_sha256": conflict.get("conflict_sha256"),
            "type": candidate_name,
            "subtype": "caption-only-gap",
            "label": label,
            "attributes": {"geometry_provenance": {"candidate_only": True, "geometry_evidence": "numbered-caption-gap-estimate-review-required", "visual_conflict_id": conflict.get("visual_conflict_id")}},
        })
        selected.append({"object": pseudo, "kind": kind, "bbox": estimated_bbox, "page": page, "width": width_f, "height": height_f})
        existing_labels.add(key)
    selected.sort(key=lambda row: (row["page"], row["kind"], row["bbox"], str(row["object"].get("visual_object_id"))))
    if len(selected) < 2:
        raise ReviewerWorkbenchError("portable_review_candidate_count_too_small")

    per_page_kind: dict[tuple[int, str], int] = defaultdict(int)
    rendered: dict[int, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    for index, row in enumerate(selected):
        page = int(row["page"])
        if page not in rendered:
            renderer_version, render_bytes, render_sha = _render_page(source, page, pdftoppm, dpi)
            render_relative = f"source-renders/{book_id}/page-{page:03d}.png"
            render_path = destination / render_relative
            render_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(render_path.parent, 0o700)
            render_path.write_bytes(render_bytes)
            os.chmod(render_path, 0o600)
            rendered[page] = {"bytes": render_bytes, "sha256": render_sha, "dimensions": _png_dimensions_bytes(render_bytes), "path": render_relative, "version": renderer_version}
        render = rendered[page]
        kind = str(row["kind"])
        per_page_kind[(page, kind)] += 1
        ordinal = per_page_kind[(page, kind)]
        item_id = _safe_id(f"{book_id}-{kind}-{page}-{ordinal}", "item_id")
        padding = 12.0 if kind == "equation" else 18.0
        bbox = list(row["bbox"])
        crop_bbox = [max(0.0, bbox[0] - padding), max(0.0, bbox[1] - padding), min(row["width"], bbox[2] + padding), min(row["height"], bbox[3] + padding)]
        dimensions = render["dimensions"]
        left = max(0, min(int(round(crop_bbox[0] / row["width"] * dimensions["width"])), dimensions["width"] - 1))
        top = max(0, min(int(round(crop_bbox[1] / row["height"] * dimensions["height"])), dimensions["height"] - 1))
        right = max(left + 1, min(int(round(crop_bbox[2] / row["width"] * dimensions["width"])), dimensions["width"]))
        bottom = max(top + 1, min(int(round(crop_bbox[3] / row["height"] * dimensions["height"])), dimensions["height"]))
        crop_result = crop_render_png(render["bytes"], {"render_dimensions_px": dimensions, "render_pixel_bbox": [left, top, right, bottom]}, expected_crop_sha256=None)
        crop_relative = f"crops/{book_id}/{item_id}.png"
        crop_path = destination / crop_relative
        crop_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(crop_path.parent, 0o700)
        crop_path.write_bytes(crop_result["bytes"])
        os.chmod(crop_path, 0o600)
        obj = row["object"]
        page_geometry = obj.get("page_geometry") if isinstance(obj.get("page_geometry"), Mapping) else {"physical_page": page, "coordinate_origin": "mediabox-top-left"}
        candidate = {
            "schema_version": "tkc.portable-visual-review-candidate/v0.1",
            "protocol": PORTABLE_VISUAL_REVIEW_WORKPACK_PROTOCOL,
            "release_included": False,
            "item_id": item_id,
            "book_id": book_id,
            "split": "calibration" if index % 2 == 0 else "test",
            "kind_candidate": kind,
            "candidate_status": "candidate-benchmark",
            "review_required": True,
            "verified_gold": False,
            "independent_real_gold": False,
            "promotion_allowed": False,
            "source": {
                "source_pdf_relative": source.name,
                "source_sha256": source_sha,
                "physical_page": page,
                "page_geometry": dict(page_geometry),
                "source_visual_object_id": obj.get("visual_object_id"),
                "source_object_type": obj.get("type"),
                "source_object_label_candidate": obj.get("label"),
                "source_object_sha256": obj.get("object_sha256"),
                "source_visual_bundle_sha256": sha256_file(bundle_path),
            },
            "render": {"path": render["path"], "sha256": render["sha256"], "dimensions_px": dimensions, "dpi": dpi, "rotation": 0, "renderer": "pdftoppm", "renderer_version": render["version"], "context_pdf_bbox": [0.0, 0.0, row["width"], row["height"]]},
            "geometry": {"coordinate_space": COORDINATE_SPACE, "candidate_bbox_pdf": bbox, "candidate_bbox_precision": "candidate-derived-review-required", "crop_bbox_pdf": crop_bbox, "crop_pixel_bbox": [left, top, right, bottom], "page_width_points": row["width"], "page_height_points": row["height"], "context_pdf_bbox": [0.0, 0.0, row["width"], row["height"]]},
            "crop": {"path": crop_relative, "sha256": crop_result["sha256"], "dimensions_px": crop_result["dimensions_px"], "source_type": "local-render-crop"},
            "strata": {"language_candidate": "source-language", "density_candidate": "review-required", "source_form_candidate": "native-visual-candidate", "table_topology_candidate": "table-topology-review-required" if kind == "table" else None, "selection_reason": "actual top-level visual candidate in bounded single-page source range"},
            "source_provenance": {"parser": obj.get("parser_receipt"), "backend": obj.get("backend_receipt"), "model": obj.get("model_receipt"), "config": obj.get("config_receipt"), "prior_candidate_crop_sha256": obj.get("crop_sha256")},
            "metrics_status": "not-run",
        }
        candidate["item_sha256"] = sha256_json(candidate)
        candidates.append(candidate)
        task = {
            "schema_version": "tkc.phase-7d5a-independent-review-task/v0.1",
            "protocol": PORTABLE_VISUAL_REVIEW_WORKPACK_PROTOCOL,
            "release_included": False,
            "item_id": item_id,
            "item_sha256": candidate["item_sha256"],
            "source_sha256": source_sha,
            "physical_page": page,
            "render_sha256": render["sha256"],
            "crop_sha256": crop_result["sha256"],
            "bbox_sha256": sha256_json({"coordinate_space": COORDINATE_SPACE, "bbox": crop_bbox}),
            "kind_candidate": kind,
            "review_scope": _portable_review_scope(kind),
            "required_independent_reviewers": 2 if kind in {"table", "chart"} else 1,
            "reviewer_instances_external_to_preparation_agent": True,
            "compiler_authored_attestation_allowed": False,
            "gold_generation_authority": "external-independent-reviewer-only",
            "status": "pending-independent-review",
        }
        task["review_task_id"] = "review-" + sha256_json(task)[:24]
        task["task_sha256"] = sha256_json(task)
        tasks.append(task)

    _write_jsonl(destination / "candidate-items.jsonl", candidates)
    _write_jsonl(destination / "independent-review-tasks.jsonl", tasks)
    _write_jsonl(destination / "gold-templates" / "gold-fragments.jsonl", [_portable_empty_gold(item, task) for item, task in zip(candidates, tasks)])
    calibration = sorted(item["item_id"] for item in candidates if item["split"] == "calibration")
    test = sorted(item["item_id"] for item in candidates if item["split"] == "test")
    split = {"schema_version": "tkc.phase-7d5a-split-commitment/v0.1", "protocol": PORTABLE_VISUAL_REVIEW_WORKPACK_PROTOCOL, "release_included": False, "item_count": len(candidates), "frozen_item_ids": sorted(item["item_id"] for item in candidates), "calibration_item_ids": calibration, "test_item_ids": test, "expected_answers_present": False, "verified_gold": False, "split_leakage_policy": {"same_crop_sha256_cannot_cross_split": True, "same_source_page_crop_cannot_cross_split": True, "same_source_visual_object_cannot_cross_split": True, "test_answers_created_after_freeze": True}}
    split["commitment_sha256"] = _hash_without(split, "commitment_sha256")
    _write_json(destination / "split-commitment.json", split)
    review_plan = {"schema_version": "tkc.phase-7d5a-independent-review-plan/v0.1", "protocol": PORTABLE_VISUAL_REVIEW_WORKPACK_PROTOCOL, "release_included": False, "review_plan_id": "portable-review-plan-" + sha256_json([task["review_task_id"] for task in tasks])[:24], "reviewer_registry": [], "proposer_instances": ["portable-single-page-preparation-agent"], "reviewer_independence_required": True, "attestation_authoring_allowed": False, "promotion_allowed": False, "required_reviewer_policy": {"table": 2, "chart": 2, "figure": 1, "equation": 1}, "task_count": len(tasks), "tasks_path": "independent-review-tasks.jsonl", "gold_fragments_path": "gold-templates/gold-fragments.jsonl", "state": "awaiting-external-independent-review"}
    review_plan["review_plan_sha256"] = _hash_without(review_plan, "review_plan_sha256")
    _write_json(destination / "independent-review-plan.json", review_plan)
    _write_json(destination / "metrics-contract.json", {"schema_version": "tkc.phase-7d5a-metrics-contract/v0.1", "protocol": PORTABLE_VISUAL_REVIEW_WORKPACK_PROTOCOL, "release_included": False, "metrics": [], "accuracy_claim": False, "verified_gold": False, "independent_real_gold": False, "note": "Review-only workpack; benchmark evaluation is not authorized by this artifact."})
    manifest = {"schema_version": PORTABLE_VISUAL_REVIEW_WORKPACK_MANIFEST_SCHEMA, "protocol": PORTABLE_VISUAL_REVIEW_WORKPACK_PROTOCOL, "compiler_version": PORTABLE_VISUAL_REVIEW_COMPILER_VERSION, "created_at": created_at, "source_sha256": source_sha, "visual_bundle_sha256": sha256_file(bundle_path), "candidate_item_count": len(candidates), "physical_pages": sorted(rendered), "pagination_policy": "single-physical-page-no-region-split", "review_scope": ["equation", "figure", "table", "chart"], "candidate_correctness_review_only": True, "whole_range_recall_claimed": False, "model_invocations": 0, "network_access": False, "mcp_calls": 0, "verified_gold": False, "promotion_allowed": False, "release_included": False}
    manifest["manifest_sha256"] = _hash_without(manifest, "manifest_sha256")
    _write_json(destination / "manifest.json", manifest)
    _load_review_workpack(destination.resolve())
    return {"status": "prepared-review-only", "workpack": str(destination.resolve()), "candidate_item_count": len(candidates), "kind_counts": {kind: sum(item["kind_candidate"] == kind for item in candidates) for kind in sorted({item["kind_candidate"] for item in candidates})}, "physical_pages_with_candidates": sorted(rendered), "pagination_policy": manifest["pagination_policy"], "model_invocations": 0, "network_access": False, "mcp_calls": 0, "verified_gold": False, "promotion_allowed": False, "release_included": False}


def generate_instance(workpack: Path, output: Path, *, reviewer_id: str, session_id: str | None = None, created_at: str = "2026-08-24T00:00:00+08:00") -> dict[str, Any]:
    reviewer_id = _safe_id(reviewer_id, "reviewer_id")
    facts = _load_review_workpack(Path(workpack).expanduser().resolve())
    identity, workpack_hash = _canonical_identity(facts)
    profile_identity = review_convention_profile_identity()
    session_id = _safe_id(session_id or f"session-{workpack_hash[:20]}", "review_session_id")
    instance_id = _reviewer_instance_id(workpack_hash, reviewer_id, session_id)
    destination = _secure_directory(Path(output).expanduser(), must_be_empty=True)
    data, _ = _reviewer_data(facts, identity, reviewer_id, session_id, instance_id, destination, profile_identity)
    data_path = destination / "data" / "reviewer-workpack.json"
    _write_json(data_path, data)
    html_text = render_review_html(data)
    (destination / "review.html").write_text(html_text, encoding="utf-8")
    os.chmod(destination / "review.html", 0o600)
    instructions = render_usage_instructions(destination.name, reviewer_id, profile_identity, item_count=len(data["items"]), portable_review=bool(facts.get("portable_review")))
    (destination / "人工Review使用说明.md").write_text(instructions, encoding="utf-8")
    os.chmod(destination / "人工Review使用说明.md", 0o600)
    manifest = {
        "schema_version": VISUAL_REVIEW_WORKBENCH_PROFILE_MANIFEST_SCHEMA,
        "protocol": VISUAL_REVIEW_WORKBENCH_PROFILE_PROTOCOL,
        "compiler_version": VISUAL_REVIEW_CONVENTION_COMPILER_VERSION,
        "created_at": created_at,
        "workbench_instance_id": instance_id,
        "reviewer": data["reviewer"],
        "workpack_identity": identity,
        "item_count": len(data["items"]),
        "items": data["items"],
        "assets": data["assets"],
        "ui_policy": data["ui_policy"],
        "profile": profile_identity,
        "data_file_sha256": sha256_file(data_path),
        "release_included": False,
    }
    manifest["manifest_sha256"] = _hash_without(manifest, "manifest_sha256")
    _write_json(destination / "workbench-manifest.json", manifest)
    validate_instance(destination, Path(workpack).expanduser().resolve())
    return {"status": "generated", "instance": str(destination.resolve()), "instructions": str((destination / "人工Review使用说明.md").resolve()), "workbench_instance_id": instance_id, "workpack_sha256": workpack_hash, "item_count": len(data["items"]), "reviewer_instance": reviewer_id, "profile": profile_identity, "release_included": False}


def generate_adjudication_workbench(
    workpack: Path,
    reviewer_instance: Path,
    submissions: Sequence[Path],
    output: Path,
    *,
    adjudicator_id: str,
    session_id: str,
    created_at: str = "2026-08-26T00:00:00+08:00",
    comparison_receipt: Path | None = None,
    portable_gold_review: bool = False,
) -> dict[str, Any]:
    facts, identity, parsed, source = _source_adjudication_inputs(
        workpack,
        reviewer_instance,
        submissions,
        comparison_receipt=comparison_receipt,
    )
    adjudicator_id, session_id = _validate_adjudicator_identity(adjudicator_id, session_id, facts, set(source["reviewer_ids"]))
    disputes = source["disputes"]
    source_submissions = _adjudication_source_submissions(parsed)
    dispute_set_sha256 = _adjudication_dispute_set_hash(disputes)
    workbench_id = _adjudication_workbench_id(identity, source_submissions, dispute_set_sha256, adjudicator_id, session_id)
    destination = _secure_directory(Path(output).expanduser(), must_be_empty=True)
    items, assets = _adjudication_dispute_items(disputes, Path(reviewer_instance).expanduser().resolve(), destination)
    data = {
        "schema_version": VISUAL_REVIEW_ADJUDICATION_MANIFEST_SCHEMA,
        "protocol": VISUAL_REVIEW_ADJUDICATION_PROTOCOL,
        "compiler_version": VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION,
        "workbench_instance_id": workbench_id,
        "adjudicator": {"adjudicator_instance": adjudicator_id, "adjudication_session_id": session_id, "role": "third-party-independent"},
        "workpack_identity": dict(identity),
        "source_submissions": source_submissions,
        "dispute_set_sha256": dispute_set_sha256,
        "item_count": len(items),
        "items": items,
        "assets": assets,
        "ui_policy": {
            "network_enabled": False,
            "external_resources": False,
            "split_labels_visible": False,
            "prediction_or_model_fields_present": False,
            "score_fields_present": False,
            "reviewer_results_visible": True,
            "adjudicator_answers_preselected": False,
            "autosave_namespace": f"tkc.visual-review-adjudication:{identity['workpack_sha256']}:{adjudicator_id}:{session_id}",
        },
    }
    data_path = destination / "data" / "adjudication-workpack.json"
    _write_json(data_path, data)
    manifest = dict(data)
    manifest.update({"created_at": created_at, "status": "ready", "data_file_sha256": sha256_file(data_path), "release_included": False, "immutable": True})
    manifest["manifest_sha256"] = _hash_without(manifest, "manifest_sha256")
    _write_json(destination / "adjudication-manifest.json", manifest)
    (destination / "review.html").write_text(render_adjudication_html(data, manifest["manifest_sha256"], portable_gold_review=portable_gold_review), encoding="utf-8")
    os.chmod(destination / "review.html", 0o600)
    if portable_gold_review:
        (destination / "Gold人工审查使用说明.md").write_text(render_portable_gold_review_usage_instructions(destination.name, adjudicator_id, len(facts["candidate_ids"])), encoding="utf-8")
        os.chmod(destination / "Gold人工审查使用说明.md", 0o600)
        instructions_path = destination / "Gold人工审查使用说明.md"
    else:
        (destination / "人工裁决使用说明.md").write_text(render_adjudication_usage_instructions(destination.name, adjudicator_id), encoding="utf-8")
        os.chmod(destination / "人工裁决使用说明.md", 0o600)
        instructions_path = destination / "人工裁决使用说明.md"
    result = validate_adjudication_workbench(
        destination,
        Path(workpack).expanduser().resolve(),
        reviewer_instance,
        submissions,
        comparison_receipt=comparison_receipt,
    )
    result.update({"status": "generated", "workbench": str(destination.resolve()), "adjudication_manifest": str((destination / "adjudication-manifest.json").resolve()), "instructions": str(instructions_path.resolve()), "adjudicator_instance": adjudicator_id, "adjudication_session_id": session_id})
    if portable_gold_review:
        result.update({"portable_gold_review": True, "gold_review_instructions": str((destination / "Gold人工审查使用说明.md").resolve()), "comparison_receipt_sha256": source.get("comparison_receipt_sha256")})
    return result


def plan_adjudication_disputes(workpack: Path, reviewer_instance: Path, submissions: Sequence[Path], output: Path | None = None, *, adjudicator_id: str | None = None, session_id: str | None = None, created_at: str = "2026-08-26T00:00:00+08:00", comparison_receipt: Path | None = None, portable_gold_review: bool = False) -> dict[str, Any]:
    if bool(adjudicator_id) != bool(session_id):
        raise ReviewerWorkbenchError("adjudicator_identity_and_session_must_be_provided_together")
    if adjudicator_id and session_id:
        if output is None:
            raise ReviewerWorkbenchError("adjudication_workbench_output_required")
        return generate_adjudication_workbench(workpack, reviewer_instance, submissions, output, adjudicator_id=adjudicator_id, session_id=session_id, created_at=created_at, comparison_receipt=comparison_receipt, portable_gold_review=portable_gold_review)
    facts, identity, parsed, source = _source_adjudication_inputs(workpack, reviewer_instance, submissions, comparison_receipt=comparison_receipt)
    disputes = source["disputes"]
    source_submissions = _adjudication_source_submissions(parsed)
    plan = {
        "schema_version": VISUAL_REVIEW_ADJUDICATION_MANIFEST_SCHEMA,
        "protocol": VISUAL_REVIEW_ADJUDICATION_PROTOCOL,
        "compiler_version": VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION,
        "status": "pending-adjudicator-identity",
        "created_at": created_at,
        "workpack_identity": dict(identity),
        "source_submissions": source_submissions,
        "dispute_set_sha256": _adjudication_dispute_set_hash(disputes),
        "item_count": len(disputes),
        "dispute_items": [{"item_id": row["item_id"], "item_sha256": row["item_sha256"], "review_task_id": row["review_task_id"], "input_hashes": row["input_hashes"], "differing_fields": row["differing_fields"], "reviewer_payload_sha256s": [row["reviewer_1"]["payload_sha256"], row["reviewer_2"]["payload_sha256"]]} for row in disputes],
        "adjudicator": {"adjudicator_instance": None, "adjudication_session_id": None, "status": "operator-identity-required"},
        "release_included": False,
        "source_payloads_included": False,
    }
    plan["plan_sha256"] = _hash_without(plan, "plan_sha256")
    result = {"status": plan["status"], "item_count": len(disputes), "dispute_item_ids": [row["item_id"] for row in disputes], "dispute_set_sha256": plan["dispute_set_sha256"], "workpack_sha256": identity["workpack_sha256"], "adjudicator_identity_required": True, "formal_workbench_generated": False, "release_included": False}
    if output is not None:
        destination = _secure_directory(Path(output).expanduser(), must_be_empty=True)
        _write_json(destination / "adjudication-dispute-plan.json", plan)
        result["plan"] = str((destination / "adjudication-dispute-plan.json").resolve())
    return result


def _forbidden_key_present(value: Any, *, ignore_declarations: bool = False) -> str | None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            lowered = str(key).casefold().replace("-", "_")
            if ignore_declarations and lowered == "declarations":
                continue
            if lowered == "prediction_or_model_fields_present":
                continue
            if lowered in FORBIDDEN_REVIEW_KEYS or any(token in lowered for token in ("pp_structure", "chart2table", "model_output", "expected_answer")):
                return str(key)
            found = _forbidden_key_present(nested, ignore_declarations=ignore_declarations)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _forbidden_key_present(nested, ignore_declarations=ignore_declarations)
            if found:
                return found
    return None


def validate_instance(instance: Path, workpack: Path) -> dict[str, Any]:
    instance = Path(instance).expanduser()
    facts = _load_review_workpack(Path(workpack).expanduser().resolve())
    identity, _ = _canonical_identity(facts)
    root = _secure_directory(instance)
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReviewerWorkbenchError("workbench_symlink_leak")
        if path.is_file() and path.suffix.casefold() == ".pdf":
            raise ReviewerWorkbenchError("workbench_source_pdf_leak")
    manifest = _read_json(root / "workbench-manifest.json", "workbench_manifest")
    if manifest.get("schema_version") == VISUAL_REVIEW_WORKBENCH_MANIFEST_SCHEMA and manifest.get("protocol") == VISUAL_REVIEW_WORKBENCH_PROTOCOL and manifest.get("compiler_version") == VISUAL_REVIEW_WORKBENCH_COMPILER_VERSION:
        profile_bound = False
        profile_identity = None
    elif manifest.get("schema_version") == VISUAL_REVIEW_WORKBENCH_PROFILE_MANIFEST_SCHEMA and manifest.get("protocol") == VISUAL_REVIEW_WORKBENCH_PROFILE_PROTOCOL and manifest.get("compiler_version") == VISUAL_REVIEW_CONVENTION_COMPILER_VERSION:
        profile_bound = True
        profile_identity = review_convention_profile_identity()
        if not _json_exact_equal(manifest.get("profile"), profile_identity):
            raise ReviewerWorkbenchError("workbench_profile_drift")
    else:
        raise ReviewerWorkbenchError("workbench_manifest_schema_invalid")
    if manifest.get("manifest_sha256") != _hash_without(manifest, "manifest_sha256"):
        raise ReviewerWorkbenchError("workbench_manifest_hash_mismatch")
    if manifest.get("release_included") is not False:
        raise ReviewerWorkbenchError("workbench_release_policy_invalid")
    if not _json_exact_equal(manifest.get("workpack_identity"), identity):
        raise ReviewerWorkbenchError("workbench_identity_mismatch")
    expected_count = int(identity["item_count"])
    if manifest.get("item_count") != expected_count or len(manifest.get("items", [])) != expected_count:
        raise ReviewerWorkbenchError("workbench_item_count_invalid")
    policy = manifest.get("ui_policy") if isinstance(manifest.get("ui_policy"), Mapping) else {}
    if any(policy.get(key) is not expected for key, expected in (("network_enabled", False), ("external_resources", False), ("split_labels_visible", False), ("prediction_or_model_fields_present", False))):
        raise ReviewerWorkbenchError("workbench_ui_policy_invalid")
    data_path = _secure_file(root / "data" / "reviewer-workpack.json", "reviewer_data")
    data = _read_json(data_path, "reviewer_data")
    data_keys = ("schema_version", "protocol", "compiler_version", "workbench_instance_id", "reviewer", "workpack_identity", "item_count", "items", "assets", "ui_policy", "profile") if profile_bound else ("schema_version", "protocol", "compiler_version", "workbench_instance_id", "reviewer", "workpack_identity", "item_count", "items", "assets", "ui_policy")
    expected_data = {key: manifest[key] for key in data_keys if key in manifest}
    if profile_bound and "profile" not in expected_data:
        raise ReviewerWorkbenchError("workbench_profile_binding_missing")
    if sha256_file(data_path) != manifest.get("data_file_sha256") or data != expected_data:
        raise ReviewerWorkbenchError("workbench_data_binding_invalid")
    if _forbidden_key_present(data) is not None:
        raise ReviewerWorkbenchError("workbench_prediction_or_model_field_leak")
    if any("split" in str(key).casefold() for key in data.get("workpack_identity", {})):
        # The commitment is intentionally named split_commitment_sha256 in the
        # locked identity, but individual calibration/test labels must never
        # enter the reviewer-facing data.  The hash itself is required.
        allowed = {"split_commitment_sha256", "split_file_sha256"}
        if any(str(key) not in allowed for key in data["workpack_identity"] if "split" in str(key).casefold()):
            raise ReviewerWorkbenchError("workbench_split_label_leak")
    item_ids: set[str] = set()
    asset_hashes: dict[str, str] = {}
    for asset in manifest.get("assets", []):
        if not isinstance(asset, Mapping) or not isinstance(asset.get("path"), str) or asset.get("path", "").lower().endswith(".pdf"):
            raise ReviewerWorkbenchError("workbench_asset_metadata_invalid")
        asset_path = _relative_instance_file(root, asset["path"], "workbench_asset")
        if sha256_file(asset_path) != asset.get("sha256"):
            raise ReviewerWorkbenchError("workbench_asset_hash_mismatch")
        asset_hashes[asset["path"]] = str(asset["sha256"])
    for item in data["items"]:
        if not isinstance(item, Mapping) or not isinstance(item.get("item_id"), str) or item["item_id"] in item_ids:
            raise ReviewerWorkbenchError("workbench_item_duplicate_or_missing")
        item_ids.add(item["item_id"])
        if _forbidden_key_present(item) is not None or "split" in item:
            raise ReviewerWorkbenchError("workbench_item_policy_invalid")
        canonical_item = facts["candidate_by_id"].get(item["item_id"])
        if not isinstance(canonical_item, Mapping):
            raise ReviewerWorkbenchError("workbench_item_canonical_missing")
        for field in ("item_sha256", "bbox_sha256", "source_sha256"):
            if not _is_hash(item.get(field)):
                raise ReviewerWorkbenchError("workbench_item_hash_invalid")
        for section in ("context", "crop"):
            asset = item.get(section) if isinstance(item.get(section), Mapping) else {}
            asset_path = asset.get("asset")
            if asset_path not in asset_hashes or asset_hashes[asset_path] != asset.get("sha256"):
                raise ReviewerWorkbenchError("workbench_item_asset_binding_invalid")
        context = item.get("context") if isinstance(item.get("context"), Mapping) else {}
        if context.get("coordinate_mapping") != _context_coordinate_mapping(canonical_item):
            raise ReviewerWorkbenchError("workbench_coordinate_mapping_binding_invalid")
    if item_ids != set(str(value) for value in facts["candidate_ids"]):
        raise ReviewerWorkbenchError("workbench_item_set_mismatch")
    html_path = _secure_file(root / "review.html", "review_html")
    html_text = html_path.read_text(encoding="utf-8")
    if "default-src 'none'" not in html_text or "connect-src 'none'" not in html_text or "object-src 'none'" not in html_text:
        raise ReviewerWorkbenchError("review_html_csp_missing")
    if any(token in html_text for token in ("http://", "https://", "<iframe", "<link", "fetch(", "importScripts(")):
        raise ReviewerWorkbenchError("review_html_external_resource_or_network")
    for asset in asset_hashes:
        if f'"{asset}"' not in html_text:
            raise ReviewerWorkbenchError("review_html_asset_not_embedded")
    if profile_bound:
        for marker in ("Review Convention Profile", profile_identity["profile_version"], profile_identity["profile_sha256"], "profile stale"):
            if marker not in html_text:
                raise ReviewerWorkbenchError("review_html_profile_binding_missing")
    result = {"status": "verified", "instance": str(root.resolve()), "item_count": len(item_ids), "workpack_sha256": identity["workpack_sha256"], "reviewer_instance": manifest["reviewer"]["reviewer_instance"], "network_enabled": False, "external_resources": False, "split_labels_visible": False, "prediction_or_model_fields_present": False, "release_included": False, "profile_bound": profile_bound}
    if profile_bound:
        result["profile"] = profile_identity
    return result


def validate_adjudication_workbench(workbench: Path, workpack: Path, reviewer_instance: Path, submissions: Sequence[Path], *, comparison_receipt: Path | None = None) -> dict[str, Any]:
    root = _secure_directory(Path(workbench).expanduser())
    facts, identity, parsed, source = _source_adjudication_inputs(workpack, reviewer_instance, submissions, comparison_receipt=comparison_receipt)
    manifest = _read_json(root / "adjudication-manifest.json", "adjudication_manifest")
    if manifest.get("schema_version") != VISUAL_REVIEW_ADJUDICATION_MANIFEST_SCHEMA or manifest.get("protocol") != VISUAL_REVIEW_ADJUDICATION_PROTOCOL or manifest.get("compiler_version") != VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION:
        raise ReviewerWorkbenchError("adjudication_manifest_schema_invalid")
    if manifest.get("manifest_sha256") != _hash_without(manifest, "manifest_sha256"):
        raise ReviewerWorkbenchError("adjudication_manifest_hash_mismatch")
    if manifest.get("status") != "ready" or manifest.get("release_included") is not False or manifest.get("immutable") is not True:
        raise ReviewerWorkbenchError("adjudication_manifest_policy_invalid")
    if manifest.get("workpack_identity") != identity:
        raise ReviewerWorkbenchError("adjudication_workpack_identity_mismatch")
    adjudicator = manifest.get("adjudicator") if isinstance(manifest.get("adjudicator"), Mapping) else {}
    adjudicator_id, session_id = _validate_adjudicator_identity(adjudicator.get("adjudicator_instance"), adjudicator.get("adjudication_session_id"), facts, set(source["reviewer_ids"]))
    if adjudicator.get("role") != "third-party-independent":
        raise ReviewerWorkbenchError("adjudicator_role_invalid")
    expected_source_submissions = _adjudication_source_submissions(parsed)
    if manifest.get("source_submissions") != expected_source_submissions:
        raise ReviewerWorkbenchError("adjudication_source_submission_binding_invalid")
    expected_disputes = source["disputes"]
    expected_dispute_hash = _adjudication_dispute_set_hash(expected_disputes)
    if manifest.get("dispute_set_sha256") != expected_dispute_hash:
        raise ReviewerWorkbenchError("adjudication_dispute_set_hash_mismatch")
    if manifest.get("item_count") != len(expected_disputes) or not isinstance(manifest.get("items"), list) or len(manifest["items"]) != len(expected_disputes):
        raise ReviewerWorkbenchError("adjudication_item_count_invalid")
    data_path = _secure_file(root / "data" / "adjudication-workpack.json", "adjudication_data")
    data = _read_json(data_path, "adjudication_data")
    data_keys = ("schema_version", "protocol", "compiler_version", "workbench_instance_id", "adjudicator", "workpack_identity", "source_submissions", "dispute_set_sha256", "item_count", "items", "assets", "ui_policy")
    if manifest.get("data_file_sha256") != sha256_file(data_path) or not _json_exact_equal(data, {key: manifest.get(key) for key in data_keys}):
        raise ReviewerWorkbenchError("adjudication_data_binding_invalid")
    if _forbidden_key_present(data) is not None:
        raise ReviewerWorkbenchError("adjudication_prediction_or_model_field_leak")
    policy = data.get("ui_policy") if isinstance(data.get("ui_policy"), Mapping) else {}
    expected_policy = {
        "network_enabled": False,
        "external_resources": False,
        "split_labels_visible": False,
        "prediction_or_model_fields_present": False,
        "score_fields_present": False,
        "reviewer_results_visible": True,
        "adjudicator_answers_preselected": False,
        "autosave_namespace": f"tkc.visual-review-adjudication:{identity['workpack_sha256']}:{adjudicator_id}:{session_id}",
    }
    if policy != expected_policy:
        raise ReviewerWorkbenchError("adjudication_ui_policy_invalid")
    expected_by_id = {str(row["item_id"]): row for row in expected_disputes}
    stored_by_id: dict[str, Mapping[str, Any]] = {}
    for stored in data["items"]:
        if not isinstance(stored, Mapping) or not isinstance(stored.get("item_id"), str) or stored["item_id"] in stored_by_id:
            raise ReviewerWorkbenchError("adjudication_item_duplicate_or_missing")
        item_id = str(stored["item_id"])
        if item_id not in expected_by_id:
            raise ReviewerWorkbenchError(f"adjudication_unknown_dispute:{item_id}")
        if set(stored) != set(_adjudication_record_core(stored)) | {"context", "crop"}:
            raise ReviewerWorkbenchError(f"adjudication_item_unknown_field:{item_id}")
        if not _json_exact_equal(_adjudication_record_core(stored), _adjudication_record_core(expected_by_id[item_id])):
            raise ReviewerWorkbenchError(f"adjudication_item_binding_invalid:{item_id}")
        for section in ("context", "crop"):
            actual = stored.get(section) if isinstance(stored.get(section), Mapping) else {}
            expected = expected_by_id[item_id].get(section) if isinstance(expected_by_id[item_id].get(section), Mapping) else {}
            if not isinstance(actual.get("asset"), str) or not isinstance(expected.get("asset"), str):
                raise ReviewerWorkbenchError(f"adjudication_{section}_binding_invalid:{item_id}")
            actual_without_asset = {key: value for key, value in actual.items() if key != "asset"}
            expected_without_asset = {key: value for key, value in expected.items() if key != "asset"}
            if not _json_exact_equal(actual_without_asset, expected_without_asset):
                raise ReviewerWorkbenchError(f"adjudication_{section}_metadata_invalid:{item_id}")
        stored_by_id[item_id] = stored
    if set(stored_by_id) != set(expected_by_id):
        raise ReviewerWorkbenchError("adjudication_dispute_set_mismatch")
    asset_hashes: dict[str, str] = {}
    asset_rows = data.get("assets")
    if not isinstance(asset_rows, list):
        raise ReviewerWorkbenchError("adjudication_assets_required")
    for asset in asset_rows:
        if not isinstance(asset, Mapping) or not isinstance(asset.get("path"), str) or asset["path"] in asset_hashes or asset["path"].casefold().endswith(".pdf"):
            raise ReviewerWorkbenchError("adjudication_asset_metadata_invalid")
        asset_path = _relative_instance_file(root, asset["path"], "adjudication_asset")
        if sha256_file(asset_path) != asset.get("sha256"):
            raise ReviewerWorkbenchError("adjudication_asset_hash_mismatch")
        asset_hashes[asset["path"]] = str(asset["sha256"])
    expected_asset_paths = {str(stored[section]["asset"]) for stored in stored_by_id.values() for section in ("context", "crop")}
    if set(asset_hashes) != expected_asset_paths:
        raise ReviewerWorkbenchError("adjudication_asset_set_mismatch")
    for item_id, stored in stored_by_id.items():
        for section in ("context", "crop"):
            section_value = stored[section]
            if asset_hashes.get(section_value["asset"]) != section_value.get("sha256"):
                raise ReviewerWorkbenchError(f"adjudication_item_asset_binding_invalid:{item_id}")
    html_path = _secure_file(root / "review.html", "adjudication_review_html")
    html_text = html_path.read_text(encoding="utf-8")
    if "default-src 'none'" not in html_text or "connect-src 'none'" not in html_text or "object-src 'none'" not in html_text:
        raise ReviewerWorkbenchError("adjudication_review_html_csp_missing")
    if any(token in html_text for token in ("http://", "https://", "<iframe", "<link", "fetch(", "XMLHttpRequest", "WebSocket", "navigator.sendBeacon")):
        raise ReviewerWorkbenchError("adjudication_review_html_external_resource_or_network")
    for asset in asset_hashes:
        if f'"{asset}"' not in html_text:
            raise ReviewerWorkbenchError("adjudication_review_html_asset_not_embedded")
    if any(path.suffix.casefold() == ".pdf" or path.is_symlink() for path in root.rglob("*")):
        raise ReviewerWorkbenchError("adjudication_source_or_symlink_leak")
    return {"status": "verified", "workbench": str(root.resolve()), "adjudication_manifest": str((root / "adjudication-manifest.json").resolve()), "item_count": len(expected_disputes), "dispute_item_ids": sorted(expected_by_id), "dispute_set_sha256": expected_dispute_hash, "workpack_sha256": identity["workpack_sha256"], "adjudicator_instance": adjudicator_id, "adjudication_session_id": session_id, "network_enabled": False, "release_included": False}


def _submission_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {key: identity[key] for key in ("workpack_sha256", "phase7d5a_manifest_sha256", "candidate_items_file_sha256", "split_commitment_sha256", "review_plan_sha256")}


def _load_submission(path: Path, identity: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    submission_path = _secure_file(Path(path).expanduser(), "review_submission")
    value = _read_json(submission_path, "review_submission")
    if value.get("schema_version") != VISUAL_REVIEW_WORKBENCH_SUBMISSION_SCHEMA or value.get("protocol") != VISUAL_REVIEW_WORKBENCH_PROTOCOL:
        raise ReviewerWorkbenchError("review_submission_schema_invalid")
    if value.get("workpack_identity") != _submission_identity(identity):
        raise ReviewerWorkbenchError("review_submission_identity_mismatch")
    reviewer_id = _safe_id(value.get("reviewer_instance"), "submission_reviewer_id")
    session_id = _safe_id(value.get("review_session_id"), "submission_review_session_id")
    expected_instance = _reviewer_instance_id(str(identity["workpack_sha256"]), reviewer_id, session_id)
    if value.get("workbench_instance_id") != expected_instance:
        raise ReviewerWorkbenchError("review_submission_instance_binding_invalid")
    if "split" in value or _forbidden_key_present(value, ignore_declarations=True) is not None:
        raise ReviewerWorkbenchError("review_submission_hidden_answer_or_split_field")
    declarations = value.get("declarations") if isinstance(value.get("declarations"), Mapping) else {}
    declaration_issues = [f"declaration_missing_or_false:{key}" for key in DECLARATION_KEYS if declarations.get(key) is not True]
    raw_items = value.get("items")
    if not isinstance(raw_items, list):
        raise ReviewerWorkbenchError("review_submission_items_required")
    item_by_id: dict[str, dict[str, Any]] = {}
    content_issues = list(declaration_issues)
    for raw in raw_items:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("item_id"), str):
            content_issues.append("submission_item_invalid")
            continue
        item_id = str(raw["item_id"])
        if item_id in item_by_id:
            content_issues.append(f"duplicate_submission_item:{item_id}")
            continue
        if item_id not in facts["candidate_by_id"]:
            raise ReviewerWorkbenchError(f"submission_unknown_item:{item_id}")
        canonical_item = facts["candidate_by_id"][item_id]
        task = facts["task_by_item"][item_id]
        expected_hashes = _gold_input_hashes(canonical_item, task)
        if raw.get("item_sha256") != canonical_item.get("item_sha256") or raw.get("review_task_id") != task.get("review_task_id") or raw.get("input_hashes") != expected_hashes:
            raise ReviewerWorkbenchError(f"submission_item_hash_or_task_tampered:{item_id}")
        if "split" in raw or _forbidden_key_present(raw) is not None:
            raise ReviewerWorkbenchError(f"submission_item_hidden_field:{item_id}")
        review = raw.get("review")
        if not isinstance(review, Mapping):
            content_issues.append(f"review_missing:{item_id}")
            continue
        item_by_id[item_id] = {"item_id": item_id, "item_sha256": canonical_item["item_sha256"], "review_task_id": task["review_task_id"], "input_hashes": expected_hashes, "review": dict(review)}
        content_issues.extend(_review_content_issues(item_id, str(canonical_item["kind_candidate"]), review, canonical_item))
    if set(item_by_id) != set(str(value) for value in facts["candidate_ids"]):
        missing = sorted(set(str(value) for value in facts["candidate_ids"]) - set(item_by_id))
        content_issues.extend(f"missing_submission_item:{item_id}" for item_id in missing)
    return {"path": submission_path, "file_sha256": sha256_file(submission_path), "reviewer_instance": reviewer_id, "review_session_id": session_id, "workbench_instance_id": value["workbench_instance_id"], "declarations": dict(declarations), "items": item_by_id, "content_issues": sorted(set(content_issues))}


def _nonempty(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip())) and value not in ({}, [])


def _difference_observation_placeholder(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = "".join(value.split()).casefold()
    return normalized if normalized in DIFFERENCE_OBSERVATION_PLACEHOLDERS else None


def _valid_bbox(value: Any) -> bool:
    numbers = _bbox_numbers(value)
    return numbers is not None and numbers[2] > numbers[0] and numbers[3] > numbers[1]


def _latex_delimiter_kind(value: Any) -> str | None:
    """Return a stable error category for display/inline delimiters.

    Gold stores the mathematical body only.  This deliberately does not
    rewrite the reviewer value: an existing draft containing delimiters must
    remain visible to the reviewer and be rejected until explicitly corrected.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    for kind, pattern in LATEX_DELIMITER_PATTERNS:
        if pattern.search(value):
            return kind
    return None


def _review_corrected_bbox(review: Mapping[str, Any]) -> tuple[Any, str | None]:
    """Read the new field, while accepting the previous observed_bbox draft key."""

    if "corrected_bbox" in review:
        value = review.get("corrected_bbox")
        return (value, "corrected_bbox") if value is not None else (None, None)
    legacy = review.get("observed_bbox")
    if _valid_bbox(legacy):
        return legacy, "observed_bbox"
    return None, None


def _canonical_page_dimensions(item: Mapping[str, Any]) -> tuple[float, float] | None:
    geometry = item.get("geometry") if isinstance(item.get("geometry"), Mapping) else {}
    width = geometry.get("page_width_points")
    height = geometry.get("page_height_points")
    if not _finite_number(width) or not _finite_number(height) or float(width) <= 0 or float(height) <= 0:
        return None
    return float(width), float(height)


def _corrected_bbox_valid_for_item(value: Any, item: Mapping[str, Any]) -> bool:
    dimensions = _canonical_page_dimensions(item)
    return dimensions is not None and _bbox_within_page(value, dimensions[0], dimensions[1])


def _review_content_issues(item_id: str, kind: str, review: Mapping[str, Any], canonical_item: Mapping[str, Any] | None = None) -> list[str]:
    issues: list[str] = []
    if kind == "equation":
        delimiter_kind = _latex_delimiter_kind(review.get("transcription_latex"))
        if delimiter_kind is not None:
            issues.append(f"equation_latex_delimiter_forbidden:{item_id}:{delimiter_kind}")
    status = review.get("review_status")
    if status not in REVIEW_STATUSES:
        return [f"review_status_missing_or_invalid:{item_id}"]
    if "notes" in review and not isinstance(review.get("notes"), str):
        issues.append(f"notes_invalid:{item_id}")
    difference_observation = review.get("difference_observation")
    if not isinstance(difference_observation, str) or not difference_observation.strip():
        issues.append(f"difference_observation_missing:{item_id}")
    elif _difference_observation_placeholder(difference_observation) is not None:
        issues.append(f"difference_observation_placeholder:{item_id}")
    type_verdict = review.get("type_verdict")
    presence = review.get("presence")
    bbox_verdict = review.get("bbox_verdict")
    if type_verdict in TYPE_MISMATCH_VERDICTS:
        if status != "candidate_incorrect":
            issues.append(f"type_mismatch_requires_candidate_incorrect:{item_id}")
        if presence != "absent":
            issues.append(f"type_mismatch_requires_absent:{item_id}")
        if bbox_verdict != "not_applicable":
            issues.append(f"type_mismatch_requires_not_applicable_bbox:{item_id}")
    if status == "complete":
        if presence != "present":
            issues.append(f"complete_requires_present:{item_id}")
        if type_verdict == "unknown":
            issues.append(f"complete_type_verdict_unknown:{item_id}")
        elif type_verdict in TYPE_MISMATCH_VERDICTS:
            issues.append(f"complete_type_verdict_mismatch:{item_id}")
    if status in UNRESOLVED_STATUSES:
        issues.append(f"unresolved_{status}:{item_id}")
        return issues
    if review.get("presence") not in PRESENCE_VALUES:
        issues.append(f"presence_missing:{item_id}")
    if review.get("reviewer_confidence") not in CONFIDENCE_VALUES:
        issues.append(f"reviewer_confidence_missing:{item_id}")
    if review.get("type_verdict") in (None, ""):
        issues.append(f"type_verdict_missing:{item_id}")
    if presence == "present":
        if bbox_verdict not in BBOX_VERDICTS - {"not_applicable"}:
            issues.append(f"bbox_verdict_missing:{item_id}")
        corrected_bbox, _ = _review_corrected_bbox(review)
        if bbox_verdict == "correct":
            candidate_bbox = canonical_item.get("geometry", {}).get("candidate_bbox_pdf") if isinstance(canonical_item, Mapping) else None
            if corrected_bbox is not None and (not _corrected_bbox_valid_for_item(corrected_bbox, canonical_item or {}) or not _bbox_equal(corrected_bbox, candidate_bbox)):
                issues.append(f"correct_bbox_cannot_override_candidate:{item_id}")
        elif bbox_verdict in CORRECTED_BBOX_VERDICTS:
            if not _corrected_bbox_valid_for_item(corrected_bbox, canonical_item or {}):
                issues.append(f"corrected_bbox_missing_or_invalid:{item_id}")
            elif _context_coordinate_mapping(canonical_item or {}).get("status") != "verified":
                issues.append(f"bbox_coordinate_mapping_unverifiable:{item_id}")
    elif presence == "absent":
        if bbox_verdict not in {"not_applicable", "incorrect", "wrong", "correct"}:
            issues.append(f"bbox_absence_verdict_missing:{item_id}")
    else:
        issues.append(f"presence_unresolved:{item_id}")
    corrected_bbox, _ = _review_corrected_bbox(review)
    if (presence == "absent" or bbox_verdict == "not_applicable") and corrected_bbox is not None:
        issues.append(f"corrected_bbox_not_applicable:{item_id}")
    if status == "candidate_incorrect" and presence != "absent":
        issues.append(f"candidate_incorrect_requires_absent:{item_id}")
    if status == "candidate_incorrect" and bbox_verdict != "not_applicable":
        issues.append(f"candidate_incorrect_requires_not_applicable_bbox:{item_id}")
    if status == "complete" and presence == "present":
        issues.extend(_type_specific_issues(item_id, kind, review))
    return issues


def _type_specific_issues(item_id: str, kind: str, review: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if kind == "equation":
        for key in ("equation_type", "formula_number", "crosses_lines", "transcription_completeness"):
            if not _nonempty(review.get(key)):
                issues.append(f"equation_field_missing:{item_id}:{key}")
        if review.get("transcription_completeness") == "complete" and not _nonempty(review.get("transcription_latex")):
            issues.append(f"equation_transcription_missing:{item_id}")
        for key in ("subscripts", "superscripts", "special_symbols"):
            if review.get(key) not in {"checked", "none", "uncertain"}:
                issues.append(f"equation_symbol_check_missing:{item_id}:{key}")
    elif kind == "figure":
        for key in ("figure_type", "figure_number", "caption_presence", "completeness", "has_subfigures"):
            if not _nonempty(review.get(key)):
                issues.append(f"figure_field_missing:{item_id}:{key}")
        if review.get("has_subfigures") == "yes" and not isinstance(review.get("subfigure_count"), int):
            issues.append(f"figure_subfigure_count_missing:{item_id}")
    elif kind == "table":
        for key in ("table_type", "row_count", "column_count", "header_text", "merged_cells", "caption_presence", "table_number", "structure_completeness"):
            if key not in review or review.get(key) is None:
                issues.append(f"table_field_missing:{item_id}:{key}")
        rows = review.get("row_count")
        columns = review.get("column_count")
        if type(rows) is not int or rows < 1:
            issues.append(f"table_row_count_invalid:{item_id}")
        if type(columns) is not int or columns < 1:
            issues.append(f"table_column_count_invalid:{item_id}")
        if type(rows) is int and rows >= 1 and type(columns) is int and columns >= 1:
            cells = review.get("cells")
            expected = rows * columns
            if not isinstance(cells, list) or len(cells) != expected or any(not isinstance(cell, Mapping) for cell in cells):
                issues.append(f"table_cells_incomplete:{item_id}")
            else:
                expected_coordinates = {(row, column) for row in range(rows) for column in range(columns)}
                seen_coordinates: set[tuple[int, int]] = set()
                coordinates_invalid = False
                text_invalid = False
                for cell in cells:
                    row = cell.get("row")
                    column = cell.get("column")
                    if type(row) is not int or type(column) is not int or not (0 <= row < rows and 0 <= column < columns):
                        coordinates_invalid = True
                    else:
                        coordinate = (row, column)
                        if coordinate in seen_coordinates:
                            coordinates_invalid = True
                        seen_coordinates.add(coordinate)
                    if not isinstance(cell.get("text"), str):
                        text_invalid = True
                if seen_coordinates != expected_coordinates:
                    coordinates_invalid = True
                if coordinates_invalid:
                    issues.append(f"table_cell_coordinates_invalid:{item_id}")
                if text_invalid:
                    issues.append(f"table_cell_text_invalid:{item_id}")
                elif not any(cell["text"].strip() for cell in cells):
                    issues.append(f"table_cells_all_blank:{item_id}")
    elif kind == "chart":
        for key in ("chart_type", "title", "x_axis_label", "y_axis_label", "legend", "series_count", "data_points_or_curves", "caption_presence", "chart_completeness"):
            if key not in review or review.get(key) is None:
                issues.append(f"chart_field_missing:{item_id}:{key}")
        if not isinstance(review.get("series_count"), int) or review.get("series_count", 0) < 0:
            issues.append(f"chart_series_count_invalid:{item_id}")
        series_names = review.get("series_names")
        if not isinstance(series_names, list) or len(series_names) != int(review.get("series_count", -1)):
            issues.append(f"chart_series_names_incomplete:{item_id}")
        if not _nonempty(review.get("data_points_or_curves")):
            issues.append(f"chart_data_or_curve_info_missing:{item_id}")
    else:
        issues.append(f"kind_invalid:{item_id}")
    return issues


def _scorable_payload(kind: str, review: Mapping[str, Any], canonical_item: Mapping[str, Any]) -> dict[str, Any]:
    bbox = None
    corrected_bbox = None
    bbox_source = "not_applicable"
    if review.get("presence") == "present":
        if review.get("bbox_verdict") == "correct":
            bbox = canonical_item["geometry"]["candidate_bbox_pdf"]
            bbox_source = "candidate"
        else:
            corrected_bbox, _ = _review_corrected_bbox(review)
            bbox = corrected_bbox
            bbox_source = "corrected"
    detail_keys = {
        "equation": ("equation_type", "transcription_latex", "formula_number", "crosses_lines", "subscripts", "superscripts", "special_symbols", "transcription_completeness"),
        "figure": ("figure_type", "caption", "caption_presence", "figure_number", "completeness", "has_subfigures", "subfigure_count"),
        "table": ("table_type", "row_count", "column_count", "header_text", "cells", "merged_cells", "caption", "caption_presence", "table_number", "structure_completeness"),
        "chart": ("chart_type", "title", "x_axis_label", "y_axis_label", "legend", "series_count", "series_names", "data_points_or_curves", "caption", "caption_presence", "chart_completeness"),
    }[kind]
    details = {key: review.get(key) for key in detail_keys} if review.get("presence") == "present" else {key: None for key in detail_keys}
    return {
        "presence": review.get("presence"),
        "type_verdict": review.get("type_verdict"),
        "bbox_verdict": review.get("bbox_verdict"),
        "bbox": bbox,
        "corrected_bbox": corrected_bbox,
        "bbox_source": bbox_source,
        "bbox_coordinate_space": COORDINATE_SPACE if bbox is not None else None,
        "kind": kind,
        "details": details,
    }


def _payload_with_audit(kind: str, review: Mapping[str, Any], canonical_item: Mapping[str, Any]) -> dict[str, Any]:
    payload = _scorable_payload(kind, review, canonical_item)
    payload["review_status"] = review.get("review_status")
    payload["reviewer_confidence"] = review.get("reviewer_confidence")
    payload["notes"] = review.get("notes", "")
    return payload


def _adjudication_detail_keys(kind: str) -> tuple[str, ...]:
    return {
        "equation": ("equation_type", "transcription_latex", "formula_number", "crosses_lines", "subscripts", "superscripts", "special_symbols", "transcription_completeness"),
        "figure": ("figure_type", "caption", "caption_presence", "figure_number", "completeness", "has_subfigures", "subfigure_count"),
        "table": ("table_type", "row_count", "column_count", "header_text", "cells", "merged_cells", "caption", "caption_presence", "table_number", "structure_completeness"),
        "chart": ("chart_type", "title", "x_axis_label", "y_axis_label", "legend", "series_count", "series_names", "data_points_or_curves", "caption", "caption_presence", "chart_completeness"),
    }[kind]


def _validate_manual_adjudication_value(field_path: str, value: Any, item: Mapping[str, Any]) -> None:
    if not ADJUDICATION_FIELD_PATH_RE.fullmatch(field_path):
        raise ReviewerWorkbenchError(f"adjudication_field_path_invalid:{field_path}")
    if field_path.startswith("details."):
        detail_key = field_path.split(".", 1)[1]
        kind = str(item.get("kind_candidate"))
        if detail_key not in _adjudication_detail_keys(kind):
            raise ReviewerWorkbenchError(f"adjudication_unknown_detail_field:{field_path}")
        if detail_key == "cells":
            if not isinstance(value, list) or any(not isinstance(cell, Mapping) or set(cell) != {"row", "column", "text"} or type(cell.get("row")) is not int or type(cell.get("column")) is not int or not isinstance(cell.get("text"), str) for cell in value):
                raise ReviewerWorkbenchError(f"adjudication_manual_value_invalid:{field_path}")
        elif detail_key in {"series_names"}:
            if not isinstance(value, list) or any(not isinstance(nested, str) for nested in value):
                raise ReviewerWorkbenchError(f"adjudication_manual_value_invalid:{field_path}")
        elif detail_key in {"row_count", "column_count", "series_count", "subfigure_count"}:
            if value is not None and (type(value) is not int or value < 0):
                raise ReviewerWorkbenchError(f"adjudication_manual_value_invalid:{field_path}")
        elif value is not None and not isinstance(value, str):
            raise ReviewerWorkbenchError(f"adjudication_manual_value_invalid:{field_path}")
        return
    if field_path == "presence" and value not in PRESENCE_VALUES:
        raise ReviewerWorkbenchError(f"adjudication_manual_value_invalid:{field_path}")
    if field_path == "bbox_verdict" and value not in BBOX_VERDICTS:
        raise ReviewerWorkbenchError(f"adjudication_manual_value_invalid:{field_path}")
    if field_path == "bbox_source" and value not in {"candidate", "corrected", "not_applicable"}:
        raise ReviewerWorkbenchError(f"adjudication_manual_value_invalid:{field_path}")
    if field_path == "bbox_coordinate_space" and value not in {None, COORDINATE_SPACE}:
        raise ReviewerWorkbenchError(f"adjudication_manual_value_invalid:{field_path}")
    if field_path in {"bbox", "corrected_bbox"}:
        if value is not None and (not isinstance(value, list) or len(value) != 4 or any(type(number) not in {int, float} or not math.isfinite(number) for number in value) or not _valid_bbox(value)):
            raise ReviewerWorkbenchError(f"adjudication_manual_value_invalid:{field_path}")
        return
    if field_path == "kind":
        if value != item.get("kind_candidate"):
            raise ReviewerWorkbenchError(f"adjudication_manual_value_invalid:{field_path}")
        return
    if field_path == "type_verdict" and (not isinstance(value, str) or not value.strip()):
        raise ReviewerWorkbenchError(f"adjudication_manual_value_invalid:{field_path}")
    if field_path not in {"presence", "bbox_verdict", "bbox_source", "bbox_coordinate_space", "kind", "type_verdict"} and not isinstance(value, str):
        raise ReviewerWorkbenchError(f"adjudication_manual_value_invalid:{field_path}")


def _set_adjudication_field(payload: dict[str, Any], field_path: str, value: Any) -> None:
    if field_path.startswith("details."):
        payload.setdefault("details", {})[field_path.split(".", 1)[1]] = deepcopy(value)
    else:
        payload[field_path] = deepcopy(value)


def _validate_adjudicated_payload(item_id: str, item: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(item["kind_candidate"])
    expected_keys = set(ADJUDICATION_SCALAR_FIELDS) | {"details"}
    if set(payload) != expected_keys or not isinstance(payload.get("details"), Mapping) or set(payload["details"]) != set(_adjudication_detail_keys(kind)):
        raise ReviewerWorkbenchError(f"adjudication_payload_shape_invalid:{item_id}")
    details = dict(payload["details"])
    review = dict(details)
    review.update({"review_status": "complete", "reviewer_confidence": "high", "difference_observation": "adjudication observation", "presence": payload.get("presence"), "type_verdict": payload.get("type_verdict"), "bbox_verdict": payload.get("bbox_verdict")})
    if payload.get("corrected_bbox") is not None:
        review["corrected_bbox"] = payload.get("corrected_bbox")
    issues = _review_content_issues(item_id, kind, review, item)
    candidate_bbox = item.get("geometry", {}).get("candidate_bbox_pdf") if isinstance(item.get("geometry"), Mapping) else None
    bbox = payload.get("bbox")
    corrected_bbox = payload.get("corrected_bbox")
    if payload.get("kind") != kind:
        issues.append(f"kind_invalid:{item_id}")
    if payload.get("bbox_coordinate_space") != (COORDINATE_SPACE if bbox is not None else None):
        issues.append(f"bbox_coordinate_space_invalid:{item_id}")
    if payload.get("presence") == "present":
        if payload.get("bbox_source") == "candidate":
            if corrected_bbox is not None or not _json_exact_equal(bbox, candidate_bbox):
                issues.append(f"candidate_bbox_binding_invalid:{item_id}")
        elif payload.get("bbox_source") == "corrected":
            if corrected_bbox is None or not _json_exact_equal(bbox, corrected_bbox):
                issues.append(f"corrected_bbox_binding_invalid:{item_id}")
        else:
            issues.append(f"bbox_source_invalid:{item_id}")
    elif any(payload.get(key) is not None for key in ("bbox", "corrected_bbox")) or payload.get("bbox_source") != "not_applicable":
        issues.append(f"absent_bbox_binding_invalid:{item_id}")
    if issues:
        raise ReviewerWorkbenchError(f"adjudication_payload_invalid:{item_id}:{'|'.join(sorted(set(issues)))}")
    normalized = deepcopy(dict(payload))
    if kind == "table":
        cells = normalized["details"]["cells"]
        normalized["details"]["cells"] = sorted((deepcopy(cell) for cell in cells), key=lambda cell: (cell["row"], cell["column"]))
    return normalized


def _load_adjudication_submission(path: Path, manifest: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Any]:
    submission_path = _secure_file(Path(path).expanduser(), "adjudication_submission")
    value = _read_json(submission_path, "adjudication_submission")
    if value.get("schema_version") != VISUAL_REVIEW_ADJUDICATION_SUBMISSION_SCHEMA or value.get("protocol") != VISUAL_REVIEW_ADJUDICATION_PROTOCOL or value.get("compiler_version") != VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION:
        raise ReviewerWorkbenchError("adjudication_submission_schema_invalid")
    if value.get("adjudication_manifest_sha256") != manifest.get("manifest_sha256") or value.get("workbench_instance_id") != manifest.get("workbench_instance_id"):
        raise ReviewerWorkbenchError("adjudication_submission_manifest_binding_invalid")
    if not _json_transport_identity_equal(value.get("workpack_identity"), manifest.get("workpack_identity")) or not _json_exact_equal(value.get("source_submissions"), manifest.get("source_submissions")) or value.get("dispute_set_sha256") != manifest.get("dispute_set_sha256"):
        raise ReviewerWorkbenchError("adjudication_submission_identity_mismatch")
    adjudicator = value.get("adjudicator") if isinstance(value.get("adjudicator"), Mapping) else {}
    manifest_adjudicator = manifest.get("adjudicator") if isinstance(manifest.get("adjudicator"), Mapping) else {}
    if dict(adjudicator) != dict(manifest_adjudicator):
        raise ReviewerWorkbenchError("adjudication_submission_adjudicator_mismatch")
    if _forbidden_key_present(value, ignore_declarations=True) is not None:
        raise ReviewerWorkbenchError("adjudication_submission_hidden_field")
    declarations = value.get("declarations") if isinstance(value.get("declarations"), Mapping) else {}
    if set(declarations) != set(ADJUDICATION_DECLARATION_KEYS):
        raise ReviewerWorkbenchError("adjudication_declaration_set_invalid")
    content_issues = [f"adjudication_declaration_missing_or_false:{key}" for key in ADJUDICATION_DECLARATION_KEYS if declarations.get(key) is not True]
    expected_by_id = {str(row["item_id"]): row for row in manifest.get("items", []) if isinstance(row, Mapping) and isinstance(row.get("item_id"), str)}
    raw_items = value.get("items")
    if not isinstance(raw_items, list):
        raise ReviewerWorkbenchError("adjudication_submission_items_required")
    item_by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("item_id"), str):
            raise ReviewerWorkbenchError("adjudication_submission_item_invalid")
        item_id = str(raw["item_id"])
        if item_id in item_by_id:
            raise ReviewerWorkbenchError(f"adjudication_duplicate_dispute:{item_id}")
        if item_id not in expected_by_id:
            raise ReviewerWorkbenchError(f"adjudication_unknown_dispute:{item_id}")
        expected = expected_by_id[item_id]
        allowed_item_keys = {"item_id", "item_sha256", "review_task_id", "input_hashes", "resolutions", "rationale", "observation"}
        if set(raw) != allowed_item_keys:
            raise ReviewerWorkbenchError(f"adjudication_submission_item_field_invalid:{item_id}")
        if raw.get("item_sha256") != expected.get("item_sha256") or raw.get("review_task_id") != expected.get("review_task_id") or raw.get("input_hashes") != expected.get("input_hashes"):
            raise ReviewerWorkbenchError(f"adjudication_submission_item_hash_mismatch:{item_id}")
        resolutions = raw.get("resolutions")
        if not isinstance(resolutions, Mapping):
            raise ReviewerWorkbenchError(f"adjudication_resolutions_invalid:{item_id}")
        expected_fields = set(str(field) for field in expected.get("differing_fields", []))
        if set(resolutions) - expected_fields:
            raise ReviewerWorkbenchError(f"adjudication_resolution_unknown_field:{item_id}")
        missing_fields = sorted(expected_fields - set(resolutions))
        content_issues.extend(f"adjudication_resolution_missing:{item_id}:{field}" for field in missing_fields)
        for field_path, resolution in resolutions.items():
            if not isinstance(resolution, Mapping) or set(resolution) - {"source", "value"} or "source" not in resolution:
                raise ReviewerWorkbenchError(f"adjudication_resolution_invalid:{item_id}:{field_path}")
            source = resolution.get("source")
            if source not in ADJUDICATION_SOURCE_VALUES:
                raise ReviewerWorkbenchError(f"adjudication_resolution_source_invalid:{item_id}:{field_path}")
            if source == "unresolved":
                if "value" in resolution and resolution.get("value") is not None:
                    raise ReviewerWorkbenchError(f"adjudication_unresolved_value_present:{item_id}:{field_path}")
                content_issues.append(f"adjudication_unresolved:{item_id}:{field_path}")
                continue
            if "value" not in resolution:
                raise ReviewerWorkbenchError(f"adjudication_resolution_value_missing:{item_id}:{field_path}")
            expected_source = expected["reviewer_1"] if source == "reviewer-1" else expected["reviewer_2"] if source == "reviewer-2" else None
            if expected_source is not None:
                expected_value = _adjudication_field_values(expected_source["payload"]).get(field_path)
                if not _json_exact_equal(resolution.get("value"), expected_value):
                    raise ReviewerWorkbenchError(f"adjudication_reviewer_value_hash_mismatch:{item_id}:{field_path}")
            else:
                _validate_manual_adjudication_value(str(field_path), resolution.get("value"), {"kind_candidate": expected["kind_candidate"]})
        rationale = raw.get("rationale")
        observation = raw.get("observation")
        if not isinstance(rationale, str) or not rationale.strip():
            content_issues.append(f"adjudication_rationale_missing:{item_id}")
        if not isinstance(observation, str) or not observation.strip() or _difference_observation_placeholder(observation) is not None:
            content_issues.append(f"adjudication_observation_missing_or_placeholder:{item_id}")
        item_by_id[item_id] = {"item_id": item_id, "item_sha256": raw["item_sha256"], "review_task_id": raw["review_task_id"], "input_hashes": raw["input_hashes"], "resolutions": {str(key): deepcopy(value) for key, value in resolutions.items()}, "rationale": rationale, "observation": observation}
    missing_items = sorted(set(expected_by_id) - set(item_by_id))
    content_issues.extend(f"adjudication_missing_dispute:{item_id}" for item_id in missing_items)
    return {"path": submission_path, "file_sha256": sha256_file(submission_path), "adjudicator_instance": manifest_adjudicator.get("adjudicator_instance"), "adjudication_session_id": manifest_adjudicator.get("adjudication_session_id"), "declarations": dict(declarations), "items": item_by_id, "content_issues": sorted(set(content_issues))}


def _make_attestation(item: Mapping[str, Any], task: Mapping[str, Any], submission: Mapping[str, Any], review: Mapping[str, Any], proposer_instance: str) -> dict[str, Any]:
    expected_hashes = _gold_input_hashes(item, task)
    attestation = {
        "schema_version": VISUAL_BENCHMARK_ATTESTATION_SCHEMA,
        "protocol": VISUAL_BENCHMARK_PROTOCOL,
        "attestation_id": "attestation-" + sha256_json({"item_id": item["item_id"], "reviewer": submission["reviewer_instance"], "session": submission["review_session_id"], "item_sha256": item["item_sha256"]})[:24],
        "reviewer_instance": submission["reviewer_instance"],
        "reviewer_role": "external-independent",
        "review_session_id": submission["review_session_id"],
        "proposer_instance": proposer_instance,
        "review_method": "fresh-crop-and-render-inspection",
        "difference_observation": str(review["difference_observation"]).strip(),
        "review_checks": {"crop_opened": True, "render_opened": True, "source_identity_checked": True, "item_hash_checked": True, "geometry_checked": True, "coordinate_mapping_checked": True, "corrected_bbox_checked": True, "split_checked": True},
        "input_hashes": {"item_sha256": item["item_sha256"], **expected_hashes},
    }
    attestation["attestation_sha256"] = _attestation_hash(attestation)
    return attestation


def _reviewer_registry(parsed: Sequence[Mapping[str, Any]], identity: Mapping[str, Any], expected_ids: set[str]) -> dict[str, Any]:
    reviewers: list[dict[str, Any]] = []
    for submission in sorted(parsed, key=lambda row: str(row["reviewer_instance"])):
        item_ids = sorted(str(value) for value in submission["items"])
        reviewers.append({
            "reviewer_instance": submission["reviewer_instance"],
            "review_session_id": submission["review_session_id"],
            "reviewer_role": "external-independent",
            "independent_from_proposer": submission["declarations"].get("independent_from_proposer") is True,
            "not_evaluator": submission["declarations"].get("not_evaluator") is True,
            "reviewed_item_count": len(item_ids),
            "reviewed_item_ids_sha256": sha256_json(item_ids),
        })
    registry = {"schema_version": VISUAL_REVIEW_WORKBENCH_REVIEWER_REGISTRY_SCHEMA, "protocol": VISUAL_REVIEW_WORKBENCH_PROTOCOL, "workpack_identity": _submission_identity(identity), "expected_item_count": len(expected_ids), "reviewers": reviewers, "expected_item_ids_sha256": sha256_json(sorted(expected_ids))}
    registry["registry_sha256"] = _hash_without(registry, "registry_sha256")
    return registry


def _resolve_adjudication_payload(item: Mapping[str, Any], dispute: Mapping[str, Any], adjudication_item: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = deepcopy(dispute["reviewer_1"]["payload"])
    field_values = {
        "reviewer-1": _adjudication_field_values(dispute["reviewer_1"]["payload"]),
        "reviewer-2": _adjudication_field_values(dispute["reviewer_2"]["payload"]),
    }
    for field_path in dispute["differing_fields"]:
        resolution = adjudication_item.get("resolutions", {}).get(field_path) if isinstance(adjudication_item.get("resolutions"), Mapping) else None
        if not isinstance(resolution, Mapping) or resolution.get("source") == "unresolved":
            return None
        source = resolution.get("source")
        value = resolution.get("value")
        if source in field_values:
            expected_value = field_values[source].get(field_path)
            if not _json_exact_equal(value, expected_value):
                raise ReviewerWorkbenchError(f"adjudication_reviewer_value_hash_mismatch:{item['item_id']}:{field_path}")
        elif source == "manual":
            _validate_manual_adjudication_value(field_path, value, item)
        else:
            raise ReviewerWorkbenchError(f"adjudication_resolution_source_invalid:{item['item_id']}:{field_path}")
        _set_adjudication_field(payload, field_path, value)
    return _validate_adjudicated_payload(str(item["item_id"]), item, payload)


def _adjudication_resolution_record(dispute: Mapping[str, Any], adjudication_item: Mapping[str, Any], resolved_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    resolution_core = {
        "item_id": dispute["item_id"],
        "resolutions": deepcopy(adjudication_item.get("resolutions", {})),
        "rationale": adjudication_item.get("rationale"),
        "observation": adjudication_item.get("observation"),
    }
    return {
        "item_id": dispute["item_id"],
        "item_sha256": dispute["item_sha256"],
        "review_task_id": dispute["review_task_id"],
        "kind_candidate": dispute["kind_candidate"],
        "input_hashes": deepcopy(dispute["input_hashes"]),
        "reviewer_payload_sha256s": {"reviewer-1": dispute["reviewer_1"]["payload_sha256"], "reviewer-2": dispute["reviewer_2"]["payload_sha256"]},
        "resolution_sha256": sha256_json(resolution_core),
        "resolutions": deepcopy(adjudication_item.get("resolutions", {})),
        "rationale": adjudication_item.get("rationale"),
        "observation": adjudication_item.get("observation"),
        "resolved_payload_sha256": sha256_json(resolved_payload) if resolved_payload is not None else None,
    }


def _make_adjudication_receipt(manifest: Mapping[str, Any], adjudication_submission: Mapping[str, Any], records: Sequence[Mapping[str, Any]], *, final_status: str, issues: Sequence[str]) -> dict[str, Any]:
    source_submissions = manifest["source_submissions"]
    receipt = {
        "schema_version": VISUAL_REVIEW_ADJUDICATION_RECEIPT_SCHEMA,
        "protocol": VISUAL_REVIEW_ADJUDICATION_PROTOCOL,
        "compiler_version": VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION,
        "receipt_id": "adjudication-receipt-" + sha256_json({"manifest_sha256": manifest["manifest_sha256"], "submission_sha256": adjudication_submission["file_sha256"], "dispute_set_sha256": manifest["dispute_set_sha256"]})[:24],
        "workbench_instance_id": manifest["workbench_instance_id"],
        "adjudication_manifest_sha256": manifest["manifest_sha256"],
        "workpack_identity": deepcopy(manifest["workpack_identity"]),
        "source_submissions": deepcopy(source_submissions),
        "source_submission_sha256s": sorted(str(row["submission_sha256"]) for row in source_submissions),
        "adjudication_submission_sha256": adjudication_submission["file_sha256"],
        "dispute_set_sha256": manifest["dispute_set_sha256"],
        "adjudicator": deepcopy(manifest["adjudicator"]),
        "declarations": deepcopy(adjudication_submission["declarations"]),
        "item_count": len(manifest["items"]),
        "resolved_dispute_count": sum(1 for row in records if row.get("resolved_payload_sha256")),
        "items": [deepcopy(row) for row in sorted(records, key=lambda value: str(value["item_id"]))],
        "finalization_status": final_status,
        "import_compatible": final_status == "accepted-for-import",
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
        "issues": sorted(set(str(issue) for issue in issues)),
    }
    receipt["receipt_sha256"] = _hash_without(receipt, "receipt_sha256")
    return receipt


def _build_adjudication_gold_rows(facts: Mapping[str, Any], identity: Mapping[str, Any], parsed: Sequence[Mapping[str, Any]], disputes: Mapping[str, Mapping[str, Any]], adjudicated_payloads: Mapping[str, Mapping[str, Any]], receipt: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    expected_ids = set(str(value) for value in facts["candidate_ids"])
    proposer_instances = set(str(value) for value in facts["review_plan"].get("proposer_instances", []) if isinstance(value, str))
    proposer_instance = sorted(proposer_instances)[0] if proposer_instances else "phase7d5a-preparation-agent"
    rows: list[dict[str, Any]] = []
    attestations_all: list[dict[str, Any]] = []
    issues: list[str] = []
    ordered_parsed = sorted(parsed, key=lambda value: str(value["reviewer_instance"]))
    for item_id in sorted(expected_ids):
        item = facts["candidate_by_id"][item_id]
        task = facts["task_by_item"][item_id]
        item_reviews = [row for row in ordered_parsed if item_id in row["items"]]
        required = int(task["required_independent_reviewers"])
        if len(item_reviews) < required or (len(item_reviews) > required and item["kind_candidate"] in {"table", "chart"}):
            issues.append(f"reviewer_count_invalid:{item_id}:{len(item_reviews)}:{required}")
            continue
        chosen = item_reviews[:required]
        reviews = [row["items"][item_id] for row in chosen]
        signatures = [sha256_json(_scorable_payload(str(item["kind_candidate"]), row["review"], item)) for row in reviews]
        if any(_review_content_issues(item_id, str(item["kind_candidate"]), row["review"], item) for row in reviews):
            issues.append(f"item_not_complete:{item_id}")
            continue
        if item_id in disputes:
            if item_id not in adjudicated_payloads:
                issues.append(f"adjudication_unresolved:{item_id}")
                continue
            payload = deepcopy(adjudicated_payloads[item_id])
            payload["review_status"] = "adjudicated"
            payload["reviewer_confidence"] = "adjudicator-selected"
            payload["notes"] = ""
            payload["independent_reviewer_count"] = required
            payload["reviewer_agreement"] = "adjudicated"
            resolution_record = next((row for row in receipt["items"] if row.get("item_id") == item_id), None)
            if not isinstance(resolution_record, Mapping) or resolution_record.get("resolved_payload_sha256") != sha256_json(payload):
                issues.append(f"adjudication_resolved_payload_hash_invalid:{item_id}")
                continue
            adjudication_provenance = {
                "receipt_schema": VISUAL_REVIEW_ADJUDICATION_RECEIPT_SCHEMA,
                "receipt_id": receipt["receipt_id"],
                "receipt_sha256": receipt["receipt_sha256"],
                "manifest_sha256": receipt["adjudication_manifest_sha256"],
                "dispute_set_sha256": receipt["dispute_set_sha256"],
                "source_submission_sha256s": list(receipt["source_submission_sha256s"]),
                "item_resolution_sha256": resolution_record["resolution_sha256"],
                "receipt": deepcopy(dict(receipt)),
            }
        else:
            if len(set(signatures)) != 1:
                issues.append(f"needs_adjudication:reviewer_disagreement:{item_id}")
                continue
            payload = _payload_with_audit(str(item["kind_candidate"]), reviews[0]["review"], item)
            payload["independent_reviewer_count"] = required
            payload["reviewer_agreement"] = "exact-scorable-agreement"
            adjudication_provenance = None
        attestations = [_make_attestation(item, task, chosen[index], reviews[index]["review"], proposer_instance) for index in range(len(reviews))]
        attestations_all.extend(attestations)
        registry = [{"reviewer_instance": row["reviewer_instance"], "reviewer_role": "external-independent", "independent_from_proposer": row["declarations"].get("independent_from_proposer") is True} for row in chosen]
        provenance = {"source_type": "external-independent-review-file", "created_outside_preparation_agent": True, "compiler_authored": False, "evaluator_role_separate": True}
        if adjudication_provenance is not None:
            provenance["adjudication"] = adjudication_provenance
        row = {
            "schema_version": VISUAL_BENCHMARK_GOLD_FRAGMENT_SCHEMA,
            "protocol": VISUAL_BENCHMARK_PROTOCOL,
            "item_id": item_id,
            "item_sha256": item["item_sha256"],
            "review_task_id": task["review_task_id"],
            "kind_candidate": item["kind_candidate"],
            "split": "calibration" if item_id in facts["calibration_ids"] else "test",
            "input_hashes": _gold_input_hashes(item, task),
            "gold_payload": payload,
            "reviewer_registry": registry,
            "attestations": attestations,
            "fragment_provenance": provenance,
            "independent_real_gold": True,
            "verified_gold": False,
            "promotion_allowed": False,
        }
        row["reviewer_registry_sha256"] = sha256_json(registry)
        row["gold_payload_sha256"] = sha256_json(payload)
        row["fragment_sha256"] = _fragment_hash(row)
        rows.append(row)
    return rows, attestations_all, sorted(set(issues))


def _adjudication_revision_context(workbench: Path, workpack: Path, reviewer_instance: Path, reviewer_submissions: Sequence[Path], *, comparison_receipt: Path | None = None) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    validate_adjudication_workbench(workbench, workpack, reviewer_instance, reviewer_submissions, comparison_receipt=comparison_receipt)
    root = _secure_directory(Path(workbench).expanduser())
    manifest = _read_json(root / "adjudication-manifest.json", "adjudication_manifest")
    facts, identity, parsed, source = _source_adjudication_inputs(workpack, reviewer_instance, reviewer_submissions, comparison_receipt=comparison_receipt)
    return facts, identity, parsed, source, manifest


def finalize_adjudication_revision(workbench: Path, workpack: Path, reviewer_instance: Path, reviewer_submissions: Sequence[Path], adjudication_submission: Path, output: Path, *, revision: int = 1, comparison_receipt: Path | None = None) -> dict[str, Any]:
    if revision < 1:
        raise ReviewerWorkbenchError("adjudication_revision_invalid")
    facts, identity, parsed, source, manifest = _adjudication_revision_context(workbench, workpack, reviewer_instance, reviewer_submissions, comparison_receipt=comparison_receipt)
    expected_count = len(facts["candidate_ids"])
    adjudication = _load_adjudication_submission(Path(adjudication_submission), manifest, facts)
    issues = list(adjudication["content_issues"])
    disputes = {str(row["item_id"]): row for row in source["disputes"]}
    adjudication_items = adjudication["items"]
    resolved_payloads: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for item_id, dispute in sorted(disputes.items()):
        adjudication_item = adjudication_items.get(item_id)
        if not isinstance(adjudication_item, Mapping):
            records.append(_adjudication_resolution_record(dispute, {"resolutions": {}, "rationale": None, "observation": None}, None))
            continue
        item = facts["candidate_by_id"][item_id]
        payload = _resolve_adjudication_payload(item, dispute, adjudication_item)
        if payload is not None:
            audited_payload = deepcopy(payload)
            required = int(facts["task_by_item"][item_id]["required_independent_reviewers"])
            audited_payload.update({"review_status": "adjudicated", "reviewer_confidence": "adjudicator-selected", "notes": "", "independent_reviewer_count": required, "reviewer_agreement": "adjudicated"})
            resolved_payloads[item_id] = audited_payload
        records.append(_adjudication_resolution_record(dispute, adjudication_item, resolved_payloads.get(item_id)))
    all_resolved = not issues and len(resolved_payloads) == len(disputes)
    provisional_status = "accepted-for-import" if all_resolved else "paused"
    receipt = _make_adjudication_receipt(manifest, adjudication, records, final_status=provisional_status, issues=issues)
    ready_rows: list[dict[str, Any]] = []
    all_attestations: list[dict[str, Any]] = []
    row_issues: list[str] = []
    if all_resolved:
        ready_rows, all_attestations, row_issues = _build_adjudication_gold_rows(facts, identity, parsed, disputes, resolved_payloads, receipt)
        issues.extend(row_issues)
    if issues:
        final_status = "paused"
        ready_rows = []
        if not all_attestations:
            _, all_attestations, _ = _build_adjudication_gold_rows(facts, identity, parsed, disputes, {}, receipt)
    else:
        final_status = "accepted-for-import"
    if receipt["finalization_status"] != final_status or receipt["import_compatible"] != (final_status == "accepted-for-import") or receipt["issues"] != sorted(set(issues)):
        receipt = _make_adjudication_receipt(manifest, adjudication, records, final_status=final_status, issues=issues)
        if final_status == "accepted-for-import":
            ready_rows, all_attestations, row_issues = _build_adjudication_gold_rows(facts, identity, parsed, disputes, resolved_payloads, receipt)
            issues.extend(row_issues)
            if row_issues:
                final_status = "paused"
                ready_rows = []
                receipt = _make_adjudication_receipt(manifest, adjudication, records, final_status=final_status, issues=issues)
    import_compatible = final_status == "accepted-for-import" and len(ready_rows) == expected_count
    if not import_compatible:
        ready_rows = []
    destination = _secure_directory(Path(output).expanduser(), must_be_empty=True)
    registry = _reviewer_registry(parsed, identity, set(str(value) for value in facts["candidate_ids"]))
    _write_json(destination / "reviewer-registry.json", registry)
    declarations = [{"reviewer_instance": row["reviewer_instance"], "declarations": row["declarations"]} for row in sorted(parsed, key=lambda value: str(value["reviewer_instance"]))]
    attestation_bundle = {"schema_version": VISUAL_REVIEW_WORKBENCH_ATTESTATION_BUNDLE_SCHEMA, "protocol": VISUAL_REVIEW_WORKBENCH_PROTOCOL, "workpack_identity": _submission_identity(identity), "coverage": {"expected_item_count": expected_count, "reviewed_item_count": len({att["input_hashes"]["item_sha256"] for att in all_attestations}), "item_ids_sha256": sha256_json(sorted({att["input_hashes"]["item_sha256"] for att in all_attestations}))}, "declarations": declarations, "attestations": all_attestations, "compiler_authored": False, "evaluator_role_separate": True}
    attestation_bundle["attestation_bundle_sha256"] = _hash_without(attestation_bundle, "attestation_bundle_sha256")
    _write_json(destination / "review-attestation.json", attestation_bundle)
    _write_jsonl(destination / "gold-fragments.jsonl", ready_rows)
    _write_json(destination / "adjudication-receipt.json", receipt)
    submission_hash = str(adjudication["file_sha256"])
    revision_id = "adjudication-revision-" + sha256_json({"workbench_instance_id": manifest["workbench_instance_id"], "adjudication_submission_sha256": submission_hash, "revision": revision, "status": final_status})[:32]
    return {"status": final_status, "revision": str(destination.resolve()), "adjudication_receipt": str((destination / "adjudication-receipt.json").resolve()), "gold_fragments": str((destination / "gold-fragments.jsonl").resolve()), "revision_id": revision_id, "coverage": {"expected_item_count": expected_count, "dispute_item_count": len(disputes), "resolved_dispute_count": len(resolved_payloads), "gold_row_count": len(ready_rows), "complete_all_items": len(ready_rows) == expected_count, "complete_31_of_31": len(ready_rows) == expected_count}, "issues": sorted(set(issues)), "import_compatible": import_compatible, "verified_gold": False, "promotion_allowed": False, "release_included": False, "immutable": True}


def validate_adjudication_revision(revision: Path, workbench: Path, workpack: Path, reviewer_instance: Path, reviewer_submissions: Sequence[Path], adjudication_submission: Path, *, comparison_receipt: Path | None = None) -> dict[str, Any]:
    root = _secure_directory(Path(revision).expanduser())
    facts, identity, parsed, source, manifest = _adjudication_revision_context(workbench, workpack, reviewer_instance, reviewer_submissions, comparison_receipt=comparison_receipt)
    expected_count = len(facts["candidate_ids"])
    adjudication = _load_adjudication_submission(Path(adjudication_submission), manifest, facts)
    receipt = _read_json(root / "adjudication-receipt.json", "adjudication_receipt")
    if receipt.get("schema_version") != VISUAL_REVIEW_ADJUDICATION_RECEIPT_SCHEMA or receipt.get("protocol") != VISUAL_REVIEW_ADJUDICATION_PROTOCOL or receipt.get("compiler_version") != VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION:
        raise ReviewerWorkbenchError("adjudication_receipt_schema_invalid")
    if receipt.get("receipt_sha256") != _hash_without(receipt, "receipt_sha256"):
        raise ReviewerWorkbenchError("adjudication_receipt_hash_mismatch")
    disputes = {str(row["item_id"]): row for row in source["disputes"]}
    records: list[dict[str, Any]] = []
    resolved_payloads: dict[str, dict[str, Any]] = {}
    for item_id, dispute in sorted(disputes.items()):
        adjudication_item = adjudication["items"].get(item_id)
        payload = _resolve_adjudication_payload(facts["candidate_by_id"][item_id], dispute, adjudication_item) if isinstance(adjudication_item, Mapping) else None
        if payload is not None:
            audited = deepcopy(payload)
            required = int(facts["task_by_item"][item_id]["required_independent_reviewers"])
            audited.update({"review_status": "adjudicated", "reviewer_confidence": "adjudicator-selected", "notes": "", "independent_reviewer_count": required, "reviewer_agreement": "adjudicated"})
            resolved_payloads[item_id] = audited
        records.append(_adjudication_resolution_record(dispute, adjudication_item or {"resolutions": {}, "rationale": None, "observation": None}, resolved_payloads.get(item_id)))
    expected_status = "accepted-for-import" if not adjudication["content_issues"] and len(resolved_payloads) == len(disputes) else "paused"
    expected_receipt = _make_adjudication_receipt(manifest, adjudication, records, final_status=expected_status, issues=adjudication["content_issues"])
    if not _json_exact_equal(receipt, expected_receipt):
        raise ReviewerWorkbenchError("adjudication_receipt_binding_invalid")
    gold_rows = _read_jsonl(_secure_file(root / "gold-fragments.jsonl", "adjudication_gold_fragments"), "adjudication_gold_fragments")
    if expected_status == "accepted-for-import":
        try:
            external = validate_external_gold_fragments(facts, [root / "gold-fragments.jsonl"])
        except (VisualBenchmarkError, OSError, ValueError) as error:
            raise ReviewerWorkbenchError(f"adjudication_gold_compatibility_invalid:{error}") from error
        if external.get("item_count") != expected_count or len(gold_rows) != expected_count:
            raise ReviewerWorkbenchError("adjudication_gold_count_invalid")
    elif gold_rows:
        raise ReviewerWorkbenchError("adjudication_paused_gold_must_be_empty")
    if receipt.get("verified_gold") is not False or receipt.get("promotion_allowed") is not False or receipt.get("release_included") is not False:
        raise ReviewerWorkbenchError("adjudication_receipt_policy_invalid")
    return {"status": "verified", "finalization_status": receipt["finalization_status"], "revision": str(root.resolve()), "gold_row_count": len(gold_rows), "dispute_item_count": len(disputes), "resolved_dispute_count": len(resolved_payloads), "import_compatible": receipt["import_compatible"], "verified_gold": False, "promotion_allowed": False, "release_included": False, "immutable": True}


def _portable_gold_decision_rows(receipt: Mapping[str, Any], comparison_receipt_sha256: str) -> list[dict[str, Any]]:
    curator = receipt.get("adjudicator") if isinstance(receipt.get("adjudicator"), Mapping) else {}
    rows: list[dict[str, Any]] = []
    for record in receipt.get("items", []):
        if not isinstance(record, Mapping):
            raise ReviewerWorkbenchError("portable_gold_decision_record_invalid")
        resolutions = record.get("resolutions") if isinstance(record.get("resolutions"), Mapping) else {}
        row = {
            "schema_version": PORTABLE_GOLD_REVIEW_DECISION_SCHEMA,
            "protocol": PORTABLE_GOLD_REVIEW_PROTOCOL,
            "compiler_version": PORTABLE_GOLD_REVIEW_COMPILER_VERSION,
            "item_id": record.get("item_id"),
            "item_sha256": record.get("item_sha256"),
            "review_task_id": record.get("review_task_id"),
            "input_hashes": deepcopy(record.get("input_hashes")),
            "curator_instance": curator.get("adjudicator_instance"),
            "curator_session_id": curator.get("adjudication_session_id"),
            "comparison_receipt_sha256": comparison_receipt_sha256,
            "dispute_set_sha256": receipt.get("dispute_set_sha256"),
            "field_decisions": {
                str(path): {"source": value.get("source"), "value_sha256": sha256_json(value.get("value"))}
                for path, value in sorted(resolutions.items())
                if isinstance(value, Mapping)
            },
            "rationale": record.get("rationale"),
            "observation": record.get("observation"),
            "resolution_sha256": record.get("resolution_sha256"),
            "resolved_payload_sha256": record.get("resolved_payload_sha256"),
            "release_included": False,
        }
        row["decision_sha256"] = _hash_without(row, "decision_sha256")
        rows.append(row)
    return sorted(rows, key=lambda value: str(value["item_id"]))


def finalize_portable_gold_review(
    workbench: Path,
    workpack: Path,
    reviewer_instance: Path,
    reviewer_submissions: Sequence[Path],
    gold_review_submission: Path,
    comparison_receipt: Path,
    output: Path,
    *,
    revision: int = 1,
) -> dict[str, Any]:
    comparison_path = _secure_file(Path(comparison_receipt).expanduser(), "comparison_receipt")
    comparison = _read_json(comparison_path, "comparison_receipt")
    result = finalize_adjudication_revision(
        workbench,
        workpack,
        reviewer_instance,
        reviewer_submissions,
        gold_review_submission,
        output,
        revision=revision,
        comparison_receipt=comparison_path,
    )
    root = _secure_directory(Path(output).expanduser())
    receipt = _read_json(root / "adjudication-receipt.json", "adjudication_receipt")
    decisions = _portable_gold_decision_rows(receipt, str(comparison["receipt_sha256"]))
    _write_jsonl(root / "gold-decisions.jsonl", decisions)
    manifest = {
        "schema_version": PORTABLE_GOLD_REVIEW_REVISION_SCHEMA,
        "protocol": PORTABLE_GOLD_REVIEW_PROTOCOL,
        "compiler_version": PORTABLE_GOLD_REVIEW_COMPILER_VERSION,
        "revision_id": "portable-gold-review-" + sha256_json({"adjudication_receipt_sha256": receipt["receipt_sha256"], "comparison_receipt_sha256": comparison["receipt_sha256"], "revision": revision})[:32],
        "revision": revision,
        "status": result["status"],
        "assurance_tier": "dual-source-adjudicated",
        "workpack_identity": deepcopy(receipt["workpack_identity"]),
        "source_submissions": deepcopy(receipt["source_submissions"]),
        "curator": deepcopy(receipt["adjudicator"]),
        "comparison_receipt_sha256": comparison["receipt_sha256"],
        "adjudication_receipt_sha256": receipt["receipt_sha256"],
        "dispute_set_sha256": receipt["dispute_set_sha256"],
        "decision_item_count": len(decisions),
        "resolved_decision_item_count": sum(1 for row in decisions if row["resolved_payload_sha256"] is not None),
        "gold_row_count": result["coverage"]["gold_row_count"],
        "file_hashes": {
            "adjudication-receipt.json": sha256_file(root / "adjudication-receipt.json"),
            "gold-decisions.jsonl": sha256_file(root / "gold-decisions.jsonl"),
            "gold-fragments.jsonl": sha256_file(root / "gold-fragments.jsonl"),
            "review-attestation.json": sha256_file(root / "review-attestation.json"),
            "reviewer-registry.json": sha256_file(root / "reviewer-registry.json"),
        },
        "import_compatible": result["import_compatible"],
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
        "immutable": True,
    }
    manifest["manifest_sha256"] = _hash_without(manifest, "manifest_sha256")
    _write_json(root / "gold-review-revision.json", manifest)
    result.update({
        "portable_gold_review": True,
        "gold_review_revision": str((root / "gold-review-revision.json").resolve()),
        "gold_decisions": str((root / "gold-decisions.jsonl").resolve()),
        "assurance_tier": manifest["assurance_tier"],
        "comparison_receipt_sha256": comparison["receipt_sha256"],
    })
    return result


def validate_portable_gold_review_revision(
    revision: Path,
    workbench: Path,
    workpack: Path,
    reviewer_instance: Path,
    reviewer_submissions: Sequence[Path],
    gold_review_submission: Path,
    comparison_receipt: Path,
) -> dict[str, Any]:
    comparison_path = _secure_file(Path(comparison_receipt).expanduser(), "comparison_receipt")
    validate_comparison_receipt(comparison_path, workpack, reviewer_instance, reviewer_submissions)
    comparison = _read_json(comparison_path, "comparison_receipt")
    base = validate_adjudication_revision(
        revision,
        workbench,
        workpack,
        reviewer_instance,
        reviewer_submissions,
        gold_review_submission,
        comparison_receipt=comparison_path,
    )
    root = _secure_directory(Path(revision).expanduser())
    receipt = _read_json(root / "adjudication-receipt.json", "adjudication_receipt")
    manifest = _read_json(root / "gold-review-revision.json", "portable_gold_review_revision")
    if manifest.get("schema_version") != PORTABLE_GOLD_REVIEW_REVISION_SCHEMA or manifest.get("protocol") != PORTABLE_GOLD_REVIEW_PROTOCOL or manifest.get("compiler_version") != PORTABLE_GOLD_REVIEW_COMPILER_VERSION:
        raise ReviewerWorkbenchError("portable_gold_review_revision_schema_invalid")
    if manifest.get("manifest_sha256") != _hash_without(manifest, "manifest_sha256"):
        raise ReviewerWorkbenchError("portable_gold_review_revision_hash_mismatch")
    if manifest.get("comparison_receipt_sha256") != comparison.get("receipt_sha256") or manifest.get("adjudication_receipt_sha256") != receipt.get("receipt_sha256") or manifest.get("dispute_set_sha256") != receipt.get("dispute_set_sha256"):
        raise ReviewerWorkbenchError("portable_gold_review_revision_binding_invalid")
    decisions_path = _secure_file(root / "gold-decisions.jsonl", "portable_gold_decisions")
    decisions = _read_jsonl(decisions_path, "portable_gold_decisions")
    expected_decisions = _portable_gold_decision_rows(receipt, str(comparison["receipt_sha256"]))
    if not _json_exact_equal(decisions, expected_decisions):
        raise ReviewerWorkbenchError("portable_gold_decisions_binding_invalid")
    for relative, expected_hash in (manifest.get("file_hashes") or {}).items():
        if sha256_file(_relative_instance_file(root, relative, "portable_gold_review_file")) != expected_hash:
            raise ReviewerWorkbenchError(f"portable_gold_review_file_hash_mismatch:{relative}")
    if manifest.get("status") != base["finalization_status"] or manifest.get("import_compatible") is not base["import_compatible"] or manifest.get("gold_row_count") != base["gold_row_count"] or manifest.get("assurance_tier") != "dual-source-adjudicated":
        raise ReviewerWorkbenchError("portable_gold_review_revision_status_invalid")
    if manifest.get("verified_gold") is not False or manifest.get("promotion_allowed") is not False or manifest.get("release_included") is not False or manifest.get("immutable") is not True:
        raise ReviewerWorkbenchError("portable_gold_review_revision_policy_invalid")
    return {**base, "portable_gold_review": True, "gold_review_revision": str((root / "gold-review-revision.json").resolve()), "gold_decision_count": len(decisions), "assurance_tier": manifest["assurance_tier"], "comparison_receipt_sha256": comparison["receipt_sha256"]}


def _finalization_status(issues: Sequence[str]) -> str:
    # Missing coverage is a recoverable human-review pause.  Integrity,
    # independence, and identity failures remain rejected so the operator
    # can distinguish a wait-for-review state from a tainted submission.
    if any(code.startswith(("submission_", "review_submission_", "declaration_missing_or_false", "duplicate_reviewer", "reviewer_not_independent", "proposer_overlap", "evaluator_overlap", "split_", "hash_", "gold_identity_")) for code in issues):
        return "rejected"
    return "paused" if issues else "accepted-for-import"


def finalize_revision(instance: Path, workpack: Path, submissions: Sequence[Path], output: Path, *, revision: int = 1) -> dict[str, Any]:
    if revision < 1:
        raise ReviewerWorkbenchError("revision_invalid")
    instance = Path(instance).expanduser()
    workpack = Path(workpack).expanduser().resolve()
    instance_result = validate_instance(instance, workpack)
    facts = _load_review_workpack(workpack)
    identity, _ = _canonical_identity(facts)
    if not submissions:
        raise ReviewerWorkbenchError("review_submission_required")
    parsed = [_load_submission(Path(path), identity, facts) for path in submissions]
    issues: list[str] = sorted({issue for row in parsed for issue in row["content_issues"]})
    reviewer_ids = [str(row["reviewer_instance"]) for row in parsed]
    if len(set(reviewer_ids)) != len(reviewer_ids):
        issues.append("duplicate_reviewer_instance")
    proposer_instances = set(str(value) for value in facts["review_plan"].get("proposer_instances", []))
    if proposer_instances & set(reviewer_ids):
        issues.append("reviewer_not_independent")
    if not all(row["declarations"].get("not_evaluator") is True for row in parsed):
        issues.append("evaluator_overlap")
    expected_ids = set(str(value) for value in facts["candidate_ids"])
    expected_count = len(expected_ids)
    ready_rows: list[dict[str, Any]] = []
    all_attestations: list[dict[str, Any]] = []
    for item_id in sorted(expected_ids):
        item = facts["candidate_by_id"][item_id]
        task = facts["task_by_item"][item_id]
        item_reviews = [row for row in parsed if item_id in row["items"]]
        required = int(task["required_independent_reviewers"])
        if len(item_reviews) < required:
            issues.append(f"reviewer_count_invalid:{item_id}:{len(item_reviews)}:{required}")
            continue
        if len(item_reviews) > required and item["kind_candidate"] in {"table", "chart"}:
            issues.append(f"reviewer_count_invalid:{item_id}:{len(item_reviews)}:{required}")
            continue
        # Presence/localization tasks need at least one reviewer.  When two
        # people independently inspect every item, retain one deterministic
        # reviewer for figure/equation Gold rows; table/chart rows still use
        # exactly the two required reviewers.
        item_reviews = sorted(item_reviews, key=lambda row: str(row["reviewer_instance"]))[:required]
        reviews = [row["items"][item_id] for row in item_reviews]
        if any(_review_content_issues(item_id, str(item["kind_candidate"]), review["review"], item) for review in reviews):
            # The per-submission issues are already present; keep a stable
            # item-level code for the finalization manifest.
            issues.append(f"item_not_complete:{item_id}")
            continue
        signatures = [sha256_json(_scorable_payload(str(item["kind_candidate"]), review["review"], item)) for review in reviews]
        if len(set(signatures)) != 1:
            issues.append(f"needs_adjudication:reviewer_disagreement:{item_id}")
            continue
        attestations = [_make_attestation(item, task, item_reviews[index], reviews[index]["review"], sorted(proposer_instances)[0] if proposer_instances else "phase7d5a-preparation-agent") for index in range(len(reviews))]
        all_attestations.extend(attestations)
        payload = _payload_with_audit(str(item["kind_candidate"]), reviews[0]["review"], item)
        payload["independent_reviewer_count"] = required
        payload["reviewer_agreement"] = "exact-scorable-agreement"
        registry = [{"reviewer_instance": row["reviewer_instance"], "reviewer_role": "external-independent", "independent_from_proposer": row["declarations"].get("independent_from_proposer") is True} for row in item_reviews]
        row = {
            "schema_version": VISUAL_BENCHMARK_GOLD_FRAGMENT_SCHEMA,
            "protocol": VISUAL_BENCHMARK_PROTOCOL,
            "item_id": item_id,
            "item_sha256": item["item_sha256"],
            "review_task_id": task["review_task_id"],
            "kind_candidate": item["kind_candidate"],
            "split": "calibration" if item_id in facts["calibration_ids"] else "test",
            "input_hashes": _gold_input_hashes(item, task),
            "gold_payload": payload,
            "reviewer_registry": registry,
            "attestations": attestations,
            "fragment_provenance": {"source_type": "external-independent-review-file", "created_outside_preparation_agent": True, "compiler_authored": False, "evaluator_role_separate": True},
            "independent_real_gold": True,
            "verified_gold": False,
            "promotion_allowed": False,
        }
        row["reviewer_registry_sha256"] = sha256_json(registry)
        row["gold_payload_sha256"] = sha256_json(payload)
        row["fragment_sha256"] = _fragment_hash(row)
        ready_rows.append(row)
    issues = sorted(set(issues))
    final_status = _finalization_status(issues)
    import_compatible = final_status == "accepted-for-import" and len(ready_rows) == expected_count
    destination = _secure_directory(Path(output).expanduser(), must_be_empty=True)
    registry = _reviewer_registry(parsed, identity, expected_ids)
    _write_json(destination / "reviewer-registry.json", registry)
    declarations_by_reviewer = [{"reviewer_instance": row["reviewer_instance"], "declarations": row["declarations"]} for row in sorted(parsed, key=lambda value: str(value["reviewer_instance"]))]
    attestation_bundle = {
        "schema_version": VISUAL_REVIEW_WORKBENCH_ATTESTATION_BUNDLE_SCHEMA,
        "protocol": VISUAL_REVIEW_WORKBENCH_PROTOCOL,
        "workpack_identity": _submission_identity(identity),
        "coverage": {"expected_item_count": expected_count, "reviewed_item_count": len({att["input_hashes"]["item_sha256"] for att in all_attestations}), "item_ids_sha256": sha256_json(sorted({att["input_hashes"]["item_sha256"] for att in all_attestations}))},
        "declarations": declarations_by_reviewer,
        "attestations": all_attestations,
        "compiler_authored": False,
        "evaluator_role_separate": True,
    }
    attestation_bundle["attestation_bundle_sha256"] = _hash_without(attestation_bundle, "attestation_bundle_sha256")
    _write_json(destination / "review-attestation.json", attestation_bundle)
    gold_rows = ready_rows if import_compatible else []
    _write_jsonl(destination / "gold-fragments.jsonl", gold_rows)
    if not import_compatible:
        provisional = []
        for submission in parsed:
            for item_id, row in sorted(submission["items"].items()):
                provisional.append({"schema_version": "tkc.visual-review-workbench-provisional-fragment/v0.1", "item_id": item_id, "reviewer_instance": submission["reviewer_instance"], "item_sha256": row["item_sha256"], "review": row["review"], "release_included": False})
        _write_jsonl(destination / "provisional-review-fragments.jsonl", provisional)
    submission_hashes = sorted(str(row["file_sha256"]) for row in parsed)
    coverage = {"expected_item_count": expected_count, "covered_item_count": len({item_id for row in parsed for item_id in row["items"]}), "gold_row_count": len(gold_rows), "attestation_count": len(all_attestations), "item_ids_sha256": sha256_json(sorted(expected_ids)), "complete_all_items": len(gold_rows) == expected_count, "complete_31_of_31": len(gold_rows) == expected_count}
    revision_id = "review-revision-" + sha256_json({"workpack_sha256": identity["workpack_sha256"], "submission_hashes": submission_hashes, "revision": revision, "status": final_status, "issues": issues})[:32]
    output_files = ["reviewer-registry.json", "review-attestation.json", "gold-fragments.jsonl"]
    if (destination / "provisional-review-fragments.jsonl").is_file():
        output_files.append("provisional-review-fragments.jsonl")
    file_hashes = {relative: sha256_file(destination / relative) for relative in output_files}
    manifest = {
        "schema_version": VISUAL_REVIEW_WORKBENCH_REVIEW_MANIFEST_SCHEMA,
        "protocol": VISUAL_REVIEW_WORKBENCH_PROTOCOL,
        "compiler_version": VISUAL_REVIEW_WORKBENCH_COMPILER_VERSION,
        "revision_id": revision_id,
        "revision": revision,
        "workpack_identity": identity,
        "coverage": coverage,
        "reviewer_instances": reviewer_ids,
        "submission_file_sha256s": submission_hashes,
        "issues": issues,
        "finalization_status": final_status,
        "import_compatible": import_compatible,
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
        "immutable": True,
        "canonical_workpack_replayed": True,
        "model_invocations": 0,
        "network_access": False,
        "mcp_calls": 0,
        "file_hashes": file_hashes,
    }
    manifest["manifest_sha256"] = _hash_without(manifest, "manifest_sha256")
    _write_json(destination / "review-manifest.json", manifest)
    result = {"status": final_status, "revision": str(destination.resolve()), "review_manifest": str((destination / "review-manifest.json").resolve()), "gold_fragments": str((destination / "gold-fragments.jsonl").resolve()), "coverage": coverage, "issues": issues, "import_compatible": import_compatible, "verified_gold": False, "promotion_allowed": False, "release_included": False, "immutable": True, "workpack_sha256": identity["workpack_sha256"]}
    return result


def validate_revision(revision: Path, workpack: Path) -> dict[str, Any]:
    root = _secure_directory(Path(revision).expanduser())
    facts = _load_review_workpack(Path(workpack).expanduser().resolve())
    identity, _ = _canonical_identity(facts)
    expected_count = len(facts["candidate_ids"])
    manifest = _read_json(root / "review-manifest.json", "review_manifest")
    if manifest.get("schema_version") != VISUAL_REVIEW_WORKBENCH_REVIEW_MANIFEST_SCHEMA or manifest.get("protocol") != VISUAL_REVIEW_WORKBENCH_PROTOCOL:
        raise ReviewerWorkbenchError("review_manifest_schema_invalid")
    if manifest.get("manifest_sha256") != _hash_without(manifest, "manifest_sha256"):
        raise ReviewerWorkbenchError("review_manifest_hash_mismatch")
    if manifest.get("workpack_identity") != identity or manifest.get("canonical_workpack_replayed") is not True:
        raise ReviewerWorkbenchError("review_manifest_workpack_identity_invalid")
    if manifest.get("verified_gold") is not False or manifest.get("promotion_allowed") is not False or manifest.get("release_included") is not False or manifest.get("immutable") is not True:
        raise ReviewerWorkbenchError("review_manifest_policy_invalid")
    for relative, expected in (manifest.get("file_hashes") or {}).items():
        path = _relative_instance_file(root, relative, "review_result")
        if sha256_file(path) != expected:
            raise ReviewerWorkbenchError(f"review_result_hash_mismatch:{relative}")
    gold_path = _secure_file(root / "gold-fragments.jsonl", "gold_fragments")
    gold_rows = _read_jsonl(gold_path, "gold_fragments")
    coverage = manifest.get("coverage")
    if not isinstance(coverage, Mapping) or type(coverage.get("gold_row_count")) is not int or coverage.get("gold_row_count") != len(gold_rows):
        raise ReviewerWorkbenchError("review_coverage_gold_row_count_mismatch")
    if not isinstance(coverage, Mapping) or coverage.get("complete_31_of_31") is not (len(gold_rows) == expected_count):
        raise ReviewerWorkbenchError("review_coverage_complete_31_of_31_mismatch")
    if "complete_all_items" in coverage and coverage.get("complete_all_items") is not (len(gold_rows) == expected_count):
        raise ReviewerWorkbenchError("review_coverage_complete_all_items_mismatch")
    if manifest.get("import_compatible") is True:
        if not gold_rows:
            raise ReviewerWorkbenchError("import_compatible_gold_empty")
        try:
            external = validate_external_gold_fragments(facts, [gold_path])
        except (VisualBenchmarkError, OSError, ValueError) as error:
            raise ReviewerWorkbenchError(f"gold_import_compatibility_invalid:{error}") from error
        if external.get("item_count") != expected_count or external.get("verified_gold") is not False:
            raise ReviewerWorkbenchError("gold_import_compatibility_policy_invalid")
    elif gold_rows:
        raise ReviewerWorkbenchError("paused_revision_gold_file_must_be_empty")
    registry = _read_json(root / "reviewer-registry.json", "reviewer_registry")
    if registry.get("registry_sha256") != _hash_without(registry, "registry_sha256"):
        raise ReviewerWorkbenchError("reviewer_registry_hash_mismatch")
    bundle = _read_json(root / "review-attestation.json", "review_attestation")
    if bundle.get("attestation_bundle_sha256") != _hash_without(bundle, "attestation_bundle_sha256"):
        raise ReviewerWorkbenchError("review_attestation_hash_mismatch")
    if any(path.suffix.casefold() == ".pdf" or path.is_symlink() for path in root.rglob("*")):
        raise ReviewerWorkbenchError("review_result_source_or_symlink_leak")
    return {"status": "verified", "finalization_status": manifest["finalization_status"], "revision": str(root.resolve()), "item_count": manifest["coverage"]["expected_item_count"], "covered_item_count": manifest["coverage"]["covered_item_count"], "import_compatible": manifest["import_compatible"], "verified_gold": False, "promotion_allowed": False, "release_included": False, "immutable": True, "network_access": False, "model_invocations": 0, "mcp_calls": 0}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-single-page", help="prepare a variable-size, no-region visual review workpack from a frozen native visual bundle")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--visual-bundle", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--pdftoppm", type=Path, required=True)
    prepare.add_argument("--book-id", required=True)
    prepare.add_argument("--created-at", default="2026-08-29T00:00:00+08:00")
    generate = subparsers.add_parser("generate", help="generate a blank private reviewer instance")
    generate.add_argument("--workpack", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--reviewer-id", required=True)
    generate.add_argument("--review-session-id")
    generate.add_argument("--created-at", default="2026-08-24T00:00:00+08:00")
    validate = subparsers.add_parser("validate", help="validate an instance or immutable finalization revision")
    validate.add_argument("--instance", type=Path)
    validate.add_argument("--revision", type=Path)
    validate.add_argument("--workpack", type=Path, required=True)
    finalize = subparsers.add_parser("finalize", help="finalize external form submissions into an import-compatible revision when complete")
    finalize.add_argument("--instance", type=Path, required=True)
    finalize.add_argument("--workpack", type=Path, required=True)
    finalize.add_argument("--submission", type=Path, action="append", required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.add_argument("--revision", type=int, default=1)
    adjudication_plan = subparsers.add_parser("adjudication-plan", help="freeze the current reviewer disagreement set; without identity only a pending private plan is written")
    adjudication_plan.add_argument("--workpack", type=Path, required=True)
    adjudication_plan.add_argument("--reviewer-instance", type=Path, required=True)
    adjudication_plan.add_argument("--submission", type=Path, action="append", required=True)
    adjudication_plan.add_argument("--output", type=Path)
    adjudication_plan.add_argument("--adjudicator-id")
    adjudication_plan.add_argument("--adjudication-session-id")
    adjudication_plan.add_argument("--comparison-receipt", type=Path)
    adjudication_plan.add_argument("--created-at", default="2026-08-26T00:00:00+08:00")
    adjudication_validate = subparsers.add_parser("adjudication-validate", help="validate a private adjudication workbench against its original reviewer submissions")
    adjudication_validate.add_argument("--workbench", type=Path, required=True)
    adjudication_validate.add_argument("--workpack", type=Path, required=True)
    adjudication_validate.add_argument("--reviewer-instance", type=Path, required=True)
    adjudication_validate.add_argument("--submission", type=Path, action="append", required=True)
    adjudication_validate.add_argument("--comparison-receipt", type=Path)
    adjudication_finalize = subparsers.add_parser("adjudication-finalize", help="finalize one independent adjudication submission into a private revision")
    adjudication_finalize.add_argument("--workbench", type=Path, required=True)
    adjudication_finalize.add_argument("--workpack", type=Path, required=True)
    adjudication_finalize.add_argument("--reviewer-instance", type=Path, required=True)
    adjudication_finalize.add_argument("--reviewer-submission", type=Path, action="append", required=True)
    adjudication_finalize.add_argument("--adjudication-submission", type=Path, required=True)
    adjudication_finalize.add_argument("--output", type=Path, required=True)
    adjudication_finalize.add_argument("--revision", type=int, default=1)
    adjudication_finalize.add_argument("--comparison-receipt", type=Path)
    adjudication_revision_validate = subparsers.add_parser("adjudication-revision-validate", help="replay and validate a private adjudication revision")
    adjudication_revision_validate.add_argument("--revision", type=Path, required=True)
    adjudication_revision_validate.add_argument("--workbench", type=Path, required=True)
    adjudication_revision_validate.add_argument("--workpack", type=Path, required=True)
    adjudication_revision_validate.add_argument("--reviewer-instance", type=Path, required=True)
    adjudication_revision_validate.add_argument("--reviewer-submission", type=Path, action="append", required=True)
    adjudication_revision_validate.add_argument("--adjudication-submission", type=Path, required=True)
    adjudication_revision_validate.add_argument("--comparison-receipt", type=Path)
    gold_review_generate = subparsers.add_parser("gold-review-generate", help="generate a portable Gold Curator workbench bound to two reviews and a verified profile comparison receipt")
    gold_review_generate.add_argument("--workpack", type=Path, required=True)
    gold_review_generate.add_argument("--reviewer-instance", type=Path, required=True)
    gold_review_generate.add_argument("--submission", type=Path, action="append", required=True)
    gold_review_generate.add_argument("--comparison-receipt", type=Path, required=True)
    gold_review_generate.add_argument("--output", type=Path, required=True)
    gold_review_generate.add_argument("--curator-id", required=True)
    gold_review_generate.add_argument("--curator-session-id", required=True)
    gold_review_generate.add_argument("--created-at", default="2026-08-27T00:00:00+08:00")
    gold_review_validate = subparsers.add_parser("gold-review-validate", help="validate a portable Gold Curator workbench and all bound inputs")
    gold_review_validate.add_argument("--workbench", type=Path, required=True)
    gold_review_validate.add_argument("--workpack", type=Path, required=True)
    gold_review_validate.add_argument("--reviewer-instance", type=Path, required=True)
    gold_review_validate.add_argument("--submission", type=Path, action="append", required=True)
    gold_review_validate.add_argument("--comparison-receipt", type=Path, required=True)
    gold_review_finalize = subparsers.add_parser("gold-review-finalize", help="finalize one human Gold Review submission into an immutable private revision")
    gold_review_finalize.add_argument("--workbench", type=Path, required=True)
    gold_review_finalize.add_argument("--workpack", type=Path, required=True)
    gold_review_finalize.add_argument("--reviewer-instance", type=Path, required=True)
    gold_review_finalize.add_argument("--reviewer-submission", type=Path, action="append", required=True)
    gold_review_finalize.add_argument("--gold-review-submission", type=Path, required=True)
    gold_review_finalize.add_argument("--comparison-receipt", type=Path, required=True)
    gold_review_finalize.add_argument("--output", type=Path, required=True)
    gold_review_finalize.add_argument("--revision", type=int, default=1)
    gold_review_revision_validate = subparsers.add_parser("gold-review-revision-validate", help="replay and validate a portable Gold Review revision")
    gold_review_revision_validate.add_argument("--revision", type=Path, required=True)
    gold_review_revision_validate.add_argument("--workbench", type=Path, required=True)
    gold_review_revision_validate.add_argument("--workpack", type=Path, required=True)
    gold_review_revision_validate.add_argument("--reviewer-instance", type=Path, required=True)
    gold_review_revision_validate.add_argument("--reviewer-submission", type=Path, action="append", required=True)
    gold_review_revision_validate.add_argument("--gold-review-submission", type=Path, required=True)
    gold_review_revision_validate.add_argument("--comparison-receipt", type=Path, required=True)
    comparison = subparsers.add_parser("review-convention-compare", help="compare two reviewer submissions with the frozen R2 profile; this only emits a private pause receipt")
    comparison.add_argument("--workpack", type=Path, required=True)
    comparison.add_argument("--reviewer-instance", type=Path, required=True)
    comparison.add_argument("--submission", type=Path, action="append", required=True)
    comparison.add_argument("--output", type=Path, required=True)
    comparison.add_argument("--created-at", default="2026-08-27T00:00:00+08:00")
    comparison_validate = subparsers.add_parser("review-convention-validate", help="validate a profile-aware comparison receipt and replay its bindings")
    comparison_validate.add_argument("--receipt", type=Path, required=True)
    comparison_validate.add_argument("--workpack", type=Path, required=True)
    comparison_validate.add_argument("--reviewer-instance", type=Path, required=True)
    comparison_validate.add_argument("--submission", type=Path, action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare-single-page":
            result = prepare_single_page_visual_review_workpack(args.source, args.visual_bundle, args.output, pdftoppm=args.pdftoppm, book_id=args.book_id, created_at=args.created_at)
        elif args.command == "generate":
            result = generate_instance(args.workpack, args.output, reviewer_id=args.reviewer_id, session_id=args.review_session_id, created_at=args.created_at)
        elif args.command == "validate":
            if bool(args.instance) == bool(args.revision):
                raise ReviewerWorkbenchError("validate_requires_exactly_one_instance_or_revision")
            result = validate_instance(args.instance, args.workpack) if args.instance else validate_revision(args.revision, args.workpack)
        elif args.command == "finalize":
            result = finalize_revision(args.instance, args.workpack, args.submission, args.output, revision=args.revision)
        elif args.command == "adjudication-plan":
            result = plan_adjudication_disputes(args.workpack, args.reviewer_instance, args.submission, args.output, adjudicator_id=args.adjudicator_id, session_id=args.adjudication_session_id, created_at=args.created_at, comparison_receipt=args.comparison_receipt)
        elif args.command == "adjudication-validate":
            result = validate_adjudication_workbench(args.workbench, args.workpack, args.reviewer_instance, args.submission, comparison_receipt=args.comparison_receipt)
        elif args.command == "adjudication-finalize":
            result = finalize_adjudication_revision(args.workbench, args.workpack, args.reviewer_instance, args.reviewer_submission, args.adjudication_submission, args.output, revision=args.revision, comparison_receipt=args.comparison_receipt)
        elif args.command == "adjudication-revision-validate":
            result = validate_adjudication_revision(args.revision, args.workbench, args.workpack, args.reviewer_instance, args.reviewer_submission, args.adjudication_submission, comparison_receipt=args.comparison_receipt)
        elif args.command == "gold-review-generate":
            result = generate_adjudication_workbench(args.workpack, args.reviewer_instance, args.submission, args.output, adjudicator_id=args.curator_id, session_id=args.curator_session_id, created_at=args.created_at, comparison_receipt=args.comparison_receipt, portable_gold_review=True)
        elif args.command == "gold-review-validate":
            result = validate_adjudication_workbench(args.workbench, args.workpack, args.reviewer_instance, args.submission, comparison_receipt=args.comparison_receipt)
            result["portable_gold_review"] = True
        elif args.command == "gold-review-finalize":
            result = finalize_portable_gold_review(args.workbench, args.workpack, args.reviewer_instance, args.reviewer_submission, args.gold_review_submission, args.comparison_receipt, args.output, revision=args.revision)
        elif args.command == "gold-review-revision-validate":
            result = validate_portable_gold_review_revision(args.revision, args.workbench, args.workpack, args.reviewer_instance, args.reviewer_submission, args.gold_review_submission, args.comparison_receipt)
        elif args.command == "review-convention-compare":
            result = compare_submissions_with_profile(args.workpack, args.reviewer_instance, args.submission, args.output, created_at=args.created_at)
        elif args.command == "review-convention-validate":
            result = validate_comparison_receipt(args.receipt, args.workpack, args.reviewer_instance, args.submission)
        else:
            raise ReviewerWorkbenchError("command_invalid")
    except (ReviewerWorkbenchError, VisualBenchmarkError, OSError, ValueError) as error:
        print(json.dumps({"status": "rejected", "error": str(error)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "EXPECTED_ITEM_COUNT",
    "ReviewerWorkbenchError",
    "generate_instance",
    "prepare_single_page_visual_review_workpack",
    "load_review_convention_profile",
    "review_convention_profile_identity",
    "bbox_iou",
    "bbox_equivalence",
    "compare_submissions_with_profile",
    "compare_review_submissions_with_profile",
    "validate_comparison_receipt",
    "generate_adjudication_workbench",
    "plan_adjudication_disputes",
    "parse_table_paste",
    "table_cells_from_paste",
    "validate_instance",
    "validate_adjudication_workbench",
    "finalize_portable_gold_review",
    "finalize_revision",
    "finalize_adjudication_revision",
    "validate_revision",
    "validate_adjudication_revision",
    "validate_portable_gold_review_revision",
    "main",
]


REVIEW_HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>Independent Visual Review Workbench</title>
<style>
:root { color-scheme: light; --ink:#19212a; --muted:#64717d; --line:#d7dee5; --soft:#f4f7f9; --accent:#075985; --warn:#9a3412; --ok:#166534; }
* { box-sizing: border-box; }
body { margin:0; color:var(--ink); background:#eef2f5; font:14px/1.45 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Segoe UI",sans-serif; }
header { position:sticky; top:0; z-index:5; padding:14px 20px; background:#fff; border-bottom:1px solid var(--line); display:flex; gap:18px; align-items:center; flex-wrap:wrap; }
h1 { font-size:18px; margin:0; letter-spacing:.02em; }
.identity { color:var(--muted); font-size:12px; }
.shell { display:grid; grid-template-columns:minmax(180px,240px) minmax(0,1fr) minmax(300px,420px); gap:14px; padding:14px; max-width:1800px; margin:auto; }
.panel { background:#fff; border:1px solid var(--line); border-radius:10px; box-shadow:0 2px 8px rgba(20,35,50,.05); }
.side { padding:12px; align-self:start; position:sticky; top:78px; max-height:calc(100vh - 94px); overflow:auto; }
.side h2, .review h2, .form h2 { font-size:14px; margin:0 0 10px; }
.incomplete { border-top:1px solid var(--line); margin-top:14px; padding-top:12px; }
.progress { height:8px; background:#e5e7eb; border-radius:99px; overflow:hidden; margin:8px 0 12px; }
.progress span { display:block; height:100%; background:var(--accent); width:0; transition:width .15s ease; }
.item-list { display:grid; gap:5px; }
.item-list button { text-align:left; border:1px solid transparent; background:transparent; border-radius:7px; padding:7px 8px; cursor:pointer; color:var(--ink); }
.item-list button:hover, .item-list button.active { background:#e0f2fe; border-color:#7dd3fc; }
.item-list button.done { color:var(--ok); }
.item-list small { display:block; color:var(--muted); }
.review { padding:14px; min-width:0; }
.meta { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin-bottom:12px; }
.fact { padding:8px; background:var(--soft); border:1px solid var(--line); border-radius:7px; min-width:0; }
.fact label { display:block; color:var(--muted); font-size:11px; }
.fact div { overflow-wrap:anywhere; font-weight:600; }
.fact.bbox-fact { grid-column:1/-1; }
.visuals { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(220px,.8fr); gap:10px; }
.visual { border:1px solid var(--line); border-radius:8px; padding:8px; background:#fbfcfd; }
.visual h3 { font-size:12px; color:var(--muted); margin:0 0 6px; }
.context-toolbar { display:flex; align-items:center; flex-wrap:wrap; gap:6px; margin:0 0 7px; }
.context-toolbar .action { padding:6px 8px; font-size:12px; }
.context-mode[aria-pressed="true"] { box-shadow:0 0 0 2px #f59e0b inset; font-weight:700; }
.context-mode-readout,.zoom-readout { color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
.context-viewport { position:relative; width:100%; height:430px; overflow:auto; background:#fff; border:1px solid #e7ebef; overscroll-behavior:contain; touch-action:none; user-select:none; }
.context-viewport.mode-select { cursor:crosshair; }
.context-viewport.mode-pan { cursor:grab; }
.context-viewport.mode-pan:active { cursor:grabbing; }
.context-space { position:relative; min-width:100%; min-height:100%; }
.context-stage { position:absolute; left:0; top:0; overflow:hidden; background:#fff; border:0; transform-origin:top left; user-select:none; }
.context-stage img { display:block; width:100%; height:100%; object-fit:fill; border:0; cursor:inherit; user-select:none; -webkit-user-drag:none; }
.context-stage canvas { position:absolute; inset:0; width:100%; height:100%; cursor:inherit; touch-action:none; user-select:none; }
.visual > img { display:block; width:100%; height:430px; object-fit:contain; background:#fff; border:1px solid #e7ebef; user-select:none; -webkit-user-drag:none; }
.bbox-tools { margin-top:8px; padding:8px; border:1px solid var(--line); border-radius:7px; background:#f8fafc; }
.bbox-readout { min-height:22px; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
.bbox-legend { display:flex; flex-wrap:wrap; gap:10px; margin-top:6px; color:var(--muted); font-size:12px; }
.swatch { display:inline-block; width:18px; height:10px; margin-right:4px; vertical-align:middle; border:2px solid currentColor; }
.swatch.candidate { color:#d97706; border-style:dashed; }
.swatch.corrected { color:#15803d; }
.swatch.selected { color:#7c3aed; border-style:dashed; }
.mapping-unavailable { color:var(--warn); font-weight:600; }
.mapping-verified { color:var(--ok); font-weight:600; }
.bbox-review { padding:8px; border:1px solid #bae6fd; border-radius:7px; background:#f0f9ff; }
.form { padding:14px; align-self:start; }
.field-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px; }
.field { display:grid; gap:4px; min-width:0; }
.field.full { grid-column:1/-1; }
label { font-size:12px; color:#40505c; }
input, select, textarea { width:100%; border:1px solid #bec9d2; border-radius:6px; padding:7px 8px; background:#fff; color:var(--ink); font:inherit; }
textarea { min-height:68px; resize:vertical; }
input:focus, select:focus, textarea:focus { outline:2px solid #bae6fd; border-color:#0284c7; }
.section { border-top:1px solid var(--line); margin-top:14px; padding-top:12px; }
.checks { display:grid; gap:7px; }
.checks label { display:flex; gap:8px; align-items:flex-start; color:var(--ink); }
.checks input { width:auto; margin-top:3px; }
.hint { font-size:12px; color:var(--muted); }
.warning { color:var(--warn); }
.actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }
button.action { border:1px solid #0c4a6e; background:#075985; color:#fff; border-radius:7px; padding:8px 11px; cursor:pointer; font:inherit; }
button.action.secondary { color:#075985; background:#fff; }
button.action.danger { border-color:#9a3412; background:#9a3412; }
button.action:disabled { opacity:.45; cursor:not-allowed; }
#save-state { min-height:20px; margin-top:8px; color:var(--ok); }
.cell-grid { display:grid; gap:4px; overflow:auto; padding:6px; background:var(--soft); border:1px solid var(--line); border-radius:6px; }
.cell-grid input { min-width:90px; }
.declaration-box { background:#fffbeb; border:1px solid #fcd34d; border-radius:8px; padding:10px; margin-bottom:12px; }
.declaration-box h2 { color:#92400e; }
.export-box { background:#eff6ff; border:1px solid #93c5fd; border-radius:8px; padding:10px; margin-top:12px; }
.import-box { background:#f0fdf4; border:1px solid #86efac; border-radius:8px; padding:10px; margin-top:12px; }
.profile-banner { flex-basis:100%; color:#075985; background:#eff6ff; border:1px solid #93c5fd; border-radius:7px; padding:7px 9px; font-size:12px; }
.profile-banner code { overflow-wrap:anywhere; }
.finalize-issues { margin-top:10px; padding:8px; border:1px solid #fdba74; border-radius:7px; background:#fff7ed; }
.finalize-issues ul { margin:6px 0 0 18px; padding:0; }
.finalize-issues button { border:0; padding:0; background:transparent; color:#9a3412; text-decoration:underline; cursor:pointer; font:inherit; text-align:left; }
@media (min-width:1101px) { .form { max-height:calc(200vh - 188px); overflow-y:auto; overscroll-behavior:contain; scrollbar-gutter:stable; } }
@media (max-width:1100px) { .shell { grid-template-columns:200px minmax(0,1fr); } .form { grid-column:2; max-height:none; overflow:visible; overscroll-behavior:auto; scrollbar-gutter:auto; } .visuals { grid-template-columns:1fr; } }
@media (max-width:760px) { .shell { display:block; padding:8px; } .side,.form { position:static; max-height:none; margin-top:8px; overflow:visible; overscroll-behavior:auto; scrollbar-gutter:auto; } .meta { grid-template-columns:repeat(2,minmax(0,1fr)); } .context-viewport,.visual > img { height:310px; } }
</style>
</head>
<body>
<header>
  <h1>独立视觉 Review Workbench</h1>
  <div class="identity">审阅人（reviewer）：<span id="reviewer-id"></span> · 本地离线 · 仅查看冻结的 Context（页面上下文）/Crop（候选裁剪）</div>
  <div class="profile-banner" id="profile-banner"><strong>Review Convention Profile</strong>：<span id="profile-version">__PROFILE_VERSION__</span> · SHA-256 <code id="profile-hash">__PROFILE_HASH__</code>。开始前确认 profile；改变 profile 会使旧 workbench/submission <span class="warning">profile stale</span>，必须显式 comparison receipt 或 migration 后重新确认受影响字段。</div>
</header>
<main class="shell">
  <aside class="panel side">
    <h2>进度 <span id="progress-text"></span></h2>
    <div class="progress"><span id="progress-bar"></span></div>
    <div class="hint">列表只显示项目事实与填写状态，不显示评测分层。</div>
    <div class="item-list" id="item-list"></div>
    <div class="incomplete">
      <h2>未完成项目 <span id="incomplete-count"></span></h2>
      <div class="item-list" id="incomplete-list"></div>
    </div>
    <div class="actions"><button class="action secondary" id="clear-draft" type="button">清除本实例本地草稿</button></div>
  </aside>
  <section class="panel review">
    <div class="profile-banner"><strong>分页与覆盖边界</strong>：当前页面只审查冻结的公式、图、表或图表候选；不要求人工拆分物理页，不提供 region，也不会修改上游自动分页结果。它确认“已有候选是否正确”，不自动证明未列出页面不存在漏检对象。</div>
    <div class="meta">
      <div class="fact"><label>项目编号（Item ID）</label><div id="item-id"></div></div>
      <div class="fact"><label>候选类型（candidate type）</label><div id="item-kind"></div></div>
      <div class="fact"><label>物理页</label><div id="item-page"></div></div>
      <div class="fact bbox-fact"><label>候选 BBox（Candidate BBox，锁定只读）</label><div id="item-bbox"></div></div>
      <div class="fact bbox-fact"><label>修正 BBox（corrected_bbox，尚未采用时为空）</label><div id="item-corrected-bbox"></div></div>
      <div class="fact"><label>Source SHA-256</label><div id="item-source-hash"></div></div>
      <div class="fact"><label>Render SHA-256</label><div id="item-render-hash"></div></div>
      <div class="fact"><label>Crop SHA-256</label><div id="item-crop-hash"></div></div>
      <div class="fact"><label>BBox 坐标系</label><div id="item-coordinate-system"></div></div>
      <div class="fact"><label>审阅范围（review scope）</label><div id="item-scope"></div></div>
    </div>
    <div class="visuals">
      <div class="visual"><h3>Context / page render（候选框叠加；原始 render 不变）</h3><div class="context-toolbar"><button class="action secondary context-mode" id="select-mode" type="button" aria-pressed="true" title="在页面上拖动采集 corrected_bbox">框选 BBox</button><button class="action secondary context-mode" id="pan-mode" type="button" aria-pressed="false" title="拖动只移动页面视口">平移页面</button><span id="context-mode-readout" class="context-mode-readout">当前模式：框选 BBox</span><span id="zoom-readout" class="zoom-readout">缩放：正在计算…</span><button class="action secondary" id="zoom-out" type="button" title="缩小 Context 页面">缩小 −</button><button class="action secondary" id="zoom-in" type="button" title="放大 Context 页面">放大 +</button><button class="action secondary" id="fit-view" type="button" title="让整页适合当前窗口">适合窗口</button><button class="action secondary" id="zoom-100" type="button" title="恢复到 100%">100%</button><button class="action secondary" id="reset-view" type="button" title="重置为适合窗口并回到框选模式">重置视图</button></div><div class="hint context-viewer-hint">当前模式只影响鼠标拖动：框选 BBox 会创建紫色临时框，平移页面只移动视口；按住 Space 可临时平移，松开后恢复原模式。缩放/平移不会改变 PDF points 或冻结 render。</div><div class="context-viewport mode-select" id="context-viewport"><div class="context-space" id="context-space"><div class="context-stage" id="context-stage"><img id="context-image" alt="Context page render" draggable="false"><canvas id="context-overlay" aria-label="BBox overlay and selection surface"></canvas></div></div></div><div class="bbox-tools"><div id="context-mapping-status" class="hint">坐标映射：正在检查…</div><div id="pointer-readout" class="bbox-readout">鼠标坐标：请移动到 Context 页面内容上。</div><div id="selection-readout" class="bbox-readout">当前框选：未开始。</div><div class="bbox-legend"><span><span class="swatch candidate"></span>橙色虚线：冻结候选 BBox（只读）</span><span><span class="swatch corrected"></span>绿色实线：已采用 corrected_bbox</span><span><span class="swatch selected"></span>紫色虚线：当前未采用框选</span></div><div class="hint">坐标映射不可验证时仍可查看、缩放、平移，但不能框选修正。清晰框选后点击“采用为修正 BBox”；Esc 或“清除当前框选”只清除紫色临时框，“重置视图”不会清除已采用的绿色修正框。</div><div class="actions"><button class="action secondary" id="clear-selection" type="button">清除当前框选</button><button class="action" id="adopt-corrected" type="button" disabled>采用为修正 BBox</button><button class="action secondary" id="reset-corrected" type="button">重置修正 BBox</button></div></div></div>
      <div class="visual"><h3>Crop（候选裁剪，只读）</h3><img id="crop-image" alt="Candidate crop" draggable="false"></div>
    </div>
    <div class="actions">
        <button class="action secondary" id="previous" type="button">上一项 Previous</button>
        <button class="action" id="save" type="button">保存草稿 Save</button>
        <button class="action" id="next" type="button">下一项 Next</button>
    </div>
    <div id="save-state" aria-live="polite"></div>
  </section>
  <section class="panel form">
    <h2>人工填写</h2>
    <div class="profile-banner"><strong>本实例字段规则</strong>：Caption 规范内容不含前导编号，table_number/figure_number 单独填写；Table BBox 包含表体、表头和边框，排除外部编号、Caption、脚注和上下文正文；Chart BBox 包含绘图区、轴、刻度、轴标签、legend、图内标题，排除外部 Caption/figure number；Figure BBox 包含图像主体及图内标注，排除外部 Caption/figure number。raw transcription 原样保留，canonical comparison 仅允许 NFC、换行统一、每行首尾空白裁剪和可证明的编号拆分；不允许 NFKC、大小写折叠、技术标识符/公式/单位模糊替换或把 <code>2</code> 与 <code>2.0</code> 当作同一 raw 文本。</div>
    <div class="profile-banner"><strong>Equation / 自动规范化边界</strong>：Equation BBox 仅保守包含展示公式主体及确实属于公式对象的可见符号/编号，排除周围正文和外部 Caption/公式编号；不得自动平均、union、intersection 或自动纠正字符。大小写、全半角标点、连字符、技术标识符、公式、单位和数字词法差异都保留为 raw disagreement。</div>
    <div class="declaration-box">
      <h2>审阅声明（完成全部项目后再导出）</h2>
      <div class="hint">这些声明会随本地提交一起导出。若不能真实确认，请不要勾选；Finalize 会据此暂停。</div>
      <div class="checks" id="declarations"></div>
    </div>
    <div id="form-fields"></div>
    <div class="import-box">
      <strong>导入已有 submission.json（仅恢复本地草稿）</strong>
      <div class="hint">选择同一 reviewer、review session 和 workpack 导出的 JSON；导入只恢复声明和允许的人工 review 字段，不调用 Finalize、不生成 Gold，也不建立信任判定。导入失败时当前草稿、声明和表单保持不变。</div>
      <div class="actions"><input id="import-submission-file" type="file" accept=".json,application/json"><button class="action secondary" id="import-submission-button" type="button">导入并恢复填写内容</button></div>
      <div id="import-status" class="hint" aria-live="polite">尚未选择 submission 文件。</div>
    </div>
    <div class="export-box">
      <strong>导出 / Finalize</strong>
      <div class="hint">浏览器只导出人工填写内容和锁定的 workpack identity，不计算信任哈希，也不生成 Gold。普通“导出提交”允许保存草稿；“按规则检查并导出 Finalize 输入”只在浏览器侧按 canonical 必填规则检查，有缺项或逻辑矛盾时阻断，不调用后端 Finalize。</div>
      <div id="finalize-issues" class="finalize-issues" role="alert" hidden></div>
      <div class="actions">
        <button class="action" id="export" type="button">导出提交</button>
        <button class="action danger" id="finalize" type="button">按规则检查并导出 Finalize 输入</button>
      </div>
    </div>
  </section>
</main>
<script id="workpack-data" type="application/json">__WORKPACK_DATA__</script>
<script>
(function () {
  "use strict";
  /* Defense in depth: this file has no network code; these guards make a
     future accidental network call fail closed while the page is open. */
  window.fetch = function () { throw new Error("network_disabled"); };
  window.XMLHttpRequest = function () { throw new Error("network_disabled"); };
  window.WebSocket = function () { throw new Error("network_disabled"); };
  if (navigator.sendBeacon) { navigator.sendBeacon = function () { return false; }; }
  const PACK = JSON.parse(document.getElementById("workpack-data").textContent);
  const ITEMS = PACK.items;
  const ITEM_BY_ID = Object.fromEntries(ITEMS.map(item => [item.item_id, item]));
  const PROFILE = PACK.profile || null;
  const profileVersionNode = document.getElementById("profile-version");
  const profileHashNode = document.getElementById("profile-hash");
  if (PROFILE) {
    if (profileVersionNode) profileVersionNode.textContent = `${PROFILE.profile_id} v${PROFILE.profile_version}`;
    if (profileHashNode) profileHashNode.textContent = PROFILE.profile_sha256;
  }
  const profileVersion = PROFILE ? `${PROFILE.profile_id} v${PROFILE.profile_version}` : "legacy-unbound";
  const VIEW_ZOOM_MIN = __VIEW_ZOOM_MIN__;
  const VIEW_ZOOM_MAX = __VIEW_ZOOM_MAX__;
  const VIEW_ZOOM_STEP = __VIEW_ZOOM_STEP__;
  const STORAGE_KEY = PACK.ui_policy.autosave_namespace;
  const state = { current: 0, items: {}, declarations: {} };
  const declarationLabels = {
    qualified_reviewer: "我符合本项目指定的 reviewer 资格。",
    reviewed_specified_source_evidence: "我审阅了本项目指定的 source evidence 身份。",
    did_not_view_predictions: "我没有查看任何模型输出、模型置信度或隐藏答案。",
    did_not_change_split: "我没有修改或重新分配任何评测分层。",
    reviewed_specified_render_crop: "我实际查看了本项目指定的 Context render 与 Crop。",
    not_evaluator: "我不是本 benchmark 的 evaluator。",
    independent_from_proposer: "我独立于 preparation proposer，未参与候选生成。"
  };
  const declarationKeys = Object.keys(declarationLabels);
  const statuses = [["complete","完成"],["uncertain","不确定（不猜）"],["needs_adjudication","需要裁决"],["unreadable","看不清"],["candidate_incorrect","候选类型/定位不正确"]];
  const bboxVerdicts = [["correct","正确：锁定候选框 correct"],["too_large","过大：需要修正框 too_large"],["too_small","过小：需要修正框 too_small"],["wrong","位置错误：需要修正框 wrong"],["not_applicable","不适用：对象不存在 not_applicable"],["adjusted","需调整 adjusted（旧草稿）"],["incorrect","错误 incorrect（旧草稿）"]];
  const options = (pairs, selected) => `<option value="">请选择…</option>${pairs.map(pair => `<option value="${escapeHtml(pair[0])}"${selected === pair[0] ? " selected" : ""}>${escapeHtml(pair[1])}</option>`).join("")}`;
  const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[character]));
  const textField = (label, key, value, full = false, placeholder = "") => `<div class="field${full ? " full" : ""}"><label>${escapeHtml(label)}</label><textarea data-field="${escapeHtml(key)}" placeholder="${escapeHtml(placeholder)}">${escapeHtml(value ?? "")}</textarea></div>`;
  const inputField = (label, key, value, type = "text", full = false, extra = "") => `<div class="field${full ? " full" : ""}"><label>${escapeHtml(label)}</label><input type="${type}" data-field="${escapeHtml(key)}" value="${escapeHtml(value ?? "")}" ${extra}></div>`;
  const selectField = (label, key, pairs, value, full = false) => `<div class="field${full ? " full" : ""}"><label>${escapeHtml(label)}</label><select data-field="${escapeHtml(key)}">${options(pairs, value)}</select></div>`;
  const reviewKeys = ["review_status","presence","type_verdict","bbox_verdict","corrected_bbox","observed_bbox","reviewer_confidence","notes","difference_observation","equation_type","transcription_latex","formula_number","crosses_lines","subscripts","superscripts","special_symbols","transcription_completeness","figure_type","caption","caption_presence","figure_number","completeness","has_subfigures","subfigure_count","table_type","row_count","column_count","header_text","cells","merged_cells","table_number","structure_completeness","chart_type","title","x_axis_label","y_axis_label","legend","series_count","series_names","data_points_or_curves","chart_completeness"];
  const blankReview = () => ({ review_status:"", presence:"", type_verdict:"", bbox_verdict:"", corrected_bbox:null, observed_bbox:[null,null,null,null], reviewer_confidence:"", notes:"", difference_observation:"", equation_type:"", transcription_latex:"", formula_number:"", crosses_lines:"", subscripts:"", superscripts:"", special_symbols:"", transcription_completeness:"", figure_type:"", caption:"", caption_presence:"", figure_number:"", completeness:"", has_subfigures:"", subfigure_count:null, table_type:"", row_count:null, column_count:null, header_text:"", cells:[], merged_cells:"", table_number:"", structure_completeness:"", chart_type:"", title:"", x_axis_label:"", y_axis_label:"", legend:"", series_count:null, series_names:[], data_points_or_curves:"", chart_completeness:"" });
  function clearInapplicableBbox(review) {
    if (review.presence === "absent" || review.bbox_verdict === "not_applicable") {
      review.corrected_bbox = null;
      review.observed_bbox = [null, null, null, null];
    }
    return review;
  }
  function synchronizeNotApplicableCandidate(review) {
    if (review.bbox_verdict === "not_applicable") {
      review.review_status = "candidate_incorrect";
      review.presence = "absent";
      if (!["other", "not_this_type"].includes(review.type_verdict)) review.type_verdict = "not_this_type";
    }
    return clearInapplicableBbox(review);
  }
  function normalizeReview(saved) {
    const review = blankReview();
    if (saved && typeof saved === "object" && !Array.isArray(saved)) {
      for (const key of reviewKeys) {
        if (!Object.prototype.hasOwnProperty.call(saved, key)) continue;
        const value = saved[key];
        if (key === "cells") review.cells = Array.isArray(value) ? value.filter(cell => cell && typeof cell === "object" && !Array.isArray(cell)).map(cell => ({ row:cell.row, column:cell.column, text:typeof cell.text === "string" ? cell.text : "" })) : [];
        else if (key === "series_names") review.series_names = Array.isArray(value) ? value.filter(name => typeof name === "string") : [];
        else if (key === "corrected_bbox" || key === "observed_bbox") review[key] = value === null ? null : Array.isArray(value) ? value.slice() : null;
        else review[key] = value;
      }
    }
    if (review.corrected_bbox === null && Array.isArray(review.observed_bbox) && review.observed_bbox.length === 4 && review.observed_bbox.every(value => value !== null && value !== "" && Number.isFinite(Number(value)))) review.corrected_bbox = review.observed_bbox.slice(0, 4).map(Number);
    return synchronizeNotApplicableCandidate(review);
  }
  function normalizeDeclarations(saved) {
    const declarations = {};
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return declarations;
    declarationKeys.forEach(key => { if (typeof saved[key] === "boolean") declarations[key] = saved[key]; });
    return declarations;
  }
  function readDraft() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (saved && typeof saved === "object" && !Array.isArray(saved)) {
        const savedItems = saved.items && typeof saved.items === "object" && !Array.isArray(saved.items) ? saved.items : {};
        state.items = Object.fromEntries(Object.entries(savedItems).filter(([key]) => Object.prototype.hasOwnProperty.call(ITEM_BY_ID, key)).map(([key, value]) => [key, normalizeReview(value)]));
        state.declarations = normalizeDeclarations(saved.declarations);
      }
    } catch (_) { /* corrupted local draft is ignored; export remains explicit */ }
  }
  function sameValue(left, right) {
    if (left === right) return true;
    if (Array.isArray(left) || Array.isArray(right)) return Array.isArray(left) && Array.isArray(right) && left.length === right.length && left.every((value, index) => sameValue(value, right[index]));
    if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
    const leftKeys = Object.keys(left).sort();
    const rightKeys = Object.keys(right).sort();
    return leftKeys.length === rightKeys.length && leftKeys.every((key, index) => key === rightKeys[index] && sameValue(left[key], right[key]));
  }
  function forbiddenImportField(value, ignoreDeclarations = false) {
    if (Array.isArray(value)) { for (const nested of value) { const found = forbiddenImportField(nested, ignoreDeclarations); if (found) return found; } return null; }
    if (!value || typeof value !== "object") return null;
    for (const [key, nested] of Object.entries(value)) {
      const normalized = String(key).toLowerCase().replace(/[\s-]+/g, "_");
      if (ignoreDeclarations && normalized === "declarations") continue;
      if (normalized === "split" || normalized.includes("prediction") || normalized.includes("model") || normalized.includes("hidden_answer") || normalized.includes("expected_answer") || normalized === "gold" || normalized.startsWith("gold_")) return key;
      const found = forbiddenImportField(nested, ignoreDeclarations);
      if (found) return found;
    }
    return null;
  }
  function expectedSubmissionIdentity() {
    return { workpack_sha256:PACK.workpack_identity.workpack_sha256, phase7d5a_manifest_sha256:PACK.workpack_identity.phase7d5a_manifest_sha256, candidate_items_file_sha256:PACK.workpack_identity.candidate_items_file_sha256, split_commitment_sha256:PACK.workpack_identity.split_commitment_sha256, review_plan_sha256:PACK.workpack_identity.review_plan_sha256 };
  }
  function importSubmissionObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("JSON 顶层必须是对象");
    if (value.schema_version !== "__SUBMISSION_SCHEMA__" || value.protocol !== "__PROTOCOL__") throw new Error("submission schema_version 或 protocol 与当前页面不一致");
    if (value.workbench_instance_id !== PACK.workbench_instance_id) throw new Error("workbench_instance_id 与当前页面不一致");
    if (value.reviewer_instance !== PACK.reviewer.reviewer_instance) throw new Error("reviewer_instance 与当前页面不一致；请导入对应 reviewer 页面导出的文件");
    if (value.review_session_id !== PACK.reviewer.review_session_id) throw new Error("review_session_id 与当前页面不一致；请导入同一 review session 的文件");
    if (!sameValue(value.workpack_identity, expectedSubmissionIdentity())) throw new Error("完整 workpack_identity 与当前 workpack 不一致");
    const forbidden = forbiddenImportField(value, true);
    if (forbidden) throw new Error(`检测到禁止导入的隐藏字段：${forbidden}`);
    if (!Array.isArray(value.items) || value.items.length !== ITEMS.length) throw new Error(`items 必须恰好包含 ${ITEMS.length} 个项目；当前为 ${Array.isArray(value.items) ? value.items.length : "非数组"}`);
    const importedItems = {};
    for (const raw of value.items) {
      if (!raw || typeof raw !== "object" || Array.isArray(raw) || typeof raw.item_id !== "string") throw new Error("items 中存在缺少 item_id 的项目");
      const id = raw.item_id;
      const item = ITEM_BY_ID[id];
      if (!item) throw new Error(`items 包含当前 PACK 不存在的 item_id：${id}`);
      if (Object.prototype.hasOwnProperty.call(importedItems, id)) throw new Error(`items 存在重复 item_id：${id}`);
      const expectedHashes = { source_sha256:item.source_sha256, render_sha256:item.context.sha256, crop_sha256:item.crop.sha256, bbox_sha256:item.bbox_sha256 };
      if (raw.item_sha256 !== item.item_sha256 || raw.review_task_id !== item.review_task_id || !sameValue(raw.input_hashes, expectedHashes)) throw new Error(`项目 ${id} 的 item/task/input_hashes 与当前 workpack 不一致`);
      importedItems[id] = normalizeReview(raw.review);
    }
    const missing = ITEMS.find(item => !Object.prototype.hasOwnProperty.call(importedItems, item.item_id));
    if (missing) throw new Error(`items 缺少项目：${missing.item_id}`);
    return { items:importedItems, declarations:normalizeDeclarations(value.declarations) };
  }
  function setStatus(message, good = true) { const node = document.getElementById("save-state"); node.textContent = message; node.className = good ? "" : "warning"; }
  function selectedItem() { return ITEMS[state.current]; }
  function currentReview() { return normalizeReview(state.items[selectedItem().item_id]); }
  function renderDeclarations() {
    document.getElementById("declarations").innerHTML = Object.entries(declarationLabels).map(([key, label]) => `<label><input type="checkbox" data-declaration="${key}"${state.declarations[key] === true ? " checked" : ""}>${escapeHtml(label)}</label>`).join("");
  }
  function renderCellGrid(review) {
    const holder = document.getElementById("cell-grid");
    if (!holder) return;
    const rows = Number.isInteger(review.row_count) ? review.row_count : Number.parseInt(review.row_count || "", 10);
    const columns = Number.isInteger(review.column_count) ? review.column_count : Number.parseInt(review.column_count || "", 10);
    if (!Number.isInteger(rows) || !Number.isInteger(columns) || rows < 1 || columns < 1) { holder.innerHTML = `<div class="hint">先填写行数和列数，再点击“生成单元格表格”。</div>`; return; }
    const old = Array.isArray(review.cells) ? review.cells : [];
    holder.style.gridTemplateColumns = `repeat(${columns}, minmax(110px, 1fr))`;
    let html = "";
    for (let row = 0; row < rows; row += 1) for (let column = 0; column < columns; column += 1) {
      const existing = old.find(cell => cell && cell.row === row && cell.column === column);
      html += `<input data-cell-row="${row}" data-cell-column="${column}" value="${escapeHtml(existing ? existing.text : "")}" placeholder="r${row + 1} c${column + 1}">`;
    }
    holder.innerHTML = html;
  }
  function stripTerminalLineBreak(value) { return value.endsWith("\r\n") ? value.slice(0, -2) : value.endsWith("\n") || value.endsWith("\r") ? value.slice(0, -1) : value; }
  function parseDelimitedTable(value, delimiter) {
    const source = stripTerminalLineBreak(String(value).replace(/^\uFEFF/, ""));
    if (!source) throw new Error("未识别到数据行");
    const rows = [];
    let row = [];
    let cell = [];
    let quoted = false;
    for (let index = 0; index < source.length; index += 1) {
      const character = source[index];
      if (quoted) {
      if (character === '"') {
          if (source[index + 1] === '"') { cell.push('"'); index += 1; }
          else quoted = false;
        } else cell.push(character);
      } else if (character === '"' && cell.length === 0) quoted = true;
      else if (character === delimiter) { row.push(cell.join("")); cell = []; }
      else if (character === "\n" || character === "\r") {
        if (row.length === 0 && cell.length === 0) throw new Error("存在空行，无法确定表格尺寸");
        if (character === "\r" && source[index + 1] === "\n") index += 1;
        row.push(cell.join("")); cell = []; rows.push(row); row = [];
      } else cell.push(character);
    }
    if (quoted) throw new Error("引号未闭合");
    row.push(cell.join("")); rows.push(row);
    const columns = rows[0].length;
    if (!columns || rows.some(candidate => candidate.length !== columns)) throw new Error("每行列数不一致");
    return rows;
  }
  function markdownPipeRow(line) {
    let value = line.trim();
    if (value.startsWith("|")) value = value.slice(1);
    if (value.endsWith("|") && !value.endsWith("\\|")) value = value.slice(0, -1);
    const cells = [];
    let cell = "";
    for (let index = 0; index < value.length; index += 1) {
      if (value[index] === "\\" && value[index + 1] === "|") { cell += "|"; index += 1; }
      else if (value[index] === "|") { cells.push(cell.trim()); cell = ""; }
      else cell += value[index];
    }
    cells.push(cell.trim());
    return cells;
  }
  function markdownSeparator(cells) { return cells.length > 0 && cells.every(cell => /^:?-{3,}:?$/.test(cell.trim())); }
  function looksLikeMarkdownPipeTable(value) {
    const lines = value.split(/\r?\n/).filter(line => line.trim());
    return lines.length >= 2 && lines.some(line => line.includes("|")) && lines.slice(1).some(line => markdownSeparator(markdownPipeRow(line)));
  }
  function parseTablePaste(value) {
    const source = String(value ?? "").replace(/^\uFEFF/, "");
    if (!source.trim()) throw new Error("粘贴内容为空");
    if (looksLikeMarkdownPipeTable(source)) {
      const rows = [];
      source.split(/\r?\n/).forEach(line => { if (!line.trim()) return; const cells = markdownPipeRow(line); if (!markdownSeparator(cells)) rows.push(cells); });
      if (!rows.length) throw new Error("Markdown 表格没有数据行");
      const columns = rows[0].length;
      if (!columns || rows.some(row => row.length !== columns)) throw new Error("Markdown 每行列数不一致");
      return { format:"markdown", rows };
    }
    if (source.includes("\t")) return { format:"tsv", rows:parseDelimitedTable(source, "\t") };
    if (source.includes(",")) return { format:"csv", rows:parseDelimitedTable(source, ",") };
    return { format:"tsv", rows:parseDelimitedTable(source, "\t") };
  }
  function tablePasteFormatLabel(format) { return format === "markdown" ? "Markdown pipe table" : format.toUpperCase(); }
  let transientSelection = null;
  let dragStart = null;
  let dragActive = false;
  let panActive = false;
  let panPointerId = null;
  let panLast = null;
  const contextView = { itemId:null, zoom:null, fitZoom:1, fitMode:true, mode:"select", spacePan:false };
  const MIN_SELECTION_POINTS = 1.0;
  function numericBBox(value) {
    if (!Array.isArray(value) || value.length !== 4 || !value.every(number => number !== null && number !== "" && Number.isFinite(Number(number)))) return null;
    const numbers = value.map(Number);
    return numbers[0] < numbers[2] && numbers[1] < numbers[3] ? numbers : null;
  }
  function bboxLabel(bbox, source = "") {
    const numbers = numericBBox(bbox);
    if (!numbers) return "未设置";
    const suffix = source ? `（${source}）` : "";
    return `x0（左）= ${numbers[0].toFixed(3)}；y0（上）= ${numbers[1].toFixed(3)}；x1（右）= ${numbers[2].toFixed(3)}；y1（下）= ${numbers[3].toFixed(3)}；单位 = PDF points；原点 = 页面左上角${suffix}`;
  }
  function mappingFor(item) { return item && item.context && item.context.coordinate_mapping ? item.context.coordinate_mapping : { status:"unverifiable", unsupported_reason:"缺少 canonical context mapping" }; }
  function currentBboxVerdict() { const field = document.querySelector('[data-field="bbox_verdict"]'); return field ? field.value : currentReview().bbox_verdict; }
  function renderCorrectedSummary(item, review) {
    const node = document.getElementById("corrected-bbox-field");
    if (!node) return;
    if (currentBboxVerdict() === "correct") { node.textContent = "未单独设置；correct 将锁定并采用候选 BBox。"; return; }
    node.textContent = bboxLabel(review.corrected_bbox, "corrected_bbox");
  }
  function renderFormBboxSummary(item, review) { renderCorrectedSummary(item, review); }
  function typeFields(item, review) {
    const kind = item.candidate_type;
    let fields = "";
    if (kind === "equation") {
      fields = `<div class="field-grid">${selectField("公式类型（equation_type）", "equation_type", [["display","独立展示 display"],["inline","行内 inline"],["numbered","带编号 numbered"],["unknown","无法分类 unknown"]], review.equation_type)}${selectField("公式转录完整度（transcription_completeness）", "transcription_completeness", [["complete","完整 complete"],["partial","部分可读 partial"],["unreadable","不可读 unreadable"]], review.transcription_completeness)}${inputField("公式编号（formula_number；明确没有编号才填 none；看不清请写 unreadable）", "formula_number", review.formula_number)}${selectField("是否跨行（crosses_lines）", "crosses_lines", [["yes","是 yes"],["no","否 no"],["uncertain","不确定 uncertain"]], review.crosses_lines)}${selectField("下标检查（subscripts；none 在此专用字段合法）", "subscripts", [["checked","已检查 checked"],["none","明确没有 none"],["uncertain","不确定 uncertain"]], review.subscripts)}${selectField("上标检查（superscripts；none 在此专用字段合法）", "superscripts", [["checked","已检查 checked"],["none","明确没有 none"],["uncertain","不确定 uncertain"]], review.superscripts)}${selectField("特殊符号检查（special_symbols；none 在此专用字段合法）", "special_symbols", [["checked","已检查 checked"],["none","明确没有 none"],["uncertain","不确定 uncertain"]], review.special_symbols)}${textField("公式 LaTeX 主体（不含定界符；transcription_latex）", "transcription_latex", review.transcription_latex, true)}<div class="field full"><div class="hint warning">输入契约：选择 <code>complete</code> 时必须填写裸 LaTeX math body；选择 <code>partial</code>/<code>unreadable</code> 时不要猜，LaTeX 可留空。任何情况下不要输入 <code>$$...$$</code>、<code>\\[...\\]</code>、<code>$...$</code> 或 <code>\\(...\\)</code>。示例：<code>\\nabla \\times \\mathbf{B}=\\mu_0\\mathbf{J}</code>。</div></div></div>`;
    } else if (kind === "figure") {
      fields = `<div class="field-grid">${selectField("图类型（figure_type）", "figure_type", [["photo","照片/图像 photo"],["diagram","示意图 diagram"],["schematic","结构图 schematic"],["plot","绘图 plot"],["other","其他 other"],["unknown","无法分类 unknown"]], review.figure_type)}${selectField("完整性（completeness）", "completeness", [["complete","完整 complete"],["partial","部分 partial"],["uncertain","不确定 uncertain"]], review.completeness)}${inputField("图号（figure_number；明确没有图号才填 none；看不清请写 unreadable）", "figure_number", review.figure_number)}${selectField("是否有子图（subfigure）", "has_subfigures", [["yes","是 yes"],["no","否 no"],["uncertain","不确定 uncertain"]], review.has_subfigures)}${inputField("子图数量（subfigure_count；无子图填 0，看不清不要猜）", "subfigure_count", review.subfigure_count, "number", false, "min=0 step=1 data-int")}${selectField("Caption 是否存在（caption_presence；必须选择：present/absent/unreadable）", "caption_presence", [["present","有 present"],["absent","无 absent"],["unreadable","看不清 unreadable"]], review.caption_presence)}${textField("Caption 内容（caption；present 且可读才抄录；absent 才填 none；unreadable 可留空或写 unreadable）", "caption", review.caption, true)}</div>`;
    } else if (kind === "table") {
      fields = `<div class="field-grid">${selectField("表格类型（table_type）", "table_type", [["ruled","有框线 ruled"],["unruled","无框线 unruled"],["matrix","矩阵 matrix"],["other","其他 other"],["unknown","无法分类 unknown"]], review.table_type)}${selectField("结构完整度（structure_completeness）", "structure_completeness", [["complete","完整 complete"],["partial","部分 partial"],["unreadable","不可读 unreadable"]], review.structure_completeness)}${inputField("行数（row_count；包括表头；正整数）", "row_count", review.row_count, "number", false, "min=1 step=1 data-int")}${inputField("列数（column_count；包括表头列；正整数）", "column_count", review.column_count, "number", false, "min=1 step=1 data-int")}${textField("表头（header_text；按阅读顺序记录；明确无可见表头可填 none；看不清写 unreadable）", "header_text", review.header_text, true)}${textField("合并单元格（merged_cells；明确没有才填 none；看不清请写 unreadable）", "merged_cells", review.merged_cells, true)}${inputField("表号（table_number；明确没有才填 none；看不清请写 unreadable）", "table_number", review.table_number)}${selectField("Caption 是否存在（caption_presence；必须选择：present/absent/unreadable）", "caption_presence", [["present","有 present"],["absent","无 absent"],["unreadable","看不清 unreadable"]], review.caption_presence)}${textField("Caption 内容（caption；present 且可读才抄录；absent 才填 none；unreadable 可留空或写 unreadable）", "caption", review.caption, true)}<div class="field full"><label>可编辑单元格（cells）</label><div class="actions"><button class="action secondary" type="button" id="make-cells">生成/更新单元格表格</button></div><div id="cell-grid" class="cell-grid"></div><div class="field table-paste-box"><label for="table-paste-input">批量粘贴识别（table paste）</label><textarea id="table-paste-input" placeholder="从 Excel 复制 TSV；第一行表头也请保留。"></textarea><div class="hint">支持 Excel/表格工具 TSV，也支持带引号的 CSV 和 Markdown pipe table。第一行表头也计入 row_count；真实空白 cell 保留为空字符串。识别出的实际 R×C 必须与期望 row_count×column_count 完全一致；尺寸不符不应用、不补齐、不截断、不改写当前 cells。</div><div class="actions"><button class="action secondary" type="button" id="recognize-table-paste">识别并应用到 cells</button></div><div id="table-paste-status" class="hint" aria-live="polite">尚未识别；请先填写 row_count 和 column_count。</div></div></div></div></div>`;
    } else if (kind === "chart") {
      fields = `<div class="field-grid">${selectField("图表类型（chart_type）", "chart_type", [["line","折线 line"],["bar","柱状 bar"],["scatter","散点 scatter"],["area","面积 area"],["other","其他 other"],["unknown","无法分类 unknown"]], review.chart_type)}${selectField("完整性（chart_completeness）", "chart_completeness", [["complete","完整 complete"],["partial","部分 partial"],["unreadable","不可读 unreadable"]], review.chart_completeness)}${inputField("标题（title；明确没有标题才填 none；看不清请写 unreadable）", "title", review.title)}${inputField("X 轴标签（x_axis_label；明确没有才填 none；看不清请写 unreadable）", "x_axis_label", review.x_axis_label)}${inputField("Y 轴标签（y_axis_label；明确没有才填 none；看不清请写 unreadable）", "y_axis_label", review.y_axis_label)}${inputField("系列数量（series_count；数不清不要猜；正整数或 0）", "series_count", review.series_count, "number", false, "min=0 step=1 data-int")}${textField("图例（legend；按行记录；明确没有才填 none；看不清请写 unreadable）", "legend", review.legend, true)}${textField("系列名称（series；每行一个；无名称但系列存在时填 unnamed-series-n）", "series_names_text", Array.isArray(review.series_names) ? review.series_names.join("\\n") : "", true)}${textField("数据点或曲线信息（data_points_or_curves；必须填写；只记录看清内容；明确无可记录数据才填 none）", "data_points_or_curves", review.data_points_or_curves, true)}${selectField("Caption 是否存在（caption_presence；必须选择：present/absent/unreadable）", "caption_presence", [["present","有 present"],["absent","无 absent"],["unreadable","看不清 unreadable"]], review.caption_presence)}${textField("Caption 内容（caption；present 且可读才抄录；absent 才填 none；unreadable 可留空或写 unreadable）", "caption", review.caption, true)}</div>`;
    }
    return fields;
  }
  function renderForm(item) {
    const review = currentReview();
    const common = `<div class="field-grid"><div class="field full"><label>审阅状态（review_status）</label><select data-field="review_status">${options(statuses, review.review_status)}</select></div><div class="field full hint">只有所有适用必填字段都能根据当前 Context/Crop 可靠记录时才选 complete；uncertain、needs_adjudication、unreadable 是合法的不猜状态，但会阻断 canonical Finalize。</div>${selectField("候选类型对象是否存在（presence）", "presence", [["present","存在 present"],["absent","不存在 absent"],["uncertain","不确定 uncertain"]], review.presence)}<div class="field full hint">Presence 只判断当前候选类型对象是否存在；若 type_verdict=other/not_this_type，应使用 review_status=candidate_incorrect、presence=absent、bbox_verdict=not_applicable，并填写具体差异观察。</div>${selectField("候选/对象类型判断（candidate/object type）", "type_verdict", [[item.candidate_type,"与候选类型一致 " + item.candidate_type],["other","其他类型 other"],["not_this_type","不是此类型 not_this_type"],["unknown","无法判断 unknown"]], review.type_verdict)}${selectField("BBox 判断（bbox_verdict）", "bbox_verdict", bboxVerdicts, review.bbox_verdict)}<div class="field full hint warning">选择“对象不存在 not_applicable”会自动联动为 candidate_incorrect + absent + 类型不匹配，并清除修正 BBox；已明确选择 other 时会保留，否则设为 not_this_type。导入旧 submission 时也会执行相同联动。仍须选择人工审阅把握度，并在差异观察中具体说明为何这是误检。此时下方 ${escapeHtml(item.candidate_type)} 专用字段不参与 Finalize，已有内容只保留为草稿。</div><div class="field full bbox-review"><label>BBox 修正（corrected_bbox）</label><div id="corrected-bbox-field" class="bbox-readout"></div><div class="hint">只在 Context 上拖拽并点击“采用为修正 BBox”后写入。correct 会锁定候选 BBox；too_large / too_small / wrong（以及旧值 adjusted / incorrect）在对象存在且状态可完成时必须有有效修正框。</div></div>${selectField("人工审阅把握度（confidence）", "reviewer_confidence", [["high","高 high"],["medium","中 medium"],["low","低 low"]], review.reviewer_confidence)}${textField("可选补充说明（notes；可选）", "notes", review.notes, true)}${textField("差异/无差异观察（difference_observation；必填；不能留空或只填 none/N/A/na/null/-/无）", "difference_observation", review.difference_observation, true, "无差异：对象存在，类型和候选 BBox 与页面一致")}<div class="field full"><div class="hint warning">此字段只接受本项实际观察；即使没有差异，也请写具体内容，例如“无差异：对象存在，类型和候选 BBox 与页面一致”。不能只填 none、N/A、na、null、- 或 无。这个限制只适用于 difference_observation；formula_number、figure_number、merged_cells、table_number 以及明确没有标题/轴/图例/Caption 的专用文字字段仍可按各自说明填 none。</div></div></div><div class="section"><h2>${escapeHtml(item.candidate_type)} 专用字段（${escapeHtml(item.candidate_type)} fields）</h2>${typeFields(item, review)}</div>`;
    document.getElementById("form-fields").innerHTML = common;
    if (item.candidate_type === "table") {
      renderCellGrid(review);
      document.getElementById("make-cells").addEventListener("click", () => { const next = collectReview(); renderCellGrid(next); });
      document.getElementById("recognize-table-paste").addEventListener("click", applyTablePaste);
      document.getElementById("table-paste-input").addEventListener("input", () => tablePasteStatus("已输入待识别内容；点击“识别并应用到 cells”。当前网格不会自动改变。"));
    }
    applyReview(review);
    renderFormBboxSummary(item, review);
  }
  function applyReview(review) {
    document.querySelectorAll("[data-field]").forEach(element => {
      const key = element.dataset.field;
      if (key === "series_names_text") { element.value = Array.isArray(review.series_names) ? review.series_names.join("\n") : ""; return; }
      if (key === "bbox0") { element.value = review.observed_bbox?.[0] ?? ""; return; }
      if (key === "bbox1") { element.value = review.observed_bbox?.[1] ?? ""; return; }
      if (key === "bbox2") { element.value = review.observed_bbox?.[2] ?? ""; return; }
      if (key === "bbox3") { element.value = review.observed_bbox?.[3] ?? ""; return; }
      if (review[key] !== undefined && review[key] !== null) element.value = review[key];
    });
    if (selectedItem().candidate_type === "table") renderCellGrid(review);
  }
  function collectReview() {
    const review = blankReview();
    const previous = currentReview();
    review.corrected_bbox = numericBBox(previous.corrected_bbox);
    document.querySelectorAll("[data-field]").forEach(element => {
      const key = element.dataset.field;
      if (key === "series_names_text") { review.series_names = element.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean); return; }
      if (element.dataset.int !== undefined) { review[key] = element.value === "" ? null : Number.parseInt(element.value, 10); return; }
      review[key] = element.value;
    });
    if (review.bbox_verdict === "correct") review.corrected_bbox = null;
    if (selectedItem().candidate_type === "table") {
      review.cells = Array.from(document.querySelectorAll("[data-cell-row]")).map(element => ({ row:Number(element.dataset.cellRow), column:Number(element.dataset.cellColumn), text:element.value }));
    }
    return synchronizeNotApplicableCandidate(review);
  }
  function tablePasteStatus(message, good = true) {
    const node = document.getElementById("table-paste-status");
    if (node) { node.textContent = message; node.className = good ? "hint" : "hint warning"; }
  }
  function tablePasteExpectedDimensions() {
    const rowField = document.querySelector('[data-field="row_count"]');
    const columnField = document.querySelector('[data-field="column_count"]');
    const rows = completionInteger(rowField ? rowField.value : null);
    const columns = completionInteger(columnField ? columnField.value : null);
    return { rows:rows !== null && rows >= 1 ? rows : null, columns:columns !== null && columns >= 1 ? columns : null };
  }
  function applyTablePaste() {
    const input = document.getElementById("table-paste-input");
    if (!input) return;
    let parsed;
    try { parsed = parseTablePaste(input.value); }
    catch (error) { tablePasteStatus(`识别失败：${error && error.message ? error.message : "格式无法解析"}。当前 cells 未改变。`, false); return; }
    const actualRows = parsed.rows.length;
    const actualColumns = parsed.rows[0] ? parsed.rows[0].length : 0;
    const expected = tablePasteExpectedDimensions();
    const expectedLabel = expected.rows === null || expected.columns === null ? "未设置有效 row_count×column_count" : `${expected.rows}×${expected.columns}`;
    const actualLabel = `${actualRows}×${actualColumns}`;
    if (expected.rows === null || expected.columns === null) { tablePasteStatus(`已识别 ${tablePasteFormatLabel(parsed.format)}：实际 ${actualLabel} / 期望 ${expectedLabel}。请先填写正整数 row_count 和 column_count；未应用，当前 cells 未改变。`, false); return; }
    if (actualRows !== expected.rows || actualColumns !== expected.columns) { tablePasteStatus(`已识别 ${tablePasteFormatLabel(parsed.format)}：实际 ${actualLabel} / 期望 ${expectedLabel}。尺寸必须完全一致；未应用，当前 cells 未改变。请调整行列数或粘贴内容后重试。`, false); return; }
    const review = collectReview();
    review.cells = parsed.rows.flatMap((row, rowIndex) => row.map((text, columnIndex) => ({ row:rowIndex, column:columnIndex, text })));
    state.items[selectedItem().item_id] = review;
    renderCellGrid(review);
    saveDraft(`已应用 ${tablePasteFormatLabel(parsed.format)}：实际 ${actualLabel} / 期望 ${expectedLabel}；${actualRows * actualColumns} 个 cells 已按 row-major 写入`);
    tablePasteStatus(`识别成功并已应用 ${tablePasteFormatLabel(parsed.format)}：实际 ${actualLabel} / 期望 ${expectedLabel}；${actualRows * actualColumns} 个 cells 已按 0-based row-major 写入。真实空白格保留为空字符串。`, true);
  }
  function saveDraft(message = "已保存到本机草稿") {
    state.items[selectedItem().item_id] = collectReview();
    document.querySelectorAll("[data-declaration]").forEach(element => { state.declarations[element.dataset.declaration] = element.checked; });
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ items:state.items, declarations:state.declarations }));
    updateProgress(); setStatus(message);
  }
  const completionStatusValues = new Set(statuses.map(pair => pair[0]));
  const completionBboxValues = new Set(bboxVerdicts.map(pair => pair[0]));
  const typeMismatchVerdicts = new Set(["other", "not_this_type"]);
  const unresolvedStatuses = new Set(["uncertain", "needs_adjudication", "unreadable"]);
  const correctedBboxVerdicts = new Set(["too_large", "too_small", "wrong", "adjusted", "incorrect"]);
  function completionNonempty(value) {
    if (value === null || value === undefined) return false;
    if (typeof value === "string") return Boolean(value.trim());
    if (Array.isArray(value)) return value.length > 0;
    if (typeof value === "object") return Object.keys(value).length > 0;
    return true;
  }
  function completionInteger(value) {
    if (Number.isInteger(value)) return value;
    if (typeof value === "string" && /^-?\d+$/.test(value.trim())) return Number.parseInt(value, 10);
    return null;
  }
  const differenceObservationPlaceholders = new Set(__DIFFERENCE_OBSERVATION_PLACEHOLDERS__);
  function differenceObservationPlaceholder(value) {
    if (typeof value !== "string") return null;
    const normalized = value.replace(/\s+/g, "").toLowerCase();
    return differenceObservationPlaceholders.has(normalized) ? normalized : null;
  }
  function correctedBboxValidForItem(value, item) {
    const bbox = numericBBox(value);
    const page = numericBBox(mappingFor(item).canonical_page_bbox);
    return Boolean(bbox && page && bbox[0] >= page[0] && bbox[1] >= page[1] && bbox[2] <= page[2] && bbox[3] <= page[3]);
  }
  function clientTableCellIssues(id, review, rows, columns) {
    const cells = review.cells;
    const expected = rows * columns;
    if (!Array.isArray(cells) || cells.length !== expected || cells.some(cell => !cell || typeof cell !== "object" || Array.isArray(cell))) return [`table_cells_incomplete:${id}`];
    const expectedCoordinates = new Set();
    for (let row = 0; row < rows; row += 1) for (let column = 0; column < columns; column += 1) expectedCoordinates.add(`${row}:${column}`);
    const seenCoordinates = new Set();
    let coordinatesInvalid = false;
    let textInvalid = false;
    for (const cell of cells) {
      const row = cell.row;
      const column = cell.column;
      if (!Number.isInteger(row) || !Number.isInteger(column) || row < 0 || row >= rows || column < 0 || column >= columns) coordinatesInvalid = true;
      else {
        const coordinate = `${row}:${column}`;
        if (seenCoordinates.has(coordinate)) coordinatesInvalid = true;
        seenCoordinates.add(coordinate);
      }
      if (typeof cell.text !== "string") textInvalid = true;
    }
    if (seenCoordinates.size !== expectedCoordinates.size || [...expectedCoordinates].some(coordinate => !seenCoordinates.has(coordinate))) coordinatesInvalid = true;
    const issues = [];
    if (coordinatesInvalid) issues.push(`table_cell_coordinates_invalid:${id}`);
    if (textInvalid) issues.push(`table_cell_text_invalid:${id}`);
    else if (!cells.some(cell => cell.text.trim())) issues.push(`table_cells_all_blank:${id}`);
    return issues;
  }
  function clientTypeSpecificIssues(item, review) {
    const issues = [];
    const id = item.item_id;
    if (item.candidate_type === "equation") {
      for (const key of ["equation_type", "formula_number", "crosses_lines", "transcription_completeness"]) if (!completionNonempty(review[key])) issues.push(`equation_field_missing:${id}:${key}`);
      if (review.transcription_completeness === "complete" && !completionNonempty(review.transcription_latex)) issues.push(`equation_transcription_missing:${id}`);
      for (const key of ["subscripts", "superscripts", "special_symbols"]) if (!["checked", "none", "uncertain"].includes(review[key])) issues.push(`equation_symbol_check_missing:${id}:${key}`);
    } else if (item.candidate_type === "figure") {
      for (const key of ["figure_type", "figure_number", "caption_presence", "completeness", "has_subfigures"]) if (!completionNonempty(review[key])) issues.push(`figure_field_missing:${id}:${key}`);
      if (review.has_subfigures === "yes" && completionInteger(review.subfigure_count) === null) issues.push(`figure_subfigure_count_missing:${id}`);
    } else if (item.candidate_type === "table") {
      for (const key of ["table_type", "row_count", "column_count", "header_text", "merged_cells", "caption_presence", "table_number", "structure_completeness"]) if (!(key in review) || review[key] === null) issues.push(`table_field_missing:${id}:${key}`);
      const rows = completionInteger(review.row_count);
      const columns = completionInteger(review.column_count);
      if (rows === null || rows < 1) issues.push(`table_row_count_invalid:${id}`);
      if (columns === null || columns < 1) issues.push(`table_column_count_invalid:${id}`);
      if (rows !== null && rows >= 1 && columns !== null && columns >= 1) issues.push(...clientTableCellIssues(id, review, rows, columns));
    } else if (item.candidate_type === "chart") {
      for (const key of ["chart_type", "title", "x_axis_label", "y_axis_label", "legend", "series_count", "data_points_or_curves", "caption_presence", "chart_completeness"]) if (!(key in review) || review[key] === null) issues.push(`chart_field_missing:${id}:${key}`);
      const seriesCount = completionInteger(review.series_count);
      if (seriesCount === null || seriesCount < 0) issues.push(`chart_series_count_invalid:${id}`);
      if (!Array.isArray(review.series_names) || review.series_names.length !== (seriesCount === null ? -1 : seriesCount)) issues.push(`chart_series_names_incomplete:${id}`);
      if (!completionNonempty(review.data_points_or_curves)) issues.push(`chart_data_or_curve_info_missing:${id}`);
    } else {
      issues.push(`kind_invalid:${id}`);
    }
    return issues;
  }
  function completionIssuesForItem(item, review) {
    const issues = [];
    const id = item.item_id;
    const status = review.review_status;
    const presence = review.presence;
    const typeVerdict = review.type_verdict;
    const bboxVerdict = review.bbox_verdict;
    if (!completionStatusValues.has(status)) return [`review_status_missing_or_invalid:${id}`];
    if (review.notes !== undefined && typeof review.notes !== "string") issues.push(`notes_invalid:${id}`);
    if (typeof review.difference_observation !== "string" || !review.difference_observation.trim()) issues.push(`difference_observation_missing:${id}`);
    else if (differenceObservationPlaceholder(review.difference_observation)) issues.push(`difference_observation_placeholder:${id}`);
    if (typeMismatchVerdicts.has(typeVerdict)) {
      if (status !== "candidate_incorrect") issues.push(`type_mismatch_requires_candidate_incorrect:${id}`);
      if (presence !== "absent") issues.push(`type_mismatch_requires_absent:${id}`);
      if (bboxVerdict !== "not_applicable") issues.push(`type_mismatch_requires_not_applicable_bbox:${id}`);
    }
    if (status === "complete") {
      if (presence !== "present") issues.push(`complete_requires_present:${id}`);
      if (typeVerdict === "unknown") issues.push(`complete_type_verdict_unknown:${id}`);
      else if (typeMismatchVerdicts.has(typeVerdict)) issues.push(`complete_type_verdict_mismatch:${id}`);
    }
    if (unresolvedStatuses.has(status)) {
      issues.push(`unresolved_${status}:${id}`);
      return issues;
    }
    if (!["present", "absent", "uncertain"].includes(presence)) issues.push(`presence_missing:${id}`);
    if (!["low", "medium", "high"].includes(review.reviewer_confidence)) issues.push(`reviewer_confidence_missing:${id}`);
    if (typeVerdict === null || typeVerdict === undefined || typeVerdict === "") issues.push(`type_verdict_missing:${id}`);
    if (presence === "present") {
      if (!completionBboxValues.has(bboxVerdict) || bboxVerdict === "not_applicable") issues.push(`bbox_verdict_missing:${id}`);
      if (bboxVerdict === "correct") {
        const corrected = review.corrected_bbox;
        if (corrected !== null && corrected !== undefined && (!correctedBboxValidForItem(corrected, item) || !numericBBox(item.candidate_bbox_pdf) || !numericBBox(corrected).every((value, index) => Math.abs(value - Number(item.candidate_bbox_pdf[index])) <= 1e-6))) issues.push(`correct_bbox_cannot_override_candidate:${id}`);
      } else if (correctedBboxVerdicts.has(bboxVerdict)) {
        if (!correctedBboxValidForItem(review.corrected_bbox, item)) issues.push(`corrected_bbox_missing_or_invalid:${id}`);
        else if (mappingFor(item).status !== "verified") issues.push(`bbox_coordinate_mapping_unverifiable:${id}`);
      }
    } else if (presence === "absent") {
      if (!["not_applicable", "incorrect", "wrong", "correct"].includes(bboxVerdict)) issues.push(`bbox_absence_verdict_missing:${id}`);
    } else {
      issues.push(`presence_unresolved:${id}`);
    }
    if ((presence === "absent" || bboxVerdict === "not_applicable") && review.corrected_bbox !== null && review.corrected_bbox !== undefined) issues.push(`corrected_bbox_not_applicable:${id}`);
    if (status === "candidate_incorrect" && presence !== "absent") issues.push(`candidate_incorrect_requires_absent:${id}`);
    if (status === "candidate_incorrect" && bboxVerdict !== "not_applicable") issues.push(`candidate_incorrect_requires_not_applicable_bbox:${id}`);
    if (status === "complete" && presence === "present") issues.push(...clientTypeSpecificIssues(item, review));
    if (item.candidate_type === "equation" && latexDelimiterKind(review.transcription_latex)) issues.push(`equation_latex_delimiter_forbidden:${id}`);
    return issues;
  }
  function completionIssueText(item, issue) {
    if (issue.startsWith("review_status_missing_or_invalid")) return "请选择合法审阅状态";
    if (issue.startsWith("difference_observation_missing")) return "差异/无差异观察必填；即使无差异也要写具体观察，例如“无差异：对象存在，类型和候选 BBox 与页面一致”";
    if (issue.startsWith("difference_observation_placeholder")) return "不能只填 none、N/A、na、null、- 或 无；请写具体差异或合法的“无差异：具体观察...”";
    if (issue.startsWith("notes_invalid")) return "Notes 若填写必须是文字";
    if (issue.startsWith("unresolved_")) return "仍是不确定/待裁决/看不清状态";
    if (issue.startsWith("complete_requires_present")) return "complete 必须确认候选类型对象存在（presence=present）";
    if (issue.startsWith("complete_type_verdict_unknown")) return "unknown 不能标记为 complete";
    if (issue.startsWith("complete_type_verdict_mismatch") || issue.startsWith("type_mismatch_requires")) return "类型不一致须改为 candidate_incorrect + absent + not_applicable";
    if (issue.startsWith("reviewer_confidence_missing")) return "请选择人工审阅把握度";
    if (issue.startsWith("presence_")) return "请填写候选类型对象是否存在";
    if (issue.startsWith("type_verdict_missing")) return "请选择候选/对象类型判断；无法可靠分类请选 unknown 并保留不确定状态";
    if (issue.startsWith("bbox_") || issue.startsWith("corrected_bbox")) return "请补齐或清理适用的 BBox 判断/修正框；对象存在且不是 correct 时须采用有效 PDF points 修正框";
    if (issue.startsWith("table_cell_coordinates_invalid")) return "cells 必须覆盖唯一且范围内的完整 0-based row/column 网格；请重新生成或重新批量粘贴";
    if (issue.startsWith("table_cell_text_invalid")) return "每个 cell.text 必须是文字；真实空白格请保留为空，不要填非文字对象";
    if (issue.startsWith("table_cells_all_blank")) return "cells 至少要有一个非空 cell.text；真实空白格可以留空，但不能整表都没有观察文字";
    if (issue.startsWith("table_cells_incomplete")) return "cells 必须恰好有 row_count×column_count 个单元格；可生成网格或批量粘贴，尺寸不符不会应用";
    if (issue.startsWith("table_row_count_invalid")) return "row_count 必须是包含表头在内的正整数";
    if (issue.startsWith("table_column_count_invalid")) return "column_count 必须是正整数，并包含表头列";
    if (issue.startsWith("table_field_missing")) return `请填写 Table 专用字段 ${issue.split(":").pop()}；明确没有的文字字段按字段说明填 none`;
    if (issue.startsWith("chart_series_names_incomplete")) return "series_names 的行数必须等于 series_count；无名称但系列存在请按说明记录 unnamed-series-n";
    if (issue.startsWith("chart_series_count_invalid")) return "series_count 必须是实际可数的数据系列数量，不确定时不要猜";
    if (issue.startsWith("chart_data_or_curve_info_missing")) return "请记录看清的数据点或曲线信息；看不清时改用 unreadable/uncertain，不要猜数值";
    if (issue.startsWith("chart_field_missing")) return `请填写 Chart 专用字段 ${issue.split(":").pop()}；明确没有的标题/轴/图例/Caption 才填 none`;
    if (issue.startsWith("figure_subfigure_count_missing")) return "has_subfigures=yes 时请填写实际可数的 subfigure_count；数不清请改为不确定状态";
    if (issue.startsWith("figure_field_missing")) return `请填写 Figure 专用字段 ${issue.split(":").pop()}；明确不存在的图号/Caption 才填 none`;
    if (issue.startsWith("equation_transcription_missing")) return "transcription_completeness=complete 时必须填写裸 LaTeX 主体，不含任何定界符";
    if (issue.startsWith("equation_symbol_check_missing")) return `请逐项选择 ${issue.split(":").pop()}；none 仅表示该专用符号确实不存在`;
    if (issue.startsWith("equation_field_missing")) return `请填写 Equation 专用字段 ${issue.split(":").pop()}；formula_number 明确无编号才填 none`;
    if (issue.startsWith("equation_latex_delimiter_forbidden")) return "transcription_latex 只能是裸 LaTeX math body；请删除 $$、$、\\[...\\] 或 \\(...\\) 定界符，系统不会静默删除";
    if (issue.startsWith("equation_") || issue.startsWith("figure_") || issue.startsWith("table_") || issue.startsWith("chart_")) return "请补齐该类型的必填内容";
    return issue;
  }
  function browserCompletionIssues() {
    return ITEMS.map((item, index) => ({ item, index, issues: completionIssuesForItem(item, normalizeReview(state.items[item.item_id])) })).filter(problem => problem.issues.length);
  }
  function updateProgress() {
    const problems = browserCompletionIssues();
    const incompleteIds = new Set(problems.map(problem => problem.item.item_id));
    const done = ITEMS.length - problems.length;
    document.getElementById("progress-text").textContent = `${done}/${ITEMS.length}`;
    document.getElementById("progress-bar").style.width = `${Math.round(done / ITEMS.length * 100)}%`;
    document.querySelectorAll("#item-list button").forEach(button => button.classList.toggle("done", !incompleteIds.has(button.dataset.itemId)));
    renderIncompleteList(problems);
  }
  function renderIncompleteList(problems = browserCompletionIssues()) {
    const incomplete = problems;
    document.getElementById("incomplete-count").textContent = `(${incomplete.length})`;
    const holder = document.getElementById("incomplete-list");
    holder.innerHTML = incomplete.length ? incomplete.map(problem => `<button type="button" data-incomplete-index="${problem.index}" data-item-id="${escapeHtml(problem.item.item_id)}">${escapeHtml(problem.item.item_id)}<small>${escapeHtml(problem.item.candidate_type)} · page ${problem.item.physical_page} · ${escapeHtml(completionIssueText(problem.item, problem.issues[0]))}</small></button>`).join("") : `<div class="hint">全部项目已满足当前必填规则；仍需核对声明并处理不确定项。</div>`;
    holder.querySelectorAll("button").forEach(button => button.addEventListener("click", () => { saveDraft("已保存，已切换到未完成项目"); state.current = Number(button.dataset.incompleteIndex); renderItem(); }));
  }
  function renderFinalizeIssues(problems) {
    const holder = document.getElementById("finalize-issues");
    if (!holder) return;
    if (!problems.length) { holder.hidden = true; holder.innerHTML = ""; return; }
    holder.hidden = false;
    holder.innerHTML = `<strong>Finalize 已阻断：以下项目还有明显缺项或逻辑矛盾（点击可定位）</strong><ul>${problems.map(problem => `<li><button type="button" data-finalize-index="${problem.index}">${escapeHtml(problem.item.item_id)}：${escapeHtml(problem.issues.map(issue => completionIssueText(problem.item, issue)).join("；"))}</button></li>`).join("")}</ul>`;
    holder.querySelectorAll("button").forEach(button => button.addEventListener("click", () => { state.current = Number(button.dataset.finalizeIndex); renderItem(); holder.scrollIntoView({ block:"nearest" }); }));
  }
  function renderList() {
    document.getElementById("item-list").innerHTML = ITEMS.map((item, index) => `<button type="button" data-item-id="${escapeHtml(item.item_id)}" data-index="${index}">${escapeHtml(item.item_id)}<small>${escapeHtml(item.candidate_type)} · page ${item.physical_page}</small></button>`).join("");
    document.querySelectorAll("#item-list button").forEach(button => button.addEventListener("click", () => { saveDraft("已保存，已切换项目"); state.current = Number(button.dataset.index); renderItem(); }));
  }
  function clampZoom(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return VIEW_ZOOM_MIN;
    return Math.min(VIEW_ZOOM_MAX, Math.max(VIEW_ZOOM_MIN, number));
  }
  function contextViewport() { return document.getElementById("context-viewport"); }
  function contextSpace() { return document.getElementById("context-space"); }
  function resetContextViewForItem(item) {
    if (contextView.itemId === (item && item.item_id)) return;
    contextView.itemId = item ? item.item_id : null;
    contextView.zoom = null;
    contextView.fitZoom = 1;
    contextView.fitMode = true;
    contextView.mode = "select";
    contextView.spacePan = false;
    dragStart = null;
    dragActive = false;
    panActive = false;
    panPointerId = null;
    panLast = null;
  }
  function displayedImageRect(image) {
    if (!image || !image.naturalWidth || !image.naturalHeight) return null;
    const rect = image.getBoundingClientRect();
    if (!Number.isFinite(rect.width) || !Number.isFinite(rect.height) || rect.width <= 0 || rect.height <= 0) return null;
    return { left:rect.left, top:rect.top, width:rect.width, height:rect.height };
  }
  function fitZoomForContext() {
    const viewport = contextViewport();
    const image = document.getElementById("context-image");
    if (!viewport || !image || !image.naturalWidth || !image.naturalHeight || viewport.clientWidth <= 0 || viewport.clientHeight <= 0) return VIEW_ZOOM_MIN;
    const availableWidth = Math.max(1, viewport.clientWidth - 16);
    const availableHeight = Math.max(1, viewport.clientHeight - 16);
    return clampZoom(Math.min(availableWidth / image.naturalWidth, availableHeight / image.naturalHeight));
  }
  function layoutContextSurface() {
    const item = selectedItem();
    const viewport = contextViewport();
    const space = contextSpace();
    const stage = document.getElementById("context-stage");
    const image = document.getElementById("context-image");
    if (!item || !viewport || !space || !stage || !image || !image.naturalWidth || !image.naturalHeight) return null;
    resetContextViewForItem(item);
    contextView.fitZoom = fitZoomForContext();
    contextView.zoom = contextView.fitMode || contextView.zoom === null ? contextView.fitZoom : clampZoom(contextView.zoom);
    const width = Math.max(1, image.naturalWidth * contextView.zoom);
    const height = Math.max(1, image.naturalHeight * contextView.zoom);
    space.style.width = `${Math.max(viewport.clientWidth, width)}px`;
    space.style.height = `${Math.max(viewport.clientHeight, height)}px`;
    stage.style.width = `${width}px`;
    stage.style.height = `${height}px`;
    stage.style.left = "0px";
    stage.style.top = "0px";
    return { viewport, space, stage, image, width, height, zoom:contextView.zoom };
  }
  function contextCanvasGeometry() {
    const layout = layoutContextSurface();
    const canvas = document.getElementById("context-overlay");
    if (!layout || !canvas) return null;
    const { viewport, stage, image } = layout;
    const mapping = mappingFor(selectedItem());
    const dimensions = mapping.render_dimensions_px || {};
    if (mapping.status !== "verified" || image.naturalWidth !== Number(dimensions.width) || image.naturalHeight !== Number(dimensions.height)) return null;
    const stageRect = stage.getBoundingClientRect();
    const displayed = displayedImageRect(image);
    if (!displayed || stageRect.width <= 0 || stageRect.height <= 0) return null;
    const deviceScale = Math.max(1, Math.min(4, window.devicePixelRatio || 1));
    canvas.width = Math.max(1, Math.round(stageRect.width * deviceScale));
    canvas.height = Math.max(1, Math.round(stageRect.height * deviceScale));
    const context = canvas.getContext("2d");
    if (!context) return null;
    context.setTransform(deviceScale, 0, 0, deviceScale, 0, 0);
    context.clearRect(0, 0, stageRect.width, stageRect.height);
    return { viewport, stage, canvas, image, mapping, stageRect, displayed, context, width:stageRect.width, height:stageRect.height, deviceScale };
  }
  function pdfToCanvasPoint(bbox, geometry) {
    const contextBBox = numericBBox(geometry.mapping.context_pdf_bbox);
    const values = numericBBox(bbox);
    if (!contextBBox || !values) return null;
    return {
      x: geometry.displayed.left - geometry.stageRect.left + (values[0] - contextBBox[0]) / (contextBBox[2] - contextBBox[0]) * geometry.displayed.width,
      y: geometry.displayed.top - geometry.stageRect.top + (values[1] - contextBBox[1]) / (contextBBox[3] - contextBBox[1]) * geometry.displayed.height,
      width: (values[2] - values[0]) / (contextBBox[2] - contextBBox[0]) * geometry.displayed.width,
      height: (values[3] - values[1]) / (contextBBox[3] - contextBBox[1]) * geometry.displayed.height,
    };
  }
  function pdfToClientPoint(point, geometry) {
    const contextBBox = numericBBox(geometry.mapping.context_pdf_bbox);
    if (!contextBBox || !Array.isArray(point) || point.length < 2) return null;
    return {
      x: geometry.displayed.left + (Number(point[0]) - contextBBox[0]) / (contextBBox[2] - contextBBox[0]) * geometry.displayed.width,
      y: geometry.displayed.top + (Number(point[1]) - contextBBox[1]) / (contextBBox[3] - contextBBox[1]) * geometry.displayed.height,
    };
  }
  function drawOverlay() {
    const geometry = contextCanvasGeometry();
    const canvas = document.getElementById("context-overlay");
    if (!geometry) { if (canvas) canvas.style.pointerEvents = "none"; return; }
    canvas.style.pointerEvents = "auto";
    const draw = (bbox, color, dash, width) => { const point = pdfToCanvasPoint(bbox, geometry); if (!point) return; geometry.context.save(); geometry.context.strokeStyle = color; geometry.context.lineWidth = width; geometry.context.setLineDash(dash); geometry.context.strokeRect(point.x, point.y, point.width, point.height); geometry.context.restore(); };
    draw(selectedItem().candidate_bbox_pdf, "#d97706", [8, 5], 2);
    const review = currentReview();
    if (currentBboxVerdict() !== "correct") draw(review.corrected_bbox, "#15803d", [], 2.5);
    draw(transientSelection, "#7c3aed", [5, 4], 2);
  }
  function clientToPdf(clientX, clientY, clampToPage = false) {
    const geometry = contextCanvasGeometry();
    if (!geometry) return { inside:false, mappingVerified:false, unavailable:true };
    const displayed = geometry.displayed;
    const inside = clientX >= displayed.left && clientX <= displayed.left + displayed.width && clientY >= displayed.top && clientY <= displayed.top + displayed.height;
    if (!inside && !clampToPage) return { inside:false };
    const x = Math.min(displayed.left + displayed.width, Math.max(displayed.left, clientX));
    const y = Math.min(displayed.top + displayed.height, Math.max(displayed.top, clientY));
    const pixelX = (x - displayed.left) / displayed.width * geometry.image.naturalWidth;
    const pixelY = (y - displayed.top) / displayed.height * geometry.image.naturalHeight;
    const pdf = _contextPixelToPdfForUi(geometry.mapping, pixelX, pixelY, geometry.image.naturalWidth, geometry.image.naturalHeight);
    return { inside, pdf, pixel:[pixelX, pixelY] };
  }
  function _contextPixelToPdfForUi(mapping, pixelX, pixelY, naturalWidth, naturalHeight) {
    const contextBBox = numericBBox(mapping.context_pdf_bbox);
    if (mapping.status !== "verified" || !contextBBox || naturalWidth <= 0 || naturalHeight <= 0) return null;
    return [contextBBox[0] + Math.min(1, Math.max(0, pixelX / naturalWidth)) * (contextBBox[2] - contextBBox[0]), contextBBox[1] + Math.min(1, Math.max(0, pixelY / naturalHeight)) * (contextBBox[3] - contextBBox[1])];
  }
  function normalizedSelection(first, second, mapping) {
    const page = numericBBox(mapping.canonical_page_bbox);
    if (!first || !second || !page) return null;
    const x0 = Math.max(page[0], Math.min(page[2], Math.min(first[0], second[0])));
    const y0 = Math.max(page[1], Math.min(page[3], Math.min(first[1], second[1])));
    const x1 = Math.max(page[0], Math.min(page[2], Math.max(first[0], second[0])));
    const y1 = Math.max(page[1], Math.min(page[3], Math.max(first[1], second[1])));
    if (x1 - x0 < MIN_SELECTION_POINTS || y1 - y0 < MIN_SELECTION_POINTS) return null;
    return [x0, y0, x1, y1];
  }
  function effectiveContextMode() { return contextView.spacePan ? "pan" : contextView.mode; }
  function renderContextMode() {
    const viewport = contextViewport();
    const select = document.getElementById("select-mode");
    const pan = document.getElementById("pan-mode");
    const readout = document.getElementById("context-mode-readout");
    const mode = effectiveContextMode();
    if (viewport) viewport.className = `context-viewport mode-${mode}`;
    if (select) select.setAttribute("aria-pressed", contextView.mode === "select" ? "true" : "false");
    if (pan) pan.setAttribute("aria-pressed", contextView.mode === "pan" ? "true" : "false");
    if (readout) readout.textContent = contextView.spacePan ? "当前模式：临时平移（松开 Space 后恢复）" : `当前模式：${mode === "select" ? "框选 BBox" : "平移页面"}`;
  }
  function renderZoomReadout() {
    const node = document.getElementById("zoom-readout");
    if (!node) return;
    const zoom = Number.isFinite(contextView.zoom) ? contextView.zoom : contextView.fitZoom;
    const fit = Number.isFinite(contextView.fitZoom) ? contextView.fitZoom : VIEW_ZOOM_MIN;
    node.textContent = `缩放：${Math.round(zoom * 100)}%（适合窗口 ${Math.round(fit * 100)}%；范围 ${Math.round(VIEW_ZOOM_MIN * 100)}%–${Math.round(VIEW_ZOOM_MAX * 100)}%）`;
  }
  function currentViewportCenter() {
    const viewport = contextViewport();
    if (!viewport) return null;
    const rect = viewport.getBoundingClientRect();
    return { x:rect.left + rect.width / 2, y:rect.top + rect.height / 2 };
  }
  function setZoom(nextZoom, anchorClient = null) {
    const viewport = contextViewport();
    if (!viewport) return;
    const before = contextCanvasGeometry();
    const anchor = anchorClient || currentViewportCenter();
    let anchorPdf = null;
    if (before && anchor) {
      const value = clientToPdf(anchor.x, anchor.y, true);
      if (value && value.pdf) anchorPdf = value.pdf;
    }
    const oldScrollLeft = viewport.scrollLeft;
    const oldScrollTop = viewport.scrollTop;
    contextView.zoom = clampZoom(nextZoom);
    contextView.fitMode = false;
    layoutContextSurface();
    const after = contextCanvasGeometry();
    if (after && anchor && anchorPdf) {
      const newClient = pdfToClientPoint(anchorPdf, after);
      if (newClient) {
        viewport.scrollLeft = Math.max(0, oldScrollLeft + newClient.x - anchor.x);
        viewport.scrollTop = Math.max(0, oldScrollTop + newClient.y - anchor.y);
      }
    }
    renderZoomReadout();
    renderContextMode();
    renderContextStatus();
    renderSelectionReadout();
    drawOverlay();
  }
  function fitContextView(resetMode = false) {
    const viewport = contextViewport();
    contextView.zoom = null;
    contextView.fitMode = true;
    layoutContextSurface();
    if (viewport) { viewport.scrollLeft = 0; viewport.scrollTop = 0; }
    if (resetMode) { contextView.mode = "select"; contextView.spacePan = false; }
    renderZoomReadout();
    renderContextMode();
    refreshContextUI();
  }
  function setContextMode(mode) {
    if (mode !== "select" && mode !== "pan") return;
    if (dragActive || panActive) { dragActive = false; dragStart = null; panActive = false; panLast = null; panPointerId = null; clearTransientSelection("已切换模式，未完成的临时框已清除"); }
    contextView.mode = mode;
    contextView.spacePan = false;
    renderContextMode();
    setStatus(mode === "select" ? "当前模式：框选 BBox" : "当前模式：平移页面；拖动只移动视口");
  }
  function renderContextStatus() {
    const node = document.getElementById("context-mapping-status");
    const mapping = mappingFor(selectedItem());
    const geometry = contextCanvasGeometry();
    const verified = mapping.status === "verified" && Boolean(geometry);
    if (node) { node.className = verified ? "mapping-verified" : "mapping-unavailable"; node.textContent = verified ? `坐标映射：可验证；主坐标 = PDF points；Context bbox = ${bboxLabel(mapping.context_pdf_bbox)}` : `坐标映射不可验证：${mapping.unsupported_reason || "图片尚未加载或 natural 尺寸与冻结 metadata 不一致"}。已禁用修正 BBox。`; }
    const canvas = document.getElementById("context-overlay");
    if (canvas) canvas.style.pointerEvents = verified ? "auto" : "none";
    renderContextMode();
    renderZoomReadout();
    updateBboxControls();
  }
  function renderPointerReadout(value) {
    const node = document.getElementById("pointer-readout");
    if (!node) return;
    if (value && value.unavailable) { node.textContent = "鼠标坐标：坐标映射不可验证；可查看/缩放/平移，但禁用 PDF points 采集。"; return; }
    if (!value || value.inside === false || !value.pdf) { node.textContent = "鼠标坐标：页面内容区域外；主坐标不会把 CSS 像素当作 PDF points。"; return; }
    node.textContent = `鼠标坐标（PDF points，页面左上原点）：x = ${value.pdf[0].toFixed(3)}，y = ${value.pdf[1].toFixed(3)}；次要 render pixel：x = ${value.pixel[0].toFixed(1)}，y = ${value.pixel[1].toFixed(1)}`;
  }
  function renderSelectionReadout() {
    const node = document.getElementById("selection-readout");
    if (node) node.textContent = transientSelection ? `当前框选（尚未采用）：${bboxLabel(transientSelection)}` : "当前框选：未开始。";
    const review = currentReview();
    const meta = document.getElementById("item-corrected-bbox");
    if (meta) meta.textContent = bboxLabel(review.corrected_bbox, "corrected_bbox；绿色实线");
    renderCorrectedSummary(selectedItem(), review);
    updateBboxControls();
  }
  function updateBboxControls() {
    const adopt = document.getElementById("adopt-corrected");
    const reset = document.getElementById("reset-corrected");
    const mapping = mappingFor(selectedItem());
    const review = currentReview();
    const bboxNotApplicable = review.presence === "absent" || currentBboxVerdict() === "not_applicable";
    if (adopt) adopt.disabled = mapping.status !== "verified" || bboxNotApplicable || currentBboxVerdict() === "correct" || !numericBBox(transientSelection);
    if (reset) reset.disabled = !numericBBox(review.corrected_bbox);
  }
  function refreshContextUI() { renderContextStatus(); renderSelectionReadout(); drawOverlay(); }
  function clearTransientSelection(message = "已清除当前临时框选") { transientSelection = null; renderSelectionReadout(); drawOverlay(); if (message) setStatus(message); }
  function adoptTransientSelection() {
    if (!numericBBox(transientSelection)) { setStatus("请先在 Context 页面拖出有效框；过小框不会被采用", false); return; }
    if (currentBboxVerdict() === "correct") { setStatus("BBox verdict=correct 会锁定候选框，不能采用修正框", false); return; }
    if (currentReview().presence === "absent" || currentBboxVerdict() === "not_applicable") { setStatus("对象不存在或 BBox 不适用时不会保留 corrected_bbox", false); return; }
    const review = collectReview();
    review.corrected_bbox = transientSelection.slice();
    state.items[selectedItem().item_id] = review;
    transientSelection = null;
    saveDraft("已采用为 corrected_bbox；原候选框仍保留为橙色虚线");
    renderSelectionReadout(); drawOverlay();
  }
  function resetCorrectedSelection() {
    const review = collectReview();
    review.corrected_bbox = null;
    state.items[selectedItem().item_id] = review;
    transientSelection = null;
    saveDraft("已重置 corrected_bbox，恢复为未修正状态");
    renderSelectionReadout(); drawOverlay();
  }
  function renderItem() {
    const item = selectedItem();
    transientSelection = null;
    document.getElementById("item-id").textContent = item.item_id;
    document.getElementById("item-kind").textContent = item.candidate_type;
    document.getElementById("item-page").textContent = String(item.physical_page);
    document.getElementById("item-bbox").textContent = bboxLabel(item.candidate_bbox_pdf, "candidate_bbox；橙色虚线");
    document.getElementById("item-corrected-bbox").textContent = bboxLabel(currentReview().corrected_bbox, "corrected_bbox；绿色实线");
    document.getElementById("item-source-hash").textContent = item.source_sha256;
    document.getElementById("item-render-hash").textContent = item.context.sha256;
    document.getElementById("item-crop-hash").textContent = item.crop.sha256;
    document.getElementById("item-coordinate-system").textContent = `${item.page_geometry?.coordinate_origin || "locked page origin"} · PDF points · 非像素`;
    document.getElementById("item-scope").textContent = item.review_scope.join(", ");
    document.getElementById("context-image").src = item.context.asset;
    document.getElementById("crop-image").src = item.crop.asset;
    document.querySelectorAll("#item-list button").forEach(button => button.classList.toggle("active", button.dataset.itemId === item.item_id));
    renderForm(item); updateProgress(); refreshContextUI();
  }
  function latexDelimiterKind(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    if (/\$\$/.test(value)) return "double-dollar";
    if (/\\\[|\\\]/.test(value)) return "display-bracket";
    if (/\\\(|\\\)/.test(value)) return "inline-parenthesis";
    if (/(^|[^\\])\$(?!\$)/.test(value)) return "single-dollar";
    return null;
  }
  function exportSubmission(message, finalizeOnly = false) {
    saveDraft("已保存，正在导出提交");
    if (finalizeOnly) {
      const problems = browserCompletionIssues();
      renderFinalizeIssues(problems);
      if (problems.length) {
        state.current = problems[0].index;
        renderItem();
        renderFinalizeIssues(problems);
        const issuePanel = document.getElementById("finalize-issues");
        if (issuePanel) issuePanel.scrollIntoView({ block:"nearest" });
        setStatus(`Finalize 已阻断：${problems.length} 个项目存在明显缺项或逻辑矛盾；请按列表逐项处理。普通“导出提交”仍可导出草稿。`, false);
        return false;
      }
    }
    const payload = { schema_version:"__SUBMISSION_SCHEMA__", protocol:"__PROTOCOL__", workbench_instance_id:PACK.workbench_instance_id, reviewer_instance:PACK.reviewer.reviewer_instance, review_session_id:PACK.reviewer.review_session_id, workpack_identity:{ workpack_sha256:PACK.workpack_identity.workpack_sha256, phase7d5a_manifest_sha256:PACK.workpack_identity.phase7d5a_manifest_sha256, candidate_items_file_sha256:PACK.workpack_identity.candidate_items_file_sha256, split_commitment_sha256:PACK.workpack_identity.split_commitment_sha256, review_plan_sha256:PACK.workpack_identity.review_plan_sha256 }, declarations:state.declarations, items:ITEMS.map(item => { const review = normalizeReview(state.items[item.item_id]); return { item_id:item.item_id, item_sha256:item.item_sha256, review_task_id:item.review_task_id, input_hashes:{ source_sha256:item.source_sha256, render_sha256:item.context.sha256, crop_sha256:item.crop.sha256, bbox_sha256:item.bbox_sha256 }, review }; }), exported_at:new Date().toISOString() };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type:"application/json" });
    const link = document.createElement("a"); link.download = `review-submission-${PACK.workpack_identity.workpack_sha256.slice(0,12)}-${PACK.reviewer.reviewer_instance}.json`; link.href = URL.createObjectURL(blob); link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 0); setStatus(message); return true;
  }
  function setImportStatus(message, good = true) {
    const node = document.getElementById("import-status");
    if (node) { node.textContent = message; node.className = good ? "hint" : "hint warning"; }
  }
  function importSubmissionFile() {
    const input = document.getElementById("import-submission-file");
    const file = input && input.files ? input.files[0] : null;
    if (!file) { setImportStatus("导入失败：请先选择 submission.json；当前草稿、声明和表单未改变。", false); return; }
    const reader = new FileReader();
    reader.onload = () => {
      const previousItems = state.items;
      const previousDeclarations = state.declarations;
      let previousStorage = null;
      let storageRead = false;
      let storageChanged = false;
      try {
        previousStorage = localStorage.getItem(STORAGE_KEY);
        storageRead = true;
        const imported = importSubmissionObject(JSON.parse(String(reader.result || "")));
        const serialized = JSON.stringify({ items:imported.items, declarations:imported.declarations });
        localStorage.setItem(STORAGE_KEY, serialized);
        storageChanged = true;
        state.items = imported.items;
        state.declarations = imported.declarations;
        renderDeclarations(); renderItem();
        setImportStatus(`导入成功：已恢复 ${ITEMS.length} 项和声明；仅写入当前 autosave namespace，未调用 Finalize、未生成 Gold。`, true);
        setStatus("旧 submission 已作为本地草稿恢复；请继续核对页面并按当前规则填写。", true);
      } catch (error) {
        state.items = previousItems;
        state.declarations = previousDeclarations;
        if (storageChanged && storageRead) {
          try { if (previousStorage === null) localStorage.removeItem(STORAGE_KEY); else localStorage.setItem(STORAGE_KEY, previousStorage); } catch (_) { /* keep the validation failure visible */ }
        }
        const reason = error && error.message ? error.message : "文件无法读取";
        setImportStatus(`导入失败：${reason}。当前草稿、声明和表单未改变。`, false);
        setStatus("导入未应用；请按提示修正文件或选择对应 reviewer 页面导出的 submission。", false);
      }
    };
    reader.onerror = () => { setImportStatus("导入失败：本地文件读取失败；当前草稿、声明和表单未改变。", false); setStatus("导入未应用；请重新选择本地 JSON 文件。", false); };
    reader.readAsText(file);
  }
  readDraft();
  document.getElementById("reviewer-id").textContent = PACK.reviewer.reviewer_instance;
  renderDeclarations(); renderList(); renderItem();
  const contextCanvas = document.getElementById("context-overlay");
  const contextImage = document.getElementById("context-image");
  const contextStage = document.getElementById("context-stage");
  const contextViewportNode = document.getElementById("context-viewport");
  contextImage.addEventListener("load", () => { contextView.zoom = null; refreshContextUI(); });
  contextImage.addEventListener("dragstart", event => event.preventDefault());
  contextStage.addEventListener("dragstart", event => event.preventDefault());
  function releaseViewportPointer(pointerId) {
    if (contextViewportNode && pointerId !== null && contextViewportNode.hasPointerCapture(pointerId)) contextViewportNode.releasePointerCapture(pointerId);
  }
  function cancelViewportInteraction(message) {
    const wasSelecting = dragActive;
    dragActive = false;
    dragStart = null;
    panActive = false;
    panPointerId = null;
    panLast = null;
    if (wasSelecting) clearTransientSelection(message || "已取消当前框选");
    else { renderContextMode(); drawOverlay(); if (message) setStatus(message); }
  }
  contextViewportNode.addEventListener("pointermove", event => {
    if (panActive) {
      event.preventDefault();
      if (panLast) {
        contextViewportNode.scrollLeft += panLast.x - event.clientX;
        contextViewportNode.scrollTop += panLast.y - event.clientY;
      }
      panLast = { x:event.clientX, y:event.clientY };
      renderPointerReadout(clientToPdf(event.clientX, event.clientY, false));
      return;
    }
    const value = clientToPdf(event.clientX, event.clientY, dragActive);
    renderPointerReadout(value);
    if (dragActive && value && value.pdf) { transientSelection = normalizedSelection(dragStart, value.pdf, mappingFor(selectedItem())); renderSelectionReadout(); }
    drawOverlay();
  });
  contextViewportNode.addEventListener("pointerleave", () => { if (!dragActive && !panActive) renderPointerReadout(null); });
  contextViewportNode.addEventListener("pointerdown", event => {
    const mode = effectiveContextMode();
    if (mode === "pan") {
      event.preventDefault();
      panActive = true;
      panPointerId = event.pointerId;
      panLast = { x:event.clientX, y:event.clientY };
      contextViewportNode.setPointerCapture(event.pointerId);
      renderContextMode();
      return;
    }
    const mapping = mappingFor(selectedItem());
    if (mapping.status !== "verified") { setStatus("坐标映射不可验证，已禁用 BBox 修正", false); return; }
    if (currentBboxVerdict() === "correct") { setStatus("BBox verdict=correct 会锁定候选框；如需修正请先选择 too_large / too_small / wrong", false); return; }
    const value = clientToPdf(event.clientX, event.clientY, false);
    if (!value || value.inside === false || !value.pdf) { setStatus("请在 Context 页面内容区域内开始框选", false); return; }
    event.preventDefault();
    dragActive = true;
    dragStart = value.pdf;
    transientSelection = null;
    contextViewportNode.setPointerCapture(event.pointerId);
    renderSelectionReadout();
  });
  contextViewportNode.addEventListener("pointerup", event => {
    if (panActive) {
      panActive = false;
      panLast = null;
      panPointerId = null;
      releaseViewportPointer(event.pointerId);
      renderContextMode();
      return;
    }
    if (!dragActive) return;
    const value = clientToPdf(event.clientX, event.clientY, true);
    const next = value && value.pdf ? normalizedSelection(dragStart, value.pdf, mappingFor(selectedItem())) : null;
    dragActive = false;
    dragStart = null;
    if (!next) { transientSelection = null; setStatus("框选过小或无效；请拖出至少 1 PDF point 的有效矩形", false); }
    else { transientSelection = next; setStatus("已生成临时框选；检查紫色虚线后点击“采用为修正 BBox”"); }
    renderSelectionReadout(); drawOverlay();
    releaseViewportPointer(event.pointerId);
  });
  contextViewportNode.addEventListener("pointercancel", event => { releaseViewportPointer(event.pointerId); cancelViewportInteraction("指针取消，已清除当前框选/平移状态"); });
  contextViewportNode.addEventListener("lostpointercapture", () => { if (dragActive || panActive) cancelViewportInteraction("指针捕获丢失，已清除半成品交互状态"); });
  document.getElementById("clear-selection").addEventListener("click", () => clearTransientSelection());
  document.getElementById("adopt-corrected").addEventListener("click", adoptTransientSelection);
  document.getElementById("reset-corrected").addEventListener("click", resetCorrectedSelection);
  document.getElementById("select-mode").addEventListener("click", () => setContextMode("select"));
  document.getElementById("pan-mode").addEventListener("click", () => setContextMode("pan"));
  document.getElementById("zoom-out").addEventListener("click", () => setZoom((contextView.zoom || contextView.fitZoom) / VIEW_ZOOM_STEP));
  document.getElementById("zoom-in").addEventListener("click", () => setZoom((contextView.zoom || contextView.fitZoom) * VIEW_ZOOM_STEP));
  document.getElementById("fit-view").addEventListener("click", () => fitContextView(false));
  document.getElementById("zoom-100").addEventListener("click", () => setZoom(1));
  document.getElementById("reset-view").addEventListener("click", () => fitContextView(true));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") { clearTransientSelection("已用 Esc 清除当前临时框选；视图和绿色修正框未改变"); return; }
    const target = event.target;
    const editing = target && ["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(target.tagName);
    if (event.code === "Space" && !event.repeat && !editing && contextViewportNode) { event.preventDefault(); contextView.spacePan = true; renderContextMode(); }
  });
  document.addEventListener("keyup", event => {
    if (event.code !== "Space") return;
    if (contextView.spacePan) { event.preventDefault(); contextView.spacePan = false; if (panActive) { const pointerId = panPointerId; panActive = false; panLast = null; panPointerId = null; releaseViewportPointer(pointerId); } renderContextMode(); }
  });
  window.addEventListener("blur", () => cancelViewportInteraction("窗口失焦，已清除半成品交互状态"));
  window.addEventListener("resize", () => refreshContextUI());
  document.getElementById("previous").addEventListener("click", () => { saveDraft(); state.current = (state.current - 1 + ITEMS.length) % ITEMS.length; renderItem(); });
  document.getElementById("next").addEventListener("click", () => { saveDraft(); state.current = (state.current + 1) % ITEMS.length; renderItem(); });
  document.getElementById("save").addEventListener("click", () => saveDraft());
  document.getElementById("export").addEventListener("click", () => exportSubmission("已导出草稿提交。请将 JSON 交给本地 finalize/validate。"));
  document.getElementById("finalize").addEventListener("click", () => exportSubmission("已导出 Finalize 输入；浏览器不会把它标为 Gold。", true));
  document.getElementById("import-submission-file").addEventListener("change", event => { const file = event.target.files && event.target.files[0]; setImportStatus(file ? `已选择 ${file.name}；点击“导入并恢复填写内容”后才会校验。` : "尚未选择 submission 文件。", true); });
  document.getElementById("import-submission-button").addEventListener("click", importSubmissionFile);
  document.getElementById("clear-draft").addEventListener("click", () => { localStorage.removeItem(STORAGE_KEY); state.items = {}; state.declarations = {}; renderDeclarations(); renderItem(); setStatus("本实例本地草稿已清除"); });
  document.addEventListener("input", event => { if (event.target && event.target.dataset && event.target.dataset.field === "transcription_latex" && latexDelimiterKind(event.target.value)) setStatus("公式 LaTeX 主体含有定界符；请明确删除 $$、$、\\[...\\] 或 \\(…\\) 后再导出。系统不会静默删除。", false); window.clearTimeout(window.__autosave); window.__autosave = window.setTimeout(() => saveDraft("自动保存完成"), 350); });
  document.addEventListener("change", event => {
    if (event.target && event.target.dataset && ["presence", "bbox_verdict"].includes(event.target.dataset.field)) {
      const review = collectReview();
      state.items[selectedItem().item_id] = review;
      if (event.target.dataset.field === "bbox_verdict" && event.target.value === "not_applicable") {
        applyReview(review);
        setStatus("已将对象不存在联动为 candidate_incorrect + absent + 类型不匹配；请补齐把握度和具体差异观察后再 Finalize。");
      }
      if (review.presence === "absent" || review.bbox_verdict === "not_applicable" || (event.target.dataset.field === "bbox_verdict" && event.target.value === "correct")) transientSelection = null;
      renderSelectionReadout(); drawOverlay();
    }
    window.clearTimeout(window.__autosave); window.__autosave = window.setTimeout(() => saveDraft("自动保存完成"), 350);
  });
}());
</script>
</body>
</html>
'''


def render_review_html(data: Mapping[str, Any]) -> str:
    serialized = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    serialized = serialized.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    profile = data.get("profile") if isinstance(data.get("profile"), Mapping) else {}
    profile_version = f"{profile.get('profile_id')} v{profile.get('profile_version')}" if profile else "legacy-unbound"
    profile_hash = str(profile.get("profile_sha256")) if profile else "unbound"
    return (REVIEW_HTML_TEMPLATE.replace("__WORKPACK_DATA__", serialized)
            .replace("__PROFILE_VERSION__", profile_version)
            .replace("__PROFILE_HASH__", profile_hash)
            .replace("__SUBMISSION_SCHEMA__", VISUAL_REVIEW_WORKBENCH_SUBMISSION_SCHEMA)
            .replace("__PROTOCOL__", VISUAL_REVIEW_WORKBENCH_PROTOCOL)
            .replace("__DIFFERENCE_OBSERVATION_PLACEHOLDERS__", json.dumps(sorted(DIFFERENCE_OBSERVATION_PLACEHOLDERS), ensure_ascii=False, separators=(",", ":")))
            .replace("__VIEW_ZOOM_MIN__", str(VIEW_ZOOM_MIN))
            .replace("__VIEW_ZOOM_MAX__", str(VIEW_ZOOM_MAX))
            .replace("__VIEW_ZOOM_STEP__", str(VIEW_ZOOM_STEP)))


ADJUDICATION_HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>Phase 7D Portable Gold Review</title>
<style>
:root { color-scheme: light; --ink:#18212b; --muted:#64717d; --line:#d8dee5; --soft:#f4f7f9; --accent:#075985; --warn:#9a3412; --ok:#166534; }
* { box-sizing:border-box; }
body { margin:0; color:var(--ink); background:#eef2f5; font:14px/1.45 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Segoe UI",sans-serif; }
header { position:sticky; top:0; z-index:4; padding:13px 18px; background:#fff; border-bottom:1px solid var(--line); display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
h1 { margin:0; font-size:18px; } h2 { font-size:15px; margin:0 0 8px; } h3 { font-size:13px; margin:12px 0 6px; }
.identity,.muted { color:var(--muted); font-size:12px; }
.shell { display:grid; grid-template-columns:220px minmax(0,1fr); gap:14px; max-width:1700px; margin:auto; padding:14px; }
.panel { background:#fff; border:1px solid var(--line); border-radius:10px; box-shadow:0 2px 8px rgba(20,35,50,.05); }
aside { padding:12px; align-self:start; position:sticky; top:70px; max-height:calc(100vh - 84px); overflow:auto; }
.item-list { display:grid; gap:5px; } .item-list button { text-align:left; border:1px solid transparent; background:transparent; border-radius:7px; padding:7px 8px; cursor:pointer; color:var(--ink); }
.item-list button.active { background:#e0f2fe; border-color:#7dd3fc; } .item-list button.done { color:var(--ok); }
.progress { height:8px; background:#e5e7eb; border-radius:99px; overflow:hidden; margin:8px 0 12px; } .progress span { display:block; height:100%; width:0; background:var(--accent); }
main { min-width:0; padding:14px; } .notice { padding:10px 12px; background:#fff7ed; border:1px solid #fed7aa; border-radius:8px; margin-bottom:12px; }
.visuals { display:grid; grid-template-columns:minmax(0,1.2fr) minmax(220px,.65fr); gap:10px; }
.visual { border:1px solid var(--line); border-radius:8px; padding:8px; background:#fbfcfd; min-width:0; }
.visual h3 { color:var(--muted); margin-top:0; } .context-box,.crop-box { overflow:auto; max-height:620px; background:#fff; border:1px solid #e7ebef; text-align:center; }
.context-stage { position:relative; margin:auto; touch-action:none; width:max-content; min-width:100%; }
.context-stage img { display:block; max-width:none; margin:auto; user-select:none; -webkit-user-drag:none; }
.context-stage canvas { position:absolute; inset:0; display:block; }
.context-box.mode-pan canvas { cursor:grab; } .context-box.mode-pan canvas.dragging { cursor:grabbing; }
.crop-box img { display:block; max-width:100%; height:auto; margin:auto; }
.context-toolbar,.quick-actions { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin:0 0 7px; }
.bbox-readout { color:var(--muted); font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; margin-top:6px; }
.quick-actions { padding:9px; border:1px solid #bae6fd; background:#f0f9ff; border-radius:8px; }
.quick-actions strong { margin-right:4px; }
.compare { margin-top:12px; overflow:auto; border:1px solid var(--line); border-radius:8px; }
table { border-collapse:collapse; width:100%; min-width:760px; } th,td { border-bottom:1px solid var(--line); padding:7px; vertical-align:top; text-align:left; } th { background:var(--soft); font-size:12px; }
tr.diff { background:#fff7ed; } td.value { white-space:pre-wrap; word-break:break-word; font:12px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace; max-width:250px; }
select,textarea,input { width:100%; border:1px solid #cbd5df; border-radius:6px; padding:7px; font:inherit; background:#fff; } textarea { min-height:74px; resize:vertical; }
.manual { min-height:58px; font:12px ui-monospace,SFMono-Regular,Menlo,monospace; } .form-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:12px; }
.cell-compare { display:grid; grid-template-columns:repeat(var(--cell-columns, 4), minmax(150px, 1fr)); gap:4px; min-width:640px; max-height:360px; overflow:auto; padding:4px; background:#f8fafc; border:1px solid var(--line); border-radius:6px; }
.cell-compare-card { min-height:62px; padding:5px; background:#fff; border:1px solid #e5e7eb; border-radius:5px; } .cell-compare-card.diff { background:#fff7ed; border-color:#fb923c; }
.cell-compare-label { color:var(--muted); font:11px ui-monospace,SFMono-Regular,Menlo,monospace; } .cell-compare-value { white-space:pre-wrap; overflow-wrap:anywhere; font:12px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace; }
.form-card { border:1px solid var(--line); border-radius:8px; padding:10px; } .checks { display:grid; grid-template-columns:1fr 1fr; gap:5px; } .checks label { display:flex; gap:6px; align-items:flex-start; font-size:12px; } .checks input { width:auto; margin-top:3px; }
.actions { display:flex; flex-wrap:wrap; gap:7px; margin-top:12px; } button,.button { border:1px solid #b8c4cf; border-radius:6px; padding:7px 10px; background:#fff; cursor:pointer; color:var(--ink); } button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
.status { min-height:22px; color:var(--muted); font-size:12px; margin-top:7px; } .boundary { margin:0 0 12px; }
@media (max-width:900px) { .shell { grid-template-columns:1fr; } aside { position:static; max-height:none; } .visuals,.form-grid { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header><h1>Phase 7D Portable Gold Review（人工定稿）</h1><span class="identity">当前需人工确认：<span id="header-count"></span> 项 · manifest <span id="manifest-short"></span></span></header>
<div class="shell">
<aside class="panel"><h2>Gold 审查项目</h2><div class="progress"><span id="progress-bar"></span></div><div id="progress-text" class="muted"></div><div id="item-list" class="item-list"></div><div class="actions"><button id="save-button">保存草稿</button><button id="import-button">导入已有 Gold Review submission</button><input id="import-file" type="file" accept="application/json,.json" hidden></div><div id="status" class="status"></div></aside>
<main class="panel"><section class="notice boundary"><strong>边界声明：</strong>Gold Curator 可以查看两份人工 reviewer 结果、Context 和 Crop，用它们形成唯一规范值；页面不会显示 prediction、split label、score、model confidence 或隐藏答案。机器差异只用于提示，不代表语义必然不同。本页不会自动投票、平均 BBox 或静默修改文本；最终选择必须由当前操作者明确完成。</section><div id="content"></div></main>
</div>
<script>
(() => {
  const DATA = __ADJUDICATION_DATA__;
  const MANIFEST_SHA256 = "__ADJUDICATION_MANIFEST_SHA256__";
  const STORAGE_KEY = DATA.ui_policy.autosave_namespace;
  const scalarFields = ["presence","type_verdict","bbox_verdict","bbox","corrected_bbox","bbox_source","bbox_coordinate_space","kind"];
  const clone = value => JSON.parse(JSON.stringify(value));
  const stable = value => { if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`; if (value && typeof value === "object") return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(",")}}`; return JSON.stringify(value); };
  const escapeHtml = value => String(value == null ? "" : value).replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
  const pretty = value => JSON.stringify(value, null, 2);
  const fieldValues = payload => { const result = {}; scalarFields.forEach(key => result[key] = clone(payload[key])); Object.keys(payload.details || {}).forEach(key => result[`details.${key}`] = clone(payload.details[key])); return result; };
  const itemById = Object.fromEntries(DATA.items.map(item => [item.item_id, item]));
  const emptyState = () => ({ current: DATA.items[0] && DATA.items[0].item_id, declarations: {}, items: Object.fromEntries(DATA.items.map(item => [item.item_id, { resolutions: Object.fromEntries(item.differing_fields.map(path => [path, {source:"", value:null}])), rationale:"", observation:"" }])) });
  let state = emptyState();
  try { const saved = localStorage.getItem(STORAGE_KEY); if (saved) state = Object.assign(emptyState(), JSON.parse(saved)); } catch (error) { /* local-only draft is optional */ }
  const selected = () => itemById[state.current] || DATA.items[0];
  const fieldValue = (payload, path) => fieldValues(payload)[path];
  const commonFields = item => { const a = fieldValues(item.reviewer_1.payload), b = fieldValues(item.reviewer_2.payload); return Object.keys(a).filter(path => stable(a[path]) === stable(b[path]) && !item.differing_fields.includes(path)); };
  let contextView = { zoom:1, mode:"select", spacePan:false, selection:null, drag:null };
  function mappingFor(item) { const value = item && item.context && item.context.coordinate_mapping; return value && value.status === "verified" ? value : null; }
  function validBBox(value) { return Array.isArray(value) && value.length === 4 && value.every(Number.isFinite) && value[0] < value[2] && value[1] < value[3]; }
  function setItemDecisionSource(source) {
    syncFromDom(); const item = selected(); if (!item) return; const draft = state.items[item.item_id];
    item.differing_fields.forEach(path => { const value = clone(fieldValue(source === "reviewer-1" ? item.reviewer_1.payload : item.reviewer_2.payload, path)); draft.resolutions[path] = {source, value}; });
    if (!String(draft.rationale || "").trim()) draft.rationale = `人工查看指定 Context/Crop 后，确认本项目采用 ${source} 作为最终规范表示。`;
    if (!String(draft.observation || "").trim()) draft.observation = "人工对照两份 reviewer 结果与可视证据；未发现需要继续阻塞 Gold 定稿的可见语义差异。";
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (error) { /* export remains available */ }
    render(); document.getElementById("status").textContent = `本项全部差异已明确采用 ${source}；请在导出前复核 rationale/observation`;
  }
  function setManualResolution(draft, path, value) { if (!selected().differing_fields.includes(path)) return; draft.resolutions[path] = {source:"manual",value:clone(value),manual_text:JSON.stringify(value)}; }
  function useSelectedBBox() {
    syncFromDom(); const item = selected(); const bbox = contextView.selection; if (!item || !validBBox(bbox)) { document.getElementById("status").textContent = "请先在 Context 上框选有效 BBox"; return; }
    const draft = state.items[item.item_id];
    setManualResolution(draft,"bbox",bbox); setManualResolution(draft,"corrected_bbox",bbox); setManualResolution(draft,"bbox_source","corrected"); setManualResolution(draft,"bbox_coordinate_space","pdf-page-top-left-points-v1"); setManualResolution(draft,"bbox_verdict","adjusted");
    if (!String(draft.rationale || "").trim()) draft.rationale = "人工依据 Context 页面重新框选对象边界，并采用该 PDF-points BBox 作为最终规范定位。";
    if (!String(draft.observation || "").trim()) draft.observation = "人工检查原候选框与两份 reviewer 定位后，在冻结页面 render 上重新选取对象边界。";
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (error) { /* export remains available */ }
    render(); document.getElementById("status").textContent = "已把当前框选写入本项所有适用 BBox 差异字段";
  }
  function contextPoint(event, item) {
    const canvas = document.getElementById("context-overlay"), mapping = mappingFor(item); if (!canvas || !mapping) return null;
    const rect = canvas.getBoundingClientRect(), box = mapping.context_pdf_bbox; if (!rect.width || !rect.height || !validBBox(box)) return null;
    const x = Math.max(0,Math.min(rect.width,event.clientX-rect.left)), y = Math.max(0,Math.min(rect.height,event.clientY-rect.top));
    return [box[0]+x/rect.width*(box[2]-box[0]),box[1]+y/rect.height*(box[3]-box[1])];
  }
  function drawContext() {
    const item = selected(), image = document.getElementById("context-image"), canvas = document.getElementById("context-overlay"), stage = document.getElementById("context-stage"), mapping = mappingFor(item); if (!image || !canvas || !stage || !image.naturalWidth) return;
    const width = Math.max(1,Math.round(image.naturalWidth*contextView.zoom)), height = Math.max(1,Math.round(image.naturalHeight*contextView.zoom)); image.style.width=`${width}px`; image.style.height=`${height}px`; stage.style.width=`${width}px`; stage.style.height=`${height}px`; canvas.width=width; canvas.height=height; canvas.style.width=`${width}px`; canvas.style.height=`${height}px`;
    const ctx=canvas.getContext("2d"); ctx.clearRect(0,0,width,height); if (!mapping) return; const box=mapping.context_pdf_bbox;
    const draw=(bbox,color,dash,label)=>{ if(!validBBox(bbox))return; const x=(bbox[0]-box[0])/(box[2]-box[0])*width,y=(bbox[1]-box[1])/(box[3]-box[1])*height,w=(bbox[2]-bbox[0])/(box[2]-box[0])*width,h=(bbox[3]-bbox[1])/(box[3]-box[1])*height; ctx.save();ctx.strokeStyle=color;ctx.lineWidth=2;ctx.setLineDash(dash);ctx.strokeRect(x,y,w,h);ctx.fillStyle=color;ctx.font="12px sans-serif";ctx.fillText(label,x+4,Math.max(12,y+14));ctx.restore(); };
    draw(item.reviewer_1.payload.bbox,"#0284c7",[7,4],"R1"); draw(item.reviewer_2.payload.bbox,"#ea580c",[7,4],"R2"); draw(contextView.selection,"#7c3aed",[3,3],"当前框选");
  }
  function setupContextViewer(item) {
    const image=document.getElementById("context-image"),canvas=document.getElementById("context-overlay"),box=document.getElementById("context-box"); if(!image||!canvas||!box)return; contextView.selection=null; contextView.drag=null; contextView.zoom=1; contextView.mode="select"; box.className="context-box mode-select";
    image.addEventListener("load",drawContext,{once:true}); if(image.complete) drawContext();
    canvas.addEventListener("pointerdown",event=>{ const point=contextPoint(event,item); if(!point)return; canvas.setPointerCapture(event.pointerId); if(contextView.mode==="pan"||contextView.spacePan){contextView.drag={kind:"pan",x:event.clientX,y:event.clientY,left:box.scrollLeft,top:box.scrollTop};canvas.classList.add("dragging");}else{contextView.drag={kind:"select",start:point};contextView.selection=[point[0],point[1],point[0],point[1]];drawContext();} });
    canvas.addEventListener("pointermove",event=>{ const point=contextPoint(event,item),readout=document.getElementById("bbox-pointer"); if(point&&readout)readout.textContent=`鼠标 PDF points：x=${point[0].toFixed(3)}，y=${point[1].toFixed(3)}`; if(!contextView.drag)return; if(contextView.drag.kind==="pan"){box.scrollLeft=contextView.drag.left-(event.clientX-contextView.drag.x);box.scrollTop=contextView.drag.top-(event.clientY-contextView.drag.y);}else if(point){const start=contextView.drag.start;contextView.selection=[Math.min(start[0],point[0]),Math.min(start[1],point[1]),Math.max(start[0],point[0]),Math.max(start[1],point[1])];drawContext();const out=document.getElementById("bbox-selection");if(out)out.textContent=`当前框选：x0=${contextView.selection[0].toFixed(3)}, y0=${contextView.selection[1].toFixed(3)}, x1=${contextView.selection[2].toFixed(3)}, y1=${contextView.selection[3].toFixed(3)}`;} });
    const finish=event=>{if(contextView.drag){contextView.drag=null;canvas.classList.remove("dragging");try{canvas.releasePointerCapture(event.pointerId);}catch(_){}}}; canvas.addEventListener("pointerup",finish);canvas.addEventListener("pointercancel",finish);
    document.getElementById("zoom-in").addEventListener("click",()=>{contextView.zoom=Math.min(4,contextView.zoom*1.25);drawContext();}); document.getElementById("zoom-out").addEventListener("click",()=>{contextView.zoom=Math.max(.1,contextView.zoom/1.25);drawContext();}); document.getElementById("zoom-reset").addEventListener("click",()=>{contextView.zoom=1;drawContext();});
    document.getElementById("mode-select").addEventListener("click",()=>{contextView.mode="select";box.className="context-box mode-select";}); document.getElementById("mode-pan").addEventListener("click",()=>{contextView.mode="pan";box.className="context-box mode-pan";}); document.getElementById("use-bbox").addEventListener("click",useSelectedBBox);
  }
  const tableCellCompare = (item, side) => {
    const left = item.reviewer_1.payload.details || {}, right = item.reviewer_2.payload.details || {};
    const rowCount = Math.max(Number.isInteger(left.row_count) ? left.row_count : 0, Number.isInteger(right.row_count) ? right.row_count : 0);
    const columnCount = Math.max(Number.isInteger(left.column_count) ? left.column_count : 0, Number.isInteger(right.column_count) ? right.column_count : 0);
    if (rowCount < 1 || columnCount < 1) return `<span class="muted">无法在页面重建 cell 网格；后端会拒绝非法尺寸。</span>`;
    const lookup = cells => Object.fromEntries((Array.isArray(cells) ? cells : []).map(cell => [`${cell.row}:${cell.column}`, cell.text]));
    const a = lookup(left.cells), b = lookup(right.cells), own = side === "reviewer-1" ? a : b, other = side === "reviewer-1" ? b : a, label = side === "reviewer-1" ? "R1" : "R2";
    const cards = [];
    for (let row = 0; row < rowCount; row += 1) for (let column = 0; column < columnCount; column += 1) {
      const key = `${row}:${column}`, ownValue = Object.prototype.hasOwnProperty.call(own, key) ? own[key] : null, otherValue = Object.prototype.hasOwnProperty.call(other, key) ? other[key] : null, different = stable(ownValue) !== stable(otherValue);
      cards.push(`<div class="cell-compare-card ${different ? "diff" : ""}"><div class="cell-compare-label">r${row}, c${column}${different ? " · 差异" : ""}</div><div class="cell-compare-value"><b>${label}</b> ${escapeHtml(pretty(ownValue))}</div></div>`);
    }
    return `<div class="cell-compare" style="--cell-columns:${Math.min(4, Math.max(1, columnCount))}">${cards.join("")}</div>`;
  };
  const isDone = item => { const draft = state.items[item.item_id] || {}; return item.differing_fields.every(path => draft.resolutions && draft.resolutions[path] && draft.resolutions[path].source) && !!String(draft.rationale || "").trim() && !!String(draft.observation || "").trim(); };
  function syncFromDom() {
    const item = selected(); if (!item) return;
    const draft = state.items[item.item_id] || (state.items[item.item_id] = {resolutions:{},rationale:"",observation:""});
    item.differing_fields.forEach(path => {
      const select = document.querySelector(`[data-resolution="${CSS.escape(path)}"]`); const manual = document.querySelector(`[data-manual="${CSS.escape(path)}"]`);
      if (select) { const source = select.value; draft.resolutions[path] = {source, value: source === "reviewer-1" ? clone(fieldValue(item.reviewer_1.payload,path)) : source === "reviewer-2" ? clone(fieldValue(item.reviewer_2.payload,path)) : null}; }
      if (manual && draft.resolutions[path] && draft.resolutions[path].source === "manual") draft.resolutions[path].manual_text = manual.value;
    });
    const rationale = document.getElementById("rationale"), observation = document.getElementById("observation"); if (rationale) draft.rationale = rationale.value; if (observation) draft.observation = observation.value;
    document.querySelectorAll("[data-declaration]").forEach(box => { state.declarations[box.dataset.declaration] = box.checked; });
  }
  function renderList() {
    document.getElementById("header-count").textContent = DATA.items.length; const done = DATA.items.filter(isDone).length; document.getElementById("progress-bar").style.width = `${done / Math.max(1, DATA.items.length) * 100}%`; document.getElementById("progress-text").textContent = `已填写 ${done}/${DATA.items.length}；所有差异字段和文字说明均需完成`;
    document.getElementById("item-list").innerHTML = DATA.items.map(item => `<button class="${item.item_id===state.current?"active ":""}${isDone(item)?"done":""}" data-item="${escapeHtml(item.item_id)}">${escapeHtml(item.item_id)}<small>${escapeHtml(item.kind_candidate)} · ${item.differing_fields.length} 个差异字段</small></button>`).join("");
    document.querySelectorAll("[data-item]").forEach(button => button.addEventListener("click", () => { syncFromDom(); state.current = button.dataset.item; render(); }));
  }
  function renderItem() {
    const item = selected(); if (!item) return; const draft = state.items[item.item_id] || {resolutions:{},rationale:"",observation:""}; const r1 = fieldValues(item.reviewer_1.payload), r2 = fieldValues(item.reviewer_2.payload); const diff = new Set(item.differing_fields);
    const rows = Object.keys(r1).map(path => { const isDiff = diff.has(path); const current = draft.resolutions && draft.resolutions[path] || {}; const manualText = current.source === "manual" ? (current.manual_text !== undefined ? current.manual_text : pretty(current.value)) : ""; const tableField = path === "details.cells" && item.kind_candidate === "table"; const leftMarkup = tableField ? tableCellCompare(item, "reviewer-1") : `<div class="value">${escapeHtml(pretty(r1[path]))}</div>`; const rightMarkup = tableField ? tableCellCompare(item, "reviewer-2") : `<div class="value">${escapeHtml(pretty(r2[path]))}</div>`; const reason = item.comparison_reasons && item.comparison_reasons[path]; const reasonMarkup = reason ? `<br><small>Profile 判定：${escapeHtml(reason.reason || "substantive_difference")}</small>` : ""; return `<tr class="${isDiff ? "diff" : ""}"><td><code>${escapeHtml(path)}</code>${isDiff ? "<br><small>必须人工确认</small>" : "<br><small>共同字段，只读</small>"}${reasonMarkup}</td><td>${leftMarkup}</td><td>${rightMarkup}</td><td>${isDiff ? `<select data-resolution="${escapeHtml(path)}"><option value="">请选择最终值来源</option><option value="reviewer-1" ${current.source === "reviewer-1" ? "selected" : ""}>采用 reviewer-1</option><option value="reviewer-2" ${current.source === "reviewer-2" ? "selected" : ""}>采用 reviewer-2</option><option value="manual" ${current.source === "manual" ? "selected" : ""}>人工修正</option><option value="unresolved" ${current.source === "unresolved" ? "selected" : ""}>暂不确定（保持暂停）</option></select><textarea class="manual" data-manual="${escapeHtml(path)}" placeholder="仅选择“人工修正”时填写合法 JSON。字符串必须带双引号；数组/对象按 JSON 填写；不要无意改写空格、标点、连字符或浮点值。">${escapeHtml(manualText)}</textarea>` : `<span class="muted">${escapeHtml(pretty(r1[path]))}</span>`}</td></tr>`; }).join("");
    const declarations = ["qualified_adjudicator","reviewed_specified_context_crop","reviewed_two_reviewer_results","did_not_view_predictions","did_not_view_split_or_scores","independent_from_reviewers","independent_from_proposer","not_evaluator"];
    document.getElementById("content").innerHTML = `<h2>${escapeHtml(item.item_id)}</h2><div class="muted">Context/Crop 仅来自已锁定 reviewer workbench；item/task/input hashes 在后端复核，浏览器不产生信任哈希。</div><div class="quick-actions"><strong>整项快捷确认：</strong><button id="accept-r1-all">本项全部采用 reviewer-1</button><button id="accept-r2-all">本项全部采用 reviewer-2</button><span class="muted">只在查看 Context/Crop 后使用；工具不会自动点击。</span></div><div class="visuals"><div class="visual"><h3>Context / page render（候选框叠加；原始 render 不变）</h3><div class="context-toolbar"><button id="mode-select">框选 BBox</button><button id="mode-pan">平移拖动</button><button id="zoom-out">缩小</button><button id="zoom-in">放大</button><button id="zoom-reset">100%</button></div><div class="context-box mode-select" id="context-box"><div class="context-stage" id="context-stage"><img id="context-image" src="${escapeHtml(item.context.asset)}" alt="Context"><canvas id="context-overlay"></canvas></div></div><div id="bbox-pointer" class="bbox-readout">鼠标 PDF points：将指针移到页面上读取 x / y</div><div id="bbox-selection" class="bbox-readout">当前框选：尚未选择</div><div class="actions"><button id="use-bbox">将当前框选写入适用的 BBox 差异字段</button></div></div><div class="visual"><h3>Crop（只读，可独立滚动）</h3><div class="crop-box"><img src="${escapeHtml(item.crop.asset)}" alt="Crop"></div></div></div><div class="compare"><table><thead><tr><th>字段</th><th>reviewer-1</th><th>reviewer-2</th><th>Gold 最终值来源 / 人工值</th></tr></thead><tbody>${rows}</tbody></table></div><div class="form-grid"><div class="form-card"><h3>Gold Curator rationale（必填）</h3><textarea id="rationale" placeholder="说明为何依据指定 Context/Crop 采用此最终值；不要只填 none / n/a">${escapeHtml(draft.rationale || "")}</textarea></div><div class="form-card"><h3>Gold Curator observation（必填）</h3><textarea id="observation" placeholder="记录在指定 Context/Crop 中实际观察到的依据，以及两份 reviewer 表示为何可接受或为何需要修正">${escapeHtml(draft.observation || "")}</textarea></div></div><div class="form-card" style="margin-top:10px"><h3>Gold Curator 事实声明（全部勾选后才可导入）</h3><div class="checks">${declarations.map(key => `<label><input type="checkbox" data-declaration="${key}" ${state.declarations[key]===true?"checked":""}>${escapeHtml(key)}</label>`).join("")}</div></div><div class="actions"><button id="prev-button">上一项</button><button id="next-button">下一项</button><button class="primary" id="save-current">保存本项</button><button class="primary" id="export-button">导出 Gold Review submission</button></div>`;
    setupContextViewer(item);
    document.getElementById("accept-r1-all").addEventListener("click", () => setItemDecisionSource("reviewer-1")); document.getElementById("accept-r2-all").addEventListener("click", () => setItemDecisionSource("reviewer-2"));
    item.differing_fields.forEach(path => { const select = document.querySelector(`[data-resolution="${CSS.escape(path)}"]`); const manual = document.querySelector(`[data-manual="${CSS.escape(path)}"]`); if (select) select.addEventListener("change", () => { syncFromDom(); renderList(); }); if (manual) manual.addEventListener("input", () => { syncFromDom(); }); });
    document.getElementById("rationale").addEventListener("input", syncFromDom); document.getElementById("observation").addEventListener("input", syncFromDom); document.querySelectorAll("[data-declaration]").forEach(box => box.addEventListener("change", syncFromDom));
    document.getElementById("prev-button").addEventListener("click", () => { syncFromDom(); const index = DATA.items.findIndex(row => row.item_id === item.item_id); state.current = DATA.items[(index + DATA.items.length - 1) % DATA.items.length].item_id; render(); });
    document.getElementById("next-button").addEventListener("click", () => { syncFromDom(); const index = DATA.items.findIndex(row => row.item_id === item.item_id); state.current = DATA.items[(index + 1) % DATA.items.length].item_id; render(); });
    document.getElementById("save-current").addEventListener("click", () => saveDraft("本项已保存")); document.getElementById("export-button").addEventListener("click", exportSubmission);
  }
  function render() { renderList(); renderItem(); document.getElementById("manifest-short").textContent = MANIFEST_SHA256.slice(0,12); }
  function saveDraft(message) { syncFromDom(); try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); document.getElementById("status").textContent = message || "草稿已保存到本机 localStorage"; } catch (error) { document.getElementById("status").textContent = "localStorage 保存失败，请使用导出文件"; } renderList(); }
  function buildSubmission() { syncFromDom(); const items = DATA.items.map(item => { const draft = state.items[item.item_id] || {}; const resolutions = {}; item.differing_fields.forEach(path => { const resolution = draft.resolutions && draft.resolutions[path]; if (!resolution) resolutions[path] = {source:"",value:null}; else if (resolution.source === "manual") { let value; try { value = JSON.parse(resolution.manual_text || ""); } catch (error) { throw new Error(`${item.item_id} / ${path} 的人工值不是合法 JSON`); } resolutions[path] = {source:"manual",value}; } else resolutions[path] = {source:resolution.source || "",value:resolution.source === "reviewer-1" ? clone(fieldValue(item.reviewer_1.payload,path)) : resolution.source === "reviewer-2" ? clone(fieldValue(item.reviewer_2.payload,path)) : null}; }); return {item_id:item.item_id,item_sha256:item.item_sha256,review_task_id:item.review_task_id,input_hashes:clone(item.input_hashes),resolutions,rationale:draft.rationale || "",observation:draft.observation || ""}; }); return {schema_version:"__ADJUDICATION_SCHEMA__",protocol:"__ADJUDICATION_PROTOCOL__",compiler_version:"__ADJUDICATION_COMPILER__",adjudication_manifest_sha256:MANIFEST_SHA256,workbench_instance_id:DATA.workbench_instance_id,workpack_identity:clone(DATA.workpack_identity),source_submissions:clone(DATA.source_submissions),dispute_set_sha256:DATA.dispute_set_sha256,adjudicator:clone(DATA.adjudicator),declarations:clone(state.declarations),items,exported_at:new Date().toISOString()}; }
  function exportSubmission() { try { const value = buildSubmission(); const blob = new Blob([JSON.stringify(value,null,2)+"\n"], {type:"application/json"}); const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = "gold-review-submission.json"; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 0); document.getElementById("status").textContent = "已导出 Gold Review submission；后端仍会重新验证全部 identity/hash/type"; } catch (error) { document.getElementById("status").textContent = error.message; } }
  function restoreSubmission(value) { if (!value || value.schema_version !== "__ADJUDICATION_SCHEMA__" || value.protocol !== "__ADJUDICATION_PROTOCOL__" || value.compiler_version !== "__ADJUDICATION_COMPILER__" || value.adjudication_manifest_sha256 !== MANIFEST_SHA256 || value.workbench_instance_id !== DATA.workbench_instance_id || value.dispute_set_sha256 !== DATA.dispute_set_sha256 || stable(value.source_submissions) !== stable(DATA.source_submissions) || stable(value.workpack_identity) !== stable(DATA.workpack_identity) || stable(value.adjudicator) !== stable(DATA.adjudicator)) throw new Error("submission identity/hash 与当前 Gold Review workbench 不一致"); const expected = new Set(DATA.items.map(item => item.item_id)); const incoming = new Set((value.items || []).map(item => item.item_id)); if (expected.size !== incoming.size || [...expected].some(item => !incoming.has(item))) throw new Error("submission dispute set 不完整或含未知项目"); const next = emptyState(); next.declarations = value.declarations || {}; (value.items || []).forEach(row => { const item = itemById[row.item_id]; if (!item || stable(row.item_sha256) !== stable(item.item_sha256) || stable(row.review_task_id) !== stable(item.review_task_id) || stable(row.input_hashes) !== stable(item.input_hashes) || Object.keys(row.resolutions || {}).some(path => !item.differing_fields.includes(path))) throw new Error(`submission item binding invalid: ${row.item_id}`); next.items[row.item_id] = {resolutions:clone(row.resolutions || {}),rationale:row.rationale || "",observation:row.observation || ""}; }); state = next; saveDraft("已导入并恢复同一 Gold Review workbench 的草稿"); render(); }
  document.getElementById("save-button").addEventListener("click", () => saveDraft("草稿已保存到本机 localStorage")); document.getElementById("import-button").addEventListener("click", () => document.getElementById("import-file").click()); document.getElementById("import-file").addEventListener("change", event => { const file = event.target.files && event.target.files[0]; if (!file) return; const reader = new FileReader(); reader.onload = () => { try { restoreSubmission(JSON.parse(reader.result)); } catch (error) { document.getElementById("status").textContent = `导入拒绝：${error.message}`; } }; reader.readAsText(file); });
  render();
})();
</script>
</body>
</html>
'''


def render_adjudication_html(
    data: Mapping[str, Any],
    manifest_sha256: str = "__ADJUDICATION_MANIFEST_SHA256__",
    *,
    portable_gold_review: bool = False,
) -> str:
    # ``portable_gold_review`` is an explicit call-site marker.  The portable
    # labels and controls are also safe for legacy adjudication workbenches, so
    # one HTML implementation remains sufficient and legacy submissions keep
    # their existing wire schema.
    del portable_gold_review
    serialized = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    serialized = serialized.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (ADJUDICATION_HTML_TEMPLATE.replace("__ADJUDICATION_DATA__", serialized)
            .replace("__ADJUDICATION_MANIFEST_SHA256__", manifest_sha256)
            .replace("__ADJUDICATION_SCHEMA__", VISUAL_REVIEW_ADJUDICATION_SUBMISSION_SCHEMA)
            .replace("__ADJUDICATION_PROTOCOL__", VISUAL_REVIEW_ADJUDICATION_PROTOCOL)
            .replace("__ADJUDICATION_COMPILER__", VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION))


def render_portable_gold_review_usage_instructions(instance_name: str, curator_id: str, item_count: int) -> str:
    return f'''# Portable Gold 人工审查使用说明

这是 `{instance_name}` 的离线 Gold Curator 工具，分配给 **{curator_id}**。它把两份已经完成的独立 reviewer 提交、冻结的 Context/Crop 和 Review Convention Profile 比较结果放在同一页面里，供人工形成唯一、可追溯的 Gold 规范值。

## 1. 这一步做什么、不做什么

- 页面只列出 profile receipt 仍认定需要人工确认的字段；机器发现的差异只是提醒，不等于两种写法在语义上必然不同。
- 你必须查看 Context/Crop 后，明确采用 reviewer-1、reviewer-2、人工修正，或选择“暂不确定”。工具不会投票、平均 BBox、合并文本或自动把一致意见写成 Gold。
- 页面不含 prediction、score、split label、model confidence、OCR 输出或隐藏答案。Curator 不能同时充当本轮 reviewer、7D.5A proposer 或 benchmark evaluator。
- 本工具不修改两份原 submission；最终 revision 另存为新的私有、不可变目录。

## 2. 最省时的填写方式

先查看左侧 Context 与右侧 Crop。若本项目所有差异字段都应采用同一 reviewer，点击“本项全部采用 reviewer-1/2”；按钮只是批量填写，仍需你复核自动补入的 rationale/observation。若只有个别字段不同，在表格中逐行选择。

表格的 `details.cells` 会以网格显示两份结果，通常直接选择完整的 reviewer-1 或 reviewer-2 即可，无需重录数十个单元格。只有两份都不正确时才选择“人工修正”，并输入完整、合法 JSON。

## 3. BBox 工具

`BBox` 是 `[x0, y0, x1, y1]`：`x0/y0` 为左上角，`x1/y1` 为右下角，单位是原 PDF 页面 points，原点在页面左上角。

在 Context 上：

1. 用“放大/缩小/100%”调整比例；滚动条与“平移拖动”用于移动页面。
2. 选择“框选 BBox”，从对象左上角拖到右下角；鼠标位置和当前框选会实时显示 PDF points。
3. 点击“将当前框选写入适用的 BBox 差异字段”。只有当前项目确实存在相应差异字段时才会写入；原始 render 不会改变。

如果差异列表同时包含 `bbox` 和 `corrected_bbox`，它们必须表示同一个最终矩形：采用 reviewer 值时两行选择同一 reviewer；人工框选时让页面把同一数组同时写入两行。不要混用两个来源。

不要把 caption、表号或图号是否纳入框内当作个人偏好。沿用本 workpack 的 Profile 约定：定位对象主体；caption/number 保存在各自语义字段。若证据确实无法判断，选择“暂不确定”，让流程保持暂停。

## 4. 人工修正 JSON

- 字符串：`"文本"`
- 数字：`12.5`
- BBox：`[x0, y0, x1, y1]`
- 数组/对象：必须是完整 JSON，键名和字符串使用双引号。
- 公式字段使用未包裹的 LaTeX body，不添加 `$$...$$`、`\\[...\\]` 或其他 Markdown delimiters。

`rationale` 说明为什么采用该最终值，`observation` 写你在指定 Context/Crop 中实际看到了什么。不要用 `none`、`n/a` 等占位词代替事实说明。

## 5. 保存、导入与导出

“保存草稿”只写本机 `localStorage`。工具也可导入此前从同一 workbench 导出的 `gold-review-submission.json`；manifest、workpack、两份 submission、profile receipt、dispute set 或操作者身份任一不一致都会拒绝恢复。

填写完成后导出 `gold-review-submission.json`，再由操作者运行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/reviewer_workbench.py gold-review-finalize \\
  --workbench /absolute/path/to/{instance_name} \\
  --reviewer-instance /absolute/path/to/reviewer-instance \\
  --reviewer-submission /absolute/path/to/reviewer-1.json \\
  --reviewer-submission /absolute/path/to/reviewer-2.json \\
  --gold-review-submission /absolute/path/to/gold-review-submission.json \\
  --comparison-receipt /absolute/path/to/review-convention-comparison-receipt.json \\
  --workpack /absolute/path/to/phase7d5a \\
  --output /absolute/path/to/new-gold-review-revision
```

后端会重新验证全部 identity、hash、字段类型、表格形状、BBox 范围、独立性声明和 profile receipt。只有所有项目都已明确解决时才生成 {item_count} 行 import-compatible `gold-fragments.jsonl`；同时保留 `gold-decisions.jsonl` 与 `gold-review-revision.json` 供以后审查和修订。该 revision 仍明确为 `verified_gold=false`、`promotion_allowed=false`、`release_included=false`，不会绕过后续 benchmark/resource 门禁。
'''


def render_adjudication_usage_instructions(instance_name: str, adjudicator_id: str) -> str:
    return f'''# 独立裁决使用说明

这是 `{instance_name}` 的离线 Phase 7D.5B adjudication workbench，分配给 **{adjudicator_id}**。本目录只包含当前冻结争议项目的 Context/Crop 和两份 reviewer 的 scorable 结果；没有原 PDF、split label、prediction、score、model confidence、OCR 结果或隐藏答案。

`adjudicator_id` 是操作者分配给真实人员的稳定匿名角色标签，不是密码、数字签名或身份认证；同一人跨轮可以继续使用同一 `adjudicator_id`。`session_id` 标识特定的 workpack + dispute set + 轮次；workpack、dispute set 或轮次改变时必须使用新 session。推荐格式为 `external-adjudicator-1` 与 `adj-7d5b-<workpack8>-<dispute8>-r1`，但工具不能伪造真实人员身份或替操作者生成这些值。

在进入正式 adjudication 前，先对两份 reviewer submission 运行 `review-convention-compare`。该步骤绑定 Review Convention Profile v0.2 的版本/hash，并按字段区分规范表示差异与 substantive disagreement；只有 receipt 中 `remaining_substantive_disagreements` 才能进入后续人工裁决。比较 receipt 不是 Gold，也不会填写答案、选择 reviewer 值或生成 adjudicator 身份；profile/hash drift 会 fail-closed。

裁决者可以看到两份 reviewer 结果，但不能是 reviewer-1、reviewer-2、7D.5A proposer 或 benchmark evaluator。每个实际差异字段必须明确选择 reviewer-1、reviewer-2、manual 或 unresolved；页面不预选、不自动合并。人工值按 JSON 输入，真实空格、标点、连字符和 BBox 浮点值不会被页面规范化。

每个项目都要依据指定 Context/Crop 填写 rationale 和 observation，并完成全部事实声明。保存按钮写入本机 `localStorage`；“导入裁决 submission”只接受与本 workbench 的 manifest、workpack、两份原 submission、dispute set 和 item/input hashes 完全一致的文件。

操作者完成后运行：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/reviewer_workbench.py adjudication-finalize \\
  --workbench /absolute/path/to/{instance_name} \\
  --reviewer-instance /absolute/path/to/verified-reviewer-instance \\
  --reviewer-submission /absolute/path/to/reviewer-1.json \\
  --reviewer-submission /absolute/path/to/reviewer-2.json \\
  --adjudication-submission /absolute/path/to/adjudication-submission.json \\
  --workpack /absolute/path/to/tmp/workspace/output/phase7d5a \\
  --output /absolute/path/to/new-adjudication-revision
```

不完整、unresolved、身份重合、字段未知、值类型/形状不合法或任何 hash drift 都不会产生 Gold；完整合法裁决也只产生 `verified_gold=false`、`promotion_allowed=false`、`release_included=false` 的私有 candidate revision，之后仍须现有独立 Gold/resource handoff 门禁。
'''


def render_usage_instructions(
    instance_name: str,
    reviewer_id: str,
    profile_identity: Mapping[str, Any] | None = None,
    *,
    item_count: int = EXPECTED_ITEM_COUNT,
    portable_review: bool = False,
) -> str:
    profile_identity = dict(profile_identity or {"profile_id": "legacy-unbound", "profile_version": "legacy", "profile_sha256": "unbound"})
    pagination_note = "本工作包中的每个物理页就是一页内容，不做左右页拆分，也没有 region 字段；分页由上游自动完成，人工只审查公式、图、表和图表候选。" if portable_review else "本工具不要求 reviewer 修改分页；人工只审查当前冻结的视觉候选。"
    return f'''# 人工 Review 使用说明

这是 `{instance_name}` 的离线视觉 reviewer 实例，分配给 reviewer **{reviewer_id}**。本目录只包含完成人工标注所需的 Context 页面 render、Crop、只读事实和本地页面；没有复制原始源文件，也没有隐藏答案。

{pagination_note}

本工具审核的是“已检测候选是否正确”，不会自动证明未列入工作包的页面没有漏检公式/图/表/图表，因此本轮不能单独形成整段页面的 recall 结论。若后续需要完整 detection recall Gold，必须另加逐页漏检登记，而不是补造候选。

## 0. 开始前确认 Review Convention Profile

- 当前 profile：`{profile_identity.get("profile_id")}` v`{profile_identity.get("profile_version")}`。
- 当前 profile SHA-256：`{profile_identity.get("profile_sha256")}`。它绑定在本实例的 `reviewer-workpack.json`、`workbench-manifest.json`、`review.html` 和后续 profile-aware comparison receipt 中；开始审阅前请核对页面顶部的版本和哈希。
- profile 改变会使旧 workbench/submission 变为 stale；本工具不会静默迁移或改写旧 submission。需要显式 comparison receipt 或 migration，并对受影响字段重新确认；旧 v0.1 submission 的 raw 字段必须保留。

本轮 profile 的最小规则：Caption 的规范内容不含前导编号，编号单独填写；只有编号字段与 Caption 前缀逐字匹配且中间仅有 ASCII 空格/Tab 时，比较 receipt 才能派生 canonical caption。例如：`Table 16.2 Parameters for AT operation of the ITER experiment` 应记录 `table_number=Table 16.2`、`caption=Parameters for AT operation of the ITER experiment`。不能猜测或宽松删除前缀。

BBox 范围按对象类型固定：Table 包含表体、表头和边框，排除外部 table number、Caption、脚注和上下文正文；Chart 包含绘图区、轴、刻度、轴标签、legend 和图内 title，排除外部 Caption/figure number；Figure 包含图像主体及图内标注，排除外部 Caption/figure number；Equation 只保守包含展示公式主体及确实属于公式对象的可见符号/编号，排除周围正文和外部 Caption。`raw transcription` 必须原样保留；`canonical comparison` 仅允许 Unicode NFC、换行统一、每行首尾空白裁剪和已证明的编号拆分。禁止 NFKC、大小写折叠、自动纠正、技术标识符/公式/单位模糊替换、标点/连字符模糊替换；`2` 与 `2.0` 仍是不同 raw 文本，`Abingdon` 与 `Abinglon`、`TEXTOR94` 与 `TEXTOR84` 仍是不同标识符。

## 1. 资格与独立性

- 你应具备阅读本项目技术图、表、公式或图表的能力，并由操作者分配唯一 reviewer ID。
- 你不能是 7D.5A candidate/workpack 的 preparation proposer，也不能是 benchmark evaluator；table 和 chart 项目必须由两名不同的独立 reviewer 分别提交，figure 和 equation 至少一名独立 reviewer。
- 只能根据本实例中指定的 source identity、Context render 和 Crop 作判断。不要打开其他推理结果、模型输出、模型置信度、评分结果或任何预先填写的答案。本页面没有这些内容。

### reviewer ID 如何生成和分配

`reviewer ID` 不是浏览器随机生成的，也不是 reviewer 在 HTML 页面里填写的；它是由操作者在生成 reviewer 实例时分配的、用于区分独立人工 reviewer 的稳定标签。它不是密码、数字签名或身份认证，只用于把提交结果绑定到正确的 reviewer 槽位。

推荐使用不含姓名和隐私信息的简单编号，例如：

```text
external-reviewer-1
external-reviewer-2
```

规则如下：

- 一个真实 reviewer 只能使用一个 ID；不同 reviewer 必须使用不同 ID。不能把两个文件夹都交给不同的人却仍使用 `external-reviewer-1`。
- ID 只能使用 ASCII 字母、数字、`.`、`_`、`-`，首字符必须是字母或数字，长度不超过 80；不要使用空格、中文、斜杠或邮箱地址。
- ID 不能等于 preparation proposer、evaluator 或项目内部自动生成 agent 的 ID。
- 文件夹名（例如 `reviewer-1-final`）不是 reviewer ID；页面顶部显示的 ID 才是最终绑定值。不要手工修改 JSON 中的 ID。

操作者在本机为第一位 reviewer 生成实例：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/reviewer_workbench.py generate \
  --workpack /absolute/path/to/tmp/workspace/output/phase7d5a \
  --output /absolute/path/to/reviewer-1-final \
  --reviewer-id external-reviewer-1 \
  --review-session-id session-reviewer-1
```

第二位独立 reviewer 使用不同的 ID 和输出目录：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/reviewer_workbench.py generate \
  --workpack /absolute/path/to/tmp/workspace/output/phase7d5a \
  --output /absolute/path/to/reviewer-2-final \
  --reviewer-id external-reviewer-2 \
  --review-session-id session-reviewer-2
```

生成命令返回的 `reviewer_instance`、`workbench_instance_id` 和 `workpack_sha256` 应记录在交接表中。若 reviewer ID 分配错了，应删除/隔离该空实例并用正确 ID 重新生成；不要修改已经生成的 `reviewer-workpack.json` 或 `workbench-manifest.json`。

## 2. 如何打开

1. 在本机文件管理器中进入本目录。
2. 双击 `review.html`，或在浏览器地址栏打开这个本地文件；不需要启动服务器，不需要联网。
3. 先确认页面顶部的 reviewer ID 与操作者分配的一致。页面显示物理页、候选 BBox、source/render/crop SHA-256 等事实，但不显示 calibration/test 分层。

### Context 放大、平移和框选的实际操作

- 工具栏的“框选 BBox”和“平移页面”是两个互斥模式；页面会在“当前模式”处明确显示当前模式。需要采集 `corrected_bbox` 时先点“框选 BBox”；只想查看页面其他区域时点“平移页面”。
- “放大 + / 缩小 −”只改变 Context viewport 的显示比例；“适合窗口”让整页尽量落入可视区；“100%”回到原始 render 的 1:1 CSS 显示比例；“重置视图”回到适合窗口并恢复“框选 BBox”模式。缩放范围有限，页面显示的缩放百分比不会改变 PDF points。
- 平移页面模式下，在 Context 内按住鼠标左键/指针拖动只移动页面，绝不会创建或改写临时框。框选模式下，按住左键/指针拖动才会生成紫色临时框。按住 `Space` 可临时进入平移，松开后恢复原模式；不要在输入框中按 Space 代替输入空格。
- 正确框选步骤：点“框选 BBox” → 用“放大 +”放大到能看清边缘 → 如需移动视野先点“平移页面”或按住 `Space` 拖动 → 再切回“框选 BBox” → 在对象左上到右下（或反方向）拖出矩形 → 检查紫色虚线与 `x0/y0/x1/y1` 读数 → 点“采用为修正 BBox”。原橙色候选框和新绿色修正框会同时保留。
- `Esc` 只清除尚未采用的紫色临时框，不会清除绿色 `corrected_bbox`，也不会重置缩放/平移。过小框会提示并丢弃，不会写入结构化结果。
- 如果看到“坐标映射不可验证”，仍可查看、放大和移动页面，但不能框选；请改用 `uncertain`、`unreadable` 或 `needs_adjudication`，不要把屏幕像素当 PDF points 手工填写。若无法拖框，先检查是否处于“框选 BBox”；若页面跟手移动而没有紫框，说明仍在“平移页面”模式。

## 3. 页面结构、通用字段与填写依据

先看左侧 Context，再看右侧 Crop。候选 BBox 是只读事实；如果边界不对，在 Context 上拖拽并采用 `corrected_bbox`，再选择 `too_large`、`too_small` 或 `wrong`。可选补充说明不能代替结构化坐标。

- **Equation**：填写存在性、公式类型、完整 LaTeX/转录、公式编号、是否跨行，并逐项检查下标、上标和特殊符号。选择 `transcription_completeness=complete` 时必须有裸 LaTeX；看不清时选择“看不清”或“不确定”，不要猜。
- **Figure**：填写存在性、图类型、图号、完整性、是否有子图和子图数量，并选择 `caption_presence`。Caption 规范内容不含前导图号；编号单独填入 `figure_number`。文字框只抄录实际可见文字；明确没有才填 `none`，看不清时选择 `unreadable`，不要猜。本阶段不要求解释物理意义。
- **Table**：填写存在性、表类型、行列数、表头、Caption 是否存在、表号、合并单元格；`table_number` 单独记录可见表号，Caption 规范内容不含前导表号。行列数确定后生成可编辑网格，或使用批量粘贴一次写入全部 `cells`。真实空白 cell 保持为空字符串；整表不能全部为空，否则 canonical completion 会阻断。
- **Chart**：填写存在性、图表类型/标题、X/Y 轴标签、legend、系列数量/名称、可读到的数据点或曲线信息、Caption 是否存在。Chart 没有独立编号字段时，不要自行从 Caption 猜编号或删除前缀。标题/轴/legend/Caption 明确不存在时按字段说明填 `none`；读不清的刻度或系列必须标为不确定或看不清。

### 3.1 通用字段：每一项都要理解后再填写

以下字段位于每个项目的共同表单区。填写依据只能是当前项目的 Context 和 Crop；候选值、Item ID 或字段名称不能作为“答案”来反推。

| 页面字段 | 含义 | 如何填写 |
|---|---|---|
| **审阅状态** `review_status` | 这一项人工审阅的总体状态，不是模型状态，也不是 Gold 状态。 | `complete`：关键字段都能从图像中判断并记录；`uncertain`：有实质内容无法确定；`needs_adjudication`：需要另一位 reviewer/裁决者解决；`unreadable`：图像质量或裁剪导致无法可靠阅读；`candidate_incorrect`：候选类型或候选位置明确错误。后三类 unresolved 状态会阻断 Gold-ready；不能为了完成进度而选 `complete`。 |
| **Presence** `presence` | 候选类型对象是否确实存在。 | `present`：Context/Crop 中能看到该候选类型对象；`absent`：根据图像确认该处没有该对象或候选指向错误；`uncertain`：无法判断。若 `type_verdict=other/not_this_type`，应使用 `review_status=candidate_incorrect`、`presence=absent`、`bbox_verdict=not_applicable`，并填写差异观察；不能用 `absent` 表示“没有仔细看”。 |
| **候选/对象类型** `candidate/object type`、`type_verdict` | 实际看到的对象类型与候选类型的比较。 | 选当前候选类型表示一致；`other`/`not_this_type` 表示另一类或不是此类，此时应配合 `candidate_incorrect` + `absent` + `not_applicable`；`unknown` 表示无法可靠分类，不能与 `complete` 组合；不要依据物理意义猜类型。 |
| **BBox 判断** `bbox_verdict` | 候选矩形是否覆盖正确对象范围，以及是否需要修正。 | `correct`：锁定并采用候选框；`too_large`：候选框包含过多邻近内容；`too_small`：漏掉对象内容；`wrong`：指向错误位置；旧草稿的 `adjusted/incorrect` 仍可兼容。选择“对象不存在 `not_applicable`”时，页面会自动联动为 `candidate_incorrect + absent + 类型不匹配` 并清除修正框；已经明确选择 `other` 时保留，否则设为 `not_this_type`。导入旧 submission 也会执行同样联动。此时不要求填写类型专用字段，但仍须选择把握度并填写具体差异观察。除 `correct/not_applicable` 外，对象存在且要完成 Gold 时必须有有效 `corrected_bbox`，否则阻断 Gold-ready。 |
| **观察到的 BBox x0/y0/x1/y1** | Context 上人工观察到的对象矩形；页面现在用鼠标工具采集并写入 `corrected_bbox`，不是让 reviewer 手写数组。 | `x0（左）`、`y0（上）`、`x1（右）`、`y1（下）`；满足 `x1>x0`、`y1>y0`；单位为 PDF points，原点为页面左上角。只有“坐标映射：可验证”时才框选；若显示“坐标映射不可验证”，修正按钮会禁用，必须改用不确定状态，不得猜。无效框、越界框或过小框会阻断/拒绝完成。 |
| **人工审阅把握度** `confidence`、`reviewer_confidence` | 你对自己视觉判断的把握度，不是模型 confidence。 | `high/medium/low` 依据你是否看清对象、边界和专用字段填写；低把握度要说明原因，不能掩盖看不清。单独低 confidence 不必然阻断，但与 unresolved 或缺说明一起会阻断 Gold-ready。 |
| **可选补充说明** `notes` | 该项目的补充说明、异常或阅读约定。 | 可选填写，例如“右侧刻度被裁切”“图号可见但 Caption 模糊”；若填写必须是文字。不能粘贴模型答案或凭常识补全图像中没有的内容；不填写不会阻断 Finalize。 |
| **差异/无差异观察** `difference_observation` | 你独立观察与候选事实之间的差异记录，也是 attestation 的人工观察依据。 | 必须填写具体观察；不能留空，也不能只写 `none`、`N/A`、`na`、`null`、`-` 或 `无`。无差异示例：“无差异：对象存在，类型和候选 BBox 与页面一致”。若有差异，具体写出位置/字段，例如“候选框漏掉右侧分母”“图中有两个子图而候选只覆盖上半部分”。这条占位词限制只适用于本字段。 |

### 3.2 reviewer-facing 术语总表：是什么、何时填、是否阻断 Gold-ready

页面已经把关键英文术语放在中文主标签后面；下面的规则是 Finalize 的实际语义。任何术语看不清时都可以选择合法的不确定状态，但未解决状态会阻断 Gold-ready，不允许靠猜测绕过。

| 术语 | 中文含义和填写时机 | 不能猜什么、对 Gold-ready 的影响 |
|---|---|---|
| `complete` | 这一项的对象、类型、BBox 和该类型要求的字段都能从当前 Context/Crop 可靠判断。 | 不能因为进度需要而选；缺字段、无效 corrected_bbox 或冲突会阻断。进度只把满足这些 canonical 必填规则的项目计为完成。 |
| `uncertain` | 有实质内容无法确定，但仍可记录已观察到的部分。 | 不能把物理常识当作图像证据；会阻断 Gold-ready，直到后续独立 review/裁决解决。 |
| `needs_adjudication` | 两位 reviewer 或 reviewer 与事实之间需要裁决者处理。 | 不能自行选一个“看起来合理”的答案；会阻断。 |
| `unreadable` | 图像、文字、边缘或渲染质量不足以可靠阅读。 | 不能补写模糊符号/数字；会阻断。 |
| `candidate_incorrect` | 候选对象的类型或定位明确错误；`type_verdict=other/not_this_type` 时必须配合 `presence=absent` 和 `bbox_verdict=not_applicable`。直接选择 BBox 的“对象不存在 `not_applicable`”会由页面自动完成这组联动。 | 不能把“我没看懂”当作 candidate_incorrect；仍须选择把握度并填写具体差异观察，否则阻断。误检状态下不要求补填原候选类型的专用字段。 |
| `caption` / `caption_presence` | `caption` 是当前 Context 中实际可见的图注文字；`caption_presence` 表示有、无或看不清。 | `caption_presence` 必须选择；有且看得清才抄录文字，明确没有时文字框填 `none`，看不清时选 `unreadable`，文字框可以留空或记录 `unreadable`，但不能猜。不要按图号或常识编写图注。 |
| `transcription` / `transcription_latex` | Equation 中把看见的公式逐符号记录为转录/LaTeX。 | 不能从章节上下文补齐模糊符号；`transcription_completeness=complete` 却没有转录会阻断。 |
| `subfigure` / `has_subfigures` / `subfigure_count` | Figure 中明确的 `(a)`、`(b)` 等子图及可数出的数量。 | 不能把多个视觉区域自动当作子图；需要子图数量但数不清时应不确定并阻断。 |
| `merged cells` / `merged_cells` | Table 中跨行或跨列的合并单元格位置/说明。 | 不要依据表格习惯猜合并；结构字段缺失或单元格网格不完整会阻断。 |
| `legend` | Chart 中图例文字以及它与系列的对应关系。 | 不能用颜色/物理含义猜系列名称；看不清应写 `unreadable` 或说明。 |
| `series` / `series_count` / `series_names` | Chart 中独立绘制的数据系列数量和每个系列的可见名称。 | `series_count` 不是数据点数量；数量和名称不一致会阻断。没有名称但系列确实存在可写 `unnamed-series-1` 并说明。 |
| `attestation` | Finalize 生成的外部 reviewer 事实声明与每项审阅绑定，不是普通备注，也不是签名证明。 | 不能代勾声明；声明缺失、reviewer 不独立或未查看指定 render/crop 会暂停/拒绝。 |
| `Finalize` | 操作者在原始 canonical workpack 上重新校验并生成不可覆盖 revision 的本地 CLI 步骤。 | 浏览器按钮只导出 submission，不会自动 Gold-ready；缺项、哈希漂移、unresolved 或独立性失败会暂停/拒绝。 |
| `revision` | 一次固定的 review 输出目录，含 manifest、registry、attestation 和 Gold-compatible rows。 | 已生成 revision 不可覆盖；修改必须新建 revision，旧 revision 保持 immutable。 |

### 3.3 BBox 工具、坐标映射和双 overlay

1. Context 上的**橙色虚线**是冻结的候选 BBox，只读；**绿色实线**是已经点击“采用为修正 BBox”的 `corrected_bbox`；**紫色虚线**是当前拖拽但尚未采用的临时框。Crop 不会被修改或重新生成。
2. 鼠标移动时，页面把主读数显示为 `PDF points` 的 `x/y`，原点是页面左上角；render pixel 只作为次要调试读数。CSS 像素、`devicePixelRatio` 像素和 Crop 局部坐标都不能直接填写为 PDF 坐标。
3. 页面只有在冻结 metadata 提供可信的 canonical page width/height、Context 对应的 `context_pdf_bbox`（当前实例是可验证的整页 Context）、render naturalWidth/naturalHeight 和实际显示矩形时才启用工具。若看到“**坐标映射不可验证**”，工具会禁用，必须选择不确定/看不清；如需记录原因可写入可选补充说明，不能手工猜数字。
4. 在 Context 内容区域按下并拖动即可框选；从右下向左上拖（反向拖拽）也会自动归一化，框会限制在 canonical page bounds 内。拖得过小或没有面积的框会被提示且不会生成 `corrected_bbox`。按 Esc 或“清除当前框选”只清除紫色临时框。
5. 检查紫色框后点击“采用为修正 BBox”，它会把结构化 `corrected_bbox` 保存到本地草稿和导出 submission；橙色候选框仍保留。点击“重置修正 BBox”可清除绿色框。选择 `bbox_verdict=correct` 时，候选框被锁定，不能采用修正框；选择 `too_large`、`too_small` 或 `wrong` 时，若对象存在且状态不是 unresolved，必须成功采用一个有效修正框才能 Finalize。

### 3.4 四类最小填写示例

- **Equation**：看到一条独立成行、编号为 `(16.1)` 的公式，能读清所有符号：`presence=present`、`type_verdict=equation`、`bbox_verdict=correct`、`equation_type=display`、`formula_number=(16.1)`、`transcription_completeness=complete`，逐符号填写 `transcription_latex`，并检查 `subscripts/superscripts/special_symbols=checked`。看不清一个下标时不要补全，改为 `uncertain` 或 `unreadable`。
- **Figure**：看到完整示意图，图注可读且包含两个 `(a)/(b)` 子图：`presence=present`、`figure_type=diagram`、`completeness=complete`、`caption_presence=present`、填写实际 `caption`、`has_subfigures=yes`、`subfigure_count=2`。不要求解释物理意义。
- **Table**：看到 4 行 3 列、第一行是表头、左侧有跨两行单元格：填写 `row_count=4`、`column_count=3`、`header_text`、`merged_cells` 的实际位置，点击生成 `cells` 网格或粘贴 4×3 数据；真实空白格的 `text` 保持为空字符串 `""`，看不清写 `unreadable`。不要把空白格写成 `blank`，也不要用空白代替看不清。
- **Chart**：看到一张有标题、X/Y 轴和两条曲线的图：填写 `chart_type=line`、`title`、两个轴标签、`legend`、`series_count=2`，`series_names` 每行一个；只能记录看清的数据点/曲线趋势，不要猜刻度或物理意义。

### 3.5 BBox 的具体判定方法

1. 先看 Context，确定对象在整页上的位置；再看 Crop，确认对象的细节和边界。Crop 是候选裁剪，不代表候选框一定正确。
2. 把只读的“候选 BBox（PDF points）”当作待核对的矩形，而不是标准答案。观察对象的左、上、右、下边缘是否落在同一对象上，是否漏边、包含邻近对象或跨越错误区域。
3. 只有在页面显示“坐标映射：可验证”时，才用 Context 的 pointer 拖拽采集修正后的 PDF points；不要输入截图像素坐标、Crop 坐标、宽高或百分比。拖拽后检查紫色框和四个分项读数，再点击“采用为修正 BBox”。
4. 如果对象存在但边界无法可靠换算，选择 `too_large`/`too_small`/`wrong` 会要求有效 `corrected_bbox`；此时应改用 `uncertain`、`unreadable` 或 `needs_adjudication`，并在 Notes 说明“无法可靠给出 PDF points”，不要伪造数字。
5. 对不存在的候选对象，Presence 选 `absent`，BBox 可选 `not_applicable`；在差异观察中说明候选框落在了什么错误区域（如果看得出来）。

### 3.6 Equation 专用字段

仅在候选对象确实是公式并且审阅状态允许填写时，按下表填写：

| 专用字段 | 含义与填写方法 |
|---|---|
| **公式类型** `equation_type` | `display`：独立成行/展示公式；`inline`：嵌在正文行内；`numbered`：能看到独立公式编号；`unknown`：版面太模糊无法分类。按版面外观填写，不依据公式内容猜。 |
| **公式完整度** `transcription_completeness` | `complete`：所有可见符号、上下标、括号和关系都能可靠记录；`partial`：能读出一部分但有缺失；`unreadable`：主体无法可靠读取。选 `complete` 时必须填写完整 LaTeX/转录。 |
| **公式编号** `formula_number` | 记录公式旁实际印刷的编号，例如 `(16.1)`；若清楚看到没有编号才填 `none`。不要根据 Item ID、章节号或猜测补编号；看不清时写 `unreadable` 并降低状态。 |
| **公式 LaTeX 主体（不含定界符）** `transcription_latex` | Gold 字段只存裸 LaTeX math body：保留分式、根号、希腊字母、运算符、括号、上下标和可见空格关系，但不要输入 `$$...$$`、`$...$`、`\\[...\\]` 或 `\\(...\\)`。裸主体示例：`\\nabla \\times \\mathbf{{B}}=\\mu_0\\mathbf{{J}}`。如复制到标准 LaTeX display math 环境，展示层可写 `\\[ ... \\]`，但不要写回此字段；检测到定界符会在页面导出和 canonical Finalize 两处阻断，系统不会静默删除。不要把自己认为“应该是”的公式补进去；部分可读时只记录确实读到的部分并在 Notes 说明缺失区域。 |
| **是否跨行** `crosses_lines` | 公式主体是否跨越两行或更多行排版；`yes`/`no`/`uncertain`。不要把页面换行或编号单独一行误认为公式跨行。 |
| **下标检查** `subscripts` | 是否认真检查过下标；`checked` 表示已检查并在转录中记录可见下标，`none` 表示明确没有下标，`uncertain` 表示看不清。 |
| **上标检查** `superscripts` | 与下标相同，针对指数、转置、撇号等位于基号上方的符号；无法辨认时选 `uncertain`。 |
| **特殊符号检查** `special_symbols` | 检查希腊字母、微分符号、向量/粗体、点乘、箭头、约等号、负号和其他容易混淆的符号；`checked`/`none`/`uncertain` 依据实际图像填写。 |

公式最容易发生“凭上下文补全”。如果某个符号只靠物理常识才能推断而图像本身看不清，必须保留不确定状态，不能把推断写成完整转录。

### 3.7 Figure 专用字段

本阶段只记录图像对象和文字关联，不要求解释图的物理意义：

| 专用字段 | 含义与填写方法 |
|---|---|
| **图类型** `figure_type` | 按可见外观选择 `photo`（照片/实物图）、`diagram`（示意图）、`schematic`（结构/系统图）、`plot`（绘图/曲线图）、`other` 或 `unknown`。不要因为图中内容熟悉就推断类别。 |
| **完整性** `completeness` | 判断当前候选图是否完整可见：`complete`、`partial`、`uncertain`。边缘被裁掉、只看到子图或明显缺少图主体时选 `partial` 并说明缺失区域。 |
| **图号** `figure_number` | 记录图中/图注实际出现的图号，例如 `Fig. 2.3`；明确没有图号才填 `none`，读不清则写 `unreadable` 并标记相应状态。不要按 Item ID 猜图号。 |
| **Caption** `caption_presence` 与 `caption` | `caption_presence` 选择 `present`、`absent` 或 `unreadable`。Caption 内容只抄录在 Context 中实际可读的文字；没有 caption 才写 `none`，看不清时可留空或写 `unreadable`，不要编写“可能是”的句子。 |
| **是否有子图** `has_subfigures` | 判断是否存在明确的 `(a)`, `(b)` 等子图标记或清晰分区；选择 `yes`/`no`/`uncertain`。仅仅有多个视觉区域不一定就是子图。 |
| **子图数量** `subfigure_count` | `has_subfigures=yes` 时，填写实际能数清的子图数量；`no` 时填 `0`；无法数清时不要猜，改为 `uncertain` 并说明。 |

若 figure 的 caption 或图号不在当前 Context/Crop 中，不要去打开外部 PDF 或网络检索；按当前实例可见证据记录“不可见/不可读”。

### 3.8 Table 和 Chart 专用字段

- **Table**：`table_type` 按框线/矩阵等可见结构选择；`row_count` 和 `column_count` 统计表格内实际可见的所有行列（包括表头行和 stub column，不包括 caption）；多行文字仍属于一个单元格。`header_text` 按阅读顺序记录表头；`merged_cells` 记录跨行/跨列的单元格位置，明确没有时填 `none`；`table_number` 明确没有时填 `none`，看不清写 `unreadable`。Caption 先选 `caption_presence`：有且可读才抄录，明确没有时填 `none`，看不清时选 `unreadable` 并可留空文字框。填好行列后点击“生成/更新单元格表格”，或粘贴 TSV/CSV/Markdown：第一行表头也计入 `row_count`，实际 R×C 必须完全等于期望尺寸，空白 cell 的 `text` 保持 `""`，尺寸不符不会应用；看不清写 `unreadable`。`structure_completeness` 记录结构是否完整可读。
- **Chart**：`chart_type` 按可见几何选择 line/bar/scatter/area/other/unknown；`title`、`x_axis_label`、`y_axis_label`、`legend` 和 Caption 只抄录看见的文字，明确没有才填 `none`，看不清时选 `unreadable` 或不确定状态，不要猜。`series_count` 统计实际绘制的数据系列数量，不是数据点数量；`series_names` 每行填写一个系列名称，没有名称但系列确实存在时写 `unnamed-series-1` 等并在 Notes 说明，无法数清则标记不确定而不要猜。`data_points_or_curves` 必须记录可见的数据点/曲线信息；明确没有可记录系列时可按字段说明填 `none`，看不清时不要用空白伪装完成。

所有类型都要选择人工审阅把握度；可选补充说明（Notes）可用于记录本项异常或阅读约定，但不填写不会阻断。 “差异/无差异观察”必须说明本项实际看到了什么、是否发现候选边界或文字问题。`uncertain`、`needs_adjudication`、`unreadable` 是合法的停止猜测状态，但它们会使 Finalize 暂停，直到另一次独立审阅或后续裁决完成。`candidate_incorrect` 用于明确记录候选类型/定位错误。

### 3.9 顶部审阅声明

完成所有项目后，顶部的七个勾选框是事实声明，不是普通的“同意按钮”。只有能够如实确认时才勾选：

- **我符合 reviewer 资格**：你具备阅读本批技术图、表、公式或图表的能力，并接受了操作者分配的 reviewer ID。
- **我审阅了指定 source evidence 身份**：你核对了页面中锁定的 source SHA、物理页、Context/Crop 和相关只读事实；不是只凭文件名审核。
- **我没有查看模型输出、模型置信度或隐藏答案**：整个独立审阅过程中没有打开预测、benchmark 结果、模型 confidence 或预先填写的答案。
- **我没有修改或重新分配评测分层**：你没有修改任何 calibration/test 分层，也没有要求页面为你显示分层标签。
- **我查看了指定 Context render 与 Crop**：每个项目都实际看过整页 Context 和对应 Crop；不是只看表单或 Item ID。
- **我不是 evaluator**：你没有负责后续模型推理、指标计算或 benchmark evaluator 工作。
- **我独立于 preparation proposer**：你没有参与 7D.5A candidate/workpack 的生成、候选框制作或准备工作，也没有根据 proposer 的答案复核。

任何一项不能确认时不要勾选；Finalize 会把未勾选声明作为暂停或拒绝原因。声明勾选不能替代 {item_count}/{item_count} 的项目填写，也不能把不确定内容变成 Gold。

## 4. 保存、恢复、导出与 Finalize

- `保存草稿` 会把当前表单保存到本机浏览器的 localStorage；自动保存 key 按 **workpack hash + reviewer ID** 隔离。重新打开同一个 `review.html` 会恢复本实例草稿。
- `上一项`、`下一项` 会先保存当前项；左侧列表可跳转，进度会按 canonical 必填规则显示已完成项数，并在“未完成项目”中列出首个缺项原因。
- 普通“导出提交”允许保存当前草稿，即使仍有缺项；“按规则检查并导出 Finalize 输入”只在浏览器侧按同一组 canonical 必填规则检查，发现缺项或逻辑矛盾时阻断并列出可点击定位的问题。浏览器不会调用后端 Finalize，不重新计算或伪造 hash，不修改 split，也不会把结果标为 Gold。
- 将下载的 `review-submission-*.json` 交给操作者。操作者在本机重新读取 canonical workpack 后运行 `reviewer_workbench.py finalize` 和 `validate`。不要手工编辑 JSON；需要修正时回到页面修改并重新导出。

### 4.1 如何导入旧 submission 并恢复填写内容

1. 在右侧“导入已有 submission.json”区域选择以前从**同一 reviewer、同一 review session、同一 workpack 实例**导出的 JSON，再点击“导入并恢复填写内容”。整个过程使用本地 file input/FileReader，不联网。
2. 页面会精确核对 schema/protocol、workbench instance、reviewer/session、完整 `workpack_identity`，以及 {item_count} 个 `item_id` 和每项的 `item_sha256`、`review_task_id`、source/render/crop/bbox `input_hashes`。旧 submission 可以是不完整草稿；只要这些身份和绑定一致就可以恢复。
3. 导入成功后只恢复协议允许的 declarations 和人工 review 字段，并写入现有 autosave namespace；未知字段会被丢弃，旧 `observed_bbox` 仍会兼容为 `corrected_bbox`。导入只是恢复草稿，不是信任、Finalize 或 Gold 判定。
4. 导入失败时当前表单、声明、localStorage 和当前草稿保持不变。常见原因包括 schema/protocol 不同、跨 reviewer/session/workpack、item 缺项或重复、item/task/hash/input_hash 不一致，或文件包含 split、prediction、model、hidden-answer 等禁止字段；请回到对应页面选择正确文件，不要手工改 JSON。

最小本地验证（由操作者执行，将占位路径替换为绝对路径）：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/reviewer_workbench.py validate \\
  --instance /absolute/path/to/reviewer-instance \\
  --workpack /absolute/path/to/tmp/workspace/output/phase7d5a

PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/reviewer_workbench.py finalize \\
  --instance /absolute/path/to/reviewer-instance \\
  --workpack /absolute/path/to/tmp/workspace/output/phase7d5a \\
  --submission /absolute/path/to/review-submission.json \\
  --output /absolute/path/to/new-review-revision

PYTHONDONTWRITEBYTECODE=1 python3 skills/fake-expert/scripts/reviewer_workbench.py validate \\
  --revision /absolute/path/to/new-review-revision \\
  --workpack /absolute/path/to/tmp/workspace/output/phase7d5a
```

只有 `review-manifest.json` 显示 `finalization_status=accepted-for-import` 且 `import_compatible=true` 时，操作者才可把同级 `gold-fragments.jsonl` 交给当前 7D.5B `visual_benchmark.py import-gold`。缺项、重复、哈希/来源/几何/split 漂移、声明不完整、reviewer 不独立、未解决不确定项或 reviewer disagreement 都必须保持 paused/rejected；此时 `gold-fragments.jsonl` 是空文件，不能导入。

## 5. 隐私、冻结与下一阶段

- 本页面完全离线：CSP 禁止连接，脚本拒绝 fetch/XHR/WebSocket/beacon，资源只从本目录加载。不要把目录上传、同步到云盘或发给模型。
- 生成的 reviewer 结果是私有、candidate-only、release-excluded。Gold revision 通过 content-addressed revision ID 固定；已经生成的 revision 不可覆盖，修订必须新建 revision 目录。
- Finalize 不会设置 `verified_gold=true`，也不会开启 promotion、模型推理或 release。Gold 导入后仍须通过现有 7D.5B 状态机；当前资源 handoff 和真实 benchmark 仍是另外的门禁。
- 完成独立 Gold 后，下一阶段由操作者按现有协议处理 Gold import、两路 runtime qualification/resource handoff，再在获得单独授权后进行私有 GPT-5.6 Luna `thinking=max` 评测。当前工作不会停止或重启 OCR 服务，也不会加载模型。

常见错误：

- **进度不是 {item_count}/{item_count}**：继续填写或说明缺项；不要用复制行补齐。
- **批量粘贴未应用**：先确认第一行表头已保留且计入 `row_count`，真实空白 cell 保持为空；页面显示的实际 R×C 必须与期望尺寸完全一致，尺寸不符或解析失败时调整行列数/粘贴内容后重试，当前网格不会被覆盖。
- **看不清**：选择“看不清/不确定”，写明具体区域；不要猜数字、单位、轴标签或公式符号。
- **导入旧 submission 失败**：确认文件来自同一 reviewer、review session 和 workpack，并且 {item_count} 个项目没有缺项/重复；不要删改 identity、item hash 或禁止字段。失败时当前草稿不会改变。
- **Finalize rejected**：通常是 reviewer ID 与 proposer/evaluator 冲突、提交 hash 与 canonical workpack 不一致、重复 reviewer/item 或路径/symlink 不安全；交给操作者读取错误码后重新导出。
- **Finalize paused**：通常是声明未勾选、{item_count} 项未覆盖、table/chart 缺第二名独立 reviewer、项目间存在 disagreement 或有 unresolved 状态；补齐真实独立 review 或走后续裁决。
'''


if __name__ == "__main__":
    raise SystemExit(main())
