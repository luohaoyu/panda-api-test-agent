import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import asset_validator  # noqa: E402
import scan_facts  # noqa: E402


SCAN_REPORT = {
    "source": [
        {
            "kind": "route",
            "file": "src/OrderController.java",
            "line": 42,
            "status": "candidate",
            "source": "source_scanner",
            "confidence": "high",
            "evidence": "@PostMapping(\"/orders\")",
            "method": "POST",
            "path_pattern": "/orders",
        }
    ],
    "contracts": [
        {
            "kind": "contract",
            "file": "openapi.yaml",
            "line": 12,
            "status": "discovered",
            "source": "contract_scanner",
            "confidence": "high",
            "evidence": "paths./orders.post",
            "method": "POST",
            "path_pattern": "/orders",
        }
    ],
    "deployment": [
        {
            "kind": "service",
            "file": "docker-compose.yml",
            "line": 8,
            "status": "candidate",
            "source": "deployment_scanner",
            "confidence": "medium",
            "evidence": "ORDER_SERVICE_URL",
            "name": "order-service",
            "details": {"url": "http://order-service:8080"},
        }
    ],
    "gaps": [],
}


class FactDerivationTests(unittest.TestCase):
    def test_facts_are_candidates_with_evidence_and_stable_ids(self):
        facts = scan_facts.facts_from_scan(SCAN_REPORT, project_id="demo")
        self.assertEqual(len(facts), 3)
        for fact in facts:
            self.assertTrue(fact["candidate_only"])
            self.assertEqual(fact["status"], "candidate")
            self.assertEqual(fact["semantic_decision_owner"], "ai_skill")
            self.assertEqual(fact["artifact_type"], "scan_fact")
            self.assertTrue(fact["evidence"]["file"])
        again = scan_facts.facts_from_scan(SCAN_REPORT, project_id="demo")
        self.assertEqual([f["fact_id"] for f in facts], [f["fact_id"] for f in again])

    def test_semantic_change_changes_fact_id(self):
        base = scan_facts.facts_from_scan(SCAN_REPORT, project_id="demo")
        changed = json.loads(json.dumps(SCAN_REPORT))
        changed["source"][0]["path_pattern"] = "/orders/v2"
        other = scan_facts.facts_from_scan(changed, project_id="demo")
        self.assertNotEqual(base[0]["fact_id"], other[0]["fact_id"])

    def test_scan_id_defaults_to_deterministic_value(self):
        facts = scan_facts.facts_from_scan(SCAN_REPORT, project_id="demo")
        self.assertTrue(all(fact["scan_id"].startswith("SCAN-") for fact in facts))

    def test_facts_are_never_stored_as_confirmed(self):
        facts = scan_facts.facts_from_scan(SCAN_REPORT, project_id="demo")
        forged = dict(facts[0], status="confirmed", candidate_only=False)
        with self.assertRaisesRegex(scan_facts.FactError, "candidate only"):
            scan_facts.validate_fact(forged)

    def test_missing_project_id_is_rejected(self):
        with self.assertRaisesRegex(scan_facts.FactError, "project_id"):
            scan_facts.facts_from_scan(SCAN_REPORT, project_id="")


class FactLedgerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.facts = scan_facts.facts_from_scan(SCAN_REPORT, project_id="demo")
        scan_facts.append_facts(self.root, self.facts)
        self.fact_id = self.facts[0]["fact_id"]

    def test_ingest_is_idempotent(self):
        written = scan_facts.append_facts(self.root, self.facts)
        self.assertEqual(written, [])
        self.assertEqual(len(scan_facts.load_ledger(self.root)), 3)

    def test_candidate_state_before_decision(self):
        state = scan_facts.current_facts(self.root)
        self.assertEqual(state[self.fact_id]["status"], "candidate")
        self.assertIsNone(state[self.fact_id]["decision"])

    def test_ai_confirmation_requires_evidence(self):
        with self.assertRaisesRegex(scan_facts.FactError, "evidence_refs"):
            scan_facts.record_decision(
                self.root, self.fact_id, status="confirmed", actor_type="ai", actor_id="ai:semantic", evidence_refs=[]
            )

    def test_ai_confirmation_moves_fact_out_of_candidate(self):
        scan_facts.record_decision(
            self.root,
            self.fact_id,
            status="confirmed",
            actor_type="ai",
            actor_id="ai:semantic",
            evidence_refs=["src/OrderController.java#L42"],
            rationale="route annotation matches contract",
        )
        state = scan_facts.current_facts(self.root)[self.fact_id]
        self.assertEqual(state["status"], "confirmed")
        self.assertFalse(state["candidate_only"])
        self.assertEqual(state["decision"]["decision_owner"], "ai_skill")

    def test_ai_cannot_confirm_reviewer_owned_fact(self):
        with self.assertRaisesRegex(scan_facts.FactError, "cannot be confirmed by ai"):
            scan_facts.record_decision(
                self.root,
                self.fact_id,
                status="confirmed",
                actor_type="ai",
                actor_id="ai:semantic",
                evidence_refs=["code:1"],
                decision_owner="reviewer",
            )

    def test_reviewer_owned_fact_needs_reviewer_actor(self):
        scan_facts.record_decision(
            self.root,
            self.fact_id,
            status="confirmed",
            actor_type="reviewer",
            actor_id="backend-owner",
            evidence_refs=["sig:owner-approval"],
            decision_owner="reviewer",
        )
        self.assertEqual(scan_facts.current_facts(self.root)[self.fact_id]["decision"]["actor_type"], "reviewer")

    def test_rejection_requires_reason_and_is_terminal(self):
        with self.assertRaisesRegex(scan_facts.FactError, "rationale"):
            scan_facts.record_decision(
                self.root, self.fact_id, status="rejected", actor_type="ai", actor_id="ai:semantic", evidence_refs=["x"]
            )
        scan_facts.record_decision(
            self.root,
            self.fact_id,
            status="rejected",
            actor_type="ai",
            actor_id="ai:semantic",
            evidence_refs=["x"],
            rationale="route belongs to a legacy controller",
        )
        with self.assertRaisesRegex(scan_facts.FactError, "cannot be decided again"):
            scan_facts.record_decision(
                self.root, self.fact_id, status="confirmed", actor_type="ai", actor_id="ai:semantic", evidence_refs=["y"]
            )

    def test_supersede_requires_target_fact(self):
        with self.assertRaisesRegex(scan_facts.FactError, "superseded_by"):
            scan_facts.record_decision(
                self.root, self.fact_id, status="superseded", actor_type="ai", actor_id="ai:semantic", evidence_refs=["v2"]
            )

    def test_unknown_fact_is_rejected(self):
        with self.assertRaisesRegex(scan_facts.FactError, "unknown fact_id"):
            scan_facts.record_decision(
                self.root, "FACT-unknown", status="confirmed", actor_type="ai", actor_id="ai:semantic", evidence_refs=["x"]
            )

    def test_corrupted_ledger_fails_closed(self):
        scan_facts.ledger_path(self.root).write_text("{not json}\n", encoding="utf-8")
        with self.assertRaisesRegex(scan_facts.FactError, "not valid JSON"):
            scan_facts.load_ledger(self.root)


class GovernanceRefTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.facts = scan_facts.facts_from_scan(SCAN_REPORT, project_id="demo")

    def test_candidate_refs_block_executable_profile(self):
        refs = scan_facts.governance_refs(self.facts)
        with self.assertRaisesRegex(scan_facts.FactError, "requires confirmed facts"):
            scan_facts.assert_refs_confirmed(refs)

    def test_confirmed_refs_pass_and_render(self):
        scan_facts.append_facts(self.root, self.facts)
        fact_id = self.facts[0]["fact_id"]
        scan_facts.record_decision(
            self.root,
            fact_id,
            status="confirmed",
            actor_type="ai",
            actor_id="ai:semantic",
            evidence_refs=["src/OrderController.java#L42"],
        )
        state = scan_facts.current_facts(self.root)
        refs = scan_facts.governance_refs([state[fact_id]], applies_to=["route", "request_schema"])
        scan_facts.assert_refs_confirmed(refs)
        block = scan_facts.render_governance_block(refs)
        self.assertIn(f"fact_id: {fact_id}", block)
        self.assertIn("status: confirmed", block)
        self.assertIn("applies_to: [route, request_schema]", block)


class ProfileFactsValidationTests(unittest.TestCase):
    def _profile(self, facts_block, lifecycle="executable"):
        placeholder = "sha256:" + ("0" * 64)
        contract_fingerprint = "sha256:" + hashlib.sha256(b"contract").hexdigest()
        body = (
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
            f"  document_fingerprint: {placeholder}\n"
            f"  contract_fingerprint: {contract_fingerprint}\n"
            "```\n"
        )
        normalized = asset_validator._normalize_profile(body)
        fingerprint = "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return body.replace(placeholder, fingerprint, 1)

    def test_executable_profile_rejects_candidate_fact(self):
        text = self._profile(
            "facts:\n  - fact_id: FACT-1\n    status: candidate\n    decision_owner: ai_skill\n"
        )
        with self.assertRaisesRegex(asset_validator.AssetValidationError, "must be confirmed"):
            asset_validator.validate_profile(text)

    def test_executable_profile_accepts_confirmed_fact(self):
        text = self._profile(
            "facts:\n  - fact_id: FACT-1\n    status: confirmed\n    decision_owner: ai_skill\n"
            "    confirmed_by: ai:semantic\n    applies_to: [route]\n"
        )
        result = asset_validator.validate_profile(text)
        self.assertEqual(result["fact_ids"], ["FACT-1"])

    def test_draft_profile_may_hold_candidate_fact(self):
        text = self._profile(
            "facts:\n  - fact_id: FACT-1\n    status: candidate\n    decision_owner: ai_skill\n",
            lifecycle="drafted",
        )
        self.assertEqual(asset_validator.validate_profile(text)["fact_ids"], ["FACT-1"])

    def test_duplicate_fact_ids_are_rejected(self):
        text = self._profile(
            "facts:\n  - fact_id: FACT-1\n    status: confirmed\n    confirmed_by: ai:x\n"
            "  - fact_id: FACT-1\n    status: confirmed\n    confirmed_by: ai:x\n"
        )
        with self.assertRaisesRegex(asset_validator.AssetValidationError, "duplicate fact_id"):
            asset_validator.validate_profile(text)

    def test_confirmed_fact_requires_confirmed_by(self):
        text = self._profile("facts:\n  - fact_id: FACT-1\n    status: confirmed\n")
        with self.assertRaisesRegex(asset_validator.AssetValidationError, "confirmed_by"):
            asset_validator.validate_profile(text)


if __name__ == "__main__":
    unittest.main()
