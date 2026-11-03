import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
L2_SCRIPTS = Path(__file__).resolve().parents[2] / "panda-ai-api-tester-v2" / "scripts"
sys.path.insert(0, str(L2_SCRIPTS))
sys.path.insert(0, str(SCRIPTS))
from run_orchestrator import (
    build_requirement_manifest,
    compile_and_create_run,
    compile_approved_design_to_package,
    next_action,
    record_execution_authorization,
    reduce_run,
)
from plan_compiler import compile_plan
from package_compiler import calculate_design_fingerprint
from run_state import load_state
from slice_executor import ApprovalRequired, execute_slice


class RunOrchestratorTests(unittest.TestCase):
    def _intent(self):
        return {
            "requirement_id": "REQ-ORCH-1",
            "environment_id": "test",
            "acceptance_criteria": [{"ac_id": "AC-1", "text": "读取业务结果"}],
            "tasks": [{
                "task_id": "READ",
                "type": "business",
                "method": "GET",
                "side_effect": "read",
                "acceptance_refs": ["AC-1"],
            }],
        }

    def _approved_review(self, intent):
        plan = compile_plan(intent)
        return {
            "status": "approved",
            "approved_case_ids": ["READ"],
            "execution_case_ids": ["READ"],
            "review_id": "REV-1",
            "reviewer": "qa",
            "approved_at": "2026-11-01T05:00:00+08:00",
            "plan_fingerprint": plan["plan_fingerprint"],
            "reviewed_fields": {"READ": ["case", "request", "assertions", "data_dependencies", "side_effects"]},
        }

    def _write_evidence(self, root, name):
        path = root / "evidence" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return f"evidence/{name}"

    def _execution_authorization(self, case_ids, authorization_id="AUTH-1"):
        return {
            "kind": "execution_approval",
            "status": "approved",
            "authorized_case_ids": list(case_ids),
            "authorization_id": authorization_id,
            "authorizer": "qa",
            "authorized_at": "2026-11-01T05:05:00+08:00",
        }

    def test_compile_keeps_candidate_omitted_from_tasks_in_plan_as_not_run(self):
        intent = self._intent()
        intent["candidate_cases"] = [
            {"case_id": "READ", "type": "business", "method": "GET", "side_effect": "read"},
            {"case_id": "BLOCKED", "type": "business", "method": "GET", "side_effect": "read"},
        ]
        plan = compile_plan(intent)
        blocked = next(item for item in plan["tasks"] if item["case_id"] == "BLOCKED")
        self.assertEqual(blocked["planned_disposition"], "not_run")

    def test_compile_keeps_blocked_candidate_in_plan_as_not_run(self):
        intent = self._intent()
        intent["candidate_cases"] = [
            {"case_id": "READ", "type": "business", "method": "GET", "side_effect": "read"},
            {"case_id": "BLOCKED", "type": "business", "method": "GET", "side_effect": "read", "execution_dependencies": [{"dependency_id": "DATA-1", "kind": "data", "status": "blocked", "description": "fixture unavailable", "evidence": ["data-sources.md checked"]}]},
        ]
        plan = compile_plan(intent)
        blocked = next(item for item in plan["tasks"] if item["case_id"] == "BLOCKED")
        self.assertEqual(blocked["planned_disposition"], "not_run")
        self.assertIn("BLOCKED", plan["candidate_case_ids"])

    def test_compile_creates_one_run_and_returns_first_slice(self):
        root = Path(tempfile.mkdtemp()) / "run"
        result = compile_and_create_run(self._intent(), root, run_id="RUN-ORCH-1")
        self.assertEqual(result["run"]["run_id"], "RUN-ORCH-1")
        self.assertTrue((root / "run.json").is_file())
        self.assertTrue((root / "plan.json").is_file())
        self.assertEqual(result["next_action"]["action"], "request_plan_review")
        self.assertEqual(result["next_action"]["approval_kind"], "plan_review")
        action = next_action(root)
        self.assertEqual(action["action"], "request_plan_review")
        self.assertEqual(action["approval_kind"], "plan_review")
        self.assertFalse(action["design_review_still_valid"])
        self.assertEqual(action["slice_id"], "S1")

    def test_execute_reduce_and_project_requirement_manifest(self):
        root = Path(tempfile.mkdtemp()) / "run"
        intent = self._intent()
        intent["review"] = self._approved_review(intent)
        compile_and_create_run(intent, root, run_id="RUN-ORCH-2")
        record_execution_authorization(root, self._execution_authorization(["READ"], "AUTH-ORCH-2"))
        evidence_ref = self._write_evidence(root, "read.json")
        executed = execute_slice(
            root,
            "S1",
            task_executor=lambda task, **_: {"status": "passed", "evidence_ref": evidence_ref},
        )
        self.assertEqual(executed["status"], "passed")
        result = reduce_run(root)
        self.assertEqual(result["verdict"], "passed")
        manifest = build_requirement_manifest(root, title="编排闭环")
        self.assertEqual(manifest["acceptance_items"][0]["verdict"], "passed")
        self.assertEqual(manifest["evidence_runs"][0]["kind"], "business_execution")
        self.assertEqual(manifest["evidence_runs"][0]["immutable"], True)

    def test_one_review_allows_multiple_slices_to_progress_without_reapproval(self):
        root = Path(tempfile.mkdtemp()) / "run"
        intent = {
            "requirement_id": "REQ-MULTI-SLICE",
            "environment_id": "test",
            "acceptance_criteria": [{"ac_id": "AC-1", "text": "完成依赖链"}],
            "tasks": [
                {"task_id": "FIRST", "type": "business", "method": "GET", "side_effect": "read", "acceptance_refs": ["AC-1"]},
                {"task_id": "SECOND", "type": "business", "method": "GET", "side_effect": "read", "depends_on": ["FIRST"], "acceptance_refs": ["AC-1"]},
            ],
        }
        plan = compile_plan(intent)
        intent["review"] = {
            "status": "approved",
            "approved_case_ids": ["FIRST", "SECOND"],
            "execution_case_ids": ["FIRST", "SECOND"],
            "review_id": "REV-MULTI",
            "reviewer": "qa",
            "approved_at": "2026-11-01T05:00:00+08:00",
            "plan_fingerprint": plan["plan_fingerprint"],
            "reviewed_fields": ["case", "request", "assertions", "data_dependencies", "side_effects"],
        }
        compile_and_create_run(intent, root, run_id="RUN-MULTI")
        self.assertEqual(next_action(root)["action"], "request_execution_approval")
        record_execution_authorization(root, self._execution_authorization(["FIRST", "SECOND"], "AUTH-MULTI"))
        self.assertEqual(next_action(root)["slice_id"], "S1")
        execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/first.json"})
        second = next_action(root)
        self.assertEqual(second["action"], "execute_slice")
        self.assertEqual(second["slice_id"], "S2")
        execute_slice(root, "S2", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/second.json"})
        self.assertEqual(next_action(root)["action"], "execute_slice")
        self.assertEqual(next_action(root)["slice_id"], "S3")
        execute_slice(root, "S3")
        self.assertEqual(next_action(root)["action"], "reduce")
        review_events = [
            json.loads(line)
            for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("type") == "review_recorded"
        ]
        self.assertEqual(len(review_events), 1)
        self.assertEqual(load_state(root)["run_id"], "RUN-MULTI")

    def test_high_risk_authorization_is_not_reported_as_design_review_and_resumes_same_run(self):
        root = Path(tempfile.mkdtemp()) / "run"
        intent = {
            "requirement_id": "REQ-RISK",
            "environment_id": "test",
            "acceptance_criteria": [{"ac_id": "AC-1", "text": "执行受保护动作"}],
            "tasks": [{
                "task_id": "DELETE",
                "type": "business",
                "method": "DELETE",
                "side_effect": "destructive",
                "object_ref": "order-1",
                "acceptance_refs": ["AC-1"],
            }],
        }
        plan = compile_plan(intent)
        intent["review"] = {
            "status": "approved",
            "approved_case_ids": ["DELETE"],
            "execution_case_ids": ["DELETE"],
            "review_id": "REV-RISK",
            "reviewer": "qa",
            "approved_at": "2026-11-01T05:00:00+08:00",
            "plan_fingerprint": plan["plan_fingerprint"],
            "reviewed_fields": ["case", "request", "assertions", "data_dependencies", "side_effects"],
        }
        compile_and_create_run(intent, root, run_id="RUN-RISK")
        self.assertEqual(next_action(root)["approval_kind"], "execution_approval")
        record_execution_authorization(root, self._execution_authorization(["DELETE"], "AUTH-RISK"))
        action = next_action(root)
        self.assertEqual(action["approval_kind"], "high_risk_authorization")
        self.assertTrue(action["design_review_still_valid"])
        with self.assertRaises(ApprovalRequired):
            execute_slice(root, "S1")
        blocked_action = next_action(root)
        self.assertEqual(blocked_action["approval_kind"], "high_risk_authorization")
        self.assertTrue(blocked_action["design_review_still_valid"])
        result = execute_slice(root, "S1", approval_token="risk-token", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/delete.json"})
        self.assertEqual(result["status"], "passed")
        self.assertNotEqual(load_state(root)["status"], "blocked")
        self.assertEqual(load_state(root)["run_id"], "RUN-RISK")

    def test_compile_failure_returns_structured_diagnosis(self):
        design = {
            "requirement_id": "REQ-DIAG-ORCH",
            "mode": "EXECUTE",
            "design_fingerprint": "sha256:" + "b" * 64,
            "review": {"status": "pending_review", "approved_case_ids": []},
            "cases": [{"case_id": "C1", "request": {"method": "GET", "path": "/health"}}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(Exception) as raised:
                compile_approved_design_to_package(design, Path(temp_dir) / "package.json")
            diagnosis = raised.exception.args[0]
            self.assertEqual(diagnosis["status"], "blocked")
            self.assertEqual(diagnosis["issues"][0]["code"], "REVIEW_DECISION_NOT_PERSISTED")
            self.assertFalse(diagnosis["execution_allowed"])

    def test_approved_design_compiles_to_package_without_second_review(self):
        design = {
            "requirement_id": "REQ-PACKAGE-1",
            "mode": "EXECUTE",
            "design_fingerprint": "sha256:" + "a" * 64,
            "review": {
                "status": "approved",
                "approved_case_ids": ["C1"],
                "execution_case_ids": ["C1"],
                "review_id": "REV-PACKAGE-1",
                "reviewer": "user",
                "approved_at": "2026-11-01T10:00:00+08:00",
                "design_fingerprint": "sha256:" + "a" * 64,
                "reviewed_fields": {"C1": ["case", "request", "assertions", "data_dependencies", "side_effects"]},
            },
            "cases": [{
                "case_id": "C1",
                "request": {"method": "GET", "path": "/orders/1"},
                "assertions": [{"assertion_id": "A1"}],
                "side_effect": "read",
                "execution_dependencies": [],
            }],
        }
        design["design_fingerprint"] = calculate_design_fingerprint(design)
        design["review"]["design_fingerprint"] = design["design_fingerprint"]
        assets = {
            "project": {"workspace_id": "oms"},
            "test_config": {"schema_version": "2.1", "config_revision": 1, "environment_id": "test", "path": "docs/test-config.md", "fingerprint": "sha256:" + "1" * 64},
            "contract": {"profile_path": "docs/interface-profile/orders.md", "governance_schema_version": "1.0", "profile_id": "oms:orders", "profile_revision": 1, "code_baseline": "commit", "lifecycle": "confirmed", "document_fingerprint": "sha256:" + "2" * 64, "contract_fingerprint": "sha256:" + "3" * 64},
            "environment": {"environment_id": "test", "production": False, "runtime_bindings": {"base_url": {"key": "BASE_URL", "required": True, "secret": False, "format": "url"}, "auth": {"type": "none"}}},
            "risk": {"side_effect_level": "read_only"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = Path(temp_dir) / "package.json"
            result = compile_approved_design_to_package(design, package_path, execution_assets=assets)
            self.assertTrue(package_path.is_file())
            self.assertTrue(result["review_reused"])
            self.assertFalse(result["semantic_review_required"])
            package = json.loads(package_path.read_text(encoding="utf-8"))
            self.assertEqual(package["review"]["status"], "approved")
            self.assertEqual(package["review"]["approved_case_ids"], ["C1"])

    def test_existing_result_cannot_be_overwritten_by_different_reduction(self):
        root = Path(tempfile.mkdtemp()) / "run"
        intent = self._intent()
        intent["review"] = self._approved_review(intent)
        compile_and_create_run(intent, root, run_id="RUN-ORCH-3")
        record_execution_authorization(root, self._execution_authorization(["READ"], "AUTH-ORCH-3"))
        evidence_ref = self._write_evidence(root, "read.json")
        execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": evidence_ref})
        reduce_run(root)
        result_path = root / "result.json"
        saved = json.loads(result_path.read_text(encoding="utf-8"))
        saved["verdict"] = "failed"
        result_path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "different content"):
            reduce_run(root)


if __name__ == "__main__":
    unittest.main()
