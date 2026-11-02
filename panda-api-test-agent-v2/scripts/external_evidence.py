#!/usr/bin/env python3
"""Append-only registration of non-API acceptance evidence.

The API layer can only prove what it can observe. Acceptance criteria classified
as ``partial`` or ``none`` additionally depend on facts produced outside the API
(UI checks, DB rows, messages, audit trails, manual acceptance). Before this
module those facts had no home: they lived in the conversation, could not be
recomputed, and could not close an AC verdict.

Design rules:

* Registration is append-only and never rewrites history.
* API-layer facts may **not** be registered here. They are produced by Runs, and
  accepting them from a free-text channel would let a claim masquerade as
  evidence.
* ``passed``/``failed`` require at least one concrete reference. A verbal "I
  checked it" is not evidence.
* Later registrations for the same (ac_id, layer) supersede earlier ones instead
  of mutating them.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "external_evidence"
LEDGER_NAME = "external-evidence.jsonl"

# Validation layers a human/AI can attest to.
LAYERS = {
    "ui",
    "client",
    "db",
    "mq",
    "cache",
    "audit",
    "log",
    "trace",
    "manual",
    "security",
    "performance",
    "compatibility",
    "other",
}
# Facts that belong to the API layer must come from an immutable Run result.
API_LAYERS = {"api", "http", "business_execution", "run", "runner"}

STATUSES = {"passed", "failed", "blocked"}
EVIDENCE_ID = re.compile(r"^EXT-[0-9a-f]{12}$")


class ExternalEvidenceError(ValueError):
    """Raised when a registration would create untrustworthy evidence."""


def ledger_path(store_root: str | Path, requirement_id: str) -> Path:
    return Path(store_root).resolve() / "requirements" / str(requirement_id).strip() / LEDGER_NAME


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _evidence_id(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return "EXT-" + digest[:12]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalEvidenceError(f"{field} is required and must be a non-empty string")
    return value.strip()


def _normalize_refs(refs: Any, status: str) -> list[str]:
    if refs is None:
        refs = []
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        raise ExternalEvidenceError("refs must be a string or an array of strings")
    cleaned: list[str] = []
    for item in refs:
        if not isinstance(item, str) or not item.strip():
            raise ExternalEvidenceError("refs must contain non-empty strings only")
        cleaned.append(item.strip())
    if status in {"passed", "failed"} and not cleaned:
        raise ExternalEvidenceError(
            f"status={status} requires at least one evidence ref; a verbal claim is not evidence"
        )
    return sorted(set(cleaned))


def record_external_evidence(
    store_root: str | Path,
    requirement_id: str,
    ac_id: str,
    *,
    layer: str,
    status: str,
    refs: Iterable[str] | str | None = None,
    provided_by: str,
    note: str | None = None,
    recorded_at: str | None = None,
    source: str = "user_report",
    supersedes: str | None = None,
) -> dict[str, Any]:
    """Append one external evidence record and return the persisted object."""
    requirement_id = _require_text(requirement_id, "requirement_id")
    ac_id = _require_text(ac_id, "ac_id")
    layer = _require_text(layer, "layer").lower()
    status = _require_text(status, "status").lower()
    provided_by = _require_text(provided_by, "provided_by")

    if layer in API_LAYERS:
        raise ExternalEvidenceError(
            f"layer={layer!r} belongs to the API layer and must be produced by an immutable Run result, "
            "not registered as external evidence"
        )
    if layer not in LAYERS:
        raise ExternalEvidenceError(f"unsupported external evidence layer: {layer!r}")
    if status not in STATUSES:
        raise ExternalEvidenceError(f"unsupported external evidence status: {status!r}")

    clean_refs = _normalize_refs(refs, status)
    timestamp = recorded_at or _utc_now()
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise ExternalEvidenceError("recorded_at must be an ISO-8601 string")

    existing = load_external_evidence(store_root, requirement_id)
    if supersedes is None:
        same_scope = [item for item in existing if item.get("ac_id") == ac_id and item.get("layer") == layer]
        if same_scope:
            supersedes = same_scope[-1].get("evidence_id")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "requirement_id": requirement_id,
        "ac_id": ac_id,
        "layer": layer,
        "status": status,
        "refs": clean_refs,
        "provided_by": provided_by,
        "note": (note or "").strip(),
        "recorded_at": timestamp.strip(),
        "source": _require_text(source, "source"),
        "supersedes": supersedes,
    }
    record = {**payload, "evidence_id": _evidence_id(payload)}

    path = ledger_path(store_root, requirement_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def load_external_evidence(store_root: str | Path, requirement_id: str) -> list[dict[str, Any]]:
    """Read the append-only ledger; a malformed line is corruption, not a gap."""
    path = ledger_path(store_root, requirement_id)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ExternalEvidenceError(f"corrupted external evidence ledger at line {number}: {exc}") from exc
            if not isinstance(item, dict) or not EVIDENCE_ID.match(str(item.get("evidence_id") or "")):
                raise ExternalEvidenceError(f"invalid external evidence record at line {number}")
            if item.get("requirement_id") and item["requirement_id"] != requirement_id:
                raise ExternalEvidenceError(
                    f"external evidence ledger for {requirement_id} contains a record for {item['requirement_id']}"
                )
            records.append(item)
    return records


def _latest_per_layer(records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Return the effective record per layer plus superseded evidence ids."""
    effective: dict[str, dict[str, Any]] = {}
    superseded: set[str] = set()
    for record in records:
        layer = str(record.get("layer") or "")
        previous = effective.get(layer)
        if previous:
            superseded.add(str(previous.get("evidence_id")))
        # Ledger order is the tie-breaker; recorded_at only when explicitly set.
        effective[layer] = record
    return effective, superseded


def summarize_external_evidence(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse records into per-AC facts plus one requirement-level summary."""
    by_ac: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_ac.setdefault(str(record.get("ac_id")), []).append(record)

    per_ac: dict[str, dict[str, Any]] = {}
    refs: set[str] = set()
    supports: set[str] = set()
    missing: set[str] = set()
    has_failed = False
    has_blocked = False
    for ac_id in sorted(by_ac):
        effective, superseded = _latest_per_layer(by_ac[ac_id])
        layers = {layer: str(item.get("status")) for layer, item in sorted(effective.items())}
        ac_refs = sorted({ref for item in effective.values() for ref in item.get("refs") or []})
        ac_superseded = sorted(superseded | {str(item.get("evidence_id")) for item in by_ac[ac_id] if item.get("evidence_id") in superseded})
        if "failed" in layers.values():
            status = "failed"
            has_failed = True
        elif "blocked" in layers.values():
            status = "blocked"
            has_blocked = True
        elif layers and set(layers.values()) == {"passed"}:
            status = "passed"
            supports.add(ac_id)
        else:
            status = "missing"
        if status != "passed":
            missing.add(ac_id)
        refs.update(ac_refs)
        per_ac[ac_id] = {
            "status": status,
            "layers": layers,
            "refs": ac_refs,
            "evidence_ids": sorted(str(item.get("evidence_id")) for item in effective.values()),
            "superseded_evidence_ids": ac_superseded,
        }

    if not per_ac:
        overall = "none"
    elif has_failed:
        overall = "failed"
    elif has_blocked or missing:
        overall = "partial"
    else:
        overall = "complete"

    return {
        "status": overall,
        # The aggregation layer only understands coverage (complete/partial/none);
        # AC-level pass/fail semantics stay in ``status`` and ``per_ac``.
        "rollup_status": {"complete": "complete", "partial": "partial", "blocked": "partial", "failed": "partial", "none": "none"}[overall],
        "refs": sorted(refs),
        "supports": sorted(supports),
        "missing": sorted(missing),
        "per_ac": per_ac,
        "record_count": len(records),
    }


def evidence_run_entries(records: list[dict[str, Any]], store_root: str | Path, requirement_id: str) -> list[dict[str, Any]]:
    """Expose each effective record as an immutable evidence reference for the report."""
    path = ledger_path(store_root, requirement_id)
    entries = []
    seen: set[str] = set()
    for record in records:
        evidence_id = str(record.get("evidence_id"))
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        entries.append({
            "id": evidence_id,
            "kind": "external_evidence",
            "result_path": str(path),
            "summary": f"{record.get('layer')} 层外部验证 {record.get('status')}（{record.get('provided_by')}）",
            "immutable": True,
        })
    return entries


def unknown_acceptance_ids(records: list[dict[str, Any]], known_ac_ids: Iterable[str]) -> list[str]:
    known = {str(item) for item in known_ac_ids}
    return sorted({str(record.get("ac_id")) for record in records if str(record.get("ac_id")) not in known})
