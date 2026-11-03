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
SECRET_LITERAL = re.compile(
    r"(?im)^[ \t]*(?:token|password|cookie|authorization|api[_-]?key)[ \t]*:[ \t]*([^$\n][^\n]*)$"
)
SECRET_FIELD = re.compile(r"(?im)^[ \t]*secret[ \t]*:[ \t]*(?P<value>[^\n#]+)")
LEGACY_ENV_FIELD = re.compile(r"(?m)^\s*[A-Za-z0-9_.-]+_env\s*:")


def _scalar(body: str, field: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(field)}:\s*[\"']?([^\"'\n#]+?)[\"']?\s*(?:#.*)?$", body)
    return match.group(1).strip() if match else None


def extract_config(text: str) -> str:
    blocks = list(CONFIG_BLOCK.finditer(text))
    if len(blocks) != 1:
        raise AssetValidationError("exactly one yaml test-config block is required")
    body = blocks[0].group("body")
    if LEGACY_ENV_FIELD.search(body):
        raise AssetValidationError("legacy _env fields are forbidden")
    return body


def _yaml_scalar_map(body: str) -> dict[tuple[str, ...], str]:
    """Extract scalar paths from the documented YAML subset without loading YAML."""
    scalars: dict[tuple[str, ...], str] = {}
    stack: list[tuple[int, str]] = []
    for raw_line in body.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith(("#", "- ")):
            continue
        match = re.match(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_${}.-]+):(?:\s*(?P<value>.*?))?\s*$", raw_line)
        if not match:
            continue
        indent = len(match.group("indent").expandtabs(2))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = match.group("key")
        path = tuple(item[1] for item in stack) + (key,)
        value = (match.group("value") or "").split(" #", 1)[0].strip().strip("\"'")
        if value:
            scalars[path] = value
        stack.append((indent, key))
    return scalars


def _validate_runtime_bindings(body: str, errors: list[str]) -> dict[str, int]:
    scalars = _yaml_scalar_map(body)
    binding_count = 0
    stable_value_count = 0
    secret_ref_count = 0
    allowed_secret_fields = {"key", "required", "secret", "format", "secret_ref"}
    for path, secret in scalars.items():
        if path[-1] != "secret" or "runtime_bindings" not in path:
            continue
        binding_count += 1
        binding_path = path[:-1]
        if secret not in {"true", "false"}:
            errors.append(f"{'.'.join(path)} must be true or false")
            continue
        direct_fields = {
            candidate[-1]
            for candidate in scalars
            if len(candidate) == len(binding_path) + 1 and candidate[:-1] == binding_path
        }
        if secret == "true":
            if "value" in direct_fields:
                errors.append(f"{'.'.join(binding_path)}: secret=true forbids value")
            unsupported = sorted(direct_fields - allowed_secret_fields)
            if unsupported:
                errors.append(
                    f"{'.'.join(binding_path)}: secret=true only permits binding metadata/secret_ref; "
                    f"unsupported fields: {', '.join(unsupported)}"
                )
            if "secret_ref" in direct_fields:
                secret_ref_count += 1
        elif "secret_ref" in direct_fields:
            errors.append(f"{'.'.join(binding_path)}: secret=false forbids secret_ref")
        elif "value" in direct_fields:
            stable_value_count += 1
    return {
        "runtime_binding_count": binding_count,
        "stable_non_secret_value_count": stable_value_count,
        "secret_ref_count": secret_ref_count,
    }


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
    binding_summary = _validate_runtime_bindings(body, errors)
    secret_field_literals = [
        match.group("value").strip()
        for match in SECRET_FIELD.finditer(body)
        if match.group("value").strip() not in {"true", "false"}
    ]
    if SECRET_LITERAL.search(body) or secret_field_literals:
        errors.append("runtime secrets must be binding metadata or placeholders")
    if errors:
        raise AssetValidationError("; ".join(errors))
    return {
        "schema_version": "2.1",
        "project_id": _scalar(body, "project_id"),
        "config_revision": int(revision),
        **binding_summary,
        "block_fingerprint": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def validate_config_update(previous_text: str, current_text: str) -> dict[str, Any]:
    """Validate revision movement for canonical config content changes."""
    previous = validate_config(previous_text)
    current = validate_config(current_text)
    previous_body = extract_config(previous_text)
    current_body = extract_config(current_text)
    previous_content = re.sub(r"(?m)^(\s*config_revision:\s*)\d+(\s*(?:#.*)?)$", r"\1<revision>\2", previous_body)
    current_content = re.sub(r"(?m)^(\s*config_revision:\s*)\d+(\s*(?:#.*)?)$", r"\1<revision>\2", current_body)
    changed = previous_content != current_content
    expected_revision = previous["config_revision"] + (1 if changed else 0)
    if current["config_revision"] != expected_revision:
        rule = "must increment by one" if changed else "must remain unchanged"
        raise AssetValidationError(f"metadata.config_revision {rule} for canonical config content")
    return {
        "changed": changed,
        "previous_revision": previous["config_revision"],
        "config_revision": current["config_revision"],
        "block_fingerprint": current["block_fingerprint"],
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
