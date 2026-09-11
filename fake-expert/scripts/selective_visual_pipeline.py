#!/usr/bin/env python3
"""Selective, local-first visual routing for sparse technical PDFs.

This module is deliberately independent from the OCR/PP-Structure worker.  It
implements the cheap, deterministic policy and census layer and provides the
small execution contracts used by the visual parser orchestrator.  A census is
only a routing hint: it is never a visual fact, a Gold record, or a review
attestation.

The implementation uses only the Python standard library and pypdf when a PDF
is supplied.  It never opens a model, sends a PDF/crop to a service, or writes
source bytes into an index.  All identity functions exclude scheduling,
temporary-path, and wall-clock measurements by construction.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from compiler_version import (
    LEGACY_TECHNICAL_CHART_PROFILE_ID,
    LEGACY_TECHNICAL_CHART_PROFILE_PROTOCOL,
    SELECTIVE_VISUAL_COMPILER_VERSION,
    SELECTIVE_VISUAL_PROTOCOL,
    TECHNICAL_CHART_CANDIDATE_SCHEMA,
    TECHNICAL_CHART_PROFILE_ID,
    TECHNICAL_CHART_PROFILE_PROTOCOL,
    VISUAL_BATCH_RECEIPT_SCHEMA,
    VISUAL_BUDGET_RECEIPT_SCHEMA,
    VISUAL_CENSUS_SCHEMA,
    VISUAL_COVERAGE_SCHEMA,
    VISUAL_INDEX_SCHEMA,
    VISUAL_POLICY_SCHEMA,
    VISUAL_ROUTING_SCHEMA,
)

VISUAL_MODES = frozenset({"off", "auto", "full"})
VISUAL_CONTEXTS = frozenset({"none", "index", "on-demand"})
VISUAL_KINDS = ("table", "figure", "chart", "diagram", "equation")
VISUAL_KIND_SET = frozenset(VISUAL_KINDS)
ROUTE_STATUSES = frozenset(
    {
        "skip",
        "not-requested",
        "not-inspected-by-policy",
        "native-visual-only",
        "table-candidate",
        "chart-candidate",
        "manual-review",
        "budget-exhausted",
        "processed",
        "candidate",
        "conflict",
        "review-required",
    }
)
COVERAGE_STATUSES = frozenset(
    {
        "processed",
        "not-requested",
        "not-inspected-by-policy",
        "budget-exhausted",
        "candidate",
        "conflict",
        "review-required",
    }
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_LABEL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "figure": (
        re.compile(r"(?im)^\s*(?:fig(?:ure)?\.?|图)\s*[A-Za-z0-9一二三四五六七八九十.-]+"),
    ),
    "table": (
        re.compile(r"(?im)^\s*(?:tab(?:le)?\.?|表)\s*[A-Za-z0-9一二三四五六七八九十.-]+"),
    ),
    "equation": (
        re.compile(r"(?im)^\s*(?:eq(?:uation)?\.?|式)\s*[A-Za-z0-9一二三四五六七八九十.-]+"),
    ),
    "chart": (
        re.compile(r"(?i)\b(?:chart|plot|axis|axes|legend|series|bar chart|line chart)\b"),
        re.compile(r"图表|坐标轴|图例|曲线|系列"),
    ),
    "diagram": (
        re.compile(r"(?i)\b(?:diagram|schematic|flowchart|block diagram|arrow)\b"),
        re.compile(r"示意图|流程图|框图|箭头"),
    ),
}


class VisualPolicyError(ValueError):
    """Stable fail-closed policy/census error."""


class VisualBudgetExhausted(VisualPolicyError):
    """Raised by a worker session when a declared budget is exhausted."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file_streaming(path: Path) -> str:
    """Hash a model/artifact without loading the complete file into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise VisualPolicyError(f"{name}_sha256_invalid")
    return value


def _positive_budget(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VisualPolicyError(f"{name}_invalid")
    return value


@dataclass(frozen=True)
class VisualBudget:
    """Finite limits for heavy visual execution.

    ``None`` means no declared limit for that dimension.  The policy hash still
    records the absence explicitly, so changing from unlimited to bounded is a
    cache/DAG change even when no work happens to be paused.
    """

    max_visual_pages: int | None = None
    max_visual_regions: int | None = None
    max_visual_seconds: float | None = None

    def __post_init__(self) -> None:
        for name in ("max_visual_pages", "max_visual_regions"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise VisualPolicyError(f"{name}_invalid")
        seconds = self.max_visual_seconds
        if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(float(seconds)) or float(seconds) < 0):
            raise VisualPolicyError("max_visual_seconds_invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_visual_pages": self.max_visual_pages,
            "max_visual_regions": self.max_visual_regions,
            "max_visual_seconds": self.max_visual_seconds,
        }


@dataclass(frozen=True)
class VisualPolicy:
    mode: str = "auto"
    kinds: tuple[str, ...] = VISUAL_KINDS
    budget: VisualBudget = field(default_factory=VisualBudget)
    context: str = "index"

    def __post_init__(self) -> None:
        if self.mode not in VISUAL_MODES:
            raise VisualPolicyError("visual_mode_unknown")
        kinds = tuple(self.kinds)
        if not kinds:
            raise VisualPolicyError("visual_kinds_empty")
        if any(kind not in VISUAL_KIND_SET for kind in kinds):
            unknown = next(kind for kind in kinds if kind not in VISUAL_KIND_SET)
            raise VisualPolicyError(f"visual_kind_unknown:{unknown}")
        if len(set(kinds)) != len(kinds):
            raise VisualPolicyError("visual_kinds_duplicate")
        if self.context not in VISUAL_CONTEXTS:
            raise VisualPolicyError("visual_context_unknown")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": VISUAL_POLICY_SCHEMA,
            "protocol": SELECTIVE_VISUAL_PROTOCOL,
            "mode": self.mode,
            "visual_kinds": list(self.kinds),
            "budget": self.budget.to_dict(),
            "visual_context": self.context,
            "candidate_only": True,
            "promotion": False,
            "review_authoring": False,
            "network_enabled": False,
            "model_download": False,
            "silent_fallback": False,
        }

    @property
    def policy_sha256(self) -> str:
        return sha256_json(self.to_dict())


def normalize_visual_policy(
    *,
    mode: str = "auto",
    visual_kinds: Sequence[str] | None = None,
    max_visual_pages: int | None = None,
    max_visual_regions: int | None = None,
    max_visual_seconds: float | None = None,
    context: str = "index",
) -> VisualPolicy:
    """Validate and canonicalize a user-facing visual policy."""

    if not isinstance(mode, str):
        raise VisualPolicyError("visual_mode_unknown")
    kinds = tuple(visual_kinds) if visual_kinds is not None else VISUAL_KINDS
    if any(not isinstance(kind, str) for kind in kinds):
        raise VisualPolicyError("visual_kind_invalid")
    budget = VisualBudget(
        max_visual_pages=_positive_budget(max_visual_pages, "max_visual_pages"),
        max_visual_regions=_positive_budget(max_visual_regions, "max_visual_regions"),
        max_visual_seconds=max_visual_seconds,
    )
    return VisualPolicy(mode=mode, kinds=kinds, budget=budget, context=context)


def visual_policy_from_mapping(value: Mapping[str, Any] | None) -> VisualPolicy:
    if value is None:
        return normalize_visual_policy()
    if not isinstance(value, Mapping):
        raise VisualPolicyError("visual_policy_object_required")
    allowed = {
        "schema_version", "protocol", "mode", "visual_kinds", "kinds", "budget",
        "max_visual_pages", "max_visual_regions", "max_visual_seconds",
        "visual_context", "context", "candidate_only", "promotion",
        "review_authoring", "network_enabled", "model_download", "silent_fallback",
        "policy_sha256",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise VisualPolicyError(f"visual_policy_unknown_property:{unknown[0]}")
    budget = value.get("budget") if isinstance(value.get("budget"), Mapping) else value
    kinds = value.get("visual_kinds", value.get("kinds"))
    if kinds is None:
        raise VisualPolicyError("visual_kinds_empty")
    policy = normalize_visual_policy(
        mode=str(value.get("mode", "auto")),
        visual_kinds=kinds,
        max_visual_pages=budget.get("max_visual_pages"),
        max_visual_regions=budget.get("max_visual_regions"),
        max_visual_seconds=budget.get("max_visual_seconds"),
        context=str(value.get("visual_context", value.get("context", "index"))),
    )
    if "schema_version" in value and value.get("schema_version") != VISUAL_POLICY_SCHEMA:
        raise VisualPolicyError("visual_policy_schema_mismatch")
    if "protocol" in value and value.get("protocol") != SELECTIVE_VISUAL_PROTOCOL:
        raise VisualPolicyError("visual_policy_protocol_mismatch")
    for field, expected in (("candidate_only", True), ("promotion", False), ("review_authoring", False), ("network_enabled", False), ("model_download", False), ("silent_fallback", False)):
        if field in value and value.get(field) is not expected:
            raise VisualPolicyError(f"visual_policy_{field}_invalid")
    if value.get("policy_sha256") is not None and value.get("policy_sha256") != policy.policy_sha256:
        raise VisualPolicyError("visual_policy_hash_mismatch")
    return policy


def build_visual_policy(**kwargs: Any) -> dict[str, Any]:
    """Return the closed, hash-bound policy record used by job manifests."""

    policy = normalize_visual_policy(**kwargs)
    record = policy.to_dict()
    record["policy_sha256"] = policy.policy_sha256
    return record


def _text_from_page(page: Any) -> str:
    try:
        text = page.extract_text() or ""
    except Exception:
        text = ""
    return str(text)


def _page_geometry(page: Any) -> tuple[float, float, int]:
    try:
        media = page.mediabox
        width, height = float(media.width), float(media.height)
    except Exception:
        width, height = 0.0, 0.0
    try:
        rotation = int(page.get("/Rotate", 0) or 0) % 360
    except Exception:
        rotation = 0
    return width, height, rotation


def _page_bytes(page: Any) -> bytes:
    try:
        contents = page.get_contents()
        if contents is None:
            return b""
        if isinstance(contents, (list, tuple)):
            return b"\n".join(obj.get_data() for obj in contents if hasattr(obj, "get_data"))
        if hasattr(contents, "get_data"):
            return contents.get_data()
    except Exception:
        return b""
    return b""


def _resource_counts(page: Any) -> tuple[int, int, int]:
    """Return (images, forms, xobjects) without dereferencing image bytes."""

    images = forms = xobjects = 0
    try:
        resources = page.get("/Resources")
        if resources is not None and hasattr(resources, "get_object"):
            resources = resources.get_object()
        xobject_map = resources.get("/XObject") if isinstance(resources, Mapping) else None
        if xobject_map is not None and hasattr(xobject_map, "get_object"):
            xobject_map = xobject_map.get_object()
        if isinstance(xobject_map, Mapping):
            xobjects = len(xobject_map)
            for value in xobject_map.values():
                try:
                    obj = value.get_object() if hasattr(value, "get_object") else value
                    kind = str(obj.get("/Subtype", "")) if isinstance(obj, Mapping) else ""
                    if kind == "/Image":
                        images += 1
                    elif kind == "/Form":
                        forms += 1
                except Exception:
                    continue
    except Exception:
        pass
    return images, forms, xobjects


def _drawing_signal(content: bytes) -> dict[str, int]:
    text = content.decode("latin-1", errors="ignore")
    # These are intentionally counts, not a geometry claim.  PDF path syntax
    # is used only as a cheap deterministic routing signal.
    return {
        "line_ops": len(re.findall(r"(?<![A-Za-z])m\s+[-+0-9.]+\s+[-+0-9.]+\s+l\s+[-+0-9.]+\s+[-+0-9.]+\s+s?", text)),
        "stroke_ops": len(re.findall(r"(?:\n|\r|\s)(?:S|s|B|b|f|F|n)(?:\s|\r|\n)", text)),
        "transform_ops": len(re.findall(r"\bcm\b", text)),
        "image_ops": len(re.findall(r"\bDo\b", text)),
    }


def _line_like_rows(text: str) -> int:
    rows = [line.strip() for line in text.splitlines() if line.strip()]
    return sum(1 for line in rows if len(re.split(r"\s{2,}|\t|\|", line)) >= 3)


def _signals_for_page(page: Any) -> tuple[dict[str, Any], str]:
    text = _text_from_page(page)
    content = _page_bytes(page)
    images, forms, xobjects = _resource_counts(page)
    drawings = _drawing_signal(content)
    width, height, rotation = _page_geometry(page)
    labels = {kind: sum(len(pattern.findall(text)) for pattern in patterns) for kind, patterns in _LABEL_PATTERNS.items()}
    table_rows = _line_like_rows(text)
    signals: dict[str, Any] = {
        "text_chars": len(text),
        "text_sha256": _sha256_text(text),
        "content_bytes": len(content),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "images": images,
        "forms": forms,
        "xobjects": xobjects,
        "line_ops": drawings["line_ops"],
        "stroke_ops": drawings["stroke_ops"],
        "transform_ops": drawings["transform_ops"],
        "image_ops": drawings["image_ops"],
        "tabular_text_rows": table_rows,
        "label_counts": labels,
        "page_width": width,
        "page_height": height,
        "rotation": rotation,
    }
    # Preserve only a short bounded text cue for audit navigation.  Never put
    # full OCR/native text into the census or index.
    cue = " ".join(text.split())[:160]
    return signals, cue


def _candidate_kinds(signals: Mapping[str, Any]) -> tuple[str, ...]:
    labels = signals.get("label_counts") if isinstance(signals.get("label_counts"), Mapping) else {}
    candidates: list[str] = []
    if int(labels.get("table", 0)) > 0 or int(signals.get("tabular_text_rows", 0)) >= 1 or (int(signals.get("line_ops", 0)) >= 2 and int(signals.get("stroke_ops", 0)) >= 2):
        candidates.append("table")
    if int(labels.get("figure", 0)) > 0 or int(signals.get("images", 0)) + int(signals.get("forms", 0)) > 0:
        candidates.append("figure")
    if int(labels.get("chart", 0)) > 0 or (int(signals.get("line_ops", 0)) >= 2 and int(signals.get("tabular_text_rows", 0)) == 0 and int(signals.get("images", 0)) > 0):
        candidates.append("chart")
    if int(labels.get("diagram", 0)) > 0:
        candidates.append("diagram")
    if int(labels.get("equation", 0)) > 0:
        candidates.append("equation")
    return tuple(candidates)


def _region_for_page(page_number: int, signals: Mapping[str, Any], kind: str) -> dict[str, Any]:
    width, height = float(signals.get("page_width", 0.0)), float(signals.get("page_height", 0.0))
    # This is a census envelope only.  It is deliberately non-exact and cannot
    # be used as a visual fact until a parser/reviewer provides a crop bbox.
    return {
        "region_id": f"census-region-{page_number}-{kind}",
        "physical_page": page_number,
        "kind": kind,
        "bbox_pdf_points": [0.0, 0.0, width, height],
        "geometry_precision": "page-envelope-non-exact",
        "candidate_only": True,
        "reason": "deterministic-native-signal",
    }


def run_visual_census(
    source: Path | str,
    *,
    pages: Sequence[int] | None = None,
    visual_kinds: Sequence[str] = VISUAL_KINDS,
    policy_sha256: str | None = None,
    census_id: str | None = None,
) -> dict[str, Any]:
    """Run a no-model, no-render visual census over selected PDF pages."""

    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file() or source_path.suffix.casefold() != ".pdf":
        raise VisualPolicyError("source_pdf_required")
    kinds = tuple(visual_kinds)
    if any(kind not in VISUAL_KIND_SET for kind in kinds):
        raise VisualPolicyError("visual_kind_unknown:" + next(kind for kind in kinds if kind not in VISUAL_KIND_SET))
    try:
        from pypdf import PdfReader, __version__ as pypdf_version
    except ModuleNotFoundError as error:
        raise VisualPolicyError("pypdf_unavailable") from error
    if str(pypdf_version) != "6.10.0":
        raise VisualPolicyError("pypdf_version_mismatch")
    source_sha = _sha256_file_streaming(source_path)
    reader = PdfReader(str(source_path))
    if reader.is_encrypted:
        raise VisualPolicyError("unsupported_encrypted_pdf")
    selected = sorted(set(int(page) for page in pages)) if pages is not None else list(range(1, len(reader.pages) + 1))
    if not selected or any(page < 1 or page > len(reader.pages) for page in selected):
        raise VisualPolicyError("physical_page_out_of_range")
    rows: list[dict[str, Any]] = []
    for page_number in selected:
        signals, cue = _signals_for_page(reader.pages[page_number - 1])
        all_candidates = _candidate_kinds(signals)
        candidates = tuple(kind for kind in all_candidates if kind in kinds)
        regions = [_region_for_page(page_number, signals, kind) for kind in candidates]
        row = {
            "schema_version": VISUAL_CENSUS_SCHEMA,
            "census_id": census_id or f"census-{sha256_json({'source_sha256': source_sha, 'page': page_number, 'policy_sha256': policy_sha256})[:20]}",
            "source_sha256": source_sha,
            "physical_page": page_number,
            "inspected": True,
            "candidate_kinds": list(candidates),
            "candidate_regions": regions,
            "signals": signals,
            "bounded_text_cue": cue,
            "fact_status": "candidate-routing-only",
            "policy_sha256": policy_sha256,
        }
        row["census_sha256"] = sha256_json({key: value for key, value in row.items() if key != "census_sha256"})
        rows.append(row)
    result = {
        "schema_version": VISUAL_CENSUS_SCHEMA,
        "protocol": SELECTIVE_VISUAL_PROTOCOL,
        "source_sha256": source_sha,
        "pypdf_version": "6.10.0",
        "pages": rows,
        "candidate_only": True,
        "model_invocations": 0,
        "render_invocations": 0,
        "network_enabled": False,
        "model_download": False,
        "census_sha256": sha256_json({"source_sha256": source_sha, "pages": rows, "candidate_only": True}),
    }
    return result


def policy_only_census(
    source_sha256: str,
    pages: Sequence[int],
    *,
    policy_sha256: str,
) -> dict[str, Any]:
    """Return the explicit off-policy receipt without inspecting pages."""

    _require_sha(source_sha256, "source")
    rows = []
    for page in sorted(set(int(value) for value in pages)):
        row = {
            "schema_version": VISUAL_CENSUS_SCHEMA,
            "source_sha256": source_sha256,
            "physical_page": page,
            "inspected": False,
            "candidate_kinds": [],
            "candidate_regions": [],
            "signals": {},
            "bounded_text_cue": None,
            "fact_status": "not-inspected-by-policy",
            "policy_sha256": policy_sha256,
        }
        row["census_sha256"] = sha256_json({key: value for key, value in row.items() if key != "census_sha256"})
        rows.append(row)
    return {
        "schema_version": VISUAL_CENSUS_SCHEMA,
        "protocol": SELECTIVE_VISUAL_PROTOCOL,
        "source_sha256": source_sha256,
        "pages": rows,
        "candidate_only": True,
        "model_invocations": 0,
        "render_invocations": 0,
        "policy_bypass": True,
        "policy_bypass_reason": "visual-mode-off",
        "census_sha256": sha256_json({"source_sha256": source_sha256, "pages": rows, "policy_bypass": True}),
    }


def validate_visual_census(census: Mapping[str, Any], *, source_sha256: str | None = None) -> list[str]:
    issues: list[str] = []
    if census.get("schema_version") != VISUAL_CENSUS_SCHEMA or census.get("protocol") != SELECTIVE_VISUAL_PROTOCOL:
        issues.append("census_schema_invalid")
    if source_sha256 is not None and census.get("source_sha256") != source_sha256:
        issues.append("census_source_mismatch")
    pages = census.get("pages") if isinstance(census.get("pages"), list) else []
    for row in pages:
        if not isinstance(row, Mapping):
            issues.append("census_page_invalid")
            continue
        expected_row = sha256_json({key: value for key, value in row.items() if key != "census_sha256"})
        if row.get("census_sha256") != expected_row:
            issues.append(f"census_page_hash_mismatch:{row.get('physical_page')}")
    if census.get("policy_bypass") is True:
        expected = sha256_json({"source_sha256": census.get("source_sha256"), "pages": pages, "policy_bypass": True})
    else:
        expected = sha256_json({"source_sha256": census.get("source_sha256"), "pages": pages, "candidate_only": True})
    if census.get("census_sha256") != expected:
        issues.append("census_hash_mismatch")
    if census.get("candidate_only") is not True or census.get("model_invocations") != 0 or census.get("render_invocations") != 0:
        issues.append("census_capability_contract_invalid")
    return sorted(set(issues))


def route_visual_census(
    census: Mapping[str, Any],
    *,
    policy: VisualPolicy,
    selected_pages: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Turn census hints into explicit heavy-route statuses and gaps."""

    source_sha = _require_sha(census.get("source_sha256"), "source")
    rows = {int(row.get("physical_page")): row for row in census.get("pages", []) if isinstance(row, Mapping) and isinstance(row.get("physical_page"), int)}
    pages = sorted(set(int(page) for page in (selected_pages if selected_pages is not None else rows)))
    routes: list[dict[str, Any]] = []
    requested_regions = 0
    requested_pages = 0
    budget = policy.budget
    for page in pages:
        row = rows.get(page)
        if policy.mode == "off":
            status = "not-inspected-by-policy"
            reason = "visual-mode-off"
            kinds: list[str] = []
            regions: list[dict[str, Any]] = []
        elif row is None or row.get("inspected") is not True:
            status = "not-inspected-by-policy"
            reason = "census-not-available"
            kinds, regions = [], []
        else:
            kinds = [str(kind) for kind in row.get("candidate_kinds", []) if str(kind) in policy.kinds]
            if policy.mode == "auto" and not kinds:
                status = "not-requested"
                reason = "census-no-candidate"
                regions = []
            else:
                if policy.mode == "full" and not kinds:
                    kinds = list(policy.kinds)
                regions = [dict(region) for region in row.get("candidate_regions", []) if isinstance(region, Mapping) and str(region.get("kind")) in kinds]
                if policy.mode == "full" and not regions:
                    regions = [_region_for_page(page, row.get("signals", {}), kind) for kind in kinds]
                proposed_regions = requested_regions + len(regions)
                over_page = budget.max_visual_pages is not None and requested_pages >= budget.max_visual_pages
                over_region = budget.max_visual_regions is not None and proposed_regions > budget.max_visual_regions
                over_seconds = budget.max_visual_seconds is not None and float(budget.max_visual_seconds) <= 0.0
                if over_page or over_region or over_seconds:
                    status = "budget-exhausted"
                    reason = "max_visual_pages" if over_page else "max_visual_regions" if over_region else "max_visual_seconds"
                    regions = []
                else:
                    requested_regions = proposed_regions
                    requested_pages += 1
                    status = "table-candidate" if "table" in kinds and len(kinds) == 1 else "chart-candidate" if "chart" in kinds and len(kinds) == 1 else "manual-review"
                    reason = "full-range-request" if policy.mode == "full" else "census-candidate"
        route = {
            "schema_version": VISUAL_ROUTING_SCHEMA,
            "source_sha256": source_sha,
            "physical_page": page,
            "mode": policy.mode,
            "requested_kinds": list(policy.kinds),
            "candidate_kinds": kinds,
            "regions": regions,
            "status": status,
            "reason": reason,
            "candidate_only": True,
            "promotion": False,
            "policy_sha256": policy.policy_sha256,
        }
        route["routing_sha256"] = sha256_json({key: value for key, value in route.items() if key != "routing_sha256"})
        routes.append(route)
    result = {
        "schema_version": VISUAL_ROUTING_SCHEMA,
        "protocol": SELECTIVE_VISUAL_PROTOCOL,
        "source_sha256": source_sha,
        "policy_sha256": policy.policy_sha256,
        "routes": routes,
        "requested_pages": requested_pages,
        "requested_regions": requested_regions,
        "candidate_only": True,
        "promotion": False,
    }
    result["routing_sha256"] = sha256_json({key: value for key, value in result.items() if key != "routing_sha256"})
    return result


def validate_visual_routing(routing: Mapping[str, Any], *, source_sha256: str | None = None, policy_sha256: str | None = None) -> list[str]:
    issues: list[str] = []
    if routing.get("schema_version") != VISUAL_ROUTING_SCHEMA or routing.get("protocol") != SELECTIVE_VISUAL_PROTOCOL:
        issues.append("routing_schema_invalid")
    if source_sha256 is not None and routing.get("source_sha256") != source_sha256:
        issues.append("routing_source_mismatch")
    if policy_sha256 is not None and routing.get("policy_sha256") != policy_sha256:
        issues.append("routing_policy_mismatch")
    routes = routing.get("routes") if isinstance(routing.get("routes"), list) else []
    for row in routes:
        if not isinstance(row, Mapping):
            issues.append("routing_row_invalid")
            continue
        expected = sha256_json({key: value for key, value in row.items() if key != "routing_sha256"})
        if row.get("routing_sha256") != expected:
            issues.append(f"routing_row_hash_mismatch:{row.get('physical_page')}")
    expected_top = sha256_json({key: value for key, value in routing.items() if key != "routing_sha256"})
    if routing.get("routing_sha256") != expected_top:
        issues.append("routing_hash_mismatch")
    if routing.get("candidate_only") is not True or routing.get("promotion") is not False:
        issues.append("routing_capability_contract_invalid")
    return sorted(set(issues))


def build_visual_budget_receipt(
    *,
    policy: VisualPolicy,
    source_sha256: str,
    routes: Sequence[Mapping[str, Any]],
    processed_pages: int = 0,
    processed_regions: int = 0,
    elapsed_seconds: float = 0.0,
    calls: int = 0,
    bytes_processed: int = 0,
    objects: int = 0,
    paused: bool = False,
    pause_reason: str | None = None,
) -> dict[str, Any]:
    """Emit an auditable budget receipt; never use elapsed in object IDs."""

    _require_sha(source_sha256, "source")
    elapsed = max(0.0, float(elapsed_seconds))
    # Reaching a declared limit after the last permitted request is a complete
    # receipt.  Only an explicit pause, an over-limit count, or an elapsed value
    # beyond the limit is exhaustion.  The routing layer marks an unprocessed
    # next route as ``budget-exhausted`` when more work remains.
    exhausted = paused or (
        policy.budget.max_visual_pages is not None and processed_pages > policy.budget.max_visual_pages
    ) or (
        policy.budget.max_visual_regions is not None and processed_regions > policy.budget.max_visual_regions
    ) or (
        policy.budget.max_visual_seconds is not None and elapsed > float(policy.budget.max_visual_seconds)
    )
    status = "paused-budget-exhausted" if exhausted else "completed"
    receipt = {
        "schema_version": VISUAL_BUDGET_RECEIPT_SCHEMA,
        "protocol": SELECTIVE_VISUAL_PROTOCOL,
        "source_sha256": source_sha256,
        "policy_sha256": policy.policy_sha256,
        "limits": policy.budget.to_dict(),
        "calls": int(calls),
        "pages": int(processed_pages),
        "regions": int(processed_regions),
        "elapsed_seconds": elapsed,
        "bytes": int(bytes_processed),
        "objects": int(objects),
        "status": status,
        "pause_reason": pause_reason if exhausted else None,
        "candidate_only": True,
        "promotion": False,
        "route_count": len(routes),
    }
    receipt["budget_receipt_sha256"] = sha256_json({key: value for key, value in receipt.items() if key != "budget_receipt_sha256"})
    return receipt


def validate_visual_budget_receipt(receipt: Mapping[str, Any], *, source_sha256: str | None = None, policy_sha256: str | None = None) -> list[str]:
    issues: list[str] = []
    if receipt.get("schema_version") != VISUAL_BUDGET_RECEIPT_SCHEMA or receipt.get("protocol") != SELECTIVE_VISUAL_PROTOCOL:
        issues.append("budget_schema_invalid")
    if source_sha256 is not None and receipt.get("source_sha256") != source_sha256:
        issues.append("budget_source_mismatch")
    if policy_sha256 is not None and receipt.get("policy_sha256") != policy_sha256:
        issues.append("budget_policy_mismatch")
    if receipt.get("status") not in {"completed", "paused-budget-exhausted"}:
        issues.append("budget_status_invalid")
    expected = sha256_json({key: value for key, value in receipt.items() if key != "budget_receipt_sha256"})
    if receipt.get("budget_receipt_sha256") != expected:
        issues.append("budget_hash_mismatch")
    if receipt.get("candidate_only") is not True or receipt.get("promotion") is not False:
        issues.append("budget_capability_contract_invalid")
    return sorted(set(issues))


def _identity_receipt(value: Any) -> Any:
    """Strip volatile/path values from a receipt before semantic hashing."""

    volatile = {"elapsed_ms", "elapsed_seconds", "wall_clock_seconds", "temporary_path", "temp_path", "workspace_path", "request_path", "response_bytes", "bytes_processed"}
    if isinstance(value, Mapping):
        return {str(key): _identity_receipt(item) for key, item in sorted(value.items()) if str(key) not in volatile and not str(key).endswith("_path")}
    if isinstance(value, (list, tuple)):
        return [_identity_receipt(item) for item in value]
    return value


def _stable_worker_receipt(value: Any) -> dict[str, Any]:
    """Keep only worker identity fields that are safe for semantic IDs."""

    if not isinstance(value, Mapping):
        return {}
    stable_keys = (
        "protocol",
        "worker_sha256",
        "request_sha256",
        "response_sha256",
        "input_crop_sha256",
        "runtime_contract_sha256",
        "network_isolation",
        "source_pdf_passed",
        "process_isolated",
        "batch",
        "candidate_only",
        "promotion",
    )
    return {
        key: _identity_receipt(value[key])
        for key in stable_keys
        if key in value
    }


def canonical_visual_object_id(
    *,
    source_sha256: str,
    physical_page: int,
    render_sha256: str,
    render_dpi: int,
    render_rotation: int,
    bbox_pdf_points: Sequence[float],
    crop_sha256: str,
    object_kind: str,
    parser_receipt: Mapping[str, Any] | None = None,
    backend_receipt: Mapping[str, Any] | None = None,
    model_receipt: Mapping[str, Any] | None = None,
    config_receipt: Mapping[str, Any] | None = None,
    request_commitment_sha256: str | None = None,
    response_content_sha256: str | None = None,
) -> str:
    """Return a mode/scheduling-independent visual object identity."""

    _require_sha(source_sha256, "source")
    _require_sha(render_sha256, "render")
    _require_sha(crop_sha256, "crop")
    if request_commitment_sha256 is not None:
        _require_sha(request_commitment_sha256, "request")
    if response_content_sha256 is not None:
        _require_sha(response_content_sha256, "response")
    commitment = {
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "render_sha256": render_sha256,
        "render_dpi": int(render_dpi),
        "render_rotation": int(render_rotation) % 360,
        "bbox_pdf_points": [float(value) for value in bbox_pdf_points],
        "crop_sha256": crop_sha256,
        "object_kind": str(object_kind),
        "parser_receipt": _identity_receipt(parser_receipt or {}),
        "backend_receipt": _identity_receipt(backend_receipt or {}),
        "model_receipt": _identity_receipt(model_receipt or {}),
        "config_receipt": _identity_receipt(config_receipt or {}),
        "request_commitment_sha256": request_commitment_sha256,
        "response_content_sha256": response_content_sha256,
    }
    return "vo-" + sha256_json(commitment)[:20]


@dataclass
class BatchCounters:
    calls: int = 0
    pages: int = 0
    regions: int = 0
    elapsed_seconds: float = 0.0
    bytes_processed: int = 0
    objects: int = 0


class _BatchHardTimeout(TimeoutError):
    """Internal signal used to distinguish a real bounded timeout."""


class VisualBatchSession:
    """Bounded sequential crop session with one model-load callback.

    The session accepts only crop descriptors/bytes; a caller cannot pass a
    source PDF through this interface.  A loader is called once and the
    returned opaque model is reused for each request.  This is a protocol-level
    optimization, not a claim about tokens or hardware performance.
    """

    def __init__(
        self,
        *,
        model_id: str,
        load_model: Callable[[], Any],
        predict: Callable[[Any, Mapping[str, Any]], Mapping[str, Any]],
        max_requests: int | None = None,
        total_timeout_seconds: float | None = None,
        per_request_timeout_seconds: float | None = None,
        batch_runner: Callable[..., Mapping[str, Any]] | None = None,
    ) -> None:
        if not model_id or not isinstance(model_id, str):
            raise VisualPolicyError("batch_model_id_invalid")
        if max_requests is not None and (isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 0):
            raise VisualPolicyError("batch_max_requests_invalid")
        for name, value in (("total_timeout_seconds", total_timeout_seconds), ("per_request_timeout_seconds", per_request_timeout_seconds)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0):
                raise VisualPolicyError(f"batch_{name}_invalid")
        self.model_id = model_id
        self.load_model = load_model
        self.predict = predict
        self.max_requests = max_requests
        self.total_timeout_seconds = None if total_timeout_seconds is None else float(total_timeout_seconds)
        self.per_request_timeout_seconds = None if per_request_timeout_seconds is None else float(per_request_timeout_seconds)
        self.batch_runner = batch_runner
        self._model: Any = None
        self._loaded = False

    def _ensure_model(self) -> Any:
        if not self._loaded:
            self._model = self.load_model()
            self._loaded = True
        return self._model

    @staticmethod
    def _invoke_hard(
        callback: Callable[[], Any],
        timeout_seconds: float | None,
        error_code: str,
    ) -> Any:
        """Run a callback with a real POSIX interrupt, failing closed if unavailable."""

        if timeout_seconds is None:
            return callback()
        timeout = float(timeout_seconds)
        if timeout <= 0.0:
            raise VisualPolicyError(error_code)
        try:
            previous_handler = signal.getsignal(signal.SIGALRM)
            previous_timer = signal.setitimer(signal.ITIMER_REAL, 0.0)
            def _alarm(_signum: int, _frame: Any) -> None:
                raise _BatchHardTimeout()
            signal.signal(signal.SIGALRM, _alarm)
            signal.setitimer(signal.ITIMER_REAL, timeout)
        except (AttributeError, OSError, ValueError):
            raise VisualPolicyError("hard_timeout_unavailable")
        try:
            return callback()
        except _BatchHardTimeout:
            raise VisualPolicyError(error_code)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0] > 0.0:
                signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])

    @staticmethod
    def _receipt(
        *,
        model_id: str,
        model_load_calls: int,
        counters: BatchCounters,
        status: str,
        errors: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        receipt = {
            "schema_version": VISUAL_BATCH_RECEIPT_SCHEMA,
            "protocol": SELECTIVE_VISUAL_PROTOCOL,
            "model_id": model_id,
            "model_load_calls": int(model_load_calls),
            "calls": int(counters.calls),
            "pages": int(counters.pages),
            "regions": int(counters.regions),
            "elapsed_seconds": max(0.0, float(counters.elapsed_seconds)),
            "bytes": int(counters.bytes_processed),
            "objects": int(counters.objects),
            "status": status,
            "errors": [dict(row) for row in errors],
            "source_pdf_passed": False,
            "items": [],
            "candidate_only": True,
            "promotion": False,
        }
        receipt["batch_receipt_sha256"] = sha256_json({key: value for key, value in receipt.items() if key != "batch_receipt_sha256"})
        return receipt

    def run(self, requests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        request_list = list(requests)
        start = time.monotonic()
        counters = BatchCounters()
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        if not request_list:
            counters.elapsed_seconds = max(0.0, time.monotonic() - start)
            receipt = self._receipt(
                model_id=self.model_id,
                model_load_calls=0,
                counters=counters,
                status="completed",
                errors=errors,
            )
            return {"results": results, "errors": errors, "receipt": receipt}
        if self.max_requests == 0:
            errors = [
                {"index": index, "request_id": request.get("request_id") if isinstance(request, Mapping) else None, "error_code": "max_requests_exceeded"}
                for index, request in enumerate(request_list)
            ]
            counters.elapsed_seconds = max(0.0, time.monotonic() - start)
            receipt = self._receipt(
                model_id=self.model_id,
                model_load_calls=0,
                counters=counters,
                status="paused-budget-exhausted",
                errors=errors,
            )
            return {"results": results, "errors": errors, "receipt": receipt}
        bounded = request_list if self.max_requests is None else request_list[: self.max_requests]
        if self.batch_runner is not None:
            try:
                raw = self._invoke_hard(
                    lambda: self.batch_runner(
                        bounded,
                        total_timeout_seconds=self.total_timeout_seconds,
                        per_request_timeout_seconds=self.per_request_timeout_seconds,
                        max_requests=self.max_requests,
                    ),
                    self.total_timeout_seconds,
                    "batch_timeout",
                )
                if not isinstance(raw, Mapping):
                    raise VisualPolicyError("batch_result_invalid")
                results = [dict(row) for row in raw.get("results", []) if isinstance(row, Mapping)]
                errors = [dict(row) for row in raw.get("errors", []) if isinstance(row, Mapping)]
                if self.max_requests is not None and len(request_list) > self.max_requests:
                    request = request_list[self.max_requests]
                    errors.append({"index": self.max_requests, "request_id": request.get("request_id") if isinstance(request, Mapping) else None, "error_code": "max_requests_exceeded"})
                raw_receipt = raw.get("receipt") if isinstance(raw.get("receipt"), Mapping) else {}
                counters.calls = int(raw_receipt.get("calls", len(bounded)) or 0)
                counters.pages = int(raw_receipt.get("pages", sum(1 for row in bounded if isinstance(row, Mapping) and row.get("physical_page") is not None)) or 0)
                counters.regions = int(raw_receipt.get("regions", len(results)) or 0)
                counters.bytes_processed = int(raw_receipt.get("bytes", 0) or 0)
                counters.objects = int(raw_receipt.get("objects", 0) or 0)
                counters.elapsed_seconds = max(0.0, time.monotonic() - start)
                error_codes = {str(row.get("error_code")) for row in errors}
                status = "paused-budget-exhausted" if raw_receipt.get("status") == "paused-budget-exhausted" or error_codes & {"batch_timeout", "request_timeout", "max_requests_exceeded", "hard_timeout"} else "paused-batch-failed" if raw_receipt.get("status") in {"paused-batch-failed", "paused-model-load-failed"} or errors else "completed"
                receipt = self._receipt(
                    model_id=self.model_id,
                    model_load_calls=int(raw_receipt.get("model_load_calls", 0) or 0),
                    counters=counters,
                    status=status,
                    errors=errors,
                )
                return {"results": results, "errors": errors, "receipt": receipt}
            except VisualPolicyError as error:
                errors.append({"index": None, "error_code": str(error)})
                counters.elapsed_seconds = max(0.0, time.monotonic() - start)
                receipt = self._receipt(
                    model_id=self.model_id,
                    model_load_calls=0,
                    counters=counters,
                    status="paused-budget-exhausted" if str(error).endswith("timeout") else "paused-batch-failed",
                    errors=errors,
                )
                return {"results": results, "errors": errors, "receipt": receipt}
            except Exception:
                errors.append({"index": None, "error_code": "batch_failed"})
                counters.elapsed_seconds = max(0.0, time.monotonic() - start)
                receipt = self._receipt(
                    model_id=self.model_id,
                    model_load_calls=0,
                    counters=counters,
                    status="paused-batch-failed",
                    errors=errors,
                )
                return {"results": results, "errors": errors, "receipt": receipt}
        try:
            model = self._ensure_model()
        except Exception:
            receipt = {
                "schema_version": VISUAL_BATCH_RECEIPT_SCHEMA,
                "protocol": SELECTIVE_VISUAL_PROTOCOL,
                "model_id": self.model_id,
                "model_load_calls": 1,
                "calls": 0,
                "pages": 0,
                "regions": 0,
                "elapsed_seconds": max(0.0, time.monotonic() - start),
                "bytes": 0,
                "objects": 0,
                "status": "paused-model-load-failed",
                "errors": [{"index": None, "error_code": "model_load_failed"}],
                "source_pdf_passed": False,
                "items": [],
                "candidate_only": True,
                "promotion": False,
            }
            receipt["batch_receipt_sha256"] = sha256_json({key: value for key, value in receipt.items() if key != "batch_receipt_sha256"})
            return {"results": [], "errors": receipt["errors"], "receipt": receipt}
        for index, request in enumerate(bounded):
            if not isinstance(request, Mapping):
                errors.append({"index": index, "error_code": "request_invalid"})
                continue
            if "source_pdf" in request or "pdf_path" in request or "source_path" in request:
                errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "source_pdf_input_forbidden"})
                continue
            if self.max_requests is not None and counters.calls >= self.max_requests:
                errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "max_requests_exceeded"})
                break
            elapsed = time.monotonic() - start
            if self.total_timeout_seconds is not None and elapsed >= self.total_timeout_seconds:
                errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "batch_timeout"})
                break
            request_start = time.monotonic()
            counters.calls += 1
            try:
                remaining = None
                if self.total_timeout_seconds is not None:
                    remaining = max(0.0, self.total_timeout_seconds - elapsed)
                timeout = self.per_request_timeout_seconds
                if remaining is not None:
                    timeout = remaining if timeout is None else min(timeout, remaining)
                timeout_code = "request_timeout" if self.per_request_timeout_seconds is not None and (remaining is None or self.per_request_timeout_seconds <= remaining) else "batch_timeout"
                output = self._invoke_hard(
                    lambda: self.predict(model, request),
                    timeout,
                    timeout_code,
                )
                if not isinstance(output, Mapping):
                    raise VisualPolicyError("predict_result_invalid")
                row = dict(output)
                row.setdefault("request_id", request.get("request_id"))
                results.append(row)
                counters.pages += 1 if request.get("physical_page") is not None else 0
                counters.regions += 1
                payload_bytes = request.get("input_bytes", request.get("bytes", 0))
                counters.bytes_processed += int(payload_bytes) if isinstance(payload_bytes, (int, float)) else 0
                objects = output.get("objects")
                counters.objects += len(objects) if isinstance(objects, list) else int(output.get("object_count", 0) or 0)
            except VisualPolicyError as error:
                errors.append({"index": index, "request_id": request.get("request_id"), "error_code": str(error)})
            except Exception:
                # Never expose arbitrary exception text or worker stderr in the
                # public batch receipt.
                errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "request_failed"})
        if self.max_requests is not None and len(request_list) > self.max_requests:
            request = request_list[self.max_requests]
            errors.append({"index": self.max_requests, "request_id": request.get("request_id") if isinstance(request, Mapping) else None, "error_code": "max_requests_exceeded"})
        counters.elapsed_seconds = max(0.0, time.monotonic() - start)
        status = "paused-budget-exhausted" if any(str(row.get("error_code", "")).endswith(("timeout", "exceeded")) for row in errors) else "completed"
        receipt = {
            "schema_version": VISUAL_BATCH_RECEIPT_SCHEMA,
            "protocol": SELECTIVE_VISUAL_PROTOCOL,
            "model_id": self.model_id,
            "model_load_calls": 1 if self._loaded else 0,
            "calls": counters.calls,
            "pages": counters.pages,
            "regions": counters.regions,
            "elapsed_seconds": counters.elapsed_seconds,
            "bytes": counters.bytes_processed,
            "objects": counters.objects,
            "status": status,
            "errors": errors,
            "source_pdf_passed": False,
            "items": [],
            "candidate_only": True,
            "promotion": False,
        }
        receipt["batch_receipt_sha256"] = sha256_json({key: value for key, value in receipt.items() if key != "batch_receipt_sha256"})
        return {"results": results, "errors": errors, "receipt": receipt}


def build_chart_candidate(
    *,
    source_sha256: str,
    physical_page: int,
    bbox_pdf_points: Sequence[float],
    render_sha256: str | None,
    crop_sha256: str | None,
    model_root: Path | str | None,
    model_dirs: Mapping[str, str] | None = None,
    constructor_options: Mapping[str, Any] | None = None,
    predict_options: Mapping[str, Any] | None = None,
    chart_result: Mapping[str, Any] | None = None,
    runtime_contract: Mapping[str, Any] | None = None,
    runtime_qualification: Mapping[str, Any] | None = None,
    worker_receipt: Mapping[str, Any] | None = None,
    runtime_status: str | None = None,
    source_anchor: Mapping[str, Any] | None = None,
    evidence_stage: str | None = None,
    error_code: str | None = None,
    profile_id: str = TECHNICAL_CHART_PROFILE_ID,
    profile_protocol: str = TECHNICAL_CHART_PROFILE_PROTOCOL,
) -> dict[str, Any]:
    """Create a chart candidate with conditional render/crop evidence.

    A missing-model/runtime decision happens before a crop is rendered.  Such
    a record keeps the planned source anchor and bbox, but must not invent
    render or crop hashes.  Any actual runtime result (including an empty or
    malformed result) requires both hashes from the real crop binding.
    """

    _require_sha(source_sha256, "source")
    if isinstance(physical_page, bool) or int(physical_page) < 1:
        raise VisualPolicyError("chart_physical_page_invalid")
    page = int(physical_page)
    try:
        bbox = [float(value) for value in bbox_pdf_points]
    except (TypeError, ValueError):
        raise VisualPolicyError("chart_bbox_invalid") from None
    if len(bbox) != 4 or any(not math.isfinite(value) for value in bbox):
        raise VisualPolicyError("chart_bbox_invalid")
    if any(bbox[index] >= bbox[index + 2] for index in (0, 1)):
        raise VisualPolicyError("chart_bbox_invalid")
    if profile_id not in {TECHNICAL_CHART_PROFILE_ID, LEGACY_TECHNICAL_CHART_PROFILE_ID}:
        raise VisualPolicyError("chart_profile_unknown")
    expected_protocol = TECHNICAL_CHART_PROFILE_PROTOCOL if profile_id == TECHNICAL_CHART_PROFILE_ID else LEGACY_TECHNICAL_CHART_PROFILE_PROTOCOL
    if profile_protocol != expected_protocol:
        raise VisualPolicyError("chart_profile_protocol_mismatch")
    candidate_schema = TECHNICAL_CHART_CANDIDATE_SCHEMA if profile_id == TECHNICAL_CHART_PROFILE_ID else "tkc.technical-chart-candidate/v0.1"
    qualification_sha256 = runtime_qualification.get("receipt_sha256") if isinstance(runtime_qualification, Mapping) else None
    planned_statuses = {"not-run-missing-models", "not-run-runtime-unavailable", "paused-runtime-unqualified"}
    effective_status = runtime_status or ("not-run-runtime-unavailable" if not isinstance(chart_result, Mapping) else "candidate")
    planned_only = effective_status in planned_statuses
    if profile_id == TECHNICAL_CHART_PROFILE_ID and not planned_only:
        _require_sha(qualification_sha256, "runtime_qualification")
    if planned_only:
        # A pre-execution decision cannot inherit caller-provided/fabricated
        # hashes from a page render or an unrelated visual object.
        render_sha256 = None
        crop_sha256 = None
    else:
        _require_sha(render_sha256, "render")
        _require_sha(crop_sha256, "crop")
    anchor = dict(source_anchor or {})
    if "source_sha256" in anchor and anchor["source_sha256"] != source_sha256:
        raise VisualPolicyError("chart_source_anchor_mismatch")
    if "physical_page" in anchor and anchor["physical_page"] != page:
        raise VisualPolicyError("chart_source_anchor_mismatch")
    if "bbox_pdf_points" in anchor:
        try:
            anchor_bbox = [float(value) for value in anchor["bbox_pdf_points"]]
        except (TypeError, ValueError):
            raise VisualPolicyError("chart_source_anchor_bbox_mismatch") from None
        if anchor_bbox != bbox:
            raise VisualPolicyError("chart_source_anchor_bbox_mismatch")
    anchor["source_sha256"] = source_sha256
    anchor["physical_page"] = page
    anchor["bbox_pdf_points"] = list(bbox)
    region_id = anchor.get("region_id")
    if region_id is not None and (not isinstance(region_id, str) or not region_id):
        raise VisualPolicyError("chart_source_anchor_region_invalid")
    anchor["region_id"] = region_id or f"chart-{sha256_json({'physical_page': page, 'bbox_pdf_points': bbox})[:20]}"
    evidence_stage_value = evidence_stage or ("planned-route-no-render" if planned_only else "rendered-crop")
    dirs = dict(model_dirs or {"chart_model_dir": "PP-Chart2Table"})
    root = Path(model_root).expanduser().resolve() if model_root is not None else None
    missing: list[str] = []
    files: list[dict[str, Any]] = []
    if root is None:
        missing = sorted(dirs)
    else:
        for key, relative in sorted(dirs.items()):
            directory = root / str(relative)
            if not directory.is_dir() or not any(path.is_file() for path in directory.rglob("*")):
                missing.append(key)
            else:
                for path in sorted(directory.rglob("*")):
                    if path.is_file():
                        digest = _sha256_file_streaming(path)
                        files.append({"relative_path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": digest})
    model_manifest_sha = sha256_json(files)
    config = {
        "profile_id": profile_id,
        "protocol": profile_protocol,
        "model_dirs": dirs,
        "constructor_options": dict(constructor_options or ({"device": "cpu", "enable_hpi": False, "engine": "paddle_dynamic"} if profile_id == TECHNICAL_CHART_PROFILE_ID else {"device": "cpu", "enable_hpi": False})),
        "predict_options": dict(predict_options or ({"batch_size": 1} if profile_id == TECHNICAL_CHART_PROFILE_ID else {})),
        "network_enabled": False,
        "model_download": False,
    }
    if missing:
        result = {
            "schema_version": candidate_schema,
            "status": "not-run-missing-models",
            "paused": True,
            "reason": "missing-local-chart-models",
            "missing_model_dirs": missing,
            "profile_id": profile_id,
            "source_sha256": source_sha256,
            "physical_page": page,
            "bbox_pdf_points": bbox,
            "source_anchor": anchor,
            "evidence_stage": evidence_stage_value,
            "render_sha256": render_sha256,
            "crop_sha256": crop_sha256,
            "candidate_only": True,
            "promotion": False,
            "executable": False,
            "network_enabled": False,
            "model_download": False,
            "config_sha256": sha256_json(config),
            "runtime_qualification_sha256": qualification_sha256,
        }
        result["candidate_sha256"] = sha256_json({key: value for key, value in result.items() if key != "candidate_sha256"})
        return result
    if effective_status in planned_statuses or not isinstance(chart_result, Mapping):
        result = {
            "schema_version": candidate_schema,
            "status": runtime_status or "not-run-runtime-unavailable",
            "paused": True,
            "reason": "chart-runtime-result-required",
            "error_code": error_code,
            "profile_id": profile_id,
            "source_sha256": source_sha256,
            "physical_page": page,
            "bbox_pdf_points": bbox,
            "source_anchor": anchor,
            "evidence_stage": evidence_stage_value,
            "render_sha256": render_sha256,
            "crop_sha256": crop_sha256,
            "model_identity_sha256": sha256_json({"profile_id": profile_id, "files": files}),
            "model_manifest_sha256": model_manifest_sha,
            "config_sha256": sha256_json(config),
            "runtime_contract_sha256": runtime_contract.get("contract_sha256") if isinstance(runtime_contract, Mapping) else None,
            "runtime_qualification_sha256": qualification_sha256,
            "worker_receipt_sha256": sha256_json(_stable_worker_receipt(worker_receipt)),
            "result_adapter_sha256": chart_result.get("normalized_result_sha256") if isinstance(chart_result, Mapping) else None,
            "chart": {"axis": [], "ticks": [], "legend": [], "series": [], "unit": [], "annotations": [], "value_candidates": []},
            "gaps": [{"gap_type": "chart-runtime", "reason": "runtime-response-not-available", "review_required": True}],
            "conflicts": [],
            "review_route": "two-independent-reviewers-for-values",
            "candidate_only": True,
            "promotion": False,
            "executable": False,
            "network_enabled": False,
            "model_download": False,
            "config_sha256": sha256_json(config),
        }
        result["candidate_sha256"] = sha256_json({key: value for key, value in result.items() if key != "candidate_sha256"})
        return result
    identity = sha256_json({"profile_id": profile_id, "files": files})
    chart_payload = chart_result.get("chart") if isinstance(chart_result.get("chart"), Mapping) else {}
    rows = [dict(row) for row in chart_result.get("chart_rows", chart_payload.get("value_candidates", [])) if isinstance(row, Mapping)]
    runtime_pause_statuses = {"paused-chart-runtime-error", "paused-chart-result-empty", "paused-chart-result-shape"}
    if not rows:
        result = {
            "schema_version": candidate_schema,
            "status": effective_status if effective_status in runtime_pause_statuses else "paused-chart-result-empty",
            "paused": True,
            "reason": "chart-runtime-produced-no-pipe-delimited-rows",
            "error_code": error_code,
            "profile_id": profile_id,
            "source_sha256": source_sha256,
            "physical_page": page,
            "bbox_pdf_points": bbox,
            "source_anchor": anchor,
            "evidence_stage": evidence_stage_value,
            "render_sha256": render_sha256,
            "crop_sha256": crop_sha256,
            "model_identity_sha256": identity,
            "model_manifest_sha256": model_manifest_sha,
            "runtime_contract_sha256": runtime_contract.get("contract_sha256") if isinstance(runtime_contract, Mapping) else None,
            "runtime_qualification_sha256": qualification_sha256,
            "worker_receipt_sha256": sha256_json(_stable_worker_receipt(worker_receipt)),
            "result_adapter_sha256": chart_result.get("normalized_result_sha256"),
            "chart": dict(chart_payload),
            "gaps": [dict(row) for row in chart_result.get("gaps", []) if isinstance(row, Mapping)] or [{"gap_type": "chart-table", "reason": "runtime-produced-no-rows", "review_required": True}],
            "conflicts": [dict(row) for row in chart_result.get("conflicts", []) if isinstance(row, Mapping)],
            "review_route": "two-independent-reviewers-for-values",
            "candidate_only": True,
            "promotion": False,
            "executable": False,
            "network_enabled": False,
            "model_download": False,
            "config_sha256": sha256_json(config),
        }
        result["candidate_sha256"] = sha256_json({key: value for key, value in result.items() if key != "candidate_sha256"})
        return result
    candidate = {
        "schema_version": candidate_schema,
        "status": effective_status if effective_status in runtime_pause_statuses else "candidate",
        "paused": effective_status in runtime_pause_statuses,
        "profile_id": profile_id,
        "source_sha256": source_sha256,
        "physical_page": page,
        "bbox_pdf_points": bbox,
        "source_anchor": anchor,
        "evidence_stage": evidence_stage_value,
        "render_sha256": render_sha256,
        "crop_sha256": crop_sha256,
        "model_identity_sha256": identity,
        "model_manifest_sha256": model_manifest_sha,
        "constructor_options": config["constructor_options"],
        "predict_options": config["predict_options"],
        "runtime_contract_sha256": runtime_contract.get("contract_sha256") if isinstance(runtime_contract, Mapping) else None,
        "runtime_qualification_sha256": qualification_sha256,
        "worker_receipt_sha256": sha256_json(_stable_worker_receipt(worker_receipt)),
        "result_adapter_sha256": chart_result.get("normalized_result_sha256"),
        "error_code": error_code,
        "chart": {**dict(chart_payload), "value_candidates": rows},
        "gaps": [dict(row) for row in chart_result.get("gaps", []) if isinstance(row, Mapping)],
        "conflicts": [dict(row) for row in chart_result.get("conflicts", []) if isinstance(row, Mapping)],
        "review_route": "two-independent-reviewers-for-values",
        "candidate_only": True,
        "promotion": False,
        "executable": False,
        "network_enabled": False,
        "model_download": False,
        "config_sha256": sha256_json(config),
    }
    candidate["candidate_sha256"] = sha256_json({key: value for key, value in candidate.items() if key != "candidate_sha256"})
    return candidate


def build_visual_index(
    *,
    source_sha256: str,
    policy: VisualPolicy,
    census: Mapping[str, Any],
    routing: Mapping[str, Any],
    objects: Sequence[Mapping[str, Any]] = (),
    chart_candidates: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a source-free index containing navigation/provenance only."""

    _require_sha(source_sha256, "source")
    rows: list[dict[str, Any]] = []
    for route in routing.get("routes", []) if isinstance(routing.get("routes"), list) else []:
        if not isinstance(route, Mapping):
            continue
        rows.append({
            "physical_page": route.get("physical_page"),
            "route_status": route.get("status"),
            "candidate_kinds": list(route.get("candidate_kinds", [])),
            "region_ids": [str(region.get("region_id")) for region in route.get("regions", []) if isinstance(region, Mapping) and region.get("region_id")],
            "coverage": "budget-exhausted" if route.get("status") == "budget-exhausted" else "not-inspected-by-policy" if route.get("status") == "not-inspected-by-policy" else "not-requested" if route.get("status") == "not-requested" else "candidate" if route.get("status") in {"table-candidate", "chart-candidate", "manual-review"} else "processed",
        })
        rows.append({
            "physical_page": int(route.get("physical_page")),
            "candidate_kinds": list(route.get("candidate_kinds", [])),
            "status": route.get("status"),
            "reason": route.get("reason"),
            "routing_sha256": route.get("routing_sha256"),
        })
    for obj in objects:
        if not isinstance(obj, Mapping):
            continue
        rows.append({
            "physical_page": obj.get("physical_page"),
            "visual_object_id": obj.get("visual_object_id"),
            "object_kind": obj.get("type", obj.get("object_kind")),
            "status": obj.get("status", "candidate"),
            "bbox": obj.get("bbox"),
            "render_sha256": obj.get("canonical_render_sha256", obj.get("render_sha256")),
            "crop_sha256": obj.get("crop_sha256"),
        })
    for chart in chart_candidates:
        if not isinstance(chart, Mapping):
            continue
        rows.append({
            "physical_page": chart.get("physical_page"),
            "chart_candidate_sha256": chart.get("candidate_sha256"),
            "object_kind": "chart",
            "status": chart.get("status"),
            "render_sha256": chart.get("render_sha256"),
            "crop_sha256": chart.get("crop_sha256"),
        })
    for obj in objects:
        if not isinstance(obj, Mapping):
            continue
        rows.append({
            "physical_page": obj.get("physical_page"),
            "visual_object_id": obj.get("visual_object_id"),
            "object_kind": obj.get("type") or obj.get("object_kind"),
            "status": obj.get("status", "candidate"),
            "render_sha256": obj.get("canonical_render_sha256") or obj.get("render_sha256"),
            "crop_sha256": obj.get("crop_sha256"),
        })
    rows.sort(key=lambda row: (int(row.get("physical_page") or 0), str(row.get("visual_object_id") or row.get("chart_candidate_sha256") or row.get("status") or "")))
    index = {
        "schema_version": VISUAL_INDEX_SCHEMA,
        "protocol": SELECTIVE_VISUAL_PROTOCOL,
        "source_sha256": source_sha256,
        "policy_sha256": policy.policy_sha256,
        "visual_context": policy.context,
        "rows": rows,
        "candidate_only": True,
        "promotion": False,
        "contains_raw_images": False,
        "contains_crop_bytes": False,
        "contains_ocr_fulltext": False,
        "contains_absolute_source_path": False,
        "contains_credentials": False,
    }
    index["index_sha256"] = sha256_json({key: value for key, value in index.items() if key != "index_sha256"})
    return index


def build_visual_coverage(
    *,
    source_sha256: str,
    policy: VisualPolicy,
    routing: Mapping[str, Any],
    budget_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    for row in routing.get("routes", []) if isinstance(routing.get("routes"), list) else []:
        if not isinstance(row, Mapping):
            continue
        status = str(row.get("status"))
        if status == "not-requested":
            coverage = "not-requested"
        elif status == "not-inspected-by-policy":
            coverage = "not-inspected-by-policy"
        elif status == "budget-exhausted":
            coverage = "budget-exhausted"
        elif status == "conflict":
            coverage = "conflict"
        elif status == "manual-review":
            coverage = "review-required"
        elif status in {"table-candidate", "chart-candidate"}:
            coverage = "candidate"
        else:
            coverage = "processed"
        pages.append({
            "physical_page": row.get("physical_page"),
            "coverage": coverage,
            "route_status": status,
            "candidate_kinds": list(row.get("candidate_kinds", [])),
            "gap": coverage in {"budget-exhausted", "not-inspected-by-policy"},
            "review_required": coverage in {"candidate", "review-required", "conflict"},
        })
    result = {
        "schema_version": VISUAL_COVERAGE_SCHEMA,
        "protocol": SELECTIVE_VISUAL_PROTOCOL,
        "source_sha256": source_sha256,
        "policy_sha256": policy.policy_sha256,
        "pages": pages,
        "status_counts": {status: sum(1 for row in pages if row.get("coverage") == status) for status in sorted({str(row.get("coverage")) for row in pages})},
        "budget_status": budget_receipt.get("status"),
        "candidate_only": True,
        "promotion": False,
        "whole_book_completeness_claimed": False,
    }
    result["coverage_sha256"] = sha256_json({key: value for key, value in result.items() if key != "coverage_sha256"})
    return result


def validate_visual_coverage(coverage: Mapping[str, Any], *, source_sha256: str | None = None, policy_sha256: str | None = None) -> list[str]:
    issues: list[str] = []
    if coverage.get("schema_version") != VISUAL_COVERAGE_SCHEMA or coverage.get("protocol") != SELECTIVE_VISUAL_PROTOCOL:
        issues.append("coverage_schema_invalid")
    if source_sha256 is not None and coverage.get("source_sha256") != source_sha256:
        issues.append("coverage_source_mismatch")
    if policy_sha256 is not None and coverage.get("policy_sha256") != policy_sha256:
        issues.append("coverage_policy_mismatch")
    expected = sha256_json({key: value for key, value in coverage.items() if key != "coverage_sha256"})
    if coverage.get("coverage_sha256") != expected:
        issues.append("coverage_hash_mismatch")
    if coverage.get("candidate_only") is not True or coverage.get("promotion") is not False or coverage.get("whole_book_completeness_claimed") is not False:
        issues.append("coverage_capability_contract_invalid")
    return sorted(set(issues))


def validate_visual_index(index: Mapping[str, Any], *, source_sha256: str | None = None) -> list[str]:
    issues: list[str] = []
    if index.get("schema_version") != VISUAL_INDEX_SCHEMA or index.get("protocol") != SELECTIVE_VISUAL_PROTOCOL:
        issues.append("index_schema_invalid")
    if source_sha256 is not None and index.get("source_sha256") != source_sha256:
        issues.append("index_source_mismatch")
    if index.get("candidate_only") is not True or index.get("promotion") is not False:
        issues.append("index_capability_escalation")
    forbidden = ("contains_raw_images", "contains_crop_bytes", "contains_ocr_fulltext", "contains_absolute_source_path", "contains_credentials")
    if any(index.get(key) is not False for key in forbidden):
        issues.append("index_privacy_contract_invalid")
    expected = sha256_json({key: value for key, value in index.items() if key != "index_sha256"})
    if index.get("index_sha256") != expected:
        issues.append("index_hash_mismatch")
    return sorted(set(issues))


def resume_visual_policy_job(
    *,
    policy: VisualPolicy,
    routing: Mapping[str, Any],
    budget_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a pause-only intent; never evaluate, promote, or seal."""

    paused = budget_receipt.get("status") == "paused-budget-exhausted" or any(row.get("status") == "budget-exhausted" for row in routing.get("routes", []) if isinstance(row, Mapping))
    return {
        "status": "paused" if paused else "candidate-only",
        "resume_intent_only": True,
        "policy_sha256": policy.policy_sha256,
        "next_action": "continue-budgeted-visual-routing" if paused else "await-independent-review",
        "review_execution": "forbidden",
        "promotion": "forbidden",
        "seal": "forbidden",
        "executable": False,
    }


__all__ = [
    "SELECTIVE_VISUAL_COMPILER_VERSION",
    "SELECTIVE_VISUAL_PROTOCOL",
    "VISUAL_POLICY_SCHEMA",
    "VISUAL_CENSUS_SCHEMA",
    "VISUAL_ROUTING_SCHEMA",
    "VISUAL_BUDGET_RECEIPT_SCHEMA",
    "VISUAL_INDEX_SCHEMA",
    "VISUAL_COVERAGE_SCHEMA",
    "VISUAL_BATCH_RECEIPT_SCHEMA",
    "TECHNICAL_CHART_PROFILE_ID",
    "TECHNICAL_CHART_PROFILE_PROTOCOL",
    "VISUAL_MODES",
    "VISUAL_CONTEXTS",
    "VISUAL_KINDS",
    "ROUTE_STATUSES",
    "COVERAGE_STATUSES",
    "VisualPolicyError",
    "VisualBudgetExhausted",
    "VisualBudget",
    "VisualPolicy",
    "normalize_visual_policy",
    "visual_policy_from_mapping",
    "build_visual_policy",
    "run_visual_census",
    "policy_only_census",
    "validate_visual_census",
    "route_visual_census",
    "validate_visual_routing",
    "build_visual_budget_receipt",
    "validate_visual_budget_receipt",
    "canonical_visual_object_id",
    "VisualBatchSession",
    "build_chart_candidate",
    "build_visual_index",
    "build_visual_coverage",
    "validate_visual_coverage",
    "validate_visual_index",
    "resume_visual_policy_job",
    "sha256_json",
]
