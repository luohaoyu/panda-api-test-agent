import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from environment_secret_store import write_environment_secrets
from runtime_secret_bridge import write_local_runtime_envelope
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

    def _project_package(self, root, package=None):
        package = package or self._package()
        package["test_config"] = {"path": "docs/test-config.md"}
        package_path = root / ".panda-api-test-agent" / "runs" / "RUN-001" / "package.json"
        package_path.parent.mkdir(parents=True)
        package_path.write_text(json.dumps(package), encoding="utf-8")
        config_path = root / "docs" / "test-config.md"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            """```yaml test-config
metadata:
  schema_version: "2.1"
  project_id: demo
  config_revision: 1
environments:
  test3:
    runtime_bindings:
      base_url:
        key: TEST3_BASE_URL
        required: true
        secret: false
        format: url
        value: https://config.example
      auth:
        credential:
          key: TEST3_TOKEN
          required: true
          secret: true
          format: string
```
""",
            encoding="utf-8",
        )
        return package, package_path

    def test_preflight_block_is_timed_and_secret_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package, package_path = self._project_package(root)
            outcome_path = root / "host-outcome.json"
            secret = "secret-token-must-not-persist"
            write_environment_secrets(package_path, package, {"TEST3_TOKEN": secret})
            outcome = execute_test_package(
                package_path,
                root / "run",
                root / "report.html",
                runtime_values={"TEST3_BASE_URL": "bad-url"},
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
            package, package_path = self._project_package(root)
            outcome = execute_test_package(
                package_path,
                root / "run",
                root / "report.html",
                runtime_values={"TEST3_BASE_URL": "https://test.example"},
            )
            self.assertEqual(outcome["status"], "blocked")
            self.assertIn("TEST3_TOKEN", outcome["reason"])
            self.assertFalse((root / "run").exists())

    def test_local_secret_is_consumed_before_runtime_preflight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            package, package_path = self._project_package(project)
            secret = "local-secret-token-must-not-persist"
            secret_path = write_local_runtime_envelope(
                package_path,
                package,
                "RUN-001",
                {"TEST3_TOKEN": secret},
            )
            outcome_path = root / "host-outcome.json"
            outcome = execute_test_package(
                package_path,
                root / "run",
                root / "report.html",
                runtime_values={"TEST3_BASE_URL": "bad-url"},
                local_secret_run_id="RUN-001",
                outcome_path=outcome_path,
            )
            self.assertEqual(outcome["status"], "blocked")
            self.assertEqual(outcome["stopped_at_stage"], "runtime_preflight")
            self.assertFalse(secret_path.exists())
            persisted = outcome_path.read_text(encoding="utf-8")
            self.assertNotIn(secret, persisted)
            self.assertIn("TEST3_TOKEN", persisted)
            self.assertEqual(outcome["stages"]["local_secret_consume"]["status"], "completed")

    def test_package_preflight_blocks_before_persistent_secret_read(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package = self._package()
            package["execution_control"] = {
                "fail_closed_before_http": True,
                "formal_http_allowed": False,
                "stop_stage": "preflight",
                "blocking_gap_ids": ["GAP-1"],
                "reason": "blocked",
            }
            package["known_gaps"] = [
                {"gap_id": "GAP-1", "blocking": True, "stage": "probe", "description": "blocked"}
            ]
            package, package_path = self._project_package(root, package)
            target = package_path.parents[2] / "local" / "secrets" / "environments" / "test3.secret.json"
            target.parent.mkdir(parents=True)
            target.write_text("{invalid", encoding="utf-8")
            outcome = execute_test_package(
                package_path,
                root / "run",
                root / "report.html",
            )
            self.assertEqual(outcome["status"], "blocked")
            self.assertEqual(outcome["stopped_at_stage"], "package_execution_preflight")
            self.assertNotIn("runtime_value_resolution", outcome["stages"])

    def test_resolver_uses_persistent_secret_without_mutating_callers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package, package_path = self._project_package(root)
            write_environment_secrets(package_path, package, {"TEST3_TOKEN": "persistent-token"})
            runtime_values = {"TEST3_BASE_URL": "bad-url"}
            snapshot = dict(runtime_values)
            outcome = execute_test_package(
                package_path,
                root / "run",
                root / "report.html",
                runtime_values=runtime_values,
            )
            self.assertEqual(outcome["status"], "blocked")
            self.assertEqual(outcome["stopped_at_stage"], "runtime_preflight")
            self.assertEqual(runtime_values, snapshot)
            self.assertEqual(outcome["stages"]["runtime_value_resolution"]["status"], "completed")

    def test_local_secret_failure_blocks_before_runtime_preflight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "project"
            package, package_path = self._project_package(project)
            secret_path = write_local_runtime_envelope(
                package_path,
                package,
                "RUN-001",
                {"TEST3_TOKEN": "tampered-token"},
            )
            payload = json.loads(secret_path.read_text(encoding="utf-8"))
            payload["package_fingerprint"] = "sha256:" + "0" * 64
            secret_path.write_text(json.dumps(payload), encoding="utf-8")
            outcome = execute_test_package(
                package_path,
                root / "run",
                root / "report.html",
                runtime_values={"TEST3_BASE_URL": "https://test.example"},
                local_secret_run_id="RUN-001",
            )
            self.assertEqual(outcome["status"], "blocked")
            self.assertEqual(outcome["stopped_at_stage"], "local_secret_consume")
            self.assertIn("package_fingerprint", outcome["reason"])
            self.assertFalse(secret_path.exists())
            self.assertNotIn("runtime_preflight", outcome["stages"])


if __name__ == "__main__":
    unittest.main()
