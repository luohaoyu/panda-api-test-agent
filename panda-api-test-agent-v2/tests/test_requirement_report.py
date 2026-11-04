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

    def test_render_uses_only_decision_metrics_and_three_closure_groups(self):
        body = report.render(sample())
        payload = report.build_report_payload(sample())
        for value in ("接口执行结果", "需求验收结果", "证据完整度", "接口可闭环", "接口部分闭环", "接口不可闭环"):
            self.assertIn(value, body)
        for value in ("验收项触达率", "完整覆盖率", "验收通过率", "接口执行覆盖", "为什么是这个结论", "优先下一步"):
            self.assertNotIn(value, body)
        self.assertEqual([group["id"] for group in payload["view"]["coverage"]["groups"]], ["direct", "partial", "none"])

    def test_information_architecture_is_decision_first(self):
        data = sample()
        data.update({
            "report_id": "AR-REQ-1-CURRENT",
            "release_recommendation": "暂不发布；补齐证据后复核",
            "aggregation": {"run_count": 2, "current": {"run_id": "run-2", "status": "inconclusive"}},
        })
        body = report.render(data)
        payload = report.build_report_payload(data)
        headings = ["总体结论", "1. 需求覆盖", "2. 需求涉及接口", "3. 场景、用例与检查证据", "4. 问题、缺口与残留风险", "证据索引"]
        positions = [body.index(value) for value in headings]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(payload["report_id"], "AR-REQ-1-CURRENT")
        self.assertEqual(payload["aggregation"]["run_count"], 2)
        self.assertEqual(payload["release_recommendation"], "暂不发布；补齐证据后复核")
        self.assertIn("场景、用例、检查点和原始报文均按需展开", body)

    def test_partial_ac_shows_validation_boundary_and_external_evidence(self):
        data = sample()
        data["acceptance_items"][1].update({
            "interface_verifiability": "partial",
            "verifiability_reason": "接口只能验证后端状态",
            "interface_scope": ["后端状态"],
            "non_interface_scope": ["页面展示"],
            "recommended_validation_layers": ["UI"],
            "external_evidence": {"status": "missing", "layers": ["UI"], "refs": []},
            "gaps": ["缺少 UI 截图"],
        })
        body = report.render(data)
        for value in ("接口只能验证后端状态", "后端状态", "页面展示", "缺少 UI 截图"):
            self.assertIn(value, body)

    def test_verdict_has_status_specific_class(self):
        body = report.render(sample())
        self.assertIn('class="verdict v-${esc(data.verdict)}"', body)

    def test_render_is_byte_deterministic(self):
        self.assertEqual(report.render(sample()), report.render(sample()))

    def test_render_does_not_read_clock(self):
        self.assertNotIn("now(", inspect.getsource(report.render))

    def test_generated_at_comes_from_manifest(self):
        data = sample()
        data["generated_at"] = "2026-11-01T22:18:01+08:00"
        self.assertIn("2026-11-01T22:18:01+08:00", report.render(data))
        self.assertIn("未标注（manifest 未提供 generated_at）", report.render(sample()))

    def test_uses_independent_template_and_application_json_payload(self):
        body = report.render(sample())
        self.assertTrue(report.TEMPLATE_PATH.is_file())
        self.assertIn('<script type="application/json" id="report-payload">', body)
        self.assertIn('JSON.parse(document.getElementById("report-payload").textContent)', body)
        self.assertIn('class="scenario" data-state=', body)
        self.assertIn('data-filter="failed"', body)
        self.assertNotIn("{{BASE_CSS}}", body)
        self.assertNotIn("{{REPORT_PAYLOAD}}", body)

    def test_embedded_json_escapes_script_breakout_characters(self):
        data = sample()
        data["requirement"]["title"] = '</script><script>alert("x")</script>&\u2028'
        body = report.render(data)
        payload_text = body.split('<script type="application/json" id="report-payload">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("</script", payload_text.lower())
        self.assertIn("\\u003c/script\\u003e", payload_text)
        payload = json.loads(payload_text)
        self.assertEqual(payload["requirement"]["title"], data["requirement"]["title"])

    def test_python_prepares_payload_instead_of_business_html(self):
        source = inspect.getsource(report.render)
        self.assertIn("build_report_payload", source)
        self.assertIn("_load_template", source)
        self.assertNotIn("<section", source)
        self.assertNotIn("<table", source)

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

    def test_report_is_self_contained_and_has_complete_evidence_sections(self):
        body = report.render(sample())
        self.assertIn("<style>", body)
        self.assertNotIn('<link rel="stylesheet"', body.lower())
        self.assertNotIn("PANDA_REPORT_THEME", body)
        self.assertIn("checkpoints_by_layer", body)
        self.assertIn("预期原始值", body)
        self.assertIn("实际原始值", body)
        self.assertIn("完整原始请求与响应", body)
        self.assertIn("复制为 cURL", body)
        self.assertIn("Request", body)
        self.assertIn("Response", body)
        self.assertNotIn("复制请求", body)
        self.assertNotIn("复制响应", body)
        self.assertNotIn("已截断", body)
        self.assertIn("mobile-top", body)
        self.assertIn("mobile-bottom", body)

    def test_shared_theme_is_inlined_and_versioned(self):
        import hashlib
        body = report.render(sample())
        self.assertIn("--canvas:", body, "统一令牌必须来自共享主题")
        for legacy in ("var(--redbg)", "var(--amberbg)", "var(--greenbg)", "var(--bluebg)", "var(--bg)"):
            self.assertNotIn(legacy, body, f"旧令牌 {legacy} 不得残留")
        self.assertIn(f'"template_id":"{report.TEMPLATE_ID}"', body,
                      "产物必须可追溯模板标识")
        self.assertIn(f'"template_version":"{report.TEMPLATE_VERSION}"', body,
                      "产物必须可追溯模板版本")
        self.assertIn(f'"base_css_sha256":"{report.BASE_CSS_SHA256[:12]}"', body,
                      "产物必须记录共享主题指纹")
        self.assertEqual(report.BASE_CSS_SHA256,
                         hashlib.sha256(report.BASE_CSS.encode("utf-8")).hexdigest())



if __name__ == "__main__":
    unittest.main()
