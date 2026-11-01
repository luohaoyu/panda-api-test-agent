import importlib.util
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

    def test_render_uses_three_rates(self):
        body = report.render(sample())
        self.assertIn("执行触达率", body)
        self.assertIn("完整覆盖率", body)
        self.assertIn("验收通过率", body)
        self.assertIn("50.0%", body)


if __name__ == "__main__":
    unittest.main()
