#!/usr/bin/env python3
"""Deterministic pre-execution diagnosis for the API test entry skill.

This module is local-only. It never reads credentials, touches runtime secret
bridges, changes a design, or sends HTTP requests. It turns a silent gate stop
into an auditable diagnosis with ownership, evidence, recoverability and the
next safe action.
"""
from __future__ import annotations

import copy
from typing import Any


EXECUTABLE_REVIEWS = {"approved", "partially_approved"}
REQUIRED_ASSETS = ("project", "test_config", "contract", "environment", "risk")
CONFIG_GAP_KINDS = {"config", "profile", "secret", "runtime_binding"}
CONFIG_SOURCE_ORDER = ("current_input", "canonical_config", "persistent_secret_store", "active_run")


def _issue(
    code: str,
    *,
    category: str,
    owner: str,
    severity: str,
    recoverable: bool,
    evidence: str,
    impact: str,
    next_action: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "category": category,
        "owner": owner,
        "severity": severity,
        "recoverable": recoverable,
        "evidence": evidence,
        "impact": impact,
        "next_action": next_action,
    }


def _case_ids(design: dict[str, Any]) -> list[str]:
    cases = design.get("cases") or design.get("tasks") or []
    return [
        item.get("case_id")
        for item in cases
        if isinstance(item, dict) and isinstance(item.get("case_id"), str)
    ]


def _scope(review: dict[str, Any], case_ids: list[str]) -> list[str]:
    value = review.get("approved_case_ids")
    if value is None:
        value = review.get("approved_scope")
    if value is None and review.get("status") == "approved":
        value = case_ids
    return list(dict.fromkeys(value)) if isinstance(value, list) else []


def _review_fields(review: dict[str, Any], scope: list[str]) -> list[str]:
    """Return approved cases whose reviewed-field declaration is missing.

    Keep this diagnostic aligned with review_adapter.canonical_review so a valid
    documented alias can never be accepted by the adapter and rejected here.
    """
    fields = review.get("case_reviewed_fields") or review.get("reviewed_fields_by_case")
    overall = review.get("reviewed_fields")
    if isinstance(overall, dict) and isinstance(overall.get("cases"), dict):
        fields = overall.get("cases")
        overall = overall.get("overall")
    if isinstance(fields, dict):
        return [case_id for case_id in scope if not isinstance(fields.get(case_id), list) or not fields.get(case_id)]
    if isinstance(overall, dict):
        return [case_id for case_id in scope if not isinstance(overall.get(case_id), list) or not overall.get(case_id)]
    if isinstance(overall, list) and overall:
        if all(isinstance(item, str) for item in overall):
            return []
        mapped = {
            item.get("case_id"): item.get("fields")
            for item in overall
            if isinstance(item, dict) and isinstance(item.get("case_id"), str)
        }
        return [case_id for case_id in scope if not isinstance(mapped.get(case_id), list) or not mapped.get(case_id)]
    return list(scope)


_GAP_SCOPE_KEYS = ("acceptance_refs", "requirement_refs", "case_refs")


def _gap_refs(gap: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for key in _GAP_SCOPE_KEYS:
        value = gap.get(key)
        if isinstance(value, list):
            refs.update(item for item in value if isinstance(item, str) and item)
    if isinstance(gap.get("ac_id"), str) and gap["ac_id"]:
        refs.add(gap["ac_id"])
    return refs


def _scope_refs(design: dict[str, Any], scope: list[str]) -> set[str]:
    cases = {
        item.get("case_id"): item
        for item in (design.get("cases") or design.get("tasks") or [])
        if isinstance(item, dict) and item.get("case_id")
    }
    refs: set[str] = set()
    for case_id in scope:
        case = cases.get(case_id) or {}
        for key in ("acceptance_refs", "requirement_refs"):
            value = case.get(key)
            if isinstance(value, list):
                refs.update(item for item in value if isinstance(item, str) and item)
        if isinstance(case.get("ac_id"), str) and case["ac_id"]:
            refs.add(case["ac_id"])
    return refs


def _split_blocking_gaps(design: dict[str, Any], scope: list[str]) -> tuple[list[str], list[str]]:
    """Split blocking gaps into those that stop this run and those that do not.

    A gap only blocks the work it actually covers: gaps that declare case/AC
    references block when they intersect the approved scope, and gaps that
    declare nothing are treated as global and still block. This keeps the
    documented "局部阻断" rule true instead of turning every blocking gap into a
    whole-requirement stop.
    """
    scoped = _scope_refs(design, scope)
    blocking: list[str] = []
    deferred: list[str] = []
    for gap in design.get("known_gaps") or []:
        if not isinstance(gap, dict) or gap.get("blocking") is not True:
            continue
        gap_id = str(gap.get("gap_id") or gap.get("description") or "unknown-gap")
        refs = _gap_refs(gap)
        if not refs or refs & scoped:
            blocking.append(gap_id)
        else:
            deferred.append(gap_id)
    return blocking, deferred


def _config_source_issues(design: dict[str, Any]) -> list[dict[str, Any]]:
    """Require proof that all four configured sources were checked before a gap.

    This consumes source-presence metadata only. It never reads or emits Secret
    values; persistent Store and active Run checks are performed by the trusted
    host and summarized as booleans.
    """
    issues: list[dict[str, Any]] = []
    for gap in design.get("known_gaps") or []:
        if not isinstance(gap, dict) or gap.get("blocking") is not True:
            continue
        if gap.get("kind") not in CONFIG_GAP_KINDS:
            continue
        checks = gap.get("source_checks")
        missing = [name for name in CONFIG_SOURCE_ORDER if not isinstance(checks, dict) or name not in checks]
        if missing:
            gap_id = str(gap.get("gap_id") or gap.get("description") or "unknown-gap")
            issues.append(_issue(
                "CONFIG_SOURCE_CHECK_INCOMPLETE",
                category="runtime_precondition",
                owner="skill_or_runtime_host",
                severity="high",
                recoverable=False,
                evidence=f"gap {gap_id} lacks source checks: " + ", ".join(missing),
                impact="the value may already exist, so the gap and a repeated user question are not justified",
                next_action="check current input, docs/test-config.md or docs/interface-profile/, the environment persistent Secret Store, and the active Run before asking",
            ))
            continue
        available = [name for name in CONFIG_SOURCE_ORDER if checks.get(name) is True]
        if available:
            gap_id = str(gap.get("gap_id") or gap.get("description") or "unknown-gap")
            issues.append(_issue(
                "CONFIG_VALUE_ALREADY_AVAILABLE",
                category="skill_state_sync",
                owner="skill_or_runtime_host",
                severity="high",
                recoverable=False,
                evidence=f"gap {gap_id} has an available value in: " + ", ".join(available),
                impact="asking again would duplicate an already persisted or active value",
                next_action="reuse the existing binding without re-asking; if the source cannot be loaded, report that source failure instead",
            ))
    return issues


def _execution_dependency_issues(design: dict[str, Any], scope: list[str]) -> list[dict[str, Any]]:
    cases = {
        item.get("case_id"): item
        for item in (design.get("cases") or design.get("tasks") or [])
        if isinstance(item, dict)
    }
    issues = []
    for case_id in scope:
        case = cases.get(case_id) or {}
        dependencies = case.get("execution_dependencies")
        if dependencies is None:
            issues.append(
                _issue(
                    "EXECUTION_DEPENDENCIES_MISSING",
                    category="feasibility",
                    owner="skill_or_l1",
                    severity="high",
                    recoverable=False,
                    evidence=f"case {case_id} has no execution_dependencies field",
                    impact="cannot prove that the approved case is executable",
                    next_action="declare each dependency with status resolved, not_required, or blocked",
                )
            )
            continue
        if not isinstance(dependencies, list):
            issues.append(
                _issue(
                    "EXECUTION_DEPENDENCIES_INVALID",
                    category="feasibility",
                    owner="skill_or_l1",
                    severity="high",
                    recoverable=False,
                    evidence=f"case {case_id}.execution_dependencies is not a list",
                    impact="feasibility planning cannot be trusted",
                    next_action="normalize execution_dependencies before package build",
                )
            )
            continue
        for dependency in dependencies:
            if not isinstance(dependency, dict):
                continue
            if dependency.get("status") == "blocked":
                dependency_kind = dependency.get("kind")
                if dependency_kind == "data":
                    evidence = dependency.get("evidence") or dependency.get("data_source_evidence_refs")
                    code = "DATA_SOURCE_CHECK_INCOMPLETE" if not evidence else "DATA_DEPENDENCY_BLOCKED"
                    next_action = (
                        "check data-sources.md and authorized fixture/datafactory channels; keep the candidate in the reviewed design"
                        if not evidence else
                        "keep the candidate in the reviewed design with planned_disposition=not_run; resolve the data dependency before execution"
                    )
                else:
                    code = "EXECUTION_DEPENDENCY_BLOCKED"
                    next_action = "resolve the dependency while keeping the candidate in the reviewed design; mark it not_run until resolved"
                issues.append(
                    _issue(
                        code,
                        category="runtime_precondition",
                        owner="runtime_or_l1",
                        severity="high",
                        recoverable=False,
                        evidence=f"case {case_id} declares a blocked {dependency_kind or 'execution'} dependency",
                        impact="candidate remains designed and reviewed but must not send a request",
                        next_action=next_action,
                    )
                )
    return issues


def diagnose_design(
    design: dict[str, Any],
    *,
    execution_assets: dict[str, Any] | None = None,
    feasibility_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a safe, structured diagnosis without mutating the input.

    ``recoverable`` means the skill may perform a deterministic local step such
    as deriving a feasibility snapshot. It never means that the skill may invent
    a review, credential, fixture, environment, or risk approval.
    """
    if not isinstance(design, dict):
        return {
            "schema_version": "1.0",
            "status": "blocked",
            "phase": "pre_execution",
            "issues": [_issue(
                "DESIGN_INVALID",
                category="skill_input",
                owner="skill",
                severity="high",
                recoverable=False,
                evidence="design is not a JSON object",
                impact="cannot inspect or execute the test design",
                next_action="provide a valid design object",
            )],
            "auto_actions": [],
            "next_action": "repair_design",
        }

    issues: list[dict[str, Any]] = []
    deferred_gaps: list[str] = []
    issues.extend(_config_source_issues(design))
    review = design.get("review") if isinstance(design.get("review"), dict) else {}
    status = review.get("status") or "pending_review"
    case_ids = _case_ids(design)
    scope = _scope(review, case_ids)

    if status == "pending_review":
        issues.append(_issue(
            "REVIEW_DECISION_NOT_PERSISTED",
            category="skill_state_sync",
            owner="skill_or_host",
            severity="high",
            recoverable=False,
            evidence="review.status=pending_review or no executable review was persisted",
            impact="the skill cannot prove that the current design was approved",
            next_action="persist the user's decision against the current design fingerprint; do not re-ask decisions already recorded in the conversation",
        ))
    elif status not in EXECUTABLE_REVIEWS:
        issues.append(_issue(
            "REVIEW_NOT_EXECUTABLE",
            category="review_gate",
            owner="user_or_reviewer",
            severity="high",
            recoverable=False,
            evidence=f"review.status={status}",
            impact="no case may send a request",
            next_action="create a new approved or partially_approved review if execution is still wanted",
        ))
    else:
        if not scope:
            issues.append(_issue(
                "APPROVED_SCOPE_MISSING",
                category="skill_state_sync",
                owner="skill_or_host",
                severity="high",
                recoverable=False,
                evidence="executable review has an empty approved_case_ids scope",
                impact="the runner cannot determine which cases are authorized",
                next_action="persist the approved case IDs from the decision; never infer a partial scope",
            ))
        unknown = sorted(set(scope) - set(case_ids))
        if unknown:
            issues.append(_issue(
                "APPROVED_SCOPE_UNKNOWN_CASE",
                category="review_integrity",
                owner="skill",
                severity="high",
                recoverable=False,
                evidence="approved scope references unknown case IDs: " + ", ".join(unknown),
                impact="review scope and design content do not match",
                next_action="recreate the design review against the current case list",
            ))
        missing_fields = _review_fields(review, scope)
        if missing_fields:
            issues.append(_issue(
                "REVIEWED_FIELDS_MISSING",
                category="review_integrity",
                owner="user_or_reviewer",
                severity="high",
                recoverable=False,
                evidence="approved cases without reviewed_fields: " + ", ".join(missing_fields),
                impact="the approval does not identify which behavior was reviewed",
                next_action="record reviewed fields per approved case and retain the review ID",
            ))

        issues.extend(_execution_dependency_issues(design, scope))

    declared = design.get("design_fingerprint") or design.get("source_design_fingerprint")
    reviewed = review.get("design_fingerprint") or review.get("source_design_fingerprint")
    if declared and reviewed and declared != reviewed and not any(item["code"] == "REVIEW_DESIGN_FINGERPRINT_MISMATCH" for item in issues):
        issues.append(_issue(
            "REVIEW_DESIGN_FINGERPRINT_MISMATCH",
            category="review_integrity",
            owner="skill",
            severity="high",
            recoverable=False,
            evidence="review fingerprint differs from design fingerprint",
            impact="the approval cannot be bound to the current behavior",
            next_action="invalidate the stale approval and request a new review; do not rewrite the fingerprint to force execution",
        ))
    if declared and not any(item["code"] == "DESIGN_FINGERPRINT_MISMATCH" for item in issues):
        try:
            from package_compiler import calculate_design_fingerprint
            actual = calculate_design_fingerprint(design)
        except Exception as exc:  # pragma: no cover - defensive diagnostic path
            actual = None
            issues.append(_issue(
                "DESIGN_FINGERPRINT_CHECK_ERROR",
                category="skill_integrity",
                owner="skill",
                severity="high",
                recoverable=False,
                evidence=f"fingerprint calculation failed: {type(exc).__name__}",
                impact="cannot establish review continuity",
                next_action="repair the skill dependency before execution",
            ))
        if actual and declared != actual:
            issues.append(_issue(
                "DESIGN_FINGERPRINT_MISMATCH",
                category="skill_integrity",
                owner="skill_or_design_producer",
                severity="high",
                recoverable=False,
                evidence="stored design_fingerprint differs from the current behavior-bearing design",
                impact="the design may have changed after approval; execution is unsafe",
                next_action="rebuild the design fingerprint and obtain a new review; do not silently mutate the approved design",
            ))

    if status in EXECUTABLE_REVIEWS:
        assets = execution_assets if isinstance(execution_assets, dict) else design.get("package_template")
        missing_assets = [field for field in REQUIRED_ASSETS if not isinstance((assets or {}).get(field), dict)]
        if missing_assets:
            issues.append(_issue(
                "EXECUTION_ASSETS_MISSING",
                category="execution_binding",
                owner="l1_or_runtime_host",
                severity="high",
                recoverable=False,
                evidence="missing execution asset groups: " + ", ".join(missing_assets),
                impact="the approved design cannot become a trusted TestPackage",
                next_action="bind only the current target's project, test_config, contract, environment and risk assets; do not use legacy config as a fallback",
            ))

        blocking_gaps, deferred_gaps = _split_blocking_gaps(design, scope)
        if blocking_gaps:
            issues.append(_issue(
                "DESIGN_BLOCKING_GAPS_PRESENT",
                category="execution_precondition",
                owner="l1_or_runtime_host",
                severity="high",
                recoverable=False,
                evidence="blocking gaps: " + ", ".join(blocking_gaps),
                impact="the package would claim readiness while required facts remain unresolved",
                next_action="resolve the blocking gaps or keep the affected cases not_run",
            ))

        if feasibility_plan is None and not any(item["code"] in {"EXECUTION_DEPENDENCIES_MISSING", "EXECUTION_DEPENDENCIES_INVALID", "EXECUTION_DEPENDENCY_BLOCKED"} for item in issues):
            # Safe local recovery: the plan is deterministic and contains no secrets.
            # The caller may build it; this module only reports that action.
            issues.append(_issue(
                "FEASIBILITY_PLAN_MISSING",
                category="skill_integration",
                owner="skill",
                severity="medium",
                recoverable=True,
                evidence="approved design has no feasibility plan supplied to the package compiler",
                impact="package compilation cannot proceed yet",
                next_action="auto_build_feasibility_plan before package compilation",
            ))

    blocking = [item for item in issues if not item.get("recoverable")]
    auto_actions = [item["next_action"] for item in issues if item.get("recoverable")]
    if not issues:
        next_action = "execute_preflight"
        result_status = "ready"
    elif blocking:
        next_action = blocking[0]["next_action"]
        result_status = "blocked"
    else:
        next_action = auto_actions[0]
        result_status = "recoverable"
    return {
        "schema_version": "1.0",
        "status": result_status,
        "phase": "pre_execution",
        "issues": copy.deepcopy(issues),
        "auto_actions": list(dict.fromkeys(auto_actions)),
        "deferred_gaps": list(deferred_gaps),
        "next_action": next_action,
        "execution_allowed": result_status == "ready",
    }


def format_diagnosis(diagnosis: dict[str, Any]) -> str:
    """Create a concise exception-safe message; never includes secret values."""
    if not isinstance(diagnosis, dict):
        return "execution blocked: diagnosis unavailable"
    lines = [
        f"EXECUTION_BLOCKED: {diagnosis.get('status', 'blocked')}",
        f"next_action: {diagnosis.get('next_action') or 'inspect diagnosis'}",
    ]
    for item in diagnosis.get("issues") or []:
        lines.append(
            f"[{item.get('code')}] owner={item.get('owner')} "
            f"recoverable={item.get('recoverable')} evidence={item.get('evidence')} "
            f"impact={item.get('impact')} next={item.get('next_action')}"
        )
    return "\n".join(lines)
