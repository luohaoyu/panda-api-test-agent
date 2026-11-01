import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from intent_router import route_intent


class IntentRouterTests(unittest.TestCase):
    def test_routes_requirement_validation(self):
        result = route_intent("按 PRD 验收这个新功能")
        self.assertEqual(result["intent"], "requirement_validation")
        self.assertEqual(result["candidate_intent"], "requirement_validation")
        self.assertTrue(result["fallback_only"])
        self.assertEqual(result["decision_owner"], "ai_skill")
        self.assertTrue(result["requires_design_review"])
        self.assertFalse(result["request_sent"])

    def test_routes_explicit_metadata_without_reading_secret_values(self):
        result = route_intent(
            "帮我处理这个接口",
            {"intent": "api_debug", "token": "should-not-appear", "target": "/orders"},
        )
        self.assertEqual(result["intent"], "api_debug")
        self.assertNotIn("token", result["metadata"])
        self.assertNotIn("should-not-appear", repr(result))

    def test_routes_quality_and_probe(self):
        self.assertEqual(route_intent("检查这个接口质量") ["intent"], "single_api_quality")
        self.assertEqual(route_intent("做环境健康检查") ["intent"], "environment_probe")

    def test_ambiguous_or_unknown_does_not_guess(self):
        result = route_intent("帮我看看")
        self.assertIsNone(result["intent"])
        self.assertFalse(result["supported"])
        self.assertEqual(result["confidence"], "low")


if __name__ == "__main__":
    unittest.main()
