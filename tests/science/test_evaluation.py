import copy
import unittest
from science.m4_support import suite,observations,libraries
from science.support import evidence_ref,sc
import science_acceptance as sa


class ScienceEvaluationTests(unittest.TestCase):
    def test_seven_layers_keep_denominators_and_no_domain_accuracy(self):
        s=suite(); report=sa.evaluate(s,observations(s))
        self.assertEqual(len(report['layers']),7)
        self.assertEqual(sum(r['planned'] for r in report['layers']),7)
        self.assertEqual(sum(r['passed'] for r in report['layers']),7)
        self.assertIsNone(report['domain_accuracy'])
        self.assertEqual(report['domain_accuracy_status'],'blocked')

    def test_missing_skipped_failed_blocked_are_not_removed_from_denominator(self):
        s=suite(); results=observations(s); results['rows'].pop()
        for row,status in zip(results['rows'],('failed','skipped','blocked')):row['status']=status; row['reason_codes']=['synthetic-outcome']
        report=sa.evaluate(s,results); counts={k:sum(r[k] for r in report['layers']) for k in ('planned','failed','skipped','blocked','not_run')}
        self.assertEqual(counts,{'planned':7,'failed':1,'skipped':1,'blocked':1,'not_run':1})

    def test_stale_duplicate_unknown_and_evidence_free_results_rejected(self):
        for mode in ('hash','duplicate','unknown','evidence'):
            s=suite(); r=observations(s)
            if mode=='hash':r['suite_sha256']='a'*64
            elif mode=='duplicate':r['rows'].append(copy.deepcopy(r['rows'][0]))
            elif mode=='unknown':r['rows'][0]['case_id']='unknown'
            else:r['rows'][0]['evidence_sha256']=None
            with self.assertRaises(ValueError):sa.evaluate(s,r)

    def test_synthetic_cannot_enter_independent_blind_split(self):
        s=suite(); s['cases'][0]['split']='blind'
        with self.assertRaises(ValueError):sa.validate_suite(s)

    def test_same_source_formula_or_sample_cannot_cross_split(self):
        for field in ('source_sha256','formula_family','material_sample'):
            s=suite(); a=s['cases'][0]; a['groups']['material_sample']='synthetic-sample-a'
            b=copy.deepcopy(s['cases'][1]); b.update(case_id='invalid-source-claim',split='blind',data_origin='source-backed',evidence_refs=[evidence_ref()])
            b['groups'].update(source_sha256=sc.sha256_json('isolated invalid blind source'),formula_family='separate-blind-family')
            b['groups']['material_sample']='synthetic-sample-b'; b['groups'][field]=a['groups'][field]; s['cases'].append(b)
            # Deliberately invalid production metadata probes grouping before
            # source validation; it is never Review, Gold, or an accepted source.
            with self.assertRaisesRegex(ValueError,'group_leak:'+field):sa.validate_suite(s)

    def test_suite_rejects_hidden_answers_and_executable_payloads(self):
        for key in ('expected_answer','code','gold'):
            s=suite(); s['cases'][0][key]='private hidden answer'
            with self.assertRaises(ValueError):sa.validate_suite(s)

    def test_synthetic_library_cannot_be_relabeled_as_real_blind(self):
        s=suite(); lib=libraries(); case=s['cases'][0]
        case.update(split='blind',data_origin='source-backed',evidence_refs=lib[0]['sidecar']['records'][0]['source_evidence_refs'])
        case['groups']['source_sha256']=lib[0]['package']['manifest']['sources'][0]['sha256']
        with self.assertRaisesRegex(ValueError,'real_source_binding_required'):sa.evaluate(s,observations(s),library=lib)

    def test_blind_group_hash_must_match_its_evidence_source_not_another_package_source(self):
        s=suite(); lib=libraries(); entry=lib[0]; package=entry['package']
        wrong_hash=sc.sha256_json('different synthetic source')
        package['manifest']['sources'].append({'id':'other-synthetic-source','sha256':wrong_hash})
        package['fingerprint']['manifest_sha256']=sc.sha256_json(package['manifest'])
        entry['sidecar']=sc.make_sidecar(entry['sidecar']['records'],package,entry['profile'],data_origin='synthetic')
        case=s['cases'][0]; case.update(split='blind',data_origin='source-backed',evidence_refs=entry['sidecar']['records'][0]['source_evidence_refs'])
        case['groups']['source_sha256']=wrong_hash
        with self.assertRaisesRegex(ValueError,'source_hash_mismatch'):sa.evaluate(s,observations(s),library=lib)

    def test_blank_review_has_no_decisions_and_never_clears_accuracy(self):
        s=suite(); review=sa.freeze_review(s,sc.sha256_json('synthetic not Gold'),proposer_instance='synthetic-author',
            reviewer_instances=['synthetic-r1','synthetic-r2'],review_session_id='synthetic-session',created_at='2026-09-19T00:00:00Z')
        self.assertFalse(review['verified_gold']); self.assertEqual(review['review_plan']['items'],[])
        report=sa.evaluate(s,observations(s),review=review,attestations=[])
        self.assertEqual(report['domain_accuracy_status'],'blocked'); self.assertIsNone(report['domain_accuracy'])

    def test_evaluator_separation_and_review_drift_fail_closed(self):
        s=suite(); review=sa.freeze_review(s,'a'*64,proposer_instance='synthetic-test-runner',
            reviewer_instances=['synthetic-r1','synthetic-r2'],review_session_id='synthetic-session',created_at='2026-09-19T00:00:00Z')
        self.assertIn('independent_evaluator_required',sa.evaluate(s,observations(s),review=review)['reason_codes'])
        review['gold_payload_sha256']='b'*64
        with self.assertRaisesRegex(ValueError,'stale_or_modified'):sa.evaluate(s,observations(s),review=review)

    def test_blind_export_excludes_development_and_no_hidden_answers(self):
        value=sa.blind_plan(suite()); self.assertEqual(value['cases'],[]); self.assertFalse(value['contains_answers'])

    def test_skips_require_reason_and_case_hash_is_checked(self):
        s=suite(); result=observations(s); result['rows'][0]['status']='skipped'
        with self.assertRaisesRegex(ValueError,'reason_required'):sa.evaluate(s,result)
        result=observations(s); result['rows'][0]['case_sha256']='a'*64
        with self.assertRaisesRegex(ValueError,'hash_invalid'):sa.evaluate(s,result)

    def test_report_keeps_every_case_and_minimum_blind_requirement(self):
        s=suite(); result=observations(s); result['rows']=[]; report=sa.evaluate(s,result)
        self.assertEqual(len(report['cases']),7)
        self.assertEqual({c['status'] for c in report['cases']},{'not_run'})
        self.assertIn('blind_minimum_100_and_20_per_category_required',report['reason_codes'])
