import copy
import unittest
from science.m4_support import libraries,tokenizer
from science.support import sc
import science_index as si
import pipeline_orchestrator as pipe
import incremental_build_dag as dag
from science.m3_support import calculation_fixture
import science_calculation as calc

STAMP='2026-09-19T00:00:00+00:00'


class ScienceInvalidationTests(unittest.TestCase):
    def snapshot(self,lib,tk=None):
        tk=tk or tokenizer()
        return pipe.build_science_snapshot(lib,tk,si.build_index(lib,tk),generated_at=STAMP)

    def test_original_dag_accepts_versioned_science_snapshot_and_noop(self):
        snapshot=self.snapshot(libraries()); pipe.validate_science_snapshot(snapshot)
        comparison=dag.compare_snapshots(snapshot,snapshot)
        self.assertFalse(comparison['changes']['fail_closed'])
        self.assertEqual(comparison['changes']['invalidations'],[])

    def test_tokenizer_change_invalidates_query_certificate_not_source(self):
        lib=libraries(); old=self.snapshot(lib); tk=tokenizer(); tk['aliases'].append({'phrase':'保温','terms':['insulation']})
        new=self.snapshot(lib,tk); result=dag.compare_snapshots(old,new)
        affected={r['node_id'] for r in result['changes']['invalidations']}
        for node in new['nodes']:
            if node['kind']=='source':self.assertNotIn(node['node_id'],affected)
            if node['kind']=='host_certificate':self.assertIn(node['node_id'],affected)
        self.assertFalse(any('re-extract' in r['required_actions'] for r in result['changes']['invalidations']))

    def test_record_edit_invalidates_sidecar_without_pdf_extraction(self):
        lib=libraries(); old=self.snapshot(lib)
        item=lib[0]; row=item['sidecar']['records'][0]; row['display_latex']='k_{x}'
        item['sidecar']=sc.make_sidecar([sc.bind_record(row)],item['package'],item['profile'],data_origin='synthetic')
        new=self.snapshot(lib); result=dag.compare_snapshots(old,new)
        self.assertTrue(result['changes']['invalidations'])
        source_ids={r['node_id'] for r in new['nodes'] if r['kind']=='source'}
        self.assertFalse(source_ids & {r['node_id'] for r in result['changes']['invalidations']})

    def test_tampered_snapshot_rejected_by_existing_loader(self):
        value=self.snapshot(libraries()); value['nodes'][0]['content_hash']='0'*64
        with self.assertRaises((ValueError,dag.IncrementalDagError)):dag.build_dag(value)

    def test_source_snapshots_unchanged_by_comparison(self):
        old=self.snapshot(libraries()); saved=copy.deepcopy(old); dag.compare_snapshots(old,old)
        self.assertEqual(old,saved)

    def test_property_change_invalidates_related_receipt_and_not_other_source(self):
        # Generate a receipt-shaped synthetic observation without Review/Gold
        # or a production execution claim; this graph checks dependency data.
        p,spec,req,side,prof=calculation_fixture(use_property=True)
        plan=calc.build_plan(spec,req,p,side,prof)
        result=calc.numeric_preflight(p['objects'][0]['formula']['ast'],{r['symbol']:{'value':r['decimal_value'],'unit':r['normalized_quantity']['unit']} for r in plan['input_bindings']})
        ctx={'plan':plan,'selected':{'module_sha256':'a'*64},'bundle':{'runner':{'sha256':'b'*64}}}
        receipt=calc._receipt(ctx,{'execution_gate_refs':[{'gate':'synthetic-test-only','sha256':'c'*64}]},result,{'status':'ok','result':result})
        lib=[{'package':p,'sidecar':side,'profile':prof,'catalog':None}]+libraries()
        tk=tokenizer(); old=pipe.build_science_snapshot(lib,tk,si.build_index(lib,tk),receipts=[receipt],generated_at=STAMP)
        saved=copy.deepcopy(receipt)
        rows=copy.deepcopy(side['records']); observed=next(r for r in rows if r['kind']=='PropertyObservation')
        observed['value_representation']['value']=6
        previous=observed['id']; changed_observation=sc.bind_record(observed)
        for row in rows:
            if row['kind']=='FormulaCard':row['dependency_refs']=[changed_observation['id'] if value==previous else value for value in row['dependency_refs']]
        rows=[sc.bind_record(r) for r in rows]
        lib[0]['sidecar']=sc.make_sidecar(rows,p,prof,data_origin='synthetic')
        new=pipe.build_science_snapshot(lib,tk,si.build_index(lib,tk),receipts=[receipt],generated_at=STAMP)
        changed=dag.compare_snapshots(old,new)['changes']; affected={r['node_id'] for r in changed['invalidations']}
        self.assertTrue(any(r['kind']=='receipt' and r['node_id'] in affected for r in new['nodes']))
        self.assertFalse(any(r['kind']=='source' and r['node_id'] in affected for r in new['nodes']))
        self.assertEqual(receipt,saved)

    def test_profile_change_invalidates_science_review_and_query_certificate(self):
        lib=libraries(); old=self.snapshot(lib); lib[0]['profile']['version']='0.2.0'
        lib[0]['sidecar']=sc.make_sidecar(lib[0]['sidecar']['records'],lib[0]['package'],lib[0]['profile'],data_origin='synthetic')
        new=self.snapshot(lib); affected={r['node_id'] for r in dag.compare_snapshots(old,new)['changes']['invalidations']}
        self.assertTrue(any(r['kind']=='review_plan' and r['node_id'] in affected for r in new['nodes']))
        self.assertTrue(any(r['kind']=='host_certificate' and r['node_id'] in affected for r in new['nodes']))

    def test_rehashed_capability_upgrade_rejected(self):
        snapshot=self.snapshot(libraries()); snapshot['nodes'][0]['capabilities']['execution_authorized']=True
        snapshot['snapshot_sha256']=sc.sha256_json({k:v for k,v in snapshot.items() if k!='snapshot_sha256'})
        with self.assertRaisesRegex(ValueError,'capability_forbidden'):dag.build_dag(snapshot)

    def test_explicit_formula_condition_and_symbol_edges_exist(self):
        p,_,_,side,prof=calculation_fixture(); lib=[{'package':p,'sidecar':side,'profile':prof,'catalog':None}]
        snapshot=self.snapshot(lib); formula=next(r for r in side['records'] if r['kind']=='FormulaCard')
        target=next(n['node_id'] for n in snapshot['nodes'] if n['stable_key'].endswith(formula['id']))
        origins={e['from'] for e in snapshot['edges'] if e['to']==target}
        for record_id in formula['symbol_bindings']+[formula['condition_set_ref']]:
            node=next(n for n in snapshot['nodes'] if n['stable_key'].endswith(record_id)); self.assertIn(node['node_id'],origins)
