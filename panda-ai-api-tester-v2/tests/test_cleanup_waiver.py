import copy
import hashlib
import json
import os
import re
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
from runtime_context import RuntimeContext  # noqa: E402


class CleanupWaiverTests(unittest.TestCase):
    def _profile(self, temp_dir, lifecycle="executable", revision=1):
        profile = Path(temp_dir) / "interface-profile.md"
        contract_fingerprint = "sha256:" + hashlib.sha256(b"test-contract-v1").hexdigest()
        placeholder = "sha256:" + ("0" * 64)
        raw = (
            "# test profile\n\n```yaml interface-profile-governance\n"
            "schema_version: \"1.0\"\n"
            "profile_id: test-workspace:test-module\n"
            f"profile_revision: {revision}\n"
            "module_id: test-module\n"
            "code_baseline: test-commit\n"
            f"lifecycle: {lifecycle}\n"
            "owners:\n"
            "  code_owner: backend-owner\n"
            "  business_owner: product-owner\n"
            "  test_owner: qa-owner\n"
            "confirmation:\n"
            "  confirmed_by: backend-owner\n"
            "  confirmed_at: 2026-10-30T20:00:00+08:00\n"
            "  scope: [contract, side_effect, cleanup]\n"
            "fingerprints:\n"
            f"  document_fingerprint: {placeholder}\n"
            f"  contract_fingerprint: {contract_fingerprint}\n"
            "```\n\n## Contract\n\nPOST /items\n"
        )
        document_fingerprint = "sha256:" + hashlib.sha256(
            runner._normalized_profile_document(raw).encode("utf-8")
        ).hexdigest()
        profile.write_text(raw.replace(placeholder, document_fingerprint, 1), encoding="utf-8")
        return profile

    def _test_config(self, temp_dir):
        config = Path(temp_dir) / "test-config.md"
        config.write_text(
            "# Project test config\n\n```yaml test-config\n"
            "metadata:\n  schema_version: \"2.1\"\n  project_id: test-workspace\n  config_revision: 1\n"
            "environments:\n  local-test:\n    production: false\n```\n",
            encoding="utf-8",
        )
        return config

    def _package(self, temp_dir, **case_updates):
        profile = self._profile(temp_dir)
        config = self._test_config(temp_dir)
        profile_identity = runner.load_interface_profile_identity(profile)
        config_fingerprint = "sha256:" + hashlib.sha256(config.read_bytes()).hexdigest()
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
        package = {
            "schema_version": "2.1",
            "package_id": "PKG-CLEANUP-WAIVER-TEST",
            "project": {
                "workspace_id": "test-workspace",
                "system_id": "test-system",
                "requirement_refs": ["REQ-CLEANUP"],
            },
            "test_config": {
                "path": str(config),
                "schema_version": "2.1",
                "config_revision": 1,
                "environment_id": "local-test",
                "fingerprint": config_fingerprint,
            },
            "mode": "EXECUTE",
            "contract": profile_identity,
            "environment": {
                "environment_id": "local-test",
                "production": False,
                "runtime_bindings": {
                    "base_url": {
                        "key": "LOCAL_TEST_BASE_URL",
                        "required": True,
                        "secret": False,
                        "format": "url",
                    },
                    "auth": {"type": "none"},
                },
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
        package = {
            **package,
            "review": {
                "status": "approved",
                "review_id": "REVIEW-CLEANUP-001",
                "reviewer": "test-reviewer",
                "approved_at": approved_at.isoformat(),
                "approved_scope": ["TC-WAIVED-POST"],
                "reviewed_fields": {"TC-WAIVED-POST": ["request", "assertions", "side_effect", "cleanup"]},
                "accepted_gaps": [],
            },
        }
        package["review"]["plan_fingerprint"] = runner.calculate_plan_fingerprint(package)
        return package

    def _runner(self, package, run_dir):
        context = RuntimeContext.from_mapping(
            "local-test",
            {"LOCAL_TEST_BASE_URL": "http://127.0.0.1:9"},
            context_id="ctx-cleanup-test",
        )
        test_runner = runner.ApiTestRunner(package, run_dir, runtime_context=context)
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
            context = RuntimeContext.from_mapping("local-test", {"LOCAL_TEST_BASE_URL": "http://127.0.0.1:9"})
            test_runner = runner.ApiTestRunner(package, Path(temp_dir) / "run", runtime_context=context)
            with self.assertRaisesRegex(runner.RunnerError, message):
                test_runner._validate_package()

    def test_write_without_teardown_or_waiver_tracks_residual_object(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            package["cases"][0].pop("cleanup_waiver")
            package["review"]["plan_fingerprint"] = runner.calculate_plan_fingerprint(package)
            context = RuntimeContext.from_mapping("local-test", {"LOCAL_TEST_BASE_URL": "http://127.0.0.1:9"})
            test_runner = runner.ApiTestRunner(package, Path(temp_dir) / "run", runtime_context=context)
            test_runner._validate_package()

    def test_valid_waiver_package_passes_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            context = RuntimeContext.from_mapping("local-test", {"LOCAL_TEST_BASE_URL": "http://127.0.0.1:9"})
            test_runner = runner.ApiTestRunner(package, Path(temp_dir) / "run", runtime_context=context)
            test_runner._validate_package()

    def test_package_cannot_self_declare_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            profile = Path(package["contract"]["profile_path"])
            raw = profile.read_text(encoding="utf-8").replace("lifecycle: executable", "lifecycle: drafted")
            placeholder = re.sub(
                r"(?m)^(\s*document_fingerprint:\s*)sha256:[0-9a-fA-F]{64}$",
                r"\g<1>sha256:" + ("0" * 64),
                raw,
            )
            digest = "sha256:" + hashlib.sha256(
                runner._normalized_profile_document(placeholder).encode("utf-8")
            ).hexdigest()
            raw = re.sub(
                r"(?m)^(\s*document_fingerprint:\s*)sha256:[0-9a-fA-F]{64}$",
                lambda match: match.group(1) + digest,
                placeholder,
            )
            profile.write_text(raw, encoding="utf-8")
            self._assert_rejected(package, "interface profile lifecycle mismatch")

    def test_profile_revision_change_invalidates_old_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            profile = Path(package["contract"]["profile_path"])
            raw = profile.read_text(encoding="utf-8").replace("profile_revision: 1", "profile_revision: 2")
            placeholder = re.sub(
                r"(?m)^(\s*document_fingerprint:\s*)sha256:[0-9a-fA-F]{64}$",
                r"\g<1>sha256:" + ("0" * 64),
                raw,
            )
            digest = "sha256:" + hashlib.sha256(
                runner._normalized_profile_document(placeholder).encode("utf-8")
            ).hexdigest()
            profile.write_text(
                re.sub(
                    r"(?m)^(\s*document_fingerprint:\s*)sha256:[0-9a-fA-F]{64}$",
                    lambda match: match.group(1) + digest,
                    placeholder,
                ),
                encoding="utf-8",
            )
            self._assert_rejected(package, "interface profile profile_revision mismatch")

    def test_profile_document_change_invalidates_old_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            profile = Path(package["contract"]["profile_path"])
            profile.write_text(profile.read_text(encoding="utf-8") + "\nAdditional note.\n", encoding="utf-8")
            self._assert_rejected(package, "document_fingerprint mismatch")

    def test_production_waiver_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            package["environment"]["production"] = True
            package["review"]["plan_fingerprint"] = runner.calculate_plan_fingerprint(package)
            self._assert_rejected(package, "requires environment.production=false")

    def test_waiver_request_and_execution_limits_must_be_one(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            package["cases"][0]["cleanup_waiver"]["max_requests"] = 2
            package["review"]["plan_fingerprint"] = runner.calculate_plan_fingerprint(package)
            self._assert_rejected(package, "invalid required fields")

        with tempfile.TemporaryDirectory() as temp_dir:
            package = self._package(temp_dir)
            package["cases"][0]["max_executions"] = 2
            package["review"]["plan_fingerprint"] = runner.calculate_plan_fingerprint(package)
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
