import copy
import unittest
from unittest.mock import patch
from science.support import sc, record, sidecar, context, profile, reseal
import science_workpack as sw


class ReviewBindingTests(unittest.TestCase):
    def freeze(self, value=None):
        return sw.freeze_proposal(value or sidecar(), context(), profile(), proposer_instance="synthetic-test-proposer")

    def test_freeze_is_deterministic_answer_free_and_pending(self):
        a = self.freeze(); self.assertEqual(a, self.freeze())
        self.assertEqual(a["status"], "paused-independent-review")
        self.assertIsNone(a["review_plan"])
        self.assertNotIn("attestations", a)
        self.assertNotIn("gold", a)
        self.assertTrue(all(r["required_reviewer_count"] == 2 for r in a["queue"]))

    def test_explicit_identity_required_no_default_reviewer(self):
        with self.assertRaises(ValueError): sw.freeze_proposal(sidecar(), context(), profile(), proposer_instance="")
        with self.assertRaises(ValueError): sw.freeze_proposal(sidecar(), context(), profile(), proposer_instance="synthetic-test-proposer", reviewer_instances=["same"], review_session_id="")

    def test_proposer_cannot_be_reviewer(self):
        with self.assertRaises(ValueError): sw.freeze_proposal(sidecar(), context(), profile(), proposer_instance="same", reviewer_instances=["same", "other"], review_session_id="synthetic-session")

    def test_field_change_invalidates_old_freeze(self):
        frozen = self.freeze(); value = sidecar()
        value["records"][0]["display_latex"] = "\\kappa"
        value["records"][0] = sc.bind_record(value["records"][0]); reseal(value)
        with self.assertRaisesRegex(ValueError, "stale|drift"):
            sw.validate_frozen(frozen, value, context(), profile())

    def test_freeze_tamper_and_lower_review_count_rejected(self):
        frozen = self.freeze(); frozen["queue"][0]["required_reviewer_count"] = 1
        with self.assertRaises(ValueError): sw.validate_frozen(frozen, sidecar(), context(), profile())

    def test_no_review_or_gold_is_authored(self):
        frozen = self.freeze()
        result = sw.validate_review(frozen, sidecar(), context(), profile(), [])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["knowledge_verified"])
        self.assertFalse(result["executable"])

    def test_existing_attestation_validator_is_used(self):
        frozen = sw.freeze_proposal(sidecar(), context(), profile(), proposer_instance="synthetic-proposer",
            reviewer_instances=["synthetic-reviewer-a", "synthetic-reviewer-b"], review_session_id="synthetic-session")
        with patch.object(sw, "validate_attestations", wraps=sw.validate_attestations) as checker:
            result = sw.validate_review(frozen, sidecar(), context(), profile(), [])
        checker.assert_called_once()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("reviewer_coverage_insufficient", result["reason_codes"])

    def test_original_review_cannot_endorse_new_semantics(self):
        frozen = sw.freeze_proposal(sidecar(), context(), profile(), proposer_instance="synthetic-proposer",
            reviewer_instances=["synthetic-reviewer-a", "synthetic-reviewer-b"], review_session_id="synthetic-session")
        # Deliberately incomplete foreign receipt, never an authored accepted Review.
        result = sw.validate_review(frozen, sidecar(), context(), profile(), [{"schema_version": "tkc.review-attestation/v0.1", "item_ref": "old-base-object"}])
        self.assertEqual(result["status"], "blocked")

    def test_each_semantic_field_has_its_own_assertion_commitment(self):
        frozen = self.freeze()
        self.assertEqual(len(frozen["assertions"]), len(sidecar()["records"][0]["provenance"]))
        self.assertTrue(all(a["risk_tier"] == 2 for a in frozen["assertions"]))
