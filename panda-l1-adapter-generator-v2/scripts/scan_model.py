#!/usr/bin/env python3
"""Structured, JSON-serializable models for read-only project discovery."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ScanFinding:
    kind: str
    file: str
    line: int
    status: str
    source: str
    confidence: str
    evidence: str
    method: str | None = None
    path_pattern: str | None = None
    name: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class ScanGap:
    code: str
    message: str
    scope: str
    status: str = "unverified"
    evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class ScanReport:
    source: list[ScanFinding] = field(default_factory=list)
    deployment: list[ScanFinding] = field(default_factory=list)
    contracts: list[ScanFinding] = field(default_factory=list)
    gaps: list[ScanGap] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    # Static scanners only propose candidates. Semantic interpretation and
    # suitability decisions remain owned by AI/Skill instructions.
    decision_policy: str = "ai_skill_semantic_decision_script_candidate_discovery"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": [item.to_dict() for item in self.source],
            "deployment": [item.to_dict() for item in self.deployment],
            "contracts": [item.to_dict() for item in self.contracts],
            "gaps": [item.to_dict() for item in self.gaps],
            "summary": self.summary,
            "decision_policy": self.decision_policy,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)
