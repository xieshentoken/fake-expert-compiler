#!/usr/bin/env python3
"""Build deterministic source-free ready or explicit candidate Skill archives."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from typing import Any

from composite_contract import COMPOSITE_SCHEMA, validate_composite_skill
from direct_reference_skill import DIRECT_REFERENCE_PATH, validate_direct_reference
from expert_skill_contract import validate_expert_skill


RELEASE_SCHEMA = "tkc.skill-release/v0.2"
ARTIFACT_STAGES = {"ready-verified", "draft-distributable"}
FORBIDDEN_SUFFIXES = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".j2k",
    ".jpeg",
    ".jpg",
    ".jp2",
    ".pdf",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}
RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)


class ReleasePackagingError(RuntimeError):
    """Stable release-packaging failure."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _validate_created_at(value: str) -> None:
    if not RFC3339_RE.fullmatch(value):
        raise ReleasePackagingError("created_at_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleasePackagingError("created_at_invalid") from error
    if parsed.tzinfo is None:
        raise ReleasePackagingError("created_at_invalid")


def _ensure_empty_output(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise ReleasePackagingError(f"output_not_empty: {path}")
    else:
        path.mkdir(parents=True)
    return path


def _reject_directory_overlap(output: Path, package_root: Path) -> None:
    target = output.expanduser().resolve()
    source = package_root.expanduser().resolve()
    if target == source or target.is_relative_to(source) or source.is_relative_to(target):
        raise ReleasePackagingError("input_output_overlap")


def _package_files(root: Path) -> list[Path]:
    output: list[Path] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ReleasePackagingError(f"nonregular_file: {relative.as_posix()}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleasePackagingError(f"nonregular_file: {relative.as_posix()}")
        if relative.name == ".DS_Store":
            continue
        if any(part.startswith(".") or part in {"__pycache__", ".pytest_cache"} for part in relative.parts):
            raise ReleasePackagingError(f"release_junk_forbidden: {relative.as_posix()}")
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise ReleasePackagingError(f"embedded_source_or_render_forbidden: {relative.as_posix()}")
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ReleasePackagingError(
                f"non_text_release_file: {relative.as_posix()}"
            ) from error
        output.append(path)
    return output


def _validate_package(
    root: Path, manifest: dict[str, Any], artifact_stage: str
) -> str:
    if artifact_stage == "draft-distributable":
        errors = validate_direct_reference(root)
        if errors:
            raise ReleasePackagingError(f"input_candidate_invalid: {errors[0]}")
        return "expert-reference"
    if manifest.get("schema_version") == COMPOSITE_SCHEMA:
        issues = validate_composite_skill(root, level="publish")
        package_kind = "composite-reference"
    else:
        issues = validate_expert_skill(root, level="publish")
        package_kind = "expert-reference"
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise ReleasePackagingError(
            f"input_publish_invalid: {errors[0].code}:{errors[0].path}"
        )
    package = manifest.get("package")
    if not isinstance(package, dict) or package.get("status") != "ready":
        raise ReleasePackagingError("input_not_ready")
    distribution = manifest.get("distribution")
    if not isinstance(distribution, dict) or distribution.get("source_mode") != "source-required":
        raise ReleasePackagingError("unsupported_distribution_mode")
    capabilities = manifest.get("capabilities")
    if capabilities == {"reference": True, "decision_support": False, "executable": False}:
        return package_kind
    if (
        capabilities == {"reference": True, "decision_support": False, "executable": True}
        and package_kind == "expert-reference"
        and manifest.get("build", {}).get("compiler") == "fake-expert"
        and manifest.get("build", {}).get("compiler_version") == "0.4.0-execution"
    ):
        return "expert-executable"
    else:
        raise ReleasePackagingError("unsupported_capability_tier")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.flag_bits |= 0x800
    return info


def package_release(
    package_root: Path,
    output: Path,
    *,
    created_at: str,
    artifact_stage: str = "ready-verified",
) -> dict[str, Any]:
    _validate_created_at(created_at)
    if artifact_stage not in ARTIFACT_STAGES:
        raise ReleasePackagingError("artifact_stage_invalid")
    package_root = package_root.expanduser().resolve()
    if not package_root.is_dir():
        raise ReleasePackagingError(f"package_missing: {package_root}")
    manifest_path = package_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleasePackagingError("manifest_invalid") from error
    package_kind = _validate_package(package_root, manifest, artifact_stage)
    files = _package_files(package_root)
    package_record = manifest["package"]
    package_id = package_record.get("id")
    version = package_record.get("version")
    if not isinstance(package_id, str) or not package_id:
        raise ReleasePackagingError("package_id_invalid")
    if not isinstance(version, str) or not version:
        raise ReleasePackagingError("package_version_invalid")
    _reject_directory_overlap(output, package_root)
    output = _ensure_empty_output(output)
    archive_name = f"{package_id}-{version}.zip"
    archive_path = output / archive_name
    uncompressed_bytes = 0
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for path in files:
            relative = path.relative_to(package_root).as_posix()
            data = path.read_bytes()
            uncompressed_bytes += len(data)
            archive.writestr(
                _zip_info(f"{package_id}/{relative}"),
                data,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    verifier_source = Path(__file__).with_name("verify_skill_release.py")
    if not verifier_source.is_file():
        raise ReleasePackagingError("release_verifier_missing")
    verifier_target = output / "verify_skill_release.py"
    shutil.copyfile(verifier_source, verifier_target)
    runtime_entrypoint = (
        "scripts/query_composite.py"
        if package_kind == "composite-reference"
        else "scripts/query_reference.py"
    )
    response_schema = (
        "tkc.composite-runtime-response/v0.1"
        if package_kind == "composite-reference"
        else "tkc.runtime-response/v0.1"
    )
    integrity = manifest.get("integrity") if isinstance(manifest.get("integrity"), dict) else {}
    release_manifest = {
        "schema_version": RELEASE_SCHEMA,
        "artifact_stage": artifact_stage,
        "release_created_at": created_at,
        "package": {
            "id": package_id,
            "name": package_record.get("name"),
            "version": version,
            "kind": package_kind,
            "root_directory": package_id,
            "manifest_sha256": sha256_file(manifest_path),
            "integrity_file_count": len(integrity.get("files", {}))
            if isinstance(integrity.get("files"), dict)
            else 0,
        },
        "archive": {
            "filename": archive_name,
            "sha256": sha256_file(archive_path),
            "file_count": len(files),
            "uncompressed_bytes": uncompressed_bytes,
            "format": "zip",
            "timestamp_profile": "dos-epoch-fixed-v1",
        },
        "adapter": {
            "skill_entrypoint": "SKILL.md",
            "runtime_entrypoint": runtime_entrypoint,
            "working_directory": "package-root",
            "interpreter": "python3-standard-library",
            "invocation": [
                "python3",
                "-B",
                runtime_entrypoint,
                "--query",
                "{query}",
                "--limit",
                "{limit}",
            ],
            "response_schema": response_schema,
            "permissions": ["read-package-files", "spawn-local-python"],
        },
        "distribution": manifest.get("distribution"),
        "capabilities": manifest.get("capabilities"),
        "restrictions": {
            "source_pdf_included": False,
            "page_renders_included": False,
            "external_source_required_for_full_verification": True,
            "decision_certification_available": False,
            "executable_procedures_available": package_kind == "expert-executable",
            "knowledge_verified": artifact_stage == "ready-verified",
        },
        "verifier": {
            "filename": verifier_target.name,
            "sha256": sha256_file(verifier_target),
            "interpreter": "python3-standard-library",
        },
    }
    if artifact_stage == "draft-distributable":
        direct = json.loads(
            (package_root / DIRECT_REFERENCE_PATH).read_text(encoding="utf-8")
        )
        catalog = json.loads(
            (package_root / "references/runtime/catalog.json").read_text(
                encoding="utf-8"
            )
        )
        release_manifest["candidate_assurance"] = {
            "knowledge_state": direct["usage"]["knowledge_state"],
            "review_state": direct["usage"]["review_state"],
            "knowledge_verified": direct["usage"]["knowledge_verified"],
            "gold": direct["gold"],
            "direct_reference_sha256": sha256_file(
                package_root / DIRECT_REFERENCE_PATH
            ),
            "warnings": catalog["policy"]["assurance"]["warnings"],
        }
        release_manifest["adapter"]["response_schema_path"] = (
            "references/runtime/response.schema.json"
        )
    if package_kind == "expert-executable":
        release_manifest["adapter"]["execution_entrypoint"] = "scripts/run_formula_sandbox.py"
        release_manifest["adapter"]["execution_invocation"] = [
            "python3", "-B", "scripts/run_formula_sandbox.py", "--formula", "{object_id}",
        ]
    lock_path = package_root / "composition-lock.json"
    if lock_path.is_file():
        release_manifest["package"]["composition_lock_sha256"] = sha256_file(lock_path)
    release_manifest_path = output / "release-manifest.json"
    _write_json(release_manifest_path, release_manifest)
    checksum_paths = [archive_path, release_manifest_path, verifier_target]
    (output / "SHA256SUMS").write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in sorted(checksum_paths)),
        encoding="utf-8",
    )
    return release_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument(
        "--artifact-stage",
        choices=sorted(ARTIFACT_STAGES),
        default="ready-verified",
        help="Default preserves the ready-only gate; draft distribution is explicit.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = package_release(
            args.package,
            args.output,
            created_at=args.created_at,
            artifact_stage=args.artifact_stage,
        )
    except (ReleasePackagingError, OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
