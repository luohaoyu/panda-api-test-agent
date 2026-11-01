import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from plan_compiler import compile_plan
from run_state import create_run, load_state
from slice_executor import SliceExecutionError, execute_slice, replay_volatile_sources, validate_recovery
from result_reducer import load_events


class SliceExecutorTests(unittest.TestCase):
    def _run(self, intent):
        plan = compile_plan(intent)
        root = Path(tempfile.mkdtemp()) / "run"
        create_run(root, run_id="RUN-1", plan_id=plan["plan_id"], plan_fingerprint=plan["plan_fingerprint"], environment_snapshot={"environment_id": "test"}, plan=plan)
        return root, plan

    def test_approval_digest_is_persisted_not_raw_token(self):
        root, plan = self._run({
            "requirement_id": "REQ-1", "acceptance_criteria": [{"ac_id": "AC-1"}],
            "tasks": [{"task_id": "DELETE", "type": "business", "method": "DELETE", "side_effect": "destructive", "object_ref": "order-1"}],
        })
        execute_slice(root, "S1", approval_token="approval-secret", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/delete.json"})
        text = (root / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("approval-secret", text)
        self.assertIn("approval_digest", text)

    def test_approval_slice_does_not_execute_task(self):
        root, plan = self._run({
            "requirement_id": "REQ-1", "acceptance_criteria": [{"ac_id": "AC-1"}],
            "tasks": [{"task_id": "DELETE", "type": "business", "method": "DELETE", "side_effect": "destructive", "object_ref": "order-1"}],
        })
        called = []
        result = execute_slice(root, "S1", task_executor=lambda task, **_: called.append(task["task_id"]) or {"status": "passed"})
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(called, [])
        self.assertEqual(load_state(root)["status"], "awaiting_approval")

    def test_slice_appends_facts_and_persists_only_safe_variables(self):
        root, plan = self._run({
            "requirement_id": "REQ-1", "acceptance_criteria": [{"ac_id": "AC-1"}],
            "tasks": [{"task_id": "READ", "type": "business", "method": "GET", "side_effect": "read"}],
        })
        result = execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/read.json"}, produced_variables={"order_id": "O-1", "secret_token": "do-not-persist"}, sensitive_variables={"secret_token"})
        self.assertEqual(result["status"], "passed")
        state = load_state(root)
        self.assertEqual(state["non_sensitive_variables"], {"order_id": "O-1"})
        events = load_events(root / "events.jsonl")
        self.assertTrue(any(event.get("type") == "task_finished" and event.get("evidence_ref") == "evidence/read.json" for event in events))
        self.assertNotIn("secret_token", json.dumps(state))

    def test_pending_cleanup_blocks_non_cleanup_slice(self):
        root, plan = self._run({
            "requirement_id": "REQ-1", "acceptance_criteria": [{"ac_id": "AC-1"}],
            "tasks": [{"task_id": "READ", "type": "business", "method": "GET", "side_effect": "read"}],
        })
        state = load_state(root)
        state["pending_cleanup"] = ["resource-1"]
        (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(Exception, "pending cleanup"):
            execute_slice(root, "S1")

    def test_slice_cannot_execute_before_dependencies(self):
        root, plan = self._run({
            "requirement_id": "REQ-1", "acceptance_criteria": [{"ac_id": "AC-1"}],
            "tasks": [
                {"task_id": "AUTH", "type": "business", "method": "GET", "side_effect": "read"},
                {"task_id": "READ", "type": "business", "method": "GET", "side_effect": "read", "depends_on": ["AUTH"]},
            ],
        })
        with self.assertRaisesRegex(SliceExecutionError, "dependencies are not satisfied"):
            execute_slice(root, "S2")
        self.assertEqual(load_state(root)["completed_slices"], [])

    def test_recovery_detects_pending_cleanup_before_resume(self):
        root, plan = self._run({
            "requirement_id": "REQ-1", "acceptance_criteria": [{"ac_id": "AC-1"}],
            "tasks": [
                {"task_id": "WRITE", "type": "business", "method": "POST", "side_effect": "write", "object_ref": "obj-1"},
                {"task_id": "CLEAN", "type": "cleanup", "method": "DELETE", "side_effect": "destructive", "object_ref": "obj-1", "depends_on": ["WRITE"]},
            ],
        })
        state = load_state(root)
        state["pending_cleanup"] = ["obj-1"]
        (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        resources = {"schema_version": "2.0", "run_id": "RUN-1", "objects": [{"object_id": "obj-1", "cleanup_status": "pending"}]}
        (root / "resources.json").write_text(json.dumps(resources), encoding="utf-8")
        result = validate_recovery(root)
        self.assertEqual(result["action"], "cleanup_first")
        self.assertFalse(result["sensitive_variables_persisted"])

    def test_volatile_source_replay_requires_executor_and_does_not_persist_secret(self):
        root, plan = self._run({
            "requirement_id": "REQ-1", "acceptance_criteria": [{"ac_id": "AC-1"}],
            "tasks": [{"task_id": "LOGIN", "type": "business", "method": "POST", "side_effect": "write_attempt", "verification": {"method": "GET"}, "teardown": True, "volatile": True}],
        })
        execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/login.json"}, produced_variables={"token": "secret"}, sensitive_variables={"token"})
        with self.assertRaisesRegex(SliceExecutionError, "trusted task_executor"):
            replay_volatile_sources(root)
        seen = []
        result = replay_volatile_sources(root, task_executor=lambda task, **kwargs: seen.append(kwargs.get("replay")) or {"status": "passed", "produced_variables": {"token": "new-secret"}, "evidence_ref": "evidence/login-replay.json"})
        self.assertEqual(result["status"], "replayed")
        self.assertEqual(seen, [True])
        self.assertNotIn("secret", (root / "state.json").read_text(encoding="utf-8"))

    def test_volatile_replay_invalidates_downstream_slices(self):
        root, plan = self._run({
            "requirement_id": "REQ-1", "acceptance_criteria": [{"ac_id": "AC-1"}],
            "tasks": [
                {"task_id": "LOGIN", "type": "business", "method": "POST", "side_effect": "write_attempt", "verification": {"method": "GET"}, "teardown": True, "volatile": True},
                {"task_id": "READ", "type": "business", "method": "GET", "side_effect": "read", "depends_on": ["LOGIN"]},
            ],
        })
        execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/login.json"})
        execute_slice(root, "S2", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/read.json"})
        result = replay_volatile_sources(root, task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/login-replay.json"})
        self.assertIn("S2", result["invalidated_slice_ids"])
        self.assertNotIn("S2", load_state(root)["completed_slices"])

    def test_fixture_oracle_cleanup_use_task_executor(self):
        root, plan = self._run({
            "requirement_id": "REQ-1", "acceptance_criteria": [{"ac_id": "AC-1"}],
            "tasks": [
                {"task_id": "FIX", "type": "fixture", "fixture_name": "order", "action": "create"},
                {"task_id": "ORACLE", "type": "oracle", "oracle_type": "db", "verification": {"query": "order"}, "depends_on": ["FIX"]},
                {"task_id": "CLEAN", "type": "cleanup", "object_ref": "order-1", "action": "delete", "depends_on": ["ORACLE"]},
            ],
        })
        calls = []
        result = execute_slice(root, "S1", task_executor=lambda task, **_: calls.append(task["type"]) or {"status": "passed", "evidence_ref": "evidence/" + task["task_id"] + ".json"})
        self.assertEqual(result["status"], "passed")
        self.assertEqual(calls, ["fixture"])

    def test_recovery_rejects_broken_event_chain(self):
        root, plan = self._run({
            "requirement_id": "REQ-1", "acceptance_criteria": [{"ac_id": "AC-1"}],
            "tasks": [{"task_id": "READ", "type": "business", "method": "GET", "side_effect": "read"}],
        })
        execute_slice(root, "S1")
        events_path = root / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[0])
        tampered["type"] = "tampered"
        lines[0] = json.dumps(tampered, ensure_ascii=False)
        events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(SliceExecutionError, "recovery refused"):
            validate_recovery(root)


if __name__ == "__main__":
    unittest.main()
