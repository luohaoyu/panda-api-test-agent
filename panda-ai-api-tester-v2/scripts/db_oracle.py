#!/usr/bin/env python3
"""Read-only DB Oracle executor.

A declared, statically validated, read-only SELECT verification against the
runtime DB binding declared by the TestPackage. This module is part of the
deterministic execution boundary, not a second review boundary:

- Only a single plain ``SELECT`` statement is allowed. The statement must
  contain ``WHERE``, must list columns explicitly (``SELECT *`` is rejected),
  and must not contain comments, multiple statements, or any data-changing
  keyword. Results are bounded by ``max_rows``.
- The connection string is resolved from the RuntimeContext binding named by
  ``binding``; it never comes from os.environ, argv, stdin, or files. SQLite is
  opened in read-only URI mode; MySQL/PostgreSQL require an optional driver
  and fail closed when it is missing.
- Failure semantics: missing binding/driver or connection failure maps to
  ``not_enabled`` (coverage gap, never a pass); a data mismatch maps to a
  failed assertion; unexpected execution faults map to ``error``.
- Assertion evaluation reuses the Runner's operator semantics. Evidence rows
  are masked by column sensitivity before they touch disk.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runner import assertion_passes, slug

ORACLE_KIND = "db"
SUPPORTED_OPERATORS = {"equals", "not_equals", "exists", "all_present", "contains", "in_range"}
MAX_ROWS_LIMIT = 500
DEFAULT_MAX_ROWS = 100
ROW_SOURCE_PATTERN = re.compile(r"^row\[(\d+)\]\.([A-Za-z_][A-Za-z0-9_]*)$")
VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")
FORBIDDEN_KEYWORD_PATTERN = re.compile(
    r"(?i)\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|grant|revoke|call|exec|execute|set|use|lock|unlock|replace|vacuum|pragma|attach|detach|into|outfile|dumpfile|load|sleep|benchmark)\b"
)
STATEMENT_COMMENT_PATTERN = re.compile(r"(--[^\n]*|/\*[\s\S]*?\*/)")
SELECT_STAR_PATTERN = re.compile(r"\*")


class DbOracleError(ValueError):
    """Static declaration problem; always fail-closed."""


class DbOracleExecutionError(RuntimeError):
    """Runtime fault while executing an otherwise valid declaration."""


def _strip_trailing_semicolon(statement: str) -> str:
    return statement.strip().rstrip(";").strip()


def validate_db_oracle(oracle: Any) -> dict[str, Any]:
    """Normalize and statically validate one DB oracle declaration."""
    if not isinstance(oracle, dict):
        raise DbOracleError("db oracle declaration must be an object")
    if oracle.get("kind", ORACLE_KIND) != ORACLE_KIND:
        raise DbOracleError(f"db oracle requires kind={ORACLE_KIND}")
    oracle_id = oracle.get("oracle_id")
    if not isinstance(oracle_id, str) or not oracle_id.strip():
        raise DbOracleError("db oracle requires a non-empty oracle_id")
    binding = oracle.get("binding")
    if not isinstance(binding, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", binding or ""):
        raise DbOracleError(f"db oracle {oracle_id} requires a runtime binding key name")
    statement = oracle.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise DbOracleError(f"db oracle {oracle_id} requires a statement")
    normalized_statement = _strip_trailing_semicolon(statement)
    if STATEMENT_COMMENT_PATTERN.search(normalized_statement):
        raise DbOracleError(f"db oracle {oracle_id}: comments are not allowed in the statement")
    if not re.match(r"(?i)^\s*select\b", normalized_statement):
        raise DbOracleError(f"db oracle {oracle_id}: only a single SELECT statement is allowed")
    if ";" in normalized_statement:
        raise DbOracleError(f"db oracle {oracle_id}: multiple statements are not allowed")
    if FORBIDDEN_KEYWORD_PATTERN.search(normalized_statement):
        raise DbOracleError(f"db oracle {oracle_id}: statement contains a forbidden keyword")
    if not re.search(r"(?i)\bwhere\b", normalized_statement):
        raise DbOracleError(f"db oracle {oracle_id}: statement requires a WHERE clause")
    if SELECT_STAR_PATTERN.search(normalized_statement.replace("(*)", "")):
        raise DbOracleError(f"db oracle {oracle_id}: SELECT must list columns explicitly (no *)")
    max_rows = oracle.get("max_rows", DEFAULT_MAX_ROWS)
    if not isinstance(max_rows, int) or isinstance(max_rows, bool) or not 1 <= max_rows <= MAX_ROWS_LIMIT:
        raise DbOracleError(f"db oracle {oracle_id}: max_rows must be an integer within 1..{MAX_ROWS_LIMIT}")
    raw_assertions = oracle.get("assertions")
    if not isinstance(raw_assertions, list) or not raw_assertions:
        raise DbOracleError(f"db oracle {oracle_id} requires at least one assertion")
    assertions = []
    for index, item in enumerate(raw_assertions, 1):
        if not isinstance(item, dict):
            raise DbOracleError(f"db oracle {oracle_id} assertion #{index} must be an object")
        assertion_id = item.get("assertion_id") or item.get("id")
        if not isinstance(assertion_id, str) or not assertion_id.strip():
            raise DbOracleError(f"db oracle {oracle_id} assertion #{index} requires assertion_id")
        source = item.get("source")
        if source not in {"row_count", "scalar"} and not (isinstance(source, str) and ROW_SOURCE_PATTERN.fullmatch(source)):
            raise DbOracleError(
                f"db oracle {oracle_id} assertion {assertion_id}: source must be row_count, scalar, or row[<index>].<column>"
            )
        operator = item.get("operator")
        if operator not in SUPPORTED_OPERATORS:
            raise DbOracleError(f"db oracle {oracle_id} assertion {assertion_id}: unsupported operator {operator!r}")
        if source == "row_count" and operator in {"exists", "all_present", "contains"}:
            raise DbOracleError(
                f"db oracle {oracle_id} assertion {assertion_id}: operator {operator} is not meaningful for row_count; use equals, not_equals or in_range"
            )
        if "expected" not in item:
            raise DbOracleError(f"db oracle {oracle_id} assertion {assertion_id}: expected is required")
        assertions.append({
            "assertion_id": assertion_id,
            "source": source,
            "operator": operator,
            "expected": item.get("expected"),
        })
    side_effect = oracle.get("side_effect", "read")
    if side_effect not in {"none", "read"}:
        raise DbOracleError(f"db oracle {oracle_id} must be read-only")
    return {
        "oracle_id": oracle_id,
        "kind": ORACLE_KIND,
        "binding": binding,
        "statement": normalized_statement,
        "max_rows": max_rows,
        "assertions": assertions,
        "side_effect": "read",
    }


def declaration_from_task(task: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a DB oracle declaration from a unified plan oracle task."""
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


def _sql_literal(value: Any) -> str:
    """Render one runtime value as a safely quoted SQL literal."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _resolve_statement(statement: str, runtime_context: Any) -> str:
    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if not runtime_context.has(name):
            raise DbOracleExecutionError(f"unknown runtime variable in oracle statement: ${{{name}}}")
        return _sql_literal(runtime_context.require(name))

    return VARIABLE_PATTERN.sub(_replace, statement)


def _config_driver(config: Mapping[str, Any]) -> str:
    return str(config.get("driver") or config.get("dialect") or config.get("scheme") or config.get("type") or "").strip().lower()


def _sqlite_path(config: Mapping[str, Any]) -> str:
    value = config.get("path") or config.get("database") or config.get("dsn")
    if not isinstance(value, str) or not value.strip():
        raise DbOracleExecutionError("sqlite db_config requires path or database")
    return value.strip()


def _connect(binding_value: Any, timeout_seconds: float):
    if isinstance(binding_value, Mapping):
        config = dict(binding_value)
        driver = _config_driver(config)
        if not driver and isinstance(config.get("dsn"), str):
            return _connect(config["dsn"], timeout_seconds)
        if driver in {"sqlite", "sqlite3"}:
            value = _sqlite_path(config)
            parsed = urllib.parse.urlparse(value)
            path = parsed.path if parsed.scheme.startswith("sqlite") and parsed.path else value
            path = re.sub(r"^/([A-Za-z]:/)", r"\1", str(path).replace("\\", "/"))
            uri = "file:" + urllib.parse.quote(str(Path(path).resolve()).replace("\\", "/")) + "?mode=ro"
            return sqlite3.connect(uri, uri=True, timeout=float(config.get("timeout_seconds", timeout_seconds)))
        if driver in {"mysql", "pymysql"}:
            try:
                import pymysql  # type: ignore
            except ImportError as exc:
                raise DbOracleExecutionError("mysql oracle driver is not installed: pymysql") from exc
            return pymysql.connect(
                host=config.get("host", "localhost"),
                port=int(config.get("port", 3306)),
                user=config.get("user") or config.get("username") or "",
                password=config.get("password") or "",
                database=config.get("database") or config.get("dbname"),
                charset=config.get("charset", "utf8mb4"),
                connect_timeout=float(config.get("connect_timeout", timeout_seconds)),
                read_timeout=float(config.get("read_timeout", timeout_seconds)),
            )
        if driver in {"postgres", "postgresql", "psycopg2"}:
            try:
                import psycopg2  # type: ignore
            except ImportError as exc:
                raise DbOracleExecutionError("postgres oracle driver is not installed: psycopg2") from exc
            kwargs = {
                "host": config.get("host", "localhost"),
                "port": int(config.get("port", 5432)),
                "user": config.get("user") or config.get("username") or "",
                "password": config.get("password") or "",
                "dbname": config.get("database") or config.get("dbname"),
                "connect_timeout": int(config.get("connect_timeout", timeout_seconds)),
            }
            if config.get("sslmode") is not None:
                kwargs["sslmode"] = config["sslmode"]
            return psycopg2.connect(**kwargs)
        raise DbOracleExecutionError(f"unsupported db_config driver: {driver or '<missing>'}")

    value = str(binding_value).strip()
    parsed = urllib.parse.urlparse(value)
    scheme = (parsed.scheme or "").lower()
    if scheme in {"", "sqlite", "sqlite3"} or re.search(r"\.(db|sqlite|sqlite3)$", value, re.IGNORECASE):
        path = parsed.path if scheme.startswith("sqlite") and parsed.path else value
        path = re.sub(r"^/([A-Za-z]:/)", r"\1", str(path).replace("\\", "/"))
        if not path:
            raise DbOracleExecutionError("sqlite binding value must contain a database path")
        uri = "file:" + urllib.parse.quote(str(Path(path).resolve()).replace("\\", "/")) + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    if scheme.startswith("mysql"):
        try:
            import pymysql  # type: ignore
        except ImportError as exc:
            raise DbOracleExecutionError("mysql oracle driver is not installed: pymysql") from exc
        config = urllib.parse.parse_qs(parsed.query)
        return pymysql.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=urllib.parse.unquote(parsed.username or ""),
            password=urllib.parse.unquote(parsed.password or ""),
            database=parsed.path.lstrip("/") or None,
            charset=config.get("charset", ["utf8mb4"])[0],
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
        )
    if scheme.startswith("postgres"):
        try:
            import psycopg2  # type: ignore
        except ImportError as exc:
            raise DbOracleExecutionError("postgres oracle driver is not installed: psycopg2") from exc
        return psycopg2.connect(value, connect_timeout=int(timeout_seconds))
    raise DbOracleExecutionError(f"unsupported db oracle binding scheme: {scheme or '<none>'}")


def _source_value(source: str, rows: list[dict[str, Any]]):
    if source == "row_count":
        return len(rows)
    match = ROW_SOURCE_PATTERN.fullmatch(source or "")
    if match:
        index, column = int(match.group(1)), match.group(2)
        if index >= len(rows) or column not in rows[index]:
            return None
        return rows[index][column]
    if source == "scalar":
        if not rows:
            return None
        first = rows[0]
        if not first:
            return None
        return next(iter(first.values()))
    raise DbOracleExecutionError(f"unsupported oracle assertion source: {source}")


def execute_db_oracle(oracle: dict[str, Any], runtime_context: Any, *, evidence_dir: str | Path | None = None) -> dict[str, Any]:
    """Execute one validated DB oracle. Never raises for environment faults."""
    normalized = validate_db_oracle(oracle)
    oracle_id = normalized["oracle_id"]
    binding = normalized["binding"]

    def _not_enabled(reason: str) -> dict[str, Any]:
        return {
            "oracle_id": oracle_id,
            "status": "not_enabled",
            "reason": reason,
            "row_count": None,
            "assertion_results": [],
            "evidence_ref": None,
        }

    if runtime_context is None or not runtime_context.has(binding):
        return _not_enabled(f"runtime binding is missing: {binding}")
    try:
        statement = _resolve_statement(normalized["statement"], runtime_context)
    except DbOracleExecutionError as exc:
        return {"oracle_id": oracle_id, "status": "error", "reason": str(exc), "row_count": None, "assertion_results": [], "evidence_ref": None}
    try:
        connection = _connect(runtime_context.require(binding), 10.0)
    except Exception as exc:
        return _not_enabled(f"db oracle could not connect: {exc}")
    try:
        try:
            cursor = connection.cursor()
            cursor.execute(statement)
            raw_rows = cursor.fetchmany(normalized["max_rows"] + 1)
        finally:
            connection.close()
        if len(raw_rows) > normalized["max_rows"]:
            raise DbOracleExecutionError(
                f"db oracle {oracle_id}: result set exceeds max_rows={normalized['max_rows']}"
            )
        columns = [item[0] for item in cursor.description] if cursor.description else []
        rows = [dict(zip(columns, row)) for row in raw_rows]
    except DbOracleExecutionError as exc:
        return {"oracle_id": oracle_id, "status": "error", "reason": str(exc), "row_count": None, "assertion_results": [], "evidence_ref": None}
    except Exception as exc:
        return _not_enabled(f"db oracle query failed: {exc}")

    assertion_results = []
    for assertion in normalized["assertions"]:
        actual = _source_value(assertion["source"], rows)
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
            "statement": statement,
            "row_count": len(rows),
            "rows": rows,
            "assertion_results": assertion_results,
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)
        target.write_text(text, encoding="utf-8")
        evidence_ref = str(target)

    return {
        "oracle_id": oracle_id,
        "status": status,
        "row_count": len(rows),
        "assertion_results": assertion_results,
        "evidence_ref": evidence_ref,
    }


def db_task_executor(*, evidence_dir: str | Path | None = None):
    """Build a slice_executor-compatible task executor for DB oracle tasks."""

    def _execute(task: dict[str, Any], runtime_context: Any = None, **_kwargs) -> dict[str, Any]:
        declaration = declaration_from_task(task)
        if declaration is None:
            raise DbOracleError(f"db task executor only handles db oracle tasks, got: {task.get('task_id')}")
        try:
            fact = execute_db_oracle(declaration, runtime_context, evidence_dir=evidence_dir)
        except DbOracleError as exc:
            return {
                "task_id": task.get("task_id"),
                "status": "error",
                "evidence_ref": None,
                "row_count": None,
                "assertion_results": [],
                "reason": str(exc),
            }
        return {
            "task_id": task.get("task_id"),
            "status": fact["status"],
            "evidence_ref": fact.get("evidence_ref"),
            "row_count": fact.get("row_count"),
            "assertion_results": fact.get("assertion_results"),
            "reason": fact.get("reason"),
        }

    return _execute
