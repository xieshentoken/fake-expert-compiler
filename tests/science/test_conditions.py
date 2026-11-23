import itertools
import unittest
from science.support import sc, record, quantity, unknown


class ConditionTests(unittest.TestCase):
    def check(self, expr, inputs=None, declared=None):
        return sc.evaluate_predicate(expr, inputs or {}, declared or [{"source": "request", "name": "x"}])

    def operand(self, value): return {"kind": "constant", "value": value}

    def state_expr(self, state):
        value = {"satisfied": quantity(True), "violated": quantity(False), "unknown": unknown()}[state]
        return {"op": "eq", "left": self.operand(value), "right": self.operand(quantity(True))}

    def test_all_three_state_truth_table(self):
        for states in itertools.product(("satisfied", "violated", "unknown"), repeat=2):
            expected = "violated" if "violated" in states else "unknown" if "unknown" in states else "satisfied"
            with self.subTest(states=states):
                self.assertEqual(self.check({"op": "all", "args": [self.state_expr(s) for s in states]})["status"], expected)

    def test_any_three_state_truth_table(self):
        for states in itertools.product(("satisfied", "violated", "unknown"), repeat=2):
            expected = "satisfied" if "satisfied" in states else "unknown" if "unknown" in states else "violated"
            with self.subTest(states=states):
                self.assertEqual(self.check({"op": "any", "args": [self.state_expr(s) for s in states]})["status"], expected)

    def test_not_unknown(self):
        for a, b in (("unknown", "unknown"), ("satisfied", "violated"), ("violated", "satisfied")):
            self.assertEqual(self.check({"op": "not", "arg": self.state_expr(a)})["status"], b)

    def test_numeric_comparison_operators(self):
        for op, expected in (("gt", "satisfied"), ("ge", "satisfied"), ("lt", "violated"), ("le", "violated"), ("eq", "violated")):
            expr = {"op": op, "left": self.operand(quantity(2)), "right": self.operand(quantity(1))}
            self.assertEqual(self.check(expr)["status"], expected)

    def test_between_and_in(self):
        self.assertEqual(self.check({"op": "between", "left": self.operand(quantity(2)), "lower": self.operand(quantity(1)), "upper": self.operand(quantity(2))})["status"], "satisfied")
        self.assertEqual(self.check({"op": "in", "left": self.operand(quantity("steady")), "choices": [self.operand(quantity("steady"))]})["status"], "satisfied")

    def test_known_missing_is_unknown_not_false(self):
        self.assertEqual(self.check({"op": "known", "arg": {"kind": "input", "source": "request", "name": "x"}})["status"], "unknown")

    def test_same_quantity_kind_and_frame(self):
        for op in ("same_quantity_kind", "same_frame"):
            expr = {"op": op, "left": self.operand(quantity(1, kind="conductivity", frame="in-plane")), "right": self.operand(quantity(1, kind="extinction", frame="through-plane"))}
            self.assertEqual(self.check(expr)["status"], "violated")

    def test_required_conditions_block_negative_and_unknown(self):
        condition = record("ConditionSet")
        for inputs, expected in (({}, "unknown"), ({"request:thickness": quantity(-2, "mm", "length")}, "violated"), ({"request:thickness": quantity(2, "mm", "length")}, "satisfied")):
            result = sc.assess_conditions(condition, inputs)
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["conditions_satisfied"], expected == "satisfied")
            self.assertFalse(result["execution_authorized"])

    def test_unit_mismatch_never_compares_raw_numbers(self):
        expr = {"op": "eq", "left": self.operand(quantity(2, "mm", "length")), "right": self.operand(quantity(2, "m", "length"))}
        self.assertEqual(self.check(expr)["status"], "unknown")

    def test_type_confusion_bool_is_not_number(self):
        expr = {"op": "eq", "left": self.operand(quantity(True)), "right": self.operand(quantity(1))}
        self.assertEqual(self.check(expr)["status"], "unknown")

    def test_undeclared_and_dynamic_inputs_rejected(self):
        for name in ("undeclared", "x.__class__", "__import__('os')"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.check({"op": "known", "arg": {"kind": "input", "source": "request", "name": name}})

    def test_code_and_empty_boolean_expressions_rejected(self):
        for expr in ("eval('1')", {"op": "call", "name": "eval"}, {"op": "all", "args": []}, {"op": "any", "args": []}):
            with self.subTest(expr=expr), self.assertRaises(ValueError): self.check(expr)

    def test_short_circuit_cannot_hide_invalid_branch(self):
        with self.assertRaises(ValueError):
            self.check({"op": "any", "args": [self.state_expr("satisfied"), {"op": "exec"}]})

    def test_reversed_interval_rejected(self):
        with self.assertRaises(ValueError):
            self.check({"op": "between", "left": self.operand(quantity(2)), "lower": self.operand(quantity(3)), "upper": self.operand(quantity(1))})

    def test_condition_receipt_contains_inputs_and_reason(self):
        result = self.check({"op": "gt", "left": self.operand(quantity(-1)), "right": self.operand(quantity(0))})
        self.assertEqual(result["inputs"][0]["value"], -1)
        self.assertIn("reason_code", result)
