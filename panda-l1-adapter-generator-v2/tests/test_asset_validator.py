import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from asset_validator import AssetValidationError, validate_config, validate_profile


CONFIG = '''```yaml test-config
metadata:
  schema_version: "2.1"
  project_id: demo
  config_revision: 1
environments:
  test:
    runtime_bindings: {}
safety:
  credentials_persistence: forbidden
```'''


PROFILE = '''```yaml interface-profile-governance
schema_version: "1.0"
profile_id: demo:orders
profile_revision: 1
module_id: orders
code_baseline: abc123
lifecycle: drafted
owners:
  code_owner: team-a
  business_owner: team-b
  test_owner: team-c
fingerprints:
  document_fingerprint: sha256:PLACEHOLDER
  contract_fingerprint: sha256:0000000000000000000000000000000000000000000000000000000000000000
```'''


class AssetValidatorTests(unittest.TestCase):
    def test_config_schema_21(self):
        result = validate_config(CONFIG)
        self.assertEqual(result["schema_version"], "2.1")

    def test_config_rejects_legacy_env_and_secret(self):
        with self.assertRaisesRegex(AssetValidationError, "legacy _env"):
            validate_config(CONFIG.replace("runtime_bindings", "auth_env"))
        with self.assertRaisesRegex(AssetValidationError, "runtime secrets"):
            validate_config(CONFIG.replace("safety:\n", "password: literal-secret\nsafety:\n"))

    def test_profile_requires_valid_governance(self):
        with self.assertRaisesRegex(AssetValidationError, "document_fingerprint mismatch"):
            validate_profile(PROFILE)
        import asset_validator
        normalized = asset_validator._normalize_profile(PROFILE.replace("sha256:PLACEHOLDER", "sha256:" + "0" * 64))
        fingerprint = "sha256:" + __import__("hashlib").sha256(normalized.encode("utf-8")).hexdigest()
        self.assertEqual(validate_profile(PROFILE.replace("sha256:PLACEHOLDER", fingerprint))["profile_revision"], 1)


if __name__ == "__main__":
    unittest.main()
