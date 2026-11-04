#!/usr/bin/env python3
"""Stage-three orchestration facade for the v2 API Test Agent.

The facade is intentionally not an HTTP runner. It owns the conversation-facing
state transitions: compile once, create one immutable Run, select the next
precompiled slice, and reduce facts into a requirement-facing projection.
Concrete execution remains delegated to the v2 L2 Slice Executor.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from typing import Any


class OrchestrationError(ValueError):
    pass


_THIS = Path(__file__).resolve()
_L2_SCRIPTS = _THIS.parents[2] / "panda-ai-api-tester-v2" / "scripts"
_ENTRY_STATE = _THIS.parent / "entry_state.py"


def _load(name: str):
    path = _L2_SCRIPTS / f"{name}.py"
    if not path.is_file():
        raise OrchestrationError(f"v2 dependency is missing: {path}")
    if str(_L2_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_L2_SCRIPTS))
    spec = importlib.util.spec_from_file_location(f"panda_v2_{name}", path)
    if spec is None or spec.loader is None:
        raise OrchestrationError(f"cannot load v2 dependency: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_local(name: str, path: Path):
    if not path.is_file():
        raise OrchestrationError(f"local dependency is missing: {path}")
    spec = importlib.util.spec_from_file_location(f"panda_agent_{name}", path)
    if spec is None or spec.loader is None:
        raise OrchestrationError(f"cannot load local dependency: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def _write_text_atomic(path: Path, content: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def diagnose_execution(
    design: dict[str, Any],
    *,
    execution_assets: dict[str, Any] | None = None,
    feasibility_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the local pre-execution self-diagnosis without changing state.

    The diagnosis is deliberately invoked before package compilation so a gate
    stop is explained immediately instead of surfacing as an opaque compiler
    exception. It never reads credentials or sends a request.
    """
    _load("package_compiler")
    diagnostics = _load_local("execution_diagnostics", _THIS.parent / "execution_diagnostics.py")
    return diagnostics.diagnose_design(
        design,
        execution_assets=execution_assets,
        feasibility_plan=feasibility_plan,
    )


def compile_approved_design_to_package(
    design: dict[str, Any],
    package_path: str | Path,
    *,
    execution_assets: dict[str, Any] | None = None,
    package_id: str | None = None,
    feasibility_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile an approved design with self-diagnosis and deterministic recovery.

    Safe local recovery currently includes building the feasibility snapshot
    when all declared dependencies are resolvable. Missing approval, fixtures,
    credentials, environment assets or risk confirmations remain fail-closed and
    are returned as structured diagnostics instead of being guessed.
    """
    compiler = _load("package_compiler")
    feasibility_was_built = feasibility_plan is None
    diagnostics = diagnose_execution(
        design,
        execution_assets=execution_assets,
        feasibility_plan=feasibility_plan,
    )
    if diagnostics["status"] == "blocked":
        raise OrchestrationError(diagnostics)

    if feasibility_plan is None:
        feasibility = _load("feasibility_plan")
        try:
            feasibility_plan = feasibility.build_feasibility_plan(design)
        except Exception as exc:
            raise OrchestrationError({
                **diagnostics,
                "status": "blocked",
                "execution_allowed": False,
                "issues": [
                    *(diagnostics.get("issues") or []),
                    {
                        "code": "FEASIBILITY_AUTO_BUILD_FAILED",
                        "category": "skill_integration",
                        "owner": "skill",
                        "severity": "high",
                        "recoverable": False,
                        "evidence": type(exc).__name__ + ": " + str(exc),
                        "impact": "cannot prove the approved cases are executable",
                        "next_action": "repair the design dependencies or the feasibility planner, then rerun diagnosis",
                    },
                ],
                "next_action": "repair_feasibility_inputs",
            }) from exc

    try:
        package = compiler.compile_approved_package(
            design,
            execution_assets=execution_assets,
            package_id=package_id,
            feasibility_plan=feasibility_plan,
        )
    except Exception as exc:
        raise OrchestrationError({
            **diagnostics,
            "status": "blocked",
            "execution_allowed": False,
            "issues": [
                *(diagnostics.get("issues") or []),
                {
                    "code": "PACKAGE_COMPILE_FAILED",
                    "category": "skill_integration",
                    "owner": "skill",
                    "severity": "high",
                    "recoverable": False,
                    "evidence": type(exc).__name__ + ": " + str(exc),
                    "impact": "no executable package was created",
                    "next_action": "inspect the structured diagnosis and repair only the reported skill or asset issue",
                },
            ],
            "next_action": "repair_package_inputs",
        }) from exc
    output = Path(package_path).resolve()
    _write_json_atomic(output, package)
    return {
        "package": package,
        "package_path": str(output),
        "review_reused": True,
        "semantic_review_required": False,
        "source_design_fingerprint": package["compilation"]["source_design_fingerprint"],
        "plan_fingerprint": package["review"]["plan_fingerprint"],
        "diagnosis": diagnostics,
        "auto_recovery": ["feasibility_plan_built"] if feasibility_was_built else [],
    }


def _adapter():
    return _load_local("review_adapter", _THIS.parent / "review_adapter.py")


def _canonical_review(review: Any, case_ids: list[str]) -> dict[str, Any]:
    return _adapter().canonical_review(review, case_ids)


_REVIEW_ERROR_HINTS = {
    "REVIEWED_FIELDS_MISSING": (
        "expected reviewed_fields to be a per-case map like "
        '{"NA-001": ["request", "assertions", "data_dependencies", "side_effects", "cleanup"], ...} '
        "covering every approved case id; see references/interaction-rules.md \u00a71.5"),
    "REVIEW_TIME_REQUIRED": (
        "expected an ISO8601 datetime with timezone under reviewed_at / approved_at / decided_at, "
        "e.g. \"2026-11-03T09:00:00+08:00\""),
    "FINGERPRINT_REQUIRED": (
        "expected the compiled plan_fingerprint from plan.json (field plan_fingerprint) "
        "so the approval binds to the exact reviewed design"),
}


def _format_review_errors(errors: list[dict[str, Any]]) -> str:
    """Render review validation errors as 'CODE: message (+ hint)' so callers can
    fix the review structurally instead of guessing what was missing."""
    parts = []
    for item in errors or []:
        code = item.get("code") if isinstance(item, dict) else None
        message = item.get("message") if isinstance(item, dict) else str(item)
        text = f"[{code}] {message}" if code else str(message)
        hint = _REVIEW_ERROR_HINTS.get(code or "")
        if hint:
            text += f" | hint: {hint}"
        parts.append(text)
    return " ; ".join(parts)


def _require_host_review_provenance(review: dict[str, Any]) -> None:
    """Require evidence that an executable review came from the host UI.

    ``reviewer=user`` is user-provided data and therefore cannot establish
    provenance. The host must attach both markers before an executable review
    can enter or change a Run.
    """
    if not isinstance(review, dict):
        raise OrchestrationError("REVIEW_SOURCE_REQUIRED: review must be a host interaction envelope")
    if review.get("review_source") != "host_user_interaction":
        raise OrchestrationError(
            "REVIEW_SOURCE_REQUIRED: executable review must be submitted by the host interaction layer "
            "with review_source=host_user_interaction"
        )
    interaction_id = review.get("interaction_id")
    if not isinstance(interaction_id, str) or not interaction_id.strip():
        raise OrchestrationError(
            "REVIEW_INTERACTION_REQUIRED: executable review must include a non-empty host interaction_id"
        )


def _validate_unified_review(review: dict[str, Any], plan: dict[str, Any], case_ids: list[str]) -> dict[str, Any]:
    """Fail-closed validation of an executable review against the compiled plan.

    Accepts every documented alias set (see ``review_adapter.canonical_review``)
    and returns the canonical review that must be persisted, so the documented
    contract and the stored contract cannot drift apart again.
    """
    canonical = _canonical_review(review, case_ids)
    structured_errors = list(canonical["errors"])
    if not canonical["execution_allowed"] and not structured_errors:
        structured_errors.append({
            "code": "REVIEW_NOT_EXECUTABLE",
            "message": f"review status {canonical['status']} is not executable",
        })
    declared = (canonical["review"] or {}).get("plan_fingerprint")
    if declared and declared != plan.get("plan_fingerprint"):
        structured_errors.append({
            "code": "REVIEW_FINGERPRINT_MISMATCH",
            "message": "review fingerprint does not match the compiled plan fingerprint; the design changed after approval",
        })
    if structured_errors:
        raise OrchestrationError(_format_review_errors(structured_errors))
    return canonical["review"]


def compile_and_create_run(intent: dict[str, Any], run_dir: str | Path, *, run_id: str | None = None, environment_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compile an intent and create a new file-backed Run.

    This is the only preparation entry point. Compilation is side-effect free;
    run creation is allowed only after compilation succeeds.
    """
    if not isinstance(intent, dict):
        raise OrchestrationError("intent must be an object")
    compiler = _load("plan_compiler")
    state = _load("run_state")
    entry_state_mod = _load_local("entry_state", _ENTRY_STATE)
    plan = compiler.compile_plan(intent)
    entry_type = intent.get("entry_type") or intent.get("intent_type") or "single_api_quality"
    review = intent.get("review") if isinstance(intent.get("review"), dict) else {"status": "pending_review", "approved_case_ids": []}
    case_ids = [item.get("case_id") or item.get("task_id") for item in (plan.get("candidate_cases") or plan.get("tasks", [])) if isinstance(item, dict) and (item.get("case_id") or item.get("task_id"))]
    task_case_ids = [item.get("case_id") or item.get("task_id") for item in plan.get("tasks", []) if isinstance(item, dict) and (item.get("case_id") or item.get("task_id"))]
    if set(case_ids) != set(task_case_ids):
        raise OrchestrationError("plan candidate_cases and tasks diverged; blocked candidates must remain in the execution plan as not_run")
    canonical = _canonical_review(review, case_ids)
    normalized_review = canonical["review"]
    if normalized_review.get("status") not in entry_state_mod.REVIEW_STATUSES:
        raise OrchestrationError("unsupported review status")
    if normalized_review.get("status") in entry_state_mod.EXECUTABLE_REVIEWS:
        _require_host_review_provenance(review)
        normalized_review = _validate_unified_review(review, plan, case_ids)
    elif canonical["errors"]:
        raise OrchestrationError("; ".join(item["message"] for item in canonical["errors"]))
    run_id = run_id or f"RUN-{uuid.uuid4().hex}"
    snapshot = dict(environment_snapshot or {})
    snapshot.setdefault("environment_id", intent.get("environment_id"))
    if not snapshot.get("environment_id"):
        raise OrchestrationError("environment_snapshot.environment_id is required")
    head = state.create_run(
        run_dir,
        run_id=run_id,
        plan_id=plan["plan_id"],
        plan_fingerprint=plan["plan_fingerprint"],
        environment_snapshot=snapshot,
        plan=plan,
        entry_metadata={
            "entry_type": entry_type,
            "review": normalized_review,
            "requirement_id": intent.get("requirement_id"),
            "interface_id": (intent.get("target") or {}).get("interface_id") if isinstance(intent.get("target"), dict) else intent.get("interface_id"),
            "environment_id": snapshot.get("environment_id"),
            "scope": intent.get("scope") or {},
            "source_refs": intent.get("source_refs") or [],
            "request_count_expected": intent.get("request_count_expected", 0),
            "execution_authorization": intent.get("execution_authorization") if isinstance(intent.get("execution_authorization"), dict) else None,
            "authorization_model": "independent",
        },
    )
    requested = normalized_review.get("execution_case_ids") if "execution_case_ids" in normalized_review else normalized_review.get("requested_case_ids", [])
    initial_action = (
        "request_plan_review"
        if normalized_review.get("status") == "pending_review"
        else "request_execution_approval"
        if normalized_review.get("status") in entry_state_mod.EXECUTABLE_REVIEWS and requested
        else "resolve_dependencies"
        if normalized_review.get("status") in entry_state_mod.EXECUTABLE_REVIEWS
        else "review_not_executable"
    )
    initial_next_action = {
        "action": initial_action,
        "slice_id": "S1" if initial_action == "execute_slice" else None,
    }
    if initial_action == "request_plan_review":
        initial_next_action.update({
            "approval_kind": "plan_review",
            "design_review_still_valid": False,
        })
    elif initial_action == "request_execution_approval":
        initial_next_action.update({
            "approval_kind": "execution_approval",
            "design_review_still_valid": True,
            "execution_case_ids": list(requested or []),
            "plan_fingerprint": plan.get("plan_fingerprint"),
        })
    state.append_event(run_dir, {
        "type": "review_recorded",
        "source": "create_run",
        "review_id": normalized_review.get("review_id"),
        "review_status": normalized_review.get("status"),
        "approved_case_ids": list(normalized_review.get("approved_case_ids") or []),
        "plan_fingerprint": plan.get("plan_fingerprint"),
        "approval_field_source": normalized_review.get("approval_field_source"),
        "fingerprint_source": normalized_review.get("fingerprint_source"),
    })
    return {"run": head, "plan": plan, "next_action": initial_next_action, "review": normalized_review}


def record_execution_authorization(run_dir: str | Path, authorization: dict[str, Any]) -> dict[str, Any]:
    """Persist an independent execution authorization; review never grants it."""
    root = Path(run_dir).resolve()
    state_mod = _load("run_state")
    plan = _read_json(root / "plan.json")
    state = state_mod.load_state(root)
    if not state.get("reviewed_case_ids") and not state.get("approved_case_ids"):
        raise OrchestrationError("PLAN_REVIEW_REQUIRED: execution authorization requires a recorded plan review")
    executable = list(state.get("executable_case_ids") or [])
    if not executable:
        raise OrchestrationError("EXECUTION_SCOPE_EMPTY: zero executable cases cannot request execution approval")
    authorization_context = copy.deepcopy(plan)
    authorization_context["feasibility"] = {
        "execution_case_ids": executable,
        "executable_case_ids": executable,
        "blocked_case_ids": list(state.get("blocked_case_ids") or []),
    }
    authorization_context["environment"] = {
        "environment_id": state.get("environment_id")
        or (_read_json(root / "run.json").get("environment_snapshot") or {}).get("environment_id")
    }
    authorization_context["review"] = copy.deepcopy(state.get("review") or {})
    authorization_context["review"]["plan_fingerprint"] = plan.get("plan_fingerprint")
    authorization_context["request_count_expected"] = int(state.get("request_count_expected") or len(executable))
    adapter = _adapter()
    normalized = adapter.normalize_execution_authorization(authorization, package=authorization_context)
    if normalized["errors"]:
        raise OrchestrationError("; ".join(normalized["errors"]))
    envelope = normalized["authorization"]
    state_mod.update_state(root, {
        "execution_authorization": envelope,
        "authorized_case_ids": list(envelope.get("authorized_case_ids") or []),
        "executable_case_ids": executable,
        "next_action": "execute_slice" if normalized["execution_allowed"] else "request_execution_approval",
        "status": "ready" if normalized["execution_allowed"] else "blocked",
        "phase": "executing" if normalized["execution_allowed"] else "pending_review",
        "last_action": "record_execution_authorization",
        "failure_class": None,
    })
    state_mod.append_event(root, {"type": "execution_authorization_recorded", "authorization_id": envelope.get("authorization_id"), "status": envelope.get("status"), "authorized_case_ids": envelope.get("authorized_case_ids") or [], "execution_scope_fingerprint": envelope.get("execution_scope_fingerprint")})
    return {"run_dir": str(root), "authorization": envelope, "next_action": "execute_slice" if normalized["execution_allowed"] else "request_execution_approval"}


def record_review(run_dir: str | Path, review: dict[str, Any]) -> dict[str, Any]:
    """Persist a host-originated human review decision onto an existing run.

    The caller must pass a review envelope produced by the host interaction
    layer.  A plain ``reviewer=user`` field is not sufficient evidence: the
    write path requires ``review_source=host_user_interaction`` and a non-empty
    ``interaction_id``.  This is intentionally the smallest fail-closed guard
    against the Agent manufacturing its own approval JSON.

    This is the missing half of the human review gate: a run compiled as
    ``pending_review`` can be approved, partially approved or rejected in place
    instead of forcing the caller to abandon the run and compile a second one.
    The run id, plan and its fingerprint stay untouched, so the approval and the
    execution it authorises live in the same auditable lineage.

    Fail-closed rules:
    - a reduced/reported run can never be re-opened;
    - an executable review must pass the same canonical validation and
      fingerprint binding as ``compile_and_create_run``;
    - an approval scope that no longer covers cases already executed is refused
      rather than silently narrowed.
    """
    root = Path(run_dir).resolve()
    state_mod = _load("run_state")
    plan_path = root / "plan.json"
    if not plan_path.is_file():
        raise OrchestrationError("plan.json is missing; the run was not compiled")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    state = state_mod.load_state(root)
    if state.get("status") in {"reduced", "reported"}:
        raise OrchestrationError("run already reduced; a further decision must start a new run instead of rewriting a closed one")

    case_ids = [item.get("case_id") or item.get("task_id") for item in (plan.get("candidate_cases") or plan.get("tasks") or []) if isinstance(item, dict) and (item.get("case_id") or item.get("task_id"))]
    task_case_ids = [item.get("case_id") or item.get("task_id") for item in plan.get("tasks") or [] if isinstance(item, dict) and (item.get("case_id") or item.get("task_id"))]
    if set(case_ids) != set(task_case_ids):
        raise OrchestrationError("run plan candidate_cases and tasks diverged; blocked candidates must remain as not_run")
    canonical = _canonical_review(review, case_ids)
    status = canonical["status"]
    if status in {"approved", "partially_approved"}:
        _require_host_review_provenance(review)
    if status in {"rejected", "plan_only", "expired", "superseded"}:
        state_mod.update_state(root, {
            "review_status": status,
            "review": canonical["review"],
            "status": "blocked",
            "phase": "pending_review",
            "last_action": "record_review",
            "next_action": "review_not_executable",
            "failure_class": "review_not_executable",
        })
        state_mod.append_event(root, {"type": "review_recorded", "source": "record_review", "review_status": status, "review_id": canonical["review"].get("review_id")})
        return {"run_dir": str(root), "review_status": status, "approved_case_ids": [], "next_action": "review_not_executable"}

    if status == "pending_review":
        state_mod.update_state(root, {
            "review_status": status,
            "review": canonical["review"],
            "status": "awaiting_approval",
            "phase": "pending_review",
            "last_action": "record_review",
            "next_action": "request_plan_review",
            "approved_case_ids": [],
        })
        state_mod.append_event(root, {"type": "review_recorded", "source": "record_review", "review_status": status, "review_id": canonical["review"].get("review_id")})
        return {"run_dir": str(root), "review_status": status, "approved_case_ids": [], "next_action": "request_plan_review"}

    if canonical["errors"]:
        raise OrchestrationError(_format_review_errors(canonical["errors"]))
    approved = _validate_unified_review(review, plan, case_ids)
    scope = list(approved.get("approved_case_ids") or [])

    completed = set(state.get("completed_slices") or [])
    if completed:
        executed_cases = {
            task_id
            for slice_spec in plan.get("slices") or []
            if isinstance(slice_spec, dict) and slice_spec.get("slice_id") in completed
            for task_id in slice_spec.get("task_ids") or []
        }
        task_to_case = {
            item.get("task_id"): item.get("case_id")
            for item in plan.get("tasks") or []
            if isinstance(item, dict) and item.get("task_id")
        }
        executed_case_ids = {task_to_case.get(task_id) for task_id in executed_cases} - {None}
        dropped = sorted(executed_case_ids - set(scope))
        if dropped:
            raise OrchestrationError("approval scope no longer covers already executed cases: " + ", ".join(dropped))

    if "execution_case_ids" in approved:
        requested = approved.get("execution_case_ids")
    elif "requested_case_ids" in approved:
        requested = approved.get("requested_case_ids")
    else:
        requested = []
    requested = list(dict.fromkeys(requested or []))
    unknown_requested = sorted(set(requested) - set(scope))
    if unknown_requested:
        raise OrchestrationError(
            "execution request contains cases outside reviewed scope: "
            + ", ".join(unknown_requested)
        )
    task_by_case = {
        item.get("case_id") or item.get("task_id"): item
        for item in plan.get("tasks") or []
        if isinstance(item, dict) and (item.get("case_id") or item.get("task_id"))
    }
    executable = []
    blocked = []
    for case_id in requested:
        task = task_by_case.get(case_id) or {}
        if task.get("planned_disposition") == "not_run":
            blocked.append(case_id)
        else:
            executable.append(case_id)
    next_step = "request_execution_approval" if executable else "resolve_dependencies"
    state_mod.update_state(root, {
        "review_status": approved.get("status"),
        "review": approved,
        "review_id": approved.get("review_id"),
        "approved_case_ids": scope,
        "reviewed_case_ids": scope,
        "requested_case_ids": requested,
        "execution_case_ids": requested,
        "executable_case_ids": executable,
        "blocked_case_ids": blocked,
        "authorized_case_ids": [],
        "execution_authorization": None,
        "plan_fingerprint": plan.get("plan_fingerprint"),
        "design_fingerprint": approved.get("design_fingerprint"),
        "phase": "pending_review",
        "status": "blocked",
        "last_action": "record_review",
        "next_action": next_step,
        "failure_class": None if requested else "no_execution_cases_requested",
    })
    state_mod.append_event(root, {
        "type": "review_recorded",
        "source": "record_review",
        "review_id": approved.get("review_id"),
        "review_status": approved.get("status"),
        "approved_case_ids": scope,
        "plan_fingerprint": plan.get("plan_fingerprint"),
        "approval_field_source": approved.get("approval_field_source"),
        "fingerprint_source": approved.get("fingerprint_source"),
    })
    return {
        "run_dir": str(root),
        "review_status": approved.get("status"),
        "approved_case_ids": scope,
        "next_action": next_step,
    }


def _cleanup_first_guidance(plan: dict[str, Any], recovery: dict[str, Any]) -> list[str]:
    """Explain, per pending object, which task is expected to settle it.

    A pending object is waiting on either a cleanup task that references its
    object_ref, or a business task with a matching settlement declaration. The
    guidance names both so an operator can see what is missing instead of just
    'cleanup_first'.
    """
    pending = recovery.get("pending_resources") or []
    if not pending:
        return []
    cleanup_by_obj: dict[str, list[str]] = {}
    settlement_by_obj: dict[str, list[str]] = {}
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        obj_ref = task.get("object_ref")
        if not isinstance(obj_ref, str):
            continue
        if task.get("type") == "cleanup":
            cleanup_by_obj.setdefault(obj_ref, []).append(str(task.get("task_id")))
        settlement = task.get("settlement") if isinstance(task.get("settlement"), dict) else None
        if settlement and settlement.get("on_success", True) and settlement.get("object_ref"):
            settlement_by_obj.setdefault(str(settlement["object_ref"]), []).append(str(task.get("task_id")))
    guidance = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        obj = str(item.get("object_id"))
        source = item.get("source_task") or "unknown"
        cleaners = cleanup_by_obj.get(obj) or cleanup_by_obj.get(f"net_address:{obj}") or []
        declarers = settlement_by_obj.get(obj) or []
        if cleaners:
            guidance.append(f"pending object {obj} (created by {source}) waits for cleanup task(s): {', '.join(cleaners)}")
        elif declarers:
            guidance.append(f"pending object {obj} (created by {source}) waits for settlement declaration on task(s): {', '.join(declarers)}")
        else:
            guidance.append(f"pending object {obj} (created by {source}) has no cleanup task and no settlement declaration; add a settlement to its writer or a cleanup task")
    return guidance


def next_action(run_dir: str | Path) -> dict[str, Any]:
    """Return one deterministic action for the next conversation round."""
    root = Path(run_dir).resolve()
    state_mod = _load("run_state")
    executor = _load("slice_executor")
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    state = state_mod.load_state(root)
    if state.get("status") in {"reported", "reduced"}:
        return {"action": "complete", "status": state.get("status")}
    if state.get("status") == "interrupted":
        return {"action": "recover", "recovery": executor.validate_recovery(root)}
    if state.get("review_status") in {"rejected", "plan_only", "expired", "superseded"}:
        return {"action": "review_not_executable", "reason": f"review status {state.get('review_status')} is not executable"}
    if state.get("review_status") == "pending_review" or state.get("status") == "awaiting_approval":
        return {
            "action": "request_plan_review",
            "approval_kind": "plan_review",
            "design_review_still_valid": False,
            "slice_id": state.get("next_slice_id"),
            "approved_case_ids": state.get("approved_case_ids") or [],
            "plan_fingerprint": plan.get("plan_fingerprint"),
        }
    requested = state.get("requested_case_ids") if "requested_case_ids" in state else state.get("execution_case_ids", [])
    executable = state.get("executable_case_ids") if "executable_case_ids" in state else requested
    authorized = state.get("authorized_case_ids") if "authorized_case_ids" in state else []
    if not requested:
        return {"action": "resolve_dependencies", "diagnostic_code": "NO_EXECUTION_CASES_REQUESTED"}
    if not executable:
        return {"action": "resolve_dependencies", "diagnostic_code": "NO_READY_EXECUTION_CASES", "requested_case_ids": requested}
    if not authorized:
        return {"action": "request_execution_approval", "approval_kind": "execution_approval", "execution_case_ids": executable, "plan_fingerprint": plan.get("plan_fingerprint")}
    recovery = executor.validate_recovery(root)
    if recovery["action"] == "cleanup_first":
        return {"action": "execute_cleanup_first", "slice_ids": recovery.get("cleanup_slice_ids") or [], "recovery": recovery,
                "guidance": _cleanup_first_guidance(plan, recovery)}
    if recovery["action"] == "replay_volatile_source":
        return {"action": "replay_volatile_source", "slice_ids": recovery.get("replay_slice_ids") or [], "recovery": recovery, "requires_runtime_context": True}
    if state.get("status") == "blocked":
        if state.get("failure_class") == "high_risk_approval_required":
            slice_id = state.get("next_slice_id")
            slice_spec = next((item for item in plan.get("slices") or [] if item.get("slice_id") == slice_id), {})
            approval = slice_spec.get("approval") or {}
            return {
                "action": "request_approval",
                "approval_kind": "high_risk_authorization",
                "design_review_still_valid": state.get("review_status") in {"approved", "partially_approved"},
                "slice_id": slice_id,
                "task_ids": slice_spec.get("task_ids") or [],
                "reasons": approval.get("reasons") or [],
                "plan_fingerprint": plan.get("plan_fingerprint"),
            }
        return {
            "action": "blocked",
            "failure_class": state.get("failure_class"),
            "design_review_still_valid": state.get("review_status") in {"approved", "partially_approved"},
            "reason": "run is blocked; resolve the recorded gate or failure before continuing",
        }
    completed = set(state.get("completed_slices") or [])
    for item in plan.get("slices") or []:
        if item.get("slice_id") not in completed:
            if item.get("approval", {}).get("required"):
                return {
                    "action": "request_approval",
                    "approval_kind": "high_risk_authorization",
                    "design_review_still_valid": True,
                    "slice_id": item["slice_id"],
                    "task_ids": item.get("task_ids") or [],
                    "reasons": (item.get("approval") or {}).get("reasons") or [],
                    "plan_fingerprint": plan.get("plan_fingerprint"),
                }
            return {"action": "execute_slice", "slice_id": item["slice_id"], "task_ids": item.get("task_ids") or [], "execution": item.get("execution")}
    return {"action": "reduce", "run_dir": str(root)}


def replay_volatile_run(run_dir: str | Path, *, task_executor, runtime_context: Any = None) -> dict[str, Any]:
    """Re-export volatile runtime values through the trusted executor only."""
    executor = _load("slice_executor")
    return executor.replay_volatile_sources(run_dir, task_executor=task_executor, runtime_context=runtime_context)


def reduce_run(run_dir: str | Path) -> dict[str, Any]:
    """Recompute the immutable run result and mark only the cursor state."""
    root = Path(run_dir).resolve()
    reducer = _load("result_reducer")
    state_mod = _load("run_state")
    if not (root / "result.json").exists():
        # First-time reduction only: every passed fact must carry schema-valid
        # evidence, otherwise the run cannot be closed into an immutable result
        # whose report claims evidence closure. Existing reduced runs are
        # immutable and are never re-validated here.
        evidence_schema = _load("evidence_schema")
        probe = _load("slice_executor")
        state = state_mod.load_state(root)
        plan = _read_json(root / "plan.json")
        executed = set()
        for slice_spec in plan.get("slices") or []:
            if isinstance(slice_spec, dict) and slice_spec.get("slice_id") in set(state.get("completed_slices") or []):
                executed.update(slice_spec.get("task_ids") or [])
        bad = []
        for event in probe.load_events(root / "events.jsonl") if (root / "events.jsonl").is_file() else []:
            if event.get("type") != "task_finished" or event.get("status") != "passed":
                continue
            ref = event.get("evidence_ref")
            if not ref:
                bad.append(f"{event.get('task_id')}: passed without evidence_ref")
                continue
            problems = evidence_schema.validate_evidence_file(root / ref)
            if problems:
                bad.extend(problems[:2])
        if bad:
            raise OrchestrationError(
                "reduce refused: evidence schema validation failed for " + str(len(bad))
                + " problem(s); first: " + bad[0])
    result = reducer.reduce_run(root)
    output = root / "result.json"
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if existing != result:
            raise OrchestrationError("result.json already exists with different content")
    else:
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state_mod.update_state(root, {"status": "reduced", "phase": "reported", "last_action": "reduce", "next_action": "complete", "verdict": result.get("verdict"), "next_slice_id": None})
    return result


def _task_matches_ac(task: dict[str, Any], ac_id: str) -> bool:
    refs = set(task.get("acceptance_refs") or []) | set(task.get("requirement_refs") or [])
    return ac_id in refs or task.get("ac_id") == ac_id


def _read_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise OrchestrationError(f"JSON object required: {path}")
    return value


_EXTERNAL_STATUSES = {"passed", "failed", "blocked", "missing"}


def _item_verdict(verifiability: str, statuses: set[str], evidence_sound: bool, external: dict[str, Any] | None = None) -> tuple[str, str, str]:
    """Map case facts plus external evidence to one acceptance item.

    A pass is never upgraded: ``partial`` needs both layers closed, ``none`` can
    only be closed by registered external evidence, and an external failure
    always wins so a contradictory record cannot hide behind an API pass.
    """
    fact = external if isinstance(external, dict) else {}
    external_status = fact.get("status") if fact.get("status") in _EXTERNAL_STATUSES else "missing"
    if verifiability == "none":
        if external_status == "passed":
            return "complete", "passed", "接口层不可验证；外部验证层证据闭合"
        if external_status == "failed":
            return "complete", "failed", "外部验证层记录失败"
        if external_status == "blocked":
            return "not_run", "blocked", "外部验证层被阻塞"
        return "not_run", "not_run", "接口层不可验证"
    if not statuses:
        return "not_run", "not_run", "未映射到接口用例"
    if statuses & {"failed", "error"}:
        return "complete", "failed", "接口层执行失败"
    executed = statuses & {"passed", "failed", "error"}
    if "blocked" in statuses and not executed:
        return "not_run", "blocked", "被门禁阻塞，未发送请求"
    if statuses == {"not_run"}:
        return "not_run", "not_run", "未执行"
    if statuses != {"passed"}:
        return "partial", "inconclusive", "仅部分用例执行"
    if verifiability == "partial":
        if not evidence_sound:
            return "partial", "inconclusive", "接口层通过但证据链未闭合"
        if external_status == "passed":
            return "complete", "passed", "接口层与外部验证层证据均已闭合"
        if external_status == "failed":
            return "complete", "failed", "接口层通过但外部验证层记录失败"
        if external_status == "blocked":
            return "partial", "blocked", "外部验证层被阻塞"
        return "partial", "inconclusive", "接口层通过，外部验证层证据缺失"
    if external_status == "failed":
        return "complete", "failed", "接口层通过但外部验证层记录失败（证据冲突）"
    if evidence_sound:
        return "complete", "passed", "接口层完整执行并通过"
    return "complete", "inconclusive", "接口层通过但证据链未闭合"


def _evidence_file_sound(refs: list[str], root: Path | None) -> tuple[bool, list[str]]:
    """Case-level evidence closure check.

    A case's evidence is sound when every ref resolves to a readable evidence
    file that passes the minimum viable raw-request schema. Run-level verdict is
    deliberately NOT consulted: a Run may be inconclusive because an unrelated
    candidate was planned not_run, which must not unclose THIS acceptance
    item's complete evidence (the r7 report defect).
    """
    if not refs:
        return False, ["no evidence refs"]
    if root is None:
        # Without a resolvable run root we cannot verify files; stay honest.
        return False, ["run root unavailable for evidence validation"]
    evidence_schema = _load("evidence_schema")
    problems: list[str] = []
    for ref in refs:
        problems.extend(evidence_schema.validate_evidence_file(root / ref))
    return not problems, problems


def _acceptance_items(
    criteria: list[dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    case_records: dict[str, dict[str, Any]],
    run_verdicts: dict[str, str],
    external_by_ac: dict[str, dict[str, Any]] | None = None,
    evidence_roots: dict[str, Path] | None = None,
) -> list[dict[str, Any]]:
    external_by_ac = external_by_ac or {}
    items = []
    for index, criterion in enumerate(criteria, 1):
        ac_id = criterion.get("ac_id") or criterion.get("id") or f"AC-{index}"
        ac_task_ids = [task_id for task_id, task in tasks.items() if _task_matches_ac(task, ac_id)]
        records = [case_records.get(task_id) or {"status": "not_run", "evidence_refs": []} for task_id in ac_task_ids]
        statuses = {record.get("status") for record in records}
        refs = sorted({ref for record in records for ref in record.get("evidence_refs") or []})
        source_runs = {record.get("source_run_id") for record in records if record.get("source_run_id")}
        evidence_roots = evidence_roots or {}
        per_case_sound = []
        evidence_problems: list[str] = []
        for record in records:
            refs = list(record.get("evidence_refs") or [])
            ok, problems = _evidence_file_sound(refs, evidence_roots.get(record.get("source_run_id") or ""))
            per_case_sound.append(ok)
            evidence_problems.extend(problems)
        evidence_sound = bool(records) and all(per_case_sound)
        verifiability = criterion.get("interface_verifiability", "direct")
        external = external_by_ac.get(ac_id) or {}
        external_status = external.get("status") if external.get("status") in _EXTERNAL_STATUSES else "missing"
        execution_status, verdict, actual = _item_verdict(
            verifiability,
            statuses if ac_task_ids else set(),
            evidence_sound,
            external,
        )
        gaps = list(criterion.get("gaps") or [])
        if verdict != "passed":
            gaps.append("证据或执行链尚未闭合")
        if not evidence_sound and evidence_problems:
            gaps.append("证据未通过最低原始请求 schema 校验：" + "; ".join(evidence_problems[:3]))
        evidence_refs: list[str] = []
        if verdict == "passed":
            evidence_refs = sorted(source_runs)
            if verifiability in {"none", "partial"}:
                # The report only accepts refs it can resolve to immutable evidence,
                # so an external closure cites registry ids, not free-text paths.
                evidence_refs = sorted(set(evidence_refs) | set(external.get("evidence_ids") or []))
        if external_status in {"missing", "blocked"} and verifiability in {"none", "partial"}:
            gaps.append("缺少外部验证层证据")
        items.append({
            "id": ac_id,
            "title": criterion.get("text") or criterion.get("title") or ac_id,
            "interface_verifiability": verifiability,
            "verifiability_reason": criterion.get("verifiability_reason") or "未声明验收项的接口可验证性",
            "interface_scope": criterion.get("interface_scope") or [],
            "non_interface_scope": criterion.get("non_interface_scope") or [],
            "recommended_validation_layers": criterion.get("recommended_validation_layers") or [],
            "execution_status": execution_status,
            "verdict": verdict,
            "actual": actual,
            "gaps": gaps,
            "evidence_refs": evidence_refs,
            "issue_refs": list(criterion.get("issue_refs") or []),
            "external_evidence": {
                "status": external_status,
                "refs": list(external.get("refs") or []),
                "layers": dict(external.get("layers") or {}),
            },
        })
    return items


_GAP_VERDICTS = {"not_run", "inconclusive", "blocked"}


def _gap_issues(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"GAP-{item['id']}",
            "type": "test_gap",
            "severity": "medium",
            "title": "接口层验收证据未闭合",
            "actual": item.get("actual") or "",
            "impact": "不能据此形成需求完整通过结论",
            "acceptance_refs": [item["id"]],
            "evidence_refs": list(item.get("evidence_refs") or []),
            "status": "open",
        }
        for item in items
        if item.get("verdict") in _GAP_VERDICTS
        and (item.get("interface_verifiability") != "direct" or item.get("verdict") == "blocked")
    ]


def _defect_issues(defects: list[dict[str, Any]], tasks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    issues = []
    for defect in defects:
        if not isinstance(defect, dict):
            continue
        case_id = defect.get("case_id")
        task = tasks.get(case_id) or {}
        classification = defect.get("classification") or "unclassified_observation"
        status = defect.get("status") or "open"
        issues.append({
            "id": str(defect.get("defect_id") or f"DEF-{case_id}"),
            "type": "defect" if classification == "product_defect" else "test_gap",
            "severity": defect.get("severity") or ("high" if classification == "product_defect" else "medium"),
            "title": defect.get("title") or f"用例 {case_id} 断言失败",
            "actual": defect.get("actual") or defect.get("observed") or "",
            "impact": defect.get("impact") or "验收结论不可视为通过",
            "acceptance_refs": list(task.get("acceptance_refs") or task.get("requirement_refs") or []),
            "evidence_refs": list(defect.get("evidence_refs") or []),
            "status": "open" if status in {"open", "reopened"} else status,
        })
    return issues


def _risk_issues(residual_risks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues = []
    for risk in residual_risks:
        if not isinstance(risk, dict):
            continue
        kind = risk.get("kind")
        case_id = risk.get("case_id") or risk.get("object_id") or "unknown"
        issues.append({
            "id": f"GAP-RISK-{case_id}-{kind}",
            "type": "test_gap",
            "severity": "medium",
            "title": "清理或残留风险未闭合",
            "actual": f"{kind}：{case_id}",
            "impact": "运行不得归约为完整通过",
            "acceptance_refs": [],
            "evidence_refs": [],
            "status": "open",
        })
    return issues


_RELEASE_RECOMMENDATION = {
    "FAIL": "阻断发布；修复失败项后补测",
    "BLOCKED": "阻断发布；先解除阻塞项",
    "PASS": "可进入下一阶段",
    "INCONCLUSIVE": "暂不可发布；补齐证据或未覆盖验收项",
}


def _overall_verdict(items: list[dict[str, Any]], issues: list[dict[str, Any]]) -> str:
    """Mirror the renderer's rule so the manifest cannot contradict its report."""
    open_blocker = any(
        issue.get("type") == "defect" and issue.get("severity") == "blocker" and issue.get("status") not in {"fixed", "closed"}
        for issue in issues
    )
    failed = any(item.get("execution_status") == "complete" and item.get("verdict") == "failed" for item in items)
    blocked = any(item.get("verdict") == "blocked" for item in items)
    all_passed = bool(items) and all(item.get("execution_status") == "complete" and item.get("verdict") == "passed" for item in items)
    if open_blocker or failed:
        return "FAIL"
    if blocked:
        return "BLOCKED"
    if all_passed:
        return "PASS"
    return "INCONCLUSIVE"


def _evidence_runs(roots: list[Path], results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    runs = []
    for root in roots:
        run = _read_json(root / "run.json")
        result = results.get(str(root)) or {}
        executed = any(
            isinstance(item, dict) and item.get("status") in {"passed", "failed", "error"}
            for item in result.get("task_results") or []
        )
        runs.append({
            "id": run.get("run_id"),
            "kind": "business_execution" if executed else "gate",
            "result_path": str(root / "result.json"),
            "summary": f"统一 Run {result.get('verdict', 'not_run')}",
            "immutable": True,
        })
    return runs


def build_requirement_manifest(run_dir: str | Path, *, title: str | None = None) -> dict[str, Any]:
    """Project one reduced Run into the deterministic report manifest model."""
    root = Path(run_dir).resolve()
    result = _read_json(root / "result.json") if (root / "result.json").is_file() else reduce_run(root)
    plan = _read_json(root / "plan.json")
    run = _read_json(root / "run.json")
    criteria = plan.get("acceptance_criteria") or []
    tasks = {item.get("task_id"): item for item in plan.get("tasks") or [] if isinstance(item, dict) and item.get("task_id")}
    run_id = run.get("run_id")
    case_records = {
        task_id: {"status": "not_run", "evidence_refs": [], "source_run_id": run_id}
        for task_id in tasks
    }
    for item in result.get("task_results") or []:
        if isinstance(item, dict) and item.get("task_id"):
            case_records[item["task_id"]] = {
                "status": item.get("status") or "not_run",
                "evidence_refs": list(item.get("evidence_refs") or []),
                "source_run_id": run_id,
            }
    items = _acceptance_items(criteria, tasks, case_records, {run_id: result.get("verdict")},
                              evidence_roots={run_id: root})
    issues = _gap_issues(items)
    verdict = _overall_verdict(items, issues)
    return {
        "schema_version": "1.0",
        "report_id": f"AR-{plan.get('requirement_id')}-{run_id}",
        "report_version": "1.0",
        "report_view_version": "1.0",
        "requirement": {"id": plan.get("requirement_id"), "title": title or plan.get("requirement_id"), "acceptance_total": len(items)},
        "baseline": {"environment": (run.get("environment_snapshot") or {}).get("environment_id")},
        "generated_at": _load("run_state").now_iso(),
        "acceptance_items": items,
        "evidence_runs": _evidence_runs([root], {str(root): result}),
        "issues": issues,
        "release_recommendation": _RELEASE_RECOMMENDATION[verdict],
        "generated_from": "v2-unified-run",
    }


def build_requirement_manifest_for_runs(
    run_dirs: Any,
    *,
    requirement_id: str | None = None,
    acceptance_criteria: list[dict[str, Any]] | None = None,
    title: str | None = None,
    external_evidence: dict[str, Any] | None = None,
    external_evidence_records: list[dict[str, Any]] | None = None,
    store_root: str | Path | None = None,
    previous_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project many Runs into one renderable ``current`` requirement manifest.

    ``requirement_aggregation`` owns the case-level/defect/residual-risk truth;
    this function only projects that truth into the report schema. Without it the
    documented "one current report per requirement" flow had no executable path,
    because the aggregation output (schema 1.1) is not what the renderer accepts.

    ``external_evidence_records`` (or ``store_root``, from which the append-only
    ledger is loaded) supplies the non-API acceptance facts that ``partial`` and
    ``none`` criteria need in order to close.
    """
    roots = [Path(item).resolve() for item in run_dirs]
    if not roots:
        raise OrchestrationError("at least one run directory is required")
    plans = {root: _read_json(root / "plan.json") for root in roots if (root / "plan.json").is_file()}
    if not plans:
        raise OrchestrationError("no compiled plan found in the given run directories")
    resolved_requirement = requirement_id or next(
        (plan.get("requirement_id") for plan in plans.values() if plan.get("requirement_id")), None)
    if not resolved_requirement:
        raise OrchestrationError("requirement_id is required to aggregate runs")

    evidence_mod = _load_local("external_evidence", _THIS.parent / "external_evidence.py")
    if external_evidence_records is not None:
        if store_root is None:
            raise OrchestrationError("store_root is required when external evidence records are supplied")
        records = list(external_evidence_records)
    elif store_root is not None:
        records = evidence_mod.load_external_evidence(store_root, resolved_requirement)
    else:
        records = []
    summary = evidence_mod.summarize_external_evidence(records) if records else None
    flat_evidence = None
    if summary:
        flat_evidence = {
            "status": summary["rollup_status"],
            "refs": summary["refs"],
            "supports": summary["supports"],
            "missing": summary["missing"],
        }
    if external_evidence:
        flat_evidence = {**(flat_evidence or {}), **external_evidence}

    aggregation_mod = _load_local("requirement_aggregation", _THIS.parent / "requirement_aggregation.py")
    aggregate = aggregation_mod.build_current_requirement_manifest(
        resolved_requirement,
        roots,
        previous_manifest=previous_manifest,
        external_evidence=flat_evidence,
    )

    results = {
        str(root): (_read_json(root / "result.json") if (root / "result.json").is_file() else {"verdict": "not_run", "task_results": []})
        for root in roots
    }
    current_run_id = (aggregate.get("current") or {}).get("run_id")
    current_root = next(
        (root for root in roots if _read_json(root / "run.json").get("run_id") == current_run_id), roots[-1])
    if acceptance_criteria is None:
        acceptance_criteria = (plans.get(current_root) or next(iter(plans.values()))).get("acceptance_criteria") or []

    tasks: dict[str, dict[str, Any]] = {}
    for plan in plans.values():
        for item in plan.get("tasks") or []:
            if isinstance(item, dict) and item.get("task_id"):
                tasks.setdefault(item["task_id"], item)

    case_records = {
        task_id: {
            "status": record.get("status") or "not_run",
            "evidence_refs": list(record.get("evidence_refs") or []),
            "source_run_id": record.get("source_run_id"),
        }
        for task_id, record in (aggregate.get("current_cases") or {}).items()
        if isinstance(record, dict)
    }
    run_verdicts = {item.get("run_id"): item.get("status") for item in aggregate.get("runs") or [] if isinstance(item, dict)}
    run_id_to_root = {}
    for root in roots:
        if (root / "run.json").is_file():
            run_id_to_root[_read_json(root / "run.json").get("run_id")] = root

    known_ac_ids = {
        str(item.get("ac_id") or item.get("id"))
        for item in acceptance_criteria
        if isinstance(item, dict) and (item.get("ac_id") or item.get("id"))
    }
    if records:
        unknown = evidence_mod.unknown_acceptance_ids(records, known_ac_ids)
        if unknown:
            raise OrchestrationError(
                "external evidence references acceptance criteria that do not exist in the requirement: "
                + ", ".join(unknown)
            )

    items = _acceptance_items(
        acceptance_criteria,
        tasks,
        case_records,
        run_verdicts,
        external_by_ac=(summary or {}).get("per_ac"),
        evidence_roots=run_id_to_root,
    )
    issues = (
        _gap_issues(items)
        + _defect_issues(aggregate.get("defects") or [], tasks)
        + _risk_issues(aggregate.get("residual_risks") or [])
    )
    verdict = _overall_verdict(items, issues)
    evidence = _evidence_runs(roots, results)
    if records and store_root is not None:
        evidence = evidence + evidence_mod.evidence_run_entries(records, store_root, resolved_requirement)
    for entry in evidence:
        # The ledger-recomputed verdict is authoritative over a possibly stale result.json.
        ledger_status = run_verdicts.get(entry["id"])
        if ledger_status:
            entry["summary"] = f"统一 Run {ledger_status}"
    current_run = _read_json(current_root / "run.json")
    return {
        "schema_version": "1.0",
        "report_id": f"AR-{resolved_requirement}-CURRENT",
        "report_version": "1.0",
        "report_view_version": "1.0",
        "requirement": {
            "id": resolved_requirement,
            "title": title or resolved_requirement,
            "acceptance_total": len(items),
        },
        "baseline": {"environment": (current_run.get("environment_snapshot") or {}).get("environment_id")},
        "generated_at": _load("run_state").now_iso(),
        "acceptance_items": items,
        "evidence_runs": evidence,
        "issues": issues,
        "release_recommendation": _RELEASE_RECOMMENDATION[verdict],
        "generated_from": "v2-unified-run-aggregate",
        "aggregation": {
            "current": aggregate.get("current"),
            "run_count": len(aggregate.get("runs") or []),
            "external_evidence": aggregate.get("external_evidence"),
        },
        "external_evidence": summary or {"status": "none", "per_ac": {}, "record_count": 0},
    }


def refresh_requirement_report(
    run_dirs: Any,
    *,
    requirement_id: str | None = None,
    store_root: str | Path,
    output_json: str | Path,
    output_html: str | Path,
    title: str | None = None,
    acceptance_criteria: list[dict[str, Any]] | None = None,
    previous_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute the one ``current`` requirement report after evidence changes.

    This is the only supported path from "new evidence registered" to "published
    report": the manifest is rebuilt from the immutable Run ledgers plus the
    append-only evidence ledger, validated by the renderer, and then rendered.
    ``store_root`` is required so a registered evidence ledger can never be
    silently dropped from the report.
    """
    manifest = build_requirement_manifest_for_runs(
        run_dirs,
        requirement_id=requirement_id,
        acceptance_criteria=acceptance_criteria,
        title=title,
        store_root=store_root,
        previous_manifest=previous_manifest,
    )
    renderer = _load_local("render_requirement_report", _THIS.parent / "render_requirement_report.py")
    manifest_path = Path(output_json).resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    # Evidence paths are written absolute, so validation must not depend on cwd.
    errors = renderer.validate(manifest, base_dir=manifest_path.parent)
    if errors:
        raise OrchestrationError("requirement report is not renderable: " + "; ".join(errors))
    _write_json_atomic(manifest_path, manifest)
    report_path = Path(output_html).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(renderer.render(manifest, base_dir=manifest_path.parent), encoding="utf-8", newline="\n")
    verdict, reason = renderer.overall_verdict(manifest)
    if manifest["release_recommendation"] != _RELEASE_RECOMMENDATION[verdict]:
        raise OrchestrationError("release_recommendation contradicts the rendered verdict")
    return {
        "manifest": str(manifest_path),
        "report": str(report_path),
        "verdict": verdict,
        "verdict_reason": reason,
        "external_evidence": manifest.get("external_evidence"),
    }


if __name__ == "__main__":
    raise SystemExit("Use this module through the v2 main Agent orchestration entry point")
