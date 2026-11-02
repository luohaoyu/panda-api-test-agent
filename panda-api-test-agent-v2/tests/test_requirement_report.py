import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

MODULE = Path(__file__).parents[1] / "scripts" / "render_requirement_report.py"
spec = importlib.util.spec_from_file_location("requirement_report", MODULE)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def sample():
    return {
        "schema_version": "1.0",
        "requirement": {"id": "REQ-1", "title": "示例", "acceptance_total": 2},
        "acceptance_items": [
            {"id": "AC01", "title": "完整失败", "execution_status": "complete", "verdict": "failed"},
            {"id": "AC02", "title": "未执行", "execution_status": "not_run", "verdict": "not_run"},
        ],
        "issues": [{"id": "DEF-1", "type": "defect", "severity": "blocker", "title": "阻断问题", "status": "open"}],
        "evidence_runs": [{"id": "run-1", "immutable": True}],
    }


class RequirementReportTests(unittest.TestCase):
    def test_valid_manifest_and_fail_verdict(self):
        data = sample()
        self.assertEqual(report.validate(data), [])
        self.assertEqual(report.overall_verdict(data)[0], "FAIL")

    def test_pass_requires_complete_execution(self):
        data = sample()
        data["acceptance_items"][0]["execution_status"] = "partial"
        data["acceptance_items"][0]["verdict"] = "passed"
        self.assertTrue(any("passed requires complete" in item for item in report.validate(data)))

    def test_pass_requires_business_execution_evidence(self):
        data = sample()
        data["acceptance_items"][0] = {
            "id": "AC01", "title": "完整通过", "execution_status": "complete",
            "verdict": "passed", "evidence_refs": ["run-1"]
        }
        data["evidence_runs"][0]["kind"] = "gate"
        errors = report.validate(data)
        self.assertTrue(any("business_execution" in item for item in errors))

    def test_render_uses_three_rates(self):
        body = report.render(sample())
        self.assertIn("执行触达率", body)
        self.assertIn("完整覆盖率", body)
        self.assertIn("验收通过率", body)
        self.assertIn("50.0%", body)

    def test_render_is_byte_deterministic(self):
        self.assertEqual(report.render(sample()), report.render(sample()))

    def test_render_does_not_read_clock(self):
        self.assertNotIn("now(", inspect.getsource(report.render))

    def test_generated_at_comes_from_manifest(self):
        data = sample()
        data["generated_at"] = "2026-11-01T22:18:01+08:00"
        self.assertIn("2026-11-01T22:18:01+08:00", report.render(data))
        self.assertIn("未标注（manifest 未提供 generated_at）", report.render(sample()))

    def _render_cli(self, manifest, output, env=None):
        subprocess.run(
            [sys.executable, str(MODULE), str(manifest), "--output", str(output)],
            check=True, capture_output=True, env=env,
        )

    def test_cli_output_is_stable_and_lf_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "requirement-report.json"
            manifest.write_text(json.dumps(sample(), ensure_ascii=False), encoding="utf-8")
            first, second = Path(tmp) / "a.html", Path(tmp) / "b.html"
            self._render_cli(manifest, first)
            self._render_cli(manifest, second)
            a, b = first.read_bytes(), second.read_bytes()
            self.assertEqual(a, b, "同一 manifest 两次渲染必须字节相等")
            self.assertNotIn(b"\r\n", a, "产物不得包含平台相关的 CRLF")

    @unittest.skipUnless(hasattr(time, "tzset"), "tzset requires a POSIX platform")
    def test_cli_output_is_independent_of_local_timezone(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "requirement-report.json"
            manifest.write_text(json.dumps(sample(), ensure_ascii=False), encoding="utf-8")
            digests = []
            for zone in ("UTC", "Asia/Shanghai", "America/New_York"):
                env = dict(os.environ, TZ=zone)
                target = Path(tmp) / f"{zone.replace('/', '_')}.html"
                self._render_cli(manifest, target, env=env)
                digests.append(target.read_bytes())
            self.assertEqual(len(set(digests)), 1, "产物不得随本地时区变化")

    def test_business_execution_run_requires_result_path(self):
        data = sample()
        data["evidence_runs"] = [{"id": "run-1", "kind": "business_execution", "immutable": True}]
        errors = report.validate(data)
        self.assertTrue(any("business_execution run requires result_path" in e for e in errors),
                        "business_execution 运行必须声明 result_path")
        data["evidence_runs"] = [{"id": "run-1", "immutable": True}]
        self.assertEqual([e for e in report.validate(data) if "result_path" in e], [])

    def test_evidence_path_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = sample()
            data["acceptance_items"][0] = {
                "id": "AC01", "title": "完整失败", "execution_status": "complete", "verdict": "failed",
            }
            data["evidence_runs"] = [{
                "id": "run-1", "kind": "business_execution", "immutable": True,
                "result_path": "evidence/missing/result.json",
            }]
            errors = report.validate(data, base_dir=tmp)
            self.assertTrue(any("evidence file not found" in e for e in errors))
            real = Path(tmp) / "evidence"
            real.mkdir()
            (real / "result.json").write_text("{}", encoding="utf-8")
            data["evidence_runs"][0]["result_path"] = "evidence/result.json"
            self.assertEqual([e for e in report.validate(data, base_dir=tmp) if "not found" in e], [])

    def test_golden_snapshot(self):
        golden = Path(__file__).parent / "golden" / "requirement_report_sample.html"
        self.assertTrue(golden.is_file(), "golden 快照缺失，请先运行 tools/update_golden.py")
        self.assertEqual(report.render(sample()), golden.read_text(encoding="utf-8"))

    def test_shared_theme_is_inlined_and_versioned(self):
        import hashlib
        body = report.render(sample())
        self.assertIn("--canvas:", body, "统一令牌必须来自共享主题")
        for legacy in ("var(--redbg)", "var(--amberbg)", "var(--greenbg)", "var(--bluebg)", "var(--bg)"):
            self.assertNotIn(legacy, body, f"旧令牌 {legacy} 不得残留")
        self.assertIn(f"Template {report.TEMPLATE_ID}@{report.TEMPLATE_VERSION}", body,
                      "产物必须可追溯模板版本")
        self.assertIn(f"base-css:{report.BASE_CSS_SHA256[:12]}", body,
                      "产物必须记录共享主题指纹")
        self.assertEqual(report.BASE_CSS_SHA256,
                         hashlib.sha256(report.BASE_CSS.encode("utf-8")).hexdigest())



if __name__ == "__main__":
    unittest.main()
