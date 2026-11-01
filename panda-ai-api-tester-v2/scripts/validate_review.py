#!/usr/bin/env python3
"""Validate the pre-execution design review bound to a TestPackage."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from plan_fingerprint import calculate_plan_fingerprint


APPROVED_STATUSES = {"approved", "partially_approved"}
REVIEW_STATUSES = {"pending_review", "approved", "partially_approved", "rejected", "plan_only", "expired", "superseded"}


def parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _reviewed_field_map(value):
    """Normalize supported reviewed_fields representations to case -> fields."""
    if isinstance(value, dict):
        result = {}
        for case_id, fields in value.items():
            if isinstance(case_id, str) and isinstance(fields, list):
                result[case_id] = fields
        return result
    if isinstance(value, list):
        result = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            case_id = item.get("case_id")
            fields = item.get("fields")
            if isinstance(case_id, str) and isinstance(fields, list):
                result[case_id] = fields
        return result
    return {}


def validate_review(package, require_execute=True, now=None):
    errors = []
    review = package.get("review")
    if not isinstance(review, dict):
        return ["TEST_DESIGN_REVIEW_REQUIRED: package.review is required"] if require_execute else []

    status = review.get("status")
    if status not in REVIEW_STATUSES:
        errors.append(f"invalid review.status: {status}")
    if not require_execute:
        return errors

    case_ids = {case.get("case_id") for case in package.get("cases") or [] if isinstance(case, dict)}
    scope = review.get("approved_scope")
    if not isinstance(scope, list) or not scope:
        errors.append("review.approved_scope must list approved case IDs")
        scope_set = set()
    else:
        scope_set = set(scope)
        if len(scope_set) != len(scope):
            errors.append("review.approved_scope must not contain duplicate case IDs")
        unknown = scope_set - case_ids
        if unknown:
            errors.append("review.approved_scope contains unknown case IDs: " + ", ".join(sorted(unknown)))

    if status not in APPROVED_STATUSES:
        if status in {"expired", "superseded", "plan_only"}:
            errors.append(f"TEST_DESIGN_REVIEW_REQUIRED: review.status {status} is not executable")
        else:
            errors.append("TEST_DESIGN_REVIEW_REQUIRED: review.status must be approved or partially_approved")
    elif status == "approved" and scope_set != case_ids:
        errors.append("approved review must cover every case in the EXECUTE package")
    elif status == "partially_approved" and scope_set == case_ids:
        errors.append("partially_approved review must identify a subset of case IDs")

    for field in ("review_id", "plan_fingerprint", "reviewer", "approved_at"):
        if not review.get(field):
            errors.append(f"review.{field} is required for EXECUTE")

    actual = calculate_plan_fingerprint(package)
    declared = review.get("plan_fingerprint")
    if declared != actual:
        errors.append("TEST_PLAN_FINGERPRINT_MISMATCH: review.plan_fingerprint does not match current package; review is invalidated")

    approved_at = parse_time(review.get("approved_at"))
    if not approved_at:
        errors.append("review.approved_at must be an ISO8601 datetime with timezone")
    expires_at = review.get("expires_at")
    if expires_at:
        expires = parse_time(expires_at)
        if not expires:
            errors.append("review.expires_at must be an ISO8601 datetime with timezone")
        elif expires <= (now or datetime.now(timezone.utc)):
            errors.append("review is expired; review is invalidated")

    reviewed = _reviewed_field_map(review.get("reviewed_fields"))
    if not isinstance(review.get("reviewed_fields"), (dict, list)):
        errors.append("review.reviewed_fields must map each approved case ID to reviewed field names")
    for case_id in sorted(scope_set):
        fields = reviewed.get(case_id)
        if not isinstance(fields, list) or not fields or any(not isinstance(field, str) or not field.strip() for field in fields):
            errors.append(f"review.reviewed_fields must cover approved case {case_id}")

    accepted = review.get("accepted_gaps", [])
    known = {gap.get("gap_id") for gap in package.get("known_gaps") or [] if isinstance(gap, dict)}
    if not isinstance(accepted, list) or not set(accepted).issubset(known):
        errors.append("review.accepted_gaps must reference known_gaps")
    blocking = {gap.get("gap_id") for gap in package.get("known_gaps") or [] if isinstance(gap, dict) and gap.get("blocking") is True}
    accepted_blocking = blocking.intersection(accepted if isinstance(accepted, list) else [])
    if accepted_blocking:
        errors.append("blocking known_gaps cannot be accepted for EXECUTE: " + ", ".join(sorted(accepted_blocking)))
    if status == "partially_approved":
        nonblocking = {gap.get("gap_id") for gap in package.get("known_gaps") or [] if isinstance(gap, dict) and gap.get("blocking") is False}
        invalid = set(accepted if isinstance(accepted, list) else []) - nonblocking
        if invalid:
            errors.append("partially_approved may only accept non-blocking known_gaps")
    return errors


def approved_case_ids(package):
    review = package.get("review") or {}
    if review.get("status") == "approved":
        return {case.get("case_id") for case in package.get("cases") or [] if isinstance(case, dict)}
    if review.get("status") == "partially_approved":
        return set(review.get("approved_scope") or [])
    return set()


def main():
    parser = argparse.ArgumentParser(description="Validate TestPackage design review")
    parser.add_argument("package", help="Path to package.json")
    parser.add_argument("--probe", action="store_true", help="Validate shape without requiring EXECUTE approval")
    args = parser.parse_args()
    package = json.loads(Path(args.package).read_text(encoding="utf-8"))
    errors = validate_review(package, require_execute=not args.probe)
    print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
