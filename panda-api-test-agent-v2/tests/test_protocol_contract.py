import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from protocol_contract import (
    build_user_decision,
    normalize_artifact_metadata,
    evaluate_evidence_closure,
    normalize_evidence_fact,
    normalize_fact_decision,
    normalize_review,
    normalize_scan_fact,
    require_confirmed_facts,
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

    def test_evidence_closure_continues_when_safe_action_exists(self):
        result = evaluate_evidence_closure(
            [{
                "fact_id": "FACT-REQ",
                "status": "unavailable",
                "checked_sources": [{"source_id": "provided-url", "status": "unavailable"}],
                "blocking_scope": {"stage": "requirement_mapping"},
            }],
            candidate_actions=[{"action": "inspect_local_contract", "available": True, "safe": True, "valuable": True}],
        )
        self.assertEqual(result["decision_state"], "FACT_GATHERING")
        self.assertEqual(result["next_action"], "inspect_local_contract")
        self.assertIsNone(result["stop_proof"])

    def test_unchecked_fact_cannot_become_user_block(self):
        result = evaluate_evidence_closure([{"fact_id": "FACT-AC", "status": "not_checked"}])
        self.assertEqual(result["decision_state"], "FACT_GATHERING")
        self.assertEqual(result["next_action"], "inspect_unchecked_evidence")

    def test_stop_proof_requires_exhausted_safe_actions(self):
        result = evaluate_evidence_closure(
            [{
                "fact_id": "FACT-AC",
                "status": "unavailable",
                "checked_sources": [{"source_id": "source-a", "status": "unavailable"}],
                "user_input": {"kind": "requirement_fact", "one_of": ["prd", "contract"]},
                "resume_from": "requirement_mapping",
            }],
            candidate_actions=[{"action": "unauthorized-source", "available": False, "safe": False}],
        )
        self.assertEqual(result["decision_state"], "BLOCKED_USER")
        self.assertEqual(result["stop_proof"]["missing_fact_ids"], ["FACT-AC"])
        self.assertEqual(result["stop_proof"]["resume_from"], ["requirement_mapping"])

    def test_degraded_ready_preserves_unresolved_facts(self):
        result = evaluate_evidence_closure(
            [{
                "fact_id": "FACT-AC",
                "status": "insufficient",
                "checked_sources": [{"source_id": "contract", "status": "insufficient"}],
            }],
            degraded_scope=["interface_level_design"],
        )
        self.assertEqual(result["decision_state"], "DEGRADED_READY")
        self.assertEqual(result["unresolved_fact_ids"], ["FACT-AC"])

    def test_unavailable_fact_requires_checked_source(self):
        with self.assertRaisesRegex(ValueError, "checked_sources"):
            normalize_evidence_fact({"fact_id": "FACT-1", "status": "unavailable"})

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

    def test_fact_decision_requires_evidence_for_confirmation(self):
        with self.assertRaisesRegex(ValueError, "evidence_refs"):
            normalize_fact_decision({"status": "confirmed", "actor_type": "ai", "actor_id": "ai:semantic"}, fact_id="FACT-1")

    def test_fact_decision_cannot_upgrade_ai_owned_fact_for_reviewer(self):
        with self.assertRaisesRegex(ValueError, "confirmed by ai"):
            normalize_fact_decision(
                {"status": "confirmed", "actor_type": "ai", "actor_id": "ai:semantic", "evidence_refs": ["code:1"]},
                fact_id="FACT-1",
                fact_owner="reviewer",
            )

    def test_fact_decision_is_canonical_and_traceable(self):
        decision = normalize_fact_decision(
            {"status": "confirmed", "actor_type": "reviewer", "actor_id": "backend-owner", "evidence_refs": ["sig:1"], "decided_at": "2026-11-02T10:00:00+08:00"},
            fact_id="FACT-1",
            fact_owner="reviewer",
        )
        self.assertEqual(decision["artifact_type"], "scan_fact_decision")
        self.assertEqual(decision["decision_owner"], "reviewer")
        self.assertEqual(decision["evidence_refs"], ["sig:1"])
        self.assertEqual(validate_compatibility(decision, "scan_fact_decision"), [])

    def test_rejection_requires_reason(self):
        with self.assertRaisesRegex(ValueError, "rationale"):
            normalize_fact_decision({"status": "rejected", "actor_type": "ai", "actor_id": "ai:semantic", "evidence_refs": ["x"]}, fact_id="FACT-1")

    def test_require_confirmed_facts_flags_candidates(self):
        errors = require_confirmed_facts([
            {"fact_id": "FACT-1", "status": "confirmed"},
            {"fact_id": "FACT-2", "status": "candidate"},
            {"fact_id": "FACT-3", "status": "trusted"},
        ], context="package")
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("FACT-2" in item for item in errors))
        self.assertTrue(any("FACT-3" in item for item in errors))
        self.assertEqual(require_confirmed_facts([{"fact_id": "FACT-1", "status": "confirmed"}]), [])


if __name__ == "__main__":
    unittest.main()
