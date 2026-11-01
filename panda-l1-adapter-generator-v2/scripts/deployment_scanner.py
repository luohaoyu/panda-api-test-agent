#!/usr/bin/env python3
"""Read deployment/configuration text and report non-secret candidates only."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from scan_model import ScanFinding, ScanGap
from source_scanner import IGNORED_DIRS

_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_PORT = re.compile(r"(?im)^\s*(?:server\.)?port\s*[:=]\s*[\"']?(\d{2,5})\b|(?:^|\s)-?\s*[\"']?(\d{2,5})(?::\d{2,5})?[\"']?\s*(?:#.*)?$")
_EXPOSE_PORT = re.compile(r"(?im)^\s*EXPOSE\s+(\d{2,5})\b")
_ENV_KEY = re.compile(r"(?m)^\s*(?:(?:export|ENV|ARG)\s+)?([A-Z][A-Z0-9_]*)\s*[:=]")
_YAML_ENV_KEY = re.compile(r"(?m)^\s*([A-Z][A-Z0-9_]*)\s*:")
_PLACEHOLDER_KEY = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::[^}]*)?\}")
_SECRET_ASSIGNMENT_KEY = re.compile(r"(?im)^\s*([A-Za-z][A-Za-z0-9_.-]*(?:secret|password|passwd|token|api[_-]?key|private[_-]?key|credential|cookie)[A-Za-z0-9_.-]*)\s*[:=]")
_SECRET_NAME = re.compile(r"(?:SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|PRIVATE_KEY|CREDENTIAL|COOKIE)", re.IGNORECASE)
_SERVICE_HEADER = re.compile(r"(?m)^\s{2}([A-Za-z0-9_.-]+):\s*(?:#.*)?$")
_SERVICE_NAME = re.compile(r"(?im)^\s*(?:spring\.application\.name|application[_-]?name|service[_-]?name)\s*[:=]\s*[\"']?([A-Za-z0-9_.-]+)")
_SPRING_YAML_NAME = re.compile(r"(?im)^\s*spring:\s*(?:#.*)?\n\s+application:\s*(?:#.*)?\n\s+name:\s*[\"']?([A-Za-z0-9_.-]+)")


def _is_deployment_file(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = {part.lower() for part in rel.parts}
    name = path.name.lower()
    if any(part in IGNORED_DIRS for part in rel.parts):
        return False
    return (
        name.startswith("dockerfile")
        or name in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", ".env.example", "jenkinsfile", ".gitlab-ci.yml", ".gitlab-ci.yaml", "azure-pipelines.yml", "azure-pipelines.yaml"}
        or bool(re.fullmatch(r"application.*\.(?:yml|yaml|properties)", name))
        or "helm" in parts or "charts" in parts or "k8s" in parts or "kubernetes" in parts
        or tuple(part.lower() for part in rel.parts[:2]) == (".github", "workflows")
    )


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _safe_url(raw: str) -> str:
    parsed = urlsplit(raw.rstrip(",;)]}"))
    host = parsed.hostname or ""
    if not host:
        return ""
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def _finding(path: Path, root: Path, line: int, kind: str, name: str, evidence: str, details=None) -> ScanFinding:
    return ScanFinding(
        kind=kind, file=path.relative_to(root).as_posix(), line=line, name=name,
        status="candidate", source="deployment_config", confidence="medium",
        evidence=evidence, details={**(details or {"environment": "unverified"}), "candidate_only": True, "semantic_decision_owner": "ai_skill"},
    )


def scan_deployment(root: Path) -> tuple[list[ScanFinding], list[ScanGap]]:
    root = root.resolve()
    findings: list[ScanFinding] = []
    scanned = 0
    for path in root.rglob("*"):
        if not path.is_file() or not _is_deployment_file(path, root):
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name = path.name.lower()
        findings.append(_finding(path, root, 1, "deployment_asset", path.relative_to(root).as_posix(), "recognized deployment/configuration file"))
        if name in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}:
            services_offset = text.find("services:")
            if services_offset >= 0:
                for match in _SERVICE_HEADER.finditer(text, services_offset):
                    findings.append(_finding(path, root, _line(text, match.start()), "service", match.group(1), "compose service key"))
        for pattern in (_SERVICE_NAME, _SPRING_YAML_NAME):
            for match in pattern.finditer(text):
                findings.append(_finding(path, root, _line(text, match.start()), "service", match.group(1), "service name candidate"))
        for match in _URL.finditer(text):
            safe = _safe_url(match.group(0))
            if safe:
                findings.append(_finding(path, root, _line(text, match.start()), "url", safe, "URL candidate (credentials/query omitted)"))
        for pattern in (_PORT, _EXPOSE_PORT):
            for match in pattern.finditer(text):
                port = next((group for group in match.groups() if group), None)
                if port:
                    findings.append(_finding(path, root, _line(text, match.start()), "port", port, "port candidate"))
        keys: dict[str, int] = {}
        for pattern in (_ENV_KEY, _YAML_ENV_KEY, _PLACEHOLDER_KEY, _SECRET_ASSIGNMENT_KEY):
            for match in pattern.finditer(text):
                keys.setdefault(match.group(1), _line(text, match.start()))
        for key, line in keys.items():
            secret = bool(_SECRET_NAME.search(key))
            findings.append(_finding(
                path, root, line, "secret_key" if secret else "environment_key", key,
                "key name only; value intentionally omitted",
                {"environment": "unverified", "secret": secret},
            ))
    gaps: list[ScanGap] = []
    if not scanned:
        gaps.append(ScanGap(code="deployment_assets_not_found", scope="deployment", message="未发现支持的部署或配置文件。"))
    return findings, gaps
