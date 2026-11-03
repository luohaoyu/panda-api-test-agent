#!/usr/bin/env python3
"""Compile an approved design into an executable TestPackage.

The compiler is a structural boundary, not a second review boundary. It may
normalize aliases and add execution metadata, but it must not change behavior-
bearing case fields. The review is reused only when the approved case scope and
reviewed design identity still match the source design.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from feasibility_plan import validate_feasibility_plan
from plan_fingerprint import calculate_plan_fingerprint


class PackageCompileError(ValueError):
    """Raised when an approved design cannot be safely compiled."""


APPROVED_STATUSES = {"approved", "partially_approved"}
REQUIRED_ASSETS = ("project", "test_config", "contract", "environment", "risk")
_DESIGN_METADATA_FIELDS = {
    "review",
    "design_fingerprint",
    "source_design_fingerprint",
    "package_template",
    "execution_assets",
}


def _canonical_design(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _canonical_design(item)
            for key, item in value.items()
            if key not in _DESIGN_METADATA_FIELDS
        }
    if isinstance(value, list):
        return [_canonical_design(item) for item in value]
    return value


def calculate_design_fingerprint(design: dict[str, Any]) -> str:
    """Fingerprint only behavior-bearing design fields, not review metadata."""
    payload = _canonical_design(copy.deepcopy(design))
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _case_ids(cases: list[dict[str, Any]]) -> list[str]:
    return [case["case_id"] for case in cases]


def _source_design_fingerprint(design: dict[str, Any], review: dict[str, Any]) -> str | None:
    return (
        review.get("design_fingerprint")
        or review.get("source_design_fingerprint")
        or design.get("design_fingerprint")
        or design.get("source_design_fingerprint")
    )


def _approved_scope(review: dict[str, Any], case_ids: list[str]) -> list[str]:
    scope = review.get("approved_case_ids")
    if scope is None:
        scope = review.get("approved_scope")
    if scope is None and review.get("status") == "approved":
        scope = case_ids
    if not isinstance(scope, list) or not scope or any(not isinstance(item, str) or not item for item in scope):
        raise PackageCompileError("approved review requires a non-empty approved case scope")
    scope = list(dict.fromkeys(scope))
    unknown = set(scope) - set(case_ids)
    if unknown:
        raise PackageCompileError("review contains unknown approved case IDs: " + ", ".join(sorted(unknown)))
    status = review.get("status")
    if status == "approved" and set(scope) != set(case_ids):
        raise PackageCompileError("approved review must cover every design case")
    if status == "partially_approved" and set(scope) == set(case_ids):
        raise PackageCompileError("partially_approved review must identify a subset")
    return scope


def _reviewed_fields(review: dict[str, Any], scope: list[str]) -> dict[str, list[str]]:
    fields = review.get("reviewed_fields")
    if isinstance(fields, dict):
        result = {case_id: value for case_id, value in fields.items() if isinstance(value, list)}
    elif isinstance(fields, list) and all(isinstance(item, str) for item in fields):
        result = {case_id: list(fields) for case_id in scope}
    else:
        result = {}
    missing = [case_id for case_id in scope if not result.get(case_id)]
    if missing:
        raise PackageCompileError("reviewed_fields must cover approved cases: " + ", ".join(missing))
    return result


def _resolve_template(design: dict[str, Any], execution_assets: dict[str, Any] | None) -> dict[str, Any]:
    template = design.get("package_template")
    if template is None:
        template = execution_assets
    if not isinstance(template, dict):
        raise PackageCompileError(
            "approved design requires package_template or execution_assets; "
            "the compiler will not invent config/profile/runtime bindings"
        )
    missing = [field for field in REQUIRED_ASSETS if not isinstance(template.get(field), dict)]
    if missing:
        raise PackageCompileError("execution assets missing required fields: " + ", ".join(missing))
    return copy.deepcopy(template)


def compile_approved_package(
    design: dict[str, Any],
    *,
    execution_assets: dict[str, Any] | None = None,
    package_id: str | None = None,
    feasibility_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile one already-approved design without requesting a second review.

    The returned package has a fresh compiled ``plan_fingerprint`` while keeping
    the human decision's source design identity in ``review.design_fingerprint``.
    This distinction is intentional: compilation changes representation, not the
    reviewed HTTP behavior. Any change to cases, requests, assertions,
    dependencies, side effects, environment or known gaps must happen before this
    function and therefore produces a new source design for review.
    """
    if not isinstance(design, dict):
        raise PackageCompileError("design must be an object")
    review = design.get("review")
    if not isinstance(review, dict) or review.get("status") not in APPROVED_STATUSES:
        raise PackageCompileError(
            "HUMAN_REVIEW_REQUIRED: complete human review after feasibility planning and before package build"
        )

    raw_cases = design.get("cases")
    if raw_cases is None:
        raw_cases = design.get("tasks")
    if not isinstance(raw_cases, list) or not raw_cases or any(not isinstance(case, dict) for case in raw_cases):
        raise PackageCompileError("approved design requires a non-empty cases list")
    cases = copy.deepcopy(raw_cases)
    case_ids = _case_ids(cases)
    if any(not isinstance(case_id, str) or not case_id for case_id in case_ids) or len(case_ids) != len(set(case_ids)):
        raise PackageCompileError("case_id values must be non-empty and unique")

    scope = _approved_scope(review, case_ids)
    reviewed_fields = _reviewed_fields(review, scope)
    if feasibility_plan is None:
        raise PackageCompileError(
            "FEASIBILITY_PLAN_REQUIRED: build a feasibility plan after case design and before package build"
        )
    feasibility_errors = validate_feasibility_plan(design, feasibility_plan)
    if feasibility_errors:
        raise PackageCompileError("; ".join(feasibility_errors))
    gate = feasibility_plan.get("gate") or {}
    if gate.get("status") != "passed":
        reasons = gate.get("blocking_reasons") or []
        raise PackageCompileError(
            "PACKAGE_BUILD_BLOCKED_UNRESOLVED_DEPENDENCY: "
            + ("; ".join(reasons) if reasons else "human-approved scope contains non-executable cases")
        )
    if set(feasibility_plan.get("executable_case_ids") or []) != set(scope):
        raise PackageCompileError(
            "PACKAGE_BUILD_SCOPE_MISMATCH: feasibility executable scope must match human-approved scope"
        )
    source_fingerprint = _source_design_fingerprint(design, review)
    if not source_fingerprint:
        raise PackageCompileError("approved design requires design_fingerprint for review continuity")
    declared_design_fingerprint = design.get("design_fingerprint") or design.get("source_design_fingerprint")
    reviewed_design_fingerprint = review.get("design_fingerprint") or review.get("source_design_fingerprint")
    if declared_design_fingerprint and reviewed_design_fingerprint and declared_design_fingerprint != reviewed_design_fingerprint:
        raise PackageCompileError(
            "approved review does not match the current design; a new review is required"
        )
    if declared_design_fingerprint and declared_design_fingerprint != calculate_design_fingerprint(design):
        raise PackageCompileError(
            "design_fingerprint does not match behavior-bearing design fields; a new review is required"
        )

    package = _resolve_template(design, execution_assets)
    package.update({
        "schema_version": "2.2",
        "package_id": package_id or design.get("package_id") or f"PKG-{design.get('requirement_id', 'API')}",
        "mode": str(design.get("mode", package.get("mode", "EXECUTE"))).upper(),
        "cases": cases,
    })
    for field in ("project", "test_config", "contract", "environment", "risk"):
        package[field] = copy.deepcopy(package[field])
    for field in ("requirements", "business_rules", "scenarios", "gates", "fixtures", "known_gaps", "auth_probe"):
        if field in design:
            package[field] = copy.deepcopy(design[field])
        else:
            package.setdefault(field, [] if field not in {"auth_probe"} else None)

    normalized_review = copy.deepcopy(review)
    normalized_review["status"] = review["status"]
    normalized_review["approved_scope"] = scope
    normalized_review["approved_case_ids"] = scope
    normalized_review["reviewed_fields"] = reviewed_fields
    normalized_review["design_fingerprint"] = source_fingerprint
    normalized_review["source_design_fingerprint"] = source_fingerprint
    normalized_review["review_reused"] = True
    package["review"] = normalized_review
    package["feasibility"] = copy.deepcopy(feasibility_plan)
    package["package_build"] = {
        "status": "compiled",
        "kind": "approved_feasible_design_to_test_package",
        "review_reused": True,
        "source_design_fingerprint": source_fingerprint,
        "source_feasibility_fingerprint": feasibility_plan["plan_fingerprint"],
        "semantic_review_required": False,
    }
    # Keep the former key as a read-only compatibility alias for consumers that
    # have not yet moved to the explicit PACKAGE BUILD lifecycle name.
    package["compilation"] = copy.deepcopy(package["package_build"])

    # The compiled fingerprint is a representation fingerprint. It is attached
    # after the behavior-bearing package has been assembled and does not trigger
    # a second user review.
    package["review"]["plan_fingerprint"] = calculate_plan_fingerprint(package)
    return package
