import copy
import unittest
from science.m4_support import domain,libraries,tokenizer
import science_index as si
import science_workpack as sw


class DomainProfileTests(unittest.TestCase):
    def test_two_closed_data_only_profiles(self):
        for name in ('heat-transfer','materials-optics'):
            value=domain(name); result=sw.domain_inputs(value)
            self.assertEqual(result['profile']['profile_id'],name)
            self.assertFalse(value['execution_authorized'])
            self.assertTrue(value['question_catalog'])
            self.assertNotIn('code',value)

    def test_heat_is_gated_and_optics_reference_only(self):
        self.assertEqual(domain('heat-transfer')['calculation_policy'],'requires-existing-gates')
        self.assertEqual(domain('materials-optics')['calculation_policy'],'reference-only')

    def test_unknown_code_token_and_capability_fields_rejected(self):
        for field,value in (('code','print(1)'),('token','credential'),('execution_authorized',True)):
            profile=domain('heat-transfer'); profile[field]=value
            with self.assertRaises(ValueError): sw.domain_inputs(profile)

    def test_same_k_search_keeps_domains_and_quantity_kinds(self):
        lib=libraries(); tk=tokenizer(); index=si.build_index(lib,tk); scope=[r['package']['fingerprint'] for r in lib]
        self.assertEqual(len(si.search(index,'k',tk,scope=scope)),2)
        for name,kind in (('heat-transfer','thermal-conductivity'),('materials-optics','extinction-coefficient')):
            rows=si.search(index,'k',tk,scope=scope,domains=[name],quantity_kind=kind)
            self.assertEqual(len(rows),1); self.assertEqual(rows[0]['object_ref']['package_id'],'synthetic-'+name)

    def test_domain_aliases_do_not_change_math_identity(self):
        config=sw.domain_inputs(domain('heat-transfer'))
        self.assertIn('conduction',si.tokens('热怎么过去',config['tokenizer'])['natural'])
        self.assertNotEqual(si.tokens('k',config['tokenizer'])['math'],si.tokens('K',config['tokenizer'])['math'])
