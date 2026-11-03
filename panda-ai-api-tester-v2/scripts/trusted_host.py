#!/usr/bin/env python3
"""Trusted host for RuntimeContext 2.0 API test execution.

The caller either supplies non-secret runtime values directly or selects the
single-use project-local runtime envelope for the current run. The envelope is
the only file-backed credential channel; arbitrary files, process environment,
argv and stdin remain forbidden. Persisted outcomes contain binding names only.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

from plan_fingerprint import calculate_plan_fingerprint
from run_pipeline import PipelineBlocked, run_pipeline
from runtime_context import RuntimeContextError, build_runtime_context, package_execution_preflight
from runtime_secret_bridge import LocalSecretBridgeError, consume_local_runtime_envelope


class RuntimeRequestOverrideError(ValueError):
    """Raised when a protected in-memory request override is invalid."""


_RUNTIME_VALUES_UNSET = object()


def apply_runtime_request_overrides(package, request_overrides):
    """Bind request data after package load, without persisting it.

    The package on disk contains only reviewed case structure and placeholders.
    The trusted host may supply the original user request in memory after review.
    Only the case request is replaced; the returned package is a deep copy and
    callers must not serialize the override values into host outcomes.
    """
    if request_overrides is None:
        return package
    if not isinstance(request_overrides, dict):
        raise RuntimeRequestOverrideError("request_overrides must be a case_id to request mapping")
    import copy
    bound = copy.deepcopy(package)
    cases = bound.get("cases")
    if not isinstance(cases, list):
        raise RuntimeRequestOverrideError("package cases must be a list")
    by_id = {case.get("case_id"): case for case in cases if isinstance(case, dict)}
    unknown = sorted(set(request_overrides) - set(by_id))
    if unknown:
        raise RuntimeRequestOverrideError("request_overrides contains unknown case IDs: " + ", ".join(unknown))
    for case_id, request in request_overrides.items():
        if not isinstance(request, dict):
            raise RuntimeRequestOverrideError(f"request override for {case_id} must be an object")
        method = request.get("method")
        path = request.get("path")
        if not isinstance(method, str) or not method.strip():
            raise RuntimeRequestOverrideError(f"request override for {case_id} requires method")
        if not isinstance(path, str) or not path.startswith("/"):
            raise RuntimeRequestOverrideError(f"request override for {case_id} requires an absolute path")
        # Sensitive headers are always injected by RuntimeContext/AuthInjector.
        headers = request.get("headers") or {}
        forbidden = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key", "x-auth-token"}
        if any(str(key).lower() in forbidden for key in headers):
            raise RuntimeRequestOverrideError(f"request override for {case_id} cannot contain inline sensitive headers")
        by_id[case_id]["_reviewed_request"] = copy.deepcopy(by_id[case_id].get("request") or {})
        by_id[case_id]["request"] = copy.deepcopy(request)
        by_id[case_id]["runtime_request_bound"] = True
    return bound


def request_override_metadata(request_overrides):
    """Return non-sensitive metadata suitable for a host outcome."""
    if not isinstance(request_overrides, dict):
        return {"bound_case_ids": [], "values_persisted": False}
    return {"bound_case_ids": sorted(request_overrides), "values_persisted": False}


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
    runtime_values=_RUNTIME_VALUES_UNSET,
    local_secret_run_id=None,
    context_id=None,
    outcome_path=None,
    request_overrides=None,
):
    """Execute one package through the trusted host.

    Non-secret runtime_values stay in memory. When local_secret_run_id is set,
    the host consumes the fixed project-local envelope for that run, deletes it,
    merges only its declared secret bindings and then constructs RuntimeContext.
    Returned and persisted outcomes contain binding names only.
    """
    if runtime_values is _RUNTIME_VALUES_UNSET:
        if local_secret_run_id is None:
            raise ValueError("runtime_values or local_secret_run_id is required")
        runtime_values = {}
    elif runtime_values is None:
        raise ValueError("runtime_values must be an explicit mapping when provided")
    elif not isinstance(runtime_values, dict):
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
        package = apply_runtime_request_overrides(package, request_overrides)
        if request_overrides:
            declared = (package.get("review") or {}).get("plan_fingerprint")
            actual = calculate_plan_fingerprint(package)
            if declared != actual:
                raise PipelineBlocked("review fingerprint does not match package after runtime request binding")
        stages[stage] = {
            "status": "completed",
            "duration_ms": _duration_ms(stage_started),
            "details": request_override_metadata(request_overrides),
        }

        stage = "package_execution_preflight"
        stage_started = time.perf_counter()
        execution_errors = package_execution_preflight(package)
        if execution_errors:
            raise PipelineBlocked("package execution preflight blocked: " + "; ".join(execution_errors))
        stages[stage] = {"status": "completed", "duration_ms": _duration_ms(stage_started)}

        stage = "local_secret_consume"
        stage_started = time.perf_counter()
        if local_secret_run_id is not None:
            secret_values = consume_local_runtime_envelope(
                package_file,
                package,
                local_secret_run_id,
            )
            collisions = sorted(set(runtime_values) & set(secret_values))
            if collisions:
                raise LocalSecretBridgeError(
                    "local runtime envelope bindings must not also be supplied in memory: " + ", ".join(collisions)
                )
            runtime_values.update(secret_values)
            stages[stage] = {
                "status": "completed",
                "duration_ms": _duration_ms(stage_started),
                "details": {
                    "run_id": local_secret_run_id,
                    "consumed_bindings": sorted(secret_values),
                    "values_persisted": False,
                },
            }
        else:
            stages[stage] = {
                "status": "skipped",
                "duration_ms": _duration_ms(stage_started),
                "details": {"values_persisted": False},
            }

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
            package_override=package,
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
    except (RuntimeContextError, PipelineBlocked, RuntimeRequestOverrideError, LocalSecretBridgeError) as exc:
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
