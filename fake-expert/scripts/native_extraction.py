"""Transactional native extraction and read-only validation of frozen inputs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

PROTOCOL = "native-extraction-checkpoint-v0.1"
RECEIPT = "extraction-receipt.json"
TOOLS = ("native_extraction.py","native_text_geometry.py","native_table_geometry.py","native_structure_selection.py","pdf_structure.py","heading_locator.py",
         "semantic_promotion.py","semantic_authoring.py","visual_semantics.py","figure_crop_geometry.py","numeric_evidence.py","formula_numeric_evidence.py","unlabelled_graphics.py","compiler_version.py")


class ExtractionError(ValueError):
    def __init__(self,code,detail,retained=None):
        super().__init__(detail)
        self.code,self.retained=code,retained


def digest(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def bindings(source, scripts, renderer, selections=None):
    return {"source_sha256":digest(source),"tools":{name:digest(scripts/name) for name in TOOLS},
            "renderer_sha256":digest(Path(renderer)) if renderer else None,
            "structure_selections_sha256":digest(selections) if selections else None}


def validate_checkpoint(root, expected):
    receipt=root/RECEIPT
    if root.is_symlink() or receipt.is_symlink():return ["extraction_symlink_forbidden"]
    if not receipt.is_file():return ["extraction_receipt_missing"]
    try:
        data=json.loads(receipt.read_text())
        if data.get("protocol")!=PROTOCOL or data.get("bindings")!=expected:return ["extraction_binding_drift"]
        if not isinstance(data.get("files"),dict) or not data["files"]:return ["extraction_inventory_invalid"]
        for name,value in data["files"].items():
            relative=Path(name)
            path=root/relative
            if relative.is_absolute() or '..' in relative.parts or any(part.startswith('.') for part in relative.parts) or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file() or digest(path)!=value:
                return ["extraction_file_drift:"+name]
    except (OSError,ValueError,TypeError):return ["extraction_receipt_invalid"]
    return []


def extract(source, workspace, scripts, minimum_characters, selections=None):
    import pdf_structure
    import semantic_authoring
    import semantic_promotion

    destination=workspace/'extraction'
    renderer=shutil.which('pdftoppm')
    expected=bindings(source,scripts,renderer,selections)
    if destination.is_symlink():raise ExtractionError("extraction_workpack_mismatch","extraction_symlink_forbidden")
    if destination.exists():
        if any(destination.rglob('drafts/*.json')):
            raise ExtractionError("extraction_workpack_mismatch","Preserve the unbound workpack and replan into a fresh workspace.")
        destination.rename(workspace/f"extraction-incomplete-{uuid.uuid4().hex[:12]}")
    stage=Path(tempfile.mkdtemp(prefix='extraction-staging-',dir=workspace))
    try:
        ir=pdf_structure.compile_pdf_intermediate_representation(source,1,None,minimum_characters,'all','pypdf',renderer,
                                                                 structure_selections=selections)
        pdf_structure.write_intermediate_representation(stage/'ir',ir)
        semantic_promotion.build_semantic_workpack(source,stage/'ir',stage/'workpack',pdftoppm=Path(renderer) if renderer else None)
        # Prompts contain local paths: prepare at their final location only.
        stage.rename(destination)
        packets=semantic_authoring.prepare(destination/'workpack')
        files={str(p.relative_to(destination)):digest(p) for p in destination.rglob('*') if p.is_file()}
        receipt={"protocol":PROTOCOL,"bindings":expected,"files":files}
        target=destination/RECEIPT
        target.write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n');os.chmod(target,0o600)
        return {"renderer":renderer,"unit_count":packets['unit_count'],"figure_count":packets['figure_count']}
    except Exception as error:
        retained=workspace/f"extraction-failed-{uuid.uuid4().hex[:12]}"
        failed=stage if stage.exists() else destination
        if failed.exists():failed.rename(retained)
        detail=str(error)
        code='structure_review_required' if any(token in detail for token in ('heading_resolutions_empty','heading_resolution_required','ir_segments_missing','structure_selection_locator_mismatch')) else 'extraction_failed'
        (retained/'failure.json').write_text(json.dumps({"code":code,"detail":detail},indent=2))
        raise ExtractionError(code,detail,str(retained)) from error
