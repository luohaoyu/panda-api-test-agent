#!/usr/bin/env python3
"""Build a requirement-validation intent from an AI-produced decision.

The semantic classification of an acceptance item belongs to AI guided by the
Skill instructions. This module only normalizes the decision, validates the
allowed values, records decision evidence, and enforces the safety invariant
that ``none`` items never become API tasks. When no semantic decision is
provided, the item remains ``pending_ai_decision`` instead of being guessed
from the presence of scope fields.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


VERIFIABILITY = {"direct", "partial", "none"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def classify_acceptance_criteria(criteria: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for index, raw in enumerate(criteria, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"acceptance criterion {index} must be an object")
        item = copy.deepcopy(raw)
        ac_id = item.get("ac_id") or item.get("id") or f"AC-{index:02d}"
        declared = item.get("interface_verifiability")
        decision_status = "ai_decided" if declared in VERIFIABILITY else "pending_ai_decision"
        if declared not in VERIFIABILITY:
            interface_scope_hint = item.get("interface_scope") or []
            non_interface_scope_hint = item.get("non_interface_scope") or []
            # Legacy structured scope is accepted only as an explicit semantic
            # decision supplied by the caller; do not infer from one-sided scope.
            if interface_scope_hint and non_interface_scope_hint:
                declared = "partial"
                decision_status = "ai_decided"
            else:
                declared = None
        interface_scope = list(item.get("interface_scope") or [])
        non_interface_scope = list(item.get("non_interface_scope") or [])
        if decision_status == "pending_ai_decision":
            interface_scope = []
        if declared == "none":
            interface_scope = []
        reason = item.get("verifiability_reason") or (
            "等待 AI 结合需求、源码、契约和 Oracle 判断接口可验证性"
            if decision_status == "pending_ai_decision" else {
                "direct": "验收目标和必需后端 Oracle 可由接口层完整验证",
                "partial": "接口层只能验证验收目标的一部分",
                "none": "接口请求、响应和当前后端 Oracle 无法验证该验收目标",
            }[declared]
        )
        layers = list(item.get("recommended_validation_layers") or (
            ["AI/人工判定后补充"] if decision_status == "pending_ai_decision"
            else ([] if declared == "direct" else ["人工/其他验证层"])
        ))
        item.update({
            "ac_id": ac_id,
            "interface_verifiability": declared,
            "verifiability_decision_status": decision_status,
            "verifiability_reason": reason,
            "verifiability_decision_owner": item.get("verifiability_decision_owner") or ("ai" if decision_status == "ai_decided" else "pending"),
            "decision_evidence_refs": [item.strip() for item in (item.get("decision_evidence_refs") or []) if isinstance(item, str) and item.strip()],
            "interface_scope": interface_scope,
            "non_interface_scope": non_interface_scope,
            "recommended_validation_layers": layers,
            "requirement_refs": list(item.get("requirement_refs") or []),
            "business_rule_refs": list(item.get("business_rule_refs") or []),
            "scenario_refs": list(item.get("scenario_refs") or []),
        })
        result.append(item)
    return result


def build_requirement_validation_intent(context: dict[str, Any], environment_id: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(context, dict):
        raise ValueError("context must be an object")
    if not isinstance(environment_id, str) or not environment_id.strip():
        raise ValueError("environment_id is required")
    options = options if isinstance(options, dict) else {}
    requirement_id = context.get("requirement_id") or context.get("id")
    if not isinstance(requirement_id, str) or not requirement_id.strip():
        raise ValueError("context.requirement_id is required")
    criteria = classify_acceptance_criteria(context.get("acceptance_criteria") or context.get("acceptance_items") or [])
    interface_tasks = []
    for criterion in criteria:
        if criterion.get("verifiability_decision_status") != "ai_decided":
            continue
        if criterion["interface_verifiability"] == "none":
            continue
        for task in criterion.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            if not task.get("task_id") and not task.get("case_id"):
                continue
            item = copy.deepcopy(task)
            item.setdefault("type", "business")
            item.setdefault("requirement_refs", [requirement_id])
            item.setdefault("acceptance_refs", [criterion["ac_id"]])
            item.setdefault("interface_verifiability", criterion["interface_verifiability"])
            interface_tasks.append(item)
    gaps = copy.deepcopy(context.get("known_gaps") or [])
    for criterion in criteria:
        if criterion.get("verifiability_decision_status") != "ai_decided":
            gaps.append({"gap_id": f"GAP-{criterion['ac_id']}-AI-DECISION", "blocking": True, "stage": "design", "description": "等待 AI 结合需求、源码、契约和 Oracle 判断接口可验证性", "impact": "不得生成或执行该验收项的 API 用例", "next_action": "由 AI/人工补充 direct、partial 或 none 决策及证据"})
        elif criterion["interface_verifiability"] == "none":
            gaps.append({"gap_id": f"GAP-{criterion['ac_id']}-NON-API", "blocking": False, "stage": "report", "description": "接口层不可验证该验收项", "impact": "不得由 API 运行形成通过结论", "next_action": ", ".join(criterion["recommended_validation_layers"])})
    intent = {
        "schema_version": "2.1",
        "entry_type": "requirement_validation",
        "intent_type": "requirement_validation",
        "requirement_id": requirement_id,
        "environment_id": environment_id,
        "scope": {"kind": "requirement", "acceptance_total": len(criteria)},
        "requirement_context": {key: copy.deepcopy(context.get(key)) for key in ("context_path", "prd_path", "prd_version", "branch", "commit") if key in context},
        "acceptance_criteria": criteria,
        "mode": "EXECUTE",
        "known_gaps": gaps,
        "tasks": interface_tasks,
        "cases": copy.deepcopy(interface_tasks),
        "review": {"status": "pending_review", "approved_case_ids": [], "reviewed_fields": []},
        "source_fingerprints": copy.deepcopy(context.get("source_fingerprints") or {}),
        "risk_policy": {"design_only": True},
    }
    if not interface_tasks:
        intent["known_gaps"].append({"gap_id": "GAP-REQUIREMENT-NO-API-TASKS", "blocking": True, "stage": "design", "description": "没有可由接口层执行的验收项", "impact": "需求入口只能输出非接口验证范围，不能创建 API 执行包", "next_action": "转交 UI、客户端、性能或人工验证层"})
    intent["design_fingerprint"] = "sha256:" + hashlib.sha256(_canonical(intent).encode("utf-8")).hexdigest()
    return intent
