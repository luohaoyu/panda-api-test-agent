import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
L2_SCRIPTS = Path(__file__).resolve().parents[2] / "panda-ai-api-tester-v2" / "scripts"
sys.path.insert(0, str(L2_SCRIPTS))
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from review_adapter import adapt_review, to_l2_review


class ReviewAdapterTests(unittest.TestCase):
    def test_maps_main_agent_fields_to_l2_fields(self):
        result = adapt_review({
            "status": "pending_review",
            "design_fingerprint": "sha256:design",
            "approved_case_ids": ["C1"],
            "reviewed_fields": ["cases", "assertions"],
        })
        self.assertEqual(result["review"]["plan_fingerprint"], "sha256:design")
        self.assertEqual(result["review"]["approved_scope"], ["C1"])
        self.assertEqual(result["review"]["reviewed_fields"], {"C1": ["cases", "assertions"]})
        self.assertTrue(result["pending"])
        self.assertFalse(result["execution_allowed"])

    def test_approved_review_is_checked_by_l2_validator(self):
        package = {"cases": [{"case_id": "C1"}], "known_gaps": []}
        review = {
            "status": "approved",
            "design_fingerprint": "sha256:wrong",
            "approved_case_ids": ["C1"],
            "review_id": "R1",
            "reviewer": "qa",
            "approved_at": "2026-10-31T00:00:00+00:00",
            "reviewed_fields": {"C1": ["request", "assertions"]},
        }
        result = adapt_review(review, package)
        self.assertFalse(result["execution_allowed"])
        self.assertTrue(any(item["code"] == "L2_REVIEW_INVALID" for item in result["errors"]))

    def test_rejected_plan_only_expired_superseded_are_structured_non_executable(self):
        for status in ("rejected", "plan_only", "expired", "superseded"):
            result = adapt_review({"status": status})
            self.assertEqual(result["status"], status)
            self.assertFalse(result["execution_allowed"])
            self.assertFalse(result["ok"])
            self.assertEqual(result["errors"][0]["code"], "TEST_DESIGN_REVIEW_NOT_EXECUTABLE")

    def test_case_reviewed_fields_are_preserved(self):
        review = {
            "status": "partially_approved",
            "approved_scope": ["C1"],
            "plan_fingerprint": "sha256:p",
            "reviewed_fields": {"C1": ["request", "side_effect"]},
        }
        normalized = to_l2_review(review)
        self.assertEqual(normalized["approved_scope"], ["C1"])
        self.assertEqual(normalized["reviewed_fields"]["C1"], ["request", "side_effect"])


if __name__ == "__main__":
    unittest.main()
