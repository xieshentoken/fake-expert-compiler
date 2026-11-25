"""Transactional native children over original physical PDF ranges.

These are versioned intake children, not modified v0.2 unified jobs. They never
rewrite/crop the source to bypass the old whole-source profile gate.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid
from typing import Any
from compiler_version import SCOPED_NATIVE_CHILD_SCHEMA
from native_focus import plan_digest

from direct_reference_skill import build_direct_reference, validate_direct_reference
from pdf_structure import PdfPipelineError, compile_pdf_intermediate_representation, write_intermediate_representation
from semantic_authoring import prepare
from semantic_promotion import SemanticPromotionError, build_semantic_workpack, preview_native_focus, validate_semantic_workpack

SCHEMA = SCOPED_NATIVE_CHILD_SCHEMA


class NativeChildError(ValueError):
    def __init__(self, detail: str, audit_path: Path):
        super().__init__(detail)
        self.audit_path = str(audit_path)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def preview(source, scope, renderer, minimum_characters, page_dispositions, selection):
    try:
        ir = compile_pdf_intermediate_representation(
            source, *scope, minimum_characters, pdftoppm=renderer, page_dispositions=page_dispositions,
            structure_selections=selection, include_render_receipts=False,
        )
        counts = preview_native_focus(source, ir)
    except (PdfPipelineError, SemanticPromotionError) as error:
        if selection and str(error).startswith("structure_selection"):
            raise ValueError(str(error)) from error
        return {"status": "review_required", "reason": str(error), "unit_count": 0,
                "split_segments": 0, "focus_characters": 0, "focus_plan_sha256": None}
    return {"status": "ready", "reason": None,
            **{k: counts[k] for k in ("unit_count", "split_segments", "focus_characters", "focus_plan_sha256")}}


def run_native_child(source: Path, scope: list[int], root: Path, *, bindings: dict[str, Any],
                     package: dict[str, Any], renderer: str, minimum_characters: int,
                     native_focus: dict[str, Any], page_dispositions=None, reused_workpack=None) -> dict[str, Any]:
    expected = {"source_sha256": _digest(source), "physical_pages": scope, "workflow_bindings": bindings,
                "package": package, "minimum_native_characters": minimum_characters, "native_focus": native_focus}
    selection = native_focus["structure_selections"]
    selection_path = Path(selection["path"]) if selection else None
    if selection_path and _digest(selection_path) != selection["sha256"]:
        raise ValueError("native_structure_selections_drift")
    from intake_workpack_reuse import verify_binding
    workpack = verify_binding(source,reused_workpack) if reused_workpack else root / "workpack"
    record_path = root / "child.json"
    if root.is_symlink():
        raise ValueError("native_child_symlink_forbidden")
    if not root.exists() or not any(root.iterdir()):
        root.parent.mkdir(parents=True, exist_ok=True)
        published = False
        try:
            with tempfile.TemporaryDirectory(prefix=f".{root.name}-staging-", dir=root.parent) as temporary:
                stage = Path(temporary) / "child"
                stage.mkdir()
                try:
                    if not reused_workpack:
                        ir = compile_pdf_intermediate_representation(source, scope[0], scope[1], minimum_characters,
                                                                     "all", "pypdf", renderer, page_dispositions=page_dispositions,
                                                                     structure_selections=selection_path)
                        if not ir["preflight"]["eligible_for_current_compiler"]:
                            raise ValueError("native_child_scope_not_eligible")
                        write_intermediate_representation(stage / "ir", ir)
                        manifest = build_semantic_workpack(source, stage / "ir", stage / "workpack",
                                                           pdftoppm=Path(renderer), partition_native=True)
                        forecast = native_focus["preview"]
                        if forecast and forecast["status"] == "ready":
                            rows = manifest["units"]
                            units = (json.loads((stage / "workpack" / row["path"]).read_text()) for row in rows)
                            actual_plan = plan_digest((unit["segment_id"], unit["focus_spans"]) for unit in units)
                            if actual_plan != forecast["focus_plan_sha256"]:
                                raise ValueError("native_focus_preview_drift")
                except Exception as error:
                    audit = root.parent.parent / "audit" / f"{root.name}-failed-{uuid.uuid4().hex[:12]}"
                    audit.parent.mkdir(parents=True, exist_ok=True)
                    stage.rename(audit)
                    os.chmod(audit, 0o700)
                    detail = str(error).replace(str(stage), str(audit))
                    _write(audit / "failure.json", {"status": "failed", "physical_pages": scope,
                                                    "source_sha256": expected["source_sha256"], "detail": detail,
                                                    "knowledge_package": False})
                    raise NativeChildError(detail, audit) from error
                if root.exists() and any(root.iterdir()):
                    raise ValueError("native_child_output_not_empty")
                stage.rename(root)
                published = True
                if not reused_workpack:
                    prepare(workpack)
                files = {path.relative_to(root).as_posix(): _digest(path) for path in root.rglob("*") if path.is_file()}
                _write(record_path, {"schema_version": SCHEMA, "bindings": expected, "files": files,
                                     "skill_manifest_sha256": None})
        except Exception:
            if published and root.exists():
                shutil.rmtree(root)
            raise
    if not record_path.is_file():
        raise ValueError("native_child_output_not_empty")
    record = json.loads(record_path.read_text())
    from intake_contracts import validate_definition
    validate_definition(record, "native_child_record")
    if record.get("schema_version") != SCHEMA or record.get("bindings") != expected:
        raise ValueError("native_child_binding_drift_replan")
    for name, digest in record.get("files", {}).items():
        relative = Path(name)
        path = root / relative
        if relative.is_absolute() or ".." in relative.parts or path.is_symlink() or not path.is_file() or _digest(path) != digest:
            raise ValueError(f"native_child_file_drift:{name}")
    skill = root / "skill"
    if record.get("skill_manifest_sha256"):
        if not (skill / "manifest.json").is_file() or _digest(skill / "manifest.json") != record["skill_manifest_sha256"] or validate_direct_reference(skill):
            raise ValueError("native_child_artifact_drift")
        return {"status": "draft", "code": "native_child_draft", "child": str(record_path),
                "physical_pages": scope, "package": str(skill), "whole_book_complete": False}
    issues, _summary = validate_semantic_workpack(source, workpack)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        missing = any("draft" in issue.code for issue in errors)
        return {"status": "paused", "code": "semantic_authoring_required" if missing else errors[0].code,
                "child": str(record_path), "workpack": str(workpack), "physical_pages": scope,
                "issues": [issue.to_dict() for issue in errors[:10]], "whole_book_complete": False,
                "next_action": "author_existing_workpack_then_replan_fresh_workflow" if reused_workpack and missing else "complete_existing_authoring_then_resume"}
    if skill.exists() and any(skill.iterdir()):
        raise ValueError("native_child_output_not_empty")
    with tempfile.TemporaryDirectory(prefix=".native-skill-", dir=root) as temporary:
        stage = Path(temporary) / "skill"
        figures = package["figures"]
        result = build_direct_reference(source, workpack, stage, name=package["name"],
                                        display_name=package["display_name"], domain=package["domain"],
                                        version=package["version"], created_at=package["created_at"],
                                        figure_index=figures != "none", figure_images=figures == "index+images",
                                        term_alias_extensions=package.get("term_aliases", {}).get("extensions"))
        stage.rename(skill)
    record["skill_manifest_sha256"] = _digest(skill / "manifest.json")
    _write(record_path, record)
    result.update(package=str(skill), code="native_child_draft", child=str(record_path),
                  physical_pages=scope, whole_book_complete=False)
    return result
