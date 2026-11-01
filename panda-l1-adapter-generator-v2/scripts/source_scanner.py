#!/usr/bin/env python3
"""Read source text and discover simple HTTP route candidates without importing it."""
from __future__ import annotations

import re
from pathlib import Path

from scan_model import ScanFinding, ScanGap

IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "node_modules", "target",
    "build", "dist", "__pycache__", ".pytest_cache", ".mypy_cache", "venv", ".venv",
}
SOURCE_SUFFIXES = {".java", ".js", ".jsx", ".ts", ".tsx", ".py"}

_JAVA_ANNOTATION = re.compile(r"@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)\s*(?:\((.*?)\))?", re.DOTALL)
_EXPRESS_ROUTE = re.compile(r"\brouter\s*\.\s*(get|post|put|delete|patch|options|head|all)\s*\(\s*([\"'`])([^\"'`]+)\2", re.IGNORECASE)
_FASTAPI_ROUTE = re.compile(r"@app\s*\.\s*(get|post|put|delete|patch|options|head)\s*\(\s*([\"'])([^\"']+)\2", re.IGNORECASE)


def iter_source_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _java_path(args: str | None) -> str:
    if not args:
        return "/"
    named = re.search(r"(?:value|path)\s*=\s*(?:new\s+String\[\]\s*)?\{?\s*[\"']([^\"']+)", args)
    quoted = re.search(r"[\"']([^\"']+)[\"']", args)
    return (named or quoted).group(1) if (named or quoted) else "/"


def _java_method(annotation: str, args: str | None) -> str:
    if annotation != "RequestMapping":
        return annotation.removesuffix("Mapping").upper()
    match = re.search(r"RequestMethod\.(GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)", args or "")
    return match.group(1) if match else "*"


def _join_path(prefix: str, path: str) -> str:
    if prefix in ("", "/"):
        return path if path.startswith("/") else "/" + path
    if path == "/":
        return prefix if prefix.startswith("/") else "/" + prefix
    return "/" + "/".join(part.strip("/") for part in (prefix, path) if part.strip("/"))


def _scan_java(path: Path, root: Path, text: str) -> list[ScanFinding]:
    annotations = list(_JAVA_ANNOTATION.finditer(text))
    class_prefix = ""
    class_annotations: set[int] = set()
    for index, match in enumerate(annotations):
        tail_end = annotations[index + 1].start() if index + 1 < len(annotations) else min(len(text), match.end() + 600)
        declaration = text[match.end():tail_end]
        if re.search(r"\b(?:class|interface)\s+\w+", declaration):
            class_prefix = _java_path(match.group(2))
            class_annotations.add(index)
            break

    findings: list[ScanFinding] = []
    for index, match in enumerate(annotations):
        if index in class_annotations:
            continue
        annotation = match.group(1)
        route_path = _join_path(class_prefix, _java_path(match.group(2)))
        method = _java_method(annotation, match.group(2))
        findings.append(ScanFinding(
            kind="route_candidate", file=_relative(path, root), line=_line(text, match.start()),
            method=method, path_pattern=route_path, status="candidate", source="java_annotation",
            confidence="high" if method != "*" else "medium", evidence=match.group(0).strip(),
            details={"candidate_only": True, "semantic_decision_owner": "ai_skill", "deployment_verified": False},
        ))
    return findings


def _scan_regex(path: Path, root: Path, text: str, pattern: re.Pattern[str], source: str) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    for match in pattern.finditer(text):
        findings.append(ScanFinding(
            kind="route_candidate", file=_relative(path, root), line=_line(text, match.start()),
            method=match.group(1).upper(), path_pattern=match.group(3), status="candidate",
            source=source, confidence="high", evidence=match.group(0).strip(),
            details={"candidate_only": True, "semantic_decision_owner": "ai_skill", "deployment_verified": False},
        ))
    return findings


def scan_source(root: Path) -> tuple[list[ScanFinding], list[ScanGap]]:
    root = root.resolve()
    findings: list[ScanFinding] = []
    for path in iter_source_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.suffix.lower() == ".java":
            findings.extend(_scan_java(path, root, text))
        elif path.suffix.lower() in {".js", ".jsx", ".ts", ".tsx"}:
            findings.extend(_scan_regex(path, root, text, _EXPRESS_ROUTE, "express_router"))
        elif path.suffix.lower() == ".py":
            findings.extend(_scan_regex(path, root, text, _FASTAPI_ROUTE, "fastapi_decorator"))
    gaps = [] if findings else [ScanGap(
        code="source_routes_not_found", scope="source",
        message="未发现受支持框架的简单路由候选；需由 AI 按目标继续理解工程或确认缺口。",
        evidence="脚本仅提供 Java Spring、Express router 和 FastAPI app 的静态候选发现，不代表工程无接口。",
    )]
    return findings, gaps
