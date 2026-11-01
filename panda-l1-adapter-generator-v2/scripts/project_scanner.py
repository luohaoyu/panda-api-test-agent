#!/usr/bin/env python3
"""Aggregate read-only L1 project discovery as JSON."""
from __future__ import annotations

import argparse
from pathlib import Path

from contract_scanner import scan_contracts
from deployment_scanner import scan_deployment
from scan_model import ScanGap, ScanReport
from source_scanner import scan_source


def scan_project(project_root: Path) -> ScanReport:
    root = project_root.expanduser().resolve()
    if not root.is_dir():
        return ScanReport(
            gaps=[ScanGap(code="project_root_invalid", scope="project", message="项目根目录不存在或不是目录。", evidence=str(root))],
            summary={"project_root": str(root), "source_count": 0, "deployment_count": 0, "contract_count": 0, "gap_count": 1},
        )
    source, source_gaps = scan_source(root)
    deployment, deployment_gaps = scan_deployment(root)
    contracts, contract_gaps = scan_contracts(root, source)
    gaps = source_gaps + deployment_gaps + contract_gaps
    summary = {
        "project_root": str(root),
        "source_count": len(source),
        "deployment_count": len(deployment),
        "contract_count": len(contracts),
        "gap_count": len(gaps),
        "mode": "read_only_discovery",
        "semantic_decision_owner": "ai_skill",
        "execution_decision_owner": "human_review_and_runner",
    }
    return ScanReport(source=source, deployment=deployment, contracts=contracts, gaps=gaps, summary=summary)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only L1 project scanner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan", help="scan a project directory without executing it")
    scan_parser.add_argument("project_root", type=Path)
    scan_parser.add_argument("--output", choices=("json",), default="json")
    args = parser.parse_args()
    report = scan_project(args.project_root)
    print(report.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
