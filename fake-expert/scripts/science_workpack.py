#!/usr/bin/env python3
"""Answer-free science proposal validation/freeze; never promotion, Gold or execution."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import os

import science_contract as sc
from compiler_version import SCIENCE_REVIEW_SCHEMA, SCIENCE_COMPILER_VERSION
from semantic_assurance import _risk_tier, enrich_review_material, validate_attestations
from compiler_version import (SCIENCE_LIFECYCLE_SCHEMA, SCIENCE_DOMAIN_PROFILE_SCHEMA,
    SCIENCE_LIFECYCLE_COMPILER_VERSION, SCIENCE_LIFECYCLE_PROTOCOL, SCIENCE_LINK_PROPOSALS_SCHEMA,
    SCIENCE_LINKS_SCHEMA, SCIENCE_EVALUATION_SUITE_SCHEMA, SCIENCE_EVALUATION_RESULTS_SCHEMA)


def _safe_path(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if (not relative or rel.is_absolute() or ".." in rel.parts or str(rel) != relative or "\\" in relative):
        raise ValueError("science_package_path_unsafe")
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("science_package_symlink")
    if not current.is_file():
        raise ValueError("science_package_file_missing")
    return current


def load_package(path: Path) -> dict:
    """Verify a supplied base locally through the original validator, without source inference."""
    path = path.expanduser().absolute()
    if any(p.is_symlink() for p in [path, *path.parents]) or not path.is_dir():
        raise ValueError("science_package_root_unsafe")
    manifest_path = _safe_path(path, "manifest.json")
    manifest = sc.load_json(manifest_path)
    if manifest.get("schema_version") != "tkc.expert-skill/v0.1":
        raise ValueError("science_base_schema_unsupported")
    files = manifest.get("integrity", {}).get("files")
    if not isinstance(files, dict) or not files or len(files) > 4096:
        raise ValueError("science_base_integrity_required")
    for relative, digest in files.items():
        file = _safe_path(path, relative)
        if file.stat().st_size > 16 * sc.MAX_BYTES:
            raise ValueError("science_package_file_limit")
        if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise ValueError("science_base_integrity_drift")
    actual = {str(p.relative_to(path)) for p in path.rglob("*") if p.is_file()}
    if actual != set(files) | {"manifest.json"}:
        raise ValueError("science_base_untracked_files")
    # No duplicate implementation of the old schema, semantic, seal or execution gates.
    from expert_skill_contract import validate_expert_skill
    level = "publish" if manifest.get("capabilities", {}).get("executable") is True else "structure"
    errors = [issue for issue in validate_expert_skill(path, level=level) if issue.severity == "error"]
    if errors:
        raise ValueError("science_base_contract_invalid:" + errors[0].code)
    index = sc.load_json(_safe_path(path, "references/knowledge/index.json"))
    objects = []
    for row in index["objects"]:
        relative = row["path"]
        if relative not in files or not relative.startswith("references/knowledge/objects/"):
            raise ValueError("science_object_path_unbound")
        value = sc.load_json(_safe_path(path, relative))
        if row["id"] != value["id"]:
            raise ValueError("science_object_index_mismatch")
        objects.append(value)
    anchors_path = _safe_path(path, "references/evidence/anchors.jsonl")
    if anchors_path.stat().st_size > sc.MAX_BYTES:
        raise ValueError("science_evidence_file_limit")
    anchors = [json.loads(line) for line in anchors_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    sc.canonical_json(anchors)
    if len({o["id"] for o in objects}) != len(objects) or len({a["id"] for a in anchors}) != len(anchors):
        raise ValueError("science_base_duplicate_ids")
    ast_bindings = {}
    if manifest.get("capabilities", {}).get("executable") is True:
        bundle = sc.load_json(_safe_path(path, "references/execution/manifest.json"))
        ast_bindings = {row["object_id"]: row["formula_ast_sha256"] for row in bundle["formulas"]}
    return {"fingerprint": {"package_id": manifest["package"]["id"], "package_version": manifest["package"]["version"],
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(), "content_sha256": sc.sha256_json(files)},
            "manifest": manifest, "objects": objects, "anchors": anchors, "execution_ast_bindings": ast_bindings}


def freeze_proposal(sidecar: dict, package: dict, profile: dict, *, proposer_instance: str,
                    reviewer_instances: list[str] | None = None, review_session_id: str | None = None) -> dict:
    sc.validate_sidecar(sidecar, package, profile)
    def identity(value):
        return isinstance(value, str) and value.strip() == value and 0 < len(value) <= 256
    if not identity(proposer_instance):
        raise ValueError("science_explicit_proposer_required")
    reviewers = reviewer_instances or []
    if (not isinstance(reviewers, list) or any(not identity(r) for r in reviewers)
            or len(set(reviewers)) != len(reviewers) or proposer_instance in reviewers):
        raise ValueError("science_reviewer_identity_or_separation_invalid")
    required = _risk_tier("object", {}, "applicability")
    if (reviewers or review_session_id) and (len(reviewers) < required or not identity(review_session_id)):
        raise ValueError("science_explicit_reviewers_and_session_required")
    sources = package["manifest"].get("sources", [])
    if len(sources) != 1 or re.fullmatch(r"[0-9a-f]{64}", sources[0].get("sha256", "")) is None:
        raise ValueError("science_m1_single_source_review_required")
    source_hash = sources[0]["sha256"]
    workpack_id = "science-workpack-" + sidecar["sidecar_sha256"][:24]
    assertions, queue = [], []
    for record in sidecar["records"]:
        item_ref = "science:" + record["id"]
        dependencies = sorted({ref for p in record["provenance"] for ref in p["derivation_refs"]})
        for provenance in record["provenance"]:
            field = provenance["field"]
            evidence = [sc._evidence(ref, package) for ref in provenance["evidence_refs"]]
            assertion = {"parent_item_ref": item_ref, "field": field, "value": record[field], "provenance": provenance,
                         "risk_tier": required, "evidence_spans": [span for a in evidence for span in a.get("text_spans", [])],
                         "visual_task_ids": [], "formula_node_paths": [field] if record["kind"] == "FormulaCard" else []}
            assertion["assertion_id"] = "science-field-" + sc.sha256_json(assertion)[:24]
            assertion["assertion_sha256"] = sc.sha256_json(assertion)
            assertions.append(assertion)
        queue.append({"item_ref": item_ref, "item_kind": "object", "unit_id": record["source_object_ref"]["object_id"],
                      "proposer_instance": proposer_instance, "item_sha256": record["content_sha256"], "dependency_refs": dependencies})
    queue, _, bundle_hash = enrich_review_material(queue, [], assertions, [], source_sha256=source_hash, workpack_id=workpack_id)
    plan = None
    if reviewers:
        plan = {"schema_version": SCIENCE_REVIEW_SCHEMA, "source_sha256": source_hash, "workpack_id": workpack_id,
                "bundle_sha256": bundle_hash, "reviewer_instances": sorted(reviewers), "review_session_id": review_session_id,
                "proposer_instances": [proposer_instance], "sidecar_sha256": sidecar["sidecar_sha256"],
                "base_package": sidecar["base_package"], "profile_sha256": sidecar["profile_sha256"],
                "tool_sha256s": sidecar["tool_sha256s"], "attestation_level": "host-orchestrator-recorded-not-cryptographic"}
    frozen = {"schema_version": SCIENCE_REVIEW_SCHEMA, "compiler_version": SCIENCE_COMPILER_VERSION,
              "status": "paused-independent-review", "sidecar_sha256": sidecar["sidecar_sha256"],
              "base_package": sidecar["base_package"], "profile_sha256": sidecar["profile_sha256"],
              "tool_sha256s": sidecar["tool_sha256s"], "proposer_instance": proposer_instance,
              "reviewer_instances": sorted(reviewers), "review_session_id": review_session_id,
              "assertions": assertions, "queue": queue, "review_plan": plan,
              "production_eligible": False, "knowledge_verified": False, "executable": False}
    frozen["frozen_sha256"] = sc.sha256_json(frozen)
    return frozen


def validate_frozen(frozen: dict, sidecar: dict, package: dict, profile: dict) -> dict:
    sc.canonical_json(frozen)
    expected = freeze_proposal(sidecar, package, profile, proposer_instance=frozen.get("proposer_instance"),
                               reviewer_instances=frozen.get("reviewer_instances"), review_session_id=frozen.get("review_session_id"))
    if frozen != expected:
        raise ValueError("science_frozen_review_stale_or_drift")
    return {"status": "frozen-inputs-valid", "knowledge_verified": False, "executable": False}


def validate_review(frozen: dict, sidecar: dict, package: dict, profile: dict, attestations: list[dict]) -> dict:
    validate_frozen(frozen, sidecar, package, profile)
    sc.canonical_json(attestations)
    if not isinstance(attestations, list):
        raise ValueError("science_external_receipt_list_required")
    reasons = []
    if frozen["review_plan"] is None:
        reasons.append("external_reviewer_session_required")
    else:
        issues, leads, _ = validate_attestations(attestations, frozen["queue"], frozen["review_plan"])
        reasons += [issue.code for issue in issues]
        if any(row.get("verdict") != "accepted" for row in leads.values()):
            reasons.append("independent_review_not_accepted")
    return {"status": "blocked" if reasons else "review-bindings-valid",
            "reason_codes": sorted(set(reasons)), "attestation_count": len(attestations),
            "production_eligible": False, "knowledge_verified": False, "executable": False,
            "domain_accuracy": "blocked", "attestation_level": "host-orchestrator-recorded-not-cryptographic"}


def _load_sidecar(path: Path):
    if path.is_symlink():
        raise ValueError("science_sidecar_symlink")
    return sc.load_json(path / "science-sidecar.json" if path.is_dir() else path)


def _write_new(path: Path, value: dict):
    """No overwrite, and serialization/validation happens before any write."""
    data = sc.canonical_json(value) + b"\n"
    path = path.expanduser().absolute()
    if any(p.is_symlink() for p in [path, *path.parents]) or not path.parent.is_dir():
        raise ValueError("science_output_path_unsafe")
    # Link a complete private temporary file into place. link() never replaces a
    # destination; interruption/failure cannot leave a half-written frozen file.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".science-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def lifecycle_shape(value, definition):
    import consultation_contract as cc
    sc.canonical_json(value)
    schema = sc.load_json(sc.ROOT.parent / "assets/schemas/science-lifecycle.schema.json")
    identities = {"domain_profile":SCIENCE_DOMAIN_PROFILE_SCHEMA,"link_proposals":SCIENCE_LINK_PROPOSALS_SCHEMA,
                  "evaluation_suite":SCIENCE_EVALUATION_SUITE_SCHEMA,"evaluation_results":SCIENCE_EVALUATION_RESULTS_SCHEMA}
    if schema["$id"] != SCIENCE_LIFECYCLE_SCHEMA or any(schema["$defs"][k]["properties"]["schema_version"]["const"] != v for k,v in identities.items()):
        raise ValueError("science_lifecycle_registry_drift")
    for definitions in (sc._schema()["$defs"],sc.load_json(cc.SCHEMA_PATH)["$defs"]):
        if set(definitions) & set(schema["$defs"]): raise ValueError("science_lifecycle_definition_collision")
        schema["$defs"].update(definitions)
    for row in sc._walk(schema):
        if row.get("$ref","").startswith(("science-sidecar.schema.json#", "science-consultation.schema.json#")):
            row["$ref"] = row["$ref"].split(".json",1)[1]
    sc._check(value,schema["$defs"][definition],schema)


def domain_inputs(value):
    lifecycle_shape(value,"domain_profile")
    if value["protocol"] != SCIENCE_LIFECYCLE_PROTOCOL or value["domain_id"] != value["science_profile"]["profile_id"]:
        raise ValueError("science_domain_identity_mismatch")
    if len(set(value["quantity_kinds"])) != len(value["quantity_kinds"]): raise ValueError("science_domain_duplicate_quantity")
    if any(r["quantity_kind"] not in value["quantity_kinds"] for r in value["symbol_hints"]): raise ValueError("science_domain_unbound_quantity")
    if len({r["case_id"] for r in value["question_catalog"]}) != len(value["question_catalog"]): raise ValueError("science_domain_duplicate_question")
    return {"profile":copy.deepcopy(value["science_profile"]),"tokenizer":copy.deepcopy(value["tokenizer"]),
            "routing":{"domain_sha256":sc.sha256_json(value),"calculation_policy":value["calculation_policy"],
                       "symbol_hints":copy.deepcopy(value["symbol_hints"]),"execution_authorized":False}}


def lifecycle_tool_hashes():
    paths=[sc.ROOT/name for name in ("science_workpack.py","science_acceptance.py","pipeline_orchestrator.py",
        "incremental_build_dag.py","semantic_composer.py","compiler_version.py")]
    paths += [sc.ROOT.parent/"assets/schemas/science-lifecycle.schema.json"]
    return {str(p.relative_to(sc.ROOT.parent)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def _external_registry(proposer, reviewers, session):
    reviewers=reviewers or []
    valid=lambda x:isinstance(x,str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}",x) is not None
    if not valid(proposer) or not isinstance(reviewers,list) or len(reviewers)>8 or any(not valid(r) for r in reviewers) or len(set(reviewers))!=len(reviewers) or proposer in reviewers:
        raise ValueError("science_external_reviewer_separation_invalid")
    if (reviewers or session) and (len(reviewers)<_risk_tier("object",{},"applicability") or not valid(session)):
        raise ValueError("science_explicit_reviewers_and_session_required")
    return sorted(reviewers)


def _composer_plan(*,workpack_id,items,sources,proposer,reviewers,session,created_at):
    import semantic_composer as composer
    composer._ensure_rfc3339(created_at)
    if not reviewers:return None
    plan=composer._build_review_plan(workpack_id=workpack_id,created_at=created_at,source_shas=sources,items=items,reviewer_instances=reviewers)
    # The operator-supplied identity replaces the legacy compiler-default proposer.
    # Session is bound by the workpack/candidate hashes, not a new legacy field.
    plan["proposer_instances"]=[proposer]
    plan["review_plan_sha256"]=composer.row_hash(plan,"review_plan_sha256")
    return plan


def _composer_review(plan, attestations):
    import semantic_composer as composer
    if plan is None:return {"status":"blocked","reason_codes":["external_reviewer_session_required"]}
    sc.canonical_json(attestations)
    if not isinstance(attestations,list) or len(attestations)>1024: raise ValueError("science_external_attestations_invalid")
    schema=sc.load_json(sc.ROOT.parent/"assets/schemas/semantic-composer-review-attestation.schema.json")
    for row in attestations:sc._check(row,schema,schema)
    issues=[]; result=composer._validate_attestations(plan,attestations,True,issues)
    reasons=[r.code for r in issues]
    if not plan["items"] or not attestations or any(r["verdict"]!="accepted" for r in attestations):reasons.append("independent_review_not_accepted")
    return {"status":"blocked" if reasons else "external-review-bindings-valid","reason_codes":sorted(set(reasons)),"counts":result}


def _link_endpoint(endpoint, library):
    lifecycle_shape(endpoint,"link_endpoint")
    found=[r for r in library if r["package"]["fingerprint"]==endpoint["base_package"]]
    if len(found)!=1 or found[0]["sidecar"] is None:raise ValueError("science_link_package_or_sidecar_missing")
    entry=found[0]; ref=endpoint["record_ref"]
    records=[r for r in entry["sidecar"]["records"] if r["id"]==ref["id"] and r["content_sha256"]==ref["sha256"]]
    if len(records)!=1 or records[0]["review_state"] in {"rejected","quarantined"}:raise ValueError("science_link_record_stale_or_rejected")
    return entry,records[0]


def _comparison_gaps(left,right,left_entry,right_entry):
    import science_units as units
    if left["kind"]!=right["kind"]:return ["record_kind_mismatch"]
    kind=left["kind"]; reasons=[]
    def equal_known(a,b,label):
        if any(sc._unknown(r) for value in (a,b) for r in ([value] + list(sc._walk(value)))):reasons.append(label+"_unknown")
        elif a!=b:reasons.append(label+"_mismatch")
    def conditions(rows):
        return {r["name"]:units.canonical_quantity(r["value"]) for r in rows}
    if kind=="SymbolBinding":
        for key in ("quantity_kind","canonical_quantity_id","dimension","value_type","shape","coordinate_frame","value_domain"):
            equal_known(left[key],right[key],key)
        if left["value_type"]=="unknown" or right["value_type"]=="unknown":reasons.append("symbol_type_unknown")
        if sc._unknown(left["definition_ref"]) or sc._unknown(right["definition_ref"]):reasons.append("definition_unknown")
        if sc._unknown(left["scope_ref"]) or sc._unknown(right["scope_ref"]):reasons.append("scope_unknown")
        if sc._unknown(left["unit"]) or sc._unknown(right["unit"]):reasons.append("unit_unknown")
        if left["value_domain"]=="unknown" or right["value_domain"]=="unknown":reasons.append("value_domain_unknown")
    elif kind=="PropertyObservation":
        equal_known(left["property_quantity_id"],right["property_quantity_id"],"quantity_kind")
        if left["unit"]!=right["unit"]:reasons.append("property_unit_conversion_review_required")
        for row in (left,right):
            if sc._unknown(row["material_state_ref"]):reasons.append("material_state_unknown")
        if not reasons:
            materials=[]
            for row,entry in ((left,left_entry),(right,right_entry)):
                materials.append(next(r for r in entry["sidecar"]["records"] if r["id"]==row["material_state_ref"]))
            for key in ("material_identity","sample_identity","orientation_frame","composition","processing_history","phase_microstructure"):
                equal_known(materials[0][key],materials[1][key],key)
            try:
                equal_known(conditions(materials[0]["conditions"]),conditions(materials[1]["conditions"]),"state_conditions")
                equal_known(conditions(left["measurement_conditions"]),conditions(right["measurement_conditions"]),"measurement_conditions")
                required=set(left_entry["profile"]["required_measurement_conditions"])|set(right_entry["profile"]["required_measurement_conditions"])
                for observation in (left,right):
                    actual=conditions(observation["measurement_conditions"])
                    if any(name not in actual or sc._unknown(actual[name]) for name in required):reasons.append("measurement_condition_unknown")
            except ValueError:reasons.append("measurement_condition_unsupported")
        equal_known(left["method_ref"],right["method_ref"],"method_reference")
    elif kind=="FormulaCard":
        for key in ("equation_kind","formula_role"):equal_known(left[key],right[key],key)
        if left["equation_kind"]=="unknown" or right["equation_kind"]=="unknown":reasons.append("equation_kind_unknown")
        for row in (left,right):
            if sc._unknown(row["condition_set_ref"]):reasons.append("formula_conditions_unknown")
        if not reasons:
            values=[next(r for r in e["sidecar"]["records"] if r["id"]==f["condition_set_ref"])["predicates"] for f,e in ((left,left_entry),(right,right_entry))]
            equal_known(values[0],values[1],"formula_conditions")
        contexts=[]
        for formula,entry in ((left,left_entry),(right,right_entry)):
            bindings=[r for r in entry["sidecar"]["records"] if r["id"] in formula["symbol_bindings"]]
            if not bindings:reasons.append("formula_symbol_context_missing")
            for binding in bindings:
                reasons.extend(_comparison_gaps(binding,binding,entry,entry))
            contexts.append(sorted(sc.canonical_json({key:r[key] for key in
                ("canonical_quantity_id","quantity_kind","dimension","value_type","shape","coordinate_frame","value_domain")}).decode() for r in bindings))
        if contexts[0]!=contexts[1]:reasons.append("formula_symbol_context_mismatch")
        if left["equation_kind"]=="numerical-value-equation":
            equal_known(left["unit_convention"],right["unit_convention"],"formula_unit_convention")
    elif kind=="ConditionSet":equal_known(left["predicates"],right["predicates"],"conditions")
    else:
        for key in ("material_identity","sample_identity","orientation_frame","composition","processing_history","phase_microstructure","conditions"):
            equal_known(left[key],right[key],key)
    return sorted(set(reasons))


def build_cross_links(library, proposals, *, proposer_instance, reviewer_instances=None, review_session_id=None, created_at):
    import science_index as index
    import semantic_composer as composer
    index.validate_library(library); lifecycle_shape(proposals,"link_proposals")
    reviewers=_external_registry(proposer_instance,reviewer_instances,review_session_id)
    composer._ensure_rfc3339(created_at)
    tools=lifecycle_tool_hashes(); alignments=[]; endpoints={}; deferred=[]; pairs=set(); sources=set()
    for pair in proposals["pairs"]:
        key=tuple(sorted((sc.sha256_json(pair["left"]),sc.sha256_json(pair["right"]))))
        if key in pairs or key[0]==key[1]:raise ValueError("science_link_duplicate_or_self_pair")
        pairs.add(key)
        if pair["left"]["base_package"]["package_id"]==pair["right"]["base_package"]["package_id"]:raise ValueError("science_cross_source_packages_required")
        le,left=_link_endpoint(pair["left"],library); re_,right=_link_endpoint(pair["right"],library)
        gaps=_comparison_gaps(left,right,le,re_)
        if gaps:
            deferred.append({"pair":copy.deepcopy(pair),"reason_codes":gaps,"classification":"not-comparable-not-scientific-contradiction"}); continue
        refs=[]
        for side,row,entry,role in (("left",left,le,"reference-package"),("right",right,re_,"candidate-workpack")):
            source_shas={r["sha256"] for r in entry["package"]["manifest"]["sources"]}
            if len(source_shas)!=1:raise ValueError("science_link_single_source_package_required")
            source_hash=next(iter(source_shas)); sources.add(source_hash)
            endpoint=pair[side]; ident="science-link-ref-"+sc.sha256_json(endpoint)[:24]
            endpoints[ident]={"concept_ref_id":ident,"endpoint":copy.deepcopy(endpoint),"record":copy.deepcopy(row),
                              "source_evidence_refs":row["source_evidence_refs"],"data_origin":entry["sidecar"]["data_origin"]}
            refs.append({"concept_ref_id":ident,"concept_ref_sha256":sc.sha256_json(endpoints[ident]),"source_role":role,
                         "source_sha256":source_hash,"package_id":endpoint["base_package"]["package_id"],"local_ref":row["id"],
                         "workpack_id":proposals["proposal_id"],"symbols":[row["original_glyph"]] if row["kind"]=="SymbolBinding" else [],
                         "evidence_binding_ids":[sc.sha256_json(r) for r in row["source_evidence_refs"]],"normalized_terms":[],"alias_groups":[]})
        result=composer._alignment_rows(refs,differences=[],conflicts=[],unit_maps=[],explicit_relations={(refs[0]["concept_ref_id"],refs[1]["concept_ref_id"]):pair["relation"]})
        for row in result:
            # Bind the complete science context/tools/session into the candidate
            # through the existing evidence-binding list; this is not a verdict.
            row["evidence_binding_ids"].append(sc.sha256_json({"pair":pair,"tools":tools,"session":review_session_id,"sidecars":[le["sidecar"]["sidecar_sha256"],re_["sidecar"]["sidecar_sha256"]]}))
            row["evidence_binding_ids"].sort()
            review=row["review_requirement"]; review["required_reviewer_count"]=max(review["required_reviewer_count"],_risk_tier("object",{},"applicability"))
            review["risk_tier"]="high"; review["reasons"].append("science_semantic_link")
            row["alignment_candidate_sha256"]=composer.row_hash(row,"alignment_candidate_sha256")
        alignments.extend(result)
    workpack="science-links-"+sc.sha256_json({"proposals":proposals,"alignments":alignments,"session":review_session_id,"proposer":proposer_instance})[:24]
    plan=_composer_plan(workpack_id=workpack,items=composer._review_items(alignments,[],[],[]),sources=sorted(sources),proposer=proposer_instance,reviewers=reviewers,session=review_session_id,created_at=created_at)
    result={"schema_version":SCIENCE_LINKS_SCHEMA,"protocol":SCIENCE_LIFECYCLE_PROTOCOL,"compiler_version":SCIENCE_LIFECYCLE_COMPILER_VERSION,
        "status":"paused-independent-review","proposals_sha256":sc.sha256_json(proposals),"created_at":created_at,"proposer_instance":proposer_instance,
        "reviewer_instances":reviewers,"review_session_id":review_session_id,"endpoints":sorted(endpoints.values(),key=lambda r:r["concept_ref_id"]),
        "alignments":alignments,"deferred_pairs":deferred,"review_plan":plan,"tool_sha256s":tools,"resolution":composer.RESOLUTION_POLICY,
        "execution_authorized":False,"production_eligible":False,"knowledge_verified":False}
    result["links_sha256"]=sc.sha256_json(result)
    return result


def validate_cross_links(value, library, proposals, attestations):
    expected=build_cross_links(library,proposals,proposer_instance=value.get("proposer_instance"),reviewer_instances=value.get("reviewer_instances"),
        review_session_id=value.get("review_session_id"),created_at=value.get("created_at"))
    if value!=expected:raise ValueError("science_cross_links_stale_or_modified")
    review=_composer_review(value["review_plan"],attestations)
    return {**review,"links_sha256":value["links_sha256"],"endpoints":copy.deepcopy(value["endpoints"]),
        "resolution":value["resolution"],"execution_authorized":False,"production_eligible":False,"knowledge_verified":False,
        "domain_accuracy":"blocked","semantic_truth_verified":False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    domain=sub.add_parser("domain-config")
    domain.add_argument("--domain",type=Path,required=True); domain.add_argument("--part",choices=("profile","tokenizer","routing"),required=True)
    domain.add_argument("--output",type=Path,required=True)
    for name in ("links","validate-links"):
        p=sub.add_parser(name); p.add_argument("--library",type=Path,required=True); p.add_argument("--proposals",type=Path,required=True)
        if name=="links":
            p.add_argument("--proposer-instance",required=True); p.add_argument("--reviewer-instance",action="append")
            p.add_argument("--review-session-id"); p.add_argument("--created-at",required=True); p.add_argument("--output",type=Path,required=True)
        else:
            p.add_argument("--links",type=Path,required=True); p.add_argument("--attestations",type=Path,required=True)
    for name in ("build", "validate", "freeze", "validate-review"):
        p = sub.add_parser(name)
        p.add_argument("--package", type=Path, required=True)
        p.add_argument("--profile", type=Path, required=True)
        if name == "build":
            p.add_argument("--records", type=Path, required=True)
            p.add_argument("--data-origin", choices=("synthetic", "source-backed-proposal"), required=True)
        else:
            p.add_argument("--sidecar", type=Path, required=name != "validate")
        if name in {"build", "freeze"}:
            p.add_argument("--output", type=Path, required=True, help="New JSON file; existing destinations are rejected")
        if name == "freeze":
            p.add_argument("--proposer-instance", required=True)
            p.add_argument("--reviewer-instance", action="append")
            p.add_argument("--review-session-id")
        if name == "validate-review":
            p.add_argument("--frozen", type=Path, required=True)
            p.add_argument("--attestations", type=Path, required=True, help="Externally supplied JSON array; never generated here")
    args = parser.parse_args(argv)
    try:
        if args.command=="domain-config":
            if args.output.resolve().is_relative_to(sc.ROOT.parent.resolve()):raise ValueError("science_output_inside_compiler_forbidden")
            result=domain_inputs(sc.load_json(args.domain))[args.part]; _write_new(args.output,result)
            print(sc.canonical_json(result).decode()); return 0
        if args.command in {"links","validate-links"}:
            import science_index as index
            library,_=index.load_library(args.library); proposals=sc.load_json(args.proposals)
            if args.command=="links":
                index.check_output(args.output,args.library)
                result=build_cross_links(library,proposals,proposer_instance=args.proposer_instance,reviewer_instances=args.reviewer_instance,review_session_id=args.review_session_id,created_at=args.created_at)
                _write_new(args.output,result)
            else:result=validate_cross_links(sc.load_json(args.links),library,proposals,sc.load_json(args.attestations))
            print(sc.canonical_json(result).decode()); return 2 if result["status"]=="blocked" else 0
        package = load_package(args.package)
        profile = sc.load_json(args.profile)
        if args.command == "build":
            sidecar = sc.make_sidecar(sc.load_json(args.records), package, profile, data_origin=args.data_origin)
            # Sidecars must remain outside the sealed base, even when the output is new.
            if args.output.resolve().is_relative_to(args.package.resolve()):
                raise ValueError("science_output_inside_base_forbidden")
            _write_new(args.output, sidecar)
            result = sc.validate_sidecar(sidecar, package, profile)
        else:
            sidecar = _load_sidecar(args.sidecar) if args.sidecar else None
            if args.command == "freeze":
                result = freeze_proposal(sidecar, package, profile, proposer_instance=args.proposer_instance,
                                          reviewer_instances=args.reviewer_instance, review_session_id=args.review_session_id)
                if args.output.resolve().is_relative_to(args.package.resolve()):
                    raise ValueError("science_output_inside_base_forbidden")
                _write_new(args.output, result)
                result = {k: v for k, v in result.items() if k not in {"assertions", "queue", "review_plan"}}
            elif args.command == "validate-review":
                result = validate_review(sc.load_json(args.frozen), sidecar, package, profile, sc.load_json(args.attestations))
            else:
                result = sc.validate_sidecar(sidecar, package, profile)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 2 if result["status"] == "blocked" else 0
    except (ValueError, OSError, ImportError, KeyError, TypeError, RecursionError) as error:
        # Do not dump private package paths or source material in CLI diagnostics.
        reason = str(error).split(":", 1)[0] if isinstance(error, ValueError) else type(error).__name__
        print(json.dumps({"status": "blocked", "reason_codes": [reason], "production_eligible": False,
                          "knowledge_verified": False, "executable": False, "domain_accuracy": "blocked"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
