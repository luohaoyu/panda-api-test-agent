import copy
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from execution_authorization import (
    calculate_execution_scope_fingerprint,
    derive_side_effect_summary,
    normalize_execution_authorization,
)
from feasibility_plan import build_feasibility_plan
from package_compiler import calculate_design_fingerprint, compile_approved_package
from run_state import create_run, load_state
from slice_executor import execute_slice
from plan_compiler import compile_plan


class ExecutionAuthorizationContractTests(unittest.TestCase):
    def _design(self, requested):
        cases = [
            {
                "case_id": case_id,
                "depends_on": [],
                "execution_dependencies": [],
                "request": {"method": "GET", "path": f"/{case_id.lower()}"},
                "assertions": [{"assertion_id": f"A-{case_id}"}],
                "side_effect": "read",
            }
            for case_id in ("C1", "C2", "C3")
        ]
        design = {
            "requirement_id": "REQ-AUTH",
            "mode": "EXECUTE",
            "cases": cases,
            "requirements": [],
            "business_rules": [],
            "scenarios": [],
            "known_gaps": [],
            "package_template": {
                "project": {"workspace_id": "test"},
                "test_config": {"environment_id": "test"},
                "contract": {},
                "environment": {"environment_id": "test"},
                "risk": {},
            },
        }
        design["design_fingerprint"] = calculate_design_fingerprint(design)
        design["review"] = {
            "status": "approved",
            "review_id": "REV-AUTH",
            "reviewer": "qa",
            "reviewed_at": "2026-11-03T12:00:00+08:00",
            "approved_scope": ["C1", "C2", "C3"],
            "approved_case_ids": ["C1", "C2", "C3"],
            "reviewed_case_ids": ["C1", "C2", "C3"],
            "execution_case_ids": list(requested),
            "design_fingerprint": design["design_fingerprint"],
            "reviewed_fields": {case_id: ["request", "assertions"] for case_id in ("C1", "C2", "C3")},
        }
        return design

    def test_explicit_empty_execution_scope_stays_empty(self):
        design = self._design([])
        plan = build_feasibility_plan(design)
        self.assertEqual(plan["requested_case_ids"], [])
        self.assertEqual(plan["execution_case_ids"], [])
        self.assertEqual(plan["gate"]["status"], "passed")

    def test_requested_but_not_ready_scope_fails_gate(self):
        design = self._design(["C1"])
        design["cases"][0]["execution_dependencies"] = [
            {
                "dependency_id": "DATA-1",
                "kind": "data",
                "status": "blocked",
                "description": "missing data",
                "evidence": ["data-sources.md checked"],
            }
        ]
        design["design_fingerprint"] = calculate_design_fingerprint(design)
        design["review"]["design_fingerprint"] = design["design_fingerprint"]
        plan = build_feasibility_plan(design)
        self.assertEqual(plan["execution_case_ids"], [])
        self.assertEqual(plan["gate"]["status"], "failed")
        self.assertTrue(plan["gate"]["blocking_reasons"])

    def test_authorization_may_select_a_ready_subset(self):
        design = self._design(["C1", "C2", "C3"])
        plan = build_feasibility_plan(design)
        package = compile_approved_package(design, feasibility_plan=plan)
        authorization = {
            "kind": "execution_approval",
            "status": "partially_approved",
            "authorized_case_ids": ["C2"],
            "execution_scope_fingerprint": package["execution_authorization"]["execution_scope_fingerprint"],
            "authorization_id": "AUTH-1",
            "authorizer": "qa-lead",
            "authorized_at": "2026-11-03T12:05:00+08:00",
        }
        normalized, errors = normalize_execution_authorization(
            authorization,
            executable_case_ids=plan["execution_case_ids"],
            plan_fingerprint=package["review"]["plan_fingerprint"],
            environment_id="test",
            request_count_expected=package["request_count_expected"],
            side_effect_summary=derive_side_effect_summary(package, plan["execution_case_ids"]),
        )
        self.assertEqual(errors, [])
        self.assertEqual(normalized["authorized_case_ids"], ["C2"])

    def test_blocked_case_cannot_be_authorized(self):
        authorization = {
            "kind": "execution_approval",
            "status": "approved",
            "authorized_case_ids": ["C2"],
            "authorization_id": "AUTH-BLOCKED",
            "authorizer": "qa-lead",
            "authorized_at": "2026-11-03T12:05:00+08:00",
        }
        normalized, errors = normalize_execution_authorization(
            authorization,
            executable_case_ids=["C1"],
            blocked_case_ids=["C2"],
            plan_fingerprint="sha256:plan",
            environment_id="test",
            request_count_expected=1,
            side_effect_summary=[{"case_id": "C1", "side_effect": "read"}],
        )
        self.assertEqual(normalized["authorized_case_ids"], ["C2"])
        self.assertTrue(any("EXECUTION_SCOPE_NOT_READY" in error for error in errors))
        self.assertTrue(any("blocked/not_ready" in error for error in errors))

    def test_equivalent_authorization_normalizes_idempotently(self):
        authorization = {
            "kind": "execution_approval",
            "status": "approved",
            "authorized_case_ids": ["C1", "C1"],
            "authorization_id": "AUTH-IDEMPOTENT",
            "authorizer": "qa-lead",
            "authorized_at": "2026-11-03T12:05:00+08:00",
        }
        kwargs = {
            "executable_case_ids": ["C1", "C2"],
            "plan_fingerprint": "sha256:plan",
            "environment_id": "test",
            "request_count_expected": 2,
            "side_effect_summary": [{"case_id": "C1", "side_effect": "read"}],
        }
        first, first_errors = normalize_execution_authorization(authorization, **kwargs)
        second, second_errors = normalize_execution_authorization(first, **kwargs)
        self.assertEqual(first_errors, [])
        self.assertEqual(second_errors, [])
        self.assertEqual(first, second)
        self.assertEqual(first["authorized_case_ids"], ["C1"])

    def test_scope_fingerprint_changes_when_executable_scope_changes(self):
        first = calculate_execution_scope_fingerprint(
            "sha256:plan", ["C1"], "test", 1,
            [{"case_id": "C1", "side_effect": "read"}],
        )
        second = calculate_execution_scope_fingerprint(
            "sha256:plan", ["C1", "C2"], "test", 2,
            [
                {"case_id": "C1", "side_effect": "read"},
                {"case_id": "C2", "side_effect": "read"},
            ],
        )
        self.assertNotEqual(first, second)

    def test_scope_fingerprint_ignores_token_values(self):
        base = calculate_execution_scope_fingerprint(
            "sha256:plan",
            ["C1"],
            "test",
            1,
            [{"case_id": "C1", "token": "old", "side_effect": "read"}],
        )
        rotated = calculate_execution_scope_fingerprint(
            "sha256:plan",
            ["C1"],
            "test",
            1,
            [{"case_id": "C1", "token": "new", "side_effect": "read"}],
        )
        changed = calculate_execution_scope_fingerprint(
            "sha256:plan",
            ["C1", "C2"],
            "test",
            2,
            [{"case_id": "C1", "token": "new", "side_effect": "read"}],
        )
        self.assertEqual(base, rotated)
        self.assertNotEqual(base, changed)

    def test_high_risk_token_does_not_replace_execution_authorization(self):
        plan = compile_plan({
            "requirement_id": "REQ-RISK",
            "acceptance_criteria": [{"ac_id": "AC-1"}],
            "tasks": [{
                "task_id": "DELETE",
                "case_id": "DELETE",
                "type": "business",
                "method": "DELETE",
                "side_effect": "destructive",
                "object_ref": "O-1",
            }],
        })
        plan["authorization_model"] = "independent"
        plan["plan_fingerprint"] = __import__("run_state").calculate_plan_fingerprint(plan)
        root = Path(tempfile.mkdtemp()) / "run"
        create_run(
            root,
            run_id="RUN-RISK",
            plan_id=plan["plan_id"],
            plan_fingerprint=plan["plan_fingerprint"],
            environment_snapshot={"environment_id": "test"},
            plan=plan,
            entry_metadata={
                "entry_type": "single_api_quality",
                "review": {
                    "status": "approved",
                    "review_id": "REV-RISK",
                    "approved_case_ids": ["DELETE"],
                    "execution_case_ids": ["DELETE"],
                },
            },
        )
        called = []
        with self.assertRaisesRegex(Exception, "not executable"):
            execute_slice(
                root,
                "S1",
                approval_token="high-risk-token",
                task_executor=lambda task, **_: called.append(task["task_id"]) or {"status": "passed"},
            )
        self.assertEqual(called, [])
        self.assertEqual(load_state(root)["next_action"], "request_execution_approval")


if __name__ == "__main__":
    unittest.main()
