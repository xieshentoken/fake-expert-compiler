"""Numeric evidence gates for proposed facts; no review authoring or inference."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

PROTOCOL = "numeric-evidence-v0.2"
# Whole numeric tokens only: never backtrack into the suffix of k12 or q999.
NUMBER_PATTERN = re.compile(r"(?<![A-Za-zΑ-ω_\d.])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?(?![A-Za-zΑ-ω_\d]|\.\d)")
LABEL_PATTERN = re.compile(r"\(\s*\d+(?:[.-]\d+)*[A-Za-z]?\s*\)")
POWER_PATTERN = re.compile(r"([A-Za-zΑ-ω][A-Za-zΑ-ω_]*)\s*(?:\*\*|\^)\s*([-+]?\d+)")


class NumericEvidenceError(ValueError):
    pass


def _matching_text(text):
    # Matching view only. Canonical source text, offsets and hashes stay intact.
    text = re.sub(r"([A-Za-zΑ-ω])([⁰¹²³⁴⁵⁶⁷⁸⁹]+)",lambda m:m[1]+"**"+unicodedata.normalize("NFKC",m[2]),str(text))
    return unicodedata.normalize("NFKC",text).replace("−","-")


def _canonical_number(value):
    try:
        number=Decimal(value)
        if not number.is_finite() or abs(number.adjusted())>300:return value
        return format(number.normalize(),'f')
    except InvalidOperation:return value


def scalar_numbers(text):
    return {_canonical_number(m[0]) for m in NUMBER_PATTERN.finditer(_matching_text(text))}


def _formula_roles(text):
    text=LABEL_PATTERN.sub('',_matching_text(text))
    # A detached glyph suffix such as L 2 is ambiguous after PDF flattening.
    # It cannot authorize a scalar coefficient or an exponent without geometry.
    text=re.sub(r'(?<![A-Za-zΑ-ω_])([A-Za-zΑ-ω])\s+(\d+)(?![A-Za-zΑ-ω_\d])',r'\1',text)
    powers=list(POWER_PATTERN.finditer(text))
    roles={('exponent',m[1],_canonical_number(m[2])) for m in powers}
    hidden=list(text)
    for m in powers:
        for i in range(m.start(2),m.end(2)):hidden[i]=' '
    roles.update(('value','',n) for n in scalar_numbers(''.join(hidden)))
    return roles


def untraced_numbers(proposal: Mapping[str, Any], evidence_text: str, *, verified_formula_binding=None) -> list[str]:
    fields=[proposal.get("title",""),proposal.get("statement",""),*proposal.get("assumptions",[]),
            *proposal.get("valid_when",[]),*proposal.get("fails_when",[])]
    missing=scalar_numbers(' '.join(str(v) for v in fields))-scalar_numbers(evidence_text)
    formula=proposal.get('formula')
    if isinstance(formula,dict):
        supported=_formula_roles(evidence_text)
        if verified_formula_binding:
            supported.update((r['role'],r['base'],r['value']) for r in verified_formula_binding['supported_numbers'])
        aliases=formula.get('notation_symbols',{})
        if not isinstance(aliases,dict):aliases={}
        for field in ('source_notation','normalized_notation','derived_notation'):
            for role,base,value in _formula_roles(formula.get(field,'') or ''):
                if (role,aliases.get(base,base),value) not in supported:missing.add(value)
    return sorted(missing)


def checked_untraced_numbers(proposal, evidence_text, source):
    from formula_numeric_evidence import verify_native_binding
    try:
        binding=verify_native_binding(source,proposal)
    except (ValueError,KeyError,TypeError,OSError) as error:
        raise NumericEvidenceError(f"formula_numeric_binding_invalid:{error}") from error
    return untraced_numbers(proposal,evidence_text,verified_formula_binding=binding)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def check_legacy_numeric(records: dict, manifest: dict, source: Path | None = None) -> None:
    """Recheck old numeric facts before upgrading; old package bytes stay untouched."""
    from pdf_structure import normalize_text
    from pypdf import PdfReader

    sources = {row["source_id"]: row["sha256"] for row in manifest["sources"]}
    anchors = {row["id"]: row for row in records["anchors"]}
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest() if source else None
    reader = PdfReader(source) if source else None
    for obj in records["objects"]:
        if not untraced_numbers(obj, ""):
            continue
        texts = []
        for identifier in obj["evidence_ids"]:
            anchor = anchors[identifier]
            if sources.get(anchor["source_id"]) != source_hash or reader is None or anchor.get("scan_locator"):
                raise NumericEvidenceError("legacy_numeric_revalidation_required:matching_native_source")
            for span in anchor["text_spans"]:
                text = normalize_text(reader.pages[span["page"] - 1].extract_text() or "")
                excerpt = text[span["start"]:span["end"]]
                if hashlib.sha256(text.encode()).hexdigest() != span["page_text_sha256"] or hashlib.sha256(excerpt.encode()).hexdigest() != span["span_sha256"]:
                    raise NumericEvidenceError("legacy_numeric_revalidation_required:anchor_drift")
                texts.append(excerpt)
        proposal = obj
        binding = (obj.get("formula") or {}).get("native_numeric_evidence")
        if binding:
            spans = [{k:s[k] for k in ("page", "start", "end", "span_sha256")}
                     for identifier in obj["evidence_ids"] for s in anchors[identifier]["text_spans"]]
            ordered = lambda rows: sorted(rows, key=lambda row:(row["page"],row["start"],row["end"]))
            if ordered(spans) != ordered(binding.get("evidence_spans", [])):
                raise NumericEvidenceError("legacy_numeric_revalidation_required:formula_anchor_drift")
            proposal = {**obj, "evidence_spans":binding["evidence_spans"]}
        if checked_untraced_numbers(proposal, " ".join(texts), source):
            raise NumericEvidenceError("legacy_numeric_revalidation_required:untraced_values")


def reviewed_figure_basis(source: Path, root: Path, pending: Mapping[str, dict]) -> dict[str, dict]:
    """Consume the existing full semantic + visual Review, bound to exact proposals.

    A filename, a boolean, or a figure-presence review cannot clear this gate.
    The existing validators check the frozen source, proposal, geometry, inputs,
    registry/session, assertions and external attestations before counting reviewers.
    """
    if not pending:
        return {}
    from semantic_promotion import validate_reviewed_workpack

    try:
        issues, _ = validate_reviewed_workpack(source, root)
        errors = [issue.code for issue in issues if issue.severity == "error"]
        if errors:
            raise NumericEvidenceError(f"numeric_review_required:{errors[0]}")
        plan = _load(root / "review-plan.json")
        if plan.get("review_protocol") != "atomic-support-review-v2" or not plan.get("visual_review_protocol"):
            raise NumericEvidenceError("numeric_review_required:independent_atomic_and_visual_review")
        semantic = _rows(root / "review-attestations.jsonl")
        visual = _rows(root / "visual-review-attestations.jsonl")
        manifest = _load(root / "workpack.json")
        crops = manifest.get("visual_semantics", {}).get("figure_crops", {})
        result = {}
        for reference, proposal in pending.items():
            accepted = [row for row in semantic if row.get("item_ref") == reference
                        and row.get("verdict") == "accepted" and row.get("evidence_support") == "full"]
            if len({row.get("reviewer_instance") for row in accepted}) < 2:
                raise NumericEvidenceError("numeric_review_required:two_semantic_reviewers")
            used_visual = []
            for identifier in proposal["figure_object_ids"]:
                rows = [row for row in visual if identifier in row.get("visual_object_ids", [])
                        and row.get("verdict") == "accepted"]
                if len({row.get("reviewer_instance") for row in rows}) < 2:
                    raise NumericEvidenceError("numeric_review_required:two_visual_reviewers")
                crop = crops.get(identifier)
                if crop:
                    raw_path = root / crop["path"]
                    path = raw_path.resolve()
                    if not path.is_relative_to(root.resolve()) or raw_path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != crop["sha256"]:
                        raise NumericEvidenceError("numeric_review_required:figure_crop_drift")
                used_visual.extend(row["attestation_sha256"] for row in rows)
            result[reference] = {
                "protocol": PROTOCOL,
                "review_binding_sha256": _hash({"plan": plan, "proposal": proposal,
                    "semantic": accepted, "visual_attestations": sorted(set(used_visual))}),
            }
        return result
    except (OSError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, NumericEvidenceError):
            raise
        raise NumericEvidenceError(f"numeric_review_required:{type(error).__name__}") from error
