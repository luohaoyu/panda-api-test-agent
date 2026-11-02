#!/usr/bin/env python3
"""Fact ledger that connects read-only scan candidates to trusted assets.

The scanners only propose candidates. This module records each candidate as an
immutable, addressable fact, and records *decisions* about it as separate
append-only entries. Nothing here decides whether a project may execute: it only
answers "which fact is this, who confirmed it, and on what evidence".

Two decision levels are required by the L1 responsibility model:

- ``ai``: semantic interpretation of code, contract and deployment context.
- ``reviewer``: accountable owner confirmation for facts an AI cannot own.

An AI decision can never satisfy a fact whose owner is ``reviewer``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

FACT_SCHEMA_VERSION = "1.0"
PROTOCOL_VERSION = "1.0"
LEDGER_RELATIVE = Path("docs") / "scan-facts" / "ledger.jsonl"

CANDIDATE_STATUS = "candidate"
CONFIRMED_STATUS = "confirmed"
REJECTED_STATUS = "rejected"
NEEDS_REVIEWER_STATUS = "needs_reviewer"
SUPERSEDED_STATUS = "superseded"
FACT_STATUSES = {
    CANDIDATE_STATUS,
    CONFIRMED_STATUS,
    REJECTED_STATUS,
    NEEDS_REVIEWER_STATUS,
    SUPERSEDED_STATUS,
}
DECISION_STATUSES = {CONFIRMED_STATUS, REJECTED_STATUS, NEEDS_REVIEWER_STATUS, SUPERSEDED_STATUS}
ACTOR_TYPES = {"ai", "reviewer"}
DECISION_OWNERS = {"ai_skill", "reviewer"}
EXECUTABLE_LIFECYCLES = {"confirmed", "executable"}


class FactError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def fact_id_for(project_id: str, fact_type: str, subject: dict[str, Any], claim: str) -> str:
    """Stable identity: the same claim about the same subject keeps its id.

    A re-scan that observes the same fact therefore does not create noise, while
    any semantic change produces a new id and can be reported as drift.
    """
    digest = _hash(
        {
            "project_id": project_id,
            "fact_type": fact_type,
            "subject": subject,
            "claim": claim,
        }
    )[7:23]
    return f"FACT-{digest}"


def _subject_from_finding(kind: str, finding: dict[str, Any]) -> dict[str, Any]:
    if kind == "deployment":
        subject = {"service": finding.get("name") or finding.get("path_pattern")}
    else:
        subject = {"method": finding.get("method"), "path": finding.get("path_pattern")}
    return {key: value for key, value in subject.items() if value}


def _claim_from_finding(kind: str, finding: dict[str, Any]) -> str:
    method = finding.get("method") or ""
    path = finding.get("path_pattern") or ""
    if kind == "deployment":
        detail = finding.get("details") or {}
        target = detail.get("url") or detail.get("base_url") or detail.get("service") or ""
        name = finding.get("name") or path
        return f"deployment candidate {name} -> {target}".strip()
    if kind == "contracts":
        return f"contract declares {method} {path}".strip()
    return f"route {method} {path}".strip()


def facts_from_scan(
    report: dict[str, Any],
    *,
    project_id: str,
    scan_id: str | None = None,
    baseline: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Turn a scan report into candidate facts with stable ids and evidence."""
    if not isinstance(report, dict):
        raise FactError("scan report must be an object")
    if not project_id:
        raise FactError("project_id is required to derive facts")
    resolved_scan_id = scan_id or f"SCAN-{_hash({'project_id': project_id, 'report': report})[7:23]}"
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind in ("source", "contracts", "deployment"):
        for finding in report.get(kind) or []:
            if not isinstance(finding, dict):
                continue
            subject = _subject_from_finding(kind, finding)
            if not subject:
                continue
            claim = _claim_from_finding(kind, finding)
            fact_id = fact_id_for(project_id, kind, subject, claim)
            if fact_id in seen:
                continue
            seen.add(fact_id)
            facts.append(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "artifact_type": "scan_fact",
                    "schema_version": FACT_SCHEMA_VERSION,
                    "fact_id": fact_id,
                    "scan_id": resolved_scan_id,
                    "project_id": project_id,
                    "fact_type": kind,
                    "subject": subject,
                    "claim": claim,
                    "evidence": {
                        "file": finding.get("file"),
                        "line": finding.get("line"),
                        "source": finding.get("source"),
                        "confidence": finding.get("confidence"),
                        "detail": finding.get("evidence"),
                    },
                    "candidate_only": True,
                    "semantic_decision_owner": "ai_skill",
                    "status": CANDIDATE_STATUS,
                    "baseline": dict(baseline or {}),
                }
            )
    return facts


def validate_fact(fact: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(fact, dict):
        raise FactError("fact must be an object")
    if fact.get("artifact_type") != "scan_fact":
        raise FactError("fact.artifact_type must be scan_fact")
    if fact.get("schema_version") != FACT_SCHEMA_VERSION:
        raise FactError(f"fact.schema_version must be {FACT_SCHEMA_VERSION}")
    for field in ("fact_id", "project_id", "fact_type", "claim"):
        if not fact.get(field):
            raise FactError(f"fact.{field} is required")
    if not isinstance(fact.get("subject"), dict) or not fact["subject"]:
        raise FactError("fact.subject must be a non-empty object")
    if fact.get("status") not in FACT_STATUSES:
        raise FactError(f"unsupported fact.status: {fact.get('status')}")
    # A stored fact is always a candidate observation. Confirmation is a separate
    # append-only decision entry, so a fact record can never claim to be settled.
    if fact.get("status") != CANDIDATE_STATUS or fact.get("candidate_only") is not True:
        raise FactError("a fact is recorded as candidate only; confirmation belongs in the decision ledger")
    return fact


def ledger_path(store_root: str | Path) -> Path:
    return Path(store_root).resolve() / LEDGER_RELATIVE


def append_facts(store_root: str | Path, facts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append new candidates to the ledger; existing ids are never rewritten."""
    path = ledger_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {item.get("fact_id") for item in load_ledger(store_root)}
    written: list[dict[str, Any]] = []
    lines: list[str] = []
    for fact in facts:
        validate_fact(fact)
        if fact["fact_id"] in existing:
            continue
        existing.add(fact["fact_id"])
        written.append(fact)
        lines.append(_canonical(fact))
    if lines:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
    return written


def load_ledger(store_root: str | Path) -> list[dict[str, Any]]:
    path = ledger_path(store_root)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FactError(f"fact ledger line {index} is not valid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise FactError(f"fact ledger line {index} must be an object")
        records.append(record)
    return records


def _is_decision(record: dict[str, Any]) -> bool:
    return record.get("record_type") == "decision"


def current_facts(store_root: str | Path) -> dict[str, dict[str, Any]]:
    """Latest state per fact: the fact record plus its newest decision."""
    state: dict[str, dict[str, Any]] = {}
    for record in load_ledger(store_root):
        fact_id = record.get("fact_id")
        if not fact_id:
            continue
        if _is_decision(record):
            entry = state.get(fact_id)
            if entry is None:
                continue
            entry["decision"] = record
            entry["status"] = record.get("status")
            entry["candidate_only"] = record.get("status") != CONFIRMED_STATUS
            entry["semantic_decision_owner"] = record.get("decision_owner") or entry.get("semantic_decision_owner")
            if record.get("status") == SUPERSEDED_STATUS:
                entry["superseded_by"] = record.get("superseded_by")
        else:
            state[fact_id] = dict(record)
            state[fact_id]["decision"] = None
    return state


def record_decision(
    store_root: str | Path,
    fact_id: str,
    *,
    status: str,
    actor_type: str,
    actor_id: str,
    evidence_refs: list[str],
    rationale: str | None = None,
    decision_owner: str | None = None,
    decided_at: str | None = None,
    superseded_by: str | None = None,
) -> dict[str, Any]:
    """Record one decision entry. Append-only: history is never rewritten."""
    state = current_facts(store_root)
    fact = state.get(fact_id)
    if fact is None:
        raise FactError(f"unknown fact_id: {fact_id}")
    if status not in DECISION_STATUSES:
        raise FactError(f"unsupported decision status: {status}")
    if actor_type not in ACTOR_TYPES:
        raise FactError(f"unsupported actor_type: {actor_type}")
    if not actor_id:
        raise FactError("actor_id is required")
    refs = [item.strip() for item in evidence_refs or [] if isinstance(item, str) and item.strip()]
    owner = decision_owner or ("reviewer" if actor_type == "reviewer" else "ai_skill")
    if owner not in DECISION_OWNERS:
        raise FactError(f"unsupported decision_owner: {owner}")
    if status == CONFIRMED_STATUS:
        if not refs:
            raise FactError("confirmed decision requires evidence_refs")
        if owner == "reviewer" and actor_type != "reviewer":
            raise FactError("reviewer-owned fact cannot be confirmed by ai")
        if fact.get("semantic_decision_owner") == "reviewer" and actor_type != "reviewer":
            raise FactError("fact owner is reviewer: an AI decision cannot confirm it")
    if status in {REJECTED_STATUS, SUPERSEDED_STATUS} and not (rationale or superseded_by):
        raise FactError(f"{status} decision requires rationale or superseded_by")
    if status == SUPERSEDED_STATUS and not superseded_by:
        raise FactError("superseded decision requires superseded_by fact_id")
    if fact.get("status") == REJECTED_STATUS:
        raise FactError("rejected fact cannot be decided again; create a new fact from a new observation")
    record = {
        "record_type": "decision",
        "protocol_version": PROTOCOL_VERSION,
        "artifact_type": "scan_fact_decision",
        "schema_version": FACT_SCHEMA_VERSION,
        "fact_id": fact_id,
        "status": status,
        "decision_owner": owner,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "evidence_refs": refs,
        "rationale": rationale,
        "superseded_by": superseded_by,
        "decided_at": decided_at or _now(),
    }
    path = ledger_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_canonical(record) + "\n")
    return record


def governance_refs(facts: Iterable[dict[str, Any]], *, applies_to: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Compact, machine-readable references for the profile governance block."""
    refs = []
    for fact in facts:
        if not isinstance(fact, dict) or not fact.get("fact_id"):
            continue
        decision = fact.get("decision") or {}
        refs.append(
            {
                "fact_id": fact["fact_id"],
                "status": fact.get("status") or CANDIDATE_STATUS,
                "decision_owner": fact.get("semantic_decision_owner") or "ai_skill",
                "confirmed_by": decision.get("actor_id"),
                "applies_to": list(applies_to or [fact.get("fact_type") or "fact"]),
            }
        )
    return refs


def assert_refs_confirmed(refs: Iterable[dict[str, Any]], *, context: str = "profile") -> None:
    """Fail closed when candidate facts are presented as settled contract basis."""
    offenders = [
        f"{item.get('fact_id')}={item.get('status')}"
        for item in refs
        if isinstance(item, dict) and item.get("status") in {None, CANDIDATE_STATUS, NEEDS_REVIEWER_STATUS}
    ]
    if offenders:
        raise FactError(f"{context} requires confirmed facts: " + ", ".join(offenders))


def render_governance_block(refs: Iterable[dict[str, Any]], *, indent: str = "  ") -> str:
    """Render the ``facts:`` list exactly as the governance block expects."""
    lines = ["facts:"]
    for ref in refs:
        lines.append(f"{indent}- fact_id: {ref['fact_id']}")
        lines.append(f"{indent}  status: {ref['status']}")
        lines.append(f"{indent}  decision_owner: {ref['decision_owner']}")
        if ref.get("confirmed_by"):
            lines.append(f"{indent}  confirmed_by: {ref['confirmed_by']}")
        applies = ref.get("applies_to") or []
        if applies:
            lines.append(f"{indent}  applies_to: [{', '.join(str(item) for item in applies)}]")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="L1 scan fact ledger")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="record candidates from a scan report")
    ingest.add_argument("store_root", type=Path, help="project root containing docs/")
    ingest.add_argument("scan_report", type=Path)
    ingest.add_argument("--project-id", required=True)
    ingest.add_argument("--baseline", default=None, help="code baseline label for this scan")

    decide = subparsers.add_parser("decide", help="record an AI or reviewer decision")
    decide.add_argument("store_root", type=Path)
    decide.add_argument("fact_id")
    decide.add_argument("--status", required=True, choices=sorted(DECISION_STATUSES))
    decide.add_argument("--actor-type", required=True, choices=sorted(ACTOR_TYPES))
    decide.add_argument("--actor-id", required=True)
    decide.add_argument("--evidence-ref", action="append", default=[])
    decide.add_argument("--rationale", default=None)
    decide.add_argument("--superseded-by", default=None)

    status_cmd = subparsers.add_parser("status", help="print current fact state")
    status_cmd.add_argument("store_root", type=Path)

    args = parser.parse_args()
    if args.command == "ingest":
        report = json.loads(Path(args.scan_report).read_text(encoding="utf-8"))
        baseline = {"code_baseline": args.baseline} if args.baseline else {}
        facts = facts_from_scan(report, project_id=args.project_id, baseline=baseline)
        written = append_facts(args.store_root, facts)
        print(json.dumps({"derived": len(facts), "written": len(written), "fact_ids": [item["fact_id"] for item in written]}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "decide":
        record = record_decision(
            args.store_root,
            args.fact_id,
            status=args.status,
            actor_type=args.actor_type,
            actor_id=args.actor_id,
            evidence_refs=args.evidence_ref,
            rationale=args.rationale,
            superseded_by=args.superseded_by,
        )
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0
    facts = current_facts(args.store_root)
    summary = {
        fact_id: {"status": item.get("status"), "claim": item.get("claim"), "owner": item.get("semantic_decision_owner")}
        for fact_id, item in facts.items()
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
