"""M0: freeze existing arithmetic/retrieval limits before adding science semantics."""
import copy
import json
from pathlib import Path
import sys
import unittest

from science.paths import ROOT, SCRIPTS
from formula_execution import FormulaContractError, evaluate_formula_ast, parse_unit, validate_formula_ast
from portable_reference_runtime import tokenize


class LegacyProbeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent / "fixtures/legacy-synthetic.json").read_text())
        self.ast = self.fixture["ast"]
        self.inputs = self.fixture["inputs"]
        self.contract = validate_formula_ast(self.ast)

    def test_fixture_has_no_knowledge_authority(self):
        self.assertEqual(self.fixture["provenance"], "original-synthetic-software-test")
        self.assertFalse(self.fixture["knowledge_verified"])
        self.assertFalse(self.fixture["independent_gold"])

    def test_slab_power(self):
        self.assertEqual(evaluate_formula_ast(self.contract, self.inputs)["value"], "2.5")

    def test_equivalent_si_inputs(self):
        self.inputs["area"] = {"value": "0.0001", "unit": "m^2"}
        self.inputs["thickness"] = {"value": "0.002", "unit": "m"}
        self.assertEqual(evaluate_formula_ast(self.contract, self.inputs)["value"], "2.5")

    def test_negative_thickness_is_still_arithmetic_only(self):
        self.inputs["thickness"]["value"] = "-2"
        self.assertEqual(evaluate_formula_ast(self.contract, self.inputs)["value"], "-2.5")

    def test_zero_thickness_rejected(self):
        self.inputs["thickness"]["value"] = "0"
        with self.assertRaisesRegex(FormulaContractError, "execution_divide_by_zero"):
            evaluate_formula_ast(self.contract, self.inputs)

    def test_units_remain_restricted(self):
        for unit in ("MPa", "degC", "W/(m*K)"):
            with self.subTest(unit=unit), self.assertRaises(FormulaContractError):
                parse_unit(unit)
        self.assertEqual(str(parse_unit("W/m/K")[0]), "1")

    def test_unsupported_operator_rejected(self):
        ast = copy.deepcopy(self.ast)
        ast["expression"] = {"kind": "exp", "arg": {"kind": "symbol", "name": "delta_temperature"}}
        with self.assertRaisesRegex(FormulaContractError, "formula_ast_operation_invalid"):
            validate_formula_ast(ast)

    def test_single_letters_and_greek_legacy_limit(self):
        self.assertEqual(tokenize("k K T t λ ρ ν α"), [])

    def test_subscripts_and_chinese_legacy_behavior(self):
        self.assertEqual(tokenize("c_p c_v"), ["c_p", "c_v"])
        self.assertEqual(tokenize("热穿过这块板有多难"), ["热穿过这块板有多难"])


if __name__ == "__main__":
    unittest.main()
