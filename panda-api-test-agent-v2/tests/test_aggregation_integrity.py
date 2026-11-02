"""Regression tests for requirement-level aggregation integrity.

These cover three previously observed defects:
1. aggregation dropped cleanup / evidence-closure state, so a run with residual
   objects still surfaced as a green requirement status;
2. a blocked run was filtered out as non-authoritative, turning "ran and was
   stopped" into "never ran";
3. a defect was closed whenever its case had ever passed, including by the same
   run that produced the defect.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "panda-ai-api-tester-v2" / "scripts"))
import requirement_aggregation as ra


def _record(run_id, verdict, tasks, *, sequence=1, cleanup=None, closure=None, known_gaps=None, defects=None):
    return {
        "run_id": run_id,
        "run_ref": run_id,
        "requirement_id": "REQ-A",
        "verdict": verdict,
        "authoritative": True,
        "lineage": {"requirement_id": "REQ-A"},
        "sequence": sequence,
        "scope_hash": "sha256:" + run_id,
        "task_results": tasks,
        "cleanup": cleanup or {},
        "evidence_closure": closure or {},
        "known_gaps": known_gaps or [],
        "external_evidence": {},
        "defects": defects or [],
    }


def _passed(case_id):
    return {"task_id": case_id, "type": "business", "status": "passed", "evidence_refs": ["evidence/" + case_id + ".json"], "assertion_results": []}


class ResidualAwareAggregationTests(unittest.TestCase):
    def test_passed_cases_with_pending_cleanup_cannot_be_passed(self):
        record = _record(
            "RUN-1",
            "inconclusive",
            [_passed("TC-CREATE")],
            cleanup={"required": True, "status": "pending", "unresolved_obligations": ["TC-CREATE"], "pending_objects": [], "incomplete_tasks": []},
        )
        manifest = ra.aggregate_requirement_current("REQ-A", [record])
        self.assertEqual(manifest["current"]["status"], "inconclusive")
        self.assertIn("cleanup_pending", manifest["current"]["status_reasons"])
        self.assertEqual(manifest["current"]["cleanup"]["status"], "pending")
        kinds = {item["kind"] for item in manifest["residual_risks"]}
        self.assertIn("unresolved_cleanup_obligation", kinds)

    def test_pending_object_is_surfaced_as_residual_risk(self):
        record = _record(
            "RUN-1",
            "blocked",
            [_passed("TC-CREATE")],
            cleanup={"required": True, "status": "blocked", "pending_objects": [{"object_id": "GFFR-1", "case_id": "TC-CREATE", "cleanup_status": "pending"}], "unresolved_obligations": [], "incomplete_tasks": []},
        )
        manifest = ra.aggregate_requirement_current("REQ-A", [record])
        self.assertEqual(manifest["current"]["status"], "blocked")
        self.assertEqual(manifest["residual_risks"][0]["object_id"], "GFFR-1")

    def test_clean_completion_keeps_passed(self):
        record = _record(
            "RUN-1",
            "passed",
            [_passed("TC-CREATE")],
            cleanup={"required": True, "status": "complete", "pending_objects": [], "unresolved_obligations": [], "incomplete_tasks": []},
            closure={"business_execution": True, "business_evidence_closed": True, "oracle_required": True, "oracle_verified": True, "closed": True},
        )
        manifest = ra.aggregate_requirement_current("REQ-A", [record])
        self.assertEqual(manifest["current"]["status"], "passed")
        self.assertEqual(manifest["current"]["status_reasons"], [])
        self.assertEqual(manifest["residual_risks"], [])

    def test_unverified_required_oracle_downgrades(self):
        record = _record(
            "RUN-1",
            "passed",
            [_passed("TC-CREATE")],
            cleanup={"required": False, "status": "complete"},
            closure={"business_execution": True, "oracle_required": True, "oracle_verified": False},
        )
        manifest = ra.aggregate_requirement_current("REQ-A", [record])
        self.assertEqual(manifest["current"]["status"], "inconclusive")
        self.assertIn("oracle_unverified", manifest["current"]["status_reasons"])

    def test_blocked_run_is_not_dropped(self):
        record = _record("RUN-1", "blocked", [{"task_id": "TC-CREATE", "type": "business", "status": "not_run", "evidence_refs": []}])
        self.assertTrue(record["authoritative"])
        manifest = ra.aggregate_requirement_current("REQ-A", [record])
        self.assertEqual(manifest["current"]["status"], "blocked")
        self.assertEqual(manifest["runs"][0]["status"], "blocked")

    def test_external_evidence_never_defaults_to_complete(self):
        record = _record("RUN-1", "passed", [_passed("TC-CREATE")])
        manifest = ra.aggregate_requirement_current("REQ-A", [record])
        self.assertNotEqual(manifest["external_evidence"]["status"], "complete")


class DefectLifecycleTests(unittest.TestCase):
    def _failed(self, case_id):
        return {
            "task_id": case_id,
            "type": "business",
            "status": "failed",
            "evidence_refs": ["evidence/" + case_id + ".json"],
            "assertion_results": [{"assertion_id": "A1", "expected": 200, "observed": 500}],
        }

    def test_same_run_pass_cannot_close_its_own_defect(self):
        record = _record("RUN-1", "failed", [self._failed("TC-1")])
        defects = ra.reconcile_defects([], [record])
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["status"], "open")

    def test_later_pass_closes_defect(self):
        run1 = _record("RUN-1", "failed", [self._failed("TC-1")], sequence=1)
        run2 = _record("RUN-2", "passed", [_passed("TC-1")], sequence=2)
        defects = ra.reconcile_defects([], [run1, run2])
        self.assertEqual(defects[0]["status"], "fixed")
        self.assertEqual(defects[0]["fixed_by_run_id"], "RUN-2")

    def test_earlier_pass_cannot_close_a_later_failure(self):
        run1 = _record("RUN-1", "passed", [_passed("TC-1")], sequence=1)
        run2 = _record("RUN-2", "failed", [self._failed("TC-1")], sequence=2)
        defects = ra.reconcile_defects([], [run1, run2])
        self.assertEqual(defects[0]["status"], "open")
        self.assertEqual(defects[0]["last_seen_run_id"], "RUN-2")


class RunRecordSourceTests(unittest.TestCase):
    def test_stale_result_json_cannot_override_the_ledger(self):
        import json
        import tempfile
        from pathlib import Path

        from plan_compiler import compile_plan
        from run_state import append_event, create_run

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "run"
            plan = compile_plan({"requirement_id": "REQ-A", "acceptance_criteria": [{"ac_id": "AC-1"}], "tasks": [{"task_id": "TC-1", "type": "business", "method": "GET", "side_effect": "read"}]})
            create_run(root, run_id="RUN-1", plan_id=plan["plan_id"], plan_fingerprint=plan["plan_fingerprint"], environment_snapshot={"environment_id": "test"}, plan=plan)
            append_event(root, {"type": "task_finished", "task_id": "TC-1", "task_status": "failed", "status": "failed", "evidence_ref": "evidence/t.json"})
            (root / "result.json").write_text(json.dumps({"run_id": "RUN-1", "requirement_id": "REQ-A", "verdict": "passed", "task_results": [{"task_id": "TC-1", "type": "business", "status": "passed", "evidence_refs": ["evidence/t.json"]}]}), encoding="utf-8")
            record = ra.load_run_record(root)
            self.assertEqual(record["result_source"], "recomputed")
            self.assertEqual(record["verdict"], "failed")

    def test_imported_result_without_ledger_is_marked_recorded(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "result.json").write_text(json.dumps({"run_id": "RUN-EXT", "requirement_id": "REQ-A", "verdict": "passed", "task_results": []}), encoding="utf-8")
            record = ra.load_run_record(root)
            self.assertEqual(record["result_source"], "recorded")
            self.assertEqual(record["verdict"], "passed")


if __name__ == "__main__":
    unittest.main()
