"""路线B：运行级报告内嵌脱敏请求/响应明细的单元测试（函数级，协议校验由 e2e 覆盖）。"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).parents[1] / "scripts" / "render_report.py"
spec = importlib.util.spec_from_file_location("render_report_mod", MODULE)
rr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rr)


RUNNER_EXCHANGE_PAYLOAD = {
    "case_id": "B01",
    "name": "用例B01",
    "exchange": {
        "method": "POST",
        "url": "https://test3-cps.example.com/prod-api/app/order/resendScan/routeCheckAndSave",
        "request_headers": {"Authorization": "***REDACTED***", "Content-Type": "application/json"},
        "request_body": {"waybillNo": "WB001", "password": "p@ss", "token": "tk-123"},
        "status": 200,
        "response_headers": {"Content-Type": "application/json", "Set-Cookie": "***REDACTED***"},
        "response_body": {"code": 97004, "msg": "非本站运单"},
        "response_json": {"code": 97004},
        "elapsed_ms": 132.5,
    },
    "verification_exchange": None,
    "assertions": [],
}

JSONL_RECORD = {
    "case_id": "B01",
    "ts": "2026-11-01T22:09:01+08:00",
    "request": {
        "method": "POST",
        "path": "/app/order/resendScan/routeCheckAndSave",
        "headers": {"Authorization": "Bearer secret-token", "Content-Type": "application/json"},
        "body": {"waybillNo": "WB001", "password": "p@ss", "token": "tk-123", "remark": "x"},
    },
    "response": {"http": 200, "elapsed_ms": 132, "body": {"code": 97004, "msg": "非本站运单"}},
}


class RedactionTests(unittest.TestCase):
    def test_redact_json_masks_sensitive_keys_recursively(self):
        out = rr._redact_json({"a": {"password": "x", "keep": 1}, "list": [{"api_key": "k"}]})
        self.assertEqual(out["a"]["password"], "***")
        self.assertEqual(out["a"]["keep"], 1)
        self.assertEqual(out["list"][0]["api_key"], "***")

    def test_render_payload_truncates_long_text(self):
        text = rr._render_payload({"blob": "x" * 5000})
        self.assertIn("已截断", text)
        self.assertLess(len(text), 5300)

    def test_render_payload_none(self):
        self.assertIsNone(rr._render_payload(None))


class LoadExchangeTests(unittest.TestCase):
    def test_runner_convention_loaded_and_masked(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "evidence").mkdir()
            (base / "evidence" / "case-B01.json").write_text(
                json.dumps(RUNNER_EXCHANGE_PAYLOAD, ensure_ascii=False), encoding="utf-8")
            out = rr.load_http_exchanges(base / "result.json", "B01", ["evidence/case-B01.json"])
            self.assertEqual(len(out), 1)
            item = out[0]
            self.assertEqual(item["label"], "主请求")
            self.assertEqual(item["http"], 200)
            self.assertIn("97004", item["response_body"])
            self.assertNotIn("p@ss", item["request_body"], "password 值必须遮蔽")
            self.assertNotIn("tk-123", item["request_body"], "token 值必须遮蔽")
            self.assertEqual(item["headers"]["Authorization"], "***",
                             "渲染层必须对敏感头做二次兜底")

    def test_jsonl_convention_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "evidence").mkdir()
            (base / "evidence" / "B01.jsonl").write_text(
                json.dumps(JSONL_RECORD, ensure_ascii=False) + "\n", encoding="utf-8")
            out = rr.load_http_exchanges(base / "result.json", "B01", ["evidence/protocol/B01.json"])
            self.assertEqual(len(out), 1)
            item = out[0]
            self.assertNotIn("secret-token", json.dumps(item), "Authorization 值必须遮蔽")
            self.assertNotIn("tk-123", json.dumps(item))
            self.assertNotIn("p@ss", json.dumps(item))
            self.assertIn("97004", item["response_body"])

    def test_missing_evidence_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(rr.load_http_exchanges(Path(tmp) / "result.json", "B01", ["evidence/x.json"]))

    def test_corrupt_evidence_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "evidence").mkdir()
            (base / "evidence" / "B01.jsonl").write_text("{broken\n", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                rr.load_http_exchanges(base / "result.json", "B01", [])


class RenderExchangeTests(unittest.TestCase):
    def test_render_contains_sections_and_redaction_note(self):
        exchanges = rr.load_http_exchanges(
            Path(__file__).parent, "__none__", [])  # 空路径，手工构造
        exchanges = [rr._normalize_runner_exchange(RUNNER_EXCHANGE_PAYLOAD["exchange"], "主请求")]
        html = rr.render_http_exchanges(exchanges)
        self.assertIn("原始请求/响应（脱敏摘要）", html)
        self.assertIn("routeCheckAndSave", html)
        self.assertIn("敏感字段已遮蔽", html)
        self.assertNotIn("p@ss", html)

    def test_render_error_entry(self):
        html = rr.render_http_exchanges([{"error": "证据流水解析失败：x"}])
        self.assertIn("证据流水解析失败", html)

    def test_render_empty_returns_empty(self):
        self.assertEqual(rr.render_http_exchanges(None), "")
        self.assertEqual(rr.render_http_exchanges([]), "")


if __name__ == "__main__":
    unittest.main()
