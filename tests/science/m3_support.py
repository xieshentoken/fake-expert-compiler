"""Synthetic arithmetic data only; no Review, Gold, seal or execution qualification."""
from science.support import sc
from formula_execution import FORMULA_AST_SCHEMA
import copy
from science.support import context, record, profile, object_ref, evidence_ref, quantity, unknown
from science.m2_support import request
import science_calculation as calc


def node(name):
    return {"kind":"symbol","name":name}


def operation(kind,left,right):
    return {"kind":kind,"left":left,"right":right}


def slab_ast(output="P"):
    symbols={"d":{"role":"input","unit":"m","dimension":{"L":1}},
             "A":{"role":"input","unit":"m^2","dimension":{"L":2}},
             "k":{"role":"input","unit":"W/m/K","dimension":{"L":1,"M":1,"T":-3,"Theta":-1}}}
    if output=="P":
        symbols["dT"]={"role":"input","unit":"K","dimension":{"Theta":1}}
        symbols["P"]={"role":"output","unit":"W","dimension":{"L":2,"M":1,"T":-3}}
        expression=operation("divide",operation("multiply",operation("multiply",node("k"),node("A")),node("dT")),node("d"))
    else:
        symbols["R"]={"role":"output","unit":"K/W","dimension":{"L":-2,"M":-1,"T":3,"Theta":1}}
        expression=operation("divide",node("d"),operation("multiply",node("k"),node("A")))
    return {"schema_version":FORMULA_AST_SCHEMA,"symbols":symbols,"output":output,"expression":expression}


def slab_inputs(output="P"):
    value={"d":{"value":"2","unit":"mm"},"A":{"value":"100","unit":"mm^2"},"k":{"value":"5","unit":"W/m/K"}}
    if output=="P": value["dT"]={"value":"10","unit":"K"}
    return value


def unary_ast(expression):
    return {"schema_version":FORMULA_AST_SCHEMA,"output":"y","symbols":{
        "x":{"role":"input","unit":"1","dimension":{}},"y":{"role":"output","unit":"1","dimension":{}}},"expression":expression}


def property_fixture(curve=False, uncertainty=None):
    material=record("MaterialState", material_identity={"status":"known","value":"synthetic material"},
        sample_identity={"status":"known","value":"synthetic sample"},orientation_frame={"status":"known","value":"in-plane"})
    observation=record("PropertyObservation",material_state_ref=material["id"],method_ref=object_ref(),
        measurement_conditions=[] if curve else [{"name":"temperature","value":quantity(25,"degC","temperature")}],
        value_representation={"kind":"curve","x_unit":"K","points":[{"x":300,"y":5},{"x":400,"y":7}]} if curve else {"kind":"scalar","value":5},
        uncertainty=uncertainty or unknown())
    selected={"observation":calc.record_ref(observation),"material_state":calc.record_ref(material),"method_ref":object_ref(),
        "sample_identity":"synthetic sample","orientation_frame":"in-plane",
        "conditions":[{"name":"temperature","value":quantity(350,"K","temperature") if curve else quantity(298.15,"K","temperature")}],
        "mode":"linear" if curve else "scalar","axis":{"name":"temperature","quantity_kind":"temperature","frame":"not-applicable"} if curve else None,
        "coordinate":quantity(350,"K","temperature") if curve else None}
    return {"records":[material,observation]},profile(),selected


def calculation_fixture(*, output="P", equation_kind="quantity-equation", native_override=None, use_property=False):
    """In-memory binding topology ONLY. No seal, Review, Gold or publish validity.

    The ready flag exercises M1 AST-link checks; this dict is never saved as a
    knowledge package and cannot pass the production filesystem qualification.
    """
    package=context(); ast=slab_ast(output)
    package["manifest"]["package"]["status"]="ready"
    package["manifest"]["capabilities"]={"reference":True,"decision_support":False,"executable":True}
    package["objects"][0].update(origin="source-explicit",formula={"ast":ast,"ast_sha256":sc.sha256_json(ast)})
    package["execution_ast_bindings"]={package["objects"][0]["id"]:sc.sha256_json(ast)}
    package["fingerprint"].update(manifest_sha256=sc.sha256_json(package["manifest"]),content_sha256=sc.sha256_json(package["objects"]+package["anchors"]))
    ref,eref=object_ref(package),evidence_ref(package)
    def replace(value):
        if isinstance(value,dict):
            if "evidence_sha256" in value: return copy.deepcopy(eref)
            if "object_sha256" in value: return copy.deepcopy(ref)
            return {k:replace(v) for k,v in value.items()}
        return [replace(v) for v in value] if isinstance(value,list) else value
    def make(kind,**fields): return sc.bind_record(replace(record(kind,**fields)))
    kinds={"d":"length","A":"area","k":"thermal-conductivity","dT":"temperature-difference","P":"power","R":"thermal-resistance"}
    bindings={name:make("SymbolBinding",original_glyph=name,display_latex=name,canonical_quantity_id=kinds[name],quantity_kind=kinds[name],
        unit=info["unit"],dimension=list(calc.legacy.parse_unit(info["unit"])[1]),coordinate_frame={"status":"known","value":"not-applicable"},
        value_domain="nonnegative-real" if name=="dT" else "positive-real") for name,info in ast["symbols"].items()}
    condition=make("ConditionSet")
    extra=[]; selection=None
    if use_property:
        material=make("MaterialState",material_identity={"status":"known","value":"synthetic material"},
            sample_identity={"status":"known","value":"synthetic sample"},orientation_frame={"status":"known","value":"not-applicable"})
        observation=make("PropertyObservation",material_state_ref=material["id"],method_ref=ref,
            measurement_conditions=[{"name":"temperature","value":quantity(25,"degC","temperature")}])
        extra=[material,observation]
        selection={"observation":calc.record_ref(observation),"material_state":calc.record_ref(material),"method_ref":ref,
            "sample_identity":"synthetic sample","orientation_frame":"not-applicable","conditions":[{"name":"temperature","value":quantity(25,"degC","temperature")}],
            "mode":"scalar","axis":None,"coordinate":None}
    convention=unknown()
    if equation_kind=="numerical-value-equation":
        convention={"status":"known","input_units":[{"symbol_binding_ref":bindings[n]["id"],"unit":native_override or info["unit"],
            "temperature_scale":"difference" if n=="dT" else "not-applicable"} for n,info in ast["symbols"].items() if info["role"]=="input"],
            "coefficient_basis":"Synthetic formula convention only"}
    formula=make("FormulaCard",equation_kind=equation_kind,symbol_bindings=[b["id"] for b in bindings.values()],
        condition_set_ref=condition["id"],dependency_refs=[extra[-1]["id"]] if extra else [],representation_state="scalar-ast",
        execution_state="requires-existing-gates",executable_ast_ref={"source_object_ref":ref,"ast_sha256":sc.sha256_json(ast)},unit_convention=convention)
    sidecar=sc.make_sidecar([*bindings.values(),condition,*extra,formula],package,profile(),data_origin="synthetic")
    req=request([{"package":package}]); req.update(intent="calculate",execution_requested=True)
    values=slab_inputs(output)
    for name,row in values.items():
        req["facts"].append({"name":"thickness" if name=="d" else name,
            "value":quantity(float(row["value"]),row["unit"],kinds[name]),"source_ref":unknown("Synthetic software input only")})
    spec={"schema_version":calc.SCIENCE_CALCULATION_SCHEMA,"protocol":calc.SCIENCE_CALCULATION_PROTOCOL,
        "request_sha256":sc.sha256_json(req),"base_package":package["fingerprint"],"sidecar_sha256":sidecar["sidecar_sha256"],
        "formula_card":calc.record_ref(formula),"input_bindings":[{"symbol":n,"symbol_binding":calc.record_ref(bindings[n]),
            "source":{"kind":"property","selection":selection} if n=="k" and selection else {"kind":"request","name":"thickness" if n=="d" else n},
            "basis":"not-applicable"} for n in sorted(values)],"output_symbol_binding":calc.record_ref(bindings[output]),
        "condition_bindings":[{"source":"request","name":"thickness","request_name":"thickness","property_symbol":None}]}
    return package,spec,req,sidecar,profile()
