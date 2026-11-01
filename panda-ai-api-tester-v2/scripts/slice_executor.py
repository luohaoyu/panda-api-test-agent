#!/usr/bin/env python3
"""Execute one precompiled slice and persist only facts and cursor state."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from run_state import append_event, load_state, load_resources, update_resources, update_state, validate_run_integrity
from result_reducer import load_events


class SliceExecutionError(RuntimeError):
    pass


class ApprovalRequired(SliceExecutionError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SliceExecutionError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise SliceExecutionError(f"JSON object required: {path}")
    return value


def _plan_slice(plan: dict[str, Any], slice_id: str) -> dict[str, Any]:
    for item in plan.get("slices") or []:
        if isinstance(item, dict) and item.get("slice_id") == slice_id:
            return item
    raise SliceExecutionError(f"unknown slice: {slice_id}")


def _task_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["task_id"]: item for item in plan.get("tasks") or [] if isinstance(item, dict) and item.get("task_id")}


def _passed_task_ids(run_dir: Path) -> set[str]:
    """Return task facts that are durably known to have passed."""
    return {
        str(event.get("task_id"))
        for event in load_events(run_dir / "events.jsonl")
        if event.get("type") in {"task_finished", "volatile_replay_task_finished"}
        and event.get("status") == "passed"
        and event.get("task_id")
    }


def _assert_dependencies_satisfied(run_dir: Path, tasks: list[dict[str, Any]]) -> None:
    passed = _passed_task_ids(run_dir)
    for task in tasks:
        missing = sorted(set(task.get("depends_on") or []) - passed)
        if missing:
            raise SliceExecutionError(
                f"task {task.get('task_id')} dependencies are not satisfied: {missing}"
            )


def _safe_variables(values: dict[str, Any], sensitive_names: set[str]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in values.items() if key not in sensitive_names and not key.startswith("secret_")}


def _sub_package_for_slice(package: dict[str, Any], tasks: list[dict[str, Any]], slice_type: str) -> dict[str, Any]:
    """Narrow a TestPackage to compiler-selected cases without re-scheduling."""
    selected_ids = {task.get("case_id") or task.get("case_ref") or task.get("task_id") for task in tasks}
    selected_cases = []
    for case in package.get("cases") or []:
        if case.get("case_id") in selected_ids:
            item = copy.deepcopy(case)
            item["depends_on"] = []
            selected_cases.append(item)
    if slice_type == "reduce":
        selected_cases = []
    if not selected_cases and slice_type not in {"probe", "oracle", "cleanup", "reduce"}:
        raise SliceExecutionError("slice has no matching TestPackage cases")
    result = copy.deepcopy(package)
    result["cases"] = selected_cases
    declared_probe_id = ((package.get("auth_probe") or {}).get("gate_id"))
    auth_probe_gates = [
        copy.deepcopy(gate)
        for gate in package.get("gates") or []
        if declared_probe_id and gate.get("gate_id") == declared_probe_id
    ]
    result["gates"] = copy.deepcopy(package.get("gates") or []) if slice_type == "probe" else auth_probe_gates
    result["fixtures"] = copy.deepcopy(package.get("fixtures") or []) if slice_type == "fixture" else []
    if selected_cases:
        requirement_ids = {ref for case in selected_cases for ref in case.get("requirement_refs") or []}
        rule_ids = {ref for case in selected_cases for ref in case.get("business_rule_refs") or []}
        scenario_ids = {ref for case in selected_cases for ref in case.get("scenario_refs") or []}
        result["requirements"] = [item for item in package.get("requirements") or [] if item.get("requirement_id") in requirement_ids]
        result["business_rules"] = [item for item in package.get("business_rules") or [] if item.get("rule_id") in rule_ids]
        result["scenarios"] = [item for item in package.get("scenarios") or [] if item.get("scenario_id") in scenario_ids]
        project = copy.deepcopy(result.get("project") or {})
        project["requirement_refs"] = sorted(requirement_ids)
        result["project"] = project
    return result


def execute_package_slice(run_dir: str | Path, slice_id: str, package: dict[str, Any], *, runtime_context: Any, approval_token: str | None = None, task_executor=None) -> dict[str, Any]:
    """Run one compiled slice through the appropriate trusted executor.

    Business/probe slices use the existing HTTP Runner. Fixture, oracle, cleanup
    and reduce slices must use the injected task executor so they cannot be
    accidentally interpreted as empty HTTP packages.
    """
    root = Path(run_dir).resolve()
    try:
        _, plan = validate_run_integrity(root)
    except Exception as exc:
        raise SliceExecutionError(f"run integrity refused: {exc}") from exc
    state = load_state(root)
    slice_spec = _plan_slice(plan, slice_id)
    completed = set(state.get("completed_slices") or [])
    if slice_id in completed:
        raise SliceExecutionError(f"slice already completed: {slice_id}")
    if (slice_spec.get("approval") or {}).get("required") and not approval_token and not (state.get("entry_type") and state.get("review_status") in {"approved", "partially_approved"}):
        return execute_slice(root, slice_id, runtime_context=runtime_context)
    task_by_id = _task_map(plan)
    task_ids = list(slice_spec.get("task_ids") or [])
    approved_ids = set(state.get("approved_case_ids") or [])
    if state.get("review_status") == "partially_approved":
        task_ids = [task_id for task_id in task_ids if task_id in approved_ids]
    tasks = [task_by_id[task_id] for task_id in task_ids if task_id in task_by_id]
    _assert_dependencies_satisfied(root, tasks)
    slice_type = str(slice_spec.get("type", "business"))
    if slice_type in {"fixture", "oracle", "cleanup", "reduce"} or any(task.get("type") in {"fixture", "oracle", "cleanup", "reduce"} for task in tasks):
        if task_executor is None:
            raise SliceExecutionError(f"{slice_type} slice requires a trusted task_executor")
        return execute_slice(root, slice_id, runtime_context=runtime_context, approval_token=approval_token, task_executor=task_executor)
    bounded_package = _sub_package_for_slice(package, tasks, slice_type)
    append_event(root, {"type": "slice_started", "slice_id": slice_id, "task_ids": [task.get("task_id") for task in tasks], "executor": "trusted_http_runner"})
    try:
        from runner import ApiTestRunner
        bounded_run_dir = root / "slice-results" / slice_id
        bounded_run_dir.mkdir(parents=True, exist_ok=True)
        result_path = ApiTestRunner(bounded_package, bounded_run_dir, runtime_context=runtime_context).run()
        result = _load_json(Path(result_path))
        facts = []
        for case in result.get("case_results") or []:
            event = {"type": "task_finished", "slice_id": slice_id, "task_id": case.get("case_id"), "status": case.get("status", "error")}
            refs = case.get("evidence_refs") or []
            if refs:
                event["evidence_ref"] = refs[0]
            append_event(root, event)
            facts.append({"task_id": case.get("case_id"), "status": case.get("status"), "evidence_refs": refs})
        verdict = result.get("verdict")
        slice_status = "passed" if verdict == "passed" else "failed" if verdict == "failed" else "blocked" if verdict == "blocked" else "inconclusive"
        append_event(root, {"type": "slice_finished", "slice_id": slice_id, "status": slice_status, "result_ref": str(result_path)})
        update_state(root, {"completed_slices": sorted(completed | {slice_id}), "next_slice_id": None, "status": "ready" if slice_status == "passed" else "blocked"})
        return {"status": slice_status, "slice_id": slice_id, "facts": facts, "result": str(result_path), "sensitive_variables_persisted": False}
    except Exception as exc:
        append_event(root, {"type": "slice_finished", "slice_id": slice_id, "status": "error", "reason": str(exc)})
        update_state(root, {"status": "interrupted", "next_slice_id": slice_id})
        raise


def execute_slice(run_dir: str | Path, slice_id: str, *, runtime_context: Any = None, approval_token: str | None = None, task_executor=None, produced_variables: dict[str, Any] | None = None, sensitive_variables: set[str] | None = None) -> dict[str, Any]:
    """Execute exactly one slice using an injected deterministic task executor."""
    root = Path(run_dir).resolve()
    try:
        _, plan = validate_run_integrity(root)
    except Exception as exc:
        raise SliceExecutionError(f"run integrity refused: {exc}") from exc
    state = load_state(root)
    slice_spec = _plan_slice(plan, slice_id)
    completed = set(state.get("completed_slices") or [])
    if slice_id in completed:
        raise SliceExecutionError(f"slice already completed: {slice_id}")
    if state.get("entry_type") and state.get("review_status") not in {"approved", "partially_approved"}:
        if state.get("review_status") == "pending_review":
            append_event(root, {"type": "slice_waiting_review", "slice_id": slice_id, "task_ids": slice_spec.get("task_ids") or []})
            update_state(root, {"status": "awaiting_approval", "phase": "pending_review", "next_action": "request_approval", "next_slice_id": slice_id})
            return {"status": "awaiting_approval", "slice_id": slice_id, "task_ids": slice_spec.get("task_ids") or []}
        raise SliceExecutionError(f"run review is not executable: {state.get('review_status')}")
    if state.get("status") in {"blocked", "reduced", "reported"}:
        raise SliceExecutionError(f"run is not executable in status {state.get('status')}")
    pending_cleanup = state.get("pending_cleanup") or []
    if pending_cleanup and slice_spec.get("type") not in {"cleanup", "reduce"}:
        raise SliceExecutionError("pending cleanup obligations must be handled before continuing")
    if (slice_spec.get("approval") or {}).get("required") and not approval_token and not (state.get("entry_type") and state.get("review_status") in {"approved", "partially_approved"}):
        append_event(root, {"type": "slice_waiting_approval", "slice_id": slice_id, "task_ids": slice_spec.get("task_ids") or []})
        update_state(root, {"status": "awaiting_approval", "next_slice_id": slice_id})
        return {"status": "awaiting_approval", "slice_id": slice_id, "task_ids": slice_spec.get("task_ids") or []}
    if (slice_spec.get("approval") or {}).get("required"):
        approval_digest = hashlib.sha256(str(approval_token).encode("utf-8")).hexdigest()
        append_event(root, {"type": "approval_presented", "slice_id": slice_id, "approval_id": f"APR-{approval_digest[:16]}", "approval_digest": "sha256:" + approval_digest})

    task_by_id = _task_map(plan)
    task_ids = list(slice_spec.get("task_ids") or [])
    approved_ids = set(state.get("approved_case_ids") or [])
    if state.get("review_status") == "partially_approved":
        excluded_ids = [task_id for task_id in task_ids if task_id not in approved_ids]
        for task_id in excluded_ids:
            append_event(root, {"type": "task_finished", "slice_id": slice_id, "task_id": task_id, "task_type": (task_by_id.get(task_id) or {}).get("type"), "status": "not_run", "reason": "not_approved"})
        task_ids = [task_id for task_id in task_ids if task_id in approved_ids]
    selected_tasks = [task_by_id[task_id] for task_id in task_ids if task_id in task_by_id]
    _assert_dependencies_satisfied(root, selected_tasks)
    append_event(root, {"type": "slice_started", "slice_id": slice_id, "task_ids": task_ids, "execution": slice_spec.get("execution")})
    facts = []
    try:
        for task_id in task_ids:
            task = task_by_id.get(task_id)
            if task is None:
                raise SliceExecutionError(f"slice references unknown task: {task_id}")
            append_event(root, {"type": "task_started", "slice_id": slice_id, "task_id": task_id, "task_type": task.get("type")})
            fact = {"status": "passed", "evidence_ref": None, "task_id": task_id} if task_executor is None else task_executor(task, runtime_context=runtime_context)
            if not isinstance(fact, dict):
                raise SliceExecutionError(f"task executor returned non-object for {task_id}")
            status = str(fact.get("status", "error"))
            event = {"type": "task_finished", "slice_id": slice_id, "task_id": task_id, "task_type": task.get("type"), "evidence_kind": task.get("evidence_kind"), "status": status}
            if fact.get("evidence_ref"):
                event["evidence_ref"] = fact["evidence_ref"]
            if fact.get("resource"):
                event["resource_id"] = fact["resource"].get("object_id") if isinstance(fact["resource"], dict) else fact["resource"]
            append_event(root, event)
            if isinstance(fact.get("resource"), dict):
                update_resources(root, [fact["resource"]])
            facts.append({"task_id": task_id, "task_type": task.get("type"), **copy.deepcopy(fact)})
            if status in {"failed", "error", "blocked"}:
                break
    except Exception as exc:
        append_event(root, {"type": "slice_finished", "slice_id": slice_id, "status": "error", "reason": str(exc)})
        update_state(root, {"status": "interrupted", "next_slice_id": slice_id})
        raise
    statuses = {item.get("status") for item in facts}
    slice_status = "error" if statuses & {"error"} else "failed" if statuses & {"failed", "blocked"} else "passed"
    append_event(root, {"type": "slice_finished", "slice_id": slice_id, "status": slice_status})
    updates: dict[str, Any] = {"completed_slices": sorted(completed | {slice_id}), "next_slice_id": None, "status": "ready" if slice_status == "passed" else "blocked", "phase": "reducing" if slice_status == "passed" and slice_spec.get("type") == "reduce" else "executing", "last_action": "execute_slice", "next_action": "reduce" if slice_status == "passed" and slice_spec.get("type") == "reduce" else "execute_slice"}
    if produced_variables:
        updates["non_sensitive_variables"] = _safe_variables(produced_variables, set(sensitive_variables or set()))
    update_state(root, updates)
    return {"status": slice_status, "slice_id": slice_id, "facts": facts, "sensitive_variables_persisted": False}


def replay_volatile_sources(run_dir: str | Path, *, task_executor=None, runtime_context: Any = None) -> dict[str, Any]:
    """Replay completed volatile source slices without persisting their secrets.

    A new conversation round cannot recover sensitive values from state.json. The
    caller must provide the trusted executor/runtime context again; this helper
    replays only the compiler-marked source slices and never marks them complete a
    second time or writes returned sensitive variables to disk.
    """
    root = Path(run_dir).resolve()
    recovery = validate_recovery(root)
    if recovery["action"] != "replay_volatile_source":
        return {"status": "not_required", "recovery": recovery, "sensitive_variables_persisted": False}
    plan = _load_json(root / "plan.json")
    task_by_id = _task_map(plan)
    replayed = []
    for slice_id in recovery.get("replay_slice_ids") or []:
        slice_spec = _plan_slice(plan, slice_id)
        append_event(root, {"type": "volatile_replay_started", "slice_id": slice_id, "task_ids": slice_spec.get("task_ids") or []})
        for task_id in slice_spec.get("task_ids") or []:
            task = task_by_id.get(task_id)
            if task is None:
                raise SliceExecutionError(f"volatile replay references unknown task: {task_id}")
            if task_executor is None:
                raise SliceExecutionError("volatile replay requires a trusted task_executor/runtime context")
            fact = task_executor(task, runtime_context=runtime_context, replay=True)
            if not isinstance(fact, dict):
                raise SliceExecutionError(f"volatile replay returned non-object for {task_id}")
            status = str(fact.get("status", "error"))
            event = {"type": "volatile_replay_task_finished", "slice_id": slice_id, "task_id": task_id, "status": status}
            if fact.get("evidence_ref"):
                event["evidence_ref"] = fact["evidence_ref"]
            append_event(root, event)
            replayed.append({"slice_id": slice_id, "task_id": task_id, "status": status})
            if status in {"failed", "error", "blocked"}:
                update_state(root, {"status": "blocked", "next_slice_id": slice_id})
                return {"status": "blocked", "replayed": replayed, "sensitive_variables_persisted": False}
        append_event(root, {"type": "volatile_replay_finished", "slice_id": slice_id, "status": "passed"})
    state = load_state(root)
    replayed_slice_ids = set(recovery.get("replay_slice_ids") or [])
    source_task_ids = {
        task_id
        for slice_id in replayed_slice_ids
        for task_id in (_plan_slice(plan, slice_id).get("task_ids") or [])
    }
    downstream_task_ids = set(source_task_ids)
    changed = True
    while changed:
        changed = False
        for task in plan.get("tasks") or []:
            task_id = task.get("task_id")
            if task_id not in downstream_task_ids and set(task.get("depends_on") or {}).intersection(downstream_task_ids):
                downstream_task_ids.add(task_id)
                changed = True
    invalidated_slice_ids = {
        item.get("slice_id")
        for item in plan.get("slices") or []
        if set(item.get("task_ids") or {}).intersection(downstream_task_ids)
    }
    for task_id in sorted(downstream_task_ids - source_task_ids):
        append_event(root, {"type": "task_invalidated", "task_id": task_id, "reason": "volatile_source_replayed"})
    completed_slices = set(state.get("completed_slices") or []) - invalidated_slice_ids
    replayed_ids = sorted(set(state.get("volatile_replayed_slices") or []) | replayed_slice_ids)
    next_slice = recovery.get("next_slice_id")
    for item in plan.get("slices") or []:
        if item.get("slice_id") in invalidated_slice_ids:
            next_slice = item.get("slice_id")
            break
    update_state(root, {"status": "ready", "next_slice_id": next_slice, "completed_slices": sorted(completed_slices), "volatile_replayed_slices": replayed_ids, "invalidated_slices": sorted(invalidated_slice_ids)})
    return {"status": "replayed", "replayed": replayed, "invalidated_slice_ids": sorted(invalidated_slice_ids), "sensitive_variables_persisted": False}


def validate_recovery(run_dir: str | Path) -> dict[str, Any]:
    """Validate the ledger before a new conversation round may resume."""
    root = Path(run_dir).resolve()
    try:
        events = load_events(root / "events.jsonl")
        state = load_state(root)
        resources = _load_json(root / "resources.json")
        plan = _load_json(root / "plan.json")
    except Exception as exc:
        raise SliceExecutionError(f"recovery refused: {exc}") from exc
    pending_resources = [item for item in resources.get("objects") or [] if isinstance(item, dict) and item.get("cleanup_status") == "pending"]
    pending_cleanup = list(state.get("pending_cleanup") or [])
    cleanup_ids = [item.get("task_id") for item in plan.get("tasks") or [] if isinstance(item, dict) and item.get("type") == "cleanup"]
    volatile_slices = [item.get("slice_id") for item in plan.get("slices") or [] if isinstance(item, dict) and item.get("volatile")]
    completed = set(state.get("completed_slices") or [])
    replayed = set(state.get("volatile_replayed_slices") or [])
    replay = sorted(completed.intersection(volatile_slices) - replayed)
    if pending_resources or pending_cleanup:
        action = "cleanup_first"
    elif replay:
        action = "replay_volatile_source"
    else:
        action = "resume_next_slice"
    return {"status": "recoverable", "event_count": len(events), "action": action, "pending_cleanup": pending_cleanup, "pending_resources": pending_resources, "cleanup_slice_ids": cleanup_ids, "replay_slice_ids": replay, "next_slice_id": state.get("next_slice_id"), "sensitive_variables_persisted": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute one precompiled v2 execution slice")
    parser.add_argument("run_dir")
    parser.add_argument("slice_id")
    parser.add_argument("--approval-token")
    args = parser.parse_args()
    result = execute_slice(args.run_dir, args.slice_id, approval_token=args.approval_token)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
