#!/usr/bin/env python3
"""Deterministic composition contract for immutable Expert Skill modules."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from compiler_version import COMPOSITE_COMPILER_VERSION
from expert_skill_contract import (
    Issue,
    SLUG_RE,
    load_json,
    load_jsonl,
    sha256_file,
    sha256_json,
    validate_expert_skill,
)
from portable_reference_runtime import (
    conflict_weighted_terms,
    gap_weighted_terms,
    object_weighted_terms,
)


COMPOSITE_SCHEMA = "tkc.composite-skill/v0.1"
LOCK_SCHEMA = "tkc.composition-lock/v0.1"
CATALOG_SCHEMA = "tkc.composite-runtime-catalog/v0.1"
RUNTIME_VERSION = "0.1.1-composite"
REFERENCE_CAPABILITIES = {
    "reference": True,
    "decision_support": False,
    "executable": False,
}
DECISION_GUARD_TERMS = [
    "approve",
    "certification",
    "certify",
    "choose",
    "decision",
    "ready",
    "recommend",
    "safe",
    "safety",
    "决策",
    "安全",
    "批准",
    "推荐",
    "认证",
    "选择",
]
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CompositionError(RuntimeError):
    """Stable composition failure."""


def _issue(
    issues: list[Issue], severity: str, code: str, path: str, message: str
) -> None:
    issues.append(Issue(severity=severity, code=code, path=path, message=message))


def _load_json(path: Path, issues: list[Issue], label: str) -> Any | None:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError) as error:
        _issue(issues, "error", "invalid_json", label, str(error))
        return None


def _safe_relative(root: Path, relative: str) -> Path | None:
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        return None
    resolved = (root / Path(*candidate.parts)).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def composite_tracked_files(root: Path) -> dict[str, str]:
    """Hash every regular shareable file except only the root manifest."""

    root = root.resolve()
    result: dict[str, str] = {}
    ignored = {"__pycache__", ".pytest_cache"}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in ignored or part.startswith(".") for part in relative.parts):
            continue
        if path.is_symlink():
            raise CompositionError(f"nonregular_file: {relative.as_posix()}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise CompositionError(f"nonregular_file: {relative.as_posix()}")
        if relative.as_posix() == "manifest.json":
            continue
        result[relative.as_posix()] = sha256_file(path)
    return result


def _entity_rows(package: Path, relative: str) -> list[dict[str, Any]]:
    path = package / relative
    if not path.is_file():
        return []
    return [row for row in load_jsonl(path) if isinstance(row, dict)]


def _knowledge_objects(package: Path) -> list[tuple[dict[str, Any], str]]:
    index = load_json(package / "references/knowledge/index.json")
    entries = index.get("objects") if isinstance(index, dict) else None
    if not isinstance(entries, list):
        raise CompositionError("knowledge_index_invalid")
    result: list[tuple[dict[str, Any], str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise CompositionError("knowledge_index_invalid")
        relative = entry["path"]
        path = _safe_relative(package, relative)
        if path is None or not path.is_file():
            raise CompositionError(f"knowledge_object_unresolved: {relative}")
        obj = load_json(path)
        if not isinstance(obj, dict) or obj.get("id") != entry.get("id"):
            raise CompositionError(f"knowledge_object_invalid: {relative}")
        result.append((obj, relative))
    return result


def _source_scope(source: dict[str, Any]) -> tuple[int, int] | None:
    scope = source.get("scope")
    pages = scope.get("physical_pages") if isinstance(scope, dict) else None
    if (
        isinstance(pages, list)
        and len(pages) == 2
        and all(isinstance(page, int) and page >= 1 for page in pages)
        and pages[0] <= pages[1]
    ):
        return pages[0], pages[1]
    return None


def source_overlap_issues(module_bindings: list[dict[str, Any]]) -> list[str]:
    """Return unresolved same-source overlaps between different packages."""

    by_hash: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for binding in module_bindings:
        package_id = binding.get("package_id")
        for source in binding.get("sources", []):
            if isinstance(package_id, str) and isinstance(source, dict):
                source_hash = source.get("sha256")
                if isinstance(source_hash, str):
                    by_hash.setdefault(source_hash, []).append((package_id, source))
    overlaps: list[str] = []
    for source_hash, records in sorted(by_hash.items()):
        for left_index, (left_package, left_source) in enumerate(records):
            for right_package, right_source in records[left_index + 1 :]:
                if left_package == right_package:
                    continue
                left_scope = _source_scope(left_source)
                right_scope = _source_scope(right_source)
                if (
                    left_scope is None
                    or right_scope is None
                    or max(left_scope[0], right_scope[0])
                    <= min(left_scope[1], right_scope[1])
                ):
                    overlaps.append(
                        f"{source_hash}:{left_package}:{left_scope}:{right_package}:{right_scope}"
                    )
    return overlaps


def _semver_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", value)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _version_satisfies(version: str, version_range: str) -> bool:
    current = _semver_tuple(version)
    if current is None:
        return False
    constraint = version_range.strip()
    if constraint == "*":
        return True
    if constraint.startswith("^"):
        base = _semver_tuple(constraint[1:])
        if base is None:
            return False
        upper = (base[0] + 1, 0, 0) if base[0] else (0, base[1] + 1, 0)
        return base <= current < upper
    if constraint.startswith("~"):
        base = _semver_tuple(constraint[1:])
        return base is not None and base <= current < (base[0], base[1] + 1, 0)
    if "," in constraint or constraint.startswith((">", "<", "=")):
        for raw_part in constraint.split(","):
            part = raw_part.strip()
            match = re.fullmatch(r"(>=|<=|>|<|==|=)(.+)", part)
            if not match:
                return False
            expected = _semver_tuple(match.group(2).strip())
            if expected is None:
                return False
            operator = match.group(1)
            if operator in {"=", "=="} and current != expected:
                return False
            if operator == ">=" and current < expected:
                return False
            if operator == "<=" and current > expected:
                return False
            if operator == ">" and current <= expected:
                return False
            if operator == "<" and current >= expected:
                return False
        return True
    exact = _semver_tuple(constraint)
    return exact is not None and current == exact


def _module_registry(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    registry = []
    for module in manifest.get("modules", []):
        if not isinstance(module, dict):
            continue
        registry.append(
            {
                "module_id": module.get("module_id"),
                "version": module.get("version"),
                "dependencies": copy.deepcopy(module.get("dependencies")),
            }
        )
    return sorted(registry, key=lambda row: str(row.get("module_id")))


def dependency_issues(module_bindings: list[dict[str, Any]]) -> list[str]:
    registry: dict[str, dict[str, Any]] = {}
    for binding in module_bindings:
        for module in binding.get("module_registry", []):
            if isinstance(module, dict) and isinstance(module.get("module_id"), str):
                registry[module["module_id"]] = module
    issues: list[str] = []
    graph: dict[str, set[str]] = {module_id: set() for module_id in registry}
    for module_id, module in sorted(registry.items()):
        version = module.get("version")
        if not isinstance(version, str) or _semver_tuple(version) is None:
            issues.append(f"module_version_invalid:{module_id}:{version}")
        dependencies = module.get("dependencies")
        if not isinstance(dependencies, list):
            issues.append(f"module_dependencies_invalid:{module_id}")
            continue
        for dependency in dependencies:
            if not isinstance(dependency, dict):
                issues.append(f"module_dependency_invalid:{module_id}")
                continue
            dependency_id = dependency.get("module_id")
            version_range = dependency.get("version_range")
            target = registry.get(dependency_id) if isinstance(dependency_id, str) else None
            if target is None:
                issues.append(f"module_dependency_unresolved:{module_id}:{dependency_id}")
                continue
            if not isinstance(version_range, str) or not _version_satisfies(
                str(target.get("version")), version_range
            ):
                issues.append(
                    f"module_dependency_version_mismatch:{module_id}:{dependency_id}:{version_range}:{target.get('version')}"
                )
            graph[module_id].add(dependency_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module_id: str) -> bool:
        if module_id in visiting:
            return True
        if module_id in visited:
            return False
        visiting.add(module_id)
        if any(visit(dependency_id) for dependency_id in graph[module_id]):
            return True
        visiting.remove(module_id)
        visited.add(module_id)
        return False

    if any(visit(module_id) for module_id in sorted(graph)):
        issues.append("module_dependency_cycle")
    return sorted(set(issues))


def module_binding(package: Path, *, require_sealed: bool = True) -> dict[str, Any]:
    package = package.expanduser().resolve()
    manifest = load_json(package / "manifest.json")
    package_record = manifest.get("package")
    if not isinstance(package_record, dict):
        raise CompositionError("package_manifest_invalid")
    package_id = package_record.get("id")
    if not isinstance(package_id, str):
        raise CompositionError("package_id_invalid")
    integrity = manifest.get("integrity")
    if require_sealed and not isinstance(integrity, dict):
        raise CompositionError(f"package_unsealed: {package_id}")
    modules = manifest.get("modules")
    sources = manifest.get("sources")
    return {
        "package_id": package_id,
        "package_version": package_record.get("version"),
        "relative_path": f"modules/{package_id}",
        "manifest_sha256": sha256_file(package / "manifest.json"),
        "integrity_sha256": sha256_json(integrity if isinstance(integrity, dict) else None),
        "capabilities": copy.deepcopy(manifest.get("capabilities")),
        "module_ids": sorted(
            module.get("module_id")
            for module in modules or []
            if isinstance(module, dict) and isinstance(module.get("module_id"), str)
        ),
        "module_registry": _module_registry(manifest),
        "source_ids": sorted(
            source.get("source_id")
            for source in sources or []
            if isinstance(source, dict) and isinstance(source.get("source_id"), str)
        ),
        "sources": copy.deepcopy(sources if isinstance(sources, list) else []),
    }


def input_fingerprint(bindings: list[dict[str, Any]]) -> str:
    stable = []
    for binding in sorted(bindings, key=lambda row: str(row.get("package_id"))):
        stable.append(
            {
                "package_id": binding.get("package_id"),
                "package_version": binding.get("package_version"),
                "manifest_sha256": binding.get("manifest_sha256"),
                "integrity_sha256": binding.get("integrity_sha256"),
                "module_ids": binding.get("module_ids"),
                "module_registry": binding.get("module_registry"),
                "source_ids": binding.get("source_ids"),
                "capabilities": binding.get("capabilities"),
            }
        )
    return sha256_json(stable)


def build_composite_catalog(
    root: Path, bindings: list[dict[str, Any]]
) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    seen: dict[str, tuple[str, str]] = {}

    def register(kind: str, identifier: Any, package_id: str) -> None:
        if not isinstance(identifier, str) or not identifier:
            raise CompositionError(f"{kind}_id_invalid: {package_id}")
        prior = seen.get(identifier)
        if prior is not None:
            raise CompositionError(
                f"global_id_collision: {identifier}:{prior[0]}:{prior[1]}:{kind}:{package_id}"
            )
        seen[identifier] = (kind, package_id)

    for binding in sorted(bindings, key=lambda row: row["package_id"]):
        package_id = binding["package_id"]
        package = root / binding["relative_path"]
        manifest = load_json(package / "manifest.json")
        exported_by: dict[str, list[str]] = {}
        for module in manifest.get("modules", []):
            if not isinstance(module, dict):
                continue
            module_id = module.get("module_id")
            if not isinstance(module_id, str):
                continue
            for object_id in module.get("exports", []):
                if isinstance(object_id, str):
                    exported_by.setdefault(object_id, []).append(module_id)
        for obj, relative in _knowledge_objects(package):
            object_id = obj.get("id")
            register("object", object_id, package_id)
            objects.append(
                {
                    "id": object_id,
                    "package_id": package_id,
                    "module_ids": sorted(exported_by.get(object_id, [])),
                    "type": obj.get("type"),
                    "title": obj.get("title"),
                    "path": f"modules/{package_id}/{relative}",
                    "evidence_ids": sorted(
                        value for value in obj.get("evidence_ids", []) if isinstance(value, str)
                    ),
                    "source_ids": sorted(
                        value for value in obj.get("source_ids", []) if isinstance(value, str)
                    ),
                    "weighted_terms": object_weighted_terms(obj),
                }
            )
        for anchor in _entity_rows(package, "references/evidence/anchors.jsonl"):
            register("evidence", anchor.get("id"), package_id)
        for relation in _entity_rows(package, "references/knowledge/relations.jsonl"):
            if relation.get("id") is not None:
                register("relation", relation.get("id"), package_id)
        for conflict in _entity_rows(package, "references/conflicts.jsonl"):
            conflict_id = conflict.get("id")
            register("conflict", conflict_id, package_id)
            claim_ids = sorted(
                claim.get("claim_id")
                for claim in conflict.get("claims", [])
                if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str)
            )
            conflicts.append(
                {
                    "id": conflict_id,
                    "package_id": package_id,
                    "title": conflict.get("topic"),
                    "status": conflict.get("status"),
                    "claim_ids": claim_ids,
                    "weighted_terms": conflict_weighted_terms(conflict),
                }
            )
        for gap in _entity_rows(package, "references/knowledge-gaps.jsonl"):
            gap_id = gap.get("id")
            register("gap", gap_id, package_id)
            gaps.append(
                {
                    "id": gap_id,
                    "package_id": package_id,
                    "title": gap.get("statement"),
                    "status": gap.get("status"),
                    "weighted_terms": gap_weighted_terms(gap),
                }
            )
    return {
        "schema_version": CATALOG_SCHEMA,
        "runtime_version": RUNTIME_VERSION,
        "capabilities": copy.deepcopy(REFERENCE_CAPABILITIES),
        "policy": {
            "cross_module_claims": "preserve-separate-no-consensus",
            "capability_floor": "reference-only",
            "decision_guard_terms": DECISION_GUARD_TERMS,
        },
        "objects": sorted(objects, key=lambda row: row["id"]),
        "conflicts": sorted(conflicts, key=lambda row: row["id"]),
        "gaps": sorted(gaps, key=lambda row: row["id"]),
    }


def _iter_module_entities(package: Path) -> Iterable[tuple[str, str]]:
    for obj, _ in _knowledge_objects(package):
        if isinstance(obj.get("id"), str):
            yield "object", obj["id"]
    for kind, relative in (
        ("evidence", "references/evidence/anchors.jsonl"),
        ("relation", "references/knowledge/relations.jsonl"),
        ("conflict", "references/conflicts.jsonl"),
        ("gap", "references/knowledge-gaps.jsonl"),
    ):
        for row in _entity_rows(package, relative):
            if not isinstance(row.get("id"), str) or not row["id"]:
                raise CompositionError(f"{kind}_id_invalid: {relative}")
            yield kind, row["id"]


def validate_module_inputs(packages: list[Path], level: str = "publish") -> list[dict[str, Any]]:
    if level not in {"structure", "publish"}:
        raise CompositionError("invalid_module_level")
    if len(packages) < 2:
        raise CompositionError("composition_requires_two_packages")
    bindings: list[dict[str, Any]] = []
    package_ids: set[str] = set()
    module_ids: set[str] = set()
    entity_ids: dict[str, tuple[str, str]] = {}
    source_ids: dict[str, tuple[str, str]] = {}
    for package in packages:
        package = package.expanduser().resolve()
        if not package.is_dir():
            raise CompositionError(f"package_missing: {package}")
        if (package / "references/runtime/direct-reference.json").is_file():
            raise CompositionError(
                "unreviewed_direct_candidate_requires_extend: "
                f"{package}: use direct_reference_skill.py extend or complete Review and promotion"
            )
        errors = [
            issue
            for issue in validate_expert_skill(package, level=level)
            if issue.severity == "error"
        ]
        if errors:
            code = "input_publish_invalid" if level == "publish" else "input_structure_invalid"
            raise CompositionError(
                f"{code}: {package}: {errors[0].code}:{errors[0].path}"
            )
        manifest = load_json(package / "manifest.json")
        package_id = manifest["package"]["id"]
        if package_id in package_ids:
            raise CompositionError(f"duplicate_package_id: {package_id}")
        package_ids.add(package_id)
        if manifest.get("distribution", {}).get("source_mode") != "source-required":
            raise CompositionError(f"unsupported_distribution_mode: {package_id}")
        if manifest.get("capabilities") != REFERENCE_CAPABILITIES:
            raise CompositionError(f"capability_premature: {package_id}")
        if level == "structure" and manifest.get("package", {}).get("status") != "draft":
            raise CompositionError(
                f"module_lifecycle_invalid: {package_id}: draft composition requires draft children"
            )
        for path in package.rglob("*"):
            if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                raise CompositionError(f"nonregular_file: {package_id}:{path.relative_to(package)}")
            if path.is_file() and path.suffix.casefold() == ".pdf":
                raise CompositionError(f"embedded_source_forbidden: {package_id}")
        binding = module_binding(package, require_sealed=(level == "publish"))
        for source in binding["sources"]:
            if not isinstance(source, dict):
                raise CompositionError(f"source_record_invalid: {package_id}")
            source_id = source.get("source_id")
            source_hash = source.get("sha256")
            if not isinstance(source_id, str) or not isinstance(source_hash, str):
                raise CompositionError(f"source_record_invalid: {package_id}")
            prior_source = source_ids.get(source_id)
            if prior_source is not None and prior_source[0] != source_hash:
                raise CompositionError(
                    f"source_id_collision: {source_id}:{prior_source[1]}:{package_id}"
                )
            source_ids[source_id] = (source_hash, package_id)
        for module_id in binding["module_ids"]:
            if module_id in module_ids:
                raise CompositionError(f"module_id_collision: {module_id}")
            module_ids.add(module_id)
        for kind, identifier in _iter_module_entities(package):
            prior = entity_ids.get(identifier)
            if prior is not None:
                raise CompositionError(
                    f"global_id_collision: {identifier}:{prior[0]}:{prior[1]}:{kind}:{package_id}"
                )
            entity_ids[identifier] = (kind, package_id)
        bindings.append(binding)
    bindings.sort(key=lambda row: row["package_id"])
    overlaps = source_overlap_issues(bindings)
    if overlaps:
        raise CompositionError(f"source_scope_overlap_unreviewed: {overlaps[0]}")
    dependencies = dependency_issues(bindings)
    if dependencies:
        raise CompositionError(dependencies[0])
    return bindings


def validate_composite_skill(root: Path, level: str = "structure") -> list[Issue]:
    root = root.expanduser().resolve()
    issues: list[Issue] = []
    manifest_path = root / "manifest.json"
    lock_path = root / "composition-lock.json"
    if not manifest_path.is_file():
        _issue(issues, "error", "manifest_missing", "manifest.json", "Missing manifest.")
        return issues
    if not lock_path.is_file():
        _issue(issues, "error", "composition_lock_missing", "composition-lock.json", "Missing composition lock.")
        return issues
    manifest = _load_json(manifest_path, issues, "manifest.json")
    lock = _load_json(lock_path, issues, "composition-lock.json")
    if not isinstance(manifest, dict) or not isinstance(lock, dict):
        return issues
    if manifest.get("schema_version") != COMPOSITE_SCHEMA:
        _issue(issues, "error", "composite_schema_invalid", "manifest.json", "Unexpected schema.")
    if lock.get("schema_version") != LOCK_SCHEMA:
        _issue(issues, "error", "composition_lock_schema_invalid", "composition-lock.json", "Unexpected schema.")
    package_record = manifest.get("package")
    if not isinstance(package_record, dict) or not SLUG_RE.fullmatch(str(package_record.get("id", ""))):
        _issue(issues, "error", "package_id_invalid", "manifest.package", "Invalid package identity.")
        package_record = {}
    if not isinstance(package_record.get("name"), str) or not package_record.get("name"):
        _issue(issues, "error", "package_name_invalid", "manifest.package.name", "Display name is required.")
    if not isinstance(package_record.get("version"), str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?",
        package_record.get("version", ""),
    ):
        _issue(issues, "error", "package_version_invalid", "manifest.package.version", "Semantic version is required.")
    if not SLUG_RE.fullmatch(str(package_record.get("domain", ""))):
        _issue(issues, "error", "package_domain_invalid", "manifest.package.domain", "Hyphen-case domain is required.")
    if level == "publish" and package_record.get("status") != "ready":
        _issue(issues, "error", "not_ready", "manifest.package.status", "Composite is not ready.")
    if manifest.get("capabilities") != REFERENCE_CAPABILITIES:
        _issue(issues, "error", "capability_premature", "manifest.capabilities", "Phase 5 composition is reference-only.")
    if manifest.get("distribution") != {
        "source_mode": "source-required",
        "verification": "external-source-required",
    }:
        _issue(issues, "error", "distribution_invalid", "manifest.distribution", "Phase 5 composition is source-required.")
    policy = manifest.get("policy")
    if policy != {
        "cross_module_claims": "preserve-separate-no-consensus",
        "source_overlap": "reject-unreviewed",
    }:
        _issue(issues, "error", "composition_policy_invalid", "manifest.policy", "Cross-module claims must remain separate.")
    expected_lock_policy = {
        "cross_module_claims": "preserve-separate-no-consensus",
        "source_overlap": "reject-unreviewed",
        "capability_floor": "reference-only",
    }
    if lock.get("compiler_version") != COMPOSITE_COMPILER_VERSION or lock.get("policy") != expected_lock_policy:
        _issue(issues, "error", "composition_lock_policy_invalid", "composition-lock.json", "Compiler or policy differs.")
    build = manifest.get("build")
    if not isinstance(build, dict):
        build = {}
        _issue(issues, "error", "composition_build_invalid", "manifest.build", "Build record is required.")
    if build.get("compiler") not in {"technical-knowledge-compiler", "fake-expert"} or build.get("compiler_version") != COMPOSITE_COMPILER_VERSION:
        _issue(issues, "error", "composition_build_invalid", "manifest.build", "Compiler identity differs.")
    if build.get("created_at") != lock.get("created_at"):
        _issue(issues, "error", "composition_timestamp_mismatch", "manifest.build.created_at", "Build and lock timestamps differ.")
    bindings = lock.get("modules")
    if not isinstance(bindings, list) or len(bindings) < 2:
        _issue(issues, "error", "composition_modules_invalid", "composition-lock.json", "At least two module packages are required.")
        bindings = []
    if bindings != sorted(bindings, key=lambda row: str(row.get("package_id")) if isinstance(row, dict) else ""):
        _issue(issues, "error", "composition_lock_nondeterministic", "composition-lock.json", "Modules must be sorted by package ID.")
    seen_packages: set[str] = set()
    seen_modules: set[str] = set()
    seen_entities: dict[str, tuple[str, str]] = {}
    seen_sources: dict[str, tuple[str, str]] = {}
    for index, binding in enumerate(bindings):
        label = f"composition-lock.json.modules[{index}]"
        if not isinstance(binding, dict):
            _issue(issues, "error", "composition_binding_invalid", label, "Expected object.")
            continue
        package_id = binding.get("package_id")
        relative = binding.get("relative_path")
        if not isinstance(package_id, str) or package_id in seen_packages:
            _issue(issues, "error", "duplicate_package_id", label, str(package_id))
            continue
        seen_packages.add(package_id)
        if relative != f"modules/{package_id}":
            _issue(issues, "error", "module_path_invalid", label, str(relative))
            continue
        for field in ("manifest_sha256", "integrity_sha256"):
            if not isinstance(binding.get(field), str) or not SHA256_RE.fullmatch(binding[field]):
                _issue(issues, "error", "composition_binding_hash_invalid", f"{label}.{field}", "Expected SHA-256.")
        package = _safe_relative(root, relative)
        if package is None or not package.is_dir():
            _issue(issues, "error", "module_package_missing", relative, "Module package is missing.")
            continue
        child_manifest_path = package / "manifest.json"
        child_manifest = _load_json(child_manifest_path, issues, f"{relative}/manifest.json")
        if not isinstance(child_manifest, dict):
            continue
        if sha256_file(child_manifest_path) != binding.get("manifest_sha256"):
            _issue(issues, "error", "module_manifest_stale", relative, "Manifest hash differs from lock.")
        if sha256_json(child_manifest.get("integrity")) != binding.get("integrity_sha256"):
            _issue(issues, "error", "module_integrity_stale", relative, "Integrity hash differs from lock.")
        if child_manifest.get("package", {}).get("id") != package_id:
            _issue(issues, "error", "module_identity_mismatch", relative, "Package ID differs from lock.")
        if child_manifest.get("package", {}).get("version") != binding.get("package_version"):
            _issue(issues, "error", "module_version_mismatch", relative, "Package version differs from lock.")
        if child_manifest.get("capabilities") != REFERENCE_CAPABILITIES or binding.get("capabilities") != REFERENCE_CAPABILITIES:
            _issue(issues, "error", "capability_premature", relative, "Child capability exceeds the Phase 5 floor.")
        actual_sources = child_manifest.get("sources")
        actual_source_ids = sorted(
            source.get("source_id")
            for source in actual_sources or []
            if isinstance(source, dict) and isinstance(source.get("source_id"), str)
        )
        if actual_sources != binding.get("sources") or actual_source_ids != binding.get("source_ids"):
            _issue(issues, "error", "source_registry_stale", relative, "Source registry differs from lock.")
        for source in actual_sources or []:
            if not isinstance(source, dict):
                continue
            source_id = source.get("source_id")
            source_hash = source.get("sha256")
            if not isinstance(source_id, str) or not isinstance(source_hash, str):
                continue
            prior_source = seen_sources.get(source_id)
            if prior_source is not None and prior_source[0] != source_hash:
                _issue(issues, "error", "source_id_collision", relative, f"{source_id}:{prior_source[1]}")
            seen_sources[source_id] = (source_hash, package_id)
        composite_status = package_record.get("status")
        child_level = "structure" if composite_status == "draft" else "publish"
        child_errors = [
            issue
            for issue in validate_expert_skill(package, level=child_level)
            if issue.severity == "error"
        ]
        for child_error in child_errors:
            _issue(issues, "error", "module_publish_invalid", f"{relative}/{child_error.path}", child_error.code)
        if composite_status == "draft" and child_manifest.get("package", {}).get("status") != "draft":
            _issue(issues, "error", "module_lifecycle_invalid", relative, "Draft composite requires draft children.")
        actual_module_ids = sorted(
            row.get("module_id")
            for row in child_manifest.get("modules", [])
            if isinstance(row, dict) and isinstance(row.get("module_id"), str)
        )
        if actual_module_ids != binding.get("module_ids"):
            _issue(issues, "error", "module_registry_stale", relative, "Module IDs differ from lock.")
        if _module_registry(child_manifest) != binding.get("module_registry"):
            _issue(issues, "error", "module_registry_stale", relative, "Module versions or dependencies differ from lock.")
        for module_id in actual_module_ids:
            if module_id in seen_modules:
                _issue(issues, "error", "module_id_collision", relative, module_id)
            seen_modules.add(module_id)
        try:
            for kind, identifier in _iter_module_entities(package):
                prior = seen_entities.get(identifier)
                if prior is not None:
                    _issue(issues, "error", "global_id_collision", relative, f"{identifier}:{prior[0]}:{prior[1]}:{kind}")
                else:
                    seen_entities[identifier] = (kind, package_id)
        except (CompositionError, OSError, json.JSONDecodeError, ValueError) as error:
            _issue(issues, "error", "module_inventory_invalid", relative, str(error))
    if source_overlap_issues([row for row in bindings if isinstance(row, dict)]):
        _issue(issues, "error", "source_scope_overlap_unreviewed", "composition-lock.json", "Same-source scopes overlap or are unknown.")
    for dependency_issue in dependency_issues([row for row in bindings if isinstance(row, dict)]):
        _issue(issues, "error", dependency_issue.split(":", 1)[0], "composition-lock.json", dependency_issue)
    expected_fingerprint = input_fingerprint([row for row in bindings if isinstance(row, dict)])
    if lock.get("input_fingerprint") != expected_fingerprint or build.get("input_fingerprint") != expected_fingerprint:
        _issue(issues, "error", "composition_fingerprint_stale", "composition-lock.json", "Input fingerprint differs.")
    if build.get("composition_lock_sha256") != sha256_file(lock_path):
        _issue(issues, "error", "composition_lock_stale", "manifest.build", "Lock hash differs.")
    expected_sources: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        for source in binding.get("sources", []):
            if not isinstance(source, dict) or not isinstance(source.get("source_id"), str):
                continue
            source_id = source["source_id"]
            record = expected_sources.setdefault(
                source_id,
                {
                    "source_id": source_id,
                    "sha256": source.get("sha256"),
                    "media_type": source.get("media_type"),
                    "original_filename": source.get("original_filename"),
                    "required_by_packages": [],
                },
            )
            record["required_by_packages"].append(binding.get("package_id"))
    for record in expected_sources.values():
        record["required_by_packages"] = sorted(set(record["required_by_packages"]))
    expected_source_rows = sorted(expected_sources.values(), key=lambda row: row["source_id"])
    if manifest.get("sources") != expected_source_rows:
        _issue(issues, "error", "composite_source_registry_stale", "manifest.sources", "Source registry differs from locked modules.")
    catalog_path = root / "references/runtime/catalog.json"
    catalog = _load_json(catalog_path, issues, "references/runtime/catalog.json") if catalog_path.is_file() else None
    if catalog is None:
        _issue(issues, "error", "composite_catalog_missing", "references/runtime/catalog.json", "Runtime catalog is missing.")
    else:
        try:
            expected_catalog = build_composite_catalog(root, [row for row in bindings if isinstance(row, dict)])
            if catalog != expected_catalog:
                _issue(issues, "error", "composite_catalog_stale", "references/runtime/catalog.json", "Catalog differs from child packages.")
        except (CompositionError, OSError, json.JSONDecodeError, ValueError) as error:
            _issue(issues, "error", "composite_catalog_invalid", "references/runtime/catalog.json", str(error))
    for required in ("SKILL.md", "scripts/query_composite.py", "agents/openai.yaml"):
        if not (root / required).is_file():
            _issue(issues, "error", "composite_file_missing", required, "Required composite file is missing.")
    try:
        actual_files = composite_tracked_files(root)
    except CompositionError as error:
        _issue(issues, "error", "nonregular_file", ".", str(error))
        actual_files = {}
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict):
        severity = "error" if level == "publish" else "warning"
        _issue(issues, severity, "unsealed_package", "manifest.integrity", "Composite is unsealed.")
    else:
        recorded = integrity.get("files")
        if integrity.get("algorithm") != "sha256" or integrity.get("sealed_at") != build.get("created_at"):
            _issue(issues, "error", "composite_integrity_metadata_invalid", "manifest.integrity", "Seal metadata differs from build.")
        if not isinstance(recorded, dict) or recorded != actual_files:
            _issue(issues, "error", "composite_integrity_mismatch", "manifest.integrity.files", "Exact file map differs.")
    return sorted(issues, key=lambda item: (item.severity != "error", item.code, item.path))
