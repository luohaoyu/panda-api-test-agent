#!/usr/bin/env python3
"""Deterministic TestPackage plan fingerprinting.

Review metadata is deliberately excluded so a review can be attached after the
plan is generated. Runtime-only values must never be part of a plan package.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path


EXCLUDED_TOP_LEVEL_FIELDS = {"review"}


def canonical_plan(package):
    """Return the behavior-bearing package fields in canonical JSON form."""
    if not isinstance(package, dict):
        raise ValueError("package must be an object")
    plan = copy.deepcopy(package)
    for field in EXCLUDED_TOP_LEVEL_FIELDS:
        plan.pop(field, None)
    if isinstance(plan.get("plan"), dict):
        plan["plan"].pop("fingerprint", None)
        plan["plan"].pop("plan_fingerprint", None)
    # Runtime-bound request bodies/paths are supplied by the trusted host only
    # after human review. Exclude their values from the reviewed plan digest so
    # binding the approved in-memory curl does not invalidate the review.
    for case in plan.get("cases") or []:
        if isinstance(case, dict):
            bound = case.pop("runtime_request_bound", False)
            reviewed_request = case.pop("_reviewed_request", None)
            if bound is True:
                case["request"] = reviewed_request if isinstance(reviewed_request, dict) else {"runtime_bound": True}
    return plan


def canonical_bytes(package):
    return json.dumps(
        canonical_plan(package),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def calculate_plan_fingerprint(package):
    return "sha256:" + hashlib.sha256(canonical_bytes(package)).hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Calculate a TestPackage plan fingerprint")
    parser.add_argument("package", help="Path to package.json")
    args = parser.parse_args()
    package = json.loads(Path(args.package).read_text(encoding="utf-8"))
    print(json.dumps({"plan_fingerprint": calculate_plan_fingerprint(package)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
