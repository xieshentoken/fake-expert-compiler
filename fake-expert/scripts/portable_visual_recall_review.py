#!/usr/bin/env python3
"""Offline incremental missed-object review for bounded visual recall.

Accepted Portable Gold objects are shown as a locked baseline. Parser
candidates, crops, predictions and confidence values remain excluded; the one
reviewer records only additional objects. After a complete external submission
is returned, ``finalize`` combines baseline plus additions and performs a
deterministic one-to-one match against the frozen candidate workpack. The result
is a bounded recall ledger, never verified Gold, promotion evidence, or release
authorization.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping

from compiler_version import (
    PORTABLE_VISUAL_INCREMENTAL_RECALL_COMPILER_VERSION as PORTABLE_VISUAL_RECALL_COMPILER_VERSION,
    PORTABLE_VISUAL_INCREMENTAL_RECALL_PROTOCOL as PORTABLE_VISUAL_RECALL_PROTOCOL,
    PORTABLE_VISUAL_INCREMENTAL_RECALL_REVISION_SCHEMA as PORTABLE_VISUAL_RECALL_REVISION_SCHEMA,
    PORTABLE_VISUAL_INCREMENTAL_RECALL_SUBMISSION_SCHEMA as PORTABLE_VISUAL_RECALL_SUBMISSION_SCHEMA,
    PORTABLE_VISUAL_INCREMENTAL_RECALL_WORKBENCH_SCHEMA as PORTABLE_VISUAL_RECALL_WORKBENCH_SCHEMA,
)
from expert_skill_contract import sha256_file
from incremental_build_dag import sha256_json
from portable_visual_benchmark import _load_gold_revision
from visual_benchmark import load_phase7d5a_workpack
from visual_parser_orchestrator import validate_visual_parser_job


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
KINDS = ("figure", "table", "chart", "equation")
COORDINATE_SPACE = "pdf-page-top-left-points-v1"
MATCH_IOU_THRESHOLD = 0.5


class PortableVisualRecallError(RuntimeError):
    """Stable fail-closed error surface for recall review."""


def _secure_file(path: Path, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise PortableVisualRecallError(f"{label}_missing_or_symlink")
    return resolved


def _secure_directory(path: Path, *, must_be_empty: bool = False) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved.exists():
        if resolved.is_symlink() or not resolved.is_dir():
            raise PortableVisualRecallError("directory_invalid")
        if must_be_empty and any(resolved.iterdir()):
            raise PortableVisualRecallError("output_not_empty")
    else:
        resolved.mkdir(parents=True)
    os.chmod(resolved, 0o700)
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    path = _secure_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PortableVisualRecallError(f"{label}_json_invalid") from error
    if not isinstance(value, dict):
        raise PortableVisualRecallError(f"{label}_object_required")
    return value


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    path = _secure_file(path, label)
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise PortableVisualRecallError(f"{label}_jsonl_invalid:{line_number}") from error
        if not isinstance(value, dict):
            raise PortableVisualRecallError(f"{label}_row_object_required:{line_number}")
        rows.append(value)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    os.chmod(path, 0o600)


def _hash_without(value: Mapping[str, Any], key: str) -> str:
    return sha256_json({name: nested for name, nested in value.items() if name != key})


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise PortableVisualRecallError(f"{label}_invalid")
    return value


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise PortableVisualRecallError("render_png_invalid")
    return struct.unpack(">II", header[16:24])


def _run(command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PortableVisualRecallError(f"{label}_failed") from error
    if result.returncode != 0:
        raise PortableVisualRecallError(f"{label}_failed")
    return result


def _renderer_version(pdftoppm: Path) -> str:
    result = _run([str(pdftoppm), "-v"], "renderer_version")
    text = (result.stderr or result.stdout).splitlines()
    return text[0].strip() if text else "unknown"


def _load_job(job_path: Path, source: Path) -> dict[str, Any]:
    job_path = _secure_file(job_path, "visual_parser_job")
    validation = validate_visual_parser_job(job_path)
    if validation.get("passed") is not True:
        raise PortableVisualRecallError("visual_parser_job_validation_failed")
    job = _read_json(job_path, "visual_parser_job")
    source = _secure_file(source, "source_pdf")
    if job.get("source", {}).get("sha256") != sha256_file(source):
        raise PortableVisualRecallError("source_sha256_mismatch")
    if Path(str(job.get("source", {}).get("path", ""))).expanduser().resolve() != source:
        raise PortableVisualRecallError("source_path_binding_mismatch")
    pages = job.get("scope", {}).get("pages")
    if not isinstance(pages, list) or not pages or any(not isinstance(page, int) or page < 1 for page in pages) or len(set(pages)) != len(pages):
        raise PortableVisualRecallError("job_pages_invalid")
    if pages != list(range(min(pages), max(pages) + 1)):
        raise PortableVisualRecallError("bounded_page_range_must_be_contiguous")
    return {"path": job_path, "job": job, "source": source, "pages": pages, "validation": validation}


def _load_workpack(path: Path, source_sha256: str) -> dict[str, Any]:
    root = _secure_directory(path)
    try:
        facts = load_phase7d5a_workpack(root, allow_portable_review=True)
    except Exception as error:
        raise PortableVisualRecallError(f"candidate_workpack_invalid:{error}") from error
    if facts.get("manifest", {}).get("source_sha256") != source_sha256:
        raise PortableVisualRecallError("candidate_workpack_source_mismatch")
    items_path = _secure_file(root / "candidate-items.jsonl", "candidate_items")
    manifest_path = _secure_file(root / "manifest.json", "candidate_workpack_manifest")
    return {"root": root, "facts": facts, "items_path": items_path, "manifest_path": manifest_path}


def _page_geometry(source: Path, pages: list[int]) -> dict[int, dict[str, Any]]:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise PortableVisualRecallError("pypdf_unavailable") from error
    reader = PdfReader(str(source), strict=False)
    if max(pages) > len(reader.pages):
        raise PortableVisualRecallError("page_out_of_range")
    result: dict[int, dict[str, Any]] = {}
    for physical_page in pages:
        page = reader.pages[physical_page - 1]
        media = page.mediabox
        width = float(media.right) - float(media.left)
        height = float(media.top) - float(media.bottom)
        rotation = int(page.get("/Rotate", 0) or 0) % 360
        if rotation in {90, 270}:
            width, height = height, width
        result[physical_page] = {
            "physical_page": physical_page,
            "width_points": width,
            "height_points": height,
            "rotation": rotation,
            "canonical_page_bbox": [0.0, 0.0, width, height],
            "coordinate_space": COORDINATE_SPACE,
        }
    return result


def _render_pages(source: Path, pages: list[int], destination: Path, pdftoppm: Path, dpi: int, geometry: Mapping[int, Mapping[str, Any]]) -> list[dict[str, Any]]:
    renders = destination / "renders"
    renders.mkdir(parents=True)
    os.chmod(renders, 0o700)
    version = _renderer_version(pdftoppm)
    rows: list[dict[str, Any]] = []
    for physical_page in pages:
        prefix = renders / f"page-{physical_page}"
        _run([str(pdftoppm), "-f", str(physical_page), "-l", str(physical_page), "-r", str(dpi), "-png", "-singlefile", str(source), str(prefix)], f"render_page_{physical_page}")
        render_path = _secure_file(prefix.with_suffix(".png"), f"render_page_{physical_page}")
        os.chmod(render_path, 0o600)
        width_px, height_px = _png_dimensions(render_path)
        row = dict(geometry[physical_page])
        row.update({
            "render_path": f"renders/{render_path.name}",
            "render_sha256": sha256_file(render_path),
            "render_dimensions_px": {"width": width_px, "height": height_px},
            "renderer": "pdftoppm",
            "renderer_version": version,
            "dpi": dpi,
        })
        rows.append(row)
    return rows


def _workbench_id(source_sha: str, pages: list[int], reviewer: str, session: str) -> str:
    return "pvrw-" + sha256_json({"source": source_sha, "pages": pages, "reviewer": reviewer, "session": session})[:20]


def _baseline_label(kind: str, details: Mapping[str, Any]) -> str:
    number = details.get("figure_number") if kind == "figure" else details.get("table_number")
    caption = details.get("caption")
    return " ".join(str(value).strip() for value in (number, caption) if isinstance(value, str) and value.strip())


def _baseline_objects(workpack: Mapping[str, Any], gold_revision: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        gold = _load_gold_revision(Path(gold_revision).expanduser().resolve(), workpack["facts"])
    except Exception as error:
        raise PortableVisualRecallError(f"baseline_gold_revision_invalid:{error}") from error
    item_by_id = {str(item["item_id"]): item for item in workpack["facts"]["candidate_items"]}
    objects: list[dict[str, Any]] = []
    for row in gold["rows"]:
        payload = row.get("gold_payload") if isinstance(row.get("gold_payload"), Mapping) else {}
        if payload.get("presence") != "present":
            continue
        item_id = str(row.get("item_id"))
        item = item_by_id.get(item_id)
        if item is None:
            raise PortableVisualRecallError(f"baseline_gold_item_missing:{item_id}")
        kind = str(payload.get("kind"))
        bbox = payload.get("bbox")
        details = payload.get("details") if isinstance(payload.get("details"), Mapping) else {}
        object_id = "baseline-" + sha256_json({"gold_item_id": item_id, "kind": kind, "bbox": bbox})[:20]
        objects.append({
            "object_id": object_id,
            "source_gold_item_id": item_id,
            "physical_page": int(item["source"]["physical_page"]),
            "kind": kind,
            "bbox": [float(value) for value in bbox],
            "label_or_caption": _baseline_label(kind, details),
            "notes": "来自已接受的 Portable Gold；在增量漏检审查中锁定只读。",
        })
    objects.sort(key=lambda row: (row["physical_page"], row["kind"], row["object_id"]))
    return objects, gold


def _load_legacy_submission(legacy_workbench: Path, legacy_submission: Path, source_sha: str, reviewer_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    root = _secure_directory(legacy_workbench)
    manifest = _read_json(root / "workbench-manifest.json", "legacy_workbench_manifest")
    if manifest.get("schema_version") != "tkc.portable-visual-recall-workbench/v0.1" or manifest.get("protocol") != "portable-visual-recall-review-v0.1":
        raise PortableVisualRecallError("legacy_workbench_identity_invalid")
    if manifest.get("manifest_sha256") != _hash_without(manifest, "manifest_sha256") or manifest.get("source_sha256") != source_sha:
        raise PortableVisualRecallError("legacy_workbench_binding_invalid")
    submission = _read_json(legacy_submission, "legacy_recall_submission")
    if submission.get("schema_version") != "tkc.portable-visual-recall-submission/v0.1" or submission.get("protocol") != "portable-visual-recall-review-v0.1":
        raise PortableVisualRecallError("legacy_submission_identity_invalid")
    for key in ("workbench_instance_id", "manifest_sha256", "source_sha256"):
        if submission.get(key) != manifest.get(key):
            raise PortableVisualRecallError(f"legacy_submission_{key}_mismatch")
    if submission.get("reviewer_instance") != reviewer_id or submission.get("reviewer_instance") != manifest.get("reviewer", {}).get("reviewer_instance") or submission.get("review_session_id") != manifest.get("reviewer", {}).get("review_session_id"):
        raise PortableVisualRecallError("legacy_submission_reviewer_binding_mismatch")
    declarations = submission.get("declarations")
    if not isinstance(declarations, Mapping) or any(value is not True for value in declarations.values()):
        raise PortableVisualRecallError("legacy_submission_declarations_incomplete")
    rows = submission.get("pages")
    if not isinstance(rows, list) or len(rows) != len(manifest.get("pages", [])):
        raise PortableVisualRecallError("legacy_submission_page_count_mismatch")
    expected_by_page = {int(row["physical_page"]): row for row in manifest["pages"]}
    if {row.get("physical_page") for row in rows if isinstance(row, Mapping)} != set(expected_by_page):
        raise PortableVisualRecallError("legacy_submission_page_set_mismatch")
    for row in rows:
        _validate_page_row(row, expected_by_page[int(row["physical_page"])])
    return manifest, submission


def _migration_pages(pages: list[Mapping[str, Any]], baseline: list[Mapping[str, Any]], legacy: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    baseline_by_page: dict[int, list[Mapping[str, Any]]] = {}
    for obj in baseline:
        baseline_by_page.setdefault(int(obj["physical_page"]), []).append(obj)
    legacy_by_page = {int(row["physical_page"]): row for row in legacy["pages"]}
    migrated: list[dict[str, Any]] = []
    duplicate_count = 0
    additional_count = 0
    for page in pages:
        physical_page = int(page["physical_page"])
        baseline_objects = baseline_by_page.get(physical_page, [])
        initial_objects = [{
            "object_id": str(obj["object_id"]),
            "kind": str(obj["kind"]),
            "bbox": list(obj["bbox"]),
            "label_or_caption": str(obj["label_or_caption"]),
            "notes": str(obj["notes"]),
            "locked_baseline": True,
        } for obj in baseline_objects]
        for old in legacy_by_page[physical_page]["objects"]:
            duplicate = any(old["kind"] == known["kind"] and _bbox_iou(list(old["bbox"]), list(known["bbox"])) >= MATCH_IOU_THRESHOLD for known in baseline_objects)
            if duplicate:
                duplicate_count += 1
                continue
            initial_objects.append({**old, "locked_baseline": False})
            additional_count += 1
        row = dict(page)
        row["initial_review"] = {
            "page_review_status": "complete",
            "no_relevant_visual_objects": not initial_objects,
            "objects": initial_objects,
            "observation": str(legacy_by_page[physical_page].get("observation", "")),
        }
        migrated.append(row)
    summary = {
        "legacy_page_count": len(legacy["pages"]),
        "baseline_object_count": len(baseline),
        "legacy_object_count": sum(len(row["objects"]) for row in legacy["pages"]),
        "legacy_objects_matched_to_baseline": duplicate_count,
        "migrated_additional_object_count": additional_count,
        "reviewer_declarations_reset": True,
    }
    return migrated, summary


def _review_pack(manifest: Mapping[str, Any], pages: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": PORTABLE_VISUAL_RECALL_WORKBENCH_SCHEMA,
        "protocol": PORTABLE_VISUAL_RECALL_PROTOCOL,
        "compiler_version": PORTABLE_VISUAL_RECALL_COMPILER_VERSION,
        "workbench_instance_id": manifest["workbench_instance_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "source_sha256": manifest["source_sha256"],
        "reviewer": manifest["reviewer"],
        "policy": {
            "reviewer_required": 1,
            "candidate_blind": True,
            "review_mode": "incremental-missed-object-review",
            "accepted_gold_baseline_visible": True,
            "whole_page_census": False,
            "relevant_kinds": list(KINDS),
            "coordinate_space": COORDINATE_SPACE,
        },
        "baseline": {
            "object_count": len(manifest["baseline_objects"]),
            "gold_revision_manifest_sha256": manifest["bindings"]["baseline_gold_revision_manifest_sha256"],
        },
        "migration_summary": manifest["migration_summary"],
        "pages": list(pages),
    }


def generate_workbench(*, source: Path, visual_parser_job: Path, candidate_workpack: Path, baseline_gold_revision: Path, legacy_workbench: Path, legacy_submission: Path, output: Path, reviewer_id: str, review_session_id: str, proposer_instance: str) -> dict[str, Any]:
    reviewer_id = _id(reviewer_id, "reviewer_id")
    review_session_id = _id(review_session_id, "review_session_id")
    proposer_instance = _id(proposer_instance, "proposer_instance")
    if reviewer_id == proposer_instance:
        raise PortableVisualRecallError("reviewer_proposer_overlap")
    loaded = _load_job(visual_parser_job, source)
    job = loaded["job"]
    source_sha = str(job["source"]["sha256"])
    workpack = _load_workpack(candidate_workpack, source_sha)
    baseline, gold = _baseline_objects(workpack, baseline_gold_revision)
    legacy_manifest, legacy = _load_legacy_submission(legacy_workbench, legacy_submission, source_sha, reviewer_id)
    destination = _secure_directory(output, must_be_empty=True)
    pdftoppm = _secure_file(Path(str(job.get("render", {}).get("pdftoppm", ""))), "pdftoppm")
    dpi = int(job.get("render", {}).get("dpi", 0))
    if dpi < 36:
        raise PortableVisualRecallError("render_dpi_invalid")
    geometry = _page_geometry(loaded["source"], loaded["pages"])
    pages = _render_pages(loaded["source"], loaded["pages"], destination, pdftoppm, dpi, geometry)

    # Re-rendering must reproduce every existing frozen context render.
    for item in workpack["facts"]["candidate_items"]:
        page = int(item.get("source", {}).get("physical_page", 0))
        expected = item.get("render", {}).get("sha256")
        actual = next((row["render_sha256"] for row in pages if row["physical_page"] == page), None)
        if expected != actual:
            raise PortableVisualRecallError(f"frozen_render_reproduction_mismatch:{page}")

    migrated_pages, migration_summary = _migration_pages(pages, baseline, legacy)
    migration_summary["legacy_submission_sha256"] = sha256_file(Path(legacy_submission).expanduser().resolve())
    workbench_id = _workbench_id(source_sha, loaded["pages"], reviewer_id, review_session_id)
    manifest: dict[str, Any] = {
        "schema_version": PORTABLE_VISUAL_RECALL_WORKBENCH_SCHEMA,
        "protocol": PORTABLE_VISUAL_RECALL_PROTOCOL,
        "compiler_version": PORTABLE_VISUAL_RECALL_COMPILER_VERSION,
        "workbench_instance_id": workbench_id,
        "source_sha256": source_sha,
        "source_pdf_filename": loaded["source"].name,
        "bounded_scope": {"physical_pages": loaded["pages"], "page_count": len(loaded["pages"]), "contiguous": True},
        "reviewer": {"reviewer_instance": reviewer_id, "review_session_id": review_session_id},
        "proposer_instance": proposer_instance,
        "bindings": {
            "tool_sha256": sha256_file(Path(__file__).resolve()),
            "visual_parser_job_sha256": sha256_file(loaded["path"]),
            "candidate_workpack_manifest_sha256": sha256_file(workpack["manifest_path"]),
            "candidate_items_sha256": sha256_file(workpack["items_path"]),
            "baseline_gold_revision_manifest_sha256": sha256_file(gold["root"] / "gold-review-revision.json"),
            "baseline_gold_fragments_sha256": sha256_file(gold["path"]),
            "legacy_workbench_manifest_sha256": sha256_file(Path(legacy_workbench).expanduser().resolve() / "workbench-manifest.json"),
            "legacy_submission_sha256": sha256_file(Path(legacy_submission).expanduser().resolve()),
        },
        "matching_policy": {"same_page": True, "same_kind": True, "one_to_one": True, "bbox_iou_threshold": MATCH_IOU_THRESHOLD},
        "review_policy": {
            "reviewer_required": 1,
            "candidate_blind": True,
            "review_mode": "incremental-missed-object-review",
            "accepted_gold_baseline_visible": True,
            "full_page_only": True,
            "candidate_overlays_included": False,
            "parser_predictions_included": False,
            "relevant_kinds": list(KINDS),
        },
        "baseline_objects": baseline,
        "migration_summary": migration_summary,
        "legacy_workbench_instance_id": legacy_manifest["workbench_instance_id"],
        "pages": pages,
        "status": "paused-independent-incremental-recall-review",
        "submission_count": 0,
        "recall_status": "not-run",
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
    }
    manifest["manifest_sha256"] = _hash_without(manifest, "manifest_sha256")
    pack = _review_pack(manifest, migrated_pages)
    _write_json(destination / "workbench-manifest.json", manifest)
    _write_json(destination / "review-pack.json", pack)
    html = _render_html(pack)
    (destination / "review.html").write_text(html, encoding="utf-8")
    os.chmod(destination / "review.html", 0o600)
    instructions = _instructions(manifest)
    (destination / "视觉召回人工Review使用说明.md").write_text(instructions, encoding="utf-8")
    os.chmod(destination / "视觉召回人工Review使用说明.md", 0o600)
    validation = validate_workbench(destination, loaded["path"], workpack["root"], gold["root"], Path(legacy_workbench), Path(legacy_submission))
    return {"workbench": str(destination), "status": manifest["status"], "workbench_instance_id": workbench_id, "manifest_sha256": manifest["manifest_sha256"], "page_count": len(pages), "baseline_object_count": len(baseline), "migration_summary": migration_summary, "validation": validation}


def _validate_page_row(row: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    if set(row) != {"physical_page", "page_review_status", "no_relevant_visual_objects", "objects", "observation", "render_sha256"}:
        raise PortableVisualRecallError(f"submission_page_fields_invalid:{expected['physical_page']}")
    page = int(expected["physical_page"])
    if row.get("physical_page") != page or row.get("render_sha256") != expected.get("render_sha256"):
        raise PortableVisualRecallError(f"submission_page_binding_mismatch:{page}")
    if row.get("page_review_status") != "complete":
        raise PortableVisualRecallError(f"submission_page_incomplete:{page}")
    empty = row.get("no_relevant_visual_objects")
    objects = row.get("objects")
    if not isinstance(empty, bool) or not isinstance(objects, list):
        raise PortableVisualRecallError(f"submission_page_content_invalid:{page}")
    if empty == bool(objects):
        raise PortableVisualRecallError(f"submission_page_empty_object_contradiction:{page}")
    if not isinstance(row.get("observation"), str):
        raise PortableVisualRecallError(f"submission_page_observation_invalid:{page}")
    seen: set[str] = set()
    width = float(expected["width_points"])
    height = float(expected["height_points"])
    for index, obj in enumerate(objects):
        if not isinstance(obj, Mapping) or set(obj) != {"object_id", "kind", "bbox", "label_or_caption", "notes"}:
            raise PortableVisualRecallError(f"submission_object_fields_invalid:{page}:{index}")
        object_id = _id(obj.get("object_id"), f"object_id:{page}:{index}")
        if object_id in seen:
            raise PortableVisualRecallError(f"submission_object_id_duplicate:{object_id}")
        seen.add(object_id)
        if obj.get("kind") not in KINDS:
            raise PortableVisualRecallError(f"submission_object_kind_invalid:{object_id}")
        bbox = obj.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4 or any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) for value in bbox):
            raise PortableVisualRecallError(f"submission_object_bbox_invalid:{object_id}")
        x0, y0, x1, y1 = (float(value) for value in bbox)
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise PortableVisualRecallError(f"submission_object_bbox_out_of_bounds:{object_id}")
        if not isinstance(obj.get("label_or_caption"), str) or not isinstance(obj.get("notes"), str):
            raise PortableVisualRecallError(f"submission_object_text_invalid:{object_id}")


def _load_submission(path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    submission = _read_json(path, "recall_submission")
    required = {"schema_version", "protocol", "review_mode", "workbench_instance_id", "manifest_sha256", "source_sha256", "baseline_gold_revision_manifest_sha256", "legacy_submission_sha256", "reviewer_instance", "review_session_id", "declarations", "pages", "exported_at"}
    if set(submission) != required:
        raise PortableVisualRecallError("submission_fields_invalid")
    if submission.get("schema_version") != PORTABLE_VISUAL_RECALL_SUBMISSION_SCHEMA or submission.get("protocol") != PORTABLE_VISUAL_RECALL_PROTOCOL:
        raise PortableVisualRecallError("submission_identity_invalid")
    if submission.get("review_mode") != "incremental-missed-object-review" or submission.get("baseline_gold_revision_manifest_sha256") != manifest.get("bindings", {}).get("baseline_gold_revision_manifest_sha256") or submission.get("legacy_submission_sha256") != manifest.get("bindings", {}).get("legacy_submission_sha256"):
        raise PortableVisualRecallError("submission_incremental_binding_invalid")
    for key in ("workbench_instance_id", "manifest_sha256", "source_sha256"):
        if submission.get(key) != manifest.get(key):
            raise PortableVisualRecallError(f"submission_{key}_mismatch")
    reviewer = manifest["reviewer"]
    if submission.get("reviewer_instance") != reviewer["reviewer_instance"] or submission.get("review_session_id") != reviewer["review_session_id"]:
        raise PortableVisualRecallError("submission_reviewer_binding_mismatch")
    if submission.get("reviewer_instance") == manifest.get("proposer_instance"):
        raise PortableVisualRecallError("reviewer_proposer_overlap")
    declarations = submission.get("declarations")
    expected_declarations = {"inspected_every_full_page", "did_not_view_parser_candidates_or_predictions", "independent_from_proposer", "not_evaluator", "single_reviewer_scope_understood"}
    if not isinstance(declarations, Mapping) or set(declarations) != expected_declarations or any(declarations.get(name) is not True for name in expected_declarations):
        raise PortableVisualRecallError("submission_declarations_incomplete")
    rows = submission.get("pages")
    if not isinstance(rows, list) or len(rows) != len(manifest["pages"]):
        raise PortableVisualRecallError("submission_page_count_mismatch")
    by_page = {row.get("physical_page"): row for row in rows if isinstance(row, Mapping)}
    if len(by_page) != len(rows):
        raise PortableVisualRecallError("submission_pages_duplicate_or_invalid")
    for expected in manifest["pages"]:
        row = by_page.get(expected["physical_page"])
        if not isinstance(row, Mapping):
            raise PortableVisualRecallError(f"submission_page_missing:{expected['physical_page']}")
        _validate_page_row(row, expected)
    submitted_objects = {str(obj["object_id"]): obj for row in rows for obj in row["objects"]}
    baseline_ids = {str(obj["object_id"]) for obj in manifest["baseline_objects"]}
    if any(object_id.startswith("baseline-") and object_id not in baseline_ids for object_id in submitted_objects):
        raise PortableVisualRecallError("submission_unknown_baseline_object")
    for baseline in manifest["baseline_objects"]:
        submitted = submitted_objects.get(str(baseline["object_id"]))
        expected = {key: baseline[key] for key in ("object_id", "kind", "bbox", "label_or_caption", "notes")}
        if submitted != expected:
            raise PortableVisualRecallError(f"submission_baseline_object_changed_or_missing:{baseline['object_id']}")
    if not isinstance(submission.get("exported_at"), str) or not submission["exported_at"].strip():
        raise PortableVisualRecallError("submission_exported_at_invalid")
    return submission


def validate_workbench(workbench: Path, visual_parser_job: Path, candidate_workpack: Path, baseline_gold_revision: Path, legacy_workbench: Path, legacy_submission: Path) -> dict[str, Any]:
    root = _secure_directory(workbench)
    manifest = _read_json(root / "workbench-manifest.json", "workbench_manifest")
    if manifest.get("schema_version") != PORTABLE_VISUAL_RECALL_WORKBENCH_SCHEMA or manifest.get("protocol") != PORTABLE_VISUAL_RECALL_PROTOCOL or manifest.get("compiler_version") != PORTABLE_VISUAL_RECALL_COMPILER_VERSION:
        raise PortableVisualRecallError("workbench_identity_invalid")
    if manifest.get("manifest_sha256") != _hash_without(manifest, "manifest_sha256"):
        raise PortableVisualRecallError("workbench_manifest_hash_mismatch")
    job_path = _secure_file(visual_parser_job, "visual_parser_job")
    workpack = _load_workpack(candidate_workpack, str(manifest.get("source_sha256")))
    baseline, gold = _baseline_objects(workpack, baseline_gold_revision)
    legacy_manifest, legacy = _load_legacy_submission(legacy_workbench, legacy_submission, str(manifest.get("source_sha256")), str(manifest.get("reviewer", {}).get("reviewer_instance")))
    bindings = manifest.get("bindings") if isinstance(manifest.get("bindings"), Mapping) else {}
    if bindings.get("tool_sha256") != sha256_file(Path(__file__).resolve()) or bindings.get("visual_parser_job_sha256") != sha256_file(job_path) or bindings.get("candidate_workpack_manifest_sha256") != sha256_file(workpack["manifest_path"]) or bindings.get("candidate_items_sha256") != sha256_file(workpack["items_path"]) or bindings.get("baseline_gold_revision_manifest_sha256") != sha256_file(gold["root"] / "gold-review-revision.json") or bindings.get("baseline_gold_fragments_sha256") != sha256_file(gold["path"]) or bindings.get("legacy_workbench_manifest_sha256") != sha256_file(Path(legacy_workbench).expanduser().resolve() / "workbench-manifest.json") or bindings.get("legacy_submission_sha256") != sha256_file(Path(legacy_submission).expanduser().resolve()):
        raise PortableVisualRecallError("workbench_binding_hash_mismatch")
    pages = manifest.get("pages")
    if not isinstance(pages, list) or len(pages) != manifest.get("bounded_scope", {}).get("page_count"):
        raise PortableVisualRecallError("workbench_pages_invalid")
    for row in pages:
        render = _secure_file(root / str(row.get("render_path", "")), "workbench_render")
        if row.get("render_sha256") != sha256_file(render):
            raise PortableVisualRecallError(f"workbench_render_hash_mismatch:{row.get('physical_page')}")
        if row.get("coordinate_space") != COORDINATE_SPACE:
            raise PortableVisualRecallError("workbench_coordinate_space_invalid")
    migrated_pages, migration_summary = _migration_pages(pages, baseline, legacy)
    migration_summary["legacy_submission_sha256"] = sha256_file(Path(legacy_submission).expanduser().resolve())
    if manifest.get("baseline_objects") != baseline or manifest.get("migration_summary") != migration_summary or manifest.get("legacy_workbench_instance_id") != legacy_manifest.get("workbench_instance_id"):
        raise PortableVisualRecallError("workbench_incremental_baseline_or_migration_mismatch")
    pack = _read_json(root / "review-pack.json", "review_pack")
    if pack != _review_pack(manifest, migrated_pages):
        raise PortableVisualRecallError("review_pack_mismatch")
    html = _secure_file(root / "review.html", "review_html").read_text(encoding="utf-8")
    if "__REVIEW_PACK__" in html or "candidate_bbox" in html or "candidate_id" in html or "parser_prediction" in html:
        raise PortableVisualRecallError("candidate_blindness_violation")
    for item in workpack["facts"]["candidate_items"]:
        if str(item.get("item_id")) in html or str(item.get("item_id")) in json.dumps(pack):
            raise PortableVisualRecallError("candidate_identity_leaked")
    _secure_file(root / "视觉召回人工Review使用说明.md", "review_instructions")
    return {"passed": True, "status": manifest["status"], "page_count": len(pages), "reviewer_required": 1, "candidate_blind": True, "review_mode": "incremental-missed-object-review", "baseline_object_count": len(baseline), "migration_summary": migration_summary, "recall_status": "not-run"}


def _bbox_iou(left: list[float], right: list[float]) -> float:
    ax0, ay0, ax1, ay1 = (float(value) for value in left)
    bx0, by0, bx1, by1 = (float(value) for value in right)
    intersection = max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(0.0, min(ay1, by1) - max(ay0, by0))
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - intersection
    return intersection / union if union > 0 else 0.0


def _match(gold: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    possible: list[tuple[float, str, str, int, int]] = []
    for gi, g in enumerate(gold):
        for ci, c in enumerate(candidates):
            if g["physical_page"] != c["physical_page"] or g["kind"] != c["kind"]:
                continue
            iou = _bbox_iou(g["bbox"], c["bbox"])
            if iou >= MATCH_IOU_THRESHOLD:
                possible.append((iou, g["gold_object_id"], c["candidate_id"], gi, ci))
    possible.sort(key=lambda row: (-row[0], row[1], row[2]))
    used_gold: set[int] = set()
    used_candidates: set[int] = set()
    matches: list[dict[str, Any]] = []
    for iou, _, _, gi, ci in possible:
        if gi in used_gold or ci in used_candidates:
            continue
        used_gold.add(gi)
        used_candidates.add(ci)
        matches.append({"gold_object_id": gold[gi]["gold_object_id"], "candidate_id": candidates[ci]["candidate_id"], "physical_page": gold[gi]["physical_page"], "kind": gold[gi]["kind"], "bbox_iou": iou, "status": "matched"})
    for index, row in enumerate(gold):
        if index not in used_gold:
            matches.append({"gold_object_id": row["gold_object_id"], "candidate_id": None, "physical_page": row["physical_page"], "kind": row["kind"], "bbox_iou": None, "status": "missed"})
    for index, row in enumerate(candidates):
        if index not in used_candidates:
            matches.append({"gold_object_id": None, "candidate_id": row["candidate_id"], "physical_page": row["physical_page"], "kind": row["kind"], "bbox_iou": None, "status": "false-positive"})
    matches.sort(key=lambda row: (row["physical_page"], row["kind"], str(row["gold_object_id"]), str(row["candidate_id"])))
    tp = len(used_gold)
    fn = len(gold) - tp
    fp = len(candidates) - tp
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    metrics = {"true_positive_count": tp, "false_positive_count": fp, "false_negative_count": fn, "precision": precision, "recall": recall, "f1": f1}
    return matches, metrics


def finalize(*, workbench: Path, visual_parser_job: Path, candidate_workpack: Path, baseline_gold_revision: Path, legacy_workbench: Path, legacy_submission: Path, submission: Path, output: Path) -> dict[str, Any]:
    root = _secure_directory(workbench)
    validate_workbench(root, visual_parser_job, candidate_workpack, baseline_gold_revision, legacy_workbench, legacy_submission)
    manifest = _read_json(root / "workbench-manifest.json", "workbench_manifest")
    reviewed = _load_submission(submission, manifest)
    workpack = _load_workpack(candidate_workpack, str(manifest["source_sha256"]))
    destination = _secure_directory(output, must_be_empty=True)
    gold: list[dict[str, Any]] = []
    baseline_by_id = {str(obj["object_id"]): obj for obj in manifest["baseline_objects"]}
    for page in sorted(reviewed["pages"], key=lambda row: row["physical_page"]):
        for index, obj in enumerate(page["objects"], 1):
            stable = "gold-" + sha256_json({"source": manifest["source_sha256"], "page": page["physical_page"], "kind": obj["kind"], "bbox": obj["bbox"], "ordinal": index})[:20]
            baseline = baseline_by_id.get(str(obj["object_id"]))
            gold.append({"schema_version": "tkc.portable-visual-recall-object/v0.2", "gold_object_id": stable, "physical_page": page["physical_page"], "kind": obj["kind"], "bbox": obj["bbox"], "label_or_caption": obj["label_or_caption"], "notes": obj["notes"], "coordinate_space": COORDINATE_SPACE, "reviewer_instance": reviewed["reviewer_instance"], "review_session_id": reviewed["review_session_id"], "source": "accepted-portable-gold-baseline" if baseline else "single-reviewer-additional-object", "source_gold_item_id": baseline.get("source_gold_item_id") if baseline else None, "assurance": "accepted-content-gold-plus-single-reviewer-gap-check" if baseline else "single-independent-reviewer-additional-object"})
    candidates = []
    for item in workpack["facts"]["candidate_items"]:
        candidates.append({"candidate_id": str(item["item_id"]), "physical_page": int(item["source"]["physical_page"]), "kind": str(item["kind_candidate"]), "bbox": [float(value) for value in item["geometry"]["candidate_bbox_pdf"]]})
    matches, metrics = _match(gold, candidates)
    _write_jsonl(destination / "gold-objects.jsonl", gold)
    submission_copy = destination / "reviewer-submission.json"
    shutil.copyfile(_secure_file(submission, "recall_submission"), submission_copy)
    os.chmod(submission_copy, 0o600)
    additional_count = sum(1 for row in gold if row["source"] == "single-reviewer-additional-object")
    _write_json(destination / "recall-ledger.json", {"schema_version": "tkc.portable-visual-recall-ledger/v0.2", "review_mode": "incremental-missed-object-review", "matching_policy": manifest["matching_policy"], "scope": manifest["bounded_scope"], "baseline_object_count": len(baseline_by_id), "additional_object_count": additional_count, "metrics": metrics, "matches": matches, "single_reviewer_for_gap_check": True, "verified_gold": False, "promotion_allowed": False})
    revision: dict[str, Any] = {
        "schema_version": PORTABLE_VISUAL_RECALL_REVISION_SCHEMA,
        "protocol": PORTABLE_VISUAL_RECALL_PROTOCOL,
        "compiler_version": PORTABLE_VISUAL_RECALL_COMPILER_VERSION,
        "workbench_instance_id": manifest["workbench_instance_id"],
        "workbench_manifest_sha256": manifest["manifest_sha256"],
        "source_sha256": manifest["source_sha256"],
        "reviewer": manifest["reviewer"],
        "status": "accepted-incremental-bounded-recall",
        "assurance": "accepted-content-gold-plus-single-reviewer-gap-check",
        "scope": manifest["bounded_scope"],
        "object_count": len(gold),
        "baseline_object_count": len(baseline_by_id),
        "additional_object_count": additional_count,
        "candidate_count": len(candidates),
        "metrics": metrics,
        "file_hashes": {"reviewer-submission.json": sha256_file(submission_copy), "gold-objects.jsonl": sha256_file(destination / "gold-objects.jsonl"), "recall-ledger.json": sha256_file(destination / "recall-ledger.json")},
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
        "immutable": True,
    }
    revision["revision_sha256"] = _hash_without(revision, "revision_sha256")
    _write_json(destination / "recall-revision.json", revision)
    return {"revision": str(destination), "status": revision["status"], "object_count": len(gold), "metrics": metrics, "revision_sha256": revision["revision_sha256"]}


def validate_revision(*, workbench: Path, visual_parser_job: Path, candidate_workpack: Path, baseline_gold_revision: Path, legacy_workbench: Path, legacy_submission: Path, revision: Path) -> dict[str, Any]:
    root = _secure_directory(revision)
    validate_workbench(workbench, visual_parser_job, candidate_workpack, baseline_gold_revision, legacy_workbench, legacy_submission)
    manifest = _read_json(Path(workbench) / "workbench-manifest.json", "workbench_manifest")
    value = _read_json(root / "recall-revision.json", "recall_revision")
    if value.get("schema_version") != PORTABLE_VISUAL_RECALL_REVISION_SCHEMA or value.get("revision_sha256") != _hash_without(value, "revision_sha256"):
        raise PortableVisualRecallError("recall_revision_invalid")
    if value.get("workbench_manifest_sha256") != manifest.get("manifest_sha256") or value.get("status") != "accepted-incremental-bounded-recall":
        raise PortableVisualRecallError("recall_revision_binding_invalid")
    for name in ("reviewer-submission.json", "gold-objects.jsonl", "recall-ledger.json"):
        if value.get("file_hashes", {}).get(name) != sha256_file(_secure_file(root / name, name)):
            raise PortableVisualRecallError(f"recall_revision_file_hash_mismatch:{name}")
    if value.get("verified_gold") is not False or value.get("promotion_allowed") is not False or value.get("release_included") is not False:
        raise PortableVisualRecallError("recall_revision_policy_invalid")
    return {"passed": True, "status": value["status"], "object_count": value["object_count"], "metrics": value["metrics"], "verified_gold": False}


def _instructions(manifest: Mapping[str, Any]) -> str:
    start = manifest["bounded_scope"]["physical_pages"][0]
    end = manifest["bounded_scope"]["physical_pages"][-1]
    return f"""# 增量漏检人工 Review 使用说明

本工具只做物理页 {start}–{end} 的**增量漏检检查**。已接受的 Portable Gold 中 {len(manifest['baseline_objects'])} 个对象作为蓝色锁定基线，不需要重新填写；reviewer 只需确认是否还有基线之外的 `figure|table|chart|equation`。此步骤只需要 1 名独立 reviewer，不替代此前 Table/Chart 内容 Gold 的双 reviewer 审查，也不形成 verified Gold 或发布许可。

## 独立性边界

- 页面可显示已接受 Gold 的蓝色基线框，但不显示 parser 候选、候选 ID、预测、置信度或 split。
- reviewer ID 为 `{manifest['reviewer']['reviewer_instance']}`，session 为 `{manifest['reviewer']['review_session_id']}`。
- reviewer 不得与 proposer `{manifest['proposer_instance']}` 或后续 evaluator 是同一实例/角色。

## 每页如何填写

1. 使用左侧页码打开每一页，放大、缩小或切换“平移页面”。
2. 蓝色框是已接受 Gold 基线，类型、BBox、标题和备注均锁定，不能删除或修改。
3. 只在发现蓝色基线之外的新对象时点“新增漏检对象”，选择类型并框选 BBox。BBox 使用 `[x0,y0,x1,y1]`，原点在页面左上，单位是 PDF points。
4. 本工具已迁移旧 submission：旧第 309 页 Figure 与基线 IoU≥{MATCH_IOU_THRESHOLD}，作为已有对象去重；没有发现其他新增对象。迁移不会代替 reviewer 的新声明。
5. 所有页面和基线已经预填。请重新检查五项声明并导出；若确实发现新增对象，再添加后导出。

## 批量页面与导入

- “批量确认所选页无额外对象”只适用于已经逐页检查的页面；它不会自动判断页面内容，也不会删除蓝色基线。
- 页面已由旧 submission 迁移；后续只能导入本 R3 workbench 导出的 submission。源、基线 Gold、旧 submission、render、reviewer/session 任一漂移都会失败关闭。

## 导出与后续

五项声明、15 页状态、锁定基线和新增对象全部有效后才能导出。导出文件仍只是 reviewer submission。`finalize` 会把已接受基线与 reviewer 新增对象组成完整 census，再离线与冻结候选执行同页、同类型、IoU≥{MATCH_IOU_THRESHOLD} 的一对一匹配，届时才计算本页范围内的 precision/recall/F1。
"""


def _render_html(pack: Mapping[str, Any]) -> str:
    payload = json.dumps(pack, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__PACK_JSON__", payload)


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>增量漏检人工 Review</title><style>
:root{--ink:#17212b;--muted:#617080;--line:#d7dee5;--accent:#075985;--bg:#eef3f6}*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,sans-serif;color:var(--ink);background:var(--bg)}header{padding:12px 18px;background:#fff;border-bottom:1px solid var(--line)}header h1{font-size:18px;margin:0 0 4px}.layout{display:grid;grid-template-columns:220px minmax(480px,1fr) 360px;gap:10px;height:calc(100vh - 72px);padding:10px}.panel{background:#fff;border:1px solid var(--line);border-radius:9px;overflow:auto;padding:10px}.pages button{display:block;width:100%;text-align:left;margin:3px 0;padding:7px;border:1px solid var(--line);border-radius:5px;background:#fff}.pages button.active{border-color:var(--accent);background:#e8f4fa}.pages button.done{color:#166534}.toolbar{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:8px}button,.button{font:inherit;padding:6px 9px;border:1px solid #9ba9b5;border-radius:6px;background:#fff;cursor:pointer}button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}.viewport{height:calc(100% - 92px);overflow:auto;background:#313941;position:relative;cursor:crosshair}.viewport.pan{cursor:grab}.stage{position:relative;margin:20px auto;transform-origin:top left}.stage img{display:block;width:100%;height:100%;user-select:none}.stage canvas{position:absolute;inset:0;width:100%;height:100%}.readout{font-variant-numeric:tabular-nums;color:var(--muted);margin:4px 0}.right{overflow:auto}.right h2{font-size:15px;margin:12px 0 7px}.field{margin:7px 0}.field label{display:block;font-weight:600;margin-bottom:3px}.field input,.field select,.field textarea{width:100%;padding:6px;border:1px solid #bcc7d0;border-radius:5px}.object{border:1px solid var(--line);border-radius:7px;padding:8px;margin:7px 0}.object.selected{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}.hint{color:var(--muted);font-size:12px}.warning{color:#9a3412}.declarations{border-top:1px solid var(--line);margin-top:12px;padding-top:8px}.declarations label{display:block;margin:5px 0}.status{padding:7px;background:#f2f6f8;border-radius:5px;margin:8px 0}.batch-list{max-height:140px;overflow:auto;border:1px solid var(--line);padding:5px}.batch-list label{display:block}@media(max-width:1000px){.layout{grid-template-columns:180px 1fr}.right{grid-column:1/-1;height:50vh}}
</style></head><body>
<header><h1>增量漏检人工 Review（单 reviewer）</h1><div class="hint">蓝色框为已接受 Portable Gold 基线，只读；不显示 parser 候选、预测或置信度。只需添加基线之外的新对象。</div></header>
<main class="layout"><aside class="panel"><strong>页面</strong><div id="progress" class="status"></div><div id="migration" class="status"></div><div id="pages" class="pages"></div><h2>批量确认</h2><div id="batch" class="batch-list"></div><button id="batch-empty">批量确认所选页无额外对象</button></aside>
<section class="panel"><div class="toolbar"><button id="mode-select" class="primary">框选 BBox</button><button id="mode-pan">平移页面</button><button id="zoom-out">缩小 −</button><button id="zoom-in">放大 +</button><button id="fit">适合窗口</button><button id="zoom-100">100%</button><button id="adopt" disabled>采用当前框</button></div><div id="coord" class="readout">鼠标坐标：</div><div id="selection" class="readout">当前框选：</div><div id="viewport" class="viewport"><div id="stage" class="stage"><img id="page-image" draggable="false"><canvas id="overlay"></canvas></div></div></section>
<aside class="panel right"><div id="identity" class="hint"></div><div class="field"><label>页审阅状态</label><select id="page-status"><option value="pending">pending 未完成</option><option value="complete">complete 已逐页检查</option></select></div><label><input id="empty-page" type="checkbox"> 本页没有任何对象（仅无蓝色基线的页面可用）</label><div class="field"><label>本页观察（可选）</label><textarea id="page-observation"></textarea></div><h2>基线及新增对象</h2><button id="add-object" class="primary">新增漏检对象</button><div id="objects"></div>
<div class="declarations"><h2>独立性声明</h2><label><input class="decl" data-key="inspected_every_full_page" type="checkbox"> 已逐页检查全部完整 render</label><label><input class="decl" data-key="did_not_view_parser_candidates_or_predictions" type="checkbox"> 审阅期间未查看 parser 候选/预测</label><label><input class="decl" data-key="independent_from_proposer" type="checkbox"> reviewer 独立于 proposer</label><label><input class="decl" data-key="not_evaluator" type="checkbox"> reviewer 不担任后续 evaluator</label><label><input class="decl" data-key="single_reviewer_scope_understood" type="checkbox"> 理解本步骤只用一名 reviewer 且仅形成 bounded recall ledger</label></div>
<h2>保存 / 导入 / 导出</h2><input id="import-file" type="file" accept=".json,application/json"><button id="import">导入已有 submission</button><button id="export" class="primary">检查并导出 submission</button><div id="message" class="status">草稿只保存在本浏览器。</div></aside></main>
<script id="pack" type="application/json">__PACK_JSON__</script><script>
const PACK=JSON.parse(document.getElementById('pack').textContent);const KEY='pvrw:'+PACK.workbench_instance_id+':'+PACK.reviewer.reviewer_instance;const KINDS=['figure','table','chart','equation'];
const blank=p=>{const r=p.initial_review||{};return {page_review_status:r.page_review_status||'pending',no_relevant_visual_objects:!!r.no_relevant_visual_objects,objects:(r.objects||[]).map(o=>({...o,locked_baseline:!!o.locked_baseline})),observation:r.observation||''}};let saved=null;try{saved=JSON.parse(localStorage.getItem(KEY)||'null')}catch(e){}let state=saved&&saved.pages? saved:{pages:Object.fromEntries(PACK.pages.map(p=>[p.physical_page,blank(p)])),declarations:{}};let current=PACK.pages[0].physical_page,selected=null,mode='select',zoom=1,fitMode=true,drag=null,pan=null,selection=null;
const $=id=>document.getElementById(id),page=()=>PACK.pages.find(p=>p.physical_page===current),draft=()=>state.pages[current];function save(){localStorage.setItem(KEY,JSON.stringify(state));renderNav();}
function renderNav(){const done=PACK.pages.filter(p=>state.pages[p.physical_page]?.page_review_status==='complete').length;$('progress').textContent=`${done} / ${PACK.pages.length} 页完成`;$('migration').textContent=`已迁移：${PACK.migration_summary.baseline_object_count} 个锁定基线；旧对象 ${PACK.migration_summary.legacy_objects_matched_to_baseline} 个与基线去重；新增 ${PACK.migration_summary.migrated_additional_object_count} 个。请重新确认声明。`;$('pages').innerHTML='';$('batch').innerHTML='';for(const p of PACK.pages){const b=document.createElement('button');b.textContent=`物理页 ${p.physical_page}`;b.className=(p.physical_page===current?'active ':'')+(state.pages[p.physical_page]?.page_review_status==='complete'?'done':'');b.onclick=()=>openPage(p.physical_page);$('pages').appendChild(b);const l=document.createElement('label');l.innerHTML=`<input type="checkbox" value="${p.physical_page}"> 页 ${p.physical_page}`;$('batch').appendChild(l)}}
function openPage(n){current=n;selected=null;selection=null;fitMode=true;renderNav();renderForm();loadImage()}
function loadImage(){const p=page(),img=$('page-image');img.onload=()=>{fit();draw()};img.src=p.render_path}
function fit(){const p=page(),v=$('viewport');zoom=Math.min((v.clientWidth-40)/p.render_dimensions_px.width,(v.clientHeight-40)/p.render_dimensions_px.height,1);fitMode=true;sizeStage()}
function sizeStage(){const p=page(),s=$('stage');s.style.width=(p.render_dimensions_px.width*zoom)+'px';s.style.height=(p.render_dimensions_px.height*zoom)+'px';const c=$('overlay');c.width=Math.max(1,Math.round(p.render_dimensions_px.width*zoom));c.height=Math.max(1,Math.round(p.render_dimensions_px.height*zoom));draw()}
function pdfPoint(e){const r=$('overlay').getBoundingClientRect(),p=page();return [Math.max(0,Math.min(p.width_points,(e.clientX-r.left)/r.width*p.width_points)),Math.max(0,Math.min(p.height_points,(e.clientY-r.top)/r.height*p.height_points))]}
function pxBox(b){const p=page(),c=$('overlay');return [b[0]/p.width_points*c.width,b[1]/p.height_points*c.height,b[2]/p.width_points*c.width,b[3]/p.height_points*c.height]}
function draw(){const c=$('overlay'),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);draft().objects.forEach((o,i)=>{const b=pxBox(o.bbox),active=i===selected;x.strokeStyle=o.locked_baseline?'#2563eb':(active?'#7c3aed':'#16a34a');x.lineWidth=active?3:2;x.strokeRect(b[0],b[1],b[2]-b[0],b[3]-b[1]);x.fillStyle=x.strokeStyle;x.fillText(`${i+1} ${o.kind}${o.locked_baseline?' 基线':''}`,b[0]+3,b[1]+13)});if(selection){const b=pxBox(selection);x.strokeStyle='#facc15';x.lineWidth=2;x.setLineDash([6,4]);x.strokeRect(b[0],b[1],b[2]-b[0],b[3]-b[1]);x.setLineDash([])}}
function renderForm(){const d=draft(),hasBaseline=d.objects.some(o=>String(o.object_id).startsWith('baseline-'));$('page-status').value=d.page_review_status;$('empty-page').checked=d.no_relevant_visual_objects;$('empty-page').disabled=hasBaseline;$('page-observation').value=d.observation;$('identity').textContent=`${PACK.reviewer.reviewer_instance} · ${PACK.reviewer.review_session_id} · 页面 ${current}`;$('objects').innerHTML='';d.objects.forEach((o,i)=>{o.locked_baseline=String(o.object_id).startsWith('baseline-');const locked=o.locked_baseline;const div=document.createElement('div');div.className='object '+(i===selected?'selected':'');div.innerHTML=`<div><strong>对象 ${i+1}${locked?' · 已接受 Gold 基线（锁定）':' · reviewer 新增'}</strong> <button data-pick>选中</button> ${locked?'':'<button data-del>删除</button>'}</div><div class="field"><label>类型</label><select data-kind ${locked?'disabled':''}>${KINDS.map(k=>`<option ${o.kind===k?'selected':''}>${k}</option>`).join('')}</select></div><div class="field"><label>BBox [x0,y0,x1,y1]</label><input data-bbox value="${o.bbox.map(v=>Number(v).toFixed(2)).join(', ')}" readonly></div><div class="field"><label>label_or_caption（可空）</label><input data-label value="${esc(o.label_or_caption)}" ${locked?'disabled':''}></div><div class="field"><label>notes（可空）</label><textarea data-notes ${locked?'disabled':''}>${esc(o.notes)}</textarea></div>`;div.querySelector('[data-pick]').onclick=()=>{selected=i;renderForm();draw()};const del=div.querySelector('[data-del]');if(del)del.onclick=()=>{d.objects.splice(i,1);selected=null;save();renderForm();draw()};div.querySelector('[data-kind]').onchange=e=>{o.kind=e.target.value;save();draw()};div.querySelector('[data-label]').oninput=e=>{o.label_or_caption=e.target.value;save()};div.querySelector('[data-notes]').oninput=e=>{o.notes=e.target.value;save()};$('objects').appendChild(div)});document.querySelectorAll('.decl').forEach(n=>n.checked=!!state.declarations[n.dataset.key]);draw()}
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
$('page-status').onchange=e=>{draft().page_review_status=e.target.value;save()};$('empty-page').onchange=e=>{if(e.target.checked&&draft().objects.length){alert('本页已有对象，不能同时标记为空。');e.target.checked=false;return}draft().no_relevant_visual_objects=e.target.checked;save()};$('page-observation').oninput=e=>{draft().observation=e.target.value;save()};
$('add-object').onclick=()=>{if(draft().no_relevant_visual_objects){draft().no_relevant_visual_objects=false}const next=1+draft().objects.reduce((m,o)=>Math.max(m,Number((String(o.object_id).match(/-o(\d+)$/)||[])[1]||0)),0);draft().objects.push({object_id:`p${current}-o${next}`,kind:'figure',bbox:[0,0,0,0],label_or_caption:'',notes:'',locked_baseline:false});selected=draft().objects.length-1;save();renderForm()};
document.querySelectorAll('.decl').forEach(n=>n.onchange=e=>{state.declarations[e.target.dataset.key]=e.target.checked;save()});
$('mode-select').onclick=()=>{mode='select';$('viewport').classList.remove('pan')};$('mode-pan').onclick=()=>{mode='pan';$('viewport').classList.add('pan')};$('zoom-in').onclick=()=>{zoom=Math.min(3,zoom*1.2);fitMode=false;sizeStage()};$('zoom-out').onclick=()=>{zoom=Math.max(.15,zoom/1.2);fitMode=false;sizeStage()};$('fit').onclick=fit;$('zoom-100').onclick=()=>{zoom=1;fitMode=false;sizeStage()};
$('overlay').onpointerdown=e=>{if(mode==='pan'){pan=[e.clientX,e.clientY,$('viewport').scrollLeft,$('viewport').scrollTop];return}drag=pdfPoint(e);selection=null};$('overlay').onpointermove=e=>{const p=pdfPoint(e);$('coord').textContent=`鼠标坐标：x=${p[0].toFixed(2)}, y=${p[1].toFixed(2)} PDF points`;if(pan){$('viewport').scrollLeft=pan[2]-(e.clientX-pan[0]);$('viewport').scrollTop=pan[3]-(e.clientY-pan[1]);return}if(drag){selection=[Math.min(drag[0],p[0]),Math.min(drag[1],p[1]),Math.max(drag[0],p[0]),Math.max(drag[1],p[1])];$('selection').textContent=`当前框选：[x0=${selection[0].toFixed(2)}, y0=${selection[1].toFixed(2)}, x1=${selection[2].toFixed(2)}, y1=${selection[3].toFixed(2)}]`;draw()}};window.onpointerup=()=>{drag=null;pan=null;$('adopt').disabled=!selection};
$('adopt').onclick=()=>{if(selected===null||!selection){alert('请先新增/选中对象并框选 BBox。');return}if(draft().objects[selected].locked_baseline){alert('已接受 Gold 基线不可修改。');return}if(selection[2]-selection[0]<1||selection[3]-selection[1]<1){alert('BBox 太小。');return}draft().objects[selected].bbox=selection.map(v=>Number(v.toFixed(4)));selection=null;$('adopt').disabled=true;save();renderForm()};
$('batch-empty').onclick=()=>{const ids=[...$('batch').querySelectorAll('input:checked')].map(n=>Number(n.value));if(!ids.length)return;if(!confirm(`确认你已逐页看过这 ${ids.length} 页，未发现蓝色基线之外的额外对象？`))return;for(const id of ids){const d=state.pages[id],additional=d.objects.filter(o=>!String(o.object_id).startsWith('baseline-'));if(additional.length){alert(`页 ${id} 已有 reviewer 新增对象，批量操作已停止。`);return}}for(const id of ids){const d=state.pages[id],hasBaseline=d.objects.some(o=>String(o.object_id).startsWith('baseline-'));Object.assign(d,{page_review_status:'complete',no_relevant_visual_objects:!hasBaseline,observation:d.observation||'增量复核：未发现已接受 Gold 基线之外的额外对象。'})}save();renderForm()};
function validateSubmission(s){if(s.schema_version!==PACK.schema_version.replace('workbench','submission')||s.protocol!==PACK.protocol||s.review_mode!=='incremental-missed-object-review'||s.workbench_instance_id!==PACK.workbench_instance_id||s.manifest_sha256!==PACK.manifest_sha256||s.source_sha256!==PACK.source_sha256||s.baseline_gold_revision_manifest_sha256!==PACK.baseline.gold_revision_manifest_sha256||s.legacy_submission_sha256!==PACK.migration_summary.legacy_submission_sha256||s.reviewer_instance!==PACK.reviewer.reviewer_instance||s.review_session_id!==PACK.reviewer.review_session_id)throw Error('submission 身份/源/基线/旧提交/workbench 不匹配');if(!Array.isArray(s.pages)||s.pages.length!==PACK.pages.length)throw Error('页面数量不匹配');for(const p of PACK.pages){const r=s.pages.find(x=>x.physical_page===p.physical_page);if(!r||r.render_sha256!==p.render_sha256)throw Error(`页 ${p.physical_page} render 绑定不匹配`);for(const b of (p.initial_review?.objects||[]).filter(o=>o.locked_baseline)){const got=(r.objects||[]).find(o=>o.object_id===b.object_id),expected=JSON.stringify({object_id:b.object_id,kind:b.kind,bbox:b.bbox,label_or_caption:b.label_or_caption,notes:b.notes});if(!got||JSON.stringify({object_id:got.object_id,kind:got.kind,bbox:got.bbox,label_or_caption:got.label_or_caption,notes:got.notes})!==expected)throw Error(`页 ${p.physical_page} 的锁定 Gold 基线缺失或被修改`)}}}
$('import').onclick=async()=>{const f=$('import-file').files[0];if(!f)return;try{const s=JSON.parse(await f.text());validateSubmission(s);state.pages=Object.fromEntries(s.pages.map(r=>[r.physical_page,{page_review_status:r.page_review_status,no_relevant_visual_objects:r.no_relevant_visual_objects,objects:r.objects,observation:r.observation}]));state.declarations=s.declarations;save();renderForm();$('message').textContent='导入成功。'}catch(e){$('message').textContent='导入失败：'+e.message}};
$('export').onclick=()=>{const declarations=['inspected_every_full_page','did_not_view_parser_candidates_or_predictions','independent_from_proposer','not_evaluator','single_reviewer_scope_understood'];const issues=[];for(const k of declarations)if(state.declarations[k]!==true)issues.push('声明未确认：'+k);const rows=[];for(const p of PACK.pages){const d=state.pages[p.physical_page];if(d.page_review_status!=='complete')issues.push(`页 ${p.physical_page} 未 complete`);if(d.no_relevant_visual_objects===Boolean(d.objects.length))issues.push(`页 ${p.physical_page} 空页标记与对象列表矛盾`);for(const o of d.objects){if(o.bbox[2]-o.bbox[0]<1||o.bbox[3]-o.bbox[1]<1)issues.push(`页 ${p.physical_page} 有对象尚未框选有效 BBox`)}rows.push({physical_page:p.physical_page,page_review_status:d.page_review_status,no_relevant_visual_objects:d.no_relevant_visual_objects,objects:d.objects.map(o=>({object_id:o.object_id,kind:o.kind,bbox:o.bbox,label_or_caption:o.label_or_caption,notes:o.notes})),observation:d.observation,render_sha256:p.render_sha256})}if(issues.length){$('message').textContent='不能导出：'+issues.slice(0,5).join('；');return}const out={schema_version:PACK.schema_version.replace('workbench','submission'),protocol:PACK.protocol,review_mode:'incremental-missed-object-review',workbench_instance_id:PACK.workbench_instance_id,manifest_sha256:PACK.manifest_sha256,source_sha256:PACK.source_sha256,baseline_gold_revision_manifest_sha256:PACK.baseline.gold_revision_manifest_sha256,legacy_submission_sha256:PACK.migration_summary.legacy_submission_sha256,reviewer_instance:PACK.reviewer.reviewer_instance,review_session_id:PACK.reviewer.review_session_id,declarations:Object.fromEntries(declarations.map(k=>[k,true])),pages:rows,exported_at:new Date().toISOString()};const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)+'\n'],{type:'application/json'}));a.download=`visual-incremental-recall-submission-${PACK.workbench_instance_id}-${PACK.reviewer.reviewer_instance}.json`;a.click();URL.revokeObjectURL(a.href);$('message').textContent='增量 submission 已导出；它还不是 Recall 结果。'};
window.onresize=()=>{if(fitMode)fit()};renderNav();renderForm();loadImage();
</script></body></html>'''


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--source", type=Path, required=True)
    generate.add_argument("--visual-parser-job", type=Path, required=True)
    generate.add_argument("--candidate-workpack", type=Path, required=True)
    generate.add_argument("--baseline-gold-revision", type=Path, required=True)
    generate.add_argument("--legacy-workbench", type=Path, required=True)
    generate.add_argument("--legacy-submission", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--reviewer-id", required=True)
    generate.add_argument("--review-session-id", required=True)
    generate.add_argument("--proposer-instance", required=True)
    validate = commands.add_parser("validate-workbench")
    validate.add_argument("--workbench", type=Path, required=True)
    validate.add_argument("--visual-parser-job", type=Path, required=True)
    validate.add_argument("--candidate-workpack", type=Path, required=True)
    validate.add_argument("--baseline-gold-revision", type=Path, required=True)
    validate.add_argument("--legacy-workbench", type=Path, required=True)
    validate.add_argument("--legacy-submission", type=Path, required=True)
    final = commands.add_parser("finalize")
    final.add_argument("--workbench", type=Path, required=True)
    final.add_argument("--visual-parser-job", type=Path, required=True)
    final.add_argument("--candidate-workpack", type=Path, required=True)
    final.add_argument("--baseline-gold-revision", type=Path, required=True)
    final.add_argument("--legacy-workbench", type=Path, required=True)
    final.add_argument("--legacy-submission", type=Path, required=True)
    final.add_argument("--submission", type=Path, required=True)
    final.add_argument("--output", type=Path, required=True)
    revision = commands.add_parser("validate-revision")
    revision.add_argument("--workbench", type=Path, required=True)
    revision.add_argument("--visual-parser-job", type=Path, required=True)
    revision.add_argument("--candidate-workpack", type=Path, required=True)
    revision.add_argument("--baseline-gold-revision", type=Path, required=True)
    revision.add_argument("--legacy-workbench", type=Path, required=True)
    revision.add_argument("--legacy-submission", type=Path, required=True)
    revision.add_argument("--revision", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            result = generate_workbench(source=args.source, visual_parser_job=args.visual_parser_job, candidate_workpack=args.candidate_workpack, baseline_gold_revision=args.baseline_gold_revision, legacy_workbench=args.legacy_workbench, legacy_submission=args.legacy_submission, output=args.output, reviewer_id=args.reviewer_id, review_session_id=args.review_session_id, proposer_instance=args.proposer_instance)
        elif args.command == "validate-workbench":
            result = validate_workbench(args.workbench, args.visual_parser_job, args.candidate_workpack, args.baseline_gold_revision, args.legacy_workbench, args.legacy_submission)
        elif args.command == "finalize":
            result = finalize(workbench=args.workbench, visual_parser_job=args.visual_parser_job, candidate_workpack=args.candidate_workpack, baseline_gold_revision=args.baseline_gold_revision, legacy_workbench=args.legacy_workbench, legacy_submission=args.legacy_submission, submission=args.submission, output=args.output)
        else:
            result = validate_revision(workbench=args.workbench, visual_parser_job=args.visual_parser_job, candidate_workpack=args.candidate_workpack, baseline_gold_revision=args.baseline_gold_revision, legacy_workbench=args.legacy_workbench, legacy_submission=args.legacy_submission, revision=args.revision)
    except PortableVisualRecallError as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
