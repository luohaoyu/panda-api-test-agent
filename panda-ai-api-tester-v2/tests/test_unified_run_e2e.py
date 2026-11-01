import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
TESTS = SKILL_DIR / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

from plan_compiler import compile_plan  # noqa: E402
from run_state import create_run, load_state  # noqa: E402
from runtime_context import build_runtime_context  # noqa: E402
from slice_executor import execute_package_slice  # noqa: E402
from result_reducer import reduce_run  # noqa: E402
from test_end_to_end_stub import EndToEndContractTests, TOKEN, _StubEnvironment  # noqa: E402


class UnifiedRunHttpE2ETests(unittest.TestCase):
    def test_compiled_run_executes_real_http_slice_and_reduces(self):
        helper = EndToEndContractTests()
        with tempfile.TemporaryDirectory() as temp_dir, _StubEnvironment() as stub:
            package = helper._package(temp_dir, stub.base_url)
            intent = {
                "requirement_id": "REQ-STUB",
                "environment_id": "local-stub",
                "acceptance_criteria": [{"ac_id": "AC-STUB", "text": "只读接口返回业务成功"}],
                "tasks": [
                    {"task_id": "TC-STUB-HEALTH", "case_id": "TC-STUB-HEALTH", "type": "business", "method": "GET", "side_effect": "read", "acceptance_refs": ["AC-STUB"]},
                    {"task_id": "TC-STUB-ITEM", "case_id": "TC-STUB-ITEM", "type": "business", "method": "GET", "side_effect": "read", "acceptance_refs": ["AC-STUB"]},
                ],
            }
            plan = compile_plan(intent)
            root = Path(temp_dir) / "unified-run"
            create_run(root, run_id="RUN-UNIFIED-STUB", plan_id=plan["plan_id"], plan_fingerprint=plan["plan_fingerprint"], environment_snapshot={"environment_id": "local-stub"}, plan=plan)
            context = build_runtime_context(package, {
                "STUB_BASE_URL": stub.base_url,
                "STUB_ALLOWED_ORIGINS": stub.base_url,
                "STUB_READ_ONLY_OPERATIONS": "GET /probe/auth,GET /health,GET /items/STUB-ITEM-1",
                "STUB_BEARER_TOKEN": TOKEN,
            }, context_id="ctx-unified-stub")
            executed = execute_package_slice(root, "S1", package, runtime_context=context)
            self.assertEqual(executed["status"], "passed", executed)
            result = reduce_run(root)
            self.assertEqual(result["verdict"], "passed", result)
            self.assertTrue(result["evidence_closure"]["business_evidence_closed"])
            self.assertEqual([item["path"] for item in stub.requests], ["/probe/auth", "/health", "/items/STUB-ITEM-1"])
            self.assertNotIn(TOKEN, json.dumps(result, ensure_ascii=False))
            self.assertEqual(load_state(root)["completed_slices"], ["S1"])


if __name__ == "__main__":
    unittest.main()
