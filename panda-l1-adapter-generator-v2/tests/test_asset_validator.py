import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from asset_validator import AssetValidationError, validate_config, validate_config_update, validate_profile


CONFIG = '''```yaml test-config
metadata:
  schema_version: "2.1"
  project_id: demo
  config_revision: 1
environments:
  test:
    runtime_bindings:
      base_url:
        key: TEST_BASE_URL
        required: true
        secret: false
        format: url
        value: https://test.example.invalid
      auth_token:
        key: TEST_AUTH_TOKEN
        required: true
        secret: true
        format: string
        secret_ref: environments/test/auth-token
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
    def test_config_schema_21_allows_stable_non_secret_value(self):
        result = validate_config(CONFIG)
        self.assertEqual(result["schema_version"], "2.1")
        self.assertEqual(result["runtime_binding_count"], 2)
        self.assertEqual(result["stable_non_secret_value_count"], 1)
        self.assertEqual(result["secret_ref_count"], 1)

    def test_config_rejects_legacy_env_and_secret_literal(self):
        with self.assertRaisesRegex(AssetValidationError, "legacy _env"):
            validate_config(CONFIG.replace("runtime_bindings", "auth_env"))
        with self.assertRaisesRegex(AssetValidationError, "runtime secrets"):
            validate_config(CONFIG.replace("safety:\n", "password: literal-secret\nsafety:\n"))

    def test_secret_binding_rejects_value(self):
        invalid = CONFIG.replace(
            "        secret_ref: environments/test/auth-token",
            "        secret_ref: environments/test/auth-token\n        value: literal-secret",
        )
        with self.assertRaisesRegex(AssetValidationError, "secret=true forbids value"):
            validate_config(invalid)

    def test_secret_binding_only_allows_metadata_and_secret_ref(self):
        invalid = CONFIG.replace(
            "        secret_ref: environments/test/auth-token",
            "        secret_ref: environments/test/auth-token\n        source_path: local/secrets.json",
        )
        with self.assertRaisesRegex(AssetValidationError, "only permits binding metadata/secret_ref"):
            validate_config(invalid)

    def test_non_secret_binding_rejects_secret_ref(self):
        invalid = CONFIG.replace(
            "        value: https://test.example.invalid",
            "        secret_ref: environments/test/base-url",
        )
        with self.assertRaisesRegex(AssetValidationError, "secret=false forbids secret_ref"):
            validate_config(invalid)

    def test_revision_changes_for_stable_non_secret_value(self):
        changed_text = CONFIG.replace("config_revision: 1", "config_revision: 2").replace(
            "https://test.example.invalid", "https://test-2.example.invalid"
        )
        result = validate_config_update(CONFIG, changed_text)
        self.assertTrue(result["changed"])
        self.assertEqual(result["config_revision"], 2)
        with self.assertRaisesRegex(AssetValidationError, "must increment by one"):
            validate_config_update(
                CONFIG,
                CONFIG.replace("https://test.example.invalid", "https://test-2.example.invalid"),
            )

    def test_secret_rotation_does_not_update_config_revision(self):
        rotated_secret_store_value = "rotated-outside-canonical-config"
        self.assertNotIn(rotated_secret_store_value, CONFIG)
        result = validate_config_update(CONFIG, CONFIG)
        self.assertFalse(result["changed"])
        self.assertEqual(result["config_revision"], 1)
        with self.assertRaisesRegex(AssetValidationError, "must remain unchanged"):
            validate_config_update(CONFIG, CONFIG.replace("config_revision: 1", "config_revision: 2"))

    def test_profile_requires_valid_governance(self):
        with self.assertRaisesRegex(AssetValidationError, "document_fingerprint mismatch"):
            validate_profile(PROFILE)
        import asset_validator
        normalized = asset_validator._normalize_profile(PROFILE.replace("sha256:PLACEHOLDER", "sha256:" + "0" * 64))
        fingerprint = "sha256:" + __import__("hashlib").sha256(normalized.encode("utf-8")).hexdigest()
        self.assertEqual(validate_profile(PROFILE.replace("sha256:PLACEHOLDER", fingerprint))["profile_revision"], 1)


if __name__ == "__main__":
    unittest.main()
