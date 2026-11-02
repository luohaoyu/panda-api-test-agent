import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import runner  # noqa: E402


CONTRACT_FINGERPRINT = "sha256:" + hashlib.sha256(b"contract-v1").hexdigest()
PLACEHOLDER = "sha256:" + ("0" * 64)


def build_profile(store: Path, facts_block: str = "", lifecycle: str = "executable") -> Path:
    raw = (
        "# profile\n\n```yaml interface-profile-governance\n"
        "schema_version: \"1.0\"\n"
        "profile_id: demo:orders\n"
        "profile_revision: 1\n"
        "module_id: orders\n"
        "code_baseline: abc123\n"
        f"lifecycle: {lifecycle}\n"
        "owners:\n"
        "  code_owner: team-a\n"
        "  business_owner: team-b\n"
        "  test_owner: team-c\n"
        "confirmation:\n"
        "  confirmed_by: team-a\n"
        "  confirmed_at: 2026-11-02T10:00:00+08:00\n"
        "  scope: [contract]\n"
        + (facts_block + "\n" if facts_block else "")
        + "fingerprints:\n"
        f"  document_fingerprint: {PLACEHOLDER}\n"
        f"  contract_fingerprint: {CONTRACT_FINGERPRINT}\n"
        "```\n"
    )
    fingerprint = "sha256:" + hashlib.sha256(
        runner._normalized_profile_document(raw).encode("utf-8")
    ).hexdigest()
    path = store / "interface-profile.md"
    path.write_text(raw.replace(PLACEHOLDER, fingerprint, 1), encoding="utf-8")
    return path


class ProfileFactRefTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_executable_profile_rejects_candidate_fact(self):
        profile = build_profile(
            self.root,
            "facts:\n  - fact_id: FACT-1\n    status: candidate\n    decision_owner: ai_skill\n",
        )
        with self.assertRaisesRegex(runner.RunnerError, "PROFILE_FACTS_UNCONFIRMED"):
            runner.load_interface_profile_identity(profile)

    def test_executable_profile_accepts_confirmed_facts(self):
        profile = build_profile(
            self.root,
            "facts:\n  - fact_id: FACT-1\n    status: confirmed\n    decision_owner: ai_skill\n"
            "    confirmed_by: ai:semantic\n    applies_to: [route]\n",
        )
        identity = runner.load_interface_profile_identity(profile)
        self.assertEqual(identity["fact_refs"][0]["fact_id"], "FACT-1")
        self.assertEqual(identity["fact_refs"][0]["confirmed_by"], "ai:semantic")

    def test_drafted_profile_may_hold_candidate_facts(self):
        profile = build_profile(
            self.root,
            "facts:\n  - fact_id: FACT-1\n    status: candidate\n    decision_owner: ai_skill\n",
            lifecycle="drafted",
        )
        identity = runner.load_interface_profile_identity(profile, allow_drafted=True)
        self.assertEqual(identity["fact_refs"][0]["status"], "candidate")

    def test_profiles_without_facts_keep_working(self):
        identity = runner.load_interface_profile_identity(build_profile(self.root))
        self.assertEqual(identity["fact_refs"], [])

    def test_duplicate_fact_ids_are_rejected(self):
        profile = build_profile(
            self.root,
            "facts:\n  - fact_id: FACT-1\n    status: confirmed\n    confirmed_by: ai:x\n"
            "  - fact_id: FACT-1\n    status: confirmed\n    confirmed_by: ai:x\n",
        )
        with self.assertRaisesRegex(runner.RunnerError, "duplicate fact_id"):
            runner.load_interface_profile_identity(profile)

    def test_unknown_fact_status_is_rejected(self):
        profile = build_profile(
            self.root,
            "facts:\n  - fact_id: FACT-1\n    status: trusted\n    decision_owner: ai_skill\n",
        )
        with self.assertRaisesRegex(runner.RunnerError, "unsupported status"):
            runner.load_interface_profile_identity(profile)

    def test_confirmed_fact_requires_confirmed_by(self):
        profile = build_profile(self.root, "facts:\n  - fact_id: FACT-1\n    status: confirmed\n")
        with self.assertRaisesRegex(runner.RunnerError, "confirmed_by"):
            runner.load_interface_profile_identity(profile)


if __name__ == "__main__":
    unittest.main()
