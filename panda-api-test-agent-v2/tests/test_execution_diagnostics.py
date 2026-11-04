import copy
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
L2_SCRIPTS = Path(__file__).resolve().parents[2] / "panda-ai-api-tester-v2" / "scripts"
sys.path.insert(0, str(L2_SCRIPTS))
sys.path.insert(0, str(SCRIPTS))

from execution_diagnostics import diagnose_design, diagnose_evidence_closure, format_diagnosis  # noqa: E402
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
            "reviewed_fields": {"C1": ["case", "request", "assertions", "data_dependencies", "side_effects"]},
        }
        return design

    def test_evidence_diagnosis_does_not_block_on_single_source_failure(self):
        diagnosis = diagnose_evidence_closure(
            [{
                "fact_id": "FACT-REQ",
                "status": "unavailable",
                "checked_sources": [{"source_id": "provided-url", "status": "unavailable"}],
            }],
            candidate_actions=[{"action": "inspect_existing_assets", "available": True, "safe": True, "valuable": True}],
        )
        self.assertEqual(diagnosis["decision_state"], "FACT_GATHERING")
        self.assertEqual(diagnosis["status"], "recoverable")
        self.assertEqual(diagnosis["issues"], [])

    def test_evidence_diagnosis_requires_stop_proof_before_user_block(self):
        diagnosis = diagnose_evidence_closure(
            [{
                "fact_id": "FACT-REQ",
                "status": "unavailable",
                "checked_sources": [{"source_id": "provided-url", "status": "unavailable"}],
                "user_input": {"kind": "requirement_fact"},
                "resume_from": "designing",
            }],
        )
        self.assertEqual(diagnosis["decision_state"], "BLOCKED_USER")
        self.assertIsInstance(diagnosis["stop_proof"], dict)
        self.assertEqual(diagnosis["issues"][0]["code"], "EVIDENCE_STOP_PROOF_COMPLETE")

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

    def test_global_blocking_gap_still_blocks_everything(self):
        design = self._design()
        assets = {field: {} for field in ("project", "test_config", "contract", "environment", "risk")}
        design["known_gaps"] = [{"gap_id": "GAP-NO-TASKS", "blocking": True, "stage": "design"}]
        diagnosis = diagnose_design(design, execution_assets=assets)
        self.assertIn("DESIGN_BLOCKING_GAPS_PRESENT", {item["code"] for item in diagnosis["issues"]})
        self.assertFalse(diagnosis["execution_allowed"])
        self.assertEqual(diagnosis["deferred_gaps"], [])

    def test_gap_scoped_outside_the_approved_scope_does_not_block(self):
        design = self._design()
        assets = {field: {} for field in ("project", "test_config", "contract", "environment", "risk")}
        design["cases"][0]["acceptance_refs"] = ["AC-1"]
        design["known_gaps"] = [
            {"gap_id": "GAP-AC2-AI-DECISION", "blocking": True, "stage": "design", "acceptance_refs": ["AC-2"]},
        ]
        diagnosis = diagnose_design(design, execution_assets=assets)
        codes = {item["code"] for item in diagnosis["issues"]}
        self.assertNotIn("DESIGN_BLOCKING_GAPS_PRESENT", codes)
        self.assertEqual(diagnosis["deferred_gaps"], ["GAP-AC2-AI-DECISION"])

    def test_gap_scoped_into_the_approved_scope_blocks(self):
        design = self._design()
        assets = {field: {} for field in ("project", "test_config", "contract", "environment", "risk")}
        design["cases"][0]["acceptance_refs"] = ["AC-1"]
        design["known_gaps"] = [
            {"gap_id": "GAP-AC1-ORACLE", "blocking": True, "stage": "execute", "acceptance_refs": ["AC-1"]},
        ]
        diagnosis = diagnose_design(design, execution_assets=assets)
        self.assertIn("DESIGN_BLOCKING_GAPS_PRESENT", {item["code"] for item in diagnosis["issues"]})

    def test_dependency_outside_requested_scope_is_deferred(self):
        design = self._design()
        design["cases"].append({
            "case_id": "C2",
            "request": {"method": "GET", "path": "/blocked"},
            "execution_dependencies": [{"dependency_id": "D2", "kind": "data", "status": "blocked", "evidence": ["checked"]}],
        })
        design["review"].update({
            "approved_case_ids": ["C1", "C2"],
            "execution_case_ids": ["C1"],
            "reviewed_fields": {"C1": ["case"], "C2": ["case"]},
        })
        design["design_fingerprint"] = calculate_design_fingerprint(design)
        design["review"]["design_fingerprint"] = design["design_fingerprint"]
        diagnosis = diagnose_design(
            design,
            execution_assets={field: {} for field in ("project", "test_config", "contract", "environment", "risk")},
            feasibility_plan={},
        )
        self.assertNotIn("DATA_DEPENDENCY_BLOCKED", {item["code"] for item in diagnosis["issues"]})

    def test_blocked_data_dependency_is_not_scoped_out_by_diagnosis(self):
        design = self._design()
        design["cases"][0]["execution_dependencies"] = [{
            "dependency_id": "DATA-1",
            "kind": "data",
            "status": "blocked",
            "description": "needs fixture",
            "evidence": ["data-sources.md: checked; no matching fixture"],
        }]
        design["design_fingerprint"] = calculate_design_fingerprint(design)
        design["review"]["design_fingerprint"] = design["design_fingerprint"]
        diagnosis = diagnose_design(design, execution_assets={field: {} for field in ("project", "test_config", "contract", "environment", "risk")}, feasibility_plan={})
        issue = next(item for item in diagnosis["issues"] if item["code"] == "DATA_DEPENDENCY_BLOCKED")
        self.assertIn("not_run", issue["next_action"])
        self.assertNotIn("remove", issue["next_action"])

    def test_config_gap_requires_all_four_source_checks(self):
        design = self._design()
        design["known_gaps"] = [{
            "gap_id": "GAP-AUTH",
            "kind": "secret",
            "blocking": True,
            "source_checks": {"current_input": False, "canonical_config": False},
        }]
        diagnosis = diagnose_design(design)
        issue = next(item for item in diagnosis["issues"] if item["code"] == "CONFIG_SOURCE_CHECK_INCOMPLETE")
        self.assertIn("persistent_secret_store", issue["evidence"])
        self.assertIn("active_run", issue["evidence"])

    def test_available_persistent_secret_prevents_missing_value_question(self):
        design = self._design()
        design["known_gaps"] = [{
            "gap_id": "GAP-AUTH",
            "kind": "secret",
            "blocking": True,
            "source_checks": {
                "current_input": False,
                "canonical_config": False,
                "persistent_secret_store": True,
                "active_run": False,
            },
        }]
        diagnosis = diagnose_design(design)
        issue = next(item for item in diagnosis["issues"] if item["code"] == "CONFIG_VALUE_ALREADY_AVAILABLE")
        self.assertIn("reuse", issue["next_action"])
        self.assertNotIn("secret-value", format_diagnosis(diagnosis))

    def test_exhaustive_source_checks_allow_a_real_config_gap(self):
        design = self._design()
        design["known_gaps"] = [{
            "gap_id": "GAP-URL",
            "kind": "runtime_binding",
            "blocking": True,
            "source_checks": {name: False for name in (
                "current_input", "canonical_config", "persistent_secret_store", "active_run"
            )},
        }]
        diagnosis = diagnose_design(design)
        codes = {item["code"] for item in diagnosis["issues"]}
        self.assertNotIn("CONFIG_SOURCE_CHECK_INCOMPLETE", codes)
        self.assertNotIn("CONFIG_VALUE_ALREADY_AVAILABLE", codes)
        self.assertIn("DESIGN_BLOCKING_GAPS_PRESENT", codes)

    def test_documented_reviewed_fields_wrappers_are_accepted(self):
        assets = {field: {} for field in ("project", "test_config", "contract", "environment", "risk")}
        for reviewed_fields in (
            {"cases": {"C1": ["case", "request"]}},
            [{"case_id": "C1", "fields": ["case", "request"]}],
            ["case", "request"],
        ):
            with self.subTest(reviewed_fields=reviewed_fields):
                design = self._design()
                design["review"]["reviewed_fields"] = reviewed_fields
                diagnosis = diagnose_design(design, execution_assets=assets, feasibility_plan={})
                self.assertNotIn("REVIEWED_FIELDS_MISSING", {item["code"] for item in diagnosis["issues"]})

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
