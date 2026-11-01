#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
from pathlib import Path

from runner import RunnerError, load_interface_profile_identity


def execute(command):
    completed = subprocess.run(command)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main():
    parser = argparse.ArgumentParser(description="Promote an existing verified API TestResult to pytest collection")
    parser.add_argument("result")
    parser.add_argument("generated_tests")
    args = parser.parse_args()

    result_path = Path(args.result).resolve()
    generated_tests = Path(args.generated_tests).resolve()
    script_dir = Path(__file__).resolve().parent
    execute([sys.executable, str(script_dir / "validate_result.py"), str(result_path)])
    data = json.loads(result_path.read_text(encoding="utf-8"))
    cases = data.get("case_results") or []
    gates = data.get("gate_results") or []
    cleanup = data.get("cleanup") or {}
    contract = data.get("contract") or {}
    test_config = data.get("test_config") or {}
    known_gaps = data.get("known_gaps") or []
    try:
        current_profile = load_interface_profile_identity(contract.get("profile_path"))
    except (RunnerError, TypeError) as exc:
        raise SystemExit(f"PROMOTE blocked: interface profile governance is invalid: {exc}") from exc
    profile_matches = all(
        str(current_profile.get(field)).lower() == str(contract.get(field)).lower()
        for field in (
            "governance_schema_version", "profile_id", "profile_revision", "code_baseline",
            "lifecycle", "document_fingerprint", "contract_fingerprint",
        )
    )
    valid = (
        data.get("mode") == "EXECUTE"
        and data.get("verdict") == "passed"
        and bool(cases)
        and all(case.get("status") == "passed" for case in cases)
        and not any(gate.get("status") in {"failed", "error"} for gate in gates)
        and cleanup.get("status") in {"passed", "not_required"}
        and not cleanup.get("pending_objects")
        and cleanup.get("status") != "waived"
        and not cleanup.get("waived_cases")
        and contract.get("lifecycle") == "executable"
        and current_profile.get("lifecycle") == "executable"
        and profile_matches
        and bool(contract.get("profile_path"))
        and bool(contract.get("document_fingerprint"))
        and bool(contract.get("contract_fingerprint"))
        and test_config.get("schema_version") == "2.1"
        and isinstance(test_config.get("config_revision"), int)
        and test_config.get("config_revision") > 0
        and bool(test_config.get("fingerprint"))
        and not any(gap.get("blocking", True) for gap in known_gaps if isinstance(gap, dict))
    )
    if not valid:
        raise SystemExit("PROMOTE blocked: result lacks passed EXECUTE evidence, stable contract, cleanup, or has blocking gaps")
    if not generated_tests.is_dir():
        raise SystemExit(f"generated pytest directory not found: {generated_tests}")
    execute([sys.executable, "-m", "compileall", "-q", str(generated_tests)])
    execute([sys.executable, "-m", "pytest", "--collect-only", "-q", str(generated_tests)])
    print(json.dumps({"promoted_from": str(result_path), "collected": str(generated_tests)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
