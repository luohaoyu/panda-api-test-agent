#!/usr/bin/env python3
"""Build and validate the static feasibility plan around human review.

This module is deliberately local-only: it never reads runtime values and never
sends HTTP. Planning records unresolved data, credential, environment, fixture,
oracle and cleanup dependencies without stopping the workflow. Only the final
gate, evaluated against the human-approved scope, can block package build.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


FEASIBILITY_SCHEMA_VERSION = "1.1"
LEGACY_FEASIBILITY_SCHEMA_VERSION = "1.0"
GATE_ID = "GATE-FEASIBILITY"
DEPENDENCY_KINDS = {"data", "credential", "environment", "fixture", "oracle", "cleanup"}
DEPENDENCY_STATUSES = {"resolved", "not_required", "blocked"}
COVERAGE_STATUSES = {"covered", "partial", "blocked", "uncovered"}


class FeasibilityPlanError(ValueError):
    """Raised when candidate cases cannot form a valid feasibility plan."""


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _canonical(item)
            for key, item in value.items()
            if key not in {"plan_fingerprint", "generated_at"}
        }
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def calculate_feasibility_fingerprint(plan: dict[str, Any]) -> str:
    payload = json.dumps(
        _canonical(copy.deepcopy(plan)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _design_fingerprint(design: dict[str, Any]) -> str:
    declared = design.get("design_fingerprint") or design.get("source_design_fingerprint")
    if not isinstance(declared, str) or not declared.startswith("sha256:"):
        raise FeasibilityPlanError("candidate design requires design_fingerprint")
    return declared


def _candidate_cases(design: dict[str, Any]) -> list[dict[str, Any]]:
    cases = design.get("cases")
    if cases is None:
        cases = design.get("tasks")
    if not isinstance(cases, list) or not cases or any(not isinstance(case, dict) for case in cases):
        raise FeasibilityPlanError("candidate design requires a non-empty cases list")
    case_ids = [case.get("case_id") for case in cases]
    if any(not isinstance(case_id, str) or not case_id for case_id in case_ids) or len(case_ids) != len(set(case_ids)):
        raise FeasibilityPlanError("candidate case_id values must be non-empty and unique")
    return cases


def _review_binding(design: dict[str, Any], case_ids: list[str]) -> tuple[str, list[str], list[str], str | None]:
    """Return review status, reviewed design scope and this-round execution scope."""
    review = design.get("review") or {}
    status = review.get("status") or "pending_review"
    if status not in {"approved", "partially_approved"}:
        return status, [], [], review.get("review_id")
    reviewed_scope = review.get("reviewed_case_ids")
    if reviewed_scope is None:
        reviewed_scope = review.get("approved_case_ids")
    if reviewed_scope is None:
        reviewed_scope = review.get("approved_scope")
    if reviewed_scope is None and status == "approved":
        reviewed_scope = case_ids
    if not isinstance(reviewed_scope, list) or not reviewed_scope:
        raise FeasibilityPlanError("approved review requires a non-empty reviewed case scope")
    if any(not isinstance(item, str) or not item for item in reviewed_scope):
        raise FeasibilityPlanError("reviewed scope must contain non-empty case IDs")
    reviewed_scope = list(dict.fromkeys(reviewed_scope))
    unknown = sorted(set(reviewed_scope) - set(case_ids))
    if unknown:
        raise FeasibilityPlanError("reviewed scope contains unknown candidate cases: " + ", ".join(unknown))
    if status == "approved" and set(reviewed_scope) != set(case_ids):
        raise FeasibilityPlanError("approved review must cover every candidate case")
    execution_scope = review.get("execution_case_ids")
    if execution_scope is None:
        execution_scope = reviewed_scope
    if not isinstance(execution_scope, list) or any(not isinstance(item, str) or not item for item in execution_scope):
        raise FeasibilityPlanError("execution_case_ids must be a list of non-empty case IDs")
    execution_scope = list(dict.fromkeys(execution_scope))
    unknown_execution = sorted(set(execution_scope) - set(reviewed_scope))
    if unknown_execution:
        raise FeasibilityPlanError("execution scope contains cases outside reviewed scope: " + ", ".join(unknown_execution))
    return status, reviewed_scope, execution_scope, review.get("review_id")


def _normalize_dependencies(case: dict[str, Any], approved: bool) -> tuple[list[dict[str, Any]], list[str]]:
    dependencies = case.get("execution_dependencies")
    reasons: list[str] = []
    if dependencies is None:
        if approved:
            reasons.append(f"case {case.get('case_id')} must explicitly declare execution_dependencies (use [] when none)")
        return [], reasons
    if not isinstance(dependencies, list):
        return [], [f"case {case.get('case_id')} execution_dependencies must be a list"]
    normalized = []
    seen = set()
    for item in dependencies:
        if not isinstance(item, dict):
            reasons.append(f"case {case.get('case_id')} has a non-object execution dependency")
            continue
        dependency_id = item.get("dependency_id")
        kind = item.get("kind")
        status = item.get("status")
        description = item.get("description")
        if not isinstance(dependency_id, str) or not dependency_id:
            reasons.append(f"case {case.get('case_id')} dependency requires dependency_id")
            continue
        if dependency_id in seen:
            reasons.append(f"case {case.get('case_id')} has duplicate dependency_id: {dependency_id}")
            continue
        seen.add(dependency_id)
        if kind not in DEPENDENCY_KINDS:
            reasons.append(f"case {case.get('case_id')} dependency {dependency_id} has unsupported kind: {kind}")
        if status not in DEPENDENCY_STATUSES:
            reasons.append(f"case {case.get('case_id')} dependency {dependency_id} has unsupported status: {status}")
        if not isinstance(description, str) or not description.strip():
            reasons.append(f"case {case.get('case_id')} dependency {dependency_id} requires description")
        if kind == "data" and status == "blocked":
            evidence = item.get("evidence") or item.get("data_source_evidence_refs") or item.get("checked_sources")
            if not evidence:
                reasons.append(f"case {case.get('case_id')} data dependency {dependency_id} requires data-source evidence")
        normalized.append(copy.deepcopy(item))
        if approved and status == "blocked":
            reasons.append(f"case {case.get('case_id')} dependency is unresolved: {dependency_id}")
    return normalized, reasons


def _dependency_graph(cases: list[dict[str, Any]]) -> tuple[list[str], list[list[str]]]:
    case_ids = {case.get("case_id") for case in cases}
    unknown: list[str] = []
    graph: dict[str, set[str]] = {}
    for case in cases:
        case_id = case["case_id"]
        depends_on = set(case.get("depends_on") or [])
        unknown.extend(f"{case_id}->{item}" for item in sorted(depends_on - case_ids))
        graph[case_id] = depends_on & case_ids
    pending = set(graph)
    resolved: set[str] = set()
    while pending:
        ready = {case_id for case_id in pending if graph[case_id].issubset(resolved)}
        if not ready:
            return unknown, [sorted(pending)]
        pending -= ready
        resolved |= ready
    return unknown, []


def build_feasibility_plan(design: dict[str, Any], *, plan_id: str | None = None) -> dict[str, Any]:
    """Derive a planning snapshot and evaluate its final gate only when reviewed."""
    if not isinstance(design, dict):
        raise FeasibilityPlanError("design must be an object")
    cases = _candidate_cases(design)
    case_ids = [case["case_id"] for case in cases]
    review_status, reviewed_scope, execution_scope, review_id = _review_binding(design, case_ids)
    approved = set(reviewed_scope)
    execution_requested = set(execution_scope)
    reviewed = review_status in {"approved", "partially_approved"}
    design_fingerprint = _design_fingerprint(design)

    matrix = []
    structural_reasons: list[str] = []
    approval_blocking_reasons: list[str] = []
    ready_case_ids = []
    blocked_case_ids = []
    for case in cases:
        case_id = case["case_id"]
        applicability = case.get("applicability") or {}
        candidate_only = applicability.get("candidate_only") is True or case.get("candidate_only") is True
        dependencies, reasons = _normalize_dependencies(case, approved=False)
        unresolved = [
            item.get("dependency_id") for item in dependencies
            if item.get("status") == "blocked"
        ]
        if candidate_only:
            reasons.append(f"case {case_id} is candidate_only and cannot enter package build")
        if case.get("planned_disposition") == "not_run":
            reasons.append(f"case {case_id} is explicitly planned not_run")
        for dependency_id in unresolved:
            reasons.append(f"case {case_id} dependency is unresolved: {dependency_id}")
        reasons = list(dict.fromkeys(reasons))
        case_status = "blocked" if reasons else "resolved"
        if case_status == "resolved":
            ready_case_ids.append(case_id)
        else:
            blocked_case_ids.append(case_id)
        if reviewed and case_id in execution_requested:
            # A blocked runtime/data dependency changes this round's execution
            # disposition, not the completeness of the reviewed design. Only
            # structural candidate errors fail the review gate.
            approval_blocking_reasons.extend(
                reason for reason in reasons
                if "dependency is unresolved:" not in reason
            )
        matrix.append({
            "case_id": case_id,
            "approved": case_id in approved,
            "execution_requested": case_id in execution_requested,
            "candidate_only": candidate_only,
            "depends_on": list(case.get("depends_on") or []),
            "execution_dependencies": dependencies,
            "status": case_status,
            "planned_disposition": "not_run" if case_status == "blocked" else "execute",
            "blocking_reasons": reasons,
        })

    unknown_dependencies, cycles = _dependency_graph(cases)
    if unknown_dependencies:
        structural_reasons.append("unknown case dependencies: " + ", ".join(unknown_dependencies))
    if cycles:
        structural_reasons.append("case dependency graph contains a cycle: " + ", ".join(cycles[0]))

    execution_case_ids = [case_id for case_id in execution_scope if case_id in ready_case_ids]
    executable_case_ids = list(execution_case_ids)
    blocked_reviewed_case_ids = [case_id for case_id in reviewed_scope if case_id in blocked_case_ids]
    out_of_scope_case_ids = [case_id for case_id in case_ids if reviewed and case_id not in approved]

    coverage_entries = []
    requirements = design.get("requirements") or []
    for requirement in requirements:
        requirement_id = requirement.get("requirement_id") if isinstance(requirement, dict) else None
        candidate_refs = [
            case["case_id"] for case in cases
            if requirement_id in (case.get("requirement_refs") or [])
        ]
        executable_refs = [case_id for case_id in candidate_refs if case_id in executable_case_ids]
        required = requirement.get("required", True) if isinstance(requirement, dict) else True
        if executable_refs:
            status = "covered" if set(candidate_refs).issubset(set(executable_refs)) else "partial"
        elif candidate_refs:
            status = "blocked"
        else:
            status = "uncovered"
        coverage_entries.append({
            "requirement_id": requirement_id,
            "required": required,
            "candidate_case_ids": candidate_refs,
            "executable_case_ids": executable_refs,
            "status": status,
        })

    rule_entries = []
    for rule in design.get("business_rules") or []:
        rule_id = rule.get("rule_id") if isinstance(rule, dict) else None
        candidate_refs = [case["case_id"] for case in cases if rule_id in (case.get("business_rule_refs") or [])]
        executable_refs = [case_id for case_id in candidate_refs if case_id in executable_case_ids]
        required = rule.get("required", True) if isinstance(rule, dict) else True
        status = "covered" if executable_refs else "blocked" if candidate_refs else "uncovered"
        rule_entries.append({
            "rule_id": rule_id,
            "required": required,
            "candidate_case_ids": candidate_refs,
            "executable_case_ids": executable_refs,
            "status": status,
        })

    approval_blocking_reasons = list(dict.fromkeys(approval_blocking_reasons))
    structural_reasons = list(dict.fromkeys(structural_reasons))
    if structural_reasons:
        planning_status = "invalid"
        gate_status = "failed"
        gate_reasons = structural_reasons
    elif not reviewed:
        planning_status = "ready"
        gate_status = "pending_review"
        gate_reasons = []
    elif approval_blocking_reasons:
        planning_status = "finalized"
        gate_status = "failed"
        gate_reasons = approval_blocking_reasons
    else:
        planning_status = "finalized"
        gate_status = "passed"
        gate_reasons = []

    plan = {
        "schema_version": FEASIBILITY_SCHEMA_VERSION,
        "plan_id": plan_id or f"FEAS-{design.get('requirement_id', 'API')}",
        "requirement_id": design.get("requirement_id"),
        "design_fingerprint": design_fingerprint,
        "planning_status": planning_status,
        "review_binding": {
            "status": review_status,
            "review_id": review_id,
            "approved_case_ids": reviewed_scope,
            "reviewed_case_ids": reviewed_scope,
            "execution_case_ids": execution_scope,
        },
        "candidate_case_ids": case_ids,
        "approved_case_ids": reviewed_scope,
        "reviewed_case_ids": reviewed_scope,
        "execution_requested_case_ids": execution_scope,
        "ready_case_ids": ready_case_ids,
        "execution_case_ids": execution_case_ids,
        "executable_case_ids": executable_case_ids,
        "blocked_reviewed_case_ids": blocked_reviewed_case_ids,
        "blocked_case_ids": blocked_case_ids,
        "out_of_scope_case_ids": out_of_scope_case_ids,
        "coverage_map": {
            "requirements": coverage_entries,
            "business_rules": rule_entries,
            "full_requirement_coverage": reviewed and all(
                item["status"] == "covered" for item in coverage_entries if item["required"]
            ) and all(item["status"] == "covered" for item in rule_entries if item["required"]),
        },
        "dependency_matrix": {
            "cases": matrix,
            "unknown_dependencies": unknown_dependencies,
            "cycles": cycles,
        },
        "gate": {
            "gate_id": GATE_ID,
            "status": gate_status,
            "evaluated_against_review": reviewed,
            "blocking_reasons": gate_reasons,
        },
    }
    plan["plan_fingerprint"] = calculate_feasibility_fingerprint(plan)
    return plan


def validate_feasibility_plan(design: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    """Validate a plan against the exact candidate design and approved scope."""
    if not isinstance(plan, dict):
        return ["FEASIBILITY_PLAN_REQUIRED: feasibility plan must be an object"]
    try:
        expected = build_feasibility_plan(design, plan_id=plan.get("plan_id"))
    except FeasibilityPlanError as exc:
        return [str(exc)]
    errors = []
    if plan.get("schema_version") != FEASIBILITY_SCHEMA_VERSION:
        errors.append(f"feasibility schema_version must be {FEASIBILITY_SCHEMA_VERSION}")
    declared_fingerprint = plan.get("plan_fingerprint")
    actual_fingerprint = calculate_feasibility_fingerprint(plan)
    if declared_fingerprint != actual_fingerprint:
        errors.append("FEASIBILITY_PLAN_FINGERPRINT_MISMATCH: plan content changed after planning")
    for field in (
        "design_fingerprint", "planning_status", "review_binding", "candidate_case_ids",
        "approved_case_ids", "reviewed_case_ids", "ready_case_ids", "execution_case_ids", "executable_case_ids", "blocked_reviewed_case_ids", "blocked_case_ids",
        "out_of_scope_case_ids", "coverage_map", "dependency_matrix", "gate",
    ):
        if plan.get(field) != expected.get(field):
            errors.append(f"feasibility.{field} does not match the current candidate design")
    return errors


def validate_package_feasibility(package: dict[str, Any]) -> list[str]:
    """Validate the immutable feasibility snapshot embedded in TestPackage 2.2 / 2.3."""
    if package.get("schema_version") == "2.1":
        return []
    if package.get("schema_version") not in {"2.2", "2.3"}:
        return ["package schema_version must be 2.1 (legacy), 2.2, or 2.3"]
    feasibility = package.get("feasibility")
    build = package.get("package_build")
    if not isinstance(feasibility, dict):
        return ["FEASIBILITY_PLAN_REQUIRED: TestPackage 2.2/2.3 requires feasibility"]
    if not isinstance(build, dict) or build.get("status") != "compiled":
        return ["PACKAGE_BUILD_REQUIRED: TestPackage 2.2/2.3 requires package_build.status=compiled"]
    errors = []
    if feasibility.get("schema_version") not in {FEASIBILITY_SCHEMA_VERSION, LEGACY_FEASIBILITY_SCHEMA_VERSION}:
        errors.append(
            f"feasibility schema_version must be {LEGACY_FEASIBILITY_SCHEMA_VERSION} or {FEASIBILITY_SCHEMA_VERSION}"
        )
    declared = feasibility.get("plan_fingerprint")
    if declared != calculate_feasibility_fingerprint(feasibility):
        errors.append("FEASIBILITY_PLAN_FINGERPRINT_MISMATCH: embedded feasibility snapshot changed")
    gate = feasibility.get("gate") or {}
    if gate.get("gate_id") != GATE_ID or gate.get("status") != "passed":
        errors.append("FEASIBILITY_GATE_FAILED: embedded feasibility gate is not passed")
    review_scope = set((package.get("review") or {}).get("approved_scope") or [])
    reviewed = set(feasibility.get("reviewed_case_ids") or [])
    execution = set(feasibility.get("execution_case_ids") or feasibility.get("executable_case_ids") or [])
    if review_scope != reviewed:
        errors.append("feasibility reviewed_case_ids must exactly match review.approved_scope")
    case_ids = {case.get("case_id") for case in package.get("cases") or [] if isinstance(case, dict)}
    if not execution.issubset(case_ids):
        errors.append("feasibility references execution cases absent from package")
    if not execution.issubset(review_scope):
        errors.append("feasibility execution_case_ids must be a subset of review.approved_scope")
    if build.get("source_feasibility_fingerprint") != declared:
        errors.append("package_build source_feasibility_fingerprint mismatch")
    if build.get("source_design_fingerprint") != feasibility.get("design_fingerprint"):
        errors.append("package_build source_design_fingerprint mismatch")
    return errors
