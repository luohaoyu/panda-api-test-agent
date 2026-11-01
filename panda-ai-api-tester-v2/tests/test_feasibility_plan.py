import copy
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from feasibility_plan import (  # noqa: E402
    build_feasibility_plan,
    calculate_feasibility_fingerprint,
    validate_feasibility_plan,
    validate_package_feasibility,
)
from package_compiler import calculate_design_fingerprint, compile_approved_package  # noqa: E402


class FeasibilityPlanTests(unittest.TestCase):
    def _design(self, *, partial=False, reviewed=True):
        cases = [
            {
                "case_id": "CASE-READY",
                "depends_on": [],
                "requirement_refs": ["REQ-1"],
                "business_rule_refs": ["BR-1"],
                "execution_dependencies": [],
                "request": {"method": "GET", "path": "/ready"},
                "assertions": [{"assertion_id": "A-1", "business_rule_refs": ["BR-1"]}],
                "side_effect": "read",
            },
            {
                "case_id": "CASE-BLOCKED",
                "depends_on": [],
                "requirement_refs": ["REQ-1"],
                "business_rule_refs": ["BR-2"],
                "execution_dependencies": [
                    {
                        "dependency_id": "DATA-1",
                        "kind": "data",
                        "status": "blocked",
                        "description": "需要真实运单",
                    }
                ],
                "request": {"method": "POST", "path": "/scan"},
                "assertions": [{"assertion_id": "A-2", "business_rule_refs": ["BR-2"]}],
                "side_effect": "write_attempt",
            },
        ]
        design = {
            "requirement_id": "REQ-1",
            "mode": "EXECUTE",
            "cases": cases,
            "requirements": [{"requirement_id": "REQ-1", "required": True}],
            "business_rules": [
                {"rule_id": "BR-1", "required": True},
                {"rule_id": "BR-2", "required": True},
            ],
            "scenarios": [],
            "known_gaps": [],
            "package_template": {
                "project": {"workspace_id": "test"},
                "test_config": {},
                "contract": {},
                "environment": {},
                "risk": {},
            },
        }
        design["design_fingerprint"] = calculate_design_fingerprint(design)
        scope = ["CASE-READY"] if partial else ["CASE-READY", "CASE-BLOCKED"]
        if reviewed:
            design["review"] = {
                "status": "partially_approved" if partial else "approved",
                "approved_scope": scope,
                "approved_case_ids": scope,
                "design_fingerprint": design["design_fingerprint"],
                "reviewed_fields": {case_id: ["request", "assertions"] for case_id in scope},
            }
        return design

    def test_plan_can_be_created_without_human_review(self):
        design = self._design(reviewed=False)
        plan = build_feasibility_plan(design)
        self.assertEqual(plan["planning_status"], "ready")
        self.assertEqual(plan["gate"]["status"], "pending_review")
        self.assertEqual(plan["approved_case_ids"], [])
        self.assertIn("CASE-BLOCKED", plan["blocked_case_ids"])
        self.assertEqual(validate_feasibility_plan(design, plan), [])

    def test_blocked_approved_case_fails_only_after_human_review(self):
        design = self._design()
        plan = build_feasibility_plan(design)
        self.assertEqual(plan["planning_status"], "finalized")
        self.assertEqual(plan["gate"]["status"], "failed")
        self.assertIn("CASE-BLOCKED", " ".join(plan["gate"]["blocking_reasons"]))
        self.assertEqual(validate_feasibility_plan(design, plan), [])

    def test_out_of_scope_candidate_is_visible_but_does_not_block_approved_subset(self):
        design = self._design(partial=True)
        plan = build_feasibility_plan(design)
        self.assertEqual(plan["gate"]["status"], "passed")
        self.assertEqual(plan["executable_case_ids"], ["CASE-READY"])
        self.assertIn("CASE-BLOCKED", plan["blocked_case_ids"])
        self.assertIn("CASE-BLOCKED", plan["out_of_scope_case_ids"])
        self.assertFalse(plan["coverage_map"]["full_requirement_coverage"])
        self.assertEqual(validate_feasibility_plan(design, plan), [])

    def test_tampered_plan_fingerprint_is_rejected(self):
        design = self._design(partial=True)
        plan = build_feasibility_plan(design)
        plan["dependency_matrix"]["cases"][0]["status"] = "blocked"
        errors = validate_feasibility_plan(design, plan)
        self.assertTrue(any("FINGERPRINT" in error for error in errors))

    def test_compiled_package_carries_static_gate_and_build_ledger(self):
        design = self._design(partial=True)
        plan = build_feasibility_plan(design)
        package = compile_approved_package(design, feasibility_plan=plan)
        self.assertEqual(package["schema_version"], "2.2")
        self.assertEqual(package["feasibility"]["gate"]["gate_id"], "GATE-FEASIBILITY")
        self.assertEqual(package["package_build"]["status"], "compiled")
        self.assertEqual(validate_package_feasibility(package), [])

    def test_embedded_feasibility_tampering_blocks_preflight(self):
        design = self._design(partial=True)
        plan = build_feasibility_plan(design)
        package = compile_approved_package(design, feasibility_plan=plan)
        package["feasibility"]["gate"]["status"] = "failed"
        errors = validate_package_feasibility(package)
        self.assertTrue(any("FEASIBILITY" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
