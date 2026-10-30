import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import runner  # noqa: E402


class CleanupWaiverTests(unittest.TestCase):
    def _profile(self, temp_dir):
        profile = Path(temp_dir) / "interface-profile.md"
        profile.write_text("# test profile\n", encoding="utf-8")
        return profile

    def _package(self, temp_dir, **case_updates):
        profile = self._profile(temp_dir)
        fingerprint = "sha256:" + hashlib.sha256(profile.read_bytes()).hexdigest()
        approved_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        case = {
            "case_id": "TC-WAIVED-POST",
            "name": "单次风险豁免写请求",
            "requirement_refs": ["REQ-CLEANUP"],
            "business_rule_refs": ["BR-CLEANUP"],
            "scenario_refs": ["SC-CLEANUP"],
            "request": {
                "method": "POST",
                "path": "/items",
                "json": {"name": "test-item", "data_tag": "TEST-WAIVER-TAG"},
            },
            "assertions": [
                {
                    "assertion_id": "A-HTTP",
                    "type": "http_status",
                    "source": "http_status",
                    "operator": "equals",
                    "expected": 201,
                    "business_rule_refs": ["BR-CLEANUP"],
                },
                {
                    "assertion_id": "A-CODE",
                    "type": "business_code",
                    "source": "response_json",
                    "path": "$.code",
                    "operator": "equals",
                    "expected": 200,
                    "business_rule_refs": ["BR-CLEANUP"],
                },
                {
                    "assertion_id": "A-ID",
                    "type": "key_fields",
                    "source": "response_json",
                    "path": "$.data.id",
                    "operator": "exists",
                    "expected": True,
                    "business_rule_refs": ["BR-CLEANUP"],
                },
            ],
            "side_effect": "write",
            "object_ref": {"type": "item", "id": "static-test-item"},
            "max_executions": 1,
            "cleanup_waiver": {
                "waiver_id": "WAIVER-TEST-001",
                "scope": "case",
                "reason": "测试单次写请求且暂不具备清理接口",
                "residual_risk": "测试对象可能残留，需要人工核对",
                "approved_by": "test-approver",
                "approved_at": approved_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "max_requests": 1,
                "data_tag": "TEST-WAIVER-TAG",
            },
        }
        case.update(copy.deepcopy(case_updates))
        return {
            "schema_version": "1.0",
            "package_id": "PKG-CLEANUP-WAIVER-TEST",
            "project": {
                "workspace_id": "test-workspace",
                "system_id": "test-system",
                "requirement_refs": ["REQ-CLEANUP"],
            },
            "mode": "EXECUTE",
            "contract": {
                "profile_path": str(profile),
                "fingerprint": fingerprint,
                "lifecycle": "executable",
            },
            "environment": {
                "environment_id": "local-test",
                "base_url": "http://127.0.0.1:9",
                "production": False,
            },
            "risk": {
                "side_effect_level": "write",
                "cleanup_capability": "none",
                "cleanup_waiver_enabled": True,
            },
            "requirements": [
                {
                    "requirement_id": "REQ-CLEANUP",
                    "title": "受控写请求",
                    "description": "写请求必须受清理或风险豁免约束",
                    "required": True,
                }
            ],
            "business_rules": [
                {
                    "rule_id": "BR-CLEANUP",
                    "requirement_id": "REQ-CLEANUP",
                    "description": "写请求结果必须可验证",
                    "required": True,
                }
            ],
            "scenarios": [
                {
                    "scenario_id": "SC-CLEANUP",
                    "name": "风险豁免写请求",
                    "requirement_refs": ["REQ-CLEANUP"],
                    "business_rule_refs": ["BR-CLEANUP"],
                    "case_refs": ["TC-WAIVED-POST"],
                    "required": True,
                }
            ],
            "gates": [
                {
                    "gate_id": "GATE-HEALTH",
                    "name": "本地健康检查",
                    "classification": "environment",
                    "request": {"method": "GET", "path": "/health"},
                    "assertions": {
                        "http_status": [200],
                        "business_code": {"path": "$.code", "equals": 200},
                        "required_fields": ["$.data.status"],
                    },
                }
            ],
            "fixtures": [],
            "cases": [case],
            "known_gaps": [],
        }

    def _runner(self, package, run_dir):
        test_runner = runner.ApiTestRunner(package, run_dir)
        calls = []

        def execute(request):
            calls.append(copy.deepcopy(request))
            if request["method"] == "GET" and request["path"] == "/health":
                return {
                    "method": "GET",
                    "url": "http://127.0.0.1:9/health",
                    "request_headers": {},
                    "request_body": None,
                    "status": 200,
                    "response_headers": {"Content-Type": "application/json"},
                    "response_body": {"code": 200, "data": {"status": "ok"}},
                    "response_json": {"code": 200, "data": {"status": "ok"}},
                    "elapsed_ms": 0.1,
                }
            if request["method"] == "POST" and request["path"] == "/items":
                return {
                    "method": "POST",
                    "url": "http://127.0.0.1:9/items",
                    "request_headers": {},
                    "request_body": request.get("json"),
                    "status": 201,
                    "response_headers": {"Content-Type": "application/json"},
                    "response_body": {"code": 200, "data": {"id": "static-test-item"}},
                    "response_json": {"code": 200, "data": {"id": "static-test-item"}},
                    "elapsed_ms": 0.1,
                }
            self.fail(f"unexpected mocked request: {request}")

        test_runner.http.execute = execute
        return test_runner, calls

    def _assert_rejected(self, package, message):
        with tempfile.TemporaryDirectory() as temp_dir:
            test_runner = runner.ApiTestRunner(package, Path(temp_dir) / "run")
            with self.assertRaisesRegex(runner.RunnerError, message):
                test_runner._validate_package()

    def test_write_without_teardown_or_waiver_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            package["cases"][0].pop("cleanup_waiver")
            self._assert_rejected(package, "requires cleanup_extract")

    def test_valid_waiver_package_passes_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            test_runner = runner.ApiTestRunner(package, Path(temp_dir) / "run")
            test_runner._validate_package()

    def test_production_waiver_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            package["environment"]["production"] = True
            self._assert_rejected(package, "requires environment.production=false")

    def test_waiver_request_and_execution_limits_must_be_one(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            package["cases"][0]["cleanup_waiver"]["max_requests"] = 2
            self._assert_rejected(package, "invalid required fields")

        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            package["cases"][0]["max_executions"] = 2
            self._assert_rejected(package, "requires max_executions=1")

    def test_destructive_waiver_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(
                temp_dir,
                side_effect="destructive",
                request={"method": "DELETE", "path": "/items/static-test-item"},
            )
            self._assert_rejected(package, "cleanup_waiver is only allowed for side_effect=write")

    def test_waiver_cannot_mix_with_teardown_or_cleanup_extract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(
                temp_dir,
                teardown={"type": "http", "request": {"method": "DELETE", "path": "/items/static-test-item"}},
                cleanup_extract={"item_id": "$.data.id"},
            )
            self._assert_rejected(package, "mutually exclusive with teardown/cleanup_extract")

    def test_runner_waiver_happy_path_validates_and_renders_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            run_dir = Path(temp_dir) / "run"
            test_runner, calls = self._runner(package, run_dir)
            result_path = test_runner.run()
            result = json.loads(result_path.read_text(encoding="utf-8"))

            self.assertEqual([call["method"] for call in calls], ["GET", "POST"])
            self.assertEqual(result["cleanup"]["status"], "waived")
            self.assertTrue(result["cleanup"]["pending_objects"])
            self.assertEqual(len(result["cleanup"]["waived_cases"]), 1)
            self.assertEqual(result["case_results"][0]["cleanup_disposition"], "waived")
            self.assertEqual(result["verdict"], "inconclusive")

            validator = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "validate_result.py"), str(result_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(validator.returncode, 0, validator.stdout + validator.stderr)
            self.assertIn('"valid": true', validator.stdout)

            report_path = run_dir / "report.html"
            rendered = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "render_report.py"), str(result_path), "--output", str(report_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("已风险豁免", report)
            self.assertIn("WAIVER-TEST-001", report)
            self.assertIn("测试对象可能残留，需要人工核对", report)

    def test_promote_blocks_result_with_waiver(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            run_dir = Path(temp_dir) / "run"
            test_runner, _ = self._runner(package, run_dir)
            result_path = test_runner.run()
            generated_tests = Path(temp_dir) / "generated-tests"
            generated_tests.mkdir()
            promoted = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "promote.py"), str(result_path), str(generated_tests)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertNotEqual(promoted.returncode, 0)
            self.assertIn("PROMOTE blocked", promoted.stderr + promoted.stdout)


if __name__ == "__main__":
    unittest.main()
