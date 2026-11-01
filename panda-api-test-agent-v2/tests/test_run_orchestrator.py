import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
L2_SCRIPTS = Path(r"C:/Users/Administrator/.workbuddy/skills/panda-ai-api-tester-v2/scripts")
sys.path.insert(0, str(L2_SCRIPTS))
sys.path.insert(0, str(SCRIPTS))
from run_orchestrator import (
    build_requirement_manifest,
    compile_and_create_run,
    compile_approved_design_to_package,
    next_action,
    reduce_run,
)
from plan_compiler import compile_plan
from package_compiler import calculate_design_fingerprint
from slice_executor import execute_slice


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
            "review_id": "REV-1",
            "reviewer": "qa",
            "approved_at": "2026-11-01T05:00:00+08:00",
            "plan_fingerprint": plan["plan_fingerprint"],
            "reviewed_fields": {"READ": ["request", "assertions", "side_effect"]},
        }

    def test_compile_creates_one_run_and_returns_first_slice(self):
        root = Path(tempfile.mkdtemp()) / "run"
        result = compile_and_create_run(self._intent(), root, run_id="RUN-ORCH-1")
        self.assertEqual(result["run"]["run_id"], "RUN-ORCH-1")
        self.assertTrue((root / "run.json").is_file())
        self.assertTrue((root / "plan.json").is_file())
        self.assertEqual(result["next_action"]["action"], "request_approval")
        self.assertEqual(next_action(root)["action"], "request_approval")
        self.assertEqual(next_action(root)["slice_id"], "S1")

    def test_execute_reduce_and_project_requirement_manifest(self):
        root = Path(tempfile.mkdtemp()) / "run"
        intent = self._intent()
        intent["review"] = self._approved_review(intent)
        compile_and_create_run(intent, root, run_id="RUN-ORCH-2")
        executed = execute_slice(
            root,
            "S1",
            task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/read.json"},
        )
        self.assertEqual(executed["status"], "passed")
        result = reduce_run(root)
        self.assertEqual(result["verdict"], "passed")
        manifest = build_requirement_manifest(root, title="编排闭环")
        self.assertEqual(manifest["acceptance_items"][0]["verdict"], "passed")
        self.assertEqual(manifest["evidence_runs"][0]["kind"], "business_execution")
        self.assertEqual(manifest["evidence_runs"][0]["immutable"], True)

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
                "review_id": "REV-PACKAGE-1",
                "reviewer": "user",
                "approved_at": "2026-11-01T10:00:00+08:00",
                "design_fingerprint": "sha256:" + "a" * 64,
                "reviewed_fields": {"C1": ["request", "assertions", "side_effect"]},
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
            "contract": {"profile_path": "docs/profile.md", "governance_schema_version": "1.0", "profile_id": "oms:orders", "profile_revision": 1, "code_baseline": "commit", "lifecycle": "confirmed", "document_fingerprint": "sha256:" + "2" * 64, "contract_fingerprint": "sha256:" + "3" * 64},
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
        execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/read.json"})
        reduce_run(root)
        result_path = root / "result.json"
        saved = json.loads(result_path.read_text(encoding="utf-8"))
        saved["verdict"] = "failed"
        result_path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "different content"):
            reduce_run(root)


if __name__ == "__main__":
    unittest.main()
