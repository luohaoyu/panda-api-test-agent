#!/usr/bin/env python3
"""Parse local OpenAPI documents and merge static source route candidates."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from scan_model import ScanFinding, ScanGap
from source_scanner import IGNORED_DIRS

_SPEC_NAME = re.compile(r"^(?:openapi|swagger).*\.(json|ya?ml)$", re.IGNORECASE)
_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head", "trace"}


def _load_spec(path: Path) -> tuple[Any | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        return None, f"无法读取契约文件：{exc}"
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text), None
        except json.JSONDecodeError as exc:
            return None, f"JSON 解析失败：{exc.msg}"
    try:
        import yaml
    except ImportError:
        return None, "解析 YAML OpenAPI 需要 PyYAML，但当前环境未安装。"
    try:
        return yaml.safe_load(text), None
    except Exception as exc:
        return None, f"YAML 解析失败：{exc}"


def _parameter_view(parameters: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in parameters if isinstance(parameters, list) else []:
        if not isinstance(item, dict):
            continue
        result.append({key: item[key] for key in ("name", "in", "required") if key in item})
    return result


def scan_contracts(root: Path, source_findings: list[ScanFinding]) -> tuple[list[ScanFinding], list[ScanGap]]:
    root = root.resolve()
    findings: list[ScanFinding] = []
    gaps: list[ScanGap] = []
    spec_files = [
        path for path in root.rglob("*")
        if path.is_file() and _SPEC_NAME.match(path.name)
        and not any(part in IGNORED_DIRS for part in path.relative_to(root).parts)
    ]
    indexed: dict[tuple[str, str], ScanFinding] = {}
    for path in spec_files:
        document, error = _load_spec(path)
        relative = path.relative_to(root).as_posix()
        if error:
            gaps.append(ScanGap(code="contract_parse_failed", scope="contracts", message=error, evidence=relative))
            continue
        if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
            gaps.append(ScanGap(code="contract_paths_missing", scope="contracts", message="契约文件缺少 OpenAPI paths 对象。", evidence=relative))
            continue
        for route, path_item in document["paths"].items():
            if not isinstance(path_item, dict):
                continue
            common_parameters = _parameter_view(path_item.get("parameters"))
            for method, operation in path_item.items():
                if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                    continue
                details = {
                    "operationId": operation.get("operationId"),
                    "parameters": common_parameters + _parameter_view(operation.get("parameters")),
                    "requestBody": operation.get("requestBody"),
                    "responses": operation.get("responses", {}),
                }
                finding = ScanFinding(
                    kind="contract_candidate", file=relative, line=1, method=method.upper(), path_pattern=str(route),
                    status="discovered", source="openapi", confidence="high",
                    evidence=f"paths.{route}.{method.lower()}", details={**details, "candidate_only": True, "semantic_decision_owner": "ai_skill", "deployment_verified": False},
                )
                findings.append(finding)
                indexed[(method.upper(), str(route))] = finding
    for route in source_findings:
        key = (route.method or "", route.path_pattern or "")
        if key in indexed:
            indexed[key].status = "inferred"
            indexed[key].source = "openapi+source"
            indexed[key].details.setdefault("source_candidates", []).append({
                "file": route.file, "line": route.line, "source": route.source,
                "confidence": route.confidence, "evidence": route.evidence,
            })
        else:
            findings.append(ScanFinding(
                kind="contract_candidate", file=route.file, line=route.line, method=route.method,
                path_pattern=route.path_pattern, status="candidate", source=route.source,
                confidence=route.confidence, evidence=route.evidence,
                details={"origin": "source_route_candidate", "candidate_only": True, "semantic_decision_owner": "ai_skill", "deployment_verified": False},
            ))
    if not spec_files:
        gaps.append(ScanGap(code="openapi_not_found", scope="contracts", message="未发现 openapi*/swagger* JSON 或 YAML 文件。"))
    if not findings:
        gaps.append(ScanGap(code="contracts_not_discovered", scope="contracts", message="未从契约或源码发现接口候选。"))
    return findings, gaps
