#!/usr/bin/env python3
"""Trusted in-process host for RuntimeContext 2.0 API test execution.

The caller supplies runtime values as an in-memory mapping. This module never
loads them from process environment, argv, stdin, files, or serialized payloads.
Only binding names, non-secret metadata, stage timing, and artifact paths may be
persisted in an optional host outcome file.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

from run_pipeline import PipelineBlocked, run_pipeline
from runtime_context import RuntimeContextError, build_runtime_context, package_execution_preflight


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _duration_ms(started):
    return round((time.perf_counter() - started) * 1000, 3)


def _write_json_atomic(path, payload):
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def execute_test_package(
    package_path,
    run_dir,
    report_path,
    *,
    runtime_values,
    context_id=None,
    outcome_path=None,
):
    """Execute one package through the trusted in-process host.

    runtime_values must be supplied directly by the trusted caller and remains
    owned by that caller. It is never serialized. The returned/persisted outcome
    contains binding names only, never runtime values.
    """
    if runtime_values is None:
        raise ValueError("runtime_values must be an explicit in-memory mapping")
    if not isinstance(runtime_values, dict):
        runtime_values = dict(runtime_values)

    host_started = time.perf_counter()
    started_at = _now_iso()
    resolved_context_id = context_id or f"ctx-{uuid.uuid4().hex[:12]}"
    stage = "load_package"
    stages = {}
    context_metadata = None
    artifacts = {}

    try:
        stage_started = time.perf_counter()
        package_file = Path(package_path).resolve()
        package = json.loads(package_file.read_text(encoding="utf-8"))
        stages[stage] = {"status": "completed", "duration_ms": _duration_ms(stage_started)}

        stage = "package_execution_preflight"
        stage_started = time.perf_counter()
        execution_errors = package_execution_preflight(package)
        if execution_errors:
            raise PipelineBlocked("package execution preflight blocked: " + "; ".join(execution_errors))
        stages[stage] = {"status": "completed", "duration_ms": _duration_ms(stage_started)}

        stage = "runtime_preflight"
        stage_started = time.perf_counter()
        context = build_runtime_context(
            package,
            runtime_values,
            context_id=resolved_context_id,
        )
        context_metadata = context.metadata()
        stages[stage] = {"status": "completed", "duration_ms": _duration_ms(stage_started)}

        stage = "pipeline"
        stage_started = time.perf_counter()
        pipeline_result = run_pipeline(
            package_file,
            run_dir,
            report_path,
            runtime_context=context,
        )
        stages[stage] = {
            "status": "completed",
            "duration_ms": _duration_ms(stage_started),
            "details": pipeline_result.get("timing", {}),
        }
        artifacts = {
            "result": pipeline_result.get("result"),
            "report": pipeline_result.get("report"),
        }
        outcome = {
            "schema_version": "1.0",
            "status": "completed",
            "stopped_at_stage": None,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "duration_ms": _duration_ms(host_started),
            "stages": stages,
            "runtime_context": context_metadata,
            "artifacts": artifacts,
        }
    except (RuntimeContextError, PipelineBlocked) as exc:
        stages[stage] = {"status": "blocked", "duration_ms": _duration_ms(stage_started)}
        outcome = {
            "schema_version": "1.0",
            "status": "blocked",
            "stopped_at_stage": stage,
            "reason": str(exc),
            "started_at": started_at,
            "finished_at": _now_iso(),
            "duration_ms": _duration_ms(host_started),
            "stages": stages,
            "runtime_context": context_metadata or {
                "schema_version": "2.0",
                "context_id": resolved_context_id,
                "provided_bindings": sorted(runtime_values),
                "values_persisted": False,
                "source": "host",
            },
            "artifacts": artifacts,
        }
    except Exception as exc:
        stages[stage] = {"status": "error", "duration_ms": _duration_ms(stage_started)}
        outcome = {
            "schema_version": "1.0",
            "status": "error",
            "stopped_at_stage": stage,
            "reason": f"{type(exc).__name__}: {exc}",
            "started_at": started_at,
            "finished_at": _now_iso(),
            "duration_ms": _duration_ms(host_started),
            "stages": stages,
            "runtime_context": context_metadata or {
                "schema_version": "2.0",
                "context_id": resolved_context_id,
                "provided_bindings": sorted(runtime_values),
                "values_persisted": False,
                "source": "host",
            },
            "artifacts": artifacts,
        }

    if outcome_path is not None:
        _write_json_atomic(outcome_path, outcome)
        outcome["host_outcome"] = str(Path(outcome_path).resolve())
    return outcome
