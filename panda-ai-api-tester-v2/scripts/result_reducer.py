#!/usr/bin/env python3
"""Deterministic stage-one result reducer for v2 runs.

The reducer consumes only a plan, append-only facts and resource state. It never
trusts a caller-supplied verdict or accepted_gaps to turn a hard block into PASS.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from run_state import validate_run_integrity


class ReductionError(ValueError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReductionError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReductionError(f"JSON object required: {path}")
    return value


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events = []
    previous = ""
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReductionError(f"invalid event JSON at line {index}") from exc
        if not isinstance(event, dict):
            raise ReductionError(f"event must be object at line {index}")
        if event.get("prev_event_digest", "") != previous:
            raise ReductionError(f"event hash chain mismatch at line {index}")
        canonical = dict(event)
        declared = canonical.pop("event_digest", None)
        digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if declared != digest:
            raise ReductionError(f"event digest mismatch at line {index}")
        previous = digest
        events.append(event)
    return events


def _event_status(events: list[dict[str, Any]], task_id: str) -> str | None:
    terminal = [event for event in events if event.get("task_id") == task_id and event.get("type") in {"task_finished", "volatile_replay_task_finished"}]
    return terminal[-1].get("status") if terminal else None


def _event_evidence(events: list[dict[str, Any]], task_id: str) -> list[str]:
    return sorted({str(event.get("evidence_ref")) for event in events if event.get("task_id") == task_id and event.get("evidence_ref")})


def reduce_run(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    try:
        run_head, plan = validate_run_integrity(root)
    except Exception as exc:
        raise ReductionError(f"run integrity refused: {exc}") from exc
    events = load_events(root / "events.jsonl")
    resources = _load_json(root / "resources.json") if (root / "resources.json").is_file() else {"objects": []}
    tasks = plan.get("tasks") or []
    if not isinstance(tasks, list):
        raise ReductionError("plan.tasks must be a list")
    hard_gaps = [gap for gap in plan.get("known_gaps") or [] if isinstance(gap, dict) and gap.get("blocking") is True]
    task_results = []
    for task in tasks:
        task_id = task.get("task_id")
        if not task_id:
            raise ReductionError("every plan task requires task_id")
        status = _event_status(events, task_id) or "not_run"
        task_results.append({"task_id": task_id, "type": task.get("type"), "evidence_kind": task.get("evidence_kind"), "status": status, "evidence_refs": _event_evidence(events, task_id)})
    business = [item for item in task_results if item.get("type") == "business"]
    business_executed = [item for item in business if item.get("status") in {"passed", "failed", "error"}]
    business_evidence_refs = {
        ref for item in business for ref in item.get("evidence_refs") or []
    }
    evidence_refs = {ref for item in task_results for ref in item.get("evidence_refs") or []}
    business_evidence_closed = bool(business) and all(
        item.get("status") in {"passed", "failed", "error"}
        and bool(item.get("evidence_refs"))
        for item in business
    )
    resources_objects = [item for item in resources.get("objects") or [] if isinstance(item, dict)]
    pending = [item for item in resources_objects if item.get("cleanup_status") == "pending"]
    cleanup_tasks = [item for item in tasks if isinstance(item, dict) and item.get("type") == "cleanup"]
    cleanup_results = [item for item in task_results if item.get("type") == "cleanup"]
    cleanup_failed = any(item.get("status") in {"failed", "error", "blocked"} for item in cleanup_results)
    cleanup_task_incomplete = [
        item.get("task_id")
        for item in cleanup_tasks
        if _event_status(events, item.get("task_id")) != "passed"
    ]
    obligations = plan.get("cleanup_obligations") if isinstance(plan.get("cleanup_obligations"), dict) else {}
    residual_task_ids = [str(item) for item in obligations.get("residual_task_ids") or [] if item]
    if not residual_task_ids:
        # Plans compiled before residual_task_ids existed only expose cleanup_required
        # on the writer itself; fall back to those flags rather than trusting nothing.
        residual_task_ids = [
            str(item.get("task_id"))
            for item in tasks
            if isinstance(item, dict)
            and item.get("cleanup_required")
            and item.get("type") not in {"cleanup", "reduce"}
            and item.get("task_id")
        ]
    settled_statuses = {"cleaned", "verified_absent", "released", "not_created"}
    resource_owner = {
        str(event.get("resource_id")): str(event.get("task_id"))
        for event in events
        if event.get("resource_id") and event.get("task_id")
    }
    task_by_id = {item.get("task_id"): item for item in tasks if isinstance(item, dict)}

    def _residual_settled(task_id: str) -> bool:
        """A write obligation is settled only by resource facts or a passed cleanup task."""
        owned = [item for item in resources_objects if resource_owner.get(str(item.get("object_id"))) == task_id]
        if owned and all(str(item.get("cleanup_status")) in settled_statuses for item in owned):
            return True
        for cleanup_task in cleanup_tasks:
            if _event_status(events, cleanup_task.get("task_id")) != "passed":
                continue
            if task_id in {str(dep) for dep in cleanup_task.get("depends_on") or []}:
                return True
            locks = {str(lock) for lock in cleanup_task.get("resource_locks") or []}
            if locks and locks.intersection({str(lock) for lock in (task_by_id.get(task_id) or {}).get("resource_locks") or []}):
                return True
        return False

    unresolved_residuals = [task_id for task_id in residual_task_ids if not _residual_settled(task_id)]
    cleanup_unresolved = bool(unresolved_residuals or cleanup_task_incomplete)
    cleanup_required = bool(cleanup_tasks or residual_task_ids)
    oracle = [item for item in task_results if item.get("type") == "oracle"]
    required_oracle = [item for item in tasks if isinstance(item, dict) and item.get("type") == "oracle" and item.get("required", True) is not False]
    oracle_incomplete = bool(required_oracle) and not all(_event_status(events, item.get("task_id")) == "passed" for item in required_oracle)
    failed = any(item.get("status") in {"failed", "error"} for item in task_results)
    if hard_gaps:
        verdict = "blocked"
    elif pending or cleanup_failed or cleanup_task_incomplete:
        # A planned cleanup task that never ran, a failed cleanup, or a resource the
        # ledger still marks pending all mean the run did not finish its obligations.
        verdict = "blocked"
    elif not business_executed:
        verdict = "not_run"
    elif failed:
        verdict = "failed"
    elif cleanup_unresolved:
        # Nothing failed, but a write task left an object with no cleanup mechanism:
        # the result cannot be closed as PASS either.
        verdict = "inconclusive"
    elif len(business_executed) == len(business) and business and all(item.get("status") == "passed" for item in business_executed) and not oracle_incomplete and business_evidence_closed:
        verdict = "passed"
    else:
        verdict = "inconclusive"
    oracle_verified = bool(oracle) and all(item.get("status") == "passed" for item in oracle)
    cleanup_status = "blocked" if pending or cleanup_failed or cleanup_task_incomplete else "pending" if cleanup_unresolved else "complete"
    cleanup_complete = cleanup_status == "complete" and not cleanup_task_incomplete
    return {
        "schema_version": "2.0",
        "run_id": run_head.get("run_id"),
        "plan_id": plan.get("plan_id"),
        "verdict": verdict,
        "known_gaps": hard_gaps,
        "task_results": task_results,
        "evidence_closure": {"business_execution": bool(business_executed), "business_evidence_closed": business_evidence_closed, "oracle_required": bool(required_oracle), "oracle_verified": oracle_verified, "evidence_refs": sorted(evidence_refs), "business_evidence_refs": sorted(business_evidence_refs), "closed": verdict == "passed"},
        "cleanup": {
            "required": cleanup_required,
            "pending_objects": pending,
            "unresolved_obligations": unresolved_residuals,
            "incomplete_tasks": cleanup_task_incomplete,
            "status": cleanup_status,
            "tasks_complete": cleanup_complete,
        },
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = reduce_run(args.run_dir)
    output = Path(args.output) if args.output else Path(args.run_dir) / "result.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": str(output), "verdict": result["verdict"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
