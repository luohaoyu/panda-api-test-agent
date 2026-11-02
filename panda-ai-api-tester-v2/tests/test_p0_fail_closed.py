"""Regression tests for the fail-closed gates and cleanup semantics.

Each case here reproduces a previously observed false-green path:
1. a run whose review fields were missing still executed;
2. a writer with a declared cleanup obligation was treated as cleaned because the
   writer itself passed;
3. runner-declared residual objects never reached the durable resource ledger;
4. a rejected review could still be reached by a slice carrying a token.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from plan_compiler import compile_plan
from result_reducer import load_events, reduce_run
from run_state import create_run, load_state
from slice_executor import ApprovalRequired, _record_runner_residuals, execute_slice, review_clearance


def _intent(tasks, requirement_id="REQ-P0"):
    return {"requirement_id": requirement_id, "acceptance_criteria": [{"ac_id": "AC-1"}], "tasks": tasks}


def _create(root, plan, review=None):
    metadata = None
    if review is not None:
        metadata = {"entry_type": "single_api_quality", "review": review}
    create_run(
        root,
        run_id="RUN-P0",
        plan_id=plan["plan_id"],
        plan_fingerprint=plan["plan_fingerprint"],
        environment_snapshot={"environment_id": "test"},
        plan=plan,
        entry_metadata=metadata,
    )


def _approved(root, plan):
    case_ids = [task["case_id"] for task in plan["tasks"] if task.get("case_id")]
    _create(root, plan, {"status": "approved", "review_id": "REV-P0", "approved_case_ids": case_ids})


class ReviewGateFailClosedTests(unittest.TestCase):
    def test_review_clearance_treats_missing_state_as_unreviewed(self):
        self.assertEqual(review_clearance({})[0], "unreviewed")
        self.assertEqual(review_clearance({"review_status": ""})[0], "unreviewed")
        self.assertEqual(review_clearance({"review_status": "approved"})[0], "executable")
        self.assertEqual(review_clearance({"review_status": "pending_review"})[0], "awaiting_approval")
        self.assertEqual(review_clearance({"review_status": "rejected"})[0], "not_executable")

    def test_legacy_state_without_review_fields_cannot_execute(self):
        plan = compile_plan(_intent([{"task_id": "WRITE", "type": "business", "method": "POST", "side_effect": "write", "object_ref": "O-1"}]))
        root = Path(tempfile.mkdtemp()) / "run"
        _create(root, plan)
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        state.pop("review_status", None)
        state.pop("entry_type", None)
        state["status"] = "ready"
        (root / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        called = []
        with self.assertRaises(ApprovalRequired):
            execute_slice(root, "S1", task_executor=lambda task, **_: called.append(task["task_id"]) or {"status": "passed"})
        self.assertEqual(called, [])
        self.assertEqual(load_state(root)["status"], "blocked")
        events = load_events(root / "events.jsonl")
        self.assertTrue(any(event.get("type") == "slice_blocked_unreviewed" for event in events))

    def test_terminal_review_status_cannot_run_even_with_token(self):
        plan = compile_plan(_intent([{"task_id": "DELETE", "type": "business", "method": "DELETE", "side_effect": "destructive", "object_ref": "O-1"}]))
        root = Path(tempfile.mkdtemp()) / "run"
        _create(root, plan, {"status": "rejected", "review_id": "REV-NO", "approved_case_ids": []})
        with self.assertRaises(ApprovalRequired):
            execute_slice(root, "S1", approval_token="token-123", task_executor=lambda task, **_: {"status": "passed"})
        self.assertEqual(load_state(root)["status"], "blocked")

    def test_approved_run_executes_without_per_slice_token(self):
        plan = compile_plan(_intent([{"task_id": "READ", "type": "business", "method": "GET", "side_effect": "read"}]))
        root = Path(tempfile.mkdtemp()) / "run"
        _approved(root, plan)
        result = execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/read.json"})
        self.assertEqual(result["status"], "passed")


class CleanupSemanticsTests(unittest.TestCase):
    def test_writer_pass_is_not_proof_of_cleanup(self):
        plan = compile_plan(_intent([{"task_id": "CREATE", "type": "business", "method": "POST", "side_effect": "write", "object_ref": {"type": "order", "reference_no": "R-1"}}]))
        root = Path(tempfile.mkdtemp()) / "run"
        _approved(root, plan)
        execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/create.json"})
        result = reduce_run(root)
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertEqual(result["cleanup"]["status"], "pending")
        self.assertFalse(result["cleanup"]["tasks_complete"])
        self.assertIn("CREATE", result["cleanup"]["unresolved_obligations"])

    def test_pending_resource_blocks_even_when_business_passed(self):
        plan = compile_plan(_intent([{"task_id": "CREATE", "type": "business", "method": "POST", "side_effect": "write", "object_ref": "O-1"}]))
        root = Path(tempfile.mkdtemp()) / "run"
        _approved(root, plan)
        execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/create.json", "resource": {"object_id": "O-1", "cleanup_status": "pending"}})
        result = reduce_run(root)
        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["cleanup"]["status"], "blocked")
        self.assertEqual(len(result["cleanup"]["pending_objects"]), 1)

    def test_passed_cleanup_task_closes_the_obligation(self):
        plan = compile_plan(_intent([
            {"task_id": "CREATE", "type": "business", "method": "POST", "side_effect": "write", "object_ref": "O-1"},
            {"task_id": "CLEAN", "type": "cleanup", "action": "delete", "object_ref": "O-1", "depends_on": ["CREATE"]},
        ]))
        root = Path(tempfile.mkdtemp()) / "run"
        _approved(root, plan)

        def executor(task, **_):
            if task["type"] == "cleanup":
                return {"status": "passed", "evidence_ref": "evidence/clean.json", "resource": {"object_id": "O-1", "cleanup_status": "cleaned"}}
            return {"status": "passed", "evidence_ref": "evidence/create.json", "resource": {"object_id": "O-1", "cleanup_status": "pending"}}

        slices = [item["slice_id"] for item in plan["slices"]]
        for slice_id in slices:
            tasks = next(item for item in plan["slices"] if item["slice_id"] == slice_id)["task_ids"]
            if not tasks:
                continue
            execute_slice(root, slice_id, task_executor=executor)
        result = reduce_run(root)
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["cleanup"]["status"], "complete")
        self.assertTrue(result["cleanup"]["tasks_complete"])

    def test_declared_but_unexecuted_cleanup_task_blocks(self):
        plan = compile_plan(_intent([
            {"task_id": "CREATE", "type": "business", "method": "POST", "side_effect": "write", "object_ref": "O-1"},
            {"task_id": "CLEAN", "type": "cleanup", "action": "delete", "object_ref": "O-1", "depends_on": ["CREATE"]},
        ]))
        root = Path(tempfile.mkdtemp()) / "run"
        _approved(root, plan)
        execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/create.json"})
        result = reduce_run(root)
        self.assertEqual(result["verdict"], "blocked")
        self.assertIn("CLEAN", result["cleanup"]["incomplete_tasks"])


class RunnerResidualTests(unittest.TestCase):
    def test_runner_residuals_become_pending_resources_without_payload(self):
        runner_result = {
            "cleanup": {
                "status": "not_run",
                "pending_objects": [
                    {"case_id": "TC-ORDER", "object_ref": {"type": "order", "reference_no": "R-1", "customer": "someone"}, "reason": "no teardown configured", "risk": "accepted_pending_cleanup"},
                    {"case_id": "TC-ORDER", "object_ref": "O-2"},
                ],
            }
        }
        objects = _record_runner_residuals(Path("."), "S1", runner_result)
        self.assertEqual([item["cleanup_status"] for item in objects], ["pending", "pending"])
        self.assertEqual(objects[0]["object_type"], "order")
        self.assertEqual(objects[1]["object_id"], "O-2")
        serialized = json.dumps(objects, ensure_ascii=False)
        self.assertNotIn("someone", serialized)
        self.assertNotIn("R-1", serialized)


if __name__ == "__main__":
    unittest.main()
