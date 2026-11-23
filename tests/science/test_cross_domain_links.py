import copy
import unittest
from science.m4_support import libraries,proposals,same_quantity
from science.support import sc,record,quantity,unknown
import science_workpack as sw

STAMP='2026-09-19T00:00:00+00:00'


class CrossDomainLinkTests(unittest.TestCase):
    def build(self,lib,proposal=None,**kwargs):
        return sw.build_cross_links(lib,proposal or proposals(lib),proposer_instance='synthetic-proposer',created_at=STAMP,**kwargs)

    def test_same_glyph_different_quantity_never_equivalent(self):
        result=self.build(libraries())
        self.assertEqual(result['alignments'],[])
        self.assertIn('quantity_kind_mismatch',result['deferred_pairs'][0]['reason_codes'])

    def test_explicit_candidate_preserves_both_source_identities(self):
        lib=same_quantity(libraries()); result=self.build(lib)
        self.assertEqual(len(result['alignments']),1)
        self.assertEqual(result['alignments'][0]['resolution'],'preserve-separate-no-consensus')
        self.assertFalse(result['alignments'][0]['promotion_eligible'])
        self.assertEqual(result['alignments'][0]['review_requirement']['required_reviewer_count'],2)
        self.assertEqual(len({r['endpoint']['base_package']['package_id'] for r in result['endpoints']}),2)
        self.assertEqual(result['status'],'paused-independent-review')

    def test_empty_named_registry_never_produces_review(self):
        lib=same_quantity(libraries()); proposal=proposals(lib); result=self.build(lib,proposal)
        check=sw.validate_cross_links(result,lib,proposal,[])
        self.assertEqual(check['status'],'blocked'); self.assertFalse(check['execution_authorized'])

    def test_explicit_two_reviewers_still_require_external_fragments(self):
        lib=same_quantity(libraries()); proposal=proposals(lib)
        result=self.build(lib,proposal,reviewer_instances=['synthetic-a','synthetic-b'],review_session_id='synthetic-session')
        self.assertEqual(sw.validate_cross_links(result,lib,proposal,[])['status'],'blocked')

    def test_self_duplicate_and_single_reviewer_rejected(self):
        for names in (['synthetic-proposer','other'],['one'],['same','same']):
            with self.assertRaises(ValueError):self.build(same_quantity(libraries()),reviewer_instances=names,review_session_id='session')

    def test_hash_drift_and_self_pairs_rejected(self):
        lib=same_quantity(libraries()); prop=proposals(lib); prop['pairs'][0]['left']['record_ref']['sha256']='a'*64
        with self.assertRaises(ValueError):self.build(lib,prop)
        prop=proposals(lib); prop['pairs'][0]['right']=copy.deepcopy(prop['pairs'][0]['left'])
        with self.assertRaises(ValueError):self.build(lib,prop)

    def test_candidate_rehash_cannot_hide_semantic_modification(self):
        lib=same_quantity(libraries()); prop=proposals(lib); result=self.build(lib,prop)
        result['alignments'][0]['relation_type']='alias_candidate'
        result['links_sha256']=sc.sha256_json({k:v for k,v in result.items() if k!='links_sha256'})
        with self.assertRaises(ValueError):sw.validate_cross_links(result,lib,prop,[])

    def test_unknown_scope_cannot_be_compared_even_with_same_name(self):
        lib=same_quantity(libraries()); row=lib[0]['sidecar']['records'][0]; row['scope_ref']=unknown()
        for p in row['provenance']:
            if p['field']=='scope_ref':p['status']='unknown'
        lib[0]['sidecar']=sc.make_sidecar([sc.bind_record(row)],lib[0]['package'],lib[0]['profile'],data_origin='synthetic')
        result=self.build(lib); self.assertFalse(result['alignments']); self.assertIn('scope_unknown',result['deferred_pairs'][0]['reason_codes'])

    def property_context(self):
        material=record('MaterialState',**{key:{'status':'known','value':'synthetic '+key} for key in
            ('material_identity','sample_identity','orientation_frame','processing_history','phase_microstructure')},
            composition={'status':'known','basis':'mass','scale':'fraction','components':[{'component':'synthetic','value':1}]})
        observation=record('PropertyObservation',material_state_ref=material['id'],method_ref=material['source_object_ref'],
            measurement_conditions=[{'name':'temperature','value':quantity(25,'degC','temperature')}])
        entry={'sidecar':{'records':[material,observation]},'profile':{'required_measurement_conditions':['temperature']}}
        return observation,entry

    def test_different_sample_temperature_and_method_are_not_contradictions(self):
        for field in ('sample_identity','temperature','method_ref','orientation_frame'):
            left,le=self.property_context(); right,re_=copy.deepcopy((left,le))
            if field in ('sample_identity','orientation_frame'):re_['sidecar']['records'][0][field]['value']='different synthetic value'
            elif field=='temperature':right['measurement_conditions'][0]['value']['value']=50
            else:right['method_ref']['object_id']='other-synthetic-method'
            gaps=sw._comparison_gaps(left,right,le,re_)
            self.assertTrue(gaps,field)
            self.assertTrue(any('mismatch' in code for code in gaps),gaps)
            self.assertEqual(left['value_representation']['value'],5)

    def test_equivalent_temperature_units_are_context_not_a_value_average(self):
        left,le=self.property_context(); right,re_=copy.deepcopy((left,le))
        right['measurement_conditions'][0]['value']=quantity(298.15,'K','temperature')
        right['value_representation']['value']=7
        self.assertEqual(sw._comparison_gaps(left,right,le,re_),[])
        self.assertEqual((left['value_representation']['value'],right['value_representation']['value']),(5,7))

    def test_nested_unknown_material_context_defers_comparison(self):
        left,le=self.property_context(); right,re_=copy.deepcopy((left,le))
        for entry in (le,re_):entry['sidecar']['records'][0]['conditions']=[{'name':'temperature','value':unknown()}]
        self.assertIn('state_conditions_unknown',sw._comparison_gaps(left,right,le,re_))

    def test_link_input_and_sources_not_modified(self):
        lib=same_quantity(libraries()); prop=proposals(lib,'contradicts'); before=copy.deepcopy((lib,prop))
        result=self.build(lib,prop)
        self.assertEqual((lib,prop),before); self.assertEqual(result['alignments'][0]['relation_type'],'contradicts_candidate')

    def test_formula_symbol_and_condition_context_required(self):
        from science.m3_support import calculation_fixture
        _,_,_,side,prof=calculation_fixture(); entry={'sidecar':side,'profile':prof}
        formula=next(r for r in side['records'] if r['kind']=='FormulaCard')
        other=copy.deepcopy(entry); changed=next(r for r in other['sidecar']['records'] if r['kind']=='SymbolBinding')
        changed['quantity_kind']='different-synthetic-quantity'
        self.assertIn('formula_symbol_context_mismatch',sw._comparison_gaps(formula,formula,entry,other))
        empty=copy.deepcopy(formula); empty['symbol_bindings']=[]
        self.assertIn('formula_symbol_context_missing',sw._comparison_gaps(empty,empty,entry,entry))
