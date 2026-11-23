"""Real legacy child-process tests, with qualification mocked only in orchestration.

Temporary runner-only fixtures intentionally lack publish qualification and any
Review/Gold. The production loader is tested to reject them. A generated receipt
in these tests is software evidence only and is never saved as production data.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from science.m3_support import calculation_fixture
from science.support import sc
import science_calculation as calc
import science_workpack as sw


class CalculationReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name).resolve()
        self.p,self.spec,self.req,self.side,self.prof=calculation_fixture()
        self.plan=calc.build_plan(self.spec,self.req,self.p,self.side,self.prof)
        self.ast=self.p["objects"][0]["formula"]["ast"]
        module=self.root/"scripts/formula.py"; module.parent.mkdir()
        module.write_text(calc.legacy.render_formula_module(calc.legacy.validate_formula_ast(self.ast)))
        runner=self.root/"scripts/run_formula_sandbox.py"; runner.write_bytes((sc.ROOT/"formula_sandbox_runner.py").read_bytes())
        selected={"object_id":self.p["objects"][0]["id"],"entrypoint":"scripts/formula.py","module_sha256":hashlib.sha256(module.read_bytes()).hexdigest()}
        bundle={"formulas":[selected],"runner":{"entrypoint":"scripts/run_formula_sandbox.py","sha256":hashlib.sha256(runner.read_bytes()).hexdigest()}}
        policy={"timeout_seconds":2,"isolation":"process-resource-limits-not-kernel-sandbox"}
        ref=self.root/"references/execution"; ref.mkdir(parents=True)
        (ref/"manifest.json").write_text(json.dumps(bundle)); (ref/"policy.json").write_text(json.dumps(policy))
        # This is the original runner's minimal process seal, deliberately NOT
        # an accepted knowledge package, reviewer attestation or Gold artifact.
        minimal={"package":{"status":"ready"},"capabilities":{"reference":True,"decision_support":False,"executable":True},
            "build":{"compiler":"fake-expert","compiler_version":"0.4.0-execution"},"integrity":{"algorithm":"sha256",
                "files":{str(f.relative_to(self.root)):hashlib.sha256(f.read_bytes()).hexdigest() for f in self.root.rglob("*") if f.is_file()}}}
        (self.root/"manifest.json").write_text(json.dumps(minimal))
        self.ctx={"package":self.p,"plan":self.plan,"ast":self.ast,"policy":policy,"bundle":bundle,"selected":selected,"runner":runner}
        self.gates={"execution_authorized":True,"execution_gate_refs":[{"gate":"synthetic-mocked-boundary","sha256":"a"*64}]}
        self.inputs={r["symbol"]:{"value":r["decimal_value"],"unit":r["normalized_quantity"]["unit"]} for r in self.plan["input_bindings"]}

    def tearDown(self): self.temp.cleanup()

    def run_fixture(self):
        with patch.object(calc,"_prepare",return_value=self.ctx),patch.object(calc,"_gates",return_value=self.gates):
            return calc.run_calculation(self.root,self.spec,self.req,self.side,self.prof)

    def test_original_runner_process_returns_synthetic_2_5_w(self):
        response=calc._invoke(self.ctx,self.inputs)
        self.assertEqual(response["result"],calc.numeric_preflight(self.ast,self.inputs))
        self.assertEqual(response["result"]["value"],"2.5")
        self.assertEqual(response["result"]["unit"],"W")

    def test_process_fixture_cannot_qualify_as_production_package(self):
        with self.assertRaises(ValueError): sw.load_package(self.root)
        with self.assertRaises(ValueError): calc.run_calculation(self.root,self.spec,self.req,self.side,self.prof,allow_execution=True)

    def test_original_runner_rechecks_seal_before_child(self):
        (self.root/"scripts/formula.py").write_text("tampered")
        with self.assertRaisesRegex(ValueError,"legacy_runner_failed"): calc._invoke(self.ctx,self.inputs)

    def test_m3_requires_exact_canonical_module_even_when_loader_mocked(self):
        (self.root/"scripts/formula.py").write_text((self.root/"scripts/formula.py").read_text()+"\n# not canonical\n")
        with patch.object(sw,"load_package",return_value=self.p),self.assertRaisesRegex(ValueError,"canonical_generated_surface_required"):
            calc._prepare(self.root,self.spec,self.req,self.side,self.prof)

    def test_relative_package_path_survives_isolated_working_directory(self):
        with patch.object(sw,"load_package",return_value=self.p):
            context=calc._prepare(Path(os.path.relpath(self.root)),self.spec,self.req,self.side,self.prof)
        self.assertTrue(context["runner"].is_absolute())
        self.assertEqual(calc._invoke(context,self.inputs)["result"]["value"],"2.5")

    def test_complete_receipt_binds_result_trace_without_claiming_truth(self):
        receipt=self.run_fixture(); calc.shape(receipt,"receipt")
        self.assertEqual(receipt["result"]["value"],"2.5")
        self.assertEqual(receipt["input_bindings"],self.plan["input_bindings"])
        self.assertEqual(receipt["reviewed_ast_sha256"],sc.sha256_json(self.ast))
        self.assertEqual(receipt["uncertainty_status"],"unavailable")
        self.assertFalse(receipt["knowledge_verified"])
        self.assertEqual(receipt["domain_accuracy"],"blocked")
        self.assertNotIn("confidence",receipt)

    def test_receipt_replay_uses_no_process_and_checks_exact_data(self):
        receipt=self.run_fixture()
        with patch.object(calc,"_prepare",return_value=self.ctx),patch.object(calc,"_gates",return_value=self.gates),patch.object(calc,"_invoke") as runner:
            result=calc.validate_receipt(receipt,self.root,self.spec,self.req,self.side,self.prof)
            self.assertEqual(result["status"],"receipt-bindings-and-numeric-replay-valid"); runner.assert_not_called()
            for field,value in (("engine_sha256","a"*64),("runner_response_sha256","b"*64),("output_quantity_kind","speed")):
                changed={**receipt,field:value}
                changed["receipt_sha256"]=sc.sha256_json({k:v for k,v in changed.items() if k!="receipt_sha256"})
                with self.assertRaisesRegex(ValueError,"stale_or_modified"): calc.validate_receipt(changed,self.root,self.spec,self.req,self.side,self.prof)

    def test_expired_or_missing_gates_block_old_receipt(self):
        receipt=self.run_fixture()
        with patch.object(calc,"_prepare",return_value=self.ctx),patch.object(calc,"_gates",return_value={"execution_authorized":False,"status":"blocked"}),patch.object(calc,"_invoke") as runner:
            result=calc.validate_receipt(receipt,self.root,self.spec,self.req,self.side,self.prof)
            self.assertEqual(result["receipt_status"],"current-gates-blocked"); runner.assert_not_called()

    def test_generated_result_disagreement_never_gets_receipt(self):
        wrong={"status":"ok","result":{"value":"999"}}
        with patch.object(calc,"_prepare",return_value=self.ctx),patch.object(calc,"_gates",return_value=self.gates),patch.object(calc,"_invoke",return_value=wrong):
            with self.assertRaisesRegex(ValueError,"result_mismatch"): calc.run_calculation(self.root,self.spec,self.req,self.side,self.prof)

    def test_numeric_failure_blocks_before_runner_after_mocked_permission(self):
        next(r for r in self.ctx["plan"]["input_bindings"] if r["symbol"]=="d")["decimal_value"]="0"
        with patch.object(calc,"_prepare",return_value=self.ctx),patch.object(calc,"_gates",return_value=self.gates),patch.object(calc,"_invoke") as runner:
            with self.assertRaisesRegex(ValueError,"divide_by_zero"): calc.run_calculation(self.root,self.spec,self.req,self.side,self.prof)
            runner.assert_not_called()

    def test_receipt_contract_rejects_unknown_fields_and_boolean_numbers(self):
        receipt=self.run_fixture(); receipt["confidence"]=1
        with self.assertRaises(ValueError): calc.shape(receipt,"receipt")
        del receipt["confidence"]; receipt["result"]["value"]=True
        with self.assertRaises(ValueError): calc.shape(receipt,"receipt")
