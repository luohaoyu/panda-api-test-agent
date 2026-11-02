#!/usr/bin/env python3
"""Deterministic compiler from TestIntent to an executable slice plan.

The compiler is deliberately side-effect free: it validates and returns a plan,
but never creates a run directory or sends a request.  Runtime scheduling is
not performed here; slice order and execution policy are fixed in the plan.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any


class PlanCompileError(ValueError):
    """Raised when an intent cannot be compiled safely."""


TASK_TYPES = {"probe", "fixture", "business", "oracle", "cleanup", "reduce"}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
WRITE_METHODS = {"POST", "PUT", "PATCH"}
DESTRUCTIVE_METHODS = {"DELETE"}
SEMANTIC_ENTRY_TYPES = {"requirement_validation", "single_api_quality", "api_debug"}
DECIDED_STATUS = "ai_decided"
PENDING_STATUSES = {"pending_ai_decision", "partial_ai_decision"}
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
}
VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fingerprint(plan: dict[str, Any]) -> str:
    payload = copy.deepcopy(plan)
    payload.pop("plan_fingerprint", None)
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def _method(task: dict[str, Any]) -> str:
    request = task.get("request") or {}
    return str(request.get("method", task.get("method", "GET"))).upper()


def _contains_sensitive_headers(value: Any, location: str = "task") -> str | None:
    if isinstance(value, dict):
        headers = value.get("headers")
        if isinstance(headers, dict):
            found = [str(key) for key in headers if str(key).lower() in SENSITIVE_HEADERS]
            if found:
                return f"{location}.headers contains inline sensitive headers: {', '.join(sorted(found))}"
        for key, item in value.items():
            found = _contains_sensitive_headers(item, f"{location}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _contains_sensitive_headers(item, f"{location}[{index}]")
            if found:
                return found
    return None


def _infer_type(task: dict[str, Any]) -> str:
    explicit = task.get("type") or task.get("task_type")
    if explicit:
        return str(explicit).lower()
    kind = str(task.get("kind", "")).lower()
    if kind in TASK_TYPES:
        return kind
    if task.get("oracle") or task.get("oracle_type"):
        return "oracle"
    if task.get("teardown") or task.get("cleanup"):
        return "cleanup"
    if task.get("fixture"):
        return "fixture"
    return "business"


def _topological_levels(tasks: list[dict[str, Any]]) -> list[list[str]]:
    dependencies = {task["task_id"]: set(task.get("depends_on") or []) for task in tasks}
    remaining = set(dependencies)
    resolved: set[str] = set()
    levels: list[list[str]] = []
    while remaining:
        ready = sorted(task_id for task_id in remaining if dependencies[task_id].issubset(resolved))
        if not ready:
            raise PlanCompileError("task dependency graph contains a cycle")
        levels.append(ready)
        resolved.update(ready)
        remaining.difference_update(ready)
    return levels


def _safe_parallel(task: dict[str, Any]) -> bool:
    method = _method(task)
    effect = str(task.get("side_effect", "read" if method in READ_METHODS else "write")).lower()
    return (
        method in READ_METHODS
        and effect in {"none", "read"}
        and not task.get("resource_locks")
        and not task.get("shared_variables")
        and not task.get("volatile")
    )


def _decision_refs(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _validate_semantic_decisions(intent: dict[str, Any], raw_tasks: list[dict[str, Any]]) -> None:
    """Keep semantic candidate discovery outside the executable plan boundary."""
    entry_type = str(intent.get("entry_type") or intent.get("intent_type") or "").lower()
    scope = intent.get("scope") if isinstance(intent.get("scope"), dict) else {}
    scope_status = scope.get("dimension_decision_status")
    if entry_type in SEMANTIC_ENTRY_TYPES and scope_status in PENDING_STATUSES and raw_tasks:
        raise PlanCompileError(f"{entry_type} has unresolved semantic decision status: {scope_status}")

    criteria = intent.get("acceptance_criteria") or intent.get("acceptance_items") or []
    criteria_by_id = {
        item.get("ac_id") or item.get("id"): item
        for item in criteria
        if isinstance(item, dict) and (item.get("ac_id") or item.get("id"))
    }
    for task in raw_tasks:
        if not isinstance(task, dict):
            raise PlanCompileError("each task must be an object")
        task_id = task.get("task_id") or task.get("case_id") or "<unknown>"
        applicability = task.get("applicability") if isinstance(task.get("applicability"), dict) else {}
        if applicability.get("candidate_only") is True:
            raise PlanCompileError(f"task {task_id} is candidate_only and cannot enter an executable plan")
        status = applicability.get("status")
        if status in PENDING_STATUSES:
            raise PlanCompileError(f"task {task_id} has unresolved applicability status: {status}")
        if applicability and status != DECIDED_STATUS:
            raise PlanCompileError(f"task {task_id} applicability.status must be {DECIDED_STATUS}")
        if applicability:
            owner = applicability.get("decision_owner")
            if not isinstance(owner, str) or not owner.strip() or owner == "pending":
                raise PlanCompileError(f"task {task_id} requires a decision_owner")
            if not _decision_refs(applicability.get("decision_evidence_refs")):
                raise PlanCompileError(f"task {task_id} requires decision_evidence_refs")

        refs = set(task.get("acceptance_refs") or [])
        if task.get("ac_id"):
            refs.add(task["ac_id"])
        for ac_id in sorted(refs):
            criterion = criteria_by_id.get(ac_id)
            if not criterion:
                continue
            semantic_criterion = entry_type in SEMANTIC_ENTRY_TYPES or any(
                key in criterion
                for key in ("interface_verifiability", "verifiability_decision_status", "verifiability_decision_owner", "decision_evidence_refs")
            )
            if not semantic_criterion:
                continue
            decision_status = criterion.get("verifiability_decision_status")
            if decision_status != DECIDED_STATUS:
                raise PlanCompileError(f"task {task_id} references acceptance criterion {ac_id} with unresolved decision")
            if criterion.get("interface_verifiability") not in {"direct", "partial"}:
                raise PlanCompileError(f"task {task_id} references non-interface acceptance criterion {ac_id}")
            owner = criterion.get("verifiability_decision_owner")
            if not isinstance(owner, str) or not owner.strip() or owner == "pending":
                raise PlanCompileError(f"acceptance criterion {ac_id} requires a decision owner")
            if not _decision_refs(criterion.get("decision_evidence_refs")):
                raise PlanCompileError(f"acceptance criterion {ac_id} requires decision_evidence_refs")


def _validate_task(task: dict[str, Any], mode: str, known_ids: set[str]) -> dict[str, Any]:
    if not isinstance(task, dict):
        raise PlanCompileError("each task must be an object")
    task = copy.deepcopy(task)
    task_id = task.get("task_id") or task.get("case_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise PlanCompileError("every task requires a non-empty task_id")
    task["task_id"] = task_id
    task_type = _infer_type(task)
    if task_type not in TASK_TYPES:
        raise PlanCompileError(f"task {task_id} has unsupported type: {task_type}")
    task["type"] = task_type
    if task_type == "business" and not task.get("case_id"):
        task["case_id"] = task_id
    dependencies = task.get("depends_on") or []
    if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
        raise PlanCompileError(f"task {task_id}.depends_on must be a list of task IDs")
    unknown = set(dependencies) - known_ids
    if unknown:
        raise PlanCompileError(f"task {task_id} has unknown dependencies: {sorted(unknown)}")
    task["depends_on"] = sorted(set(dependencies))
    sensitive_error = _contains_sensitive_headers(task)
    if sensitive_error:
        raise PlanCompileError(sensitive_error)

    method = _method(task)
    side_effect = str(task.get("side_effect", "read" if method in READ_METHODS else "write")).lower()
    task["method"] = method
    task["side_effect"] = side_effect
    if task_type == "probe" and (method not in READ_METHODS or side_effect not in {"none", "read"}):
        raise PlanCompileError(f"probe task {task_id} must be read-only GET/HEAD/OPTIONS")
    if mode == "PROBE" and task_type not in {"probe", "reduce"}:
        raise PlanCompileError(f"PROBE intent cannot contain task type {task_type}: {task_id}")
    if task_type == "business" and method in DESTRUCTIVE_METHODS:
        side_effect = "destructive"
        task["side_effect"] = side_effect
    is_write = side_effect in {"write", "write_attempt", "destructive"} or method in (WRITE_METHODS | DESTRUCTIVE_METHODS)
    if is_write and side_effect != "write_attempt":
        if not task.get("teardown") and not task.get("cleanup") and not task.get("object_ref"):
            raise PlanCompileError(f"write task {task_id} requires teardown, cleanup, or object_ref")
    if side_effect == "write_attempt" and not task.get("verification"):
        raise PlanCompileError(f"write_attempt task {task_id} requires read-only verification")
    approval_reasons = []
    if side_effect == "destructive" or method in DESTRUCTIVE_METHODS:
        approval_reasons.append("destructive")
    if task.get("batch") is True or task.get("batch_operation") is True:
        approval_reasons.append("batch")
    if task.get("production") is True or task.get("real_external_action") is True:
        approval_reasons.append("protected_target")
    approval = task.get("approval") or {}
    if approval is True:
        approval = {"required": True, "reason": "explicit"}
    if not isinstance(approval, dict):
        raise PlanCompileError(f"task {task_id}.approval must be an object")
    if approval_reasons:
        approval = dict(approval)
        approval["required"] = True
        approval["reasons"] = sorted(set((approval.get("reasons") or []) + approval_reasons))
    task["approval"] = {"required": bool(approval.get("required", False)), **{key: value for key, value in approval.items() if key != "required"}}
    task["volatile"] = bool(task.get("volatile", False))
    locks = task.get("resource_locks") or []
    if not isinstance(locks, list) or any(not isinstance(item, str) or not item for item in locks):
        raise PlanCompileError(f"task {task_id}.resource_locks must be a list of strings")
    task["resource_locks"] = sorted(set(locks))
    task["cleanup_required"] = bool(is_write and (task.get("teardown") or task.get("cleanup") or task.get("object_ref")))
    if task_type == "fixture" and not (task.get("fixture_name") or task.get("fixture") or task.get("action")):
        raise PlanCompileError(f"fixture task {task_id} requires fixture_name, fixture, or action")
    if task_type == "oracle" and not (task.get("oracle_type") or task.get("oracle") or task.get("verification")):
        raise PlanCompileError(f"oracle task {task_id} requires oracle_type, oracle, or verification")
    if task_type == "cleanup" and not (task.get("object_ref") or task.get("cleanup") or task.get("action")):
        raise PlanCompileError(f"cleanup task {task_id} requires object_ref, cleanup, or action")
    task["evidence_kind"] = {
        "probe": "gate",
        "fixture": "fixture",
        "business": "business_execution",
        "oracle": "oracle_verification",
        "cleanup": "cleanup",
        "reduce": "reduction",
    }[task_type]
    task["execution"] = "parallel" if _safe_parallel(task) else "serial"
    return task


def compile_plan(intent: dict[str, Any]) -> dict[str, Any]:
    """Compile a validated intent without touching the filesystem."""
    if not isinstance(intent, dict):
        raise PlanCompileError("TestIntent must be an object")
    requirement_id = intent.get("requirement_id")
    if not isinstance(requirement_id, str) or not requirement_id.strip():
        raise PlanCompileError("requirement_id is required")
    acceptance = intent.get("acceptance_criteria") or intent.get("acceptance_items")
    if not isinstance(acceptance, list) or not acceptance:
        raise PlanCompileError("acceptance_criteria must be a non-empty list")
    mode = str(intent.get("mode", "EXECUTE")).upper()
    if mode not in {"PROBE", "EXECUTE"}:
        raise PlanCompileError("intent mode must be PROBE or EXECUTE")
    raw_tasks = intent.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanCompileError("tasks must be a non-empty list")
    _validate_semantic_decisions(intent, raw_tasks)
    raw_ids = [item.get("task_id") or item.get("case_id") if isinstance(item, dict) else None for item in raw_tasks]
    if any(not item for item in raw_ids) or len(raw_ids) != len(set(raw_ids)):
        raise PlanCompileError("task IDs must be non-empty and unique")
    known_ids = set(raw_ids)
    tasks = [_validate_task(item, mode, known_ids) for item in raw_tasks]
    levels = _topological_levels(tasks)
    task_by_id = {task["task_id"]: task for task in tasks}
    slices: list[dict[str, Any]] = []
    for index, level in enumerate(levels, 1):
        ordered = [task_by_id[task_id] for task_id in level]
        parallel = len(ordered) > 1 and all(task["execution"] == "parallel" for task in ordered)
        slice_type = "chain-core" if any(task["type"] == "business" and task["execution"] == "serial" for task in ordered) else ordered[0]["type"]
        if all(task["type"] == "reduce" for task in ordered):
            slice_type = "reduce"
        slices.append({
            "slice_id": f"S{index}",
            "type": slice_type,
            "task_ids": level,
            "execution": "parallel" if parallel else "serial",
            "approval": {"required": any(task["approval"]["required"] for task in ordered), "task_ids": [task["task_id"] for task in ordered if task["approval"]["required"]]},
            "resource_locks": sorted({lock for task in ordered for lock in task["resource_locks"]}),
            "volatile": any(task["volatile"] for task in ordered),
        })
    if not any(task["type"] == "reduce" for task in tasks):
        slices.append({"slice_id": f"S{len(slices) + 1}", "type": "reduce", "task_ids": [], "execution": "serial", "approval": {"required": False, "task_ids": []}, "resource_locks": [], "volatile": False})
    cleanup_ids = [task["task_id"] for task in tasks if task["type"] == "cleanup"]
    residual_ids = [task["task_id"] for task in tasks if task["cleanup_required"] and task["type"] != "cleanup"]
    known_gaps = copy.deepcopy(intent.get("known_gaps") or [])
    plan = {
        "schema_version": "2.0",
        "plan_id": intent.get("plan_id") or f"PLAN-{requirement_id}",
        "requirement_id": requirement_id,
        "mode": mode,
        "acceptance_criteria": copy.deepcopy(acceptance),
        "source_fingerprints": copy.deepcopy(intent.get("source_fingerprints") or {}),
        "risk_policy": copy.deepcopy(intent.get("risk_policy") or {}),
        "tasks": tasks,
        "slices": slices,
        "cleanup_obligations": {
            # ``task_ids`` lists the cleanup tasks themselves; ``residual_task_ids``
            # lists writers that leave objects behind. A writer passing is not proof
            # that its object was cleaned, so the two must stay separate.
            "task_ids": sorted(set(cleanup_ids)),
            "residual_task_ids": sorted(set(residual_ids)),
            "required": bool(cleanup_ids or residual_ids),
        },
        "known_gaps": known_gaps,
    }
    plan["plan_fingerprint"] = _fingerprint(plan)
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile TestIntent into an immutable execution plan")
    parser.add_argument("intent")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    intent = json.loads(Path(args.intent).read_text(encoding="utf-8"))
    plan = compile_plan(intent)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"plan": str(output), "plan_fingerprint": plan["plan_fingerprint"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
