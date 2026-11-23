"""Inspect an explicitly configured local OCR runtime without loading a model."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from compiler_version import OCR_LOCAL_CONFIG_SCHEMA
from paddle_runtime_contract import inspect_local_model_status, validate_runtime_contract
from paddle_runtime_qualification import validate_qualification_plan, validate_runtime_qualification
from scanned_pdf_ocr import qualified_backend_configuration

SUPPORTED = {"paddleocr-ppocrv6": ["text"], "paddleocr-ppstructure-v3": ["text", "structure"]}
CONFIG_KEYS = {"schema_version", "backend", "python", "model_root", "runtime_root", "worker",
               "backend_config", "qualification_plan", "qualification_receipt", "expected_distributions"}


def resolve_config_path(path: Path | None) -> Path | None:
    value = str(path) if path is not None else os.environ.get("FAKE_EXPERT_OCR_CONFIG", "").strip()
    return Path(value).expanduser().absolute() if value else None


def availability_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Keep status receipts public to the local caller, not duplicated engines."""
    result = {key: value for key, value in report.items() if key != "execution"}
    result["checks"] = [{**row, **({"details": {key: value for key, value in row["details"].items()
                                               if key != "required_model_files"}} if "details" in row else {})}
                        for row in report.get("checks", [])]
    return result


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    from intake_contracts import validate_ocr_config
    validate_ocr_config(value)
    if not isinstance(value, dict) or set(value) - CONFIG_KEYS or value.get("schema_version") != OCR_LOCAL_CONFIG_SCHEMA:
        raise ValueError("ocr_config_invalid")
    if value.get("backend") not in SUPPORTED:
        raise ValueError("ocr_backend_unsupported")
    for key in CONFIG_KEYS - {"schema_version", "backend", "expected_distributions"}:
        if key in value and (not isinstance(value[key], str) or not Path(value[key]).expanduser().is_absolute()):
            raise ValueError(f"ocr_config_absolute_path_required:{key}")
        if key in value:
            value[key] = str(Path(value[key]).expanduser().absolute())
    expected = value.get("expected_distributions", {})
    if not isinstance(expected, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in expected.items()):
        raise ValueError("ocr_expected_distributions_invalid")
    return value


def _file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def probe_interpreter(path: Path, *, timeout: int = 20, distributions: list[str] | None = None) -> dict[str, Any]:
    """Execute the supplied launcher, not its resolved base Python binary."""
    launcher = path.expanduser().absolute()
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        return {"status": "missing", "code": "interpreter_missing"}
    code = ("import importlib.metadata as m,json,sys\n"
            "out={}\n"
            "for name in json.loads(sys.argv[1]):\n"
            " try: out[name]=m.version(name)\n"
            " except m.PackageNotFoundError: out[name]=None\n"
            "print(json.dumps({'python':sys.version,'executable':sys.executable,'prefix':sys.prefix,'distributions':out},sort_keys=True))")
    try:
        with tempfile.TemporaryDirectory(prefix="tkc-intake-probe-") as temporary:
            environment = {"PATH": os.environ.get("PATH", ""), "HOME": temporary,
                           "PYTHONDONTWRITEBYTECODE": "1", "PIP_NO_INDEX": "1"}
            names = sorted(set(distributions or []) | {"paddleocr", "paddlepaddle"})
            completed = subprocess.run([str(launcher), "-I", "-c", code, json.dumps(names)], cwd=temporary, env=environment,
                                       text=True, capture_output=True, timeout=timeout, check=False)
        if completed.returncode:
            return {"status": "unusable", "code": "interpreter_probe_failed", "exit_code": completed.returncode}
        result = json.loads(completed.stdout)
        if (not isinstance(result, dict) or not isinstance(result.get("distributions"), dict)
                or any(name not in result["distributions"] for name in ("paddleocr", "paddlepaddle"))
                or any(not isinstance(value, (str, type(None))) for value in result["distributions"].values())
                or any(not isinstance(result.get(key), str) for key in ("python", "executable", "prefix"))):
            raise ValueError("shape")
        result.update(status="available", code="interpreter_available", launcher=str(launcher),
                      real_binary=str(launcher.resolve()), binary_sha256=_file_hash(launcher),
                      launcher_link=os.readlink(launcher) if launcher.is_symlink() else None)
        return result
    except subprocess.TimeoutExpired:
        return {"status": "unusable", "code": "interpreter_probe_timeout"}
    except (OSError, ValueError):
        return {"status": "unusable", "code": "interpreter_probe_invalid_json"}


def inspect_renderer(renderer: str) -> dict[str, Any]:
    expanded = str(Path(renderer).expanduser())
    selected = expanded if Path(expanded).is_file() else shutil.which(expanded)
    if not selected:
        return {"code": "renderer_missing", "passed": False}
    path = Path(selected).expanduser().absolute()
    if not os.access(path, os.X_OK):
        return {"code": "renderer_unusable", "passed": False}
    try:
        result = subprocess.run([str(path), "-v"], capture_output=True, text=True, timeout=10, check=False)
        version = (result.stderr or result.stdout).strip().splitlines()
        if result.returncode or not version or "pdftoppm" not in version[0].casefold():
            return {"code": "renderer_unusable", "passed": False}
        return {"code": "renderer_available", "passed": True, "path": str(path),
                "sha256": _file_hash(path), "version": version[0]}
    except (OSError, subprocess.SubprocessError):
        return {"code": "renderer_unusable", "passed": False}


def inspect_ocr(config_path: Path | None, *, renderer: str = "pdftoppm", capability: str = "text") -> dict[str, Any]:
    config_path = resolve_config_path(config_path)
    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {"status": "unavailable", "runnable": False, "checks": checks,
                              "selected_backend": None, "selection_reason": "explicit_configuration_required",
                              "ocr_invoked": False, "model_loaded": False, "network_enabled": False}
    def check(code: str, passed: bool, **fields: Any) -> None:
        checks.append({"code": code, "passed": passed, **fields})
    checks.append(inspect_renderer(renderer))
    if config_path is None:
        check("ocr_configuration_missing", False)
        return report
    if not config_path.is_file():
        check("ocr_configuration_missing", False)
        return report
    try:
        config = load_config(config_path)
    except (OSError, ValueError) as error:
        check("ocr_configuration_invalid", False, detail=str(error))
        return report
    backend = config["backend"]
    report.update(selected_backend=backend, selection_reason="explicit_backend_with_bound_local_model_contract",
                  config_sha256=_file_hash(config_path), capability=capability)
    check("backend_capability_match", capability in SUPPORTED[backend], supported=SUPPORTED[backend])
    plan_path = Path(config.get("qualification_plan", "/nonexistent-plan"))
    receipt_path = Path(config.get("qualification_receipt", "/nonexistent-receipt"))
    plan_hint: dict[str, Any] = {}
    try:
        if plan_path.is_file():
            plan_hint = json.loads(plan_path.read_text())
        expected_names = list((plan_hint.get("runtime_contract") or {}).get("runtime_inventory", {}).get("distributions", {}))
    except (OSError, ValueError, AttributeError, TypeError):
        expected_names = []
    probe = probe_interpreter(Path(config.get("python", "/nonexistent-python")),
                              distributions=sorted(set(expected_names) | set(config.get("expected_distributions", {}))))
    report["interpreter"] = probe
    check(probe["code"], probe["status"] == "available")
    versions = probe.get("distributions", {})
    missing = [name for name in ("paddleocr", "paddlepaddle") if not versions.get(name)]
    check("dependencies_missing" if missing else "dependencies_available", not missing, missing=missing)
    expected = config.get("expected_distributions", {})
    drift = [name for name, version in expected.items() if versions.get(name) != version]
    check("dependency_versions_stale" if drift else "dependency_versions_match", not drift, distributions=drift)
    model = Path(config.get("model_root", "/nonexistent-model-root")).expanduser()
    model_status: dict[str, Any] = {"available": False, "status": "not-run-missing-models", "missing_model_dirs": ["model_root"]}
    try:
        engine = (plan_hint.get("runtime_contract") or {}).get("configuration", {})
        if "backend_config" in config and Path(config["backend_config"]).is_file():
            engine = json.loads(Path(config["backend_config"]).read_text())
        if model.is_dir():
            model_status = inspect_local_model_status(backend, model, engine)
        check("model_available" if model_status.get("available") else "model_missing", bool(model_status.get("available")), details=model_status)
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        check("model_missing", False, detail=str(error))
    if not plan_path.is_file() or not receipt_path.is_file():
        check("qualification_missing", False)
        return report
    try:
        plan = json.loads(plan_path.read_text())
        receipt = json.loads(receipt_path.read_text())
        contract = plan["runtime_contract"]
        validate_qualification_plan(plan, replay_local=True)
        validate_runtime_contract(contract)
        validate_runtime_qualification(receipt, contract=contract, plan=plan, backend_id=backend, require_qualified=True)
        comparisons = {"python": "interpreter", "model_root": "model_root", "runtime_root": "runtime_root", "worker": "worker_path"}
        for key, contract_key in comparisons.items():
            supplied_path = Path(config[key]).expanduser() if key in config else None
            bound_path = Path(contract[contract_key]).expanduser()
            # Normalize directory aliases (/var vs /private/var), while keeping
            # the venv launcher itself unresolved for interpreter comparison.
            supplied_identity = ((supplied_path.parent.resolve() / supplied_path.name) if key == "python" else supplied_path.resolve()) if supplied_path else None
            bound_identity = (bound_path.parent.resolve() / bound_path.name) if key == "python" else bound_path.resolve()
            if supplied_identity != bound_identity:
                raise ValueError(f"qualification_configuration_mismatch:{key}")
        if contract.get("backend_id") != backend or contract.get("available") is not True:
            raise ValueError("qualification_runtime_unusable")
        if contract["runtime_inventory"]["distributions"] != versions:
            raise ValueError("qualification_dependency_inventory_drift")
        execution = qualified_backend_configuration(plan, receipt)
        if "backend_config" in config:
            supplied = json.loads(Path(config["backend_config"]).read_text())
            if supplied not in (execution, contract["configuration"]):
                raise ValueError("qualification_backend_configuration_drift")
        check("qualification_valid", True, receipt_sha256=receipt["receipt_sha256"],
              runtime_contract_sha256=contract["contract_sha256"], worker_sha256=contract["worker_sha256"])
        report["execution"] = {"backend": backend, "python": config["python"], "model_root": str(model),
                               "backend_config": execution, "plan_sha256": plan["plan_sha256"],
                               "receipt_sha256": receipt["receipt_sha256"]}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        detail = str(error)
        check("qualification_stale" if any(word in detail for word in ("drift", "mismatch", "stale")) else "qualification_unusable", False, detail=detail)
    report["runnable"] = bool(checks) and all(row["passed"] for row in checks)
    report["status"] = "runnable" if report["runnable"] else "unavailable"
    return report
