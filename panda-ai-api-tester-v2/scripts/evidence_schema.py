#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evidence schema validation (minimum viable request record).

Background: runs r1-r7 of p919_2887 wrote evidence whose ``request`` held only
``{method, path}``, so no request could be reproduced from the evidence and the
report chain stayed open. This module defines the minimum viable raw-request
record and validates evidence files against it.

Minimum viable raw request record (seven elements):
- method, url, path, query, headers, json, requested_at
- ``json`` may be null (GET/DELETE), ``query`` may be an empty object
- ``headers`` are validated for structure only and recorded as-is: credential
  masking is NOT enforced here. SKILL.md §凭据与运行值 already mandates that
  credentials stay in run artifacts verbatim ("按原样保留、不再遮蔽") while
  never entering cross-run assets (canonical config, TestPackage, profiles).
- multi-step evidence (``steps``): every step that carries a ``request`` is
  validated with the same rule; steps without a request are skipped
"""
import json
from pathlib import Path
from typing import Any

REQUIRED_REQUEST_FIELDS = {"method", "url", "path", "query", "headers", "json", "requested_at"}


def validate_request_record(req: Any, where: str) -> list[str]:
    """Validate one raw request record; return a list of problems (empty = valid)."""
    problems: list[str] = []
    if not isinstance(req, dict):
        return [f"{where}: request must be an object"]
    missing = REQUIRED_REQUEST_FIELDS - set(req)
    if missing:
        problems.append(
            f"{where}: request is missing required fields {sorted(missing)}; "
            f"minimum viable raw request = {sorted(REQUIRED_REQUEST_FIELDS)}")
    headers = req.get("headers")
    if not isinstance(headers, dict):
        problems.append(f"{where}: request.headers must be an object")
    if not isinstance(req.get("requested_at"), str) or not req.get("requested_at"):
        problems.append(f"{where}: request.requested_at must be a non-empty ISO8601 timestamp")
    if not isinstance(req.get("method"), str) or not req.get("method"):
        problems.append(f"{where}: request.method must be a non-empty string")
    if not isinstance(req.get("url"), str) or not req.get("url"):
        problems.append(f"{where}: request.url must be a non-empty string")
    return problems


def validate_evidence_doc(doc: Any, where: str = "evidence") -> list[str]:
    """Validate one evidence document; return a list of problems (empty = valid).

    Documents without ``request`` and without ``steps`` are out of scope here
    (e.g. not_run notes); the caller decides whether that is acceptable.
    """
    if not isinstance(doc, dict):
        return [f"{where}: evidence must be a JSON object"]
    problems: list[str] = []
    if "request" in doc:
        problems.extend(validate_request_record(doc.get("request"), where))
    for index, step in enumerate(doc.get("steps") or []):
        if isinstance(step, dict) and "request" in step:
            problems.extend(validate_request_record(step.get("request"), f"{where}.steps[{index}]"))
    return problems


def validate_evidence_file(path: str | Path) -> list[str]:
    """Validate one evidence file on disk; return a list of problems (empty = valid)."""
    p = Path(path)
    if not p.is_file():
        return [f"{p}: evidence file is missing"]
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{p}: evidence is not readable JSON ({exc})"]
    return validate_evidence_doc(doc, str(p))
