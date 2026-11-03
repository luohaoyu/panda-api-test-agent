"""Fail-closed execution authorization primitives shared by L2 and L1."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


EXECUTION_AUTHORIZATION_STATUSES = {
    "pending_approval",
    "approved",
    "partially_approved",
    "rejected",
    "expired",
    "superseded",
}
APPROVED_AUTHORIZATION_STATUSES = {"approved", "partially_approved"}
SENSITIVE_KEYS = {"token", "secret", "password", "credential", "authorization", "cookie", "api_key", "apikey"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _without_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_sensitive(item)
            for key, item in value.items()
            if str(key).lower() not in SENSITIVE_KEYS and not any(part in str(key).lower() for part in ("token", "secret", "password", "credential"))
        }
    if isinstance(value, list):
        return [_without_sensitive(item) for item in value]
    return value


def derive_side_effect_summary(source: dict[str, Any] | None, case_ids: set[str] | list[str] | None = None) -> list[dict[str, Any]]:
    source = source if isinstance(source, dict) else {}
    explicit = source.get("side_effect_summary")
    if explicit is not None:
        return _without_sensitive(deepcopy(explicit))
    selected = set(case_ids or [])
    cases = source.get("cases")
    if cases is None:
        cases = source.get("tasks")
    summary = []
    for case in cases or []:
        if not isinstance(case, dict):
            continue
        case_id = case.get("case_id") or case.get("task_id")
        if selected and case_id not in selected:
            continue
        summary.append({
            "case_id": case_id,
            "side_effect": case.get("side_effect"),
            "type": case.get("type") or case.get("task_type"),
        })
    return sorted(_without_sensitive(summary), key=lambda item: str(item.get("case_id")))


def calculate_execution_scope_fingerprint(
    plan_fingerprint: str,
    executable_case_ids: list[str] | set[str],
    environment_id: str,
    request_count_expected: int,
    side_effect_summary: Any,
) -> str:
    payload = {
        "plan_fingerprint": plan_fingerprint,
        "executable_case_ids": sorted(set(executable_case_ids)),
        "environment_id": environment_id,
        "request_count_expected": request_count_expected,
        "side_effect_summary": _without_sensitive(side_effect_summary if side_effect_summary is not None else []),
    }
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def _parse_time(value: Any):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def normalize_execution_authorization(
    authorization: Any,
    *,
    executable_case_ids: list[str] | set[str],
    plan_fingerprint: str,
    environment_id: str,
    request_count_expected: int,
    side_effect_summary: Any = None,
    blocked_case_ids: list[str] | set[str] | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[str]]:
    value = deepcopy(authorization) if isinstance(authorization, dict) else {}
    executable = set(executable_case_ids)
    blocked = set(blocked_case_ids or [])
    status = value.get("status") or "pending_approval"
    authorized = value.get("authorized_case_ids") if "authorized_case_ids" in value else []
    errors: list[str] = []
    if value.get("kind") not in (None, "execution_approval"):
        errors.append("EXECUTION_AUTHORIZATION_KIND_INVALID")
    if status not in EXECUTION_AUTHORIZATION_STATUSES:
        errors.append("EXECUTION_AUTHORIZATION_STATUS_INVALID")
    if not isinstance(authorized, list) or any(not isinstance(item, str) or not item for item in authorized):
        errors.append("EXECUTION_SCOPE_INVALID")
        authorized = []
    authorized = list(dict.fromkeys(authorized))
    unknown = sorted(set(authorized) - executable)
    if unknown:
        errors.append("EXECUTION_SCOPE_NOT_READY: " + ", ".join(unknown))
    blocked_authorized = sorted(set(authorized) & blocked)
    if blocked_authorized:
        errors.append("blocked/not_ready cases cannot be execution-authorized: " + ", ".join(blocked_authorized))
    expected_fp = calculate_execution_scope_fingerprint(
        plan_fingerprint,
        sorted(executable),
        environment_id,
        request_count_expected,
        side_effect_summary if side_effect_summary is not None else [],
    )
    declared_fp = value.get("execution_scope_fingerprint")
    if declared_fp and declared_fp != expected_fp:
        errors.append("EXECUTION_SCOPE_FINGERPRINT_MISMATCH")
    if status in APPROVED_AUTHORIZATION_STATUSES:
        if not authorized:
            errors.append("EXECUTION_SCOPE_EMPTY")
        for field in ("authorization_id", "authorizer", "authorized_at"):
            if not value.get(field):
                errors.append(f"EXECUTION_AUTHORIZATION_{field.upper()}_REQUIRED")
        if not _parse_time(value.get("authorized_at")):
            errors.append("authorized_at must be ISO8601 with timezone")
        expires_at = value.get("expires_at")
        if expires_at and (not _parse_time(expires_at) or _parse_time(expires_at) <= (now or datetime.now(timezone.utc))):
            errors.append("execution authorization is expired")
    canonical = {
        **value,
        "kind": "execution_approval",
        "status": status,
        "authorized_case_ids": authorized,
        "execution_scope_fingerprint": expected_fp,
    }
    if status not in APPROVED_AUTHORIZATION_STATUSES:
        canonical["authorized_case_ids"] = authorized
    return canonical, list(dict.fromkeys(errors))


def execution_authorization_is_valid(
    authorization: Any,
    *,
    executable_case_ids: list[str] | set[str],
    plan_fingerprint: str,
    environment_id: str,
    request_count_expected: int,
    side_effect_summary: Any = None,
    blocked_case_ids: list[str] | set[str] | None = None,
) -> bool:
    normalized, errors = normalize_execution_authorization(
        authorization,
        executable_case_ids=executable_case_ids,
        plan_fingerprint=plan_fingerprint,
        environment_id=environment_id,
        request_count_expected=request_count_expected,
        side_effect_summary=side_effect_summary,
        blocked_case_ids=blocked_case_ids,
    )
    return not errors and normalized.get("status") in APPROVED_AUTHORIZATION_STATUSES and bool(normalized.get("authorized_case_ids"))
