import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from environment_secret_store import (
    EnvironmentSecretStoreError,
    environment_secret_path,
    read_environment_secrets,
    write_environment_secrets,
)
from runtime_value_resolver import RuntimeValueResolutionError, resolve_runtime_values


class RuntimeValueResolverTests(unittest.TestCase):
    def _package(self):
        return {
            "schema_version": "2.1",
            "test_config": {"path": "docs/test-config.md"},
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
                    "auth": {
                        "type": "basic",
                        "username": {
                            "key": "TEST3_USERNAME",
                            "required": True,
                            "secret": False,
                            "format": "string",
                        },
                        "password": {
                            "key": "TEST3_PASSWORD",
                            "required": True,
                            "secret": True,
                            "format": "string",
                        },
                    },
                },
            },
        }

    def _project(self, root, config=None):
        package = self._package()
        package_path = root / ".panda-api-test-agent" / "runs" / "RUN-001" / "package.json"
        package_path.parent.mkdir(parents=True)
        package_path.write_text(json.dumps(package), encoding="utf-8")
        config_path = root / "docs" / "test-config.md"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            config
            or """# test config
```yaml test-config
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
        value: "https://config.example/api"
      allowed_origins:
        key: TEST3_ALLOWED_ORIGINS
        required: true
        secret: false
        format: origin_list
        value: "https://config.example"
      auth:
        type: basic
        username:
          key: TEST3_USERNAME
          required: true
          secret: false
          format: string
          value: config-user
        password:
          key: TEST3_PASSWORD
          required: true
          secret: true
          format: string
          secret_ref: environments/test3/password
```
""",
            encoding="utf-8",
        )
        return package, package_path

    def test_persistent_store_uses_fixed_path_and_exact_read(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package, package_path = self._project(root)
            target = write_environment_secrets(
                package_path, package, {"TEST3_PASSWORD": "persistent-password"}
            )
            self.assertEqual(
                target,
                (
                    root
                    / ".panda-api-test-agent"
                    / "local"
                    / "secrets"
                    / "environments"
                    / "test3.secret.json"
                ).resolve(),
            )
            decoy = target.parent / "other.secret.json"
            decoy.write_text("{invalid", encoding="utf-8")
            self.assertEqual(
                read_environment_secrets(package_path, package),
                {"TEST3_PASSWORD": "persistent-password"},
            )
            self.assertTrue(decoy.exists())
            self.assertFalse(any(path.name.endswith(".writing.json") for path in target.parent.iterdir()))

    def test_store_rejects_escape_invalid_root_and_undeclared_binding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package, package_path = self._project(root)
            with self.assertRaises(EnvironmentSecretStoreError):
                environment_secret_path(package_path, "../escape")
            with self.assertRaisesRegex(EnvironmentSecretStoreError, "undeclared"):
                write_environment_secrets(
                    package_path,
                    package,
                    {"TEST3_PASSWORD": "secret", "TEST3_BASE_URL": "https://bad.example"},
                )
            with self.assertRaisesRegex(Exception, "under .panda-api-test-agent"):
                environment_secret_path(root / "package.json", "test3")

    def test_resolver_applies_config_persistent_run_and_explicit_precedence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package, package_path = self._project(root)
            write_environment_secrets(
                package_path, package, {"TEST3_PASSWORD": "persistent-password"}
            )
            resolved = resolve_runtime_values(
                package_path,
                package,
                runtime_values={"TEST3_BASE_URL": "https://explicit.example/api"},
                run_secret_values={"TEST3_PASSWORD": "run-password"},
            )
            self.assertEqual(resolved["TEST3_BASE_URL"], "https://explicit.example/api")
            self.assertEqual(resolved["TEST3_ALLOWED_ORIGINS"], "https://config.example")
            self.assertEqual(resolved["TEST3_USERNAME"], "config-user")
            self.assertEqual(resolved["TEST3_PASSWORD"], "run-password")

    def test_resolver_accepts_explicit_host_secret_and_rejects_undeclared_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package, package_path = self._project(root)
            with self.assertRaisesRegex(RuntimeValueResolutionError, "undeclared"):
                resolve_runtime_values(
                    package_path, package, runtime_values={"UNKNOWN": "value"}
                )
            resolved = resolve_runtime_values(
                package_path,
                package,
                runtime_values={"TEST3_PASSWORD": "explicit-host-secret"},
            )
            self.assertEqual(resolved["TEST3_PASSWORD"], "explicit-host-secret")
            with self.assertRaisesRegex(RuntimeValueResolutionError, "must not also be supplied explicitly"):
                resolve_runtime_values(
                    package_path,
                    package,
                    runtime_values={"TEST3_PASSWORD": "explicit-host-secret"},
                    run_secret_values={"TEST3_PASSWORD": "run-secret"},
                )
            with self.assertRaisesRegex(RuntimeValueResolutionError, "non-secret or undeclared"):
                resolve_runtime_values(
                    package_path,
                    package,
                    run_secret_values={"TEST3_BASE_URL": "https://bad.example"},
                )

    def test_resolver_rejects_secret_value_in_config_and_environment_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = """```yaml test-config
metadata:
  schema_version: "2.1"
  project_id: demo
  config_revision: 1
environments:
  test3:
    runtime_bindings:
      base_url:
        key: TEST3_BASE_URL
        secret: false
        value: https://config.example
      allowed_origins:
        key: TEST3_ALLOWED_ORIGINS
        secret: false
        value: https://config.example
      auth:
        username:
          key: TEST3_USERNAME
          secret: false
          value: user
        password:
          key: TEST3_PASSWORD
          secret: true
          value: leaked
```
"""
            package, package_path = self._project(root, config)
            with self.assertRaisesRegex(RuntimeValueResolutionError, "must not define value"):
                resolve_runtime_values(package_path, package)

            target = write_environment_secrets(
                package_path, package, {"TEST3_PASSWORD": "persistent"}
            )
            payload = json.loads(target.read_text(encoding="utf-8"))
            payload["environment_id"] = "other"
            target.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(EnvironmentSecretStoreError, "environment_id"):
                read_environment_secrets(package_path, package)


if __name__ == "__main__":
    unittest.main()
