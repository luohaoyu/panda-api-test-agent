import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

from result_reducer import reduce_run  # noqa: E402
from runner import ApiTestRunner, HttpExecutor, RunnerError  # noqa: E402
from plan_fingerprint import calculate_plan_fingerprint  # noqa: E402
from validate_review import validate_review  # noqa: E402


class StageOneP0Tests(unittest.TestCase):
    def test_blocking_gap_cannot_be_accepted(self):
        package = {
            "known_gaps": [{"gap_id": "GAP-1", "blocking": True}],
            "cases": [],
            "review": {"status": "approved", "accepted_gaps": ["GAP-1"], "review_id": "R", "plan_fingerprint": "x", "reviewer": "qa", "approved_at": "2026-10-31T00:00:00+00:00", "approved_scope": []},
        }
        errors = validate_review(package, require_execute=True)
        self.assertTrue(any("blocking known_gaps cannot be accepted" in item for item in errors), errors)

    def test_inline_sensitive_headers_are_rejected(self):
        executor = HttpExecutor("http://127.0.0.1:1")
        with self.assertRaisesRegex(RunnerError, "inline sensitive"):
            executor.execute({"method": "GET", "path": "/", "headers": {"Authorization": "Bearer inline"}})

    def test_partial_review_keeps_unapproved_cases_out_of_execution_scope(self):
        package = {
            "cases": [{"case_id": "TC-1"}, {"case_id": "TC-2"}],
            "known_gaps": [],
            "review": {
                "status": "partially_approved",
                "review_id": "R-1",
                "plan_fingerprint": "",
                "reviewer": "qa",
                "approved_at": "2026-10-31T00:00:00+00:00",
                "approved_scope": ["TC-1"],
                "reviewed_fields": {"TC-1": ["request", "assertions", "side_effect"]},
                "accepted_gaps": [],
            },
        }
        package["review"]["plan_fingerprint"] = calculate_plan_fingerprint(package)
        self.assertEqual(validate_review(package, require_execute=True), [])

    def test_expired_and_superseded_reviews_are_not_executable(self):
        for status in ("expired", "superseded", "plan_only"):
            package = {
                "cases": [{"case_id": "TC-1"}],
                "known_gaps": [],
                "review": {
                    "status": status,
                    "review_id": "R-1",
                    "plan_fingerprint": "",
                    "reviewer": "qa",
                    "approved_at": "2026-10-31T00:00:00+00:00",
                    "approved_scope": ["TC-1"],
                    "reviewed_fields": {"TC-1": ["request"]},
                    "accepted_gaps": [],
                },
            }
            package["review"]["plan_fingerprint"] = calculate_plan_fingerprint(package)
            errors = validate_review(package, require_execute=True)
            self.assertTrue(any("not executable" in error for error in errors), errors)

    def test_reducer_rejects_pass_without_business_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "run.json").write_text(json.dumps({"run_id": "RUN-1"}), encoding="utf-8")
            (root / "plan.json").write_text(json.dumps({"plan_id": "PLAN-1", "tasks": [{"task_id": "T1", "type": "business"}]}), encoding="utf-8")
            (root / "resources.json").write_text(json.dumps({"objects": []}), encoding="utf-8")
            result = reduce_run(root)
            self.assertNotEqual(result["verdict"], "passed")
            self.assertFalse(result["evidence_closure"]["closed"])

    def test_business_failure_without_evidence_cannot_close_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "run.json").write_text(json.dumps({"run_id": "RUN-1B"}), encoding="utf-8")
            plan = {"plan_id": "PLAN-1B", "tasks": [{"task_id": "T1", "type": "business"}]}
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            (root / "resources.json").write_text(json.dumps({"objects": []}), encoding="utf-8")
            item = {"type": "task_finished", "task_id": "T1", "status": "failed", "prev_event_digest": "", "event_id": "T1", "occurred_at": "2026-10-31T00:00:00+08:00"}
            item["event_digest"] = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            (root / "events.jsonl").write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
            result = reduce_run(root)
            self.assertEqual(result["verdict"], "failed")
            self.assertFalse(result["evidence_closure"]["business_evidence_closed"])

    def test_package_nested_inline_sensitive_header_is_rejected(self):
        with self.assertRaisesRegex(RunnerError, "inline sensitive"):
            ApiTestRunner._reject_inline_sensitive_headers({"cases": [{"request": {"headers": {"Cookie": "inline"}}}]})

    def test_reducer_hard_gap_blocks_even_without_events(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "run.json").write_text(json.dumps({"run_id": "RUN-2"}), encoding="utf-8")
            (root / "plan.json").write_text(json.dumps({"plan_id": "PLAN-2", "known_gaps": [{"gap_id": "GAP-HARD", "blocking": True}], "tasks": []}), encoding="utf-8")
            (root / "resources.json").write_text(json.dumps({"objects": []}), encoding="utf-8")
            result = reduce_run(root)
            self.assertEqual(result["verdict"], "blocked")

    def test_reducer_requires_required_oracle_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "run.json").write_text(json.dumps({"run_id": "RUN-3"}), encoding="utf-8")
            plan = {"plan_id": "PLAN-3", "tasks": [
                {"task_id": "BUS", "type": "business"},
                {"task_id": "ORACLE", "type": "oracle", "required": True},
                {"task_id": "CLEAN", "type": "cleanup"},
            ]}
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            (root / "resources.json").write_text(json.dumps({"objects": []}), encoding="utf-8")
            import hashlib
            item = {"type": "task_finished", "task_id": "BUS", "status": "passed", "evidence_ref": "evidence/business.json", "prev_event_digest": "", "event_id": "BUS", "occurred_at": "2026-10-31T00:00:00+08:00"}
            item["event_digest"] = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            (root / "events.jsonl").write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
            result = reduce_run(root)
            self.assertEqual(result["verdict"], "blocked")
            self.assertFalse(result["evidence_closure"]["closed"])


if __name__ == "__main__":
    unittest.main()
