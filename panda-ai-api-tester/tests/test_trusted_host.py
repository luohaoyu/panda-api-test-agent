import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from trusted_host import execute_test_package


class TrustedHostTests(unittest.TestCase):
    def _package(self):
        return {
            "schema_version": "2.1",
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
                    "auth": {
                        "type": "bearer",
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

    def test_preflight_block_is_timed_and_secret_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_path = root / "package.json"
            package_path.write_text(json.dumps(self._package()), encoding="utf-8")
            outcome_path = root / "host-outcome.json"
            secret = "secret-token-must-not-persist"
            outcome = execute_test_package(
                package_path,
                root / "run",
                root / "report.html",
                runtime_values={"TEST3_BASE_URL": "bad-url", "TEST3_TOKEN": secret},
                outcome_path=outcome_path,
            )
            self.assertEqual(outcome["status"], "blocked")
            self.assertEqual(outcome["stopped_at_stage"], "runtime_preflight")
            self.assertGreaterEqual(outcome["duration_ms"], 0)
            self.assertEqual(outcome["stages"]["runtime_preflight"]["status"], "blocked")
            persisted = outcome_path.read_text(encoding="utf-8")
            self.assertNotIn(secret, persisted)
            self.assertIn("TEST3_TOKEN", persisted)
            self.assertFalse((root / "run").exists())
            self.assertFalse((root / "report.html").exists())

    def test_package_execution_control_blocks_before_runtime_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package = self._package()
            package["execution_control"] = {
                "fail_closed_before_http": True,
                "formal_http_allowed": False,
                "stop_stage": "auth_probe_path_preflight",
                "blocking_gap_ids": ["GAP-AUTH-PROBE-01"],
                "reason": "no safe auth probe",
            }
            package["known_gaps"] = [
                {"gap_id": "GAP-AUTH-PROBE-01", "blocking": True, "stage": "probe", "description": "blocked"}
            ]
            package_path = root / "package.json"
            package_path.write_text(json.dumps(package), encoding="utf-8")
            outcome_path = root / "host-outcome.json"
            outcome = execute_test_package(
                package_path,
                root / "run",
                root / "report.html",
                runtime_values={},
                outcome_path=outcome_path,
            )
            self.assertEqual(outcome["status"], "blocked")
            self.assertEqual(outcome["stopped_at_stage"], "package_execution_preflight")
            self.assertIn("GAP-AUTH-PROBE-01", outcome["reason"])
            self.assertFalse((root / "run").exists())
            self.assertFalse((root / "report.html").exists())

    def test_missing_binding_blocks_without_creating_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_path = root / "package.json"
            package_path.write_text(json.dumps(self._package()), encoding="utf-8")
            outcome = execute_test_package(
                package_path,
                root / "run",
                root / "report.html",
                runtime_values={"TEST3_BASE_URL": "https://test.example"},
            )
            self.assertEqual(outcome["status"], "blocked")
            self.assertIn("TEST3_TOKEN", outcome["reason"])
            self.assertFalse((root / "run").exists())


if __name__ == "__main__":
    unittest.main()
