import copy
import unittest
from science.m2_support import library, tokenizer, request, answer, sc, si
import consultation_contract as cc
import science_reference_runtime as runtime
from compiler_version import SCIENCE_SUPPORT_REVIEW_SCHEMA


class AnswerSupportTests(unittest.TestCase):
    def setUp(self):
        self.lib=library(); self.tok=tokenizer(); self.req=request(self.lib)
        self.index=si.build_index(self.lib,self.tok); self.bundle=runtime.query(self.req,self.lib,self.index,self.tok)
        self.answer=answer(self.req,self.bundle)
        self.args=(self.answer,self.bundle,self.req,self.lib,self.index,self.tok)
        self.plan=cc.prepare_support_review(*self.args, proposer_instance="synthetic-proposer",
            reviewer_instances=["synthetic-reviewer-a","synthetic-reviewer-b"],review_session_id="synthetic-contract-session")

    def receipt(self, who):
        # Uncertain mock protocol input, never an accepted Review or Gold.
        return {"schema_version": SCIENCE_SUPPORT_REVIEW_SCHEMA,
            **{k:self.plan[k] for k in ("plan_sha256","request_sha256","bundle_sha256","answer_sha256","claims_sha256","review_session_id","attestation_level")},
            "reviewer_instance":who,"review_method":"fresh-source-inspection",
            "claims":[{"claim_id":c["claim_id"],"claim_sha256":sc.sha256_json(c),"verdict":"uncertain",
                       "inspected_supports":copy.deepcopy(c["supports"]),"observation":"Synthetic protocol uncertainty " + who} for c in self.answer["claims"]]}

    def test_no_external_review_is_unavailable(self):
        result=cc.validate_support_reviews([],None,*self.args)
        self.assertEqual(result["semantic_support"],"unavailable")
        self.assertFalse(result["semantically_verified"])

    def test_missing_plan_cannot_be_replaced_with_receipt(self):
        with self.assertRaises(ValueError): cc.validate_support_reviews([self.receipt("synthetic-reviewer-a")],None,*self.args)

    def test_explicit_id_and_separation_required(self):
        for reviewers in (["synthetic-proposer","synthetic-reviewer-b"], ["x","x"], ["one"]):
            with self.subTest(reviewers=reviewers), self.assertRaises(ValueError):
                cc.prepare_support_review(*self.args, proposer_instance="synthetic-proposer",reviewer_instances=reviewers,review_session_id="session")

    def test_uncertain_binding_is_not_support_or_truth(self):
        receipts=[self.receipt(who) for who in self.plan["reviewer_instances"]]
        result=cc.validate_support_reviews(receipts,self.plan,*self.args)
        self.assertEqual(result["review_binding_status"],"valid")
        self.assertEqual(result["semantic_support"],"unavailable")
        self.assertFalse(result["production_eligible"])

    def test_partial_review_remains_incomplete(self):
        result=cc.validate_support_reviews([self.receipt("synthetic-reviewer-a")],self.plan,*self.args)
        self.assertEqual(result["review_binding_status"],"incomplete")

    def test_hash_session_claim_and_evidence_drift_rejected(self):
        for field in ("plan_sha256","request_sha256","bundle_sha256","answer_sha256","claims_sha256","review_session_id"):
            receipt=self.receipt("synthetic-reviewer-a"); receipt[field]="a"*64
            with self.subTest(field=field), self.assertRaises(ValueError): cc.validate_support_reviews([receipt],self.plan,*self.args)
        receipt=self.receipt("synthetic-reviewer-a"); receipt["claims"][0]["inspected_supports"]=[]
        with self.assertRaises(ValueError): cc.validate_support_reviews([receipt],self.plan,*self.args)

    def test_unknown_reviewer_and_self_attestation_rejected(self):
        for who in ("unknown-reviewer","synthetic-proposer"):
            with self.subTest(who=who), self.assertRaises(ValueError): cc.validate_support_reviews([self.receipt(who)],self.plan,*self.args)

    def test_duplicate_reviews_and_blanket_observations_rejected(self):
        a=self.receipt("synthetic-reviewer-a"); b=self.receipt("synthetic-reviewer-b")
        with self.assertRaises(ValueError): cc.validate_support_reviews([a,a],self.plan,*self.args)
        b["claims"][0]["observation"]=a["claims"][0]["observation"]
        with self.assertRaises(ValueError): cc.validate_support_reviews([a,b],self.plan,*self.args)

    def test_stale_plan_and_missing_claim_coverage_rejected(self):
        bad=copy.deepcopy(self.plan); bad["answer_sha256"]="a"*64
        with self.assertRaises(ValueError): cc.validate_support_reviews([],bad,*self.args)
        receipt=self.receipt("synthetic-reviewer-a"); receipt["claims"]=[]
        with self.assertRaises(ValueError): cc.validate_support_reviews([receipt],self.plan,*self.args)
