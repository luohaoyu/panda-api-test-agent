#!/usr/bin/env python3
"""Adapt the main Agent review contract to the L2 review schema."""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any


_L2_SCRIPTS = Path(__file__).resolve().parents[2] / "panda-ai-api-tester-v2" / "scripts"
if str(_L2_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_L2_SCRIPTS))

from validate_review import validate_review  # noqa: E402


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
    return list(value) if isinstance(value, list) else []


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


def adapt_review(
    review: dict[str, Any],
    package: dict[str, Any] | None = None,
    *,
    now=None,
) -> dict[str, Any]:
    """Return an L2-shaped review plus a structured gate result.

    This function only maps review metadata. When a package is supplied, the
    existing L2 validator remains the source of detailed execution rules.
    """
    if not isinstance(review, dict):
        return _error("pending_review", "REVIEW_REQUIRED", "review must be an object", review={})
    status = review.get("status") or "pending_review"
    if status not in SUPPORTED_STATUSES:
        return _error(status, "REVIEW_STATUS_INVALID", "unsupported review status", review=copy.deepcopy(review))
    scope = _approved_scope(review, package)
    fields = _reviewed_map(review, scope)
    fingerprint = review.get("plan_fingerprint") or review.get("design_fingerprint")
    l2_review = {
        key: copy.deepcopy(value)
        for key, value in review.items()
        if key not in {"design_fingerprint", "approved_case_ids"}
    }
    l2_review["status"] = status
    l2_review["plan_fingerprint"] = fingerprint
    l2_review["approved_scope"] = scope
    l2_review["reviewed_fields"] = fields
    result = {
        "ok": status in {"approved", "partially_approved"},
        "status": status,
        "pending": status == "pending_review",
        "execution_allowed": status in {"approved", "partially_approved"},
        "errors": [],
        "review": l2_review,
    }
    if status == "pending_review":
        return _error(status, "TEST_DESIGN_REVIEW_REQUIRED", "review is pending human approval", review=l2_review)
    if status in _TERMINAL_NON_EXECUTABLE:
        return _error(status, "TEST_DESIGN_REVIEW_NOT_EXECUTABLE", f"review status {status} cannot execute", review=l2_review)
    if package is not None:
        errors = validate_review({**package, "review": l2_review}, require_execute=True, now=now)
        if errors:
            result["ok"] = False
            result["execution_allowed"] = False
            result["errors"] = [{"code": "L2_REVIEW_INVALID", "message": item} for item in errors]
    return result


def to_l2_review(review: dict[str, Any], package: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return only the normalized review object for callers building a package."""
    return adapt_review(review, package).get("review", {})


def normalize_review(review: dict[str, Any], package: dict[str, Any] | None = None, *, now=None) -> dict[str, Any]:
    """Compatibility alias for the adapter result."""
    return adapt_review(review, package, now=now)
