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
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
        "auto_recovery": ["feasibility_plan_built"] if feasibility_plan is not None else [],
    }


def _validate_unified_review(review: dict[str, Any], plan: dict[str, Any], case_ids: list[str]) -> None:
    status = review.get("status")
    scope = review.get("approved_case_ids") or []
    if not scope:
        raise OrchestrationError("executable review requires approved_case_ids")
    unknown = set(scope) - set(case_ids)
    if unknown:
        raise OrchestrationError("review contains unknown approved case IDs: " + ", ".join(sorted(unknown)))
    if status == "approved" and set(scope) != set(case_ids):
        raise OrchestrationError("approved review must cover every executable case")
    if status == "partially_approved" and set(scope) == set(case_ids):
        raise OrchestrationError("partially_approved review must identify a subset")
    if not review.get("review_id") or not review.get("reviewer") or not review.get("approved_at"):
        raise OrchestrationError("executable review requires review_id, reviewer, and approved_at")
    if review.get("plan_fingerprint") != plan.get("plan_fingerprint"):
        raise OrchestrationError("review.plan_fingerprint does not match compiled plan")
    reviewed = review.get("reviewed_fields")
    if not isinstance(reviewed, dict):
        raise OrchestrationError("executable review requires reviewed_fields by case")
    for case_id in scope:
        if not isinstance(reviewed.get(case_id), list) or not reviewed.get(case_id):
            raise OrchestrationError(f"reviewed_fields must cover approved case {case_id}")


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
    case_ids = [item.get("case_id") for item in plan.get("tasks", []) if isinstance(item, dict) and item.get("case_id")]
    normalized_review = entry_state_mod.normalize_review(review, case_ids)
    if normalized_review.get("status") not in entry_state_mod.REVIEW_STATUSES:
        raise OrchestrationError("unsupported review status")
    if normalized_review.get("status") in entry_state_mod.EXECUTABLE_REVIEWS:
        _validate_unified_review(normalized_review, plan, case_ids)
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
        },
    )
    initial_action = "execute_slice" if normalized_review.get("status") in entry_state_mod.EXECUTABLE_REVIEWS else "request_approval" if normalized_review.get("status") == "pending_review" else "review_not_executable"
    return {"run": head, "plan": plan, "next_action": {"action": initial_action, "slice_id": "S1" if initial_action == "execute_slice" else None}, "review": normalized_review}


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
    if state.get("status") == "blocked":
        return {"action": "blocked", "reason": "run is blocked; resolve the recorded gate or failure before continuing"}
    if state.get("review_status") == "pending_review" or state.get("status") == "awaiting_approval":
        return {"action": "request_approval", "slice_id": state.get("next_slice_id"), "approved_case_ids": state.get("approved_case_ids") or []}
    if state.get("review_status") in {"rejected", "plan_only", "expired", "superseded"} or state.get("status") == "blocked":
        return {"action": "review_not_executable", "reason": f"review status {state.get('review_status')} is not executable"}
    recovery = executor.validate_recovery(root)
    if recovery["action"] == "cleanup_first":
        return {"action": "execute_cleanup_first", "slice_ids": recovery.get("cleanup_slice_ids") or [], "recovery": recovery}
    if recovery["action"] == "replay_volatile_source":
        return {"action": "replay_volatile_source", "slice_ids": recovery.get("replay_slice_ids") or [], "recovery": recovery, "requires_runtime_context": True}
    completed = set(state.get("completed_slices") or [])
    for item in plan.get("slices") or []:
        if item.get("slice_id") not in completed:
            if item.get("approval", {}).get("required"):
                return {"action": "request_approval", "slice_id": item["slice_id"], "task_ids": item.get("task_ids") or []}
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


def build_requirement_manifest(run_dir: str | Path, *, title: str | None = None) -> dict[str, Any]:
    """Project one reduced Run into the deterministic report manifest model."""
    root = Path(run_dir).resolve()
    result = json.loads((root / "result.json").read_text(encoding="utf-8")) if (root / "result.json").is_file() else reduce_run(root)
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    run = json.loads((root / "run.json").read_text(encoding="utf-8"))
    criteria = plan.get("acceptance_criteria") or []
    tasks = {item.get("task_id"): item for item in plan.get("tasks") or [] if isinstance(item, dict)}
    results = {item.get("task_id"): item for item in result.get("task_results") or []}
    business_run = bool(result.get("evidence_closure", {}).get("business_execution"))
    evidence_run_id = run.get("run_id")
    evidence_runs = [{
        "id": evidence_run_id,
        "kind": "business_execution" if business_run else "gate",
        "result_path": str(root / "result.json"),
        "summary": f"统一 Run {result.get('verdict', 'not_run')}",
        "immutable": True,
    }]
    items = []
    for index, criterion in enumerate(criteria, 1):
        ac_id = criterion.get("ac_id") or criterion.get("id") or f"AC-{index}"
        ac_tasks = [task_id for task_id, task in tasks.items() if _task_matches_ac(task, ac_id)]
        task_items = [results.get(task_id, {"status": "not_run", "evidence_refs": []}) for task_id in ac_tasks]
        refs = sorted({ref for item in task_items for ref in item.get("evidence_refs") or []})
        statuses = {item.get("status") for item in task_items}
        if not task_items or statuses == {"not_run"}:
            status, verdict = "not_run", "not_run"
        elif statuses & {"failed", "error"}:
            status, verdict = "complete", "failed"
        elif all(item.get("status") == "passed" for item in task_items) and refs and result.get("verdict") == "passed":
            status, verdict = "complete", "passed"
        else:
            status, verdict = "partial", "inconclusive"
        item_gaps = list(criterion.get("gaps") or [])
        if verdict != "passed":
            item_gaps.append("证据或执行链尚未闭合")
        items.append({
            "id": ac_id,
            "title": criterion.get("text") or criterion.get("title") or ac_id,
            "interface_verifiability": criterion.get("interface_verifiability", "direct"),
            "verifiability_reason": criterion.get("verifiability_reason") or "未声明验收项的接口可验证性",
            "interface_scope": criterion.get("interface_scope") or [],
            "non_interface_scope": criterion.get("non_interface_scope") or [],
            "recommended_validation_layers": criterion.get("recommended_validation_layers") or [],
            "execution_status": "not_run" if criterion.get("interface_verifiability") == "none" else status,
            "verdict": "not_run" if criterion.get("interface_verifiability") == "none" else verdict,
            "actual": "接口层不可验证" if criterion.get("interface_verifiability") == "none" else f"统一 Run: {result.get('verdict')}",
            "gaps": item_gaps,
            "evidence_refs": [evidence_run_id] if evidence_run_id and business_run and verdict == "passed" and criterion.get("interface_verifiability") != "none" else [],
            "issue_refs": list(criterion.get("issue_refs") or []),
        })
    return {
        "schema_version": "1.0",
        "report_id": f"AR-{plan.get('requirement_id')}-{run.get('run_id')}",
        "report_version": "1.0",
        "requirement": {"id": plan.get("requirement_id"), "title": title or plan.get("requirement_id"), "acceptance_total": len(items)},
        "baseline": {"environment": (run.get("environment_snapshot") or {}).get("environment_id")},
        "acceptance_items": items,
        "evidence_runs": evidence_runs,
        "issues": [
            {
                "id": f"GAP-{ac_id}",
                "type": "test_gap",
                "severity": "medium",
                "title": "接口层验收证据未闭合",
                "actual": "验收项存在未执行、接口不可验证或证据不足范围",
                "impact": "不能据此形成需求完整通过结论",
                "acceptance_refs": [ac_id],
                "evidence_refs": refs,
                "status": "open",
            }
            for item in items for ac_id in [item["id"]]
            if item.get("verdict") in {"not_run", "inconclusive", "blocked"} and item.get("interface_verifiability") != "direct"
        ],
        "release_recommendation": "可进入下一阶段" if result.get("verdict") == "passed" else "阻断发布；补齐证据或处理阻断项",
        "generated_from": "v2-unified-run",
    }


if __name__ == "__main__":
    raise SystemExit("Use this module through the v2 main Agent orchestration entry point")
