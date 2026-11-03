import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "tests"))

import feasibility_plan  # noqa: E402
import package_compiler  # noqa: E402
import runner  # noqa: E402
import test_end_to_end_stub as stub_module  # noqa: E402
from trusted_host import execute_test_package  # noqa: E402


CONTRACT_FINGERPRINT = "sha256:" + hashlib.sha256(b"contract-v1").hexdigest()
PLACEHOLDER = "sha256:" + ("0" * 64)


def build_profile(store: Path, profile_id: str, *, lifecycle: str = "executable", facts_block: str = "") -> dict:
    """Write one governed interface profile and return its declared identity."""
    raw = (
        "# profile\n\n```yaml interface-profile-governance\n"
        "schema_version: \"1.0\"\n"
        f"profile_id: {profile_id}\n"
        "profile_revision: 1\n"
        "module_id: demo\n"
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
    target_dir = store / profile_id.replace(":", "_")
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "profile.md"
    path.write_text(raw.replace(PLACEHOLDER, fingerprint, 1), encoding="utf-8")
    return {
        "profile_path": str(path),
        "governance_schema_version": "1.0",
        "profile_id": profile_id,
        "profile_revision": 1,
        "code_baseline": "abc123",
        "lifecycle": lifecycle,
        "document_fingerprint": fingerprint,
        "contract_fingerprint": CONTRACT_FINGERPRINT,
    }


class MultiContractTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.trigger = build_profile(self.root, "demo:route")
        self.log_oracle = build_profile(self.root, "demo:log")
        self.track_oracle = build_profile(self.root, "demo:track")

    def _package(self, contracts=None, contract=None):
        package = {"package_id": "PKG-demo"}
        if contract is not None:
            package["contract"] = contract
        if contracts is not None:
            package["contracts"] = contracts
        return package

    def test_legacy_single_contract_is_read_as_one_trigger(self):
        bindings = runner.validate_contract_bindings(self._package(contract=dict(self.trigger)))
        self.assertEqual(1, len(bindings))
        self.assertEqual("trigger", bindings[0]["role"])

    def test_trigger_and_oracles_are_each_validated(self):
        contracts = [
            {**self.trigger, "role": "trigger"},
            {**self.log_oracle, "role": "oracle", "interface_ref": "scanOperationLog.pageList"},
            {**self.track_oracle, "role": "negative_oracle", "interface_ref": "track.public.list"},
        ]
        bindings = runner.validate_contract_bindings(self._package(contracts=contracts))
        self.assertEqual(3, len(bindings))
        self.assertEqual(
            ["trigger", "oracle", "negative_oracle"], [item["role"] for item in bindings]
        )
        self.assertEqual("demo:route", runner.primary_contract_binding(bindings)["profile_id"])

    def test_oracle_requires_interface_ref(self):
        contracts = [
            {**self.trigger, "role": "trigger"},
            {**self.log_oracle, "role": "oracle"},
        ]
        with self.assertRaisesRegex(runner.RunnerError, "requires interface_ref"):
            runner.validate_contract_bindings(self._package(contracts=contracts))

    def test_exactly_one_trigger_is_required(self):
        contracts = [
            {**self.trigger, "role": "trigger"},
            {**self.log_oracle, "role": "trigger"},
        ]
        with self.assertRaisesRegex(runner.RunnerError, "exactly one trigger"):
            runner.validate_contract_bindings(self._package(contracts=contracts))

    def test_oracle_lifecycle_below_execution_blocks_the_package(self):
        drafted = build_profile(self.root, "demo:drafted", lifecycle="drafted")
        contracts = [
            {**self.trigger, "role": "trigger"},
            {**drafted, "role": "oracle", "interface_ref": "log.list"},
        ]
        with self.assertRaisesRegex(runner.RunnerError, "lifecycle"):
            runner.validate_contract_bindings(self._package(contracts=contracts))

    def test_oracle_profile_drift_blocks_the_package(self):
        drifted = dict(self.log_oracle)
        drifted["profile_revision"] = 2
        contracts = [
            {**self.trigger, "role": "trigger"},
            {**drifted, "role": "oracle", "interface_ref": "log.list"},
        ]
        with self.assertRaisesRegex(runner.RunnerError, "profile_revision mismatch"):
            runner.validate_contract_bindings(self._package(contracts=contracts))

    def test_duplicate_profile_role_is_rejected(self):
        contracts = [
            {**self.trigger, "role": "trigger"},
            {**self.log_oracle, "role": "oracle", "interface_ref": "log.list"},
            {**self.log_oracle, "role": "oracle", "interface_ref": "log.pageList"},
        ]
        with self.assertRaisesRegex(runner.RunnerError, "duplicate contract binding"):
            runner.validate_contract_bindings(self._package(contracts=contracts))

    def test_missing_contract_and_contracts_is_rejected(self):
        with self.assertRaisesRegex(runner.RunnerError, "requires contract or contracts"):
            runner.validate_contract_bindings(self._package())

    def test_package_assets_accept_contracts_without_single_contract(self):
        template = {
            "project": {},
            "test_config": {},
            "environment": {},
            "risk": {},
            "contracts": [
                {**self.trigger, "role": "trigger"},
                {**self.log_oracle, "role": "oracle", "interface_ref": "log.list"},
            ],
        }
        resolved = package_compiler._resolve_template({}, template)
        self.assertEqual(2, len(resolved["contracts"]))


class MultiContractExecutionTests(unittest.TestCase):
    """Real HTTP round-trip through the trusted host with two governed contracts."""

    def _package_23(self, pkg, trigger, oracle):
        ids = [case["case_id"] for case in pkg["cases"]]
        design_fp = "sha256:" + hashlib.sha256(b"multi-contract-design").hexdigest()
        feasibility = {
            "schema_version": feasibility_plan.FEASIBILITY_SCHEMA_VERSION,
            "plan_id": "FEAS-MULTI",
            "design_fingerprint": design_fp,
            "candidate_case_ids": ids,
            "approved_case_ids": ids,
            "reviewed_case_ids": ids,
            "execution_case_ids": ids,
            "executable_case_ids": ids,
            "blocked_case_ids": [],
            "coverage_map": {},
            "dependency_matrix": {},
            "gate": {"gate_id": feasibility_plan.GATE_ID, "status": "passed", "blocking_reasons": []},
        }
        feasibility["plan_fingerprint"] = feasibility_plan.calculate_feasibility_fingerprint(feasibility)
        pkg.update({
            "schema_version": "2.3",
            "contract": dict(trigger),
            "contracts": [
                {**trigger, "role": "trigger"},
                {**oracle, "role": "oracle", "interface_ref": "scanOperationLog.pageList"},
            ],
            "feasibility": feasibility,
            "package_build": {
                "status": "compiled",
                "source_design_fingerprint": design_fp,
                "source_feasibility_fingerprint": feasibility["plan_fingerprint"],
            },
        })
        pkg["review"]["approved_scope"] = ids
        pkg["review"]["plan_fingerprint"] = runner.calculate_plan_fingerprint(pkg)
        return pkg

    def _execute(self, drift):
        with tempfile.TemporaryDirectory() as temp_dir, stub_module._StubEnvironment() as stub:
            pkg = stub_module.EndToEndContractTests()._package(temp_dir, stub.base_url)
            trigger = build_profile(Path(temp_dir), "stub-workspace:stub-module")
            oracle = build_profile(Path(temp_dir), "stub-workspace:stub-log")
            if drift:
                oracle["profile_revision"] = 9
            pkg = self._package_23(pkg, trigger, oracle)
            package_path = Path(temp_dir) / ".panda-api-test-agent" / "runs" / "RUN-MULTI" / "package.json"
            package_path.parent.mkdir(parents=True, exist_ok=True)
            package_path.write_text(json.dumps(pkg, ensure_ascii=False), encoding="utf-8")
            run_dir = Path(temp_dir) / "run"
            outcome = execute_test_package(
                package_path,
                run_dir,
                Path(temp_dir) / "report.html",
                runtime_values={
                    "STUB_BASE_URL": stub.base_url,
                    "STUB_ALLOWED_ORIGINS": stub.base_url,
                    "STUB_READ_ONLY_OPERATIONS": stub_module.READ_ONLY,
                    "STUB_BEARER_TOKEN": stub_module.TOKEN,
                },
                context_id="ctx-multi-contract",
            )
            result_path = run_dir / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
            return outcome, result, len(stub.requests)

    def test_multi_contract_package_runs_and_records_every_role(self):
        outcome, result, requests = self._execute(drift=False)
        self.assertEqual("completed", outcome["status"], outcome)
        self.assertEqual(3, requests)
        self.assertEqual(["trigger", "oracle"], [item["role"] for item in result["contracts"]])
        self.assertEqual("scanOperationLog.pageList", result["contracts"][1]["interface_ref"])
        self.assertEqual(result["contracts"][0]["profile_id"], result["contract"]["profile_id"])

    def test_oracle_profile_drift_blocks_before_any_request(self):
        outcome, result, requests = self._execute(drift=True)
        self.assertNotEqual("completed", outcome["status"])
        self.assertEqual(0, requests)
        self.assertIsNone(result)
        self.assertIn("profile_revision mismatch", json.dumps(outcome, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
