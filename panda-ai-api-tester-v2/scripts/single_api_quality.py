#!/usr/bin/env python3
"""Build a side-effect-free single API quality TestIntent."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


DIMENSIONS = (
    "positive",
    "required_parameters",
    "negative",
    "boundary",
    "authentication",
    "authorization",
    "response_contract",
    "idempotency",
    "side_effect",
)
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _profile_value(profile: dict[str, Any], *keys: str, default=None):
    for key in keys:
        if key in profile and profile[key] is not None:
            return profile[key]
    return default


def _request_profile(profile: dict[str, Any]) -> dict[str, Any]:
    request = profile.get("request")
    return request if isinstance(request, dict) else {}


def _method(profile: dict[str, Any]) -> str | None:
    value = _profile_value(_request_profile(profile), "method") or _profile_value(profile, "method")
    return str(value).upper() if isinstance(value, str) and value.strip() else None


def _path(profile: dict[str, Any], target: Any) -> str | None:
    value = _profile_value(_request_profile(profile), "path", "route")
    if value is None:
        value = _profile_value(profile, "path", "route")
    if value is None and isinstance(target, str) and target.strip():
        value = target.strip()
    return value if isinstance(value, str) and value.strip() else None


def _parameters(profile: dict[str, Any]) -> list[dict[str, Any]]:
    request = _request_profile(profile)
    raw = _profile_value(request, "parameters", "params")
    if raw is None:
        raw = _profile_value(profile, "parameters", "params")
    if isinstance(raw, dict):
        result = []
        for name, value in raw.items():
            item = copy.deepcopy(value) if isinstance(value, dict) else {"example": value}
            item.setdefault("name", name)
            result.append(item)
        return result
    if isinstance(raw, list):
        return [copy.deepcopy(item) for item in raw if isinstance(item, dict) and isinstance(item.get("name"), str)]
    return []


def _known_gaps(profile: dict[str, Any], method: str | None, path: str | None, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gaps = []
    if not method:
        gaps.append({"gap_id": "GAP-SAQ-METHOD", "blocking": True, "stage": "design", "description": "interface method is not declared", "impact": "the request shape cannot be reviewed safely", "next_action": "confirm the HTTP method"})
    if not path:
        gaps.append({"gap_id": "GAP-SAQ-PATH", "blocking": True, "stage": "design", "description": "interface path is not declared", "impact": "the target route cannot be reviewed safely", "next_action": "confirm the target path"})
    response = _profile_value(profile, "response", "responses", "response_contract")
    if not response:
        gaps.append({"gap_id": "GAP-SAQ-RESPONSE", "blocking": False, "stage": "design", "description": "response contract is not declared", "impact": "response assertions remain incomplete", "next_action": "confirm HTTP, business code, and key fields"})
    if not parameters:
        gaps.append({"gap_id": "GAP-SAQ-PARAMETERS", "blocking": False, "stage": "design", "description": "request parameters are not declared", "impact": "parameter and boundary coverage is limited", "next_action": "confirm required, type, format, and boundary facts"})
    if not any(item.get("invalid_examples") or item.get("invalid") for item in parameters):
        gaps.append({"gap_id": "GAP-SAQ-NEGATIVE", "blocking": False, "stage": "design", "description": "invalid input facts are not declared", "impact": "negative cases cannot assert a factual contract", "next_action": "confirm invalid examples or expected validation behavior"})
    if not any(item.get("boundary") for item in parameters):
        gaps.append({"gap_id": "GAP-SAQ-BOUNDARY", "blocking": False, "stage": "design", "description": "parameter boundaries are not declared", "impact": "boundary cases cannot assert factual limits", "next_action": "confirm lower, upper, length, and empty-value limits"})
    auth = _profile_value(profile, "authentication", "auth")
    if auth is None:
        gaps.append({"gap_id": "GAP-SAQ-AUTH", "blocking": False, "stage": "design", "description": "authentication requirements are not declared", "impact": "authentication coverage cannot be made factual", "next_action": "confirm authentication mode and credential binding"})
    authorization = _profile_value(profile, "authorization", "permissions", "access_control")
    if authorization is None:
        gaps.append({"gap_id": "GAP-SAQ-AUTHZ", "blocking": False, "stage": "design", "description": "authorization requirements are not declared", "impact": "permission coverage cannot be made factual", "next_action": "confirm roles, resource scope, and expected denial behavior"})
    return gaps


def _assertions(profile: dict[str, Any], dimension: str) -> list[dict[str, Any]]:
    response = _profile_value(profile, "response", "responses", "response_contract")
    assertions = []
    if response:
        assertions.append({"assertion_id": f"ASSERT-{dimension.upper()}-CONTRACT", "kind": "response_contract", "expected": copy.deepcopy(response)})
    else:
        assertions.append({"assertion_id": f"ASSERT-{dimension.upper()}-CONTRACT", "kind": "response_contract", "expected": "profile_declared_only", "known_gap": "GAP-SAQ-RESPONSE"})
    return assertions


def _teardown(method: str | None, profile: dict[str, Any], case_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    side_effect = _profile_value(profile, "side_effect", "side_effect_level")
    is_write = side_effect in {"write", "destructive", "write_attempt"} or method in WRITE_METHODS
    if not is_write:
        return None, None, None
    object_ref = {"type": "runtime_created_resource", "case_id": case_id}
    cleanup = _profile_value(profile, "teardown", "cleanup")
    if isinstance(cleanup, dict):
        return copy.deepcopy(cleanup), object_ref, None
    return None, object_ref, {
        "candidate": True,
        "reason": "profile does not declare a verifiable teardown",
        "requires_manual_approval": True,
        "object_ref": copy.deepcopy(object_ref),
    }


def _decision_record(value: Any) -> tuple[bool | None, str | None, list[str]]:
    if isinstance(value, bool):
        return value, None, []
    if not isinstance(value, dict):
        return None, None, []
    applicable = value.get("applicable")
    if not isinstance(applicable, bool):
        return None, value.get("decision_owner"), list(value.get("decision_evidence_refs") or [])
    owner = value.get("decision_owner")
    refs = [item.strip() for item in value.get("decision_evidence_refs") or [] if isinstance(item, str) and item.strip()]
    return applicable, owner if isinstance(owner, str) and owner.strip() else None, refs


def _case(dimension: str, method: str | None, path: str | None, profile: dict[str, Any], parameters: list[dict[str, Any]], requirement_id: str, environment_id: str, index: int, decision: Any = None) -> dict[str, Any]:
    case_id = f"SAQ-{index:03d}-{dimension.upper()}"
    teardown, object_ref, waiver = _teardown(method, profile, case_id)
    request = {"method": method or "UNCONFIRMED", "path": path or "UNCONFIRMED", "parameters": copy.deepcopy(parameters)}
    applicable, decision_owner, decision_evidence_refs = _decision_record(decision)
    case = {
        "case_id": case_id,
        "task_id": case_id,
        "type": "business",
        "name": f"single API quality: {dimension}",
        "dimension": dimension,
        "entry_type": "single_api_quality",
        "requirement_refs": [requirement_id],
        "acceptance_refs": [f"AC-{dimension.upper()}"],
        "interface_refs": [_profile_value(profile, "interface_id", "interface_ref", default=path) or "target-interface"],
        "business_rule_refs": copy.deepcopy(_profile_value(profile, "business_rule_refs", "business_rules", default=[])),
        "scenario_refs": copy.deepcopy(_profile_value(profile, "scenario_refs", "scenarios", default=[])),
        "profile_id": _profile_value(profile, "profile_id", "id"),
        "profile_revision": _profile_value(profile, "profile_revision", "revision"),
        "contract_fingerprint": _profile_value(profile, "contract_fingerprint"),
        "environment_id": environment_id,
        "approval": {"required": True, "reason": "human design review"},
        "request": request,
        "method": method or "GET",
        "depends_on": [],
        "side_effect": _profile_value(profile, "side_effect", "side_effect_level") or ("read" if method in READ_METHODS else "write"),
        "assertions": _assertions(profile, dimension),
        "data_dependencies": copy.deepcopy(_profile_value(profile, "data_dependencies", "dependencies", default=[])),
        "expected_request_count": 1,
        "review_status": "pending_review",
    }
    case["applicability"] = {
        "status": "ai_decided" if isinstance(applicable, bool) and decision_owner and decision_evidence_refs else "pending_ai_decision",
        "decision_owner": decision_owner or "pending",
        "decision_evidence_refs": decision_evidence_refs,
        "candidate_only": not (isinstance(applicable, bool) and decision_owner and decision_evidence_refs),
    }
    if object_ref is not None:
        case["object_ref"] = object_ref
    if teardown is not None:
        case["teardown"] = teardown
    if waiver is not None:
        case["cleanup_waiver_candidate"] = waiver
    if dimension == "required_parameters":
        required = [item.get("name") for item in parameters if item.get("required") is True]
        case["mutation"] = {"omit_parameters": required}
    elif dimension == "negative":
        case["mutation"] = {"invalid_input": [item.get("invalid_examples") or item.get("invalid") for item in parameters if item.get("invalid_examples") or item.get("invalid")] or None}
    elif dimension == "boundary":
        case["mutation"] = {"boundary_values": [item.get("boundary") for item in parameters if item.get("boundary") is not None] or None}
    elif dimension == "authentication":
        case["auth_mode"] = "missing_or_invalid_binding"
    elif dimension == "authorization":
        case["auth_mode"] = "out_of_scope_resource_only_if_profile_declares_it"
    elif dimension == "idempotency":
        case["repeat_policy"] = "repeat_only_after_review"
    elif dimension == "side_effect":
        case["side_effect_check"] = copy.deepcopy(_profile_value(profile, "side_effect", "side_effect_level"))
    return case


def build_single_api_quality_intent(requirement_id: str, interface_profile: dict[str, Any], target: str, environment_id: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a deterministic TestIntent; never performs network or secret access."""
    if not isinstance(requirement_id, str) or not requirement_id.strip():
        raise ValueError("requirement_id is required")
    if not isinstance(interface_profile, dict):
        raise ValueError("interface_profile must be an object")
    if not isinstance(environment_id, str) or not environment_id.strip():
        raise ValueError("environment_id is required")
    options = options if isinstance(options, dict) else {}
    method = _method(interface_profile)
    path = _path(interface_profile, target)
    parameters = _parameters(interface_profile)
    # AI/Skill decides which dimensions are semantically applicable. The builder
    # only validates that decision and materializes candidates. Without an AI
    # decision, preserve the traditional checklist as non-executable candidates
    # rather than silently deciding applicability in Python.
    ai_decisions = options.get("ai_dimension_decisions")
    if ai_decisions is None:
        ai_decisions = interface_profile.get("ai_dimension_decisions")
    decision_map = dict(ai_decisions) if isinstance(ai_decisions, dict) else {}
    # An explicit profile fact can safely exclude a dimension; inclusion still
    # requires the AI/Skill applicability decision.
    explicit_idempotency = _profile_value(interface_profile, "idempotency_applicable", "idempotent", "supports_idempotency")
    if explicit_idempotency is False and "idempotency" not in decision_map:
        decision_map["idempotency"] = False
    requested = options.get("dimensions")
    requested_dimensions = requested if isinstance(requested, list) else list(DIMENSIONS)
    dimensions = [item for item in requested_dimensions if item in DIMENSIONS]
    decision_inputs = {item: decision_map.get(item) for item in requested_dimensions if item in DIMENSIONS}
    if not decision_map:
        selected_dimensions = list(dimensions)
        dimension_decision_status = "pending_ai_decision"
    else:
        selected_dimensions = []
        unresolved = False
        for dimension in dimensions:
            applicable, owner, refs = _decision_record(decision_map.get(dimension))
            if applicable is True or applicable is None:
                selected_dimensions.append(dimension)
            if not (isinstance(applicable, bool) and owner and refs):
                unresolved = True
        dimension_decision_status = "partial_ai_decision" if unresolved else "ai_decided"
    cases = [
        _case(dimension, method, path, interface_profile, parameters, requirement_id, environment_id, index, decision_inputs.get(dimension))
        for index, dimension in enumerate(selected_dimensions, 1)
    ]
    gaps = _known_gaps(interface_profile, method, path, parameters)
    if dimension_decision_status != "ai_decided":
        gaps.append({"gap_id": "GAP-SAQ-DIMENSION-DECISION", "blocking": True, "stage": "design", "description": "测试维度适用性尚未由 AI 结合接口语义和风险判断", "impact": "候选用例不得直接进入审核执行范围", "next_action": "由 AI/人工确认各质量维度适用性"})
    decision_owners = {
        case["applicability"]["decision_owner"]
        for case in cases
        if case["applicability"]["status"] == "ai_decided"
    }
    scope_owner = next(iter(decision_owners)) if len(decision_owners) == 1 else "pending"
    acceptance_criteria = []
    for dimension, case in ((case["dimension"], case) for case in cases):
        applicability = case["applicability"]
        acceptance_criteria.append({
            "ac_id": f"AC-{dimension.upper()}",
            "text": f"cover {dimension} for the target interface",
            "interface_verifiability": "direct",
            "verifiability_decision_status": applicability["status"],
            "verifiability_decision_owner": applicability["decision_owner"],
            "decision_evidence_refs": copy.deepcopy(applicability["decision_evidence_refs"]),
        })
    side_effect = _profile_value(interface_profile, "side_effect", "side_effect_level") or ("read" if method in READ_METHODS else "write")
    intent = {
        "schema_version": "2.1",
        "entry_type": "single_api_quality",
        "intent_type": "single_api_quality",
        "requirement_id": requirement_id,
        "interface_id": _profile_value(interface_profile, "interface_id", "interface_ref", default=path),
        "target": {"interface_id": _profile_value(interface_profile, "interface_id", "interface_ref", default=path), "method": method, "path": path, "profile_id": _profile_value(interface_profile, "profile_id", "id"), "profile_revision": _profile_value(interface_profile, "profile_revision", "revision"), "contract_fingerprint": _profile_value(interface_profile, "contract_fingerprint")},
        "scope": {"kind": "single_interface", "dimensions": [case["dimension"] for case in cases], "dimension_decision_status": dimension_decision_status, "decision_owner": scope_owner},
        "environment_id": environment_id,
        "mode": "EXECUTE",
        "acceptance_criteria": acceptance_criteria,
        "source_fingerprints": copy.deepcopy(options.get("source_fingerprints") or {}),
        "quality_context": {"dimensions": [case["dimension"] for case in cases], "dimension_decision_status": dimension_decision_status, "max_request_count": options.get("max_request_count", len(cases)), "stop_on_first_failure": options.get("stop_on_first_failure", True)},
        "risk_policy": {"design_only": True, "side_effect": side_effect},
        "known_gaps": gaps,
        "cases": cases,
        "tasks": [copy.deepcopy(case) for case in cases if not case["applicability"]["candidate_only"]],
        "review": {"status": "pending_review", "approved_case_ids": [], "reviewed_fields": []},
    }
    intent["design_fingerprint"] = "sha256:" + hashlib.sha256(_canonical(intent).encode("utf-8")).hexdigest()
    return intent
