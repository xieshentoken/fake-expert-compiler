"""Original synthetic records, no real materials, reviewer decisions or Gold."""
import copy
import sys
from pathlib import Path

from science.paths import ROOT, SKILL_ROOT, SCRIPTS
import science_contract as sc
from compiler_version import SCIENCE_PROFILE_SCHEMA, SCIENCE_RECORD_SCHEMA


def unknown(reason="not supplied in synthetic source"):
    return {"status": "unknown", "reason": reason}


def quantity(value, unit="1", kind="flag", frame="not-applicable"):
    return {"status": "known", "value": value, "unit": unit, "quantity_kind": kind, "frame": frame}


def context():
    anchor = {"id": "synthetic-evidence", "supports": ["synthetic-object"],
              "source_id": "synthetic-source", "physical_page": 1, "text_sha256": sc.sha256_json("synthetic text")}
    obj = {"id": "synthetic-object", "type": "Equation", "evidence_ids": [anchor["id"]],
           "source_ids": ["synthetic-source"], "statement": "Original synthetic software fixture only"}
    manifest = {"package": {"id": "synthetic-package", "version": "0.1.0", "status": "draft"}, "capabilities": {"executable": False},
                "sources": [{"id": "synthetic-source", "sha256": sc.sha256_json("original synthetic software source")}]}
    return {"fingerprint": {"package_id": manifest["package"]["id"], "package_version": manifest["package"]["version"],
                            "manifest_sha256": sc.sha256_json(manifest), "content_sha256": sc.sha256_json([obj, anchor])},
            "manifest": manifest, "objects": [obj], "anchors": [anchor]}


def object_ref(ctx=None):
    ctx = ctx or context()
    return {"package_id": ctx["fingerprint"]["package_id"], "package_version": ctx["fingerprint"]["package_version"],
            "object_id": ctx["objects"][0]["id"], "object_sha256": sc.sha256_json(ctx["objects"][0])}


def evidence_ref(ctx=None):
    ctx = ctx or context()
    return {"object_ref": object_ref(ctx), "evidence_id": ctx["anchors"][0]["id"],
            "evidence_sha256": sc.sha256_json(ctx["anchors"][0])}


def record(kind="SymbolBinding", **changes):
    source = object_ref()
    fields = {
        "SymbolBinding": dict(scope_ref=source, original_glyph="k", display_latex="k",
            canonical_quantity_id="thermal-conductivity", quantity_kind="thermal-conductivity", unit="W/m/K",
            dimension=[1, 1, -3, 0, -1, 0, 0], value_type="scalar", shape=[], definition_ref=source,
            coordinate_frame=unknown(), value_domain="positive-real"),
        "ConditionSet": dict(predicates=[{"op": "gt", "left": {"kind": "input", "source": "request", "name": "thickness"},
            "right": {"kind": "constant", "value": quantity(0, "mm", "length")}}],
            declared_inputs=[{"source": "request", "name": "thickness"}], required_for=["calculation"],
            source_or_assumption_refs=[source], assessment_policy="three-state-required-v0.1"),
        "MaterialState": dict(material_identity=unknown(), composition=unknown(), processing_history=unknown(),
            phase_microstructure=unknown(), orientation_frame=unknown(), sample_identity=unknown(), conditions=[]),
        "PropertyObservation": dict(property_quantity_id="thermal-conductivity", material_state_ref=unknown(),
            value_representation={"kind": "scalar", "value": 5}, unit="W/m/K", measurement_conditions=[],
            method_ref=unknown(), uncertainty=unknown(), source_refs=[source], evidence_refs=[evidence_ref()],
            source_category="illustrative", value_qualifier="typical"),
        "FormulaCard": dict(notation_refs=[{"role": "original", "latex": "P=k A \\Delta T/d", "source_object_ref": source}],
            equation_kind="quantity-equation", formula_role="constitutive-law", symbol_bindings=[], dependency_refs=[],
            condition_set_ref=unknown(), representation_state="opaque", executable_ast_ref=unknown(),
            execution_state="reference-only", unit_convention=unknown()),
    }[kind]
    fields.update(changes)
    r = {"schema_version": SCIENCE_RECORD_SCHEMA, "kind": kind, "source_object_ref": source,
         "source_evidence_refs": [evidence_ref()], "review_state": "candidate", **fields}
    provenance_fields = set(r) - {"schema_version", "kind", "review_state"}
    r["provenance"] = [{"field": f, "status": "unknown" if isinstance(r[f], dict) and r[f].get("status") == "unknown" else "source-explicit",
                        "evidence_refs": [] if isinstance(r[f], dict) and r[f].get("status") == "unknown" else [evidence_ref()],
                        "derivation_refs": [], "reason": "Original synthetic software fixture only"} for f in sorted(provenance_fields)]
    return sc.bind_record(r)


def profile():
    return {"schema_version": SCIENCE_PROFILE_SCHEMA, "profile_id": "synthetic-profile", "version": "0.1.0",
            "required_material_fields": ["material_identity", "sample_identity", "orientation_frame"],
            "required_measurement_conditions": ["temperature"]}


def sidecar(records=None):
    return sc.make_sidecar(records or [record()], context(), profile(), data_origin="synthetic")


def reseal(data):
    data["inventory"] = [{"id": r["id"], "sha256": r["content_sha256"]} for r in data["records"]]
    data["sidecar_sha256"] = sc.sha256_json({k: v for k, v in data.items() if k != "sidecar_sha256"})
    return data
