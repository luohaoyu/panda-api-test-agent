#!/usr/bin/env python3
"""Build a minimal, reviewable API debugging TestIntent.

Debugging is deliberately smaller than quality testing: one reproduction case
by default, no automatic retry, and no expansion to the quality dimensions.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_observation(value: Any) -> Any:
    if not isinstance(value, dict):
        return {}
    allowed = {"http_status", "business_code", "message", "headers", "body", "expected", "correlation_id", "trace_id"}
    return copy.deepcopy({key: value[key] for key in value if key in allowed})


def build_api_debug_intent(requirement_id: str, target: dict[str, Any], environment_id: str, observation: dict[str, Any] | None = None, options: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(requirement_id, str) or not requirement_id.strip():
        raise ValueError("requirement_id is required")
    if not isinstance(target, dict):
        raise ValueError("target must be an object")
    if not isinstance(environment_id, str) or not environment_id.strip():
        raise ValueError("environment_id is required")
    options = options if isinstance(options, dict) else {}
    target_copy = {key: copy.deepcopy(target[key]) for key in ("interface_id", "method", "path", "profile_id", "profile_revision", "contract_fingerprint") if key in target}
    request = copy.deepcopy(options.get("request") or {})
    request.setdefault("method", target.get("method") or "UNCONFIRMED")
    request.setdefault("path", target.get("path") or "UNCONFIRMED")
    case_id = str(options.get("case_id") or "DBG-001-REPRO")
    observed = _safe_observation(observation)
    expected = copy.deepcopy(options.get("expected") or observed.get("expected") or {})
    side_effect = str(options.get("side_effect") or ("read" if str(request.get("method", "GET")).upper() in {"GET", "HEAD", "OPTIONS"} else "write"))
    case = {
        "case_id": case_id,
        "task_id": case_id,
        "type": "business",
        "entry_type": "api_debug",
        "name": "API debug minimal reproduction",
        "debug_action": "minimal_reproduction",
        "requirement_refs": [requirement_id],
        "acceptance_refs": ["AC-DEBUG-REPRO"],
        "interface_refs": [target.get("interface_id") or target.get("path") or "target-interface"],
        "environment_id": environment_id,
        "request": request,
        "method": request.get("method", "GET"),
        "side_effect": side_effect,
        "assertions": copy.deepcopy(options.get("assertions") or []),
        "data_dependencies": copy.deepcopy(options.get("data_dependencies") or []),
        "expected_request_count": 1,
        "approval": {"required": True, "reason": "human design review"},
        "review_status": "pending_review",
        "applicability": {
            "status": "ai_decided",
            "decision_owner": options.get("decision_owner", "ai"),
            "decision_evidence_refs": copy.deepcopy(options.get("decision_evidence_refs") or ["debug:explicit-minimal-reproduction"]),
            "candidate_only": False,
        },
    }
    if side_effect in {"write", "write_attempt", "destructive"}:
        case["object_ref"] = {"type": "debug_runtime_object", "case_id": case_id}
        case["cleanup_waiver_candidate"] = {"candidate": True, "reason": "debug request may create a business object"}
    context = {
        "debug_case_id": case_id,
        "observed": observed,
        "expected": expected,
        "request_source": options.get("request_source", "user_input"),
        "request_summary": {"method": request.get("method"), "path": request.get("path"), "parameter_categories": copy.deepcopy(options.get("parameter_categories") or [])},
        "reproduction_steps": copy.deepcopy(options.get("reproduction_steps") or []),
        "correlation_id": observed.get("correlation_id"),
        "trace_id": observed.get("trace_id"),
        "diagnostic_hypotheses": copy.deepcopy(options.get("diagnostic_hypotheses") or []),
        "diagnostic_evidence_refs": copy.deepcopy(options.get("diagnostic_evidence_refs") or []),
        "retry_policy": "no_automatic_retry",
        "next_diagnostic_action": options.get("next_diagnostic_action"),
    }
    gaps = []
    if not observed:
        gaps.append({"gap_id": "GAP-DEBUG-OBSERVATION", "blocking": False, "stage": "design", "description": "observed response is not supplied", "impact": "debug conclusion will rely on the new reproduction response", "next_action": "approve one minimal reproduction"})
    if not expected:
        gaps.append({"gap_id": "GAP-DEBUG-EXPECTED", "blocking": False, "stage": "design", "description": "expected response is not supplied", "impact": "the runner cannot assert the original mismatch without an explicit expected outcome", "next_action": "provide expected status or business result"})
    intent = {
        "schema_version": "2.1",
        "entry_type": "api_debug",
        "intent_type": "api_debug",
        "requirement_id": requirement_id,
        "interface_id": target_copy.get("interface_id") or target_copy.get("path"),
        "target": target_copy,
        "environment_id": environment_id,
        "scope": {"kind": "minimal_reproduction", "max_cases": 1},
        "debug_context": context,
        "mode": "EXECUTE",
        "acceptance_criteria": [{"ac_id": "AC-DEBUG-REPRO", "text": "reproduce and diagnose the observed API behavior", "interface_verifiability": "direct", "verifiability_decision_status": "ai_decided", "verifiability_decision_owner": options.get("decision_owner", "ai"), "decision_evidence_refs": copy.deepcopy(options.get("decision_evidence_refs") or ["debug:explicit-minimal-reproduction"])}],
        "quality_context": {"dimensions": ["minimal_reproduction"], "max_request_count": 1, "stop_on_first_failure": True},
        "known_gaps": gaps,
        "cases": [case],
        "tasks": [copy.deepcopy(case)],
        "review": {"status": "pending_review", "approved_case_ids": [], "reviewed_fields": []},
    }
    intent["design_fingerprint"] = "sha256:" + hashlib.sha256(_canonical(intent).encode("utf-8")).hexdigest()
    return intent
