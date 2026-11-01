import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from requirement_validation import build_requirement_validation_intent, classify_acceptance_criteria


class RequirementValidationTests(unittest.TestCase):
    def test_none_acceptance_item_does_not_create_api_case(self):
        intent = build_requirement_validation_intent({"requirement_id": "REQ-1", "acceptance_criteria": [{"ac_id": "AC-1", "title": "页面展示", "interface_verifiability": "none", "recommended_validation_layers": ["UI"]}]}, "uat")
        self.assertEqual(intent["cases"], [])
        self.assertEqual(intent["acceptance_criteria"][0]["interface_verifiability"], "none")
        self.assertTrue(any(gap["gap_id"] == "GAP-AC-1-NON-API" for gap in intent["known_gaps"]))

    def test_partial_keeps_interface_and_non_interface_scope(self):
        criteria = classify_acceptance_criteria([{"id": "AC-2", "interface_scope": ["backend state"], "non_interface_scope": ["client display"]}])
        self.assertEqual(criteria[0]["interface_verifiability"], "partial")
        self.assertEqual(criteria[0]["interface_scope"], ["backend state"])
        self.assertEqual(criteria[0]["non_interface_scope"], ["client display"])

    def test_direct_acceptance_keeps_declared_task(self):
        intent = build_requirement_validation_intent({"requirement_id": "REQ-2", "acceptance_criteria": [{"id": "AC-1", "interface_verifiability": "direct", "tasks": [{"task_id": "T1", "type": "business", "method": "GET", "side_effect": "read"}]}]}, "uat")
        self.assertEqual(intent["tasks"][0]["acceptance_refs"], ["AC-1"])
        self.assertEqual(intent["acceptance_criteria"][0]["verifiability_decision_status"], "ai_decided")
        self.assertEqual(intent["review"]["status"], "pending_review")

    def test_scope_fields_alone_do_not_make_semantic_decision(self):
        intent = build_requirement_validation_intent({"requirement_id": "REQ-3", "acceptance_criteria": [{"id": "AC-1", "interface_scope": ["request accepted"]}]}, "uat")
        criterion = intent["acceptance_criteria"][0]
        self.assertEqual(criterion["verifiability_decision_status"], "pending_ai_decision")
        self.assertIsNone(criterion["interface_verifiability"])
        self.assertEqual(intent["tasks"], [])
        self.assertTrue(any(gap["gap_id"] == "GAP-AC-1-AI-DECISION" for gap in intent["known_gaps"]))


if __name__ == "__main__":
    unittest.main()
