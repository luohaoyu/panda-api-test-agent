import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from api_debug import build_api_debug_intent
from plan_compiler import compile_plan


class ApiDebugTests(unittest.TestCase):
    def test_debug_is_one_minimal_reviewable_case(self):
        intent = build_api_debug_intent("REQ-D", {"interface_id": "orders:create", "method": "POST", "path": "/orders"}, "uat", {"http_status": 400, "message": "bad request"})
        self.assertEqual(intent["entry_type"], "api_debug")
        self.assertEqual(len(intent["cases"]), 1)
        self.assertEqual(intent["debug_context"]["retry_policy"], "no_automatic_retry")
        self.assertEqual(intent["review"]["status"], "pending_review")

    def test_debug_intent_compiles(self):
        intent = build_api_debug_intent("REQ-D", {"interface_id": "orders:get", "method": "GET", "path": "/orders/1"}, "uat")
        plan = compile_plan(intent)
        self.assertEqual(plan["tasks"][0]["entry_type"], "api_debug")
        self.assertEqual(plan["mode"], "EXECUTE")


if __name__ == "__main__":
    unittest.main()
