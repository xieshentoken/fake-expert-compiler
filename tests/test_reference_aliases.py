"""Frozen core queries and an independent bilingual concept/negative set."""
import copy
import unittest

import _support
import reference_search as rs
from test_reference_search import HEAT, catalog, object_entry


def with_aliases(cat):
    cat=copy.deepcopy(cat)
    aliases=rs.load_term_aliases()
    cat['policy']['term_aliases']=aliases
    for row in cat['objects']:
        row['terms'],row['length']=rs._weighted_terms(row,rs.OBJECT_FIELD_WEIGHTS,aliases)
    for row in cat['figures']:
        row['terms'],row['length']=rs._weighted_terms(row,rs.FIGURE_FIELD_WEIGHTS,aliases)
    return cat


class CoreQueries(unittest.TestCase):
    def test_core_queries_resolve_both_languages_and_keep_numeric_specificity(self):
        cat=with_aliases(HEAT)
        cases={'傅里叶定律是什么':'ko-fourier','what is a heat-transfer coefficient':'ko-h',
               'What is the thermal conductivity of copper?':'ko-cu','换热系数':'ko-h',
               '换热强度':'ko-h','流速 物性 几何':'ko-h',
               '铜的导热系数 copper conductivity at 300 K':'ko-cu'}
        for query,expected in cases.items():
            with self.subTest(query=query):
                result=rs.query_package(cat,query)
                self.assertEqual(result['behavior'],'answer')
                self.assertEqual(result['results'][0]['id'],expected)
                self.assertIn('unreviewed-candidate',result['warnings'])

    def test_unavailable_properties_design_values_and_decisions_do_not_answer(self):
        cat=with_aliases(HEAT)
        for query in ('铜的磁阻率是多少','What is the design heat flux for a wall?',
                      'What is the critical heat flux?', '量子涡流等离子',
                      'Design copper heat exchanger for me','What is the recommended choice for me?',
                      'Is copper safe for heat exchangers?'):
            with self.subTest(query=query):
                self.assertEqual(rs.query_package(cat,query)['behavior'],'abstain')

    def test_approval_nouns_are_lookups_when_the_definition_exists(self):
        cat=with_aliases(catalog([object_entry('ko-cert','Safety certification requirements',
            'Safety certification requirements describe evidence for an independent assessment.')]))
        self.assertEqual(rs.query_package(cat,'What are safety certification requirements?')['behavior'],'answer')
        self.assertEqual(rs.query_package(cat,'Certify this design for me')['reason_codes'],['decision_capability_unavailable'])


class IndependentQueries(unittest.TestCase):
    def test_other_concepts_and_negative_queries(self):
        cat=with_aliases(catalog([
            object_entry('re','Reynolds number','Reynolds number expresses the balance of inertial and viscous forces.'),
            object_entry('r','热阻','热阻表示温差与传热速率之间的比例关系。'),
            object_entry('n','Refractive index','Refractive index compares phase propagation in vacuum and material.'),
            object_entry('a','Thermal diffusivity','Thermal diffusivity describes the rate of transient thermal response.')]))
        for query,expected in [('雷诺数是什么','re'),('thermal resistance','r'),('折射率是什么','n'),('热扩散率','a')]:
            with self.subTest(query=query):
                self.assertEqual(rs.query_package(cat,query)['results'][0]['id'],expected)
        for query in ('努塞尔数','Prandtl number','minimum thermal resistance'):
            with self.subTest(query=query):
                self.assertEqual(rs.query_package(cat,query)['behavior'],'abstain')
