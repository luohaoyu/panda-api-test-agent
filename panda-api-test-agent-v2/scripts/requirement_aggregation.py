#!/usr/bin/env python3
"""Build a requirement-level current projection from immutable Run results.

This module is deliberately side-effect free. It does not rewrite historical Run
artifacts; it only normalizes supplied results and computes a new current view.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


RUN_STATUSES = {"passed", "failed", "blocked", "inconclusive", "not_run"}
EXECUTED_STATUSES = {"passed", "failed", "error", "blocked"}
DEFECT_STATUSES = {"open", "fixed", "reopened", "invalid"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _run_order(run: dict[str, Any], fallback: int) -> int:
    value = run.get("lineage", {}).get("run_sequence") if isinstance(run.get("lineage"), dict) else None
    return value if isinstance(value, int) else fallback


def normalize_run_result(result: dict[str, Any], run_ref: str | None = None, *, run_metadata: dict[str, Any] | None = None, sequence: int = 1) -> dict[str, Any]:
    """Normalize one immutable result into an aggregation record."""
    if not isinstance(result, dict):
        raise ValueError("result must be an object")
    metadata = run_metadata if isinstance(run_metadata, dict) else {}
    run_id = str(result.get("run_id") or metadata.get("run_id") or run_ref or f"RUN-{sequence}")
    verdict = str(result.get("verdict") or "not_run")
    if verdict not in RUN_STATUSES:
        verdict = "inconclusive"
    raw_lineage = result.get("lineage") if isinstance(result.get("lineage"), dict) else {}
    metadata_lineage = metadata.get("lineage") if isinstance(metadata.get("lineage"), dict) else {}
    lineage = {**metadata_lineage, **raw_lineage}
    authoritative = lineage.get("authoritative")
    if not isinstance(authoritative, bool):
        authoritative = verdict != "blocked"
    tasks = []
    for raw in result.get("task_results") or []:
        if not isinstance(raw, dict) or not raw.get("task_id"):
            continue
        status = str(raw.get("status") or "not_run")
        tasks.append({
            "task_id": str(raw["task_id"]),
            "type": raw.get("type"),
            "status": status,
            "evidence_refs": list(raw.get("evidence_refs") or []),
            "defect_id": raw.get("defect_id"),
            "assertion_results": copy.deepcopy(raw.get("assertion_results") or []),
        })
    return {
        "run_id": run_id,
        "run_ref": run_ref or metadata.get("result_ref") or run_id,
        "requirement_id": lineage.get("requirement_id") or metadata.get("requirement_id"),
        "verdict": verdict,
        "authoritative": authoritative,
        "lineage": lineage,
        "sequence": _run_order({"lineage": lineage}, sequence),
        "scope_hash": lineage.get("scope_hash") or _hash(sorted(item["task_id"] for item in tasks)),
        "retry_of_run_id": lineage.get("retry_of_run_id"),
        "supersedes_run_id": lineage.get("supersedes_run_id"),
        "task_results": tasks,
        "external_evidence": copy.deepcopy(result.get("external_evidence") or {}),
        "defects": copy.deepcopy(result.get("defects") or []),
    }


def load_run_record(run_dir: str | Path, *, sequence: int = 1) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    result = _read_json(root / "result.json")
    metadata = _read_json(root / "run.json") if (root / "run.json").is_file() else {}
    plan = _read_json(root / "plan.json") if (root / "plan.json").is_file() else {}
    record = normalize_run_result(result, str(root / "result.json"), run_metadata=metadata, sequence=sequence)
    record["requirement_id"] = record.get("requirement_id") or plan.get("requirement_id")
    record["lineage"].setdefault("requirement_id", record["requirement_id"])
    return record


def compute_defect_signature(requirement_id: str, case_id: str, assertion: dict[str, Any], classification: str = "product_defect") -> str:
    normalized = {
        "requirement_id": requirement_id,
        "case_id": case_id,
        "assertion_id": assertion.get("assertion_id") or assertion.get("id"),
        "expected": assertion.get("expected"),
        "observed": assertion.get("observed"),
        "classification": classification,
    }
    return _hash(normalized)


def _defects_from_run(record: dict[str, Any]) -> list[dict[str, Any]]:
    defects = []
    for raw in record.get("defects") or []:
        if not isinstance(raw, dict):
            continue
        signature = raw.get("signature")
        case_id = raw.get("case_id") or raw.get("task_id")
        if not signature and case_id:
            signature = compute_defect_signature(record.get("requirement_id") or "", str(case_id), raw, raw.get("classification", "product_defect"))
        if not signature:
            continue
        defects.append({**copy.deepcopy(raw), "signature": signature, "case_id": case_id, "run_id": record["run_id"]})
    for task in record.get("task_results") or []:
        if task.get("status") not in {"failed", "error"} or task.get("defect_id"):
            continue
        for assertion in task.get("assertion_results") or [{}]:
            signature = compute_defect_signature(record.get("requirement_id") or "", task["task_id"], assertion)
            defects.append({"signature": signature, "case_id": task["task_id"], "classification": "product_defect", "run_id": record["run_id"], "evidence_refs": task.get("evidence_refs") or []})
    return defects


def reconcile_defects(previous_defects: list[dict[str, Any]], current_runs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge defect identities across runs; not_run never closes a defect."""
    by_signature = {}
    for raw in previous_defects or []:
        if isinstance(raw, dict) and raw.get("signature"):
            by_signature[raw["signature"]] = copy.deepcopy(raw)
    seen_now: set[str] = set()
    passed_cases: set[str] = set()
    for record in current_runs:
        if not record.get("authoritative"):
            continue
        run_id = record["run_id"]
        for defect in _defects_from_run(record):
            signature = defect["signature"]
            seen_now.add(signature)
            item = by_signature.get(signature) or {
                "defect_id": "DEF-" + signature.split(":", 1)[-1][:16],
                "signature": signature,
                "requirement_id": record.get("requirement_id"),
                "case_id": defect.get("case_id"),
                "classification": defect.get("classification", "product_defect"),
                "status": "open",
                "first_seen_run_id": run_id,
                "seen_in_run_ids": [],
                "evidence_refs": [],
            }
            item["last_seen_run_id"] = run_id
            item["seen_in_run_ids"] = sorted(set(item.get("seen_in_run_ids") or []) | {run_id})
            item["evidence_refs"] = sorted(set(item.get("evidence_refs") or []) | set(defect.get("evidence_refs") or []))
            if item.get("status") == "fixed":
                item["status"] = "reopened"
                item["reopened_from_run_id"] = item.get("fixed_by_run_id")
                item["fixed_by_run_id"] = None
            else:
                item["status"] = "open"
            by_signature[signature] = item
        for task in record.get("task_results") or []:
            if task.get("status") == "passed":
                passed_cases.add(task["task_id"])
    for item in by_signature.values():
        if item.get("status") == "open" and item.get("case_id") in passed_cases and item.get("last_seen_run_id") not in seen_now:
            item["status"] = "fixed"
            item["fixed_by_run_id"] = item.get("last_seen_run_id")
    return sorted(by_signature.values(), key=lambda item: item.get("defect_id", ""))


def _merge_external_evidence(records: list[dict[str, Any]], supplied: dict[str, Any] | None = None) -> dict[str, Any]:
    statuses = {"complete": 3, "partial": 2, "none": 1}
    chosen = copy.deepcopy(supplied) if isinstance(supplied, dict) else {}
    refs = set(chosen.get("refs") or [])
    supports = set(chosen.get("supports") or [])
    missing = set(chosen.get("missing") or [])
    api_values = []
    for record in records:
        evidence = record.get("external_evidence") or {}
        if evidence.get("status") in statuses:
            chosen["status"] = max((chosen.get("status", "none"), evidence["status"]), key=lambda item: statuses[item])
        if evidence.get("api_verifiability"):
            api_values.append(evidence["api_verifiability"])
        refs.update(evidence.get("refs") or [])
        supports.update(evidence.get("supports") or [])
        missing.update(evidence.get("missing") or [])
    if "status" not in chosen:
        chosen["status"] = "complete" if records and all(record.get("verdict") == "passed" for record in records if record.get("authoritative")) else "partial" if refs else "none"
    chosen["refs"] = sorted(refs)
    chosen["supports"] = sorted(supports)
    chosen["missing"] = sorted(missing)
    chosen["api_verifiability"] = chosen.get("api_verifiability") or ("none" if not api_values else "partial" if "partial" in api_values else api_values[0])
    chosen["as_of_run_id"] = records[-1]["run_id"] if records else None
    return chosen


def aggregate_requirement_current(requirement_id: str, run_records: list[dict[str, Any]], *, previous_manifest: dict[str, Any] | None = None, external_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create one current projection while preserving every historical run."""
    records = [record for record in run_records if record.get("requirement_id") in {None, requirement_id}]
    records.sort(key=lambda item: (item.get("sequence", 0), item.get("run_id", "")))
    authoritative = [record for record in records if record.get("authoritative")]
    current_cases = copy.deepcopy((previous_manifest or {}).get("current_cases") or {})
    for record in authoritative:
        for task in record.get("task_results") or []:
            if task.get("status") == "not_run":
                continue
            current_cases[task["task_id"]] = {
                "status": task.get("status"),
                "source_run_id": record["run_id"],
                "evidence_refs": list(task.get("evidence_refs") or []),
                "defect_id": task.get("defect_id"),
            }
    statuses = {item.get("status") for item in current_cases.values()}
    if not current_cases:
        current_status = "not_run"
    elif statuses & {"failed", "error"}:
        current_status = "failed"
    elif statuses - {"passed"}:
        current_status = "inconclusive"
    else:
        current_status = "passed"
    current_run = authoritative[-1] if authoritative else None
    defects = reconcile_defects((previous_manifest or {}).get("defects") or [], records)
    return {
        "schema_version": "1.0",
        "requirement_id": requirement_id,
        "current": {
            "run_id": current_run.get("run_id") if current_run else None,
            "status": current_status,
            "selection_reason": "latest_authoritative_run_with_case_level_merge",
            "scope_hash": current_run.get("scope_hash") if current_run else None,
        },
        "runs": [
            {
                "run_id": record["run_id"],
                "status": record["verdict"],
                "scope_hash": record["scope_hash"],
                "authoritative": record["authoritative"],
                "result_ref": record["run_ref"],
                "retry_of_run_id": record.get("retry_of_run_id"),
                "supersedes_run_id": record.get("supersedes_run_id"),
            }
            for record in records
        ],
        "current_cases": current_cases,
        "defects": defects,
        "external_evidence": _merge_external_evidence(records, external_evidence),
    }


def build_current_requirement_manifest(requirement_id: str, run_dirs: Iterable[str | Path], *, previous_manifest: dict[str, Any] | None = None, external_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    records = [load_run_record(run_dir, sequence=index) for index, run_dir in enumerate(run_dirs, 1)]
    return aggregate_requirement_current(requirement_id, records, previous_manifest=previous_manifest, external_evidence=external_evidence)
