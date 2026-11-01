import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from entry_state import EntryStateError, create_entry_state, next_action_for_state, transition


class UnifiedEntryStateTests(unittest.TestCase):
    def test_all_supported_entries_start_at_review(self):
        for entry_type in ("requirement_validation", "single_api_quality", "api_debug"):
            state = create_entry_state(run_id="RUN-1", entry_type=entry_type, case_ids=["C1"])
            self.assertEqual(state["phase"], "pending_review")
            self.assertEqual(next_action_for_state(state), "request_approval")
            self.assertFalse(state["approved_case_ids"])

    def test_approved_scope_enters_execution(self):
        state = create_entry_state(run_id="RUN-2", entry_type="single_api_quality", case_ids=["C1"], review={"status": "partially_approved", "approved_case_ids": ["C1"]})
        self.assertEqual(state["phase"], "executing")
        self.assertEqual(next_action_for_state(state), "execute_slice")

    def test_terminal_review_never_schedules_execution(self):
        for status in ("rejected", "plan_only", "expired", "superseded"):
            state = create_entry_state(run_id="RUN-3", entry_type="api_debug", case_ids=["C1"], review={"status": status})
            self.assertEqual(next_action_for_state(state), "review_not_executable")

    def test_illegal_transition_is_rejected(self):
        state = create_entry_state(run_id="RUN-4", entry_type="api_debug")
        with self.assertRaises(EntryStateError):
            transition(state, "executing")


if __name__ == "__main__":
    unittest.main()
