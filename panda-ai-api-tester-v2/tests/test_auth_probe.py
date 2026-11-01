import copy
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from runtime_context import RuntimeContext, package_execution_preflight, preflight

PROBE_GATE = {
    "gate_id": "GATE-AUTH-PROBE",
    "name": "认证探测",
    "classification": "environment",
    "request": {"method": "GET", "path": "/probe/auth"},
    "assertions": {"http_status": [200], "business_code": {"path": "$.code", "equals": 200}},
}
PROBE_DECLARATION = {
    "strategy": "declared",
    "gate_id": "GATE-AUTH-PROBE",
    "side_effect_free": {
        "confirmed_by": "qa-owner",
        "reference": "docs/probe-contract.md",
        "rationale": "只读探测入口，不写库、不改登录态",
    },
}


class AuthProbeDeclarationTests(unittest.TestCase):
    def _package(self, *, with_probe=True, credential=True, read_only_required=False):
        bindings = {
            "base_url": {"key": "TEST3_BASE_URL", "required": True, "secret": False, "format": "url"},
        }
        if read_only_required:
            bindings["read_only_operations"] = {
                "key": "TEST3_READ_ONLY_OPERATIONS",
                "required": True,
                "secret": False,
                "format": "operation_list",
            }
        if credential:
            bindings["auth"] = {
                "type": "bearer",
                "credential": {"key": "TEST3_TOKEN", "required": True, "secret": True, "format": "string"},
            }
        package = {
            "schema_version": "2.1",
            "environment": {"environment_id": "test3", "production": False, "runtime_bindings": bindings},
            "gates": [copy.deepcopy(PROBE_GATE)],
            "known_gaps": [{"gap_id": "GAP-AUTH-PROBE-01", "blocking": True}],
        }
        if with_probe:
            package["auth_probe"] = copy.deepcopy(PROBE_DECLARATION)
        return package

    def test_missing_declaration_allows_business_request_auth_observation(self):
        errors = package_execution_preflight(self._package(with_probe=False))
        self.assertEqual(errors, [])

    def test_missing_declaration_is_allowed_without_credentials(self):
        errors = package_execution_preflight(self._package(with_probe=False, credential=False))
        self.assertEqual(errors, [])

    def test_declared_probe_requires_side_effect_evidence(self):
        package = self._package()
        package["auth_probe"].pop("side_effect_free")
        errors = package_execution_preflight(package)
        self.assertTrue(any("side_effect_free evidence" in item for item in errors), errors)

    def test_declared_probe_rejects_pending_confirmation(self):
        package = self._package()
        package["auth_probe"]["side_effect_free"]["confirmed_by"] = "pending"
        errors = package_execution_preflight(package)
        self.assertTrue(any("confirmed_by must be confirmed" in item for item in errors), errors)

    def test_declared_probe_requires_existing_safe_gate(self):
        package = self._package()
        package["auth_probe"]["gate_id"] = "GATE-MISSING"
        errors = package_execution_preflight(package)
        self.assertTrue(any("does not reference a declared gate" in item for item in errors), errors)

        package = self._package()
        package["gates"][0]["request"] = {"method": "POST", "path": "/probe/auth"}
        errors = package_execution_preflight(package)
        self.assertTrue(any("must use GET, HEAD or OPTIONS" in item for item in errors), errors)

    def test_declared_probe_requires_static_path_and_assertions(self):
        package = self._package()
        package["gates"][0]["request"] = {"method": "GET", "path": ""}
        package["gates"][0]["assertions"] = []
        errors = package_execution_preflight(package)
        self.assertTrue(any("static absolute path" in item for item in errors), errors)
        self.assertTrue(any("requires assertions" in item for item in errors), errors)

    def test_none_strategy_is_invalid_for_credential_auth(self):
        package = self._package()
        package["auth_probe"] = {"strategy": "none"}
        errors = package_execution_preflight(package)
        self.assertTrue(any("strategy=none is invalid" in item for item in errors), errors)

    def test_blocked_strategy_blocks_before_http(self):
        package = self._package()
        package["auth_probe"] = {
            "strategy": "blocked",
            "blocked": {
                "gap_id": "GAP-AUTH-PROBE-01",
                "reason": "无可靠无副作用 Bearer 探测入口",
                "required_from": "环境安全负责人",
            },
        }
        errors = package_execution_preflight(package)
        self.assertTrue(any("auth probe path not confirmed" in item for item in errors), errors)
        self.assertTrue(any("GAP-AUTH-PROBE-01" in item for item in errors), errors)

    def test_blocked_strategy_requires_blocking_gap(self):
        package = self._package()
        package["known_gaps"] = [{"gap_id": "GAP-AUTH-PROBE-01", "blocking": False}]
        package["auth_probe"] = {
            "strategy": "blocked",
            "blocked": {"gap_id": "GAP-AUTH-PROBE-01", "reason": "no probe"},
        }
        errors = package_execution_preflight(package)
        self.assertTrue(any("must reference a blocking gap" in item for item in errors), errors)

    def test_declared_probe_conflicts_with_blocked_execution_control(self):
        package = self._package()
        package["execution_control"] = {
            "fail_closed_before_http": True,
            "formal_http_allowed": False,
            "stop_stage": "auth_probe_path_preflight",
            "blocking_gap_ids": ["GAP-AUTH-PROBE-01"],
            "reason": "blocked",
        }
        errors = package_execution_preflight(package)
        self.assertTrue(
            any("blocks formal HTTP but auth_probe strategy=declared" in item for item in errors),
            errors,
        )

    def test_declared_probe_must_be_allowlisted_as_read_only(self):
        package = self._package(read_only_required=True)
        context = RuntimeContext.from_mapping(
            "test3",
            {
                "TEST3_BASE_URL": "https://test.example",
                "TEST3_READ_ONLY_OPERATIONS": "POST /preview",
                "TEST3_TOKEN": "secret-token-value",
            },
            secret_keys={"TEST3_TOKEN"},
        )
        errors = preflight(package, context)
        self.assertTrue(any("read_only_operations: GET /probe/auth" in item for item in errors), errors)

        context = RuntimeContext.from_mapping(
            "test3",
            {
                "TEST3_BASE_URL": "https://test.example",
                "TEST3_READ_ONLY_OPERATIONS": "GET /probe/auth,POST /preview",
                "TEST3_TOKEN": "secret-token-value",
            },
            secret_keys={"TEST3_TOKEN"},
        )
        self.assertEqual(preflight(package, context), [])


if __name__ == "__main__":
    unittest.main()
