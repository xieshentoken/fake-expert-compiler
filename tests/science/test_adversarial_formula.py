import copy
import unittest
from science.m3_support import slab_ast,slab_inputs,unary_ast,node
import science_calculation as calc


class AdversarialFormulaTests(unittest.TestCase):
    def test_nonfinite_huge_boolean_and_malicious_inputs(self):
        for value in ("NaN","Infinity","1e999999",True,10**1000,"__import__('os').system('false')"):
            inputs=slab_inputs(); inputs["d"]["value"]=value
            with self.subTest(value=str(value)[:30]),self.assertRaises(ValueError): calc.numeric_preflight(slab_ast(),inputs)

    def test_unit_dimension_mismatch_and_unknown_unit(self):
        for unit in ("s","unknown","m^999999"):
            inputs=slab_inputs(); inputs["d"]["unit"]=unit
            with self.subTest(unit=unit),self.assertRaises(ValueError): calc.numeric_preflight(slab_ast(),inputs)

    def test_ast_code_and_unsupported_operators_rejected(self):
        for expression in ({"kind":"call","name":"eval","value":"1"},{"kind":"exp","arg":node("x")},"x + 1"):
            with self.subTest(expression=expression),self.assertRaises(ValueError): calc.numeric_preflight(unary_ast(expression),{"x":{"value":"1","unit":"1"}})

    def test_unbounded_result_and_constant_rejected(self):
        ast=unary_ast({"kind":"power","base":node("x"),"exponent":8})
        with self.assertRaises(ValueError): calc.numeric_preflight(ast,{"x":{"value":"1e30","unit":"1"}})
        ast=unary_ast({"kind":"constant","value":"1e999999","unit":"1"})
        with self.assertRaises(ValueError): calc.numeric_preflight(ast,{"x":{"value":"1","unit":"1"}})

    def test_missing_extra_and_string_inputs_rejected(self):
        for inputs in ({}, {**slab_inputs(),"extra":{"value":1,"unit":"1"}}, "formula string"):
            with self.subTest(inputs=inputs),self.assertRaises(ValueError): calc.numeric_preflight(slab_ast(),inputs)
