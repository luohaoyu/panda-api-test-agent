import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from runner import GateExecutor, RunnerError, validate_probe_case


class ProbeSafetyTests(unittest.TestCase):
    def test_probe_rejects_write_method(self):
        with self.assertRaises(RunnerError):
            validate_probe_case("PROBE", "CASE-1", "POST", "read")

    def test_probe_rejects_write_side_effect_even_if_get(self):
        with self.assertRaises(RunnerError):
            validate_probe_case("PROBE", "CASE-2", "GET", "write")

    def test_probe_accepts_read_only_method_and_effect(self):
        validate_probe_case("PROBE", "CASE-3", "GET", "read")

    def test_non_blocking_diagnostic_gate_does_not_block_cases(self):
        self.assertFalse(GateExecutor.is_blocking({"gate_id": "GATE-HEALTH", "blocking": False}))
        self.assertTrue(GateExecutor.is_blocking({"gate_id": "GATE-AUTH"}))


if __name__ == "__main__":
    unittest.main()
