#!/usr/bin/env python3
"""Bounded read-only Redis Oracle executor."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runner import assertion_passes, slug

ORACLE_KIND = "redis"
READ_ONLY_COMMANDS = {
    "GET", "MGET", "EXISTS", "TYPE", "TTL", "PTTL", "HGET", "HMGET", "HGETALL",
    "SCARD", "SMEMBERS", "ZSCORE", "ZRANGE", "LRANGE",
}
FORBIDDEN_COMMANDS = {"KEYS", "EVAL", "EVALSHA"}
SUPPORTED_OPERATORS = {"equals", "not_equals", "exists", "all_present", "contains", "in_range"}
VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")
INDEX_SOURCE_PATTERN = re.compile(r"^(?:result|item)\[(\d+)\]$")
DEFAULT_MAX_ITEMS = 100
MAX_ITEMS_LIMIT = 1000
DEFAULT_MAX_BYTES = 1024 * 1024
MAX_BYTES_LIMIT = 10 * 1024 * 1024


class RedisOracleError(ValueError):
    """Static declaration problem; always fail-closed."""


class RedisOracleExecutionError(RuntimeError):
    """Runtime fault while executing an otherwise valid declaration."""


def validate_redis_oracle(oracle: Any) -> dict[str, Any]:
    if not isinstance(oracle, dict):
        raise RedisOracleError("redis oracle declaration must be an object")
    if str(oracle.get("kind", ORACLE_KIND)).lower() != ORACLE_KIND:
        raise RedisOracleError(f"redis oracle requires kind={ORACLE_KIND}")
    oracle_id = oracle.get("oracle_id")
    if not isinstance(oracle_id, str) or not oracle_id.strip():
        raise RedisOracleError("redis oracle requires a non-empty oracle_id")
    binding = oracle.get("binding")
    if not isinstance(binding, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", binding or ""):
        raise RedisOracleError(f"redis oracle {oracle_id} requires a runtime binding key name")
    command = str(oracle.get("command") or "").strip().upper()
    if not command:
        raise RedisOracleError(f"redis oracle {oracle_id} requires a command")
    if command in FORBIDDEN_COMMANDS or command not in READ_ONLY_COMMANDS:
        raise RedisOracleError(f"redis oracle {oracle_id}: command is not allowed: {command}")
    args = oracle.get("args", oracle.get("arguments"))
    if not isinstance(args, list) or not args:
        raise RedisOracleError(f"redis oracle {oracle_id}: args must be a non-empty list")
    if any(isinstance(item, (dict, list, tuple, set)) for item in args):
        raise RedisOracleError(f"redis oracle {oracle_id}: args must contain scalar values")
    max_items = oracle.get("max_items", DEFAULT_MAX_ITEMS)
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= MAX_ITEMS_LIMIT:
        raise RedisOracleError(f"redis oracle {oracle_id}: max_items must be an integer within 1..{MAX_ITEMS_LIMIT}")
    max_bytes = oracle.get("max_bytes", DEFAULT_MAX_BYTES)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_BYTES_LIMIT:
        raise RedisOracleError(f"redis oracle {oracle_id}: max_bytes must be an integer within 1..{MAX_BYTES_LIMIT}")
    raw_assertions = oracle.get("assertions")
    if not isinstance(raw_assertions, list) or not raw_assertions:
        raise RedisOracleError(f"redis oracle {oracle_id} requires at least one assertion")
    assertions = []
    for index, item in enumerate(raw_assertions, 1):
        if not isinstance(item, dict):
            raise RedisOracleError(f"redis oracle {oracle_id} assertion #{index} must be an object")
        assertion_id = item.get("assertion_id") or item.get("id")
        if not isinstance(assertion_id, str) or not assertion_id.strip():
            raise RedisOracleError(f"redis oracle {oracle_id} assertion #{index} requires assertion_id")
        source = item.get("source", "result")
        if source not in {"result", "value", "item_count"} and not (
            isinstance(source, str) and (INDEX_SOURCE_PATTERN.fullmatch(source) or source.startswith("field."))
        ):
            raise RedisOracleError(
                f"redis oracle {oracle_id} assertion {assertion_id}: unsupported source {source!r}"
            )
        operator = item.get("operator")
        if operator not in SUPPORTED_OPERATORS:
            raise RedisOracleError(
                f"redis oracle {oracle_id} assertion {assertion_id}: unsupported operator {operator!r}"
            )
        if "expected" not in item:
            raise RedisOracleError(f"redis oracle {oracle_id} assertion {assertion_id}: expected is required")
        assertions.append({
            "assertion_id": assertion_id,
            "source": source,
            "operator": operator,
            "expected": item.get("expected"),
        })
    if oracle.get("side_effect", "read") not in {"none", "read"}:
        raise RedisOracleError(f"redis oracle {oracle_id} must be read-only")
    return {
        "oracle_id": oracle_id,
        "kind": ORACLE_KIND,
        "binding": binding,
        "command": command,
        "args": list(args),
        "max_items": max_items,
        "max_bytes": max_bytes,
        "assertions": assertions,
        "side_effect": "read",
    }


def declaration_from_task(task: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(task, dict) or str(task.get("type") or task.get("task_type") or "").lower() != "oracle":
        return None
    oracle_type = task.get("oracle_type")
    if not oracle_type and isinstance(task.get("oracle"), dict):
        oracle_type = task["oracle"].get("kind")
    if str(oracle_type or "").lower() != ORACLE_KIND:
        return None
    if isinstance(task.get("oracle"), dict):
        return task["oracle"]
    if isinstance(task.get("verification"), dict):
        return task["verification"]
    return {key: value for key, value in task.items() if key not in {"type", "task_type"}}


def _resolve_args(args: list[Any], runtime_context: Any) -> list[Any]:
    def _resolve(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        full = VARIABLE_PATTERN.fullmatch(value)
        if full:
            name = full.group(1)
            if not runtime_context.has(name):
                raise RedisOracleExecutionError(f"unknown runtime variable in redis oracle: ${{{name}}}")
            return runtime_context.require(name)

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if not runtime_context.has(name):
                raise RedisOracleExecutionError(f"unknown runtime variable in redis oracle: ${{{name}}}")
            return str(runtime_context.require(name))

        return VARIABLE_PATTERN.sub(_replace, value)

    return [_resolve(item) for item in args]


def _default_client_factory(binding_value: Any):
    try:
        import redis  # type: ignore
    except ImportError as exc:
        raise RedisOracleExecutionError("redis oracle driver is not installed: redis") from exc
    if isinstance(binding_value, Mapping):
        config = dict(binding_value)
        url = config.pop("url", None) or config.pop("dsn", None)
        config.pop("driver", None)
        config.pop("type", None)
        config.setdefault("decode_responses", True)
        if url:
            return redis.Redis.from_url(str(url), **config)
        if "database" in config and "db" not in config:
            config["db"] = config.pop("database")
        return redis.Redis(**config)
    return redis.Redis.from_url(str(binding_value), decode_responses=True)


def _plain(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {_plain(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_plain(item) for item in value]
        return sorted(items, key=lambda item: str(item)) if isinstance(value, (set, frozenset)) else items
    return value


def _item_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (str, bytes)):
        return 1
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return len(value)
    return 1


def _source_value(source: str, result: Any) -> Any:
    if source in {"result", "value"}:
        return result
    if source == "item_count":
        return _item_count(result)
    match = INDEX_SOURCE_PATTERN.fullmatch(source or "")
    if match:
        index = int(match.group(1))
        return result[index] if isinstance(result, list) and index < len(result) else None
    if source.startswith("field."):
        return result.get(source.removeprefix("field.")) if isinstance(result, Mapping) else None
    raise RedisOracleExecutionError(f"unsupported redis oracle assertion source: {source}")


def execute_redis_oracle(
    oracle: dict[str, Any],
    runtime_context: Any,
    *,
    evidence_dir: str | Path | None = None,
    client_factory=None,
) -> dict[str, Any]:
    normalized = validate_redis_oracle(oracle)
    oracle_id = normalized["oracle_id"]
    binding = normalized["binding"]

    def _not_enabled(reason: str) -> dict[str, Any]:
        return {
            "oracle_id": oracle_id,
            "status": "not_enabled",
            "reason": reason,
            "item_count": None,
            "assertion_results": [],
            "evidence_ref": None,
        }

    if runtime_context is None or not runtime_context.has(binding):
        return _not_enabled(f"runtime binding is missing: {binding}")
    try:
        args = _resolve_args(normalized["args"], runtime_context)
    except RedisOracleExecutionError as exc:
        return {"oracle_id": oracle_id, "status": "error", "reason": str(exc), "item_count": None, "assertion_results": [], "evidence_ref": None}
    client = None
    try:
        client = (client_factory or _default_client_factory)(runtime_context.require(binding))
        if hasattr(client, "execute_command"):
            raw_result = client.execute_command(normalized["command"], *args)
        else:
            method = getattr(client, normalized["command"].lower())
            raw_result = method(*args)
    except (ImportError, ConnectionError, OSError, RedisOracleExecutionError) as exc:
        return _not_enabled(f"redis oracle could not connect or execute: {exc}")
    except Exception as exc:
        return _not_enabled(f"redis oracle could not connect or execute: {exc}")
    finally:
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    result = _plain(raw_result)
    item_count = _item_count(result)
    if item_count > normalized["max_items"]:
        return {"oracle_id": oracle_id, "status": "error", "reason": f"redis oracle {oracle_id}: result exceeds max_items={normalized['max_items']}", "item_count": item_count, "assertion_results": [], "evidence_ref": None}
    result_bytes = len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
    if result_bytes > normalized["max_bytes"]:
        return {"oracle_id": oracle_id, "status": "error", "reason": f"redis oracle {oracle_id}: result exceeds max_bytes={normalized['max_bytes']}", "item_count": item_count, "assertion_results": [], "evidence_ref": None}

    assertion_results = []
    for assertion in normalized["assertions"]:
        actual = _source_value(assertion["source"], result)
        passed = assertion_passes(assertion["operator"], assertion["expected"], actual)
        assertion_results.append({
            "assertion_id": assertion["assertion_id"],
            "source": assertion["source"],
            "operator": assertion["operator"],
            "expected": assertion["expected"],
            "actual": actual,
            "status": "passed" if passed else "failed",
        })
    status = "passed" if all(item["status"] == "passed" for item in assertion_results) else "failed"
    evidence_ref = None
    if evidence_dir is not None:
        evidence_path = Path(evidence_dir)
        evidence_path.mkdir(parents=True, exist_ok=True)
        target = evidence_path / f"oracle-{slug(oracle_id)}.json"
        payload = {
            "oracle_id": oracle_id,
            "kind": ORACLE_KIND,
            "binding": binding,
            "command": normalized["command"],
            "args": args,
            "item_count": item_count,
            "result": result,
            "assertion_results": assertion_results,
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        evidence_ref = str(target)
    return {
        "oracle_id": oracle_id,
        "status": status,
        "item_count": item_count,
        "assertion_results": assertion_results,
        "evidence_ref": evidence_ref,
    }


def redis_task_executor(*, evidence_dir: str | Path | None = None, client_factory=None):
    def _execute(task: dict[str, Any], runtime_context: Any = None, **_kwargs) -> dict[str, Any]:
        declaration = declaration_from_task(task)
        if declaration is None:
            raise RedisOracleError(
                f"redis task executor only handles redis oracle tasks, got: {task.get('task_id')}"
            )
        try:
            fact = execute_redis_oracle(
                declaration,
                runtime_context,
                evidence_dir=evidence_dir,
                client_factory=client_factory,
            )
        except RedisOracleError as exc:
            return {
                "task_id": task.get("task_id"),
                "status": "error",
                "evidence_ref": None,
                "item_count": None,
                "assertion_results": [],
                "reason": str(exc),
            }
        return {
            "task_id": task.get("task_id"),
            "status": fact["status"],
            "evidence_ref": fact.get("evidence_ref"),
            "item_count": fact.get("item_count"),
            "assertion_results": fact.get("assertion_results"),
            "reason": fact.get("reason"),
        }

    return _execute
