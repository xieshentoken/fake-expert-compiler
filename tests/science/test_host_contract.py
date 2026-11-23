import copy
import unittest
from science.m2_support import library, tokenizer, request, answer, si, sc
from science.support import record
import science_reference_runtime as runtime
import consultation_contract as cc


class HostContractTests(unittest.TestCase):
    def setUp(self):
        self.lib=library(); self.tok=tokenizer(); self.index=si.build_index(self.lib,self.tok)

    def test_query_is_answer_free_and_read_only(self):
        before=copy.deepcopy((self.lib,self.index,self.tok)); req=request(self.lib)
        result=runtime.query(req,self.lib,self.index,self.tok)
        self.assertNotIn("claims",result); self.assertNotIn("answer",result)
        self.assertEqual(before,(self.lib,self.index,self.tok))

    def test_explicit_calculate_intent_grants_no_execution(self):
        req=request(self.lib); req.update(intent="calculate",execution_requested=True)
        result=runtime.query(req,self.lib,self.index,self.tok)
        self.assertFalse(result["assurance"]["execution_authorized"])
        self.assertIn("calculation-unavailable-m2",result["limitations"])

    def test_legacy_keyword_abstain_preserved_for_explain(self):
        req=request(self.lib,"calculate heat conduction")
        result=runtime.query(req,self.lib,self.index,self.tok)
        self.assertEqual(result["behavior"],"abstain")
        self.assertEqual(result["selected_objects"],[])
        self.assertIn("decision_capability_unavailable",result["legacy_responses"][0]["response"]["reason_codes"])

    def test_out_of_scope_abstain_preserved(self):
        result=runtime.query(request(self.lib,"forbidden heat"),self.lib,self.index,self.tok)
        self.assertEqual(result["behavior"],"abstain")

    def test_new_chinese_recall_retains_legacy_lexical_miss(self):
        result=runtime.query(request(self.lib,"热怎么过去"),self.lib,self.index,self.tok)
        self.assertTrue(result["matches"])
        self.assertEqual(result["legacy_responses"][0]["response"]["reason_codes"],["no_supported_reference"])
        self.assertFalse(result["assurance"]["knowledge_verified"])

    def test_assurance_and_warning_preserved_in_validation(self):
        req=request(self.lib); bundle=runtime.query(req,self.lib,self.index,self.tok)
        result=cc.validate_answer(answer(req,bundle),bundle,req,self.lib,self.index,self.tok)
        self.assertEqual(result["assurance"],bundle["assurance"])
        self.assertIn("unreviewed-candidate",result["warnings"])

    def test_missing_sidecar_remains_reference_only(self):
        lib=library([]); index=si.build_index(lib,self.tok)
        result=runtime.query(request(lib),lib,index,self.tok)
        self.assertEqual(result["assurance"]["base_packages"][0]["science_state"],"unavailable")
        self.assertFalse(result["assurance"]["execution_authorized"])

    def test_compare_intent_does_not_consensus_or_average(self):
        req=request(self.lib); req["intent"]="compare"
        result=runtime.query(req,self.lib,self.index,self.tok)
        self.assertNotIn("consensus",result); self.assertNotIn("average",result)

    def test_no_match_is_scope_limited(self):
        result=runtime.query(request(self.lib,"unmatchedword"),self.lib,self.index,self.tok)
        self.assertEqual(result["closure_status"],"incomplete")
        self.assertIn("no-support-in-requested-scope",result["limitations"])
        self.assertIn("scope-only-no-whole-book-absence-claim",result["warnings"])

    def test_oversized_disclosures_return_bounded_abstention(self):
        self.lib[0]["package"]["conflicts"]=[{"id":"conflict-"+str(i),"description":"x"*3000} for i in range(12)]
        result=runtime.query(request(self.lib),self.lib,self.index,self.tok,limits={**runtime.DEFAULT_LIMITS,"max_bytes":8192})
        self.assertTrue(result["closure_truncated"]); self.assertEqual(result["behavior"],"abstain")
        self.assertLessEqual(len(sc.canonical_json(result)),8192)
        self.assertIn("disclosures-truncated-do-not-answer",result["limitations"])
