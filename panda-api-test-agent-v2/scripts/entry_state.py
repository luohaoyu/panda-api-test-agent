#!/usr/bin/env python3
"""Shared entry state envelope for the four API test entry points.

This module is side-effect free. It normalizes and validates the review
envelope and phase vocabulary; the durable run state (``status``,
``completed_slices``, cleanup) is written by the L2 ``run_state`` module. Keep
the two models distinct: ``phase`` says where the flow is, ``review_status``
decides whether anything may execute.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


ENTRY_TYPES = {"requirement_validation", "single_api_quality", "api_debug", "environment_probe"}
PHASES = {
    "intent_received",
    "preparing",
    "designing",
    "pending_review",
    "executing",
    "reducing",
    "reported",
}
REVIEW_STATUSES = {
    "pending_review",
    "approved",
    "partially_approved",
    "rejected",
    "plan_only",
    "expired",
    "superseded",
}
EXECUTABLE_REVIEWS = {"approved", "partially_approved"}
TERMINAL_NON_EXECUTABLE_REVIEWS = {"rejected", "plan_only", "expired", "superseded"}

_ALLOWED_PHASE_TRANSITIONS = {
    "intent_received": {"preparing", "designing", "pending_review"},
    "preparing": {"designing", "pending_review"},
    "designing": {"pending_review"},
    "pending_review": {"pending_review", "executing"},
    "executing": {"executing", "reducing", "pending_review"},
    "reducing": {"reported", "executing"},
    "reported": set(),
}


class EntryStateError(ValueError):
    """Raised when an entry state or phase transition is invalid."""


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def normalize_review(review: Any, case_ids: list[str] | None = None) -> dict[str, Any]:
    """Normalize a review envelope without declaring it executable."""
    value = deepcopy(review) if isinstance(review, dict) else {}
    status = value.get("status") or "pending_review"
    if status not in REVIEW_STATUSES:
        raise EntryStateError(f"unsupported review status: {status}")
    scope = value.get("approved_case_ids")
    if scope is None:
        scope = value.get("approved_scope")
    if scope is None and status == "approved":
        scope = list(case_ids or [])
    if not isinstance(scope, list) or any(not isinstance(item, str) or not item for item in scope):
        scope = []
    return {
        **value,
        "status": status,
        "approved_case_ids": list(dict.fromkeys(scope)),
    }


def create_entry_state(*, run_id: str, entry_type: str, review: Any = None,
                       case_ids: list[str] | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if entry_type not in ENTRY_TYPES:
        raise EntryStateError(f"unsupported entry_type: {entry_type}")
    normalized_review = normalize_review(review, case_ids)
    review_status = normalized_review["status"]
    if review_status == "pending_review":
        phase, status, next_action = "pending_review", "awaiting_approval", "request_approval"
    elif review_status in EXECUTABLE_REVIEWS:
        phase, status, next_action = "executing", "ready", "execute_slice"
    else:
        phase, status, next_action = "pending_review", "blocked", "review_not_executable"
    state = {
        "schema_version": "2.1",
        "run_id": run_id,
        "entry_type": entry_type,
        "phase": phase,
        "status": status,
        "review_status": review_status,
        "review_id": normalized_review.get("review_id"),
        "approved_case_ids": normalized_review.get("approved_case_ids", []),
        "design_fingerprint": normalized_review.get("design_fingerprint"),
        "plan_fingerprint": normalized_review.get("plan_fingerprint"),
        "config_ref": {},
        "profile_ref": {},
        "blocking_gap_ids": [],
        "last_action": "create_run",
        "next_action": next_action,
        "request_count_expected": 0,
        "request_count_sent": 0,
        "failure_class": None,
        "verdict": None,
        "completed_slices": [],
        "volatile_replayed_slices": [],
        "next_slice_id": "S1",
        "pending_cleanup": [],
        "non_sensitive_variables": {},
        "review": normalized_review,
    }
    if isinstance(metadata, dict):
        for key in (
            "requirement_id", "interface_id", "environment_id", "scope", "source_refs",
            "config_ref", "profile_ref", "blocking_gap_ids", "request_count_expected",
        ):
            if key in metadata:
                state[key] = deepcopy(metadata[key])
    return state


def validate_state(state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(state, dict):
        return ["state must be an object"]
    if state.get("entry_type") not in ENTRY_TYPES:
        errors.append("state.entry_type is invalid")
    if state.get("phase") not in PHASES:
        errors.append("state.phase is invalid")
    if state.get("review_status") not in REVIEW_STATUSES:
        errors.append("state.review_status is invalid")
    if not isinstance(state.get("approved_case_ids"), list):
        errors.append("state.approved_case_ids must be a list")
    if state.get("phase") == "executing" and state.get("review_status") not in EXECUTABLE_REVIEWS:
        errors.append("only approved reviews may enter executing phase")
    if state.get("review_status") in TERMINAL_NON_EXECUTABLE_REVIEWS and state.get("next_action") == "execute_slice":
        errors.append("non-executable review cannot schedule execute_slice")
    return errors


def transition(state: dict[str, Any], target_phase: str, *, action: str | None = None, **updates: Any) -> dict[str, Any]:
    """Return a transitioned copy and reject illegal phase/review changes."""
    errors = validate_state(state)
    if errors:
        raise EntryStateError("; ".join(errors))
    current_phase = state["phase"]
    if target_phase not in PHASES:
        raise EntryStateError(f"unsupported target phase: {target_phase}")
    if target_phase != current_phase and target_phase not in _ALLOWED_PHASE_TRANSITIONS[current_phase]:
        raise EntryStateError(f"illegal phase transition: {current_phase} -> {target_phase}")
    result = deepcopy(state)
    result.update(deepcopy(updates))
    result["phase"] = target_phase
    if action:
        result["last_action"] = action
        result["next_action"] = action
    errors = validate_state(result)
    if errors:
        raise EntryStateError("; ".join(errors))
    return result


def next_action_for_state(state: dict[str, Any]) -> str:
    errors = validate_state(state)
    if errors:
        raise EntryStateError("; ".join(errors))
    if state.get("phase") == "reported" or state.get("status") in {"reported", "reduced"}:
        return "complete"
    if state.get("review_status") == "pending_review":
        return "request_approval"
    if state.get("review_status") in TERMINAL_NON_EXECUTABLE_REVIEWS:
        return "review_not_executable"
    if state.get("phase") == "reducing":
        return "reduce"
    if state.get("phase") == "executing":
        return "execute_slice"
    return str(state.get("next_action") or "request_approval")
