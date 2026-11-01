import copy
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
from plan_compiler import compile_plan
from single_api_quality import build_single_api_quality_intent


class SingleApiQualityTests(unittest.TestCase):
    def _profile(self):
        return {
            "method": "POST",
            "path": "/orders",
            "parameters": [
                {"name": "user_id", "required": True, "type": "string"},
                {"name": "quantity", "required": True, "type": "integer", "boundary": {"min": 1, "max": 10}},
            ],
            "response": {"status": 201, "body_fields": ["order_id"]},
            "authentication": {"type": "bearer"},
            "side_effect": "write",
            "teardown": {"type": "http", "request": {"method": "DELETE", "path": "/orders/${order_id}"}, "verify": {"method": "GET", "assertions": [{"path": "$.missing", "equals": True}]}},
            "idempotency_applicable": True,
        }

    def test_builds_design_only_intent_with_required_dimensions(self):
        intent = build_single_api_quality_intent("REQ-1", self._profile(), "/orders", "uat")
        dimensions = {case["dimension"] for case in intent["cases"]}
        self.assertEqual(dimensions, {"positive", "required_parameters", "negative", "boundary", "authentication", "authorization", "response_contract", "idempotency", "side_effect"})
        self.assertEqual(intent["review"]["status"], "pending_review")
        self.assertEqual(intent["entry_type"], "single_api_quality")
        self.assertTrue(intent["cases"][0]["interface_refs"])
        self.assertIn("expected_request_count", intent["cases"][0])
        self.assertTrue(intent["design_fingerprint"].startswith("sha256:"))

    def test_idempotency_is_omitted_when_not_declared_applicable(self):
        profile = self._profile()
        profile["idempotency_applicable"] = False
        intent = build_single_api_quality_intent("REQ-1", profile, "/orders", "uat")
        self.assertNotIn("idempotency", {case["dimension"] for case in intent["cases"]})

    def test_without_ai_dimension_decisions_cases_are_candidates_only(self):
        intent = build_single_api_quality_intent("REQ-1", self._profile(), "/orders", "uat")
        self.assertEqual(intent["scope"]["dimension_decision_status"], "pending_ai_decision")
        self.assertTrue(all(case["applicability"]["candidate_only"] for case in intent["cases"]))
        self.assertTrue(any(gap["gap_id"] == "GAP-SAQ-DIMENSION-DECISION" for gap in intent["known_gaps"]))

    def test_boolean_dimension_decisions_remain_candidates_without_audit_evidence(self):
        decisions = {"positive": True, "required_parameters": True, "negative": True, "boundary": False, "authentication": True, "authorization": False, "response_contract": True, "idempotency": False, "side_effect": True}
        intent = build_single_api_quality_intent("REQ-1", self._profile(), "/orders", "uat", {"ai_dimension_decisions": decisions})
        self.assertEqual({case["dimension"] for case in intent["cases"]}, {"positive", "required_parameters", "negative", "authentication", "response_contract", "side_effect"})
        self.assertEqual(intent["scope"]["dimension_decision_status"], "partial_ai_decision")
        self.assertEqual(intent["tasks"], [])
        self.assertTrue(all(case["applicability"]["candidate_only"] for case in intent["cases"]))

    def test_structured_dimension_decisions_are_executable_candidates(self):
        decisions = {
            dimension: {"applicable": dimension in {"positive", "response_contract"}, "decision_owner": "ai", "decision_evidence_refs": [f"profile:orders:{dimension}@1"]}
            for dimension in ("positive", "required_parameters", "response_contract")
        }
        intent = build_single_api_quality_intent("REQ-1", self._profile(), "/orders", "uat", {"ai_dimension_decisions": decisions, "dimensions": list(decisions)})
        self.assertEqual(intent["scope"]["dimension_decision_status"], "ai_decided")
        self.assertEqual([case["dimension"] for case in intent["tasks"]], ["positive", "response_contract"])
        self.assertTrue(all(not case["applicability"]["candidate_only"] for case in intent["tasks"]))

    def test_missing_facts_become_known_gaps_not_guesses(self):
        intent = build_single_api_quality_intent("REQ-2", {"method": "GET"}, "/orders", "uat")
        self.assertTrue(any(gap["gap_id"] == "GAP-SAQ-RESPONSE" for gap in intent["known_gaps"]))
        self.assertTrue(any(gap["gap_id"] == "GAP-SAQ-PARAMETERS" for gap in intent["known_gaps"]))
        self.assertEqual(intent["cases"][0]["request"]["path"], "/orders")
        self.assertEqual(intent["cases"][0]["assertions"][0]["expected"], "profile_declared_only")

    def test_write_cases_have_object_ref_and_teardown_or_waiver_candidate(self):
        intent = build_single_api_quality_intent("REQ-1", self._profile(), "/orders", "uat")
        for case in intent["cases"]:
            self.assertTrue(case.get("teardown") or case.get("object_ref") or case.get("cleanup_waiver_candidate"))

    def test_output_is_consumable_by_plan_compiler(self):
        intent = build_single_api_quality_intent(
            "REQ-1", self._profile(), "/orders", "uat",
            {"dimensions": ["positive"], "ai_dimension_decisions": {"positive": {"applicable": True, "decision_owner": "ai", "decision_evidence_refs": ["profile:orders@1"]}}},
        )
        plan = compile_plan(intent)
        self.assertEqual(plan["requirement_id"], "REQ-1")
        self.assertEqual(plan["tasks"][0]["case_id"], "SAQ-001-POSITIVE")
        self.assertEqual(plan["mode"], "EXECUTE")

    def test_pending_candidates_are_not_emitted_as_tasks(self):
        intent = build_single_api_quality_intent("REQ-1", self._profile(), "/orders", "uat", {"dimensions": ["positive"]})
        self.assertTrue(intent["cases"])
        self.assertEqual(intent["tasks"], [])

    def test_builder_does_not_mutate_profile(self):
        profile = self._profile()
        original = copy.deepcopy(profile)
        build_single_api_quality_intent("REQ-1", profile, "/orders", "uat")
        self.assertEqual(profile, original)


if __name__ == "__main__":
    unittest.main()
