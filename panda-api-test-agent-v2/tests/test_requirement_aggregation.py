import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from requirement_aggregation import (
    aggregate_requirement_current,
    compute_defect_signature,
    normalize_run_result,
    reconcile_defects,
)


class RequirementAggregationTests(unittest.TestCase):
    def _run(self, run_id, sequence, tasks, verdict="passed", authoritative=True, **lineage):
        result = {"run_id": run_id, "verdict": verdict, "task_results": tasks}
        return normalize_run_result(result, f".panda-api-test-agent/runs/{run_id}/result.json", run_metadata={"lineage": {"run_sequence": sequence, "authoritative": authoritative, "requirement_id": "REQ-AGG", **lineage}}, sequence=sequence)

    def test_current_merges_partial_reruns_without_erasing_old_case(self):
        first = self._run("RUN-1", 1, [{"task_id": "C1", "status": "failed", "evidence_refs": ["e1"]}, {"task_id": "C2", "status": "passed", "evidence_refs": ["e2"]}], verdict="failed")
        second = self._run("RUN-2", 2, [{"task_id": "C1", "status": "passed", "evidence_refs": ["e3"]}], retry_of_run_id="RUN-1")
        manifest = aggregate_requirement_current("REQ-AGG", [first, second])
        self.assertEqual(manifest["current"]["run_id"], "RUN-2")
        self.assertEqual(manifest["current"]["status"], "passed")
        self.assertEqual(manifest["current_cases"]["C1"]["source_run_id"], "RUN-2")
        self.assertEqual(manifest["current_cases"]["C2"]["source_run_id"], "RUN-1")
        self.assertEqual(len(manifest["runs"]), 2)

    def test_not_run_does_not_erase_current_case_and_non_authoritative_run_is_history_only(self):
        first = self._run("RUN-1", 1, [{"task_id": "C1", "status": "passed", "evidence_refs": ["e1"]}])
        blocked = self._run("RUN-BLOCKED", 2, [{"task_id": "C1", "status": "not_run"}], verdict="blocked", authoritative=False)
        manifest = aggregate_requirement_current("REQ-AGG", [first, blocked])
        self.assertEqual(manifest["current"]["run_id"], "RUN-1")
        self.assertEqual(manifest["current_cases"]["C1"]["status"], "passed")
        self.assertFalse(manifest["runs"][1]["authoritative"])

    def test_external_evidence_partial_is_preserved_and_not_passed_as_complete(self):
        run = self._run("RUN-1", 1, [{"task_id": "C1", "status": "passed", "evidence_refs": ["e1"]}])
        run["external_evidence"] = {"api_verifiability": "partial", "status": "partial", "refs": ["e1"], "supports": ["http_status"], "missing": ["cleanup_oracle"]}
        manifest = aggregate_requirement_current("REQ-AGG", [run])
        self.assertEqual(manifest["external_evidence"]["status"], "partial")
        self.assertEqual(manifest["external_evidence"]["api_verifiability"], "partial")
        self.assertEqual(manifest["external_evidence"]["missing"], ["cleanup_oracle"])

    def test_defect_is_reused_fixed_and_reopened_across_runs(self):
        assertion = {"assertion_id": "A1", "expected": 400, "observed": 200}
        signature = compute_defect_signature("REQ-AGG", "C1", assertion)
        first = self._run("RUN-1", 1, [{"task_id": "C1", "status": "failed", "evidence_refs": ["e1"], "assertion_results": [assertion]}], verdict="failed")
        defects = reconcile_defects([], [first])
        self.assertEqual(defects[0]["signature"], signature)
        self.assertEqual(defects[0]["status"], "open")
        fixed = self._run("RUN-2", 2, [{"task_id": "C1", "status": "passed", "evidence_refs": ["e2"]}])
        defects = reconcile_defects(defects, [fixed])
        self.assertEqual(defects[0]["status"], "fixed")
        reopened = self._run("RUN-3", 3, [{"task_id": "C1", "status": "failed", "evidence_refs": ["e3"], "assertion_results": [assertion]}], verdict="failed")
        defects = reconcile_defects(defects, [reopened])
        self.assertEqual(defects[0]["status"], "reopened")
        self.assertEqual(defects[0]["first_seen_run_id"], "RUN-1")


if __name__ == "__main__":
    unittest.main()
