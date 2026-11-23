import copy
from decimal import localcontext
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from science.m3_support import slab_ast,slab_inputs,unary_ast,node
import formula_execution as legacy
import science_calculation as calc


class NumericParityTests(unittest.TestCase):
    def generated(self,ast,inputs):
        source=legacy.render_formula_module(legacy.validate_formula_ast(ast))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"synthetic-module.py"; path.write_text(source)
            result=subprocess.run([sys.executable,"-I","-S","-B",str(path)],input=json.dumps({"inputs":inputs}),
                capture_output=True,text=True,timeout=3)
        return json.loads(result.stdout)

    def test_synthetic_slab_power_and_resistance(self):
        for output,expected in (("P","2.5"),("R","4")):
            ast=slab_ast(output); values=slab_inputs(output)
            preflight=calc.numeric_preflight(ast,values)
            self.assertEqual(preflight["value"],expected)
            self.assertEqual(self.generated(ast,values)["result"],preflight)

    def test_equivalent_area_units(self):
        ast=slab_ast(); a=slab_inputs(); b=copy.deepcopy(a); b["A"]={"value":"0.0001","unit":"m^2"}
        self.assertEqual(calc.numeric_preflight(ast,a),calc.numeric_preflight(ast,b))
        self.assertEqual(self.generated(ast,a)["result"],self.generated(ast,b)["result"])

    def test_zero_temperature_difference(self):
        values=slab_inputs(); values["dT"]["value"]="0"
        self.assertEqual(calc.numeric_preflight(slab_ast(),values)["value"],"0")

    def test_divide_by_zero_and_negative_sqrt_categories(self):
        ast=slab_ast(); values=slab_inputs(); values["d"]["value"]="0"
        with self.assertRaisesRegex(ValueError,"divide_by_zero"): calc.numeric_preflight(ast,values)
        self.assertIn("divide_by_zero",self.generated(ast,values)["code"])
        ast=unary_ast({"kind":"sqrt","arg":node("x")}); values={"x":{"value":"-1","unit":"1"}}
        with self.assertRaisesRegex(ValueError,"sqrt_negative"): calc.numeric_preflight(ast,values)
        self.assertIn("sqrt_negative",self.generated(ast,values)["code"])

    def test_negative_power_zero_rejected_before_generated_runtime(self):
        ast=unary_ast({"kind":"power","base":node("x"),"exponent":-1})
        with self.assertRaisesRegex(ValueError,"divide_by_zero"): calc.numeric_preflight(ast,{"x":{"value":"0","unit":"1"}})
        self.assertEqual(self.generated(ast,{"x":{"value":"0","unit":"1"}})["result"]["value"],"Infinity")

    def test_nonfinite_malicious_boolean_and_dimension_errors_on_both_engines(self):
        for value,unit in (("NaN","1"),("Infinity","1"),(True,"1"),("__import__('os').system('no')","1"),("1","m")):
            ast=unary_ast(node("x")); values={"x":{"value":value,"unit":unit}}
            with self.subTest(value=value,unit=unit):
                with self.assertRaises(ValueError): calc.numeric_preflight(ast,values)
                self.assertEqual(self.generated(ast,values)["status"],"error")

    def test_new_bounds_reject_old_finite_huge_values(self):
        ast=unary_ast(node("x")); values={"x":{"value":"1e101","unit":"1"}}
        self.assertEqual(self.generated(ast,values)["status"],"ok")
        with self.assertRaisesRegex(ValueError,"out_of_bounds"): calc.numeric_preflight(ast,values)

    def test_legacy_unit_power_gap_is_closed_by_new_preflight(self):
        ast=unary_ast(node("x")); values={"x":{"value":"1","unit":"m^17/m^17"}}
        # Historical generated runtime admits cancellation of out-of-range powers.
        self.assertEqual(self.generated(ast,values)["status"],"ok")
        with self.assertRaisesRegex(ValueError,"unit_power_invalid"): calc.numeric_preflight(ast,values)

    def test_precision_context_is_local_and_repeatable(self):
        ast=slab_ast(); values=slab_inputs(); values["k"]["value"]="5.1234567890123456789"
        with localcontext() as ctx:
            ctx.prec=7; result=calc.numeric_preflight(ast,values); self.assertEqual(ctx.prec,7)
        with localcontext() as ctx:
            ctx.prec=60; self.assertEqual(calc.numeric_preflight(ast,values),result)
        self.assertEqual(self.generated(ast,values)["result"],result)
