import copy
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
L2_DIR = Path(__file__).resolve().parents[2] / "panda-ai-api-tester-v2"
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(L2_DIR / "scripts"))

from api_debug import build_api_debug_intent
from plan_compiler import compile_plan
from requirement_validation import build_requirement_validation_intent
from run_orchestrator import (
    build_requirement_manifest,
    compile_and_create_run,
    reduce_run,
)
from render_requirement_report import render, validate
from single_api_quality import build_single_api_quality_intent
from slice_executor import execute_slice


class ThreeEntryUnifiedE2ETests(unittest.TestCase):
    def _review(self, intent, status="approved", approved_case_ids=None):
        plan = compile_plan(intent)
        case_ids = [task["case_id"] for task in plan["tasks"]]
        scope = list(approved_case_ids or case_ids)
        return {
            "status": status,
            "approved_case_ids": scope,
            "review_id": "REV-E2E-1",
            "reviewer": "qa",
            "approved_at": "2026-11-01T05:10:00+08:00",
            "plan_fingerprint": plan["plan_fingerprint"],
            "reviewed_fields": {case_id: ["request", "assertions", "side_effect"] for case_id in scope},
        }

    def _run_and_report(self, intent, run_id, executor, review=None):
        prepared = copy.deepcopy(intent)
        prepared["review"] = review or self._review(prepared)
        root = Path(tempfile.mkdtemp()) / run_id
        created = compile_and_create_run(prepared, root, run_id=run_id)
        self.assertEqual(created["plan"]["requirement_id"], prepared["requirement_id"])
        executed = execute_slice(root, "S1", task_executor=executor)
        result = reduce_run(root)
        manifest = build_requirement_manifest(root, title="三入口离线 E2E")
        self.assertEqual(validate(manifest), [])
        html = render(manifest)
        self.assertIn("三入口离线 E2E", html)
        self.assertIn(run_id, html)
        return root, executed, result, manifest

    def test_requirement_validation_runs_through_review_execution_and_report(self):
        intent = build_requirement_validation_intent(
            {
                "requirement_id": "REQ-E2E-REQ",
                "acceptance_criteria": [
                    {
                        "ac_id": "AC-DIRECT",
                        "text": "接口返回订单摘要",
                        "interface_verifiability": "direct",
                        "verifiability_decision_owner": "ai",
                        "decision_evidence_refs": ["prd:REQ-E2E-REQ#AC-DIRECT", "contract:orders@1"],
                        "tasks": [{"task_id": "REQ-READ", "type": "business", "method": "GET", "side_effect": "read"}],
                    },
                    {"ac_id": "AC-NONE", "text": "页面展示订单", "interface_verifiability": "none", "recommended_validation_layers": ["UI"]},
                ],
            },
            "local-e2e",
        )
        self.assertEqual([task["task_id"] for task in intent["tasks"]], ["REQ-READ"])
        root, executed, result, manifest = self._run_and_report(
            intent,
            "RUN-ENTRY-REQ",
            lambda task, **_: {"status": "passed", "evidence_ref": "evidence/req-read.json"},
        )
        self.assertEqual(executed["status"], "passed")
        self.assertEqual(result["verdict"], "passed")
        by_id = {item["id"]: item for item in manifest["acceptance_items"]}
        self.assertEqual(by_id["AC-DIRECT"]["verdict"], "passed")
        self.assertEqual(by_id["AC-NONE"]["verdict"], "not_run")
        self.assertEqual(by_id["AC-NONE"]["execution_status"], "not_run")
        self.assertTrue((root / "result.json").is_file())

    def test_single_api_quality_runs_with_structured_ai_decision_and_audit_evidence(self):
        dimensions = ["positive", "response_contract"]
        decisions = {
            dimension: {
                "applicable": True,
                "decision_owner": "ai",
                "decision_evidence_refs": [f"profile:orders:create:{dimension}@1"],
            }
            for dimension in dimensions
        }
        intent = build_single_api_quality_intent(
            "REQ-E2E-SAQ",
            {"method": "GET", "path": "/orders/1", "response": {"status": 200}},
            "/orders/1",
            "local-e2e",
            {"dimensions": dimensions, "ai_dimension_decisions": decisions},
        )
        self.assertEqual(len(intent["tasks"]), 2)
        root, executed, result, manifest = self._run_and_report(
            intent,
            "RUN-ENTRY-SAQ",
            lambda task, **_: {"status": "passed", "evidence_ref": f"evidence/{task['task_id']}.json"},
        )
        self.assertEqual(executed["status"], "passed")
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual({item["verdict"] for item in manifest["acceptance_items"]}, {"passed"})
        self.assertEqual(len(result["task_results"]), 2)
        self.assertTrue((root / "result.json").is_file())

    def test_api_debug_runs_one_minimal_reproduction_without_retry(self):
        intent = build_api_debug_intent(
            "REQ-E2E-DBG",
            {"interface_id": "orders:get", "method": "GET", "path": "/orders/1"},
            "local-e2e",
            {"http_status": 500, "message": "observed"},
            {"expected": {"http_status": 200}, "diagnostic_hypotheses": ["upstream timeout"]},
        )
        self.assertEqual(intent["debug_context"]["retry_policy"], "no_automatic_retry")
        self.assertEqual(len(intent["tasks"]), 1)
        calls = []
        root, executed, result, manifest = self._run_and_report(
            intent,
            "RUN-ENTRY-DBG",
            lambda task, **_: calls.append(task["task_id"]) or {"status": "failed", "evidence_ref": "evidence/debug.json"},
        )
        self.assertEqual(calls, ["DBG-001-REPRO"])
        self.assertEqual(executed["status"], "failed")
        self.assertEqual(result["verdict"], "failed")
        self.assertEqual(manifest["acceptance_items"][0]["verdict"], "failed")
        self.assertEqual(len(intent["cases"]), 1)
        self.assertTrue((root / "result.json").is_file())

    def test_partial_review_executes_only_approved_quality_case(self):
        dimensions = ["positive", "response_contract"]
        decisions = {
            dimension: {"applicable": True, "decision_owner": "ai", "decision_evidence_refs": [f"profile:{dimension}@1"]}
            for dimension in dimensions
        }
        intent = build_single_api_quality_intent(
            "REQ-E2E-PARTIAL",
            {"method": "GET", "path": "/orders/1", "response": {"status": 200}},
            "/orders/1",
            "local-e2e",
            {"dimensions": dimensions, "ai_dimension_decisions": decisions},
        )
        first_case = intent["tasks"][0]["case_id"]
        calls = []
        root, executed, result, _ = self._run_and_report(
            intent,
            "RUN-ENTRY-PARTIAL",
            lambda task, **_: calls.append(task["task_id"]) or {"status": "passed", "evidence_ref": "evidence/partial.json"},
            review=self._review(intent, "partially_approved", [first_case]),
        )
        self.assertEqual(calls, [first_case])
        self.assertEqual(executed["status"], "passed")
        by_id = {item["task_id"]: item for item in result["task_results"]}
        self.assertEqual(by_id[first_case]["status"], "passed")
        second_case = intent["tasks"][1]["task_id"]
        self.assertEqual(by_id[second_case]["status"], "not_run")


if __name__ == "__main__":
    unittest.main()
