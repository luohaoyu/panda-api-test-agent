import json
import os
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from runtime_context import (
    RuntimeContext,
    RuntimeContextError,
    auth_config,
    binding_spec,
    build_runtime_context,
    package_binding_keys,
    package_secret_keys,
    package_execution_preflight,
    preflight,
)
import runner


class RuntimeContextTests(unittest.TestCase):
    def _package(self):
        return {
            "environment": {
                "environment_id": "test3",
                "production": False,
                "runtime_bindings": {
                    "base_url": {
                        "key": "TEST3_BASE_URL",
                        "required": True,
                        "secret": False,
                        "format": "url",
                    },
                    "allowed_origins": {
                        "key": "TEST3_ALLOWED_ORIGINS",
                        "required": True,
                        "secret": False,
                        "format": "origin_list",
                    },
                    "read_only_operations": {
                        "key": "TEST3_READ_ONLY_OPERATIONS",
                        "required": False,
                        "secret": False,
                        "format": "operation_list",
                    },
                    "auth": {
                        "type": "bearer",
                        "header": "Authorization",
                        "credential": {
                            "key": "TEST3_TOKEN",
                            "required": True,
                            "secret": True,
                            "format": "string",
                        },
                    },
                },
            },
            "gates": [
                {
                    "gate_id": "GATE-AUTH-PROBE",
                    "name": "认证探测",
                    "classification": "environment",
                    "request": {"method": "GET", "path": "/probe/auth"},
                    "assertions": {"http_status": [200]},
                }
            ],
            "auth_probe": {
                "strategy": "declared",
                "gate_id": "GATE-AUTH-PROBE",
                "side_effect_free": {
                    "confirmed_by": "qa-owner",
                    "reference": "docs/probe-contract.md",
                    "rationale": "read-only probe",
                },
            },
        }

    def _values(self):
        return {
            "TEST3_BASE_URL": "https://test.example",
            "TEST3_ALLOWED_ORIGINS": "https://test.example",
            "TEST3_READ_ONLY_OPERATIONS": "POST /preview",
            "TEST3_TOKEN": "secret-token-value",
        }

    def test_context_is_immutable_and_secret_metadata_has_no_values(self):
        package = self._package()
        context = build_runtime_context(package, self._values())
        with self.assertRaises(TypeError):
            context.values["TEST3_TOKEN"] = "changed"
        metadata = context.metadata()
        self.assertNotIn("secret-token-value", json.dumps(metadata))
        self.assertEqual(metadata["values_persisted"], False)
        self.assertEqual(metadata["schema_version"], "2.0")
        self.assertEqual(context.secret_keys, frozenset({"TEST3_TOKEN"}))

    def test_binding_schema_is_centralized(self):
        package = self._package()
        self.assertEqual(
            package_binding_keys(package),
            {"TEST3_BASE_URL", "TEST3_ALLOWED_ORIGINS", "TEST3_READ_ONLY_OPERATIONS", "TEST3_TOKEN"},
        )
        self.assertEqual(package_secret_keys(package), {"TEST3_TOKEN"})
        self.assertEqual(binding_spec(package, "base_url")["format"], "url")
        self.assertEqual(auth_config(package)["type"], "bearer")

    def test_preflight_fails_before_runner_when_binding_is_missing(self):
        package = self._package()
        values = self._values()
        values.pop("TEST3_TOKEN")
        context = RuntimeContext.from_mapping("test3", values, secret_keys=set())
        errors = preflight(package, context)
        self.assertIn("required runtime binding is missing: TEST3_TOKEN", errors)

    def test_runtime_binding_cannot_be_overwritten_by_extracted_value(self):
        package = self._package()
        context = build_runtime_context(package, self._values())
        resolver = runner.VariableResolver({}, package_binding_keys(package), context)
        with self.assertRaisesRegex(runner.RunnerError, "read-only"):
            resolver.set("TEST3_TOKEN", "other")

    def test_explicit_context_is_required_and_environment_is_not_read(self):
        package = self._package()
        old = os.environ.get("TEST3_TOKEN")
        os.environ["TEST3_TOKEN"] = "environment-token-must-not-be-used"
        try:
            with self.assertRaisesRegex(RuntimeContextError, "explicit runtime values"):
                build_runtime_context(package, None)
            with self.assertRaisesRegex(runner.RunnerError, "explicit RuntimeContext"):
                runner.ApiTestRunner(package, "unused", runtime_context=None)
        finally:
            if old is None:
                os.environ.pop("TEST3_TOKEN", None)
            else:
                os.environ["TEST3_TOKEN"] = old

    def test_undeclared_runtime_values_are_rejected(self):
        package = self._package()
        values = self._values()
        values["UNDECLARED"] = "value"
        with self.assertRaisesRegex(RuntimeContextError, "undeclared runtime bindings"):
            build_runtime_context(package, values)

    def test_secret_classification_must_match_package(self):
        package = self._package()
        context = RuntimeContext.from_mapping("test3", self._values(), secret_keys=set())
        self.assertIn("secret classification", " ".join(preflight(package, context)))

    def test_legacy_fields_are_rejected(self):
        for field, value in (
            ("base_url_env", "TEST3_BASE_URL"),
            ("base_url_key", "TEST3_BASE_URL"),
            ("auth_env", ["TEST3_TOKEN"]),
            ("allowed_origins_env", "TEST3_ALLOWED_ORIGINS"),
            ("read_only_operations_env", "TEST3_READ_ONLY_OPERATIONS"),
            ("auth", {"credential_env": "TEST3_TOKEN"}),
        ):
            package = self._package()
            package["environment"][field] = value
            self.assertTrue(preflight(package, RuntimeContext.from_mapping("test3", {})))

    def test_legacy_auth_fields_are_rejected(self):
        package = self._package()
        package["environment"]["runtime_bindings"]["auth"]["credential_env"] = "OLD_TOKEN"
        errors = preflight(package, RuntimeContext.from_mapping("test3", {}))
        self.assertIn("legacy authentication binding fields", " ".join(errors))

    def test_execution_control_is_enforced_by_runner_constructor(self):
        package = self._package()
        package["execution_control"] = {
            "fail_closed_before_http": True,
            "formal_http_allowed": False,
            "stop_stage": "auth_probe_path_preflight",
            "blocking_gap_ids": ["GAP-AUTH-PROBE-01"],
            "reason": "no safe auth probe",
        }
        package["known_gaps"] = [{"gap_id": "GAP-AUTH-PROBE-01", "blocking": True}]
        errors = package_execution_preflight(package)
        self.assertIn("GAP-AUTH-PROBE-01", " ".join(errors))
        context = RuntimeContext.from_mapping("test3", {})
        with self.assertRaisesRegex(runner.RunnerError, "package execution preflight"):
            runner.ApiTestRunner(package, "unused", runtime_context=context)

    def test_binding_formats_are_validated(self):
        package = self._package()
        values = self._values()
        values["TEST3_BASE_URL"] = "https://user:password@test.example?bad=1"
        with self.assertRaisesRegex(RuntimeContextError, "origin-safe"):
            build_runtime_context(package, values)
        values = self._values()
        values["TEST3_ALLOWED_ORIGINS"] = "https://test.example/path"
        with self.assertRaisesRegex(RuntimeContextError, "invalid origin"):
            build_runtime_context(package, values)
        values = self._values()
        values["TEST3_READ_ONLY_OPERATIONS"] = "INVALID"
        with self.assertRaisesRegex(RuntimeContextError, "invalid operation"):
            build_runtime_context(package, values)

    def test_basic_and_headers_auth_bindings(self):
        package = self._package()
        package["environment"]["runtime_bindings"]["auth"] = {
            "type": "basic",
            "username": {"key": "TEST3_USER", "required": True, "secret": False, "format": "string"},
            "password": {"key": "TEST3_PASSWORD", "required": True, "secret": True, "format": "string"},
        }
        self.assertEqual(package_secret_keys(package), {"TEST3_PASSWORD"})
        values = {
            "TEST3_BASE_URL": "https://test.example",
            "TEST3_ALLOWED_ORIGINS": "https://test.example",
            "TEST3_USER": "tester",
            "TEST3_PASSWORD": "password-value",
        }
        context = build_runtime_context(package, values)
        resolver = runner.VariableResolver({}, package_binding_keys(package), context)
        self.assertTrue(runner.AuthInjector(package, resolver).headers()["Authorization"].startswith("Basic "))

        package["environment"]["runtime_bindings"]["auth"] = {
            "type": "headers",
            "headers": {
                "X-API-Key": {"key": "TEST3_API_KEY", "required": True, "secret": True, "format": "string"}
            },
        }
        values = {
            "TEST3_BASE_URL": "https://test.example",
            "TEST3_ALLOWED_ORIGINS": "https://test.example",
            "TEST3_API_KEY": "api-key-value",
        }
        context = build_runtime_context(package, values)
        resolver = runner.VariableResolver({}, package_binding_keys(package), context)
        self.assertEqual(runner.AuthInjector(package, resolver).headers(), {"X-API-Key": "api-key-value"})


if __name__ == "__main__":
    unittest.main()
