#!/usr/bin/env python3
"""Bounded, standard-library science proposals. Structural validity grants no authority."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from compiler_version import (SCIENCE_SIDECAR_SCHEMA, SCIENCE_RECORD_SCHEMA,
                              SCIENCE_PROFILE_SCHEMA, SCIENCE_PROTOCOL)

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT.parent / "assets/schemas/science-sidecar.schema.json"
MAX_BYTES = 1024 * 1024
NONSEMANTIC = {"id", "content_sha256", "provenance", "review_state", "schema_version", "kind"}


def _bounded(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("science_depth_limit")
    if isinstance(value, str):
        if len(value) > 4096:
            raise ValueError("science_string_limit")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if abs(value) > 1e100 or not math.isfinite(value):
            raise ValueError("science_non_finite_or_unbounded_number")
    elif isinstance(value, (list, dict)):
        if len(value) > 4096:
            raise ValueError("science_collection_limit")
        if isinstance(value, dict):
            if any(not isinstance(k, str) for k in value):
                raise ValueError("science_non_string_key")
            for k, item in value.items():
                _bounded(k, depth + 1)
                _bounded(item, depth + 1)
        else:
            for item in value:
                _bounded(item, depth + 1)
    elif value is not None and not isinstance(value, bool):
        raise ValueError("science_non_json_value")


def canonical_json(value: Any) -> bytes:
    _bounded(value)
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("science_byte_limit")
    return data


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def load_json(path: Path) -> Any:
    path = path.expanduser().absolute()
    if any(p.is_symlink() for p in [path, *path.parents]) or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("science_file_unsafe_or_too_large")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("science_duplicate_json_key")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("science_non_finite_json")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    canonical_json(value)
    return value


def _schema() -> dict:
    # Read schema directly: schema documents themselves are larger/deeper than records.
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if (schema["$id"] != SCIENCE_SIDECAR_SCHEMA
            or schema["properties"]["schema_version"]["const"] != SCIENCE_SIDECAR_SCHEMA
            or schema["properties"]["protocol"]["const"] != SCIENCE_PROTOCOL
            or schema["$defs"]["profile"]["properties"]["schema_version"]["const"] != SCIENCE_PROFILE_SCHEMA
            or any(schema["$defs"][kind]["properties"]["schema_version"]["const"] != SCIENCE_RECORD_SCHEMA
                   for kind in ("SymbolBinding", "ConditionSet", "MaterialState", "PropertyObservation", "FormulaCard"))):
        raise ValueError("science_schema_registry_drift")
    return schema


def _check(value: Any, schema: dict, root: dict, path: str = "$") -> None:
    """Only the local schema's closed subset; no remote references or evaluation."""
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/$defs/"):
            raise ValueError("science_external_schema_forbidden")
        return _check(value, root["$defs"][reference.split("/")[-1]], root, path)
    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                _check(value, branch, root, path)
                matches += 1
            except ValueError:
                pass
        if matches != 1:
            raise ValueError(f"science_schema_invalid:{path}:oneOf")
        return
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise ValueError(f"science_schema_invalid:{path}:const")
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        raise ValueError(f"science_schema_invalid:{path}:enum")
    kinds = {"object": type(value) is dict, "array": type(value) is list, "string": type(value) is str,
             "number": type(value) in (int, float), "integer": type(value) is int, "boolean": type(value) is bool}
    if "type" in schema and not kinds.get(schema["type"], False):
        raise ValueError(f"science_schema_invalid:{path}:type")
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 4096):
            raise ValueError(f"science_schema_invalid:{path}:length")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ValueError(f"science_schema_invalid:{path}:pattern")
    if type(value) in (int, float):
        if not math.isfinite(value) or not schema.get("minimum", -1e100) <= value <= schema.get("maximum", 1e100):
            raise ValueError(f"science_schema_invalid:{path}:number")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 512):
            raise ValueError(f"science_schema_invalid:{path}:count")
        for index, item in enumerate(value):
            _check(item, schema["items"], root, f"{path}[{index}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if set(value) != set(schema.get("required", [])) or schema.get("additionalProperties") is not False:
            raise ValueError(f"science_schema_invalid:{path}:closed_fields")
        for key, item in value.items():
            _check(item, props[key], root, f"{path}.{key}")


def validate_shape(value: Any, definition: str | None = None) -> None:
    canonical_json(value)
    schema = _schema()
    _check(value, schema["$defs"][definition] if definition else schema, schema)


def bind_record(record: dict) -> dict:
    """Explicit proposal construction only; validators never repair a stale hash."""
    record = copy.deepcopy(record)
    identity = {k: v for k, v in record.items() if k not in {"id", "content_sha256", "provenance", "review_state"}}
    record["id"] = "scr-" + sha256_json(identity)[:24]
    record["content_sha256"] = sha256_json({k: v for k, v in record.items() if k != "content_sha256"})
    return record


def tool_hashes() -> dict[str, str]:
    files = [ROOT / name for name in ("science_contract.py", "science_workpack.py", "compiler_version.py",
                                    "semantic_assurance.py", "formula_execution.py", "expert_skill_contract.py")]
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [*files, SCHEMA_PATH]}


def make_sidecar(records: list[dict], package: dict, profile: dict, *, data_origin: str) -> dict:
    value = {"schema_version": SCIENCE_SIDECAR_SCHEMA, "protocol": SCIENCE_PROTOCOL,
             "base_package": copy.deepcopy(package["fingerprint"]), "profile_sha256": sha256_json(profile),
             "tool_sha256s": tool_hashes(), "data_origin": data_origin, "review_state": "candidate",
             "production_eligible": False, "knowledge_verified": False, "executable": False,
             "records": copy.deepcopy(records), "inventory": [{"id": r["id"], "sha256": r["content_sha256"]} for r in records]}
    value["sidecar_sha256"] = sha256_json(value)
    validate_sidecar(value, package, profile)
    return value


def _unknown(value: Any) -> bool:
    return isinstance(value, dict) and value.get("status") == "unknown"


def _object(reference: dict, package: dict) -> dict:
    fp = package["fingerprint"]
    if reference["package_id"] != fp["package_id"] or reference["package_version"] != fp["package_version"]:
        raise ValueError("science_object_package_mismatch")
    matches = [o for o in package["objects"] if o["id"] == reference["object_id"]]
    if len(matches) != 1 or sha256_json(matches[0]) != reference["object_sha256"]:
        raise ValueError("science_object_missing_or_hash_drift")
    return matches[0]


def _evidence(reference: dict, package: dict) -> dict:
    owner = _object(reference["object_ref"], package)
    matches = [a for a in package["anchors"] if a["id"] == reference["evidence_id"]]
    if (len(matches) != 1 or sha256_json(matches[0]) != reference["evidence_sha256"]
            or reference["evidence_id"] not in owner.get("evidence_ids", [])
            or owner["id"] not in matches[0].get("supports", [])
            or matches[0].get("source_id") not in owner.get("source_ids", [])):
        raise ValueError("science_evidence_outside_object_focus_or_drift")
    return matches[0]


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def validate_sidecar(value: dict | None, package: dict, profile: dict) -> dict:
    denied = {"production_eligible": False, "knowledge_verified": False, "executable": False, "domain_accuracy": "blocked"}
    if value is None:
        return {"status": "unavailable", "reason_codes": ["optional_science_sidecar_absent"], **denied}
    validate_shape(value)
    validate_shape(profile, "profile")
    if value["base_package"] != package["fingerprint"]:
        raise ValueError("science_base_package_drift")
    if value["profile_sha256"] != sha256_json(profile) or value["tool_sha256s"] != tool_hashes():
        raise ValueError("science_profile_or_tool_drift")
    if value["sidecar_sha256"] != sha256_json({k: v for k, v in value.items() if k != "sidecar_sha256"}):
        raise ValueError("science_sidecar_hash_drift")
    records = value["records"]
    by_id = {r["id"]: r for r in records}
    if len(by_id) != len(records):
        raise ValueError("science_duplicate_record")
    if value["inventory"] != [{"id": r["id"], "sha256": r["content_sha256"]} for r in records]:
        raise ValueError("science_inventory_drift")
    edges: dict[str, set[str]] = {r["id"]: set() for r in records}
    def link(owner, ident, kind=None):
        if _unknown(ident):
            return
        if ident not in by_id or (kind and by_id[ident]["kind"] != kind):
            raise ValueError("science_dangling_or_wrong_kind_reference")
        edges[owner].add(ident)
    unknowns, missing_requirements = [], []
    for record in records:
        if bind_record(record) != record:
            raise ValueError("science_record_identity_or_hash_drift")
        # Every object/evidence ref resolves against canonical objects, not nearby context.
        for item in _walk(record):
            if "object_sha256" in item:
                _object(item, package)
            if "evidence_sha256" in item:
                _evidence(item, package)
        for evidence in record["source_evidence_refs"]:
            if evidence["object_ref"] != record["source_object_ref"]:
                raise ValueError("science_source_evidence_outside_focus")
        fields = [p["field"] for p in record["provenance"]]
        if len(set(fields)) != len(fields) or set(fields) != set(record) - NONSEMANTIC:
            raise ValueError("science_field_provenance_incomplete")
        for provenance in record["provenance"]:
            state = provenance["status"]
            if state in {"source-explicit", "source-paraphrase"} and not provenance["evidence_refs"]:
                raise ValueError("science_source_evidence_required")
            if state == "inferred" and not provenance["derivation_refs"]:
                raise ValueError("science_derivation_required")
            if _unknown(record[provenance["field"]]) and state != "unknown":
                raise ValueError("science_unknown_mislabeled_as_fact")
            if state == "unknown":
                unknowns.append({"record_id": record["id"], "field": provenance["field"]})
            for ident in provenance["derivation_refs"]:
                link(record["id"], ident)
        kind = record["kind"]
        if kind == "SymbolBinding":
            shape = record["shape"]
            if not _unknown(shape):
                if ((record["value_type"] in {"scalar", "complex"} and shape != [])
                        or (record["value_type"] == "vector" and len(shape) != 1)
                        or (record["value_type"] == "tensor" and len(shape) < 2)):
                    raise ValueError("science_symbol_shape_mismatch")
        elif kind == "ConditionSet":
            assess_conditions(record, {})
        elif kind == "MaterialState":
            missing_requirements += [{"record_id": record["id"], "field": f, "reason_code": "required_material_state_unknown"}
                                     for f in profile["required_material_fields"] if _unknown(record[f])]
            composition = record["composition"]
            if not _unknown(composition):
                limit = 1 if composition["scale"] == "fraction" else 100
                names = [c["component"] for c in composition["components"]]
                if len(names) != len(set(names)) or sum(c["value"] for c in composition["components"]) > limit:
                    raise ValueError("science_composition_basis_invalid")
        elif kind == "PropertyObservation":
            link(record["id"], record["material_state_ref"], "MaterialState")
            supplied = {row["name"]: row["value"] for row in record["measurement_conditions"]}
            missing_requirements += [{"record_id": record["id"], "field": f, "reason_code": "required_measurement_condition_unknown"}
                                     for f in profile["required_measurement_conditions"] if f not in supplied or _unknown(supplied[f])]
            rep = record["value_representation"]
            if rep.get("kind") == "tensor" and math.prod(rep["shape"]) != len(rep["values"]):
                raise ValueError("science_tensor_size_mismatch")
            if rep.get("kind") == "curve" and any(a["x"] >= b["x"] for a, b in zip(rep["points"], rep["points"][1:])):
                raise ValueError("science_curve_order_invalid")
        elif kind == "FormulaCard":
            for ident in record["symbol_bindings"]:
                link(record["id"], ident, "SymbolBinding")
            for ident in record["dependency_refs"]:
                link(record["id"], ident)
            link(record["id"], record["condition_set_ref"], "ConditionSet")
            astref = record["executable_ast_ref"]
            if not _unknown(astref):
                source = _object(astref["source_object_ref"], package)
                formula = source.get("formula", {})
                if (package["manifest"].get("package", {}).get("status") != "ready"
                        or package["manifest"].get("capabilities", {}).get("executable") is not True
                        or source.get("origin") not in {"source-explicit", "source-paraphrase"}
                        or package.get("execution_ast_bindings", {}).get(source["id"]) != astref["ast_sha256"]
                        or formula.get("ast_sha256") != astref["ast_sha256"]
                        or sha256_json(formula.get("ast")) != astref["ast_sha256"]):
                    raise ValueError("science_existing_reviewed_ast_required")
                from formula_execution import validate_formula_ast
                validate_formula_ast(formula["ast"])
            if record["execution_state"] == "requires-existing-gates":
                if (_unknown(astref) or record["representation_state"] != "scalar-ast"
                        or record["equation_kind"] == "unknown"
                        or (record["equation_kind"] == "numerical-value-equation" and _unknown(record["unit_convention"]))):
                    raise ValueError("science_formula_reference_only")
            if not _unknown(record["unit_convention"]):
                for row in record["unit_convention"]["input_units"]:
                    if row["symbol_binding_ref"] not in record["symbol_bindings"]:
                        raise ValueError("science_unit_convention_unbound_symbol")
        for field in ("conditions", "measurement_conditions"):
            if field in record:
                names = [row["name"] for row in record[field]]
                if len(names) != len(set(names)):
                    raise ValueError("science_duplicate_condition_name")
    visited, active = set(), set()
    def visit(ident):
        if ident in active:
            raise ValueError("science_cyclic_semantic_dependency")
        if ident not in visited:
            active.add(ident)
            for target in edges[ident]:
                visit(target)
            active.remove(ident)
            visited.add(ident)
    for ident in by_id:
        visit(ident)
    if value["data_origin"] == "source-backed-proposal":
        for item in _walk(value):
            for key, val in item.items():
                if isinstance(val, str) and (key.endswith("sha256") or key.endswith("_id") or key == "package_id"):
                    if val == "0" * 64 or any(word in val.lower() for word in ("synthetic", "placeholder", "example")):
                        raise ValueError("science_placeholder_or_synthetic_production_reference")
    return {"status": "candidate-valid", "record_count": len(records), "unknown_fields": unknowns,
            "missing_requirements": missing_requirements,
            "reason_codes": ["independent_science_review_required", "production_publication_not_implemented"], **denied}


def resolve_symbols(records: list[dict], glyph: str, *, scope_ref=None, quantity_kind=None) -> dict:
    candidates = [copy.deepcopy(r) for r in records if r["kind"] == "SymbolBinding" and r["original_glyph"] == glyph
                  and (scope_ref is None or r["scope_ref"] == scope_ref)
                  and (quantity_kind is None or r["quantity_kind"] == quantity_kind)]
    status = "ambiguous" if len(candidates) > 1 else "resolved" if candidates else "unknown"
    if len(candidates) == 1 and any(_unknown(candidates[0][f]) for f in ("scope_ref", "definition_ref", "quantity_kind", "canonical_quantity_id")):
        status = "unknown"
    return {"status": status, "candidates": candidates, "execution_authorized": False}


def _combine(states: list[str], op: str = "all") -> str:
    decisive, fallback = ("violated", "satisfied") if op == "all" else ("satisfied", "violated")
    return decisive if decisive in states else "unknown" if "unknown" in states else fallback


def evaluate_predicate(expression: dict, inputs: dict, declared_inputs: list[dict]) -> dict:
    validate_shape(expression, "predicate")
    canonical_json(inputs)
    # Reuse the declared-input shape from ConditionSet rather than defining another grammar.
    schema = _schema()
    _check(declared_inputs, schema["$defs"]["ConditionSet"]["properties"]["declared_inputs"], schema)
    declared = {f"{r['source']}:{r['name']}" for r in declared_inputs}
    if len(declared) != len(declared_inputs) or set(inputs) - declared:
        raise ValueError("science_undeclared_or_duplicate_input")
    for value in inputs.values():
        validate_shape(value, "quantity")
    # Validate every branch before evaluation, including branches that need not run.
    for row in _walk(expression):
        if row.get("kind") == "input" and f"{row['source']}:{row['name']}" not in declared:
            raise ValueError("science_undeclared_input")
    def operand(value):
        return value["value"] if value["kind"] == "constant" else inputs.get(f"{value['source']}:{value['name']}", {"status": "unknown", "reason": "input_missing"})
    def result(status, reason, values=None, children=None):
        return {"status": status, "reason_code": reason, "inputs": values or [], "children": children or []}
    def compatible(values):
        return (all(not _unknown(v) for v in values)
                and len({(v["unit"], v["quantity_kind"], v["frame"]) for v in values}) == 1
                and (all(type(v["value"]) in (int, float) for v in values)
                     or len({type(v["value"]) for v in values}) == 1))
    def evaluate(expr):
        op = expr["op"]
        if op in {"all", "any", "not"}:
            children = [evaluate(child) for child in (expr["args"] if op != "not" else [expr["arg"]])]
            state = ({"unknown": "unknown", "satisfied": "violated", "violated": "satisfied"}[children[0]["status"]]
                     if op == "not" else _combine([c["status"] for c in children], op))
            return result(state, "three_state_" + op, children=children)
        if op == "known":
            value = operand(expr["arg"])
            return result("unknown" if _unknown(value) else "satisfied", "input_unknown" if _unknown(value) else "input_known", [value])
        left = operand(expr["left"])
        right = [operand(v) for v in expr["choices"]] if op == "in" else [operand(expr["lower"]), operand(expr["upper"])] if op == "between" else [operand(expr["right"])]
        values = [left, *right]
        if any(_unknown(v) for v in values):
            return result("unknown", "input_unknown", values)
        if op in {"same_quantity_kind", "same_frame"}:
            key = "quantity_kind" if op == "same_quantity_kind" else "frame"
            if any(v[key] in {"unknown", "not-applicable"} for v in values):
                return result("unknown", key + "_unknown", values)
            good = left[key] == right[0][key]
        else:
            if not compatible(values):
                return result("unknown", "type_unit_quantity_or_frame_mismatch", values)
            a, b = left["value"], right[0]["value"]
            if op in {"gt", "ge", "lt", "le", "between"} and not all(type(v["value"]) in (int, float) for v in values):
                return result("unknown", "numeric_input_required", values)
            if op == "between":
                if b > right[1]["value"]:
                    raise ValueError("science_reversed_interval")
                good = b <= a <= right[1]["value"]
            elif op == "in":
                good = a in [v["value"] for v in right]
            elif op == "eq": good = a == b
            elif op == "gt": good = a > b
            elif op == "ge": good = a >= b
            elif op == "lt": good = a < b
            else: good = a <= b
        return result("satisfied" if good else "violated", "predicate_" + op, values)
    return evaluate(expression)


def assess_conditions(condition: dict, inputs: dict) -> dict:
    validate_shape(condition, "ConditionSet")
    results = [evaluate_predicate(expr, inputs, condition["declared_inputs"]) for expr in condition["predicates"]]
    state = _combine([r["status"] for r in results])
    return {"status": state, "condition_results": results, "conditions_satisfied": state == "satisfied", "execution_authorized": False}


def assess_property(observation: dict, material: dict, requested: dict) -> dict:
    validate_shape(observation, "PropertyObservation")
    validate_shape(material, "MaterialState")
    canonical_json(requested)
    available = {row["name"]: row["value"] for row in material["conditions"]}
    available.update({row["name"]: row["value"] for row in observation["measurement_conditions"]})
    for key in ("material_identity", "sample_identity", "orientation_frame"):
        value = material[key]
        available[key] = value if _unknown(value) else {"status": "known", "value": value["value"], "unit": "1", "quantity_kind": "flag", "frame": "not-applicable"}
    linked = observation["material_state_ref"]
    if not _unknown(linked) and linked != material["id"]:
        raise ValueError("science_property_material_binding_mismatch")
    results = ([{"field": "material_state_ref", "status": "unknown", "reason_code": "material_state_binding_unknown", "inputs": [], "children": []}]
               if _unknown(linked) else [])
    for key, expected in requested.items():
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", key) is None:
            raise ValueError("science_invalid_condition_name")
        actual = available.get(key, {"status": "unknown", "reason": "condition_missing"})
        result = evaluate_predicate({"op": "eq", "left": {"kind": "constant", "value": actual}, "right": {"kind": "constant", "value": expected}}, {}, [])
        results.append({"field": key, **result})
    return {"status": _combine([r["status"] for r in results]) if results else "unknown",
            "condition_results": results, "value_qualifier": observation["value_qualifier"],
            "execution_authorized": False, "uncertainty": copy.deepcopy(observation["uncertainty"])}
