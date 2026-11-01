import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from protocol_contract import (
    build_user_decision,
    normalize_artifact_metadata,
    normalize_review,
    normalize_scan_fact,
    result_verdict_to_requirement_status,
    validate_compatibility,
)


class ProtocolContractTests(unittest.TestCase):
    def test_review_aliases_normalize_to_canonical_fields(self):
        normalized = normalize_review({
            "status": "approved",
            "design_fingerprint": "sha256:plan",
            "approved_scope": ["C1"],
            "approved_at": "2026-11-01T05:20:00+08:00",
            "reviewer": "qa",
            "reviewed_fields": ["request", "assertions"],
        })
        self.assertEqual(normalized["plan_fingerprint"], "sha256:plan")
        self.assertEqual(normalized["approved_case_ids"], ["C1"])
        self.assertEqual(normalized["decided_at"], "2026-11-01T05:20:00+08:00")
        self.assertEqual(normalized["reviewed_fields"]["C1"], ["request", "assertions"])

    def test_user_decision_is_bound_to_plan_and_scope(self):
        decision = build_user_decision({"status": "partially_approved", "approved_case_ids": ["C1"], "reviewer": "qa", "decided_at": "2026-11-01T05:20:00+08:00"}, plan_id="PLAN-1", plan_fingerprint="sha256:p", source={"type": "chat", "message_id": "MSG-1"})
        self.assertEqual(decision["artifact_type"], "user_decision")
        self.assertEqual(decision["subject"]["plan_fingerprint"], "sha256:p")
        self.assertEqual(decision["decision"]["approved_case_ids"], ["C1"])
        self.assertEqual(decision["source"]["message_id"], "MSG-1")

    def test_scan_fact_remains_candidate_and_keeps_traceability(self):
        fact = normalize_scan_fact({"category": "source", "subject": {"method": "POST", "path": "/orders"}}, scan_id="SCAN-1", baseline={"commit": "abc"})
        self.assertTrue(fact["candidate_only"])
        self.assertEqual(fact["semantic_decision_owner"], "ai_skill")
        self.assertEqual(fact["scan_id"], "SCAN-1")
        self.assertTrue(fact["fact_id"].startswith("FACT-"))

    def test_artifact_metadata_and_compatibility_are_explicit(self):
        artifact = normalize_artifact_metadata({"plan_id": "PLAN-1"}, "plan", producer={"skill": "test"})
        self.assertEqual(artifact["protocol_version"], "1.0")
        self.assertEqual(artifact["artifact_type"], "plan")
        self.assertEqual(artifact["schema_version"], "2.0")
        self.assertEqual(validate_compatibility(artifact, "plan"), [])
        artifact["schema_version"] = "9.0"
        self.assertTrue(validate_compatibility(artifact, "plan"))

    def test_requirement_projection_does_not_turn_partial_or_none_into_accepted(self):
        self.assertEqual(result_verdict_to_requirement_status("passed", api_verifiability="partial", external_evidence_status="partial"), "not_verified")
        self.assertEqual(result_verdict_to_requirement_status("passed", api_verifiability="none"), "not_verified")
        self.assertEqual(result_verdict_to_requirement_status("passed"), "accepted")
        self.assertEqual(result_verdict_to_requirement_status("blocked"), "blocked")


if __name__ == "__main__":
    unittest.main()
