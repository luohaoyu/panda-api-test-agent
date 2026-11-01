#!/usr/bin/env python3
"""Small cross-Skill protocol boundary.

The business payload schemas remain versioned independently. This module only
normalizes aliases at boundaries and records user decisions as durable, auditable
metadata; it never grants execution permission by itself.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

PROTOCOL_VERSION = "1.0"
ARTIFACT_SCHEMAS = {
    "intent": "2.1",
    "plan": "2.0",
    "test_package": "2.1",
    "test_result": "1.1",
    "requirement_report": "1.0",
    "scan_report": "1.0",
    "user_decision": "1.0",
}
REVIEW_STATUSES = {"pending_review", "approved", "partially_approved", "rejected", "plan_only", "expired", "superseded"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def normalize_artifact_metadata(artifact: dict[str, Any], artifact_type: str, *, producer: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a copy with the common envelope metadata populated."""
    if not isinstance(artifact, dict):
        raise ValueError("artifact must be an object")
    if artifact_type not in ARTIFACT_SCHEMAS:
        raise ValueError(f"unsupported artifact_type: {artifact_type}")
    result = copy.deepcopy(artifact)
    result.setdefault("protocol_version", PROTOCOL_VERSION)
    result.setdefault("artifact_type", artifact_type)
    result.setdefault("schema_version", ARTIFACT_SCHEMAS[artifact_type])
    result.setdefault("artifact_id", result.get("intent_id") or result.get("plan_id") or result.get("package_id") or result.get("run_id") or f"{artifact_type}:{_hash(result)[:20]}")
    if producer:
        result.setdefault("producer", copy.deepcopy(producer))
    return result


def validate_compatibility(artifact: dict[str, Any], artifact_type: str) -> list[str]:
    errors = []
    if not isinstance(artifact, dict):
        return ["artifact must be an object"]
    if artifact.get("protocol_version") not in {None, PROTOCOL_VERSION}:
        errors.append(f"unsupported protocol_version: {artifact.get('protocol_version')}")
    if artifact.get("artifact_type") not in {None, artifact_type}:
        errors.append(f"artifact_type must be {artifact_type}")
    expected = ARTIFACT_SCHEMAS.get(artifact_type)
    if expected and artifact.get("schema_version") not in {None, expected}:
        errors.append(f"unsupported {artifact_type}.schema_version: {artifact.get('schema_version')}")
    return errors


def normalize_review(review: dict[str, Any], *, plan_fingerprint: str | None = None, case_ids: list[str] | None = None) -> dict[str, Any]:
    """Normalize old review aliases to the canonical review contract."""
    if not isinstance(review, dict):
        review = {}
    result = copy.deepcopy(review)
    status = result.get("status") or "pending_review"
    if status not in REVIEW_STATUSES:
        raise ValueError(f"unsupported review status: {status}")
    scope = result.get("approved_case_ids")
    if scope is None:
        scope = result.get("approved_scope")
    if scope is None and status == "approved":
        scope = list(case_ids or [])
    result["status"] = status
    result["approved_case_ids"] = list(dict.fromkeys(scope or []))
    result["plan_fingerprint"] = result.get("plan_fingerprint") or result.get("design_fingerprint") or plan_fingerprint
    result["decided_at"] = result.get("decided_at") or result.get("approved_at") or result.get("reviewed_at")
    result["reviewer_id"] = result.get("reviewer_id") or result.get("reviewer")
    fields = result.get("reviewed_fields")
    if isinstance(fields, list):
        result["reviewed_fields"] = {case_id: list(fields) for case_id in result["approved_case_ids"]}
    elif not isinstance(fields, dict):
        result["reviewed_fields"] = {}
    result["accepted_gap_ids"] = list(result.get("accepted_gap_ids") or result.get("accepted_gaps") or [])
    result.setdefault("decision_id", f"DEC-{_hash({'status': status, 'plan': result.get('plan_fingerprint'), 'scope': result['approved_case_ids']})[7:23]}")
    return result


def build_user_decision(review: dict[str, Any], *, plan_id: str, plan_fingerprint: str, source: dict[str, Any] | None = None) -> dict[str, Any]:
    canonical = normalize_review(review, plan_fingerprint=plan_fingerprint)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "artifact_type": "user_decision",
        "artifact_id": canonical["decision_id"],
        "schema_version": ARTIFACT_SCHEMAS["user_decision"],
        "decision_type": "test_design_review",
        "subject": {"plan_id": plan_id, "plan_fingerprint": plan_fingerprint},
        "decision": {
            "status": canonical["status"],
            "approved_case_ids": canonical["approved_case_ids"],
            "accepted_gap_ids": canonical["accepted_gap_ids"],
        },
        "actor": {"type": "user" if canonical.get("reviewer_id") in {None, "user"} else "reviewer", "id": canonical.get("reviewer_id")},
        "source": copy.deepcopy(source or {"type": "chat"}),
        "decided_at": canonical.get("decided_at") or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "expires_at": canonical.get("expires_at"),
        "superseded_by": canonical.get("superseded_by"),
    }


def normalize_scan_fact(fact: dict[str, Any], *, scan_id: str, baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(fact, dict):
        raise ValueError("scan fact must be an object")
    result = copy.deepcopy(fact)
    result.setdefault("fact_id", f"FACT-{_hash({'scan_id': scan_id, 'fact': result})[7:23]}")
    result.setdefault("candidate_only", True)
    result.setdefault("semantic_decision_owner", "ai_skill")
    result.setdefault("status", "candidate")
    result.setdefault("scan_id", scan_id)
    result.setdefault("baseline", copy.deepcopy(baseline or {}))
    return result


def result_verdict_to_requirement_status(verdict: str, *, api_verifiability: str = "direct", external_evidence_status: str = "complete") -> str:
    if api_verifiability == "none":
        return "not_verified"
    if verdict == "passed" and api_verifiability == "direct" and external_evidence_status == "complete":
        return "accepted"
    if verdict in {"failed", "error"}:
        return "failed"
    if verdict == "blocked":
        return "blocked"
    return "not_verified"
