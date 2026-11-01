#!/usr/bin/env python3
"""Validate the pre-execution design review bound to a TestPackage."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from plan_fingerprint import calculate_plan_fingerprint


APPROVED_STATUSES = {"approved", "approved_with_gaps"}
REVIEW_STATUSES = {"draft", "review_pending", "approved", "approved_with_gaps", "rejected", "plan_only", "expired", "superseded"}


def parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


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
    if status not in APPROVED_STATUSES:
        errors.append("TEST_DESIGN_REVIEW_REQUIRED: review.status must be approved or approved_with_gaps")
    for field in ("review_id", "plan_fingerprint", "reviewer", "approved_at"):
        if not review.get(field):
            errors.append(f"review.{field} is required for EXECUTE")
    actual = calculate_plan_fingerprint(package)
    declared = review.get("plan_fingerprint")
    if declared != actual:
        errors.append("TEST_PLAN_FINGERPRINT_MISMATCH: review.plan_fingerprint does not match current package")
    approved_at = parse_time(review.get("approved_at"))
    if not approved_at:
        errors.append("review.approved_at must be an ISO8601 datetime with timezone")
    expires_at = review.get("expires_at")
    if expires_at:
        expires = parse_time(expires_at)
        if not expires:
            errors.append("review.expires_at must be an ISO8601 datetime with timezone")
        elif expires <= (now or datetime.now(timezone.utc)):
            errors.append("review is expired")
    case_ids = {case.get("case_id") for case in package.get("cases") or []}
    scope = review.get("approved_scope")
    if not isinstance(scope, list) or not scope:
        errors.append("review.approved_scope must list approved case IDs")
    elif not set(scope).issubset(case_ids):
        errors.append("review.approved_scope contains unknown case IDs")
    elif set(scope) != case_ids:
        errors.append("review.approved_scope must cover every case in the EXECUTE package")
    accepted = review.get("accepted_gaps", [])
    known = {gap.get("gap_id") for gap in package.get("known_gaps") or [] if isinstance(gap, dict)}
    if not isinstance(accepted, list) or not set(accepted).issubset(known):
        errors.append("review.accepted_gaps must reference known_gaps")
    blocking = {gap.get("gap_id") for gap in package.get("known_gaps") or [] if isinstance(gap, dict) and gap.get("blocking") is True}
    if blocking - set(accepted):
        errors.append("blocking known_gaps cannot be accepted for EXECUTE")
    if status == "approved_with_gaps":
        invalid = set(accepted) - {gap.get("gap_id") for gap in package.get("known_gaps") or [] if isinstance(gap, dict) and gap.get("blocking") is False}
        if invalid:
            errors.append("approved_with_gaps may only accept non-blocking known_gaps")
    return errors


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
