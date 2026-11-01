import copy
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
L2_SCRIPTS = Path(r"C:/Users/Administrator/.workbuddy/skills/panda-ai-api-tester-v2/scripts")
sys.path.insert(0, str(L2_SCRIPTS))
sys.path.insert(0, str(SCRIPTS))

from execution_diagnostics import diagnose_design, format_diagnosis  # noqa: E402
from package_compiler import calculate_design_fingerprint  # noqa: E402


class ExecutionDiagnosticsTests(unittest.TestCase):
    def _design(self):
        design = {
            "requirement_id": "REQ-DIAG-1",
            "mode": "EXECUTE",
            "cases": [
                {
                    "case_id": "C1",
                    "request": {"method": "GET", "path": "/health"},
                    "execution_dependencies": [],
                }
            ],
            "known_gaps": [],
        }
        design["design_fingerprint"] = calculate_design_fingerprint(design)
        design["review"] = {
            "status": "approved",
            "approved_case_ids": ["C1"],
            "review_id": "REV-1",
            "reviewer": "user",
            "approved_at": "2026-11-01T11:00:00+08:00",
            "design_fingerprint": design["design_fingerprint"],
            "reviewed_fields": {"C1": ["request", "assertions", "side_effect"]},
        }
        return design

    def test_pending_review_is_structured_and_not_silenced(self):
        design = self._design()
        design["review"] = {"status": "pending_review", "approved_case_ids": []}
        diagnosis = diagnose_design(design)
        self.assertEqual(diagnosis["status"], "blocked")
        self.assertFalse(diagnosis["execution_allowed"])
        issue = next(item for item in diagnosis["issues"] if item["code"] == "REVIEW_DECISION_NOT_PERSISTED")
        self.assertEqual(issue["owner"], "skill_or_host")
        self.assertIn("persist", issue["next_action"])

    def test_stale_fingerprint_is_reported_as_skill_integrity_issue(self):
        design = self._design()
        design["design_fingerprint"] = "sha256:" + "0" * 64
        design["review"]["design_fingerprint"] = design["design_fingerprint"]
        diagnosis = diagnose_design(design, execution_assets={})
        codes = {item["code"] for item in diagnosis["issues"]}
        self.assertIn("DESIGN_FINGERPRINT_MISMATCH", codes)
        self.assertIn("EXECUTION_ASSETS_MISSING", codes)

    def test_missing_assets_and_dependencies_are_actionable(self):
        design = self._design()
        del design["cases"][0]["execution_dependencies"]
        diagnosis = diagnose_design(design)
        codes = {item["code"] for item in diagnosis["issues"]}
        self.assertIn("EXECUTION_DEPENDENCIES_MISSING", codes)
        self.assertIn("EXECUTION_ASSETS_MISSING", codes)
        self.assertFalse(diagnosis["execution_allowed"])

    def test_feasibility_missing_is_recoverable_only_after_dependencies_close(self):
        design = self._design()
        assets = {field: {} for field in ("project", "test_config", "contract", "environment", "risk")}
        diagnosis = diagnose_design(design, execution_assets=assets)
        self.assertEqual(diagnosis["status"], "recoverable")
        self.assertIn("FEASIBILITY_PLAN_MISSING", {item["code"] for item in diagnosis["issues"]})
        self.assertTrue(any(item["recoverable"] for item in diagnosis["issues"]))

    def test_diagnosis_does_not_mutate_design(self):
        design = self._design()
        before = copy.deepcopy(design)
        diagnose_design(design)
        self.assertEqual(design, before)

    def test_formatter_contains_no_secret_values(self):
        diagnosis = diagnose_design(self._design())
        rendered = format_diagnosis(diagnosis)
        self.assertNotIn("Basic", rendered)
        self.assertIn("EXECUTION_ASSETS_MISSING", rendered)


if __name__ == "__main__":
    unittest.main()
