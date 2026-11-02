#!/usr/bin/env python3
"""Adapt the main Agent review contract to the L2 review schema.

The main Agent contract is defined once in ``references/interaction-rules.md``
§1.5 and mirrored in ``references/orchestration-rules.md`` §4.1. Both the
documented field names (``reviewed_at``, ``design_fingerprint``,
``reviewed_fields`` as a field-name array) and the executor-side names
(``approved_at``, ``plan_fingerprint``, per-case field map) are accepted here.
Canonicalization happens in exactly one place so a documented review can never
be rejected purely because of an alias.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any


_L2_SCRIPTS = Path(__file__).resolve().parents[2] / "panda-ai-api-tester-v2" / "scripts"


def _l2_validate_review():
    """Import the L2 validator lazily so this module stays import-side-effect free."""
    if not _L2_SCRIPTS.is_dir():
        return None
    if str(_L2_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_L2_SCRIPTS))
    try:
        from validate_review import validate_review  # noqa: PLC0415 - optional dependency
    except Exception:
        return None
    return validate_review


SUPPORTED_STATUSES = {
    "pending_review",
    "approved",
    "partially_approved",
    "rejected",
    "plan_only",
    "expired",
    "superseded",
}
_TERMINAL_NON_EXECUTABLE = {"rejected", "plan_only", "expired", "superseded"}
_EXECUTABLE = {"approved", "partially_approved"}
_REVIEWED_AT_ALIASES = ("approved_at", "reviewed_at", "decided_at")
_FINGERPRINT_ALIASES = ("plan_fingerprint", "design_fingerprint", "source_design_fingerprint")


def _case_ids(package: dict[str, Any] | None) -> list[str]:
    if not isinstance(package, dict):
        return []
    return [
        item["case_id"]
        for item in package.get("cases") or []
        if isinstance(item, dict) and isinstance(item.get("case_id"), str)
    ]


def _approved_scope(review: dict[str, Any], package: dict[str, Any] | None) -> list[str]:
    value = review.get("approved_case_ids")
    if value is None:
        value = review.get("approved_scope")
    if value is None and review.get("status") == "approved":
        return _case_ids(package)
    cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []
    return list(dict.fromkeys(cleaned))


def _first_present(review: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, str | None]:
    for key in keys:
        value = review.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()
    return None, None


def _reviewed_map(review: dict[str, Any], scope: list[str]) -> dict[str, list[str]]:
    value = review.get("case_reviewed_fields") or review.get("reviewed_fields_by_case")
    overall = review.get("reviewed_fields")
    if isinstance(overall, dict) and isinstance(overall.get("cases"), dict):
        value = overall.get("cases")
        overall = overall.get("overall")
    if isinstance(value, dict):
        result = {
            case_id: list(fields)
            for case_id, fields in value.items()
            if isinstance(case_id, str) and isinstance(fields, list)
        }
        if result:
            return result
    if isinstance(overall, dict):
        result = {
            case_id: list(fields)
            for case_id, fields in overall.items()
            if isinstance(case_id, str) and isinstance(fields, list)
        }
        if result:
            return result
    if isinstance(overall, list):
        if all(isinstance(field, str) for field in overall):
            return {case_id: list(overall) for case_id in scope}
        result = {}
        for item in overall:
            if isinstance(item, dict) and isinstance(item.get("case_id"), str) and isinstance(item.get("fields"), list):
                result[item["case_id"]] = list(item["fields"])
        return result
    return {}


def _error(status: str, code: str, message: str, *, review: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "pending": status == "pending_review",
        "execution_allowed": False,
        "errors": [{"code": code, "message": message}],
        "review": review,
    }


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def canonical_review(
    review: dict[str, Any],
    case_ids: list[str] | None = None,
    *,
    require_executable: bool = False,
) -> dict[str, Any]:
    """Normalize an accepted review envelope into the single canonical shape.

    Accepted aliases (single source of truth: references/interaction-rules.md §1.5):

    - ``reviewed_at`` | ``approved_at`` | ``decided_at``
    - ``plan_fingerprint`` | ``design_fingerprint`` | ``source_design_fingerprint``
    - ``approved_case_ids`` | ``approved_scope``
    - ``reviewed_fields``: per-case map, ``{"cases": {...}}`` wrapper,
      ``[{"case_id","fields"}]`` list, or a flat field-name array that applies to
      every approved case

    Validation here is limited to review integrity; whether the declared
    fingerprint matches a compiled plan stays the caller's check so the same
    function serves both the design stage and the compiled-run stage.
    """
    if not isinstance(review, dict):
        return _error("pending_review", "REVIEW_REQUIRED", "review must be an object", review={})

    status = review.get("status") or "pending_review"
    if status not in SUPPORTED_STATUSES:
        return _error(status, "REVIEW_STATUS_INVALID", f"unsupported review status: {status}", review=copy.deepcopy(review))

    known = [item for item in (case_ids or []) if isinstance(item, str) and item]
    scope = _approved_scope(review, None)
    if not scope and status == "approved":
        scope = list(known)
    fields = _reviewed_map(review, scope)
    at_key, approved_at = _first_present(review, _REVIEWED_AT_ALIASES)
    fp_key, fingerprint = _first_present(review, _FINGERPRINT_ALIASES)

    canonical = {key: copy.deepcopy(value) for key, value in review.items() if key not in {"design_fingerprint", "source_design_fingerprint"}}
    canonical["status"] = status
    canonical["approved_case_ids"] = list(scope)
    canonical["approved_scope"] = list(scope)
    canonical["approved_at"] = approved_at
    canonical["reviewed_at"] = review.get("reviewed_at") or approved_at
    canonical["approval_field_source"] = at_key
    canonical["plan_fingerprint"] = fingerprint
    canonical["design_fingerprint"] = review.get("design_fingerprint") or fingerprint
    canonical["fingerprint_source"] = fp_key
    canonical["reviewed_fields"] = fields

    errors: list[dict[str, str]] = []
    executable = status in _EXECUTABLE
    requires_review = executable or require_executable

    if requires_review:
        if not scope:
            errors.append(_issue("APPROVED_SCOPE_MISSING", "executable review must list approved case IDs"))
        if known:
            unknown = sorted(set(scope) - set(known))
            if unknown:
                errors.append(_issue("APPROVED_SCOPE_UNKNOWN_CASE", "approved scope references unknown case IDs: " + ", ".join(unknown)))
            if status == "approved" and set(scope) != set(known):
                errors.append(_issue("APPROVED_SCOPE_INCOMPLETE", "approved review must cover every executable case"))
            if status == "partially_approved" and set(scope) == set(known):
                errors.append(_issue("APPROVED_SCOPE_NOT_PARTIAL", "partially_approved review must identify a strict subset"))
        for field, code in (("review_id", "REVIEW_ID_REQUIRED"), ("reviewer", "REVIEWER_REQUIRED")):
            if not canonical.get(field):
                errors.append(_issue(code, f"review.{field} is required for an executable review"))
        if not approved_at:
            errors.append(_issue("REVIEW_TIME_REQUIRED", "review needs reviewed_at, approved_at or decided_at (ISO8601 with timezone)"))
        if not fingerprint:
            errors.append(_issue("FINGERPRINT_REQUIRED", "review needs plan_fingerprint or design_fingerprint"))
        missing_fields = [case_id for case_id in scope if not fields.get(case_id)]
        if missing_fields:
            errors.append(_issue("REVIEWED_FIELDS_MISSING", "approved cases without reviewed fields: " + ", ".join(missing_fields)))

    return {
        "ok": not errors and (executable or not require_executable),
        "status": status,
        "pending": status == "pending_review",
        "execution_allowed": executable and not errors,
        "errors": errors,
        "review": canonical,
    }


def adapt_review(
    review: dict[str, Any],
    package: dict[str, Any] | None = None,
    *,
    now=None,
) -> dict[str, Any]:
    """Return an L2-shaped review plus a structured gate result.

    The canonical mapping lives in :func:`canonical_review`; when a package is
    supplied the L2 validator remains the source of detailed execution rules.
    """
    canonical = canonical_review(review, _case_ids(package))
    status = canonical["status"]
    l2_review = canonical["review"]
    if status == "pending_review":
        return _error(status, "TEST_DESIGN_REVIEW_REQUIRED", "review is pending human approval", review=l2_review)
    if status in _TERMINAL_NON_EXECUTABLE:
        return _error(status, "TEST_DESIGN_REVIEW_NOT_EXECUTABLE", f"review status {status} cannot execute", review=l2_review)

    result = {
        "ok": canonical["ok"],
        "status": status,
        "pending": False,
        "execution_allowed": canonical["execution_allowed"],
        "errors": list(canonical["errors"]),
        "review": l2_review,
    }
    if package is not None:
        validator = _l2_validate_review()
        if validator is None:
            result["ok"] = False
            result["execution_allowed"] = False
            result["errors"] = [*result["errors"], _issue("L2_VALIDATOR_MISSING", "panda-ai-api-tester-v2 validate_review.py is not installed next to this skill")]
            return result
        errors = validator({**package, "review": l2_review}, require_execute=True, now=now)
        if errors:
            result["ok"] = False
            result["execution_allowed"] = False
            result["errors"] = [*result["errors"], *[_issue("L2_REVIEW_INVALID", item) for item in errors]]
    return result


def to_l2_review(review: dict[str, Any], package: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return only the normalized review object for callers building a package."""
    return adapt_review(review, package).get("review", {})


def normalize_review(review: dict[str, Any], package: dict[str, Any] | None = None, *, now=None) -> dict[str, Any]:
    """Compatibility alias for the adapter result."""
    return adapt_review(review, package, now=now)
