#!/usr/bin/env python3
"""Deterministic validation helpers for L1 Schema 2.1 assets.

The L1 skill writes Markdown assets, while execution consumes only their machine
blocks. This module validates those blocks without importing project code or
loading runtime values. It intentionally reports evidence gaps instead of
inventing configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


class AssetValidationError(ValueError):
    pass


CONFIG_BLOCK = re.compile(r"```yaml\s+test-config\s*\n(?P<body>[\s\S]*?)\n```", re.IGNORECASE)
PROFILE_BLOCK = re.compile(r"```yaml\s+interface-profile-governance\s*\n(?P<body>[\s\S]*?)\n```", re.IGNORECASE)
SECRET_LITERAL = re.compile(r"(?im)^\s*(?:token|password|secret|cookie|authorization|api[_-]?key)\s*:\s*([^$\n][^\n]*)$")


def _scalar(body: str, field: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(field)}:\s*[\"']?([^\"'\n#]+?)[\"']?\s*(?:#.*)?$", body)
    return match.group(1).strip() if match else None


def extract_config(text: str) -> str:
    blocks = list(CONFIG_BLOCK.finditer(text))
    if len(blocks) != 1:
        raise AssetValidationError("exactly one yaml test-config block is required")
    body = blocks[0].group("body")
    if "_env" in body:
        raise AssetValidationError("legacy _env fields are forbidden")
    return body


def validate_config(text: str) -> dict[str, Any]:
    body = extract_config(text)
    errors: list[str] = []
    if _scalar(body, "schema_version") != "2.1":
        errors.append("metadata.schema_version must be 2.1")
    if not _scalar(body, "project_id"):
        errors.append("metadata.project_id is required")
    revision = _scalar(body, "config_revision")
    if not revision or not revision.isdigit() or int(revision) < 1:
        errors.append("metadata.config_revision must be a positive integer")
    if not re.search(r"(?m)^environments:\s*$", body):
        errors.append("environments is required")
    if not re.search(r"(?m)^safety:\s*$", body):
        errors.append("safety is required")
    if SECRET_LITERAL.search(body):
        errors.append("runtime secrets must be binding metadata or placeholders")
    if errors:
        raise AssetValidationError("; ".join(errors))
    return {
        "schema_version": "2.1",
        "project_id": _scalar(body, "project_id"),
        "config_revision": int(revision),
        "block_fingerprint": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def _normalize_profile(text: str) -> str:
    return re.sub(
        r"(?m)^(\s*(?:document_fingerprint|contract_fingerprint):\s*)sha256:[0-9a-fA-F]{64}(\s*(?:#.*)?)$",
        r"\1sha256:<normalized>\2",
        text,
    )


FACT_STATUSES = {"candidate", "confirmed", "rejected", "needs_reviewer", "superseded"}
EXECUTABLE_LIFECYCLES = {"confirmed", "executable"}


def parse_fact_refs(body: str) -> list[dict[str, Any]]:
    """Parse the optional ``facts:`` list inside the governance block.

    The governance block is a YAML subset by design; this parser therefore only
    accepts the documented shape instead of pulling in a YAML dependency.
    """
    lines = body.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^facts:\s*(?:#.*)?$", line):
            start = index
            break
    if start is None:
        return []
    refs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            break
        item = re.match(r"^\s*-\s+([A-Za-z_]+):\s*(.*)$", line)
        if item:
            current = {item.group(1): item.group(2).strip()}
            refs.append(current)
            continue
        field = re.match(r"^\s+([A-Za-z_]+):\s*(.*)$", line)
        if field and current is not None:
            current[field.group(1)] = field.group(2).strip()
    return refs


def _validate_fact_refs(body: str, lifecycle: str | None, errors: list[str]) -> list[str]:
    refs = parse_fact_refs(body)
    if not refs:
        return []
    seen: set[str] = set()
    for index, ref in enumerate(refs):
        fact_id = (ref.get("fact_id") or "").strip()
        if not fact_id:
            errors.append(f"governance.facts[{index}] requires fact_id")
            continue
        if fact_id in seen:
            errors.append(f"governance.facts contains duplicate fact_id: {fact_id}")
        seen.add(fact_id)
        status = (ref.get("status") or "").strip()
        if status not in FACT_STATUSES:
            errors.append(f"governance.facts[{fact_id}] has unsupported status: {status or 'missing'}")
        if status == "confirmed" and not (ref.get("confirmed_by") or "").strip():
            errors.append(f"governance.facts[{fact_id}] confirmed status requires confirmed_by")
        if lifecycle in EXECUTABLE_LIFECYCLES and status != "confirmed":
            errors.append(
                f"governance.facts[{fact_id}] must be confirmed before lifecycle={lifecycle}; "
                "candidate facts cannot support executable contract facts"
            )
    return [ref.get("fact_id") for ref in refs if ref.get("fact_id")]


def validate_profile(text: str) -> dict[str, Any]:
    blocks = list(PROFILE_BLOCK.finditer(text))
    if len(blocks) != 1:
        raise AssetValidationError("exactly one interface-profile-governance block is required")
    body = blocks[0].group("body")
    errors: list[str] = []
    if _scalar(body, "schema_version") != "1.0":
        errors.append("governance.schema_version must be 1.0")
    for field in ("profile_id", "module_id", "code_baseline", "lifecycle"):
        if not _scalar(body, field):
            errors.append(f"governance.{field} is required")
    revision = _scalar(body, "profile_revision")
    if not revision or not revision.isdigit() or int(revision) < 1:
        errors.append("governance.profile_revision must be a positive integer")
    lifecycle = _scalar(body, "lifecycle")
    allowed = {"discovered", "drafted", "minimally_usable", "confirmed", "executable", "drifted", "reconfirm_required", "retired"}
    if lifecycle and lifecycle not in allowed:
        errors.append(f"unsupported lifecycle: {lifecycle}")
    if not re.search(r"(?ms)^owners:\s*\n(?:^[ \t]+[^\n]+\n?)+", body):
        errors.append("governance.owners is required")
    fact_ids = _validate_fact_refs(body, lifecycle, errors)
    normalized = _normalize_profile(text)
    document_fingerprint = _scalar(body, "document_fingerprint")
    actual = "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if document_fingerprint and document_fingerprint != actual:
        errors.append("document_fingerprint mismatch")
    if errors:
        raise AssetValidationError("; ".join(errors))
    return {
        "schema_version": "1.0",
        "profile_id": _scalar(body, "profile_id"),
        "module_id": _scalar(body, "module_id"),
        "profile_revision": int(revision),
        "lifecycle": lifecycle,
        "document_fingerprint": actual,
        "fact_ids": fact_ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate L1 Schema 2.1 Markdown assets")
    parser.add_argument("kind", choices=("config", "profile"))
    parser.add_argument("path")
    args = parser.parse_args()
    text = Path(args.path).read_text(encoding="utf-8")
    result = validate_config(text) if args.kind == "config" else validate_profile(text)
    print(json.dumps({"valid": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
