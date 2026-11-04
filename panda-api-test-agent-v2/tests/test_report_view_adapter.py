import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).parents[1] / "scripts" / "report_view_adapter.py"
spec = importlib.util.spec_from_file_location("report_view_adapter", MODULE)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class ReportViewAdapterTests(unittest.TestCase):
    def _fixture(self, root: Path):
        run = root / "runs" / "RUN-1"
        evidence = run / "evidence"
        evidence.mkdir(parents=True)
        (run / "plan.json").write_text(json.dumps({
            "tasks": [
                {
                    "task_id": "TC-1", "case_id": "TC-1", "name": "相同请求只创建一次",
                    "scenario_refs": ["重复提交"], "acceptance_refs": ["AC-1"],
                    "method": "POST", "path": "/api/orders/123?debug=true",
                    "assertions": [{"id": "planned-only", "expected": 200}],
                },
                {
                    "task_id": "TC-2", "case_id": "TC-2", "title": "页面提示",
                    "acceptance_refs": ["AC-2"], "method": "GET", "path": "/api/orders/${orderId}/",
                },
            ]
        }, ensure_ascii=False), encoding="utf-8")
        (run / "result.json").write_text(json.dumps({
            "verdict": "failed",
            "task_results": [
                {"task_id": "TC-1", "status": "failed", "evidence_refs": ["evidence/case-TC-1.json"]},
                {"task_id": "TC-2", "status": "not_run", "evidence_refs": []},
            ],
            "cleanup": {"unresolved_obligations": ["TC-1"]},
        }, ensure_ascii=False), encoding="utf-8")
        (evidence / "case-TC-1.json").write_text(json.dumps({
            "case_id": "TC-1", "name": "相同请求只创建一次",
            "exchange": {
                "method": "POST", "url": "https://example.invalid/api/orders/123?debug=true",
                "request_headers": {"Authorization": "Bearer should-not-leak", "Cookie": "sid=secret"},
                "request_body": {"mobile": "13800138000", "token": "body-secret", "name": "tester"},
                "status": 200, "response_headers": {"Set-Cookie": "sid=response-secret"},
                "response_body": {"orderId": "O-1", "secret": "response-secret"},
            },
            "verification_exchange": {
                "method": "GET", "url": "/api/orders/O-1", "status": 200,
                "response_body": {"count": 2},
            },
            "assertions": [
                {"name": "订单数量", "type": "equals", "expected": 1, "observed": 2, "status": "failed"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        (evidence / "orphan.json").write_text(json.dumps({"case_id": "UNKNOWN"}), encoding="utf-8")
        manifest = {
            "schema_version": "1.0",
            "requirement": {"id": "REQ-1", "title": "订单幂等", "acceptance_total": 2},
            "acceptance_items": [
                {"id": "AC-1", "title": "不得重复创建", "execution_status": "complete", "verdict": "failed"},
                {"id": "AC-2", "title": "页面展示成功", "execution_status": "not_run", "verdict": "not_run"},
            ],
            "issues": [{"id": "DEF-1", "type": "defect", "severity": "high", "title": "重复订单"}],
            "evidence_runs": [{
                "id": "RUN-1", "kind": "business_execution", "immutable": True,
                "result_path": "runs/RUN-1/result.json",
            }],
        }
        return manifest, run

    def test_builds_scenarios_interfaces_assertions_and_exchanges(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, _ = self._fixture(Path(temp))
            view = adapter.build_report_view(manifest, base_dir=temp)
            self.assertEqual(view["report_view_version"], "1.2")
            self.assertEqual([item["name"] for item in view["scenarios"]], ["AC-2 · 页面展示成功", "重复提交"])
            failed_case = next(case for scenario in view["scenarios"] for case in scenario["cases"] if case["id"] == "TC-1")
            execution_assertion = next(item for item in failed_case["assertions"] if item["source"] == "执行断言")
            self.assertEqual(execution_assertion["actual"], 2)
            self.assertEqual(execution_assertion["layer"], "http")
            self.assertTrue(any(item["source"] == "计划检查点" for item in failed_case["assertions"]))
            self.assertEqual(failed_case["checkpoints"], failed_case["assertions"])
            self.assertIn(execution_assertion, failed_case["checkpoints_by_layer"]["http"])
            self.assertEqual(len(failed_case["exchanges"]), 2)
            self.assertEqual(failed_case["exchanges"][0]["path"], "/api/orders/123")
            self.assertEqual(view["summary"]["interface_total"], 2)
            self.assertEqual(view["summary"]["interface_executed"], 1)
            interfaces = {(item["method"], item["path"]): item for item in view["interfaces"]}
            self.assertEqual(interfaces[("GET", "/api/orders/{orderId}")]["result"], "open")
            self.assertEqual(interfaces[("POST", "/api/orders/123")]["result"], "failed")
            self.assertFalse(interfaces[("GET", "/api/orders/O-1")]["planned"])
            self.assertEqual(view["residual_risks"][0]["kind"], "unresolved_cleanup")
            self.assertEqual(view["unmapped_evidence"][0]["ref"], "evidence/orphan.json")

    def test_exchange_payload_preserved_verbatim(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, _ = self._fixture(Path(temp))
            view = adapter.build_report_view(manifest, base_dir=temp)
            body = json.dumps(view, ensure_ascii=False)
            for secret in ("should-not-leak", "sid=secret", "body-secret", "response-secret", "13800138000"):
                self.assertIn(secret, body)
            failed_case = next(case for scenario in view["scenarios"] for case in scenario["cases"] if case["id"] == "TC-1")
            self.assertIn('"mobile": "13800138000"', failed_case["exchanges"][0]["request"])
            self.assertEqual(failed_case["exchanges"][0]["request_data"]["body"]["mobile"], "13800138000")
            self.assertIn("curl -X POST", failed_case["exchanges"][0]["curl"])
            self.assertIn("Bearer should-not-leak", failed_case["exchanges"][0]["curl"])
            self.assertIn("body-secret", failed_case["exchanges"][0]["curl"])

    def test_steps_checks_and_full_messages_are_adapted(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, run = self._fixture(Path(temp))
            huge = "X" * 6000
            evidence = run / "evidence" / "case-TC-1.json"
            evidence.write_text(json.dumps({
                "case_id": "TC-1",
                "status": "passed",
                "checks": ["业务码为 200", {"name": "成功数量", "expected": 1, "actual": 1, "status": "passed"}],
                "steps": [{
                    "request": {"method": "POST", "url": "https://example.invalid/api/orders/123", "path": "/api/orders/123", "query": {}, "headers": {"Authorization": "Basic raw-secret"}, "json": {"payload": huge}, "requested_at": "2026-11-03T00:00:00+08:00"},
                    "response": {"http_status": 200, "headers": {"X-Test": "ok"}, "body": {"message": huge}, "received_at": "2026-11-03T00:00:01+08:00"},
                }],
            }, ensure_ascii=False), encoding="utf-8")
            view = adapter.build_report_view(manifest, base_dir=temp)
            case = next(case for scenario in view["scenarios"] for case in scenario["cases"] if case["id"] == "TC-1")
            self.assertTrue(any(item["source"] == "计划检查点" for item in case["assertions"]))
            self.assertTrue(any(item["source"] == "执行检查说明" for item in case["assertions"]))
            self.assertTrue(any(item["source"] == "执行检查点" for item in case["assertions"]))
            self.assertEqual(len(case["exchanges"]), 1)
            self.assertIn("Basic raw-secret", case["exchanges"][0]["request"])
            self.assertIn(huge, case["exchanges"][0]["request"])
            self.assertIn(huge, case["exchanges"][0]["response"])
            self.assertNotIn("已截断", case["exchanges"][0]["request"])
            self.assertIn("Basic raw-secret", case["exchanges"][0]["curl"])
            self.assertIn("--data-raw", case["exchanges"][0]["curl"])

    def test_checkpoint_layers_and_closure_groups_are_modelled(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, run = self._fixture(Path(temp))
            manifest["acceptance_items"][0]["interface_verifiability"] = "direct"
            manifest["acceptance_items"][1]["interface_verifiability"] = "none"
            evidence = run / "evidence" / "case-TC-1.json"
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            payload["assertions"] = [
                {"name": "HTTP 状态", "expected": 200, "actual": 200, "status": "passed", "validation_layer": "http"},
                {"name": "订单行", "expected": 1, "actual": 1, "status": "passed", "validation_layer": "db"},
                {"name": "Redis 值", "expected": "v", "actual": "v", "status": "passed", "validation_layer": "cache"},
            ]
            evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            view = adapter.build_report_view(manifest, base_dir=temp)
            case = next(case for scenario in view["scenarios"] for case in scenario["cases"] if case["id"] == "TC-1")
            self.assertEqual([item["name"] for item in case["checkpoints_by_layer"]["db"]], ["订单行"])
            self.assertEqual([item["name"] for item in case["checkpoints_by_layer"]["cache"]], ["Redis 值"])
            groups = {group["id"]: group for group in view["coverage"]["groups"]}
            self.assertEqual([item["id"] for item in groups["direct"]["items"]], ["AC-1"])
            self.assertEqual([item["id"] for item in groups["none"]["items"]], ["AC-2"])

    def test_top_level_request_response_are_adapted(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, run = self._fixture(Path(temp))
            evidence = run / "evidence" / "case-TC-1.json"
            evidence.write_text(json.dumps({
                "case_id": "TC-1", "checks": ["返回成功"],
                "request": {"method": "POST", "url": "https://example.invalid/api/orders/123", "path": "/api/orders/123", "query": {}, "headers": {}, "json": {"id": 1}, "requested_at": "2026-11-03T00:00:00+08:00"},
                "response": {"http_status": 200, "body": {"code": 200}},
            }), encoding="utf-8")
            view = adapter.build_report_view(manifest, base_dir=temp)
            case = next(case for scenario in view["scenarios"] for case in scenario["cases"] if case["id"] == "TC-1")
            self.assertEqual(len(case["exchanges"]), 1)
            self.assertIn('"id": 1', case["exchanges"][0]["request"])
            self.assertIn('"code": 200', case["exchanges"][0]["response"])

    def test_missing_run_files_degrade_without_mutating_manifest(self):
        manifest = {
            "requirement": {"acceptance_total": 0}, "acceptance_items": [], "issues": [],
            "evidence_runs": [{"id": "RUN-MISSING", "immutable": True, "result_path": "missing/result.json"}],
        }
        before = copy.deepcopy(manifest)
        with tempfile.TemporaryDirectory() as temp:
            view = adapter.build_report_view(manifest, base_dir=temp)
        self.assertEqual(manifest, before)
        self.assertEqual(view["run_index"][0]["status"], "unavailable")
        self.assertTrue(view["run_index"][0]["notes"])

    def test_marks_missing_structured_assertions_and_missing_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, run = self._fixture(Path(temp))
            payload = json.loads((run / "evidence" / "case-TC-1.json").read_text(encoding="utf-8"))
            payload["assertions"] = []
            payload["exchange"] = {"method": "POST", "url": "/api/orders"}
            (run / "evidence" / "case-TC-1.json").write_text(json.dumps(payload), encoding="utf-8")
            view = adapter.build_report_view(manifest, base_dir=temp)
            case = next(case for scenario in view["scenarios"] for case in scenario["cases"] if case["id"] == "TC-1")
            self.assertIsNone(case["evidence_note"])
            self.assertTrue(any(item["source"] == "计划检查点" for item in case["assertions"]))
            self.assertEqual(case["exchanges"][0]["response"], "响应报文缺失")


if __name__ == "__main__":
    unittest.main()
