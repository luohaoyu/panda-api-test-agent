#!/usr/bin/env python3
"""Dispatch built-in unified oracle tasks by explicit oracle_type."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from db_oracle import db_task_executor
from redis_oracle import redis_task_executor


class CompositeOracleExecutorError(ValueError):
    pass


def oracle_type_from_task(task: dict[str, Any]) -> str:
    oracle_type = task.get("oracle_type")
    if not oracle_type and isinstance(task.get("oracle"), dict):
        oracle_type = task["oracle"].get("kind")
    return str(oracle_type or "").strip().lower()


def composite_oracle_executor(
    *,
    evidence_dir: str | Path | None = None,
    redis_client_factory=None,
):
    db_executor = db_task_executor(evidence_dir=evidence_dir)
    redis_executor = redis_task_executor(
        evidence_dir=evidence_dir,
        client_factory=redis_client_factory,
    )

    def _execute(task: dict[str, Any], runtime_context: Any = None, **kwargs) -> dict[str, Any]:
        if str(task.get("type") or task.get("task_type") or "").lower() != "oracle":
            raise CompositeOracleExecutorError(
                f"composite oracle executor only handles oracle tasks, got: {task.get('task_id')}"
            )
        oracle_type = oracle_type_from_task(task)
        if oracle_type == "db":
            return db_executor(task, runtime_context=runtime_context)
        if oracle_type == "redis":
            return redis_executor(task, runtime_context=runtime_context, **kwargs)
        raise CompositeOracleExecutorError(
            f"oracle task {task.get('task_id')} has no built-in executor for oracle_type={oracle_type or '<free-form>'}"
        )

    return _execute
