import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from entry_state import EntryStateError, create_entry_state, next_action_for_state, transition, validate_state


class UnifiedEntryStateTests(unittest.TestCase):
    def test_all_supported_entries_start_at_review(self):
        for entry_type in ("requirement_validation", "single_api_quality", "api_debug", "environment_probe"):
            state = create_entry_state(run_id="RUN-1", entry_type=entry_type, case_ids=["C1"])
            self.assertEqual(state["phase"], "pending_review")
            self.assertEqual(next_action_for_state(state), "request_plan_review")
            self.assertFalse(state["approved_case_ids"])

    def test_approved_scope_waits_for_execution_authorization(self):
        state = create_entry_state(
            run_id="RUN-2",
            entry_type="single_api_quality",
            case_ids=["C1", "C2"],
            review={
                "status": "partially_approved",
                "approved_case_ids": ["C1"],
                "execution_case_ids": ["C1"],
            },
        )
        self.assertEqual(state["phase"], "pending_review")
        self.assertEqual(next_action_for_state(state), "request_execution_approval")

    def test_terminal_review_never_schedules_execution(self):
        for status in ("rejected", "plan_only", "expired", "superseded"):
            state = create_entry_state(run_id="RUN-3", entry_type="api_debug", case_ids=["C1"], review={"status": status})
            self.assertEqual(next_action_for_state(state), "review_not_executable")

    def test_fact_gathering_uses_evidence_next_action(self):
        state = create_entry_state(run_id="RUN-E1", entry_type="requirement_validation")
        state["decision_state"] = "FACT_GATHERING"
        state["evidence_closure"] = {"next_action": "inspect_available_assets"}
        self.assertEqual(validate_state(state), [])
        self.assertEqual(next_action_for_state(state), "inspect_available_assets")

    def test_blocked_user_requires_stop_proof(self):
        state = create_entry_state(run_id="RUN-E2", entry_type="requirement_validation")
        state["decision_state"] = "BLOCKED_USER"
        state["evidence_closure"] = {"next_action": "request_minimum_user_input"}
        self.assertIn("BLOCKED_USER requires evidence_closure.stop_proof", validate_state(state))
        state["evidence_closure"]["stop_proof"] = {"missing_fact_ids": ["FACT-1"]}
        self.assertEqual(validate_state(state), [])
        self.assertEqual(next_action_for_state(state), "request_minimum_user_input")

    def test_illegal_transition_is_rejected(self):
        state = create_entry_state(run_id="RUN-4", entry_type="api_debug")
        with self.assertRaises(EntryStateError):
            transition(state, "executing")


if __name__ == "__main__":
    unittest.main()
