"""Synthetic binding/gate tests. No accepted reviewer decisions are authored."""
import copy
from datetime import datetime,timezone
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from science.m3_support import calculation_fixture
from science.support import sc,quantity,unknown,reseal
import science_calculation as calc
import science_workpack as sw

NOW=datetime(2026,9,19,8,0,tzinfo=timezone.utc)


def context_and_permission():
    p,s,r,side,prof=calculation_fixture()
    plan=calc.build_plan(s,r,p,side,prof)
    ctx={"package":p,"plan":plan,"ast":p["objects"][0]["formula"]["ast"],"bundle":{},"selected":{},"policy":{}}
    permission={"schema_version":calc.SCIENCE_EXECUTION_PERMISSION_SCHEMA,"plan_sha256":plan["plan_sha256"],
        "host_instance":"synthetic-host","session_id":"synthetic-session","issued_at":"2026-09-19T07:59:00+00:00",
        "expires_at":"2026-09-19T08:01:00+00:00","allow_execution":True,"network":False,"write_package":False,
        "attestation_level":"host-orchestrator-recorded-not-cryptographic"}
    return ctx,permission,(p,s,r,side,prof)


class CalculationGateTests(unittest.TestCase):
    def test_plan_binds_exact_request_versions_quantities_and_scope(self):
        ctx,_,_=context_and_permission(); plan=ctx["plan"]
        self.assertFalse(plan["execution_authorized"])
        rows={r["symbol"]:r for r in plan["input_bindings"]}
        self.assertEqual(rows["A"]["decimal_value"],"0.0001")
        self.assertEqual(rows["d"]["decimal_value"],"0.002")
        self.assertEqual(plan["condition_results"][0]["status"],"satisfied")
        self.assertEqual(plan["plan_sha256"],sc.sha256_json({k:v for k,v in plan.items() if k!="plan_sha256"}))

    def test_property_selection_is_bound_to_request_plan(self):
        p,s,r,side,prof=calculation_fixture(use_property=True)
        result=calc.build_plan(s,r,p,side,prof)
        self.assertEqual(len(result["property_receipts"]),1)
        self.assertEqual(result["property_receipts"][0]["selection_sha256"],sc.sha256_json(s["input_bindings"][-1]["source"]["selection"]))

    def test_request_intent_is_necessary_but_not_permission(self):
        ctx,_,args=context_and_permission(); p,s,r,side,prof=args
        self.assertFalse(calc._gates(ctx,side,prof)["execution_authorized"])
        r["execution_requested"]=False; s["request_sha256"]=sc.sha256_json(r)
        with self.assertRaisesRegex(ValueError,"intent_required"): calc.build_plan(s,r,p,side,prof)

    def test_input_hash_scope_and_record_drift_rejected(self):
        for field in ("request_sha256","sidecar_sha256","formula_card","base_package"):
            p,s,r,side,prof=calculation_fixture()
            if field.endswith("sha256"): s[field]="a"*64
            elif field=="formula_card": s[field]["sha256"]="a"*64
            else: s[field]["package_version"]="0.2.0"
            with self.subTest(field=field),self.assertRaises(ValueError): calc.build_plan(s,r,p,side,prof)

    def test_quantity_kind_unit_frame_unknown_and_negative_domain_rejected(self):
        for value in (quantity(2,"mm","speed"),quantity(2,"MPa","length"),quantity(2,"mm","length","other-frame"),unknown(),quantity(-2,"mm","length")):
            p,s,r,side,prof=calculation_fixture(); r["facts"][0]["value"]=value; s["request_sha256"]=sc.sha256_json(r)
            with self.subTest(value=value),self.assertRaises(ValueError): calc.build_plan(s,r,p,side,prof)

    def test_missing_duplicate_or_renamed_bindings_fail(self):
        for mode in ("missing","duplicate","rename"):
            p,s,r,side,prof=calculation_fixture()
            if mode=="missing": s["input_bindings"].pop()
            elif mode=="duplicate": s["input_bindings"].append(copy.deepcopy(s["input_bindings"][0]))
            else: s["input_bindings"][0]["symbol"]="wrong"
            with self.assertRaises(ValueError): calc.build_plan(s,r,p,side,prof)

    def test_condition_cannot_substitute_another_request_fact(self):
        p,s,r,side,prof=calculation_fixture(); r["facts"].append({"name":"other_thickness","value":quantity(3,"mm","length"),"source_ref":unknown()})
        s["request_sha256"]=sc.sha256_json(r); s["condition_bindings"][0]["request_name"]="other_thickness"
        with self.assertRaisesRegex(ValueError,"condition_request_input_missing"): calc.build_plan(s,r,p,side,prof)

    def test_empirical_native_units_are_explicit_and_never_silently_converted(self):
        p,s,r,side,prof=calculation_fixture(equation_kind="numerical-value-equation")
        self.assertFalse(calc.build_plan(s,r,p,side,prof)["execution_authorized"])
        p,s,r,side,prof=calculation_fixture(equation_kind="numerical-value-equation",native_override="degC")
        with self.assertRaisesRegex(ValueError,"native_units_unsupported"): calc.build_plan(s,r,p,side,prof)

    def test_sidecar_cannot_self_enable(self):
        p,s,r,side,prof=calculation_fixture(); side["executable"]=True; reseal(side); s["sidecar_sha256"]=side["sidecar_sha256"]
        with self.assertRaises(ValueError): calc.build_plan(s,r,p,side,prof)

    def test_freeze_without_identity_stays_answer_free(self):
        ctx,_,_=context_and_permission()
        frozen=calc.freeze_calculation(ctx["plan"],ctx["package"],proposer_instance="synthetic-proposer")
        self.assertIsNone(frozen["review_plan"])
        self.assertFalse(frozen["execution_authorized"])
        self.assertNotIn("attestations",frozen)
        self.assertEqual(calc.validate_calculation_review(frozen,[],ctx["plan"],ctx["package"])["status"],"blocked")

    def test_unrelated_package_context_is_not_review_focus_evidence(self):
        ctx,_,_=context_and_permission()
        ctx["package"]["anchors"].append({"id":"unrelated-anchor","text_spans":[{"not-focus":"synthetic unrelated text"}]})
        frozen=calc.freeze_calculation(ctx["plan"],ctx["package"],proposer_instance="synthetic-proposer")
        self.assertTrue(all(row["evidence_spans"]==[] for row in frozen["assertions"]))

    def test_self_review_and_single_reviewer_rejected(self):
        ctx,_,_=context_and_permission()
        for reviewers in (["synthetic-proposer","reviewer-b"],["reviewer-a"],["reviewer-a","reviewer-a"]):
            with self.assertRaises(ValueError): calc.freeze_calculation(ctx["plan"],ctx["package"],proposer_instance="synthetic-proposer",
                reviewer_instances=reviewers,review_session_id="synthetic-review-session")

    def test_named_reviewers_do_not_create_external_attestations(self):
        ctx,_,_=context_and_permission()
        frozen=calc.freeze_calculation(ctx["plan"],ctx["package"],proposer_instance="synthetic-proposer",
            reviewer_instances=["synthetic-a","synthetic-b"],review_session_id="synthetic-review-session")
        self.assertEqual(calc.validate_calculation_review(frozen,[],ctx["plan"],ctx["package"])["status"],"blocked")
        frozen["queue"][0]["item_sha256"]="a"*64
        with self.assertRaisesRegex(ValueError,"frozen_review_stale"): calc.validate_calculation_review(frozen,[],ctx["plan"],ctx["package"])

    def test_host_permission_current_exact_bounded_and_separate_flag(self):
        ctx,permission,_=context_and_permission()
        calc._permission(permission,ctx["plan"],True,NOW)
        for key,value in (("plan_sha256","a"*64),("expires_at","2026-09-19T07:59:30+00:00"),
                          ("expires_at","2026-09-20T08:00:00+00:00"),("issued_at","2026-09-19T08:00:01+00:00"),
                          ("issued_at","2026-09-19T07:59:00"),("network",True)):
            altered={**permission,key:value}
            with self.subTest(key=key,value=value),self.assertRaises(ValueError): calc._permission(altered,ctx["plan"],True,NOW)
        with self.assertRaises(ValueError): calc._permission(permission,ctx["plan"],False,NOW)

    def test_synthetic_and_missing_review_block_even_with_permission(self):
        ctx,permission,args=context_and_permission(); side,prof=args[3:]
        with patch.object(calc,"datetime",wraps=datetime) as clock:
            clock.now.return_value=NOW
            result=calc._gates(ctx,side,prof,permission=permission,allow_execution=True)
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(next(c for c in result["checks"] if c["gate"]=="host_permission")["status"],"passed")
        for code in ("science_synthetic_data_cannot_authorize_production","science_independent_review_required","calculation_independent_review_required"):
            self.assertIn(code,result["reason_codes"])

    def test_every_and_gate_blocks_independently(self):
        # Explicit mock of external gate results, not fabricated Review records.
        for failed in ("source","science","calculation","condition","permission"):
            ctx,permission,args=context_and_permission(); side,prof=args[3:]
            side["data_origin"]="source-backed-proposal" if failed!="source" else "synthetic"
            if failed=="condition": ctx["plan"]["condition_results"][0]["status"]="unknown"
            result_for=lambda which: {"status":"blocked","reason_codes":["synthetic_gate_failure"]} if failed==which else {"status":"review-bindings-valid"}
            with patch.object(sw,"validate_review",return_value=result_for("science")),patch.object(calc,"validate_calculation_review",return_value=result_for("calculation")),patch.object(calc,"datetime",wraps=datetime) as clock:
                clock.now.return_value=NOW
                result=calc._gates(ctx,side,prof,science_frozen={},permission=permission,allow_execution=failed!="permission")
                self.assertFalse(result["execution_authorized"],failed)
                self.assertEqual(sum(c["status"]=="blocked" for c in result["checks"]),1)

    def test_production_call_cannot_override_current_clock(self):
        ctx,_,args=context_and_permission(); p,s,r,side,prof=args
        with patch.object(calc,"_prepare",return_value=ctx),patch.object(calc,"_invoke") as runner:
            with self.assertRaises(TypeError): calc.run_calculation(Path("unused-synthetic"),s,r,side,prof,now=NOW)
            runner.assert_not_called()

    def test_blocked_run_never_calls_runner(self):
        ctx,_,args=context_and_permission(); p,s,r,side,prof=args
        with patch.object(calc,"_prepare",return_value=ctx),patch.object(calc,"_invoke") as runner:
            result=calc.run_calculation(Path("unused-synthetic"),s,r,side,prof)
            self.assertIsNone(result["receipt"]); runner.assert_not_called()

    def test_cli_output_invalid_rejected_before_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); base=root/"base"; base.mkdir(); kept=root/"existing"; kept.write_text("preserve")
            for dest in (base/"new.json",kept,root/"missing"/"new.json"):
                args=["run","--package",str(base),"--specification","absent","--request","absent","--sidecar","absent","--profile","absent","--output",str(dest)]
                with patch.object(calc,"run_calculation") as run,redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(calc.main(args),2); run.assert_not_called()
                    self.assertIn("science_output_",json.loads(output.getvalue())["reason_codes"][0])
            self.assertEqual(kept.read_text(),"preserve")


class CalculationDraftIOTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try: import test_direct_reference_skill as existing
        except ImportError as error: raise unittest.SkipTest("Synthetic PDF dependencies unavailable") from error
        cls.legacy=existing.DirectReferenceSkillTests(); cls.legacy.setUp()
        cls.base=cls.legacy.root.resolve()/"m3-synthetic-draft"; cls.legacy._build(cls.base)

    @classmethod
    def tearDownClass(cls): cls.legacy.tearDown()

    def test_real_synthetic_draft_rejected_by_production_path(self):
        before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.rglob("*") if p.is_file()}
        self.assertEqual(sw.load_package(self.base)["manifest"]["package"]["status"],"draft")
        with patch.object(calc,"_invoke") as runner,self.assertRaisesRegex(ValueError,"sealed_execution_package_required"):
            calc.run_calculation(self.base,{}, {},{}, {})
        runner.assert_not_called()
        self.assertEqual(before,{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.rglob("*") if p.is_file()})
