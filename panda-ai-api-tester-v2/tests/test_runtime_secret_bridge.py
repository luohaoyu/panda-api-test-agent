import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from runtime_secret_bridge import (
    LocalSecretBridgeError,
    consume_local_runtime_envelope,
    secret_envelope_path,
    write_local_runtime_envelope,
)


class RuntimeSecretBridgeTests(unittest.TestCase):
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
        }

    def _package_path(self, root):
        path = root / ".panda-api-test-agent" / "runs" / "RUN-001" / "package.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(self._package()), encoding="utf-8")
        return path

    def test_writes_and_consumes_single_use_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package = self._package()
            package_path = self._package_path(root)
            secret = "single-use-token"
            target = write_local_runtime_envelope(
                package_path,
                package,
                "RUN-001",
                {"TEST3_TOKEN": secret},
            )
            self.assertTrue(target.exists())
            values = consume_local_runtime_envelope(package_path, package, "RUN-001")
            self.assertEqual(values, {"TEST3_TOKEN": secret})
            self.assertFalse(target.exists())
            with self.assertRaisesRegex(LocalSecretBridgeError, "does not exist"):
                consume_local_runtime_envelope(package_path, package, "RUN-001")

    def test_expired_envelope_is_rejected_and_deleted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package = self._package()
            package_path = self._package_path(root)
            created = datetime.now(timezone.utc) - timedelta(minutes=8)
            target = write_local_runtime_envelope(
                package_path,
                package,
                "RUN-001",
                {"TEST3_TOKEN": "expired-token"},
                created_at=created,
                ttl_seconds=60,
            )
            with self.assertRaisesRegex(LocalSecretBridgeError, "expired"):
                consume_local_runtime_envelope(package_path, package, "RUN-001")
            self.assertFalse(target.exists())

    def test_environment_and_fingerprint_mismatch_are_rejected_and_deleted(self):
        for field, value, message in (
            ("environment_id", "prod", "environment_id"),
            ("package_fingerprint", "sha256:" + "0" * 64, "package_fingerprint"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                package = self._package()
                package_path = self._package_path(root)
                target = write_local_runtime_envelope(
                    package_path,
                    package,
                    "RUN-001",
                    {"TEST3_TOKEN": "mismatched-token"},
                )
                payload = json.loads(target.read_text(encoding="utf-8"))
                payload[field] = value
                target.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(LocalSecretBridgeError, message):
                    consume_local_runtime_envelope(package_path, package, "RUN-001")
                self.assertFalse(target.exists())

    def test_only_declared_secret_bindings_can_be_written(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package = self._package()
            package_path = self._package_path(root)
            with self.assertRaisesRegex(LocalSecretBridgeError, "non-secret or undeclared"):
                write_local_runtime_envelope(
                    package_path,
                    package,
                    "RUN-001",
                    {"TEST3_TOKEN": "token", "TEST3_BASE_URL": "https://evil.example"},
                )

    def test_tampered_non_secret_binding_is_rejected_and_deleted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package = self._package()
            package_path = self._package_path(root)
            target = write_local_runtime_envelope(
                package_path,
                package,
                "RUN-001",
                {"TEST3_TOKEN": "token"},
            )
            payload = json.loads(target.read_text(encoding="utf-8"))
            payload["values"]["TEST3_BASE_URL"] = "https://evil.example"
            target.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(LocalSecretBridgeError, "only secret bindings"):
                consume_local_runtime_envelope(package_path, package, "RUN-001")
            self.assertFalse(target.exists())

    def test_invalid_json_is_deleted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package = self._package()
            package_path = self._package_path(root)
            target = secret_envelope_path(package_path, "RUN-001")
            target.parent.mkdir(parents=True)
            target.write_text("{invalid", encoding="utf-8")
            with self.assertRaisesRegex(LocalSecretBridgeError, "could not be parsed"):
                consume_local_runtime_envelope(package_path, package, "RUN-001")
            self.assertFalse(target.exists())

    def test_path_escape_and_package_outside_project_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_path = self._package_path(root)
            for run_id in ("../escape", "bad/name", "bad\\name", "C:drive"):
                with self.subTest(run_id=run_id):
                    with self.assertRaises(LocalSecretBridgeError):
                        secret_envelope_path(package_path, run_id)
            outside = root / "package.json"
            with self.assertRaisesRegex(LocalSecretBridgeError, "under .panda-api-test-agent"):
                secret_envelope_path(outside, "RUN-001")


if __name__ == "__main__":
    unittest.main()
