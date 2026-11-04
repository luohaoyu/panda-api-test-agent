#!/usr/bin/env python3
import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

EXECUTION = {"not_run", "partial", "complete"}
VERDICTS = {"passed", "failed", "blocked", "inconclusive", "not_run"}
ISSUE_TYPES = {"defect", "test_gap"}
SEVERITIES = {"blocker", "high", "medium", "low"}
PASS_EVIDENCE_KINDS = {
    "direct": {"business_execution"},
    "partial": {"business_execution", "external_evidence"},
    "none": {"external_evidence"},
}
INTERFACE_VERIFIABILITY = {"direct", "partial", "none"}
PATH_REQUIRED_KINDS = {"business_execution", "external_evidence"}

TEMPLATE_ID = "panda-requirement-report"
TEMPLATE_VERSION = "6.0.0"
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "requirement_report.html"


def _load_report_base_css():
    """加载共享报告主题（单一来源）。缺失时报错拒绝渲染，不允许静默降级。"""
    override = os.environ.get("PANDA_REPORT_THEME")
    candidates = [Path(override)] if override else []
    candidates.append(Path(__file__).resolve().parents[2] / "panda-report-theme" / "report-base.css")
    for path in candidates:
        if path.is_file():
            css = path.read_text(encoding="utf-8").strip()
            if css:
                if ":root" not in css or "--canvas:" not in css or len(css) < 3000:
                    raise RuntimeError(f"报告主题不完整，拒绝生成正式报告：{path}")
                return css
    raise RuntimeError(
        "未找到共享报告主题 report-base.css；请将 panda-report-theme 安装到技能同级目录，"
        "或通过 PANDA_REPORT_THEME 环境变量指定文件路径"
    )


def _load_view_adapter():
    path = Path(__file__).resolve().parent / "report_view_adapter.py"
    spec = importlib.util.spec_from_file_location("panda_report_view_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载报告视图适配器：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE_CSS = _load_report_base_css()
BASE_CSS_SHA256 = hashlib.sha256(BASE_CSS.encode("utf-8")).hexdigest()
_VIEW_ADAPTER = _load_view_adapter()


def _resolve_evidence(path_value, base_dir):
    path = Path(str(path_value))
    if path.is_absolute():
        return path
    base = Path(base_dir).resolve() if base_dir else Path.cwd()
    for _ in range(6):
        candidate = base / path
        if candidate.is_file():
            return candidate
        if base.parent == base:
            break
        base = base.parent
    return base / path


def validate(data, base_dir=None):
    errors = []
    if data.get("schema_version") != "1.0":
        errors.append("schema_version must equal 1.0")
    requirement = data.get("requirement") or {}
    for key in ("id", "title", "acceptance_total"):
        if not requirement.get(key):
            errors.append(f"requirement.{key} is required")
    items = data.get("acceptance_items")
    if not isinstance(items, list):
        errors.append("acceptance_items must be an array")
        items = []
    ids = set()
    for index, item in enumerate(items):
        prefix = f"acceptance_items[{index}]"
        if not isinstance(item, dict):
            errors.append({"path": prefix, "code": "MALFORMED_LIST_ELEMENT", "message": "must be an object"})
            continue
        item_id = item.get("id")
        if not item_id:
            errors.append(f"{prefix}.id is required")
        elif item_id in ids:
            errors.append(f"duplicate acceptance id: {item_id}")
        ids.add(item_id)
        if item.get("interface_verifiability") is not None and item.get("interface_verifiability") not in INTERFACE_VERIFIABILITY:
            errors.append({"path": f"{prefix}.interface_verifiability", "code": "INVALID_ENUM", "message": "must be one of direct, partial, none"})
        if item.get("execution_status") not in EXECUTION:
            errors.append(f"{prefix}.execution_status is invalid")
        if item.get("verdict") not in VERDICTS:
            errors.append(f"{prefix}.verdict is invalid")
        if item.get("verdict") == "passed" and item.get("execution_status") != "complete":
            errors.append(f"{prefix}: passed requires complete execution")
        if item.get("verdict") == "passed":
            refs = item.get("evidence_refs") or []
            if not refs:
                errors.append(f"{prefix}: passed requires evidence_refs")
            immutable_runs = {
                run.get("id"): run
                for run in data.get("evidence_runs") or []
                if isinstance(run, dict) and run.get("id") and run.get("immutable") is True
            }
            referenced = [immutable_runs[ref] for ref in refs if ref in immutable_runs]
            if not referenced:
                errors.append(f"{prefix}: passed requires an immutable evidence run reference")
            else:
                verifiability = item.get("interface_verifiability") or "direct"
                required = PASS_EVIDENCE_KINDS.get(verifiability, PASS_EVIDENCE_KINDS["direct"])
                available = {run.get("kind") for run in referenced}
                missing = required - available
                if missing:
                    errors.append(
                        f"{prefix}: passed with interface_verifiability={verifiability} requires evidence kinds "
                        f"{sorted(required)}; missing {'/'.join(sorted(missing))}"
                    )
    total = requirement.get("acceptance_total")
    if isinstance(total, int) and total != len(items):
        errors.append("requirement.acceptance_total must equal acceptance_items length")
    for index, issue in enumerate(data.get("issues") or []):
        prefix = f"issues[{index}]"
        if issue.get("type") not in ISSUE_TYPES:
            errors.append(f"{prefix}.type is invalid")
        if issue.get("severity") not in SEVERITIES:
            errors.append(f"{prefix}.severity is invalid")
        if not issue.get("id") or not issue.get("title"):
            errors.append(f"{prefix}.id and title are required")
    for index, run in enumerate(data.get("evidence_runs") or []):
        if not run.get("id") or run.get("immutable") is not True:
            errors.append(f"evidence_runs[{index}] requires id and immutable=true")
            continue
        path_value = run.get("result_path") or run.get("report_path")
        if run.get("kind") in PATH_REQUIRED_KINDS and not path_value:
            errors.append(f"evidence_runs[{index}]: {run.get('kind')} run requires result_path")
        if path_value:
            resolved = _resolve_evidence(path_value, base_dir)
            if not resolved.is_file():
                errors.append(f"evidence_runs[{index}]: evidence file not found: {path_value}")
    return errors


def _aggregation_verdict(aggregation):
    current = aggregation.get("current") if isinstance(aggregation, dict) else {}
    status = current.get("status") if isinstance(current, dict) else None
    reasons = current.get("status_reasons") or [] if isinstance(current, dict) else []
    if status == "failed" or any(str(reason).startswith("run_verdict_failed") for reason in reasons):
        return "FAIL", "需求聚合 current 状态为失败"
    if status == "blocked" or any(reason in {"cleanup_blocked", "blocking_gap", "run_verdict_blocked"} for reason in reasons):
        return "BLOCKED", "需求聚合存在阻塞、清理或阻断缺口"
    if status in {"inconclusive", "not_run"} or reasons:
        return "INCONCLUSIVE", "需求聚合存在未闭合状态或降级原因"
    return None, None


def overall_verdict(data, aggregation=None):
    items = data["acceptance_items"]
    issues = data.get("issues") or []
    aggregation = aggregation if aggregation is not None else data.get("aggregation") or {}
    aggregation_verdict, aggregation_reason = _aggregation_verdict(aggregation)
    open_blocker = any(
        item.get("type") == "defect"
        and item.get("severity") == "blocker"
        and item.get("status") not in {"fixed", "closed"}
        for item in issues
    )
    failed = any(item.get("execution_status") == "complete" and item.get("verdict") == "failed" for item in items)
    blocked = any(item.get("verdict") == "blocked" for item in items)
    all_passed = bool(items) and all(
        item.get("execution_status") == "complete" and item.get("verdict") == "passed" for item in items
    )
    if open_blocker or failed:
        return "FAIL", "存在阻断缺陷或完整执行的验收项失败"
    if blocked:
        return "BLOCKED", "必需验收项被门禁或外部条件阻塞"
    if aggregation_verdict:
        return aggregation_verdict, aggregation_reason
    if all_passed:
        return "PASS", "全部验收项已完整执行并通过"
    return "INCONCLUSIVE", "覆盖或证据不足，尚不能形成通过结论"


_LABELS = {
    "passed": "通过", "failed": "失败", "blocked": "阻塞", "inconclusive": "证据不足",
    "not_run": "未执行", "partial": "部分执行", "complete": "完整执行", "open": "未闭合",
    "direct": "接口可完整验证", "none": "接口层不可验证", "fixed": "已修复", "closed": "已关闭",
    "business_execution": "业务执行", "external_evidence": "外部证据", "gate": "门禁",
}


def _load_template():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    for marker in ("{{BASE_CSS}}", "{{REPORT_PAYLOAD}}"):
        if template.count(marker) != 1:
            raise RuntimeError(f"报告模板必须且只能包含一个占位符：{marker}")
    return template


def _json_for_script(value):
    """Serialize JSON safely for a script data block without changing payload semantics."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _display(value):
    if value is None:
        return "未提供"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _critical_actions(items, issues, risks):
    actions = []
    for issue in issues:
        if issue.get("status") in {"fixed", "closed"}:
            continue
        actions.append(f"{issue.get('id')}: {issue.get('title')}")
    for item in items:
        if item.get("verdict") == "passed":
            continue
        detail = next(iter(item.get("gaps") or []), None) or item.get("actual") or "补齐执行与证据"
        actions.append(f"{item.get('id')}: {detail}")
    for risk in risks:
        detail = risk.get("detail") or risk.get("reason") or risk.get("kind") or "处置残留风险"
        actions.append(f"{risk.get('run_id')}: {_display(detail)}")
    seen = []
    for action in actions:
        if action not in seen:
            seen.append(action)
    return seen[:3]


def build_report_payload(data, base_dir=None):
    """Build the deterministic, read-only payload consumed by the HTML template."""
    requirement = data["requirement"]
    items = data["acceptance_items"]
    view = _VIEW_ADAPTER.build_report_view(data, base_dir=base_dir)
    verdict, verdict_reason = overall_verdict(data, data.get("aggregation") or {})
    generated = str(data.get("generated_at") or "").strip() or "未标注（manifest 未提供 generated_at）"
    report_id = data.get("report_id") or f"AR-{requirement.get('id', '-')}"
    all_issues = view["issues"] + view["gaps"]
    reason_detail = next(
        (item.get("actual") for item in items if item.get("verdict") == "failed" and item.get("actual")),
        verdict_reason,
    )
    return {
        "template_id": TEMPLATE_ID,
        "template_version": TEMPLATE_VERSION,
        "base_css_sha256": BASE_CSS_SHA256[:12],
        "report_id": report_id,
        "requirement": {"id": requirement.get("id"), "title": requirement.get("title")},
        "baseline": data.get("baseline") or {},
        "aggregation": data.get("aggregation") or {},
        "generated_at": generated,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "release_recommendation": data.get("release_recommendation") or "未提供发布建议",
        "reason_detail": reason_detail,
        "actions": _critical_actions(items, all_issues, view["residual_risks"]),
        "labels": _LABELS,
        "view": view,
    }


def render(data, base_dir=None):
    payload = build_report_payload(data, base_dir=base_dir)
    return (
        _load_template()
        .replace("{{BASE_CSS}}", BASE_CSS)
        .replace("{{REPORT_PAYLOAD}}", _json_for_script(payload))
    )

def main():
    parser = argparse.ArgumentParser(description="Render deterministic requirement acceptance report")
    parser.add_argument("manifest")
    parser.add_argument("--output")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    manifest = Path(args.manifest).resolve()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    errors = validate(data, base_dir=manifest.parent)
    if errors:
        sys.stderr.write(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False, indent=2) + "\n")
        return 2
    if args.validate_only:
        print(json.dumps({"valid": True, "acceptance_total": len(data["acceptance_items"])}, ensure_ascii=False))
        return 0
    if not args.output:
        parser.error("--output is required unless --validate-only is used")
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(render(data, base_dir=manifest.parent))
    verdict, _ = overall_verdict(data)
    print(json.dumps({"report": str(output), "verdict": verdict, "requirement": data["requirement"]["id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
