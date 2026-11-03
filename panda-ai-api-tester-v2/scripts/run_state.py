#!/usr/bin/env python3
"""Stage-one file state machine helpers for v2 runs."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def calculate_plan_fingerprint(plan: dict[str, Any]) -> str:
    payload = dict(plan)
    payload.pop("plan_fingerprint", None)
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def calculate_run_fingerprint(run: dict[str, Any]) -> str:
    payload = dict(run)
    payload.pop("run_fingerprint", None)
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def validate_run_integrity(run_dir: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(run_dir).resolve()
    try:
        run = json.loads((root / "run.json").read_text(encoding="utf-8"))
        plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError("run or plan is invalid") from exc
    if not isinstance(run, dict) or not isinstance(plan, dict):
        raise StateError("run.json and plan.json must contain objects")
    actual_plan = calculate_plan_fingerprint(plan)
    if plan.get("plan_fingerprint") and (plan.get("plan_fingerprint") != actual_plan or run.get("plan_fingerprint") != actual_plan):
        raise StateError("plan fingerprint mismatch")
    if not plan.get("plan_fingerprint") and run.get("plan_fingerprint"):
        raise StateError("plan fingerprint is missing")
    declared_run = run.get("run_fingerprint")
    if declared_run and declared_run != calculate_run_fingerprint(run):
        raise StateError("run fingerprint mismatch")
    if declared_run and run.get("run_id") != plan.get("run_id", run.get("run_id")):
        raise StateError("run and plan run_id mismatch")
    if declared_run and run.get("plan_id") and run.get("plan_id") != plan.get("plan_id"):
        raise StateError("run and plan plan_id mismatch")
    return run, plan


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def create_run(run_dir: str | Path, *, run_id: str, plan_id: str, plan_fingerprint: str, environment_snapshot: dict[str, Any], plan: dict[str, Any] | None = None, entry_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    if root.exists() and any(root.iterdir()):
        raise StateError(f"run directory must be new and empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    head = {"schema_version": "2.0", "run_id": run_id, "plan_id": plan_id, "plan_fingerprint": plan_fingerprint, "environment_snapshot": environment_snapshot, "started_at": now_iso(), "status": "compiled"}
    head["run_fingerprint"] = calculate_run_fingerprint(head)
    _write_atomic(root / "run.json", head)
    if plan is not None:
        plan = dict(plan)
        plan["run_id"] = run_id
        if calculate_plan_fingerprint(plan) != plan_fingerprint:
            plan.pop("run_id", None)
        _write_atomic(root / "plan.json", plan)
    metadata = dict(entry_metadata or {})
    if not metadata:
        entry_state = {"schema_version": "2.0", "run_id": run_id, "status": "ready", "completed_slices": [], "volatile_replayed_slices": [], "next_slice_id": "S1", "pending_cleanup": [], "non_sensitive_variables": {}, "updated_at": now_iso()}
        _write_atomic(root / "state.json", entry_state)
    else:
        entry_type = metadata.get("entry_type", "single_api_quality")
        review = metadata.get("review") if isinstance(metadata.get("review"), dict) else {"status": "pending_review", "approved_case_ids": []}
        review_status = review.get("status", "pending_review")
        approved_case_ids = review.get("approved_case_ids") or review.get("approved_scope") or []
        if review_status == "approved" and not approved_case_ids:
            approved_case_ids = [item.get("case_id") for item in plan.get("tasks", []) if isinstance(item, dict) and item.get("case_id")]
        executable = review_status in {"approved", "partially_approved"}
        initial_status = "ready" if executable else "awaiting_approval" if review_status == "pending_review" else "blocked"
        initial_phase = "executing" if executable else "pending_review"
        entry_state = {
        "schema_version": "2.1",
        "run_id": run_id,
        "entry_type": entry_type,
        "phase": initial_phase,
        "status": initial_status,
        "review_status": review_status,
        "review_id": review.get("review_id"),
        "approved_case_ids": list(approved_case_ids),
        "design_fingerprint": review.get("design_fingerprint"),
        "plan_fingerprint": review.get("plan_fingerprint") or plan_fingerprint,
        "config_ref": metadata.get("config_ref", {}),
        "profile_ref": metadata.get("profile_ref", {}),
        "blocking_gap_ids": metadata.get("blocking_gap_ids", []),
        "last_action": "create_run",
        "next_action": "execute_slice" if executable else "request_approval" if review_status == "pending_review" else "review_not_executable",
        "request_count_expected": metadata.get("request_count_expected", 0),
        "request_count_sent": 0,
        "failure_class": None,
        "verdict": None,
        "completed_slices": [],
        "volatile_replayed_slices": [],
        "next_slice_id": "S1",
        "pending_cleanup": [],
        "non_sensitive_variables": {},
        "review": review,
        "updated_at": now_iso(),
    }
    for key in ("requirement_id", "interface_id", "environment_id", "scope", "source_refs"):
        if key in metadata:
            entry_state[key] = metadata[key]
    _write_atomic(root / "state.json", entry_state)
    _write_atomic(root / "resources.json", {"schema_version": "2.0", "run_id": run_id, "objects": []})
    (root / "events.jsonl").touch(exist_ok=False)
    return head


def load_state(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    path = root / "state.json"
    if not path.is_file():
        raise StateError("state.json is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError("state.json is invalid") from exc
    if not isinstance(value, dict):
        raise StateError("state.json must contain an object")
    return value


def update_state(run_dir: str | Path, updates: dict[str, Any]) -> dict[str, Any]:
    current = load_state(run_dir)
    current.update(updates)
    current["updated_at"] = now_iso()
    _write_atomic(Path(run_dir).resolve() / "state.json", current)
    return current


def load_resources(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    path = root / "resources.json"
    if not path.is_file():
        raise StateError("resources.json is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError("resources.json is invalid") from exc
    if not isinstance(value, dict):
        raise StateError("resources.json must contain an object")
    value.setdefault("objects", [])
    if not isinstance(value["objects"], list):
        raise StateError("resources.json.objects must be a list")
    return value


def update_resources(run_dir: str | Path, objects: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge non-sensitive resource facts into the durable resource registry."""
    current = load_resources(run_dir)
    by_id = {item.get("object_id"): item for item in current.get("objects", []) if isinstance(item, dict) and item.get("object_id")}
    for item in objects:
        if not isinstance(item, dict) or not item.get("object_id"):
            raise StateError("resource facts require object_id")
        object_id = str(item["object_id"])
        previous = dict(by_id.get(object_id) or {})
        previous.update({key: value for key, value in item.items() if key not in {"secret", "token", "password", "credential"}})
        by_id[object_id] = previous
    current["objects"] = [by_id[key] for key in sorted(by_id)]
    current["updated_at"] = now_iso()
    _write_atomic(Path(run_dir).resolve() / "resources.json", current)
    return current


def append_event(run_dir: str | Path, event: dict[str, Any]) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    path = root / "events.jsonl"
    if not path.is_file():
        raise StateError("events.jsonl is missing")
    previous = ""
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines:
        previous = json.loads(lines[-1]).get("event_digest", "")
    payload = dict(event)
    payload.setdefault("event_id", f"EV-{uuid.uuid4().hex}")
    payload.setdefault("occurred_at", now_iso())
    payload["prev_event_digest"] = previous
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["event_digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return payload
