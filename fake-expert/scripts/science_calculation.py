#!/usr/bin/env python3
"""Versioned scalar input/condition gates around the existing sealed runner."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
from decimal import DecimalException, localcontext
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import consultation_contract as cc
import formula_execution as legacy
import science_contract as sc
import science_units as units
import science_workpack as sw
from compiler_version import (EXECUTION_COMPILER_VERSION, SCIENCE_CALCULATION_COMPILER_VERSION,
    SCIENCE_CALCULATION_PROTOCOL, SCIENCE_CALCULATION_SCHEMA, SCIENCE_CALCULATION_RECEIPT_SCHEMA,
    SCIENCE_CALCULATION_REVIEW_SCHEMA, SCIENCE_EXECUTION_PERMISSION_SCHEMA)

SCHEMA_PATH = sc.ROOT.parent / "assets/schemas/science-calculation-receipt.schema.json"


def shape(value, definition):
    sc.canonical_json(value)
    schema = json.loads(SCHEMA_PATH.read_text())
    if (schema["$id"] != SCIENCE_CALCULATION_RECEIPT_SCHEMA
            or schema["$defs"]["specification"]["properties"]["schema_version"]["const"] != SCIENCE_CALCULATION_SCHEMA
            or schema["$defs"]["permission"]["properties"]["schema_version"]["const"] != SCIENCE_EXECUTION_PERMISSION_SCHEMA
            or schema["$defs"]["receipt"]["properties"]["schema_version"]["const"] != SCIENCE_CALCULATION_RECEIPT_SCHEMA
            or schema["$defs"]["receipt"]["properties"]["compiler_version"]["const"] != SCIENCE_CALCULATION_COMPILER_VERSION):
        raise ValueError("science_calculation_schema_registry_drift")
    for key in ("specification", "receipt"):
        if schema["$defs"][key]["properties"]["protocol"]["const"] != SCIENCE_CALCULATION_PROTOCOL:
            raise ValueError("science_calculation_protocol_drift")
    for definitions in (json.loads(cc.SCHEMA_PATH.read_text())["$defs"], sc._schema()["$defs"]):
        if set(schema["$defs"]) & set(definitions):
            raise ValueError("science_calculation_definition_collision")
        schema["$defs"].update(definitions)
    for row in sc._walk(schema):
        reference = row.get("$ref", "")
        if reference.startswith(("science-sidecar.schema.json#/$defs/", "science-consultation.schema.json#/$defs/")):
            row["$ref"] = reference.split(".json", 1)[1]
    sc._check(value, schema["$defs"][definition], schema)


def tool_hashes():
    names = ("science_calculation.py", "science_units.py", "science_contract.py", "science_workpack.py",
             "consultation_contract.py", "compiler_version.py", "formula_execution.py", "formula_sandbox_runner.py",
             "expert_skill_contract.py", "semantic_assurance.py")
    paths = [sc.ROOT / name for name in names] + [SCHEMA_PATH, sc.SCHEMA_PATH, cc.SCHEMA_PATH]
    return [{"path": str(path.relative_to(sc.ROOT.parent)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in paths]


def numeric_preflight(ast, inputs):
    """Data-only domain/replay check, not package or execution authorization.

    The new protocol normalizes the old engine's initial Decimal context, numeric
    bounds and unit grammar before either interpreter or generated runner. The
    old generated code is never repaired in place or treated as an input script.
    """
    sc.canonical_json(ast)
    contract = legacy.validate_formula_ast(ast)
    if not isinstance(inputs, dict) or set(inputs) != set(contract.inputs):
        raise ValueError("science_execution_inputs_invalid")
    for item in inputs.values():
        if not isinstance(item, dict) or set(item) != {"value", "unit"}:
            raise ValueError("science_execution_input_invalid")
        units.decimal_number(item["value"])
    for row in sc._walk(ast):
        if row.get("kind") == "constant": units.decimal_number(row["value"])
    try:
        with localcontext() as context:
            # The sealed legacy child starts at precision 28 and switches to 50
            # inside evaluation. Match that deterministic contract without leaking
            # either setting into the caller's Decimal context.
            context.prec = 28
            for item in inputs.values():
                scale, _ = legacy.parse_unit(item["unit"])
                units.decimal_number(str(units.decimal_number(item["value"]) * scale))
            result = legacy.evaluate_formula_ast(contract, inputs)
            units.decimal_number(result["value"])
            return result
    except DecimalException as error:
        raise ValueError("science_numeric_domain_invalid") from error


def record_ref(record):
    return {"id": record["id"], "sha256": record["content_sha256"]}


def _record(sidecar, reference, kind=None):
    shape(reference, "calculation_record_ref")
    matches = [r for r in sidecar["records"] if r["id"] == reference["id"] and r["content_sha256"] == reference["sha256"]]
    if len(matches) != 1 or (kind and matches[0]["kind"] != kind):
        raise ValueError("science_calculation_record_stale_or_wrong_kind")
    if matches[0]["review_state"] in {"rejected", "quarantined"}:
        raise ValueError("science_calculation_record_rejected")
    return matches[0]


def _named(rows):
    result = {r["name"]: r["value"] for r in rows}
    if len(result) != len(rows):
        raise ValueError("science_duplicate_condition")
    return result


def select_property(sidecar, profile, selection):
    """Select one explicit state/observation; selection alone has no authority."""
    shape(selection, "selection")
    sc.validate_shape(profile, "profile")
    observation = _record(sidecar, selection["observation"], "PropertyObservation")
    material = _record(sidecar, selection["material_state"], "MaterialState")
    if observation["material_state_ref"] != material["id"]:
        raise ValueError("science_property_material_binding_mismatch")
    if sc._unknown(observation["method_ref"]) or selection["method_ref"] != observation["method_ref"]:
        raise ValueError("science_property_method_missing_or_mismatch")
    for key in ("sample_identity", "orientation_frame"):
        if sc._unknown(material[key]) or material[key]["value"] != selection[key]:
            raise ValueError("science_property_sample_or_direction_mismatch")
    if any(sc._unknown(material[key]) for key in profile["required_material_fields"]):
        raise ValueError("science_required_material_state_unknown")
    supplied = _named(selection["conditions"])
    actual = _named(material["conditions"])
    for name, value in _named(observation["measurement_conditions"]).items():
        if name in actual and units.canonical_quantity(actual[name]) != units.canonical_quantity(value):
            raise ValueError("science_property_state_condition_conflict")
        actual[name] = value
    required = set(profile["required_measurement_conditions"]) | set(actual)
    if set(supplied) != required:
        raise ValueError("science_property_required_conditions_missing_or_extra")
    value = observation["value_representation"]
    points = []
    axis = selection["axis"]
    if selection["mode"] == "linear":
        if value.get("kind") != "curve" or axis is None or selection["coordinate"] is None or axis["name"] not in required:
            raise ValueError("science_explicit_curve_axis_required")
        coordinate = units.normalize_quantity(selection["coordinate"], expected_kind=axis["quantity_kind"],
                                              expected_frame=axis["frame"], target_unit=value["x_unit"])
        if units.canonical_quantity(supplied[axis["name"]]) != units.canonical_quantity(selection["coordinate"]):
            raise ValueError("science_curve_coordinate_condition_mismatch")
        fixed = actual.get(axis["name"])
        if fixed is not None and not sc._unknown(fixed) and units.canonical_quantity(fixed) != units.canonical_quantity(selection["coordinate"]):
            raise ValueError("science_curve_fixed_condition_mismatch")
        x = units.decimal_number(coordinate["decimal_value"])
        data = value["points"]
        if len(data) < 2 or any(a["x"] >= b["x"] for a,b in zip(data,data[1:])):
            raise ValueError("science_curve_order_invalid")
        if not units.decimal_number(data[0]["x"]) <= x <= units.decimal_number(data[-1]["x"]):
            raise ValueError("science_property_extrapolation_forbidden")
        for a,b in zip(data,data[1:]):
            lo,hi = units.decimal_number(a["x"]),units.decimal_number(b["x"])
            if lo <= x <= hi:
                points = [copy.deepcopy(a),copy.deepcopy(b)]
                with localcontext() as context:
                    context.prec = 50
                    output = units.decimal_number(a["y"]) + (x-lo)/(hi-lo)*(units.decimal_number(b["y"])-units.decimal_number(a["y"]))
                break
    else:
        if value.get("kind") != "scalar" or axis is not None or selection["coordinate"] is not None:
            raise ValueError("science_scalar_property_required")
        output = units.decimal_number(value["value"])
    for name in required:
        if selection["mode"] == "linear" and name == axis["name"]:
            # Axis semantics come from the explicit, separately reviewed M3 plan,
            # never inferred from the curve's x_unit or a guessed temperature.
            continue
        if name not in actual or sc._unknown(actual[name]) or sc._unknown(supplied[name]):
            raise ValueError("science_property_condition_unknown")
        if units.canonical_quantity(actual[name]) != units.canonical_quantity(supplied[name]):
            raise ValueError("science_property_condition_mismatch")
    units.decimal_number(str(output))
    if sc._unknown(observation["unit"]):
        raise ValueError("science_property_unit_unknown")
    numeric = int(output) if output == output.to_integral() else float(output)
    if units.decimal_text(str(numeric)) != units.decimal_text(str(output)):
        raise ValueError("science_property_precision_loss")
    quantity = {"status": "known", "value": numeric, "unit": observation["unit"],
                "quantity_kind": observation["property_quantity_id"], "frame": material["orientation_frame"]["value"]}
    reported = selection["mode"] == "scalar" and not sc._unknown(observation["uncertainty"])
    result = {"selection": copy.deepcopy(selection), "selection_sha256": sc.sha256_json(selection),
              "observation": record_ref(observation), "material_state": record_ref(material), "quantity": quantity,
              "decimal_value": units.decimal_text(str(output)), "selected_points": points,
              "source_category": observation["source_category"], "value_qualifier": observation["value_qualifier"],
              "source_evidence_refs": copy.deepcopy(observation["evidence_refs"]),
              "uncertainty_status": "reported" if reported else "unavailable",
              "uncertainty_reason": "source-observation-only-not-output-propagation" if reported else "missing-or-interpolated-uncertainty-no-covariance-assumed",
              "source_uncertainty_sha256": sc.sha256_json(observation["uncertainty"]), "execution_authorized": False}
    shape(result, "property_receipt")
    return result


def _symbol(sidecar, reference, ast_symbol, formula, ast):
    binding = _record(sidecar, reference, "SymbolBinding")
    if (binding["original_glyph"] != ast_symbol or binding["scope_ref"] != formula["source_object_ref"]
            or binding["value_type"] != "scalar" or binding["shape"] != []
            or binding["value_domain"] not in {"real", "positive-real", "nonnegative-real"}
            or any(sc._unknown(binding[k]) for k in ("unit", "quantity_kind", "canonical_quantity_id", "definition_ref", "coordinate_frame", "dimension"))):
        raise ValueError("science_symbol_scope_type_or_definition_unavailable")
    if binding["id"] not in formula["symbol_bindings"]:
        raise ValueError("science_symbol_not_bound_to_formula")
    if tuple(binding["dimension"]) != legacy.parse_unit(ast["symbols"][ast_symbol]["unit"])[1]:
        raise ValueError("science_symbol_dimension_mismatch")
    if binding["unit"] != ast["symbols"][ast_symbol]["unit"]:
        raise ValueError("science_reviewed_symbol_ast_unit_mismatch")
    units._mapping(binding["quantity_kind"], binding["unit"])
    return binding


def build_plan(specification, request, package, sidecar, profile):
    shape(specification, "specification"); cc.shape(request, "request")
    cc.validate_request(request, [{"package": package}])
    sc.validate_sidecar(sidecar, package, profile)
    if sidecar is None or (specification["base_package"] != package["fingerprint"]
            or specification["sidecar_sha256"] != sidecar["sidecar_sha256"]
            or specification["request_sha256"] != sc.sha256_json(request)):
        raise ValueError("science_calculation_input_binding_drift")
    if request["intent"] != "calculate" or request["execution_requested"] is not True:
        raise ValueError("science_calculation_intent_required_not_authority")
    if request["source_scope"] != [package["fingerprint"]]:
        raise ValueError("science_calculation_single_package_scope_required")
    formula = _record(sidecar, specification["formula_card"], "FormulaCard")
    ast_ref = formula["executable_ast_ref"]
    if (sc._unknown(ast_ref) or formula["representation_state"] != "scalar-ast"
            or formula["execution_state"] != "requires-existing-gates" or sc._unknown(formula["condition_set_ref"])):
        raise ValueError("science_formula_execution_representation_unavailable")
    source = sc._object(ast_ref["source_object_ref"], package)
    if ast_ref["source_object_ref"] != formula["source_object_ref"]:
        raise ValueError("science_formula_ast_source_mismatch")
    ast = source["formula"]["ast"]; contract = legacy.validate_formula_ast(ast)
    bindings = {row["symbol"]: row for row in specification["input_bindings"]}
    if set(bindings) != set(contract.inputs) or len(bindings) != len(specification["input_bindings"]):
        raise ValueError("science_calculation_symbol_inputs_invalid")
    output_binding = _symbol(sidecar,specification["output_symbol_binding"],contract.output,formula,ast)
    facts = _named(request["facts"])
    inputs, properties = [], []
    for symbol in contract.inputs:
        row = bindings[symbol]; binding = _symbol(sidecar,row["symbol_binding"],symbol,formula,ast)
        if row["source"]["kind"] == "request":
            if row["source"]["name"] not in facts:
                raise ValueError("science_required_input_missing")
            value = facts[row["source"]["name"]]
        else:
            selected = select_property(sidecar,profile,row["source"]["selection"])
            if selected["observation"]["id"] not in formula["dependency_refs"]:
                raise ValueError("science_property_not_formula_dependency")
            value = selected["quantity"]; properties.append(selected)
        normalized = units.normalize_quantity(value,expected_kind=binding["quantity_kind"],
            target_unit=ast["symbols"][symbol]["unit"],expected_frame=binding["coordinate_frame"]["value"],basis=row["basis"])
        number = units.decimal_number(normalized["decimal_value"])
        if ((binding["value_domain"] == "positive-real" and number <= 0)
                or (binding["value_domain"] == "nonnegative-real" and number < 0)):
            raise ValueError("science_symbol_value_domain_violated")
        inputs.append({"symbol":symbol,"symbol_binding":record_ref(binding),"source":copy.deepcopy(row["source"]),
                       "original_quantity":copy.deepcopy(value),"normalized_quantity":normalized["quantity"],
                       "decimal_value":normalized["decimal_value"],"conversion":normalized["conversion"]})
    if formula["equation_kind"] == "numerical-value-equation":
        convention = formula["unit_convention"]
        if sc._unknown(convention): raise ValueError("science_numerical_unit_convention_required")
        native_units = {r["symbol_binding_ref"]:r for r in convention["input_units"]}
        if len(native_units) != len(convention["input_units"]) or set(native_units) != {r["symbol_binding"]["id"] for r in inputs}:
            raise ValueError("science_numerical_unit_convention_incomplete")
        for row in inputs:
            native = native_units[row["symbol_binding"]["id"]]
            kind = row["normalized_quantity"]["quantity_kind"]
            if (native["unit"] != row["normalized_quantity"]["unit"] or native["unit"] != units.UNITS[kind][0]
                    or native["temperature_scale"] != ("absolute" if kind == "temperature" else "difference" if kind == "temperature-difference" else "not-applicable")):
                raise ValueError("science_numerical_native_units_unsupported_by_legacy")
    elif formula["equation_kind"] != "quantity-equation":
        raise ValueError("science_equation_kind_unknown")
    records = {r["id"]:r for r in sidecar["records"]}
    pending = [formula["condition_set_ref"], *formula["dependency_refs"]]; seen = set(); conditions = []
    while pending:
        ident = pending.pop()
        if ident in seen: continue
        seen.add(ident); record = records[ident]
        if record["kind"] == "FormulaCard": raise ValueError("science_multistep_execution_not_supported")
        if record["kind"] == "ConditionSet": conditions.append(record)
    if not conditions or not any("calculation" in c["required_for"] for c in conditions):
        raise ValueError("science_calculation_conditions_required")
    aliases = {(r["source"],r["name"]):r for r in specification["condition_bindings"]}
    declared = {(r["source"],r["name"]) for c in conditions for r in c["declared_inputs"]}
    if set(aliases) != declared or len(aliases) != len(specification["condition_bindings"]):
        raise ValueError("science_condition_input_bindings_incomplete")
    condition_values = {}
    for key, row in aliases.items():
        if row["source"] == "request":
            if row["property_symbol"] is not None or row["request_name"] != row["name"] or row["request_name"] not in facts:
                raise ValueError("science_condition_request_input_missing")
            value = facts[row["request_name"]]
        else:
            selected = next((r for r in inputs if r["symbol"] == row["property_symbol"] and r["source"]["kind"] == "property"),None)
            if row["request_name"] is not None or selected is None:
                raise ValueError("science_condition_property_input_missing")
            value = selected["normalized_quantity"]
        condition_values[":".join(key)] = units.canonical_quantity(value)
    results = []
    for condition in sorted(conditions,key=lambda r:r["id"]):
        normalized = copy.deepcopy(condition)
        for node in sc._walk(normalized["predicates"]):
            if node.get("kind") == "constant": node["value"] = units.canonical_quantity(node["value"])
        declared_keys = {r["source"]+":"+r["name"] for r in condition["declared_inputs"]}
        values = {k:v for k,v in condition_values.items() if k in declared_keys}
        assessment = sc.assess_conditions(normalized,values)
        results.append({"record_ref":record_ref(condition),"status":assessment["status"],
                        "normalized_predicates_sha256":sc.sha256_json(normalized["predicates"]),
                        "inputs":[{"source":k.split(":",1)[0],"name":k.split(":",1)[1],"value":v} for k,v in sorted(values.items())],
                        "results":assessment["condition_results"]})
    plan = {"schema_version":SCIENCE_CALCULATION_SCHEMA,"protocol":SCIENCE_CALCULATION_PROTOCOL,
            "request_id":request["request_id"],"request_sha256":sc.sha256_json(request),"specification_sha256":sc.sha256_json(specification),
            "base_package":package["fingerprint"],"sidecar_sha256":sidecar["sidecar_sha256"],"profile_sha256":sc.sha256_json(profile),
            "formula_card":record_ref(formula),"source_formula_ref":formula["source_object_ref"],"reviewed_ast_sha256":ast_ref["ast_sha256"],
            "input_bindings":inputs,"property_receipts":properties,"condition_results":results,
            "output_symbol_binding":record_ref(output_binding),"output_quantity_kind":output_binding["quantity_kind"],
            "output_frame":output_binding["coordinate_frame"]["value"],"tool_sha256s":tool_hashes(),
            "assumptions":[r["name"] for r in request["user_assumptions"]],
            "execution_authorized":False,"status":"planned-awaiting-independent-review-and-host-permission"}
    plan["plan_sha256"] = sc.sha256_json(plan)
    return plan


def freeze_calculation(plan, package, *, proposer_instance, reviewer_instances=None, review_session_id=None):
    """Answer-free exact-request review input using the existing high-risk gate."""
    if plan.get("plan_sha256") != sc.sha256_json({k:v for k,v in plan.items() if k != "plan_sha256"}):
        raise ValueError("science_calculation_plan_hash_drift")
    def identity(value):
        return isinstance(value,str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}",value) is not None
    reviewers = reviewer_instances or []
    if (not identity(proposer_instance) or not isinstance(reviewers,list) or len(reviewers)>8
            or any(not identity(r) for r in reviewers) or len(set(reviewers)) != len(reviewers) or proposer_instance in reviewers):
        raise ValueError("science_calculation_reviewer_separation_invalid")
    required = sw._risk_tier("object",{},"applicability")
    if (reviewers or review_session_id) and (len(reviewers)<required or not identity(review_session_id)):
        raise ValueError("science_calculation_explicit_reviewers_and_session_required")
    sources = package["manifest"]["sources"]
    if len(sources) != 1:
        raise ValueError("science_calculation_single_source_review_required")
    source_hash = sources[0]["sha256"]
    item = "calculation:" + plan["request_id"]
    formula_source = sc._object(plan["source_formula_ref"],package)
    evidence_ids = set(formula_source["evidence_ids"])
    for selection in plan["property_receipts"]:
        for reference in selection["source_evidence_refs"]:
            evidence_ids.add(sc._evidence(reference,package)["id"])
    focused_spans = [span for anchor in package["anchors"] if anchor["id"] in evidence_ids for span in anchor.get("text_spans",[])]
    assertions = []
    fields = ("request_sha256","specification_sha256","formula_card","reviewed_ast_sha256","input_bindings",
              "property_receipts","condition_results","output_symbol_binding","output_quantity_kind","output_frame","assumptions","tool_sha256s")
    for field in fields:
        row = {"parent_item_ref":item,"field":field,"value":plan[field],"risk_tier":required,
               "evidence_spans":copy.deepcopy(focused_spans),
               "visual_task_ids":[],"formula_node_paths":[field]}
        row["assertion_id"]="calculation-field-"+sc.sha256_json(row)[:24]
        row["assertion_sha256"]=sc.sha256_json(row); assertions.append(row)
    queue = [{"item_ref":item,"item_kind":"object","unit_id":plan["source_formula_ref"]["object_id"],
              "proposer_instance":proposer_instance,"item_sha256":plan["plan_sha256"],"dependency_refs":[]}]
    workpack = "calculation-workpack-"+plan["plan_sha256"][:24]
    queue,_,bundle_hash = sw.enrich_review_material(queue,[],assertions,[],source_sha256=source_hash,workpack_id=workpack)
    review_plan = None if not reviewers else {"schema_version":SCIENCE_CALCULATION_REVIEW_SCHEMA,"source_sha256":source_hash,
        "workpack_id":workpack,"bundle_sha256":bundle_hash,"reviewer_instances":sorted(reviewers),"review_session_id":review_session_id,
        "proposer_instances":[proposer_instance],"calculation_plan_sha256":plan["plan_sha256"],
        "attestation_level":"host-orchestrator-recorded-not-cryptographic"}
    frozen = {"schema_version":SCIENCE_CALCULATION_REVIEW_SCHEMA,"status":"paused-independent-review",
        "plan_sha256":plan["plan_sha256"],"proposer_instance":proposer_instance,"reviewer_instances":sorted(reviewers),
        "review_session_id":review_session_id,"assertions":assertions,"queue":queue,"review_plan":review_plan,"execution_authorized":False}
    frozen["frozen_sha256"]=sc.sha256_json(frozen)
    return frozen


def validate_calculation_review(frozen, attestations, plan, package):
    if frozen is None:
        return {"status":"blocked","reason_codes":["calculation_independent_review_required"]}
    expected = freeze_calculation(plan,package,proposer_instance=frozen.get("proposer_instance"),
        reviewer_instances=frozen.get("reviewer_instances"),review_session_id=frozen.get("review_session_id"))
    if frozen != expected:
        raise ValueError("science_calculation_frozen_review_stale")
    if not isinstance(attestations,list):
        raise ValueError("science_calculation_external_attestations_required")
    sc.canonical_json(attestations)
    if frozen["review_plan"] is None:
        return {"status":"blocked","reason_codes":["calculation_external_reviewer_session_required"]}
    issues, leads, _ = sw.validate_attestations(attestations,frozen["queue"],frozen["review_plan"])
    reasons = [i.code for i in issues]
    if not leads or any(r.get("verdict") != "accepted" for r in leads.values()):
        reasons.append("calculation_review_not_accepted")
    return {"status":"blocked" if reasons else "review-bindings-valid","reason_codes":sorted(set(reasons))}


def _prepare(package_path, specification, request, sidecar, profile):
    package = sw.load_package(package_path)
    if (package["manifest"]["package"]["status"] != "ready"
            or package["manifest"]["capabilities"] != {"reference":True,"decision_support":False,"executable":True}):
        raise ValueError("science_existing_sealed_execution_package_required")
    plan = build_plan(specification,request,package,sidecar,profile)
    root = Path(package_path).expanduser().absolute()
    bundle = sc.load_json(sw._safe_path(root,"references/execution/manifest.json"))
    policy = sc.load_json(sw._safe_path(root,"references/execution/policy.json"))
    selected = next(row for row in bundle["formulas"] if row["object_id"] == plan["source_formula_ref"]["object_id"])
    module = sw._safe_path(root,selected["entrypoint"])
    runner = sw._safe_path(root,bundle["runner"]["entrypoint"])
    ast = sc._object(plan["source_formula_ref"],package)["formula"]["ast"]
    contract = legacy.validate_formula_ast(ast)
    if (module.read_text() != legacy.render_formula_module(contract)
            or runner.read_bytes() != (sc.ROOT/"formula_sandbox_runner.py").read_bytes()):
        raise ValueError("science_canonical_generated_surface_required")
    return {"package":package,"plan":plan,"ast":ast,"policy":policy,"bundle":bundle,"selected":selected,"runner":runner}


def _permission(permission, plan, allow_execution, now=None):
    if permission is None or allow_execution is not True:
        raise ValueError("science_explicit_host_execution_permission_required")
    shape(permission,"permission")
    if permission["plan_sha256"] != plan["plan_sha256"]:
        raise ValueError("science_host_permission_scope_drift")
    try:
        issued = datetime.fromisoformat(permission["issued_at"])
        expiry = datetime.fromisoformat(permission["expires_at"])
        current = now or datetime.now(timezone.utc)
        if issued.tzinfo is None or expiry.tzinfo is None or current.tzinfo is None:
            raise ValueError("timezone-required")
        if not issued <= current < expiry or (expiry-issued).total_seconds()>3600:
            raise ValueError("invalid-interval")
    except (ValueError,TypeError) as error:
        raise ValueError("science_host_permission_expired_or_invalid_time") from error


def _gates(context, sidecar, profile, *, science_frozen=None, science_attestations=None,
           calculation_frozen=None, calculation_attestations=None, permission=None, allow_execution=False):
    plan, package = context["plan"],context["package"]
    reasons=[]; checks=[]; refs=[]
    def check(name, function, artifact):
        try:
            result=function()
            if isinstance(result,dict) and result.get("status") == "blocked":
                reasons.extend(result["reason_codes"]); checks.append({"gate":name,"status":"blocked"}); return
            checks.append({"gate":name,"status":"passed"}); refs.append({"gate":name,"sha256":sc.sha256_json(artifact)})
        except (ValueError,KeyError,TypeError) as error:
            code = str(error).split(":",1)[0]
            reasons.append(code if re.fullmatch(r"[a-z][a-z0-9_]{1,100}",code) else "science_gate_input_invalid")
            checks.append({"gate":name,"status":"blocked"})
    def provenance():
        if sidecar["data_origin"] != "source-backed-proposal":
            raise ValueError("science_synthetic_data_cannot_authorize_production")
        if any(row["source_category"] in {"assumed","illustrative","computed","fitted"} for row in plan["property_receipts"]):
            raise ValueError("science_property_execution_provenance_unsupported")
    check("source_provenance",provenance,sidecar["sidecar_sha256"])
    def review():
        if science_frozen is None: raise ValueError("science_independent_review_required")
        return sw.validate_review(science_frozen,sidecar,package,profile,science_attestations or [])
    check("science_review",review,{"frozen":science_frozen,"attestations":science_attestations})
    check("calculation_review",lambda:validate_calculation_review(calculation_frozen,calculation_attestations or [],plan,package),
          {"frozen":calculation_frozen,"attestations":calculation_attestations})
    def conditions():
        if not plan["condition_results"] or any(row["status"] != "satisfied" for row in plan["condition_results"]):
            raise ValueError("science_calculation_conditions_not_satisfied")
    check("conditions",conditions,plan["condition_results"])
    check("host_permission",lambda:_permission(permission,plan,allow_execution),permission)
    refs.insert(0,{"gate":"original_publish_seal_tests_execution","sha256":sc.sha256_json(context["bundle"])})
    return {"status":"blocked" if reasons else "authorized-for-this-request","reason_codes":sorted(set(reasons)),
            "checks":[{"gate":"original_publish_seal_tests_execution","status":"passed"},*checks],
            "execution_gate_refs":refs,"execution_authorized":not reasons,
            "production_knowledge_publication":"blocked","domain_accuracy":"blocked"}


def check_calculation(package_path,specification,request,sidecar,profile,**review_inputs):
    context = _prepare(package_path,specification,request,sidecar,profile)
    return {"plan":context["plan"],**_gates(context,sidecar,profile,**review_inputs)}


def _invoke(context, inputs):
    with tempfile.TemporaryDirectory(prefix="science-calculation-") as directory:
        process = subprocess.run([sys.executable,"-I","-S","-B",str(context["runner"]),"--formula",context["selected"]["object_id"]],
            input=json.dumps({"inputs":inputs},ensure_ascii=False,sort_keys=True,separators=(",",":")),
            text=True,capture_output=True,cwd=directory,env={"PYTHONDONTWRITEBYTECODE":"1","PYTHONHASHSEED":"0"},
            timeout=context["policy"]["timeout_seconds"]+2,check=False)
    if process.returncode != 0 or len(process.stdout.encode())>65536:
        raise ValueError("science_legacy_runner_failed")
    response=json.loads(process.stdout)
    if not isinstance(response,dict) or set(response)!={"status","result"} or response["status"]!="ok":
        raise ValueError("science_legacy_runner_response_invalid")
    return response


def _receipt(context, gates, result, response):
    plan=context["plan"]
    record={"schema_version":SCIENCE_CALCULATION_RECEIPT_SCHEMA,"protocol":SCIENCE_CALCULATION_PROTOCOL,
        "compiler_version":SCIENCE_CALCULATION_COMPILER_VERSION,"status":"completed",
        **{k:copy.deepcopy(plan[k]) for k in ("request_id","request_sha256","plan_sha256","base_package","sidecar_sha256",
            "source_formula_ref","formula_card","reviewed_ast_sha256","input_bindings","property_receipts","condition_results",
            "tool_sha256s","output_quantity_kind","output_frame","assumptions")},
        "engine_version":EXECUTION_COMPILER_VERSION,"engine_sha256":context["selected"]["module_sha256"],
        "runner_sha256":context["bundle"]["runner"]["sha256"],"execution_gate_refs":gates["execution_gate_refs"],
        "result":{**result,"dimension":{key:result["dimension"].get(key,0) for key in legacy.DIMENSION_KEYS}},
        "runner_response_sha256":sc.sha256_json(response),"warnings":["conditional-calculation-not-measurement","not-scientific-truth-or-publication-approval"],
        "uncertainty_status":"unavailable","uncertainty_reason":"output-propagation-not-implemented-no-independence-assumed",
        "isolation":"process-resource-limits-not-kernel-sandbox","knowledge_verified":False,
        "production_knowledge_publication":"blocked","domain_accuracy":"blocked","execution_authorized":True}
    if any(row["value_qualifier"]=="typical" for row in plan["property_receipts"]):
        record["warnings"].append("typical-value-not-guaranteed-bound")
    record["receipt_sha256"]=sc.sha256_json(record)
    shape(record,"receipt")
    return record


def run_calculation(package_path,specification,request,sidecar,profile,**review_inputs):
    context=_prepare(package_path,specification,request,sidecar,profile)
    gates=_gates(context,sidecar,profile,**review_inputs)
    if not gates["execution_authorized"]:
        return {**gates,"plan_sha256":context["plan"]["plan_sha256"],"receipt":None}
    inputs={row["symbol"]:{"value":row["decimal_value"],"unit":row["normalized_quantity"]["unit"]} for row in context["plan"]["input_bindings"]}
    expected=numeric_preflight(context["ast"],inputs)
    response=_invoke(context,inputs)
    if response["result"]!=expected:
        raise ValueError("science_interpreter_generated_result_mismatch")
    return _receipt(context,gates,expected,response)


def validate_receipt(receipt,package_path,specification,request,sidecar,profile,**review_inputs):
    shape(receipt,"receipt")
    context=_prepare(package_path,specification,request,sidecar,profile)
    gates=_gates(context,sidecar,profile,**review_inputs)
    if not gates["execution_authorized"]:
        return {**gates,"receipt_status":"current-gates-blocked"}
    inputs={r["symbol"]:{"value":r["decimal_value"],"unit":r["normalized_quantity"]["unit"]} for r in context["plan"]["input_bindings"]}
    result=numeric_preflight(context["ast"],inputs)
    expected=_receipt(context,gates,result,{"status":"ok","result":result})
    if receipt!=expected:
        raise ValueError("science_calculation_receipt_stale_or_modified")
    return {"status":"receipt-bindings-and-numeric-replay-valid","knowledge_verified":False,
            "production_knowledge_publication":"blocked","domain_accuracy":"blocked",
            "attestation_level":"host-recorded-not-cryptographic-proof-of-process"}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("plan","freeze","check","run","validate-receipt"))
    for flag in ("package","specification","request","sidecar","profile"):
        parser.add_argument("--"+flag,type=Path,required=True)
    for flag in ("science-frozen","science-attestations","calculation-frozen","calculation-attestations","permission","receipt","output"):
        parser.add_argument("--"+flag,type=Path)
    parser.add_argument("--allow-execution",action="store_true")
    parser.add_argument("--proposer-instance"); parser.add_argument("--reviewer-instance",action="append")
    parser.add_argument("--review-session-id")
    args=parser.parse_args(argv)
    try:
        # Reject invalid destinations before any permitted process invocation.
        if args.output:
            if args.output.resolve().is_relative_to(args.package.resolve()):
                raise ValueError("science_output_inside_base_forbidden")
            if args.output.exists() or any(p.is_symlink() for p in [args.output,*args.output.parents]) or not args.output.parent.is_dir():
                raise ValueError("science_output_exists_or_path_invalid")
        inputs=(args.package,sc.load_json(args.specification),sc.load_json(args.request),sc.load_json(args.sidecar),sc.load_json(args.profile))
        review_inputs={key:sc.load_json(getattr(args,key)) if getattr(args,key) else None for key in
            ("science_frozen","science_attestations","calculation_frozen","calculation_attestations","permission")}
        review_inputs["allow_execution"]=args.allow_execution
        if args.command in {"plan","freeze"}:
            context=_prepare(*inputs); result=context["plan"]
            if args.command=="freeze": result=freeze_calculation(result,context["package"],proposer_instance=args.proposer_instance,
                reviewer_instances=args.reviewer_instance,review_session_id=args.review_session_id)
        elif args.command=="check": result=check_calculation(*inputs,**review_inputs)
        elif args.command=="run": result=run_calculation(*inputs,**review_inputs)
        else: result=validate_receipt(sc.load_json(args.receipt),*inputs,**review_inputs)
        if args.output:
            sw._write_new(args.output,result)
        print(sc.canonical_json(result).decode())
        return 2 if result.get("status")=="blocked" else 0
    except (ValueError,OSError,KeyError,TypeError,AttributeError,StopIteration,subprocess.TimeoutExpired) as error:
        code=str(error).split(":",1)[0]
        code=code if re.fullmatch(r"[a-z][a-z0-9_]{1,100}",code) else "science_calculation_input_or_runtime_invalid"
        print(json.dumps({"status":"blocked","reason_codes":[code],"receipt":None,"execution_authorized":False,
                          "production_knowledge_publication":"blocked","domain_accuracy":"blocked"}))
        return 2


if __name__=="__main__":
    raise SystemExit(main())
