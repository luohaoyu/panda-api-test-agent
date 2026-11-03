#!/usr/bin/env python3
"""Resolve declared RuntimeContext values from canonical project sources."""
import ast
from pathlib import Path
import re

from environment_secret_store import read_environment_secrets
from runtime_context import binding_specs, package_binding_keys, package_secret_keys
from runtime_secret_bridge import project_root_for_package
from runner import TEST_CONFIG_BLOCK_PATTERN


class RuntimeValueResolutionError(ValueError):
    """Raised when runtime values cannot be resolved from approved sources."""


_MAPPING_LINE = re.compile(r"^(?P<indent> *)(?P<key>(?:[^:#'\"]+|\"(?:[^\"\\]|\\.)*\"|'[^']*')+):(?P<value>.*)$")


def _strip_comment(value):
    quote = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def _scalar(value, field):
    text = _strip_comment(value).strip()
    if not text:
        return None
    if text[0] in {"'", '"'}:
        if len(text) < 2 or text[-1] != text[0]:
            raise RuntimeValueResolutionError(f"{field} has an unterminated quoted scalar")
        try:
            return ast.literal_eval(text) if text[0] == '"' else text[1:-1].replace("''", "'")
        except (ValueError, SyntaxError) as exc:
            raise RuntimeValueResolutionError(f"{field} has an invalid quoted scalar") from exc
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if text.startswith("[") and text.endswith("]"):
        items = [_scalar(item, field) for item in text[1:-1].split(",") if item.strip()]
        return ",".join(str(item) for item in items)
    if any(token in text for token in ("{", "}", "[", "]", "&", "*", "!", "|", ">")):
        raise RuntimeValueResolutionError(f"{field} uses an unsupported YAML scalar")
    return text


def _mapping_lines(body):
    parsed = []
    for number, raw_line in enumerate(body.splitlines(), 1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise RuntimeValueResolutionError("test-config indentation must use spaces")
        match = _MAPPING_LINE.match(raw_line)
        if not match:
            continue
        raw_key = match.group("key").strip()
        key = _scalar(raw_key, f"test-config line {number} key")
        if not isinstance(key, str) or not key:
            raise RuntimeValueResolutionError(f"test-config line {number} has an invalid mapping key")
        parsed.append((len(match.group("indent")), key, match.group("value"), number))
    return parsed


def _environment_block(body, environment_id):
    lines = _mapping_lines(body)
    environments = next((item for item in lines if item[0] == 0 and item[1] == "environments"), None)
    if environments is None:
        raise RuntimeValueResolutionError("test-config environments mapping is required")
    environments_index = lines.index(environments)
    env_indent = None
    start = None
    for index in range(environments_index + 1, len(lines)):
        indent, key, raw_value, _number = lines[index]
        if indent <= environments[0]:
            break
        if env_indent is None:
            env_indent = indent
        if indent == env_indent and key == environment_id and _scalar(raw_value, f"environment {key}") is None:
            if start is not None:
                raise RuntimeValueResolutionError(f"test-config contains duplicate environment: {environment_id}")
            start = index
        elif start is not None and indent == env_indent:
            return lines[start:index]
    if start is None:
        raise RuntimeValueResolutionError(f"test-config environment is not declared: {environment_id}")
    return lines[start:]


def _config_values(config_path, environment_id, package_specs):
    try:
        raw = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeValueResolutionError("canonical docs/test-config.md could not be read") from exc
    blocks = list(TEST_CONFIG_BLOCK_PATTERN.finditer(raw))
    if len(blocks) != 1:
        raise RuntimeValueResolutionError(
            "TEST_CONFIG_SCHEMA_UNSUPPORTED: test-config.md requires exactly one yaml test-config block"
        )
    block = _environment_block(blocks[0].group("body"), environment_id)
    values = {}
    stack = []
    for index, (indent, key, raw_value, number) in enumerate(block):
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        if key != "key":
            continue
        binding_key = _scalar(raw_value, f"test-config line {number} key")
        if binding_key not in package_specs:
            continue
        parent_indent = max(
            (block[candidate][0] for candidate in range(index - 1, -1, -1) if block[candidate][0] < indent),
            default=-1,
        )
        sibling_limit = len(block)
        for candidate in range(index + 1, len(block)):
            if block[candidate][0] <= parent_indent:
                sibling_limit = candidate
                break
        fields = {}
        for child_indent, child_key, child_value, child_number in block[index + 1 : sibling_limit]:
            if child_indent != indent:
                continue
            if child_key in fields:
                raise RuntimeValueResolutionError(
                    f"test-config binding {binding_key} repeats field {child_key}"
                )
            fields[child_key] = _scalar(
                child_value, f"test-config binding {binding_key}.{child_key} line {child_number}"
            )
        declared_secret = package_specs[binding_key]["secret"]
        if fields.get("secret") is not declared_secret:
            raise RuntimeValueResolutionError(
                f"test-config binding {binding_key} secret classification does not match the package"
            )
        if declared_secret:
            if "value" in fields:
                raise RuntimeValueResolutionError(
                    f"secret binding {binding_key} must not define value in test-config"
                )
            continue
        if "value" in fields:
            if binding_key in values:
                raise RuntimeValueResolutionError(
                    f"test-config contains duplicate binding value: {binding_key}"
                )
            if fields["value"] is None:
                raise RuntimeValueResolutionError(
                    f"test-config binding {binding_key} value must not be null"
                )
            values[binding_key] = fields["value"]
    return values


def resolve_runtime_values(
    package_path,
    package,
    *,
    runtime_values=None,
    run_secret_values=None,
):
    """Resolve config values, persistent Secrets, run Secrets, then explicit overrides."""
    explicit = {} if runtime_values is None else dict(runtime_values)
    run_secrets = {} if run_secret_values is None else dict(run_secret_values)
    declared = package_binding_keys(package)
    secret_keys = package_secret_keys(package)
    unknown_explicit = sorted(set(explicit) - declared)
    if unknown_explicit:
        raise RuntimeValueResolutionError(
            "undeclared runtime values are forbidden: " + ", ".join(unknown_explicit)
        )
    unknown_run = sorted(set(run_secrets) - secret_keys)
    if unknown_run:
        raise RuntimeValueResolutionError(
            "run Secret override contains non-secret or undeclared bindings: " + ", ".join(unknown_run)
        )

    root = project_root_for_package(package_path)
    config_path = (root / "docs" / "test-config.md").resolve(strict=False)
    try:
        config_path.relative_to(root)
    except ValueError as exc:
        raise RuntimeValueResolutionError("canonical test-config path escapes the project root") from exc
    config_ref = package.get("test_config") or {}
    configured_path = config_ref.get("path")
    if configured_path and configured_path != "docs/test-config.md":
        try:
            configured_file = Path(configured_path).resolve(strict=False)
        except (OSError, TypeError) as exc:
            raise RuntimeValueResolutionError("test_config.path must identify docs/test-config.md") from exc
        if configured_file != config_path:
            raise RuntimeValueResolutionError("test_config.path must identify docs/test-config.md")
    environment_id = (package.get("environment") or {}).get("environment_id")
    if not isinstance(environment_id, str) or not environment_id:
        raise RuntimeValueResolutionError("package environment_id is required")
    specs = {spec["key"]: spec for spec in binding_specs(package)}
    resolved = _config_values(config_path, environment_id, specs)
    resolved.update(read_environment_secrets(package_path, package))
    secret_collisions = sorted((set(run_secrets) & set(explicit)) & secret_keys)
    if secret_collisions:
        raise RuntimeValueResolutionError(
            "run Secret override bindings must not also be supplied explicitly: " + ", ".join(secret_collisions)
        )
    resolved.update(run_secrets)
    resolved.update(explicit)
    unknown_resolved = sorted(set(resolved) - declared)
    if unknown_resolved:
        raise RuntimeValueResolutionError(
            "resolved undeclared runtime values are forbidden: " + ", ".join(unknown_resolved)
        )
    return resolved
