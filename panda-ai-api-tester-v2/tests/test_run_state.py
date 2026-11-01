import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_state import StateError, append_event, create_run
from result_reducer import load_events


class RunStateTests(unittest.TestCase):
    def test_create_run_initializes_immutable_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "run"
            create_run(root, run_id="RUN-1", plan_id="PLAN-1", plan_fingerprint="sha256:x", environment_snapshot={"environment_id": "test"}, plan={"plan_id": "PLAN-1", "tasks": []})
            self.assertTrue((root / "run.json").is_file())
            self.assertTrue((root / "plan.json").is_file())
            self.assertTrue((root / "events.jsonl").is_file())
            with self.assertRaises(StateError):
                create_run(root, run_id="RUN-1", plan_id="PLAN-1", plan_fingerprint="sha256:x", environment_snapshot={})

    def test_event_chain_is_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "run"
            create_run(root, run_id="RUN-2", plan_id="PLAN-2", plan_fingerprint="sha256:x", environment_snapshot={})
            append_event(root, {"type": "task_started", "task_id": "T1"})
            append_event(root, {"type": "task_finished", "task_id": "T1", "status": "passed", "evidence_ref": "evidence/t1.json"})
            self.assertEqual(len(load_events(root / "events.jsonl")), 2)


if __name__ == "__main__":
    unittest.main()
