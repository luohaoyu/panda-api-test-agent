#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from plan_fingerprint import calculate_plan_fingerprint  # noqa: E402


CASE_STATUSES = {"passed", "failed", "error", "skipped", "blocked", "inconclusive", "passed_with_gaps", "not_api_verifiable", "not_run"}
REQUEST_DELIVERY = {"not_sent", "confirmed", "unknown"}
GATE_STATUSES = {"passed", "failed", "error"}
CLEANUP_STATUSES = {"none", "passed", "failed", "not_run", "not_required", "waived"}
VERDICTS = {"passed", "failed", "error", "blocked", "partial", "inconclusive"}
TRACE_STATUSES = CASE_STATUSES | GATE_STATUSES | CLEANUP_STATUSES | {"completed", "inconclusive", "uncovered"}


def assertion_passes(operator, expected, actual):
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "exists":
        return actual is not None and actual is not False
    if operator == "all_present":
        return isinstance(expected, list) and isinstance(actual, list) and set(expected).issubset(set(actual))
    if operator == "contains":
        if isinstance(actual, (list, str)):
            return expected in actual
        return False
    if operator == "in_range":
        return (
            isinstance(expected, dict)
            and isinstance(actual, (int, float))
            and expected.get("min") is not None
            and expected.get("max") is not None
            and expected["min"] <= actual <= expected["max"]
        )
    return False


def calculate_summary(case_results):
    summary = {
        "total": len(case_results),
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "not_run": 0,
        "executed": 0,
    }
    key_map = {"error": "errors"}
    for case in case_results:
        status = case.get("status")
        key = key_map.get(status, status)
        if key in summary:
            summary[key] += 1
    summary["executed"] = summary["passed"] + summary["failed"] + summary["errors"]
    return summary


def combined_status(statuses):
    if not statuses:
        return "uncovered"
    if "error" in statuses:
        return "error"
    if "failed" in statuses:
        return "failed"
    if "inconclusive" in statuses or "not_api_verifiable" in statuses:
        return "inconclusive"
    if "not_run" in statuses or "skipped" in statuses or "blocked" in statuses:
        return "not_run"
    if all(status == "passed" for status in statuses):
        return "passed"
    if all(status in {"passed", "passed_with_gaps"} for status in statuses):
        return "passed_with_gaps"
    return "inconclusive"


def validate_traceability(data, case_results):
    errors = []
    trace = data.get("requirement_trace")
    if not isinstance(trace, dict):
        return ["requirement_trace must be an object"]
    requirements = trace.get("requirements") or []
    rules = trace.get("business_rules") or []
    scenarios = trace.get("scenarios") or []
    if not requirements or not rules or not scenarios:
        errors.append("requirement_trace requires requirements, business_rules and scenarios")
        return errors

    def index_by(items, key, label):
        mapping = {}
        for item in items:
            value = item.get(key) if isinstance(item, dict) else None
            if not value or value in mapping:
                errors.append(f"requirement_trace has invalid or duplicate {label}: {value}")
            else:
                mapping[value] = item
        return mapping

    requirement_map = index_by(requirements, "requirement_id", "requirement_id")
    rule_map = index_by(rules, "rule_id", "rule_id")
    scenario_map = index_by(scenarios, "scenario_id", "scenario_id")
    case_map = {case.get("case_id"): case for case in case_results}

    for case_id, case in case_map.items():
        for ref in case.get("requirement_refs") or []:
            if ref not in requirement_map:
                errors.append(f"case {case_id} references unknown requirement {ref}")
        for ref in case.get("business_rule_refs") or []:
            if ref not in rule_map:
                errors.append(f"case {case_id} references unknown business rule {ref}")
        for ref in case.get("scenario_refs") or []:
            if ref not in scenario_map:
                errors.append(f"case {case_id} references unknown scenario {ref}")

    for rule_id, rule in rule_map.items():
        linked = [case for case in case_results if rule_id in (case.get("business_rule_refs") or [])]
        expected_status = combined_status([case.get("status") for case in linked])
        assertion_refs = []
        evidence_refs = set()
        for case in linked:
            evidence_refs.update(case.get("evidence_refs") or [])
            for assertion in case.get("assertions") or []:
                if rule_id in (assertion.get("business_rule_refs") or []):
                    assertion_refs.append({"case_id": case.get("case_id"), "assertion_id": assertion.get("assertion_id"), "status": assertion.get("status")})
        if expected_status == "passed" and (not assertion_refs or any(item.get("status") != "passed" for item in assertion_refs)):
            expected_status = "inconclusive"
        if rule.get("status") != expected_status:
            errors.append(f"business rule {rule_id} status mismatch: {rule.get('status')} != {expected_status}")
        if sorted(rule.get("case_refs") or []) != sorted(case.get("case_id") for case in linked):
            errors.append(f"business rule {rule_id} case_refs mismatch")
        if sorted(rule.get("assertion_refs") or [], key=lambda item: (item.get("case_id", ""), item.get("assertion_id", ""))) != sorted(assertion_refs, key=lambda item: (item.get("case_id", ""), item.get("assertion_id", ""))):
            errors.append(f"business rule {rule_id} assertion_refs mismatch")
        if sorted(rule.get("evidence_refs") or []) != sorted(evidence_refs):
            errors.append(f"business rule {rule_id} evidence_refs mismatch")

    for scenario_id, scenario in scenario_map.items():
        refs = scenario.get("case_refs") or []
        unknown = [ref for ref in refs if ref not in case_map]
        if unknown:
            errors.append(f"scenario {scenario_id} references unknown cases: {unknown}")
            continue
        expected_status = combined_status([case_map[ref].get("status") for ref in refs])
        if scenario.get("status") != expected_status:
            errors.append(f"scenario {scenario_id} status mismatch: {scenario.get('status')} != {expected_status}")
        evidence_refs = sorted({ref for case_id in refs for ref in case_map[case_id].get("evidence_refs") or []})
        if sorted(scenario.get("evidence_refs") or []) != evidence_refs:
            errors.append(f"scenario {scenario_id} evidence_refs mismatch")

    for requirement_id, requirement in requirement_map.items():
        linked_rules = [rule for rule in rules if rule.get("requirement_id") == requirement_id]
        expected_status = combined_status([rule.get("status") for rule in linked_rules if rule.get("required", True)])
        if requirement.get("status") != expected_status:
            errors.append(f"requirement {requirement_id} status mismatch: {requirement.get('status')} != {expected_status}")
        evidence_refs = sorted({ref for rule in linked_rules for ref in rule.get("evidence_refs") or []})
        if sorted(requirement.get("evidence_refs") or []) != evidence_refs:
            errors.append(f"requirement {requirement_id} evidence_refs mismatch")

    expected_summary = {
        "requirements_total": len(requirements),
        "requirements_passed": sum(1 for item in requirements if item.get("status") == "passed"),
        "rules_total": len(rules),
        "rules_passed": sum(1 for item in rules if item.get("status") == "passed"),
        "required_complete": bool([item for item in requirements if item.get("required", True)]) and all(item.get("status") == "passed" for item in requirements if item.get("required", True)),
    }
    if trace.get("summary") != expected_summary:
        errors.append(f"requirement_trace summary mismatch: declared={trace.get('summary')}, calculated={expected_summary}")
    return errors


def validate_execution_trace(data, gate_results, case_results, cleanup):
    errors = []
    trace = data.get("execution_trace")
    if not isinstance(trace, dict) or trace.get("trace_version") != "1.0" or trace.get("status") != "completed":
        return ["execution_trace must be a completed trace_version 1.0 object"]
    steps = trace.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["execution_trace requires non-empty steps"]
    sequences = [item.get("sequence") for item in steps]
    if sequences != list(range(1, len(steps) + 1)):
        errors.append("execution_trace sequences must be contiguous and ordered")
    gate_map = {item.get("gate_id"): item for item in gate_results}
    case_map = {item.get("case_id"): item for item in case_results}
    cleanup_map = {item.get("case_id"): item for item in cleanup.get("actions") or []}
    waiver_map = {item.get("case_id"): item for item in cleanup.get("waived_cases") or []}
    seen_refs = {"gate": set(), "case": set(), "teardown": set(), "cleanup_waiver": set()}
    for step in steps:
        kind = step.get("kind")
        status = step.get("status")
        if status not in TRACE_STATUSES and kind != "verdict":
            errors.append(f"execution step {step.get('step_id')} has invalid status {status}")
        ref = step.get("ref")
        source = gate_map.get(ref) if kind == "gate" else case_map.get(ref) if kind == "case" else cleanup_map.get(ref) if kind == "teardown" else waiver_map.get(ref) if kind == "cleanup_waiver" else None
        if kind in seen_refs:
            if not source:
                errors.append(f"execution step {step.get('step_id')} references unknown {kind} {ref}")
                continue
            seen_refs[kind].add(ref)
            if status != source.get("status"):
                errors.append(f"execution step {step.get('step_id')} status mismatch")
            source_refs = source.get("evidence_refs") or ([source.get("evidence_ref")] if source.get("evidence_ref") else [])
            if sorted(step.get("evidence_refs") or []) != sorted(source_refs):
                errors.append(f"execution step {step.get('step_id')} evidence_refs mismatch")
    if seen_refs["gate"] != set(gate_map):
        errors.append("execution_trace does not cover every gate")
    if seen_refs["case"] != set(case_map):
        errors.append("execution_trace does not cover every case")
    if seen_refs["teardown"] != set(cleanup_map):
        errors.append("execution_trace does not cover every teardown action")
    if seen_refs["cleanup_waiver"] != set(waiver_map):
        errors.append("execution_trace does not cover every cleanup waiver")

    for case_id, case in case_map.items():
        case_trace = case.get("execution_trace")
        case_steps = case_trace.get("steps") if isinstance(case_trace, dict) else None
        if not isinstance(case_steps, list) or not case_steps or case_trace.get("status") != case.get("status"):
            errors.append(f"case {case_id} execution_trace missing or status mismatch")
            continue
        case_sequences = [item.get("sequence") for item in case_steps]
        if case_sequences != list(range(1, len(case_steps) + 1)):
            errors.append(f"case {case_id} execution_trace sequences must be contiguous")
        http_steps = [item for item in case_steps if item.get("kind") == "http_exchange"]
        if case.get("request_sent") is True:
            if len(http_steps) != 1 or sorted(http_steps[0].get("evidence_refs") or []) != sorted(case.get("evidence_refs") or []):
                errors.append(f"case {case_id} request trace must reference case evidence")
        elif http_steps:
            errors.append(f"case {case_id} has HTTP trace although request_sent is false")
        waiver_steps = [item for item in case_steps if item.get("kind") == "cleanup_waiver"]
        if case.get("cleanup_disposition") == "waived":
            if case.get("status") not in {"passed", "failed"} or case.get("request_sent") is not True:
                errors.append(f"case {case_id} cleanup waiver requires an executed passed/failed case")
            if len(waiver_steps) != 1 or not case.get("cleanup_waiver_id") or waiver_steps[0].get("waiver_id") != case.get("cleanup_waiver_id"):
                errors.append(f"case {case_id} cleanup waiver trace is missing or inconsistent")
        elif waiver_steps:
            errors.append(f"case {case_id} has unexpected cleanup waiver trace")
        assertion_steps = {item.get("assertion_id"): item for item in case_steps if item.get("kind") == "assertion"}
        for assertion in case.get("assertions") or []:
            assertion_id = assertion.get("assertion_id")
            if not assertion_id or assertion_id not in assertion_steps:
                errors.append(f"case {case_id} assertion {assertion_id} missing execution step")
            elif assertion_steps[assertion_id].get("status") != assertion.get("status"):
                errors.append(f"case {case_id} assertion {assertion_id} trace status mismatch")
    return errors


def validate_result(data, result_dir=None):
    errors = []
    required = ["schema_version", "run_id", "mode", "project", "contract", "test_config", "environment", "gate_results", "case_results", "requirement_trace", "execution_trace", "cleanup", "verdict"]
    for field in required:
        if field not in data:
            errors.append(f"missing required field: {field}")

    if data.get("schema_version") != "1.1":
        errors.append("schema_version must be 1.1")
    if data.get("mode") not in {"PROBE", "EXECUTE"}:
        errors.append("mode must be PROBE or EXECUTE")
    plan = data.get("plan") or {}
    if data.get("mode") == "EXECUTE":
        if not plan.get("plan_fingerprint"):
            errors.append("EXECUTE result requires plan.plan_fingerprint")
        review = data.get("review") or {}
        if review.get("status") not in {"approved", "partially_approved"}:
            errors.append("EXECUTE result requires an approved or partially_approved design review")
        elif review.get("plan_fingerprint") != plan.get("plan_fingerprint"):
            errors.append("review.plan_fingerprint must match plan.plan_fingerprint")
        approved_scope = set(review.get("approved_scope") or [])
        result_case_ids = {case.get("case_id") for case in data.get("case_results") or []}
        unknown_scope = approved_scope - result_case_ids
        if unknown_scope:
            errors.append("review.approved_scope contains result-unknown case IDs: " + ", ".join(sorted(unknown_scope)))
        if review.get("status") == "approved" and approved_scope != result_case_ids:
            errors.append("approved review scope must cover every result case")
        if review.get("status") == "partially_approved":
            if not approved_scope:
                errors.append("partially_approved review requires approved_scope")
            for case in data.get("case_results") or []:
                if case.get("case_id") not in approved_scope and (case.get("status") != "not_run" or case.get("request_sent") is not False):
                    errors.append(f"unapproved case {case.get('case_id')} must be not_run with request_sent=false")
        reviewed_fields = review.get("reviewed_fields")
        if not isinstance(reviewed_fields, (dict, list)):
            errors.append("EXECUTE result requires reviewed_fields")
    test_config = data.get("test_config") or {}
    if test_config.get("schema_version") != "2.1" or not isinstance(test_config.get("config_revision"), int) or test_config.get("config_revision", 0) < 1:
        errors.append("test_config requires schema_version=2.1 and positive config_revision")
    if test_config.get("environment_id") != (data.get("environment") or {}).get("environment_id"):
        errors.append("test_config.environment_id must match environment.environment_id")
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", str(test_config.get("fingerprint", ""))):
        errors.append("test_config.fingerprint must be sha256:<64 hex>")

    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        errors.append(f"invalid verdict: {verdict}")
    contract = data.get("contract") or {}
    required_contract = (
        "profile_path", "governance_schema_version", "profile_id", "profile_revision",
        "code_baseline", "lifecycle", "document_fingerprint", "contract_fingerprint",
    )
    missing_contract = [field for field in required_contract if contract.get(field) in (None, "")]
    if missing_contract:
        errors.append("contract missing required interface profile identity fields: " + ", ".join(missing_contract))
    if contract.get("governance_schema_version") != "1.0":
        errors.append("contract governance_schema_version must be 1.0")
    if not isinstance(contract.get("profile_revision"), int) or contract.get("profile_revision", 0) < 1:
        errors.append("contract profile_revision must be a positive integer")
    execution_profile = data.get("execution_profile")
    if execution_profile != "single_api_quality" and contract.get("lifecycle") not in {"minimally_usable", "confirmed", "executable"}:
        errors.append("contract lifecycle must be minimally_usable, confirmed, or executable")
    for field in ("document_fingerprint", "contract_fingerprint"):
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", str(contract.get(field, ""))):
            errors.append(f"contract {field} must be sha256:<64 hex>")

    gate_results = data.get("gate_results", [])
    if not isinstance(gate_results, list):
        errors.append("gate_results must be a list")
        gate_results = []
    root = Path(result_dir).resolve() if result_dir is not None else None
    for index, gate in enumerate(gate_results):
        if gate.get("status") not in GATE_STATUSES:
            errors.append(f"gate_results[{index}] has invalid status")
        if "blocking" in gate and not isinstance(gate.get("blocking"), bool):
            errors.append(f"gate_results[{index}].blocking must be boolean when present")
        if gate.get("runtime") is True:
            refs = gate.get("evidence_refs") or []
            digest = gate.get("evidence_sha256")
            if len(refs) != 1 or not digest:
                errors.append(f"runtime gate {gate.get('gate_id') or index} requires one evidence ref and sha256")
                continue
            if root is None:
                continue
            evidence_path = (root / refs[0]).resolve()
            evidence_root = (root / "evidence").resolve()
            try:
                evidence_path.relative_to(evidence_root)
            except ValueError:
                errors.append(f"runtime gate evidence must be under evidence/: {refs[0]}")
                continue
            if not evidence_path.is_file():
                errors.append(f"runtime gate evidence file not found: {refs[0]}")
                continue
            if hashlib.sha256(evidence_path.read_bytes()).hexdigest() != digest:
                errors.append(f"runtime gate evidence hash mismatch: {refs[0]}")
                continue
            try:
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"runtime gate evidence invalid JSON {refs[0]}: {exc}")
                continue
            if evidence.get("gate_id") != gate.get("gate_id"):
                errors.append(f"runtime gate evidence gate_id mismatch: {refs[0]}")
                continue
            assertions = [item for item in evidence.get("assertions", []) if item.get("required", True)]
            if gate.get("status") == "error":
                if not evidence.get("error"):
                    errors.append(f"runtime error gate {gate.get('gate_id')} requires error evidence")
            else:
                calculated = bool(assertions) and all(
                    item.get("status") == "passed"
                    and assertion_passes(item.get("operator"), item.get("expected"), item.get("actual"))
                    for item in assertions
                )
                expected_status = "passed" if calculated else "failed"
                if gate.get("status") != expected_status:
                    errors.append(f"runtime gate {gate.get('gate_id')} status does not match evidence")

    case_results = data.get("case_results", [])
    if not isinstance(case_results, list):
        errors.append("case_results must be a list")
        case_results = []

    seen = set()
    for index, case in enumerate(case_results):
        case_id = case.get("case_id")
        if not case_id:
            errors.append(f"case_results[{index}] missing case_id")
        elif case_id in seen:
            errors.append(f"duplicate case_id: {case_id}")
        seen.add(case_id)

        status = case.get("status")
        if status not in CASE_STATUSES:
            errors.append(f"case {case_id or index} has invalid status: {status}")

        request_sent = case.get("request_sent")
        if not isinstance(request_sent, bool):
            errors.append(f"case {case_id or index} request_sent must be boolean")
        delivery = case.get("request_delivery", "confirmed" if request_sent else "not_sent")
        if delivery not in REQUEST_DELIVERY:
            errors.append(f"case {case_id or index} has invalid request_delivery: {delivery}")
        if request_sent is True and delivery != "confirmed":
            errors.append(f"case {case_id or index} request_sent=true requires request_delivery=confirmed")
        if request_sent is False and delivery == "confirmed":
            errors.append(f"case {case_id or index} request_sent=false cannot have confirmed request_delivery")
        if status in {"passed", "failed", "passed_with_gaps"} and request_sent is not True:
            errors.append(f"case {case_id or index} cannot be {status} when request_sent is false")
        if status == "not_run" and request_sent is True:
            errors.append(f"case {case_id or index} cannot be not_run when request_sent is true")
        if status == "not_api_verifiable" and request_sent is not True:
            errors.append(f"case {case_id or index} not_api_verifiable requires a delivered request")

        if status in {"passed", "failed", "passed_with_gaps", "not_api_verifiable"}:
            assertions = case.get("assertions")
            if not isinstance(assertions, list) or not assertions:
                errors.append(f"executed case {case_id or index} requires assertion evidence")
                assertions = []
            required_types = {"http_status", "business_code", "key_fields"}
            if data.get("execution_profile") == "single_api_quality":
                required_types = {"http_status"}
            observed_types = set()
            evidence_cache = {}
            root = Path(result_dir).resolve() if result_dir is not None else None
            for assertion_index, assertion in enumerate(assertions):
                if not isinstance(assertion, dict):
                    errors.append(f"case {case_id or index} assertion[{assertion_index}] must be an object")
                    continue
                assertion_type = assertion.get("type")
                operator = assertion.get("operator")
                expected = assertion.get("expected")
                actual = assertion.get("actual")
                if assertion.get("required", True):
                    observed_types.add(assertion_type)
                if operator not in {"equals", "not_equals", "exists", "all_present", "contains", "in_range"}:
                    errors.append(f"case {case_id or index} assertion {assertion_type} has unsupported operator: {operator}")
                calculated_pass = assertion_passes(operator, expected, actual)
                expected_assertion_status = "passed" if calculated_pass else "failed"
                if assertion.get("status") != expected_assertion_status:
                    errors.append(
                        f"case {case_id or index} assertion {assertion_type} status does not match deterministic evaluation"
                    )
                if "expected" not in assertion or "actual" not in assertion:
                    errors.append(
                        f"case {case_id or index} assertion {assertion_type or assertion_index} requires expected and actual"
                    )

                evidence_ref = assertion.get("evidence_ref")
                evidence_sha256 = assertion.get("evidence_sha256")
                if not evidence_ref or not evidence_sha256:
                    errors.append(f"case {case_id or index} assertion {assertion_type} requires evidence_ref and evidence_sha256")
                    continue
                if root is None:
                    continue
                evidence_path = (root / evidence_ref).resolve()
                evidence_root = (root / "evidence").resolve()
                try:
                    evidence_path.relative_to(evidence_root)
                except ValueError:
                    errors.append(f"case {case_id or index} evidence must be under evidence/: {evidence_ref}")
                    continue
                if not evidence_path.is_file():
                    errors.append(f"case {case_id or index} evidence file not found: {evidence_ref}")
                    continue
                digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
                if digest != evidence_sha256:
                    errors.append(f"case {case_id or index} evidence hash mismatch: {evidence_ref}")
                    continue
                if evidence_ref not in evidence_cache:
                    try:
                        evidence_cache[evidence_ref] = json.loads(evidence_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        errors.append(f"case {case_id or index} invalid evidence JSON {evidence_ref}: {exc}")
                        continue
                evidence = evidence_cache[evidence_ref]
                if evidence.get("case_id") != case_id:
                    errors.append(f"case {case_id or index} evidence case_id mismatch: {evidence_ref}")
                    continue
                evidence_assertions = evidence.get("assertions", [])
                matches = [
                    item for item in evidence_assertions
                    if item.get("type") == assertion_type
                    and item.get("operator") == operator
                    and item.get("expected") == expected
                    and item.get("actual") == actual
                ]
                if not matches:
                    errors.append(f"case {case_id or index} assertion {assertion_type} not found in evidence {evidence_ref}")
                    continue
                evidence_match = matches[0]
                if evidence_match.get("status") != expected_assertion_status:
                    errors.append(f"case {case_id or index} evidence assertion {assertion_type} status mismatch")

            assertion_ids = [item.get("assertion_id") for item in assertions if isinstance(item, dict)]
            if any(not item for item in assertion_ids) or len(assertion_ids) != len(set(assertion_ids)):
                errors.append(f"case {case_id or index} assertion_id values must be non-empty and unique")
            if status == "passed" and any(item.get("status") != "passed" for item in assertions if item.get("required", True)):
                errors.append(f"passed case {case_id or index} contains failed required assertions")
            if status == "failed" and not any(item.get("status") == "failed" for item in assertions if item.get("required", True)):
                errors.append(f"failed case {case_id or index} requires a failed required assertion")
            missing_types = sorted(required_types - observed_types)
            if status == "passed" and missing_types:
                errors.append(
                    f"passed case {case_id or index} missing required assertion types: {', '.join(missing_types)}"
                )
            evidence_refs = case.get("evidence_refs")
            if not isinstance(evidence_refs, list) or not evidence_refs:
                errors.append(f"executed case {case_id or index} requires evidence_refs")
            else:
                assertion_refs = {item.get("evidence_ref") for item in assertions if isinstance(item, dict)}
                if not assertion_refs.issubset(set(evidence_refs)):
                    errors.append(f"passed case {case_id or index} evidence_refs must include every assertion evidence_ref")

    cleanup = data.get("cleanup", {})
    cleanup_status = cleanup.get("status") if isinstance(cleanup, dict) else None
    if cleanup_status not in CLEANUP_STATUSES:
        errors.append(f"invalid cleanup status: {cleanup_status}")
    waived_cases = cleanup.get("waived_cases") or [] if isinstance(cleanup, dict) else []
    pending_objects = cleanup.get("pending_objects") or [] if isinstance(cleanup, dict) else []
    waiver_ids = set()
    for item in waived_cases:
        required_fields = ("case_id", "waiver_id", "reason", "residual_risk", "approved_by", "approved_at", "expires_at", "data_tag", "object_ref")
        if not isinstance(item, dict) or any(not item.get(field) for field in required_fields):
            errors.append("each cleanup.waived_cases entry requires complete waiver and object fields")
            continue
        if item.get("waiver_id") in waiver_ids:
            errors.append(f"duplicate cleanup waiver id: {item.get('waiver_id')}")
        waiver_ids.add(item.get("waiver_id"))
        try:
            approved = datetime.fromisoformat(str(item.get("approved_at")).replace("Z", "+00:00"))
            expires = datetime.fromisoformat(str(item.get("expires_at")).replace("Z", "+00:00"))
            if approved.tzinfo is None or expires.tzinfo is None or expires <= approved or expires <= datetime.now(timezone.utc):
                errors.append(f"cleanup waiver {item.get('waiver_id')} has invalid or expired validity")
        except ValueError:
            errors.append(f"cleanup waiver {item.get('waiver_id')} timestamps must be ISO8601 with timezone")
        if not any(pending.get("case_id") == item.get("case_id") and pending.get("risk") == "accepted" and pending.get("object_ref") == item.get("object_ref") for pending in pending_objects if isinstance(pending, dict)):
            errors.append(f"cleanup waiver {item.get('waiver_id')} must have an accepted pending object")
        case = next((case for case in case_results if case.get("case_id") == item.get("case_id")), None)
        if not case or case.get("cleanup_disposition") != "waived" or case.get("cleanup_waiver_id") != item.get("waiver_id"):
            errors.append(f"cleanup waiver {item.get('waiver_id')} does not match case result")
    if waived_cases and cleanup_status != "waived" and cleanup_status not in {"failed", "error"}:
        errors.append("cleanup with waived_cases must have status waived unless a more severe cleanup status exists")
    if cleanup_status == "waived" and not waived_cases:
        errors.append("waived cleanup requires waived_cases")
    if cleanup_status == "passed":
        if waived_cases:
            errors.append("passed cleanup cannot contain waived_cases")
        actions = cleanup.get("actions") or []
        if not actions:
            errors.append("passed cleanup requires actions")
        for action in actions:
            ref = action.get("evidence_ref")
            digest = action.get("evidence_sha256")
            if action.get("status") != "passed" or action.get("verified") is not True or not ref or not digest:
                errors.append(f"cleanup action {action.get('case_id')} requires passed verified evidence")
                continue
            if root is None:
                continue
            path = (root / ref).resolve()
            try:
                path.relative_to((root / "evidence").resolve())
            except ValueError:
                errors.append(f"cleanup evidence must be under evidence/: {ref}")
                continue
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                errors.append(f"cleanup evidence missing or hash mismatch: {ref}")
                continue
            try:
                evidence = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"cleanup evidence invalid JSON {ref}: {exc}")
                continue
            assertions = [item for item in evidence.get("verification_assertions", []) if item.get("required", True)]
            verified = bool(assertions) and all(item.get("status") == "passed" and assertion_passes(item.get("operator"), item.get("expected"), item.get("actual")) for item in assertions)
            if evidence.get("status") != "passed" or not verified:
                errors.append(f"cleanup evidence verification failed: {ref}")

    errors.extend(validate_traceability(data, case_results))
    errors.extend(validate_execution_trace(data, gate_results, case_results, cleanup if isinstance(cleanup, dict) else {}))

    calculated = calculate_summary(case_results)
    coverage = data.get("coverage")
    if not isinstance(coverage, dict):
        errors.append("coverage must be an object")
    else:
        for key in ("design", "execution_touch", "complete_scenario", "acceptance_pass", "case_execution"):
            item = coverage.get(key)
            if not isinstance(item, dict) or not isinstance(item.get("numerator"), int) or not isinstance(item.get("denominator"), int):
                errors.append(f"coverage.{key} requires integer numerator and denominator")
            elif item["numerator"] < 0 or item["denominator"] < item["numerator"]:
                errors.append(f"coverage.{key} has invalid numerator/denominator")
    declared = data.get("summary")
    if declared is not None and declared != calculated:
        errors.append(f"summary mismatch: declared={declared}, calculated={calculated}")

    gate_ids = {gate.get("gate_id") for gate in gate_results}
    required_gate_ids = {"GATE-ENV-SAFETY", "GATE-AUTH", "GATE-CLEANUP"}
    if not required_gate_ids.issubset(gate_ids):
        errors.append("result is missing required safety gates")
    executed_cases = [case for case in case_results if case.get("request_sent") is True]
    if not any(gate.get("runtime") is True for gate in gate_results) and not executed_cases:
        errors.append("result requires a runtime gate or an executed business case")
    failed_gates = [
        gate for gate in gate_results
        if gate.get("status") in {"failed", "error"}
        and gate.get("blocking", True) is not False
    ]
    executed_cases = [case for case in case_results if case.get("status") in {"passed", "failed", "error"}]
    if executed_cases and not gate_results:
        errors.append("executed business cases require recorded gate_results")
    if failed_gates:
        for case in case_results:
            scoped_failures = {
                gate.get("gate_id") for gate in failed_gates
                if not gate.get("case_scope") or case.get("case_id") in set(gate.get("case_scope") or [])
            }
            if not scoped_failures:
                continue
            if case.get("status") != "not_run":
                errors.append(
                    f"failed scoped gates require business case {case.get('case_id')} to be not_run"
                )
            blocked_by = set(case.get("blocked_by", []))
            if not (scoped_failures & blocked_by):
                errors.append(
                    f"not_run case {case.get('case_id')} must reference an affected failed gate in blocked_by"
                )

    if verdict == "passed":
        if waived_cases:
            errors.append("passed verdict cannot contain cleanup.waived_cases")
        if failed_gates:
            errors.append("passed verdict cannot contain failed or error gates")
        runtime_status = (data.get("environment") or {}).get("runtime_status") or {}
        if runtime_status.get("auth") != "verified":
            errors.append(
                "passed verdict requires authentication evidence from an auth probe or an executed approved case"
            )
        if calculated["failed"] or calculated["errors"] or calculated["skipped"] or calculated["not_run"]:
            errors.append("passed verdict requires every case to be executed and passed")
        if calculated["total"] == 0:
            errors.append("passed verdict requires at least one business case")
        if cleanup_status in {"failed", "not_run", "waived"} or waived_cases or pending_objects:
            errors.append("passed verdict cannot contain incomplete, waived, or pending cleanup")
        if ((data.get("requirement_trace") or {}).get("summary") or {}).get("required_complete") is not True:
            errors.append("passed verdict requires complete required requirement coverage")

    if waived_cases and verdict == "passed":
        errors.append("passed verdict is forbidden when cleanup.waived_cases is non-empty")
    return errors, calculated


def main():
    parser = argparse.ArgumentParser(description="Validate an AI API Tester result.json")
    parser.add_argument("result", help="Path to result.json")
    parser.add_argument("--normalize", action="store_true", help="Write calculated summary back to the file")
    args = parser.parse_args()

    path = Path(args.result)
    data = json.loads(path.read_text(encoding="utf-8"))
    errors, summary = validate_result(data, path.parent)

    if errors:
        print(json.dumps({"valid": False, "errors": errors, "summary": summary}, ensure_ascii=False, indent=2))
        return 1

    if args.normalize:
        data["summary"] = summary
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"valid": True, "summary": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
