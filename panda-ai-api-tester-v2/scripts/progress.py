#!/usr/bin/env python3
"""Runtime progress snapshots for API test runs.

Only identifiers, titles, counters and timing are persisted. Values are written
atomically so readers never observe a partial JSON document.
"""
import json
import os
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path


STATUSES = {
    "initializing", "gate_running", "fixture_running", "case_running", "waiting",
    "retrying", "cleanup_running", "validating", "reporting", "completed",
    "blocked", "error", "cancelled",
}


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def eta_range(completed_durations, remaining_cases, fixed_wait=(0, 0), cleanup=(0, 0)):
    if len(completed_durations) < 3:
        return None
    samples = [max(0.0, float(value)) for value in completed_durations]
    p50 = statistics.median(samples)
    ordered = sorted(samples)
    p90 = ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * 0.9)))]
    return {
        "min_seconds": int(round(remaining_cases * p50 + fixed_wait[0] + cleanup[0])),
        "max_seconds": int(round(remaining_cases * p90 + fixed_wait[1] + cleanup[1])),
        "confidence": "medium" if len(samples) >= 5 else "low",
    }


class ProgressWriter:
    def __init__(self, run_dir, run_id, total_cases, plan_id=None):
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "progress.json"
        self.started_at = datetime.now(timezone.utc)
        self.run_id = run_id
        self.plan_id = plan_id
        self.total_cases = int(total_cases)
        self.case_durations = []
        self.data = {
            "schema_version": "1.0",
            "run_id": run_id,
            "plan_id": plan_id,
            "status": "initializing",
            "phase": "initializing",
            "phase_label": "初始化运行",
            "completed": 0,
            "total": self.total_cases,
            "completed_cases": 0,
            "passed_cases": 0,
            "failed_cases": 0,
            "error_cases": 0,
            "not_run_cases": 0,
            "blocked_cases": 0,
            "current_case_id": None,
            "current_case_title": None,
            "elapsed_seconds": 0,
            "estimated_remaining_min_seconds": None,
            "estimated_remaining_max_seconds": None,
            "eta_confidence": "none",
            "waiting_reason": None,
            "last_updated_at": now_iso(),
        }
        self.write()

    def write(self):
        self.data["last_updated_at"] = now_iso()
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".progress-", suffix=".json", dir=self.run_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.replace(temporary, self.path)
            except PermissionError:
                # Windows can briefly reject replacement of a just-created target.
                # Remove only this run-local progress snapshot and retry once.
                if self.path.exists():
                    self.path.unlink()
                os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def update(self, status, phase, phase_label, **fields):
        if status not in STATUSES:
            raise ValueError(f"invalid progress status: {status}")
        self.data.update({"status": status, "phase": phase, "phase_label": phase_label})
        allowed = {
            "completed", "total", "completed_cases", "passed_cases", "failed_cases",
            "error_cases", "not_run_cases", "blocked_cases", "current_case_id",
            "current_case_title", "waiting_reason",
        }
        self.data.update({key: value for key, value in fields.items() if key in allowed})
        self.data["elapsed_seconds"] = round((datetime.now(timezone.utc) - self.started_at).total_seconds(), 1)
        remaining = max(0, self.total_cases - int(self.data.get("completed_cases", 0)))
        estimate = eta_range(self.case_durations, remaining)
        if estimate:
            self.data["estimated_remaining_min_seconds"] = estimate["min_seconds"]
            self.data["estimated_remaining_max_seconds"] = estimate["max_seconds"]
            self.data["eta_confidence"] = estimate["confidence"]
        else:
            self.data["estimated_remaining_min_seconds"] = None
            self.data["estimated_remaining_max_seconds"] = None
            self.data["eta_confidence"] = "none"
        self.write()

    def record_case(self, status, duration_seconds, case_id, title):
        self.case_durations.append(max(0.0, float(duration_seconds)))
        counters = {
            "passed_cases": self.data.get("passed_cases", 0),
            "failed_cases": self.data.get("failed_cases", 0),
            "error_cases": self.data.get("error_cases", 0),
            "not_run_cases": self.data.get("not_run_cases", 0),
        }
        key = f"{status}_cases"
        if key in counters:
            counters[key] += 1
        completed = int(self.data.get("completed_cases", 0)) + 1
        self.update(
            "case_running",
            "execute_cases",
            "业务用例执行",
            completed=completed,
            completed_cases=completed,
            current_case_id=case_id,
            current_case_title=title,
            **counters,
        )
