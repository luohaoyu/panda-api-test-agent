#!/usr/bin/env python3
import argparse
import hashlib
import html
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
PATH_REQUIRED_KINDS = {"business_execution", "external_evidence"}

TEMPLATE_ID = "panda-requirement-report"
TEMPLATE_VERSION = "4.0.0"


def _load_report_base_css():
    """加载共享报告主题（单一来源）。缺失时报错拒绝渲染，不允许静默降级。"""
    override = os.environ.get("PANDA_REPORT_THEME")
    candidates = [Path(override)] if override else []
    candidates.append(Path(__file__).resolve().parents[2] / "panda-report-theme" / "report-base.css")
    for path in candidates:
        if path.is_file():
            css = path.read_text(encoding="utf-8").strip()
            if css:
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


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def pct(count, total):
    return f"{(count / total * 100):.1f}%" if total else "0.0%"


def _resolve_evidence(path_value, base_dir):
    p = Path(str(path_value))
    if p.is_absolute():
        return p
    base = Path(base_dir).resolve() if base_dir else Path.cwd()
    for _ in range(6):
        candidate = base / p
        if candidate.is_file():
            return candidate
        if base.parent == base:
            break
        base = base.parent
    return base / p


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
        item_id = item.get("id")
        if not item_id:
            errors.append(f"{prefix}.id is required")
        elif item_id in ids:
            errors.append(f"duplicate acceptance id: {item_id}")
        ids.add(item_id)
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


def overall_verdict(data):
    items = data["acceptance_items"]
    issues = data.get("issues") or []
    open_blocker = any(
        i.get("type") == "defect"
        and i.get("severity") == "blocker"
        and i.get("status") not in {"fixed", "closed"}
        for i in issues
    )
    failed = any(i.get("execution_status") == "complete" and i.get("verdict") == "failed" for i in items)
    blocked = any(i.get("verdict") == "blocked" for i in items)
    all_passed = bool(items) and all(
        i.get("execution_status") == "complete" and i.get("verdict") == "passed" for i in items
    )
    if open_blocker or failed:
        return "FAIL", "存在阻断缺陷或完整执行的验收项失败"
    if blocked:
        return "BLOCKED", "必需验收项被门禁或外部条件阻塞"
    if all_passed:
        return "PASS", "全部验收项已完整执行并通过"
    return "INCONCLUSIVE", "覆盖或证据不足，尚不能形成通过结论"


_LABELS = {
    "passed": "通过", "failed": "失败", "blocked": "阻塞", "inconclusive": "证据不足",
    "not_run": "未执行", "partial": "部分执行", "complete": "完整执行", "open": "未闭合",
    "direct": "接口可完整验证", "none": "接口层不可验证", "fixed": "已修复", "closed": "已关闭",
    "business_execution": "业务执行", "external_evidence": "外部证据", "gate": "门禁",
}


def badge(value):
    normalized = str(value or "unknown")
    return f'<span class="chip s-{esc(normalized)}">{esc(_LABELS.get(normalized, normalized))}</span>'


def _joined(values, empty="-"):
    return "、".join(str(value) for value in (values or []) if str(value).strip()) or empty


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


def _coverage_rows(items):
    rows = []
    for item in items:
        boundary = item.get("interface_verifiability") or "direct"
        explanation = item.get("actual") or item.get("verifiability_reason") or "未提供说明"
        gaps = _joined(item.get("gaps"), "无")
        scopes = []
        if item.get("interface_scope"):
            scopes.append("接口内：" + _joined(item.get("interface_scope")))
        if item.get("non_interface_scope"):
            scopes.append("接口外：" + _joined(item.get("non_interface_scope")))
        external = item.get("external_evidence") or {}
        if external.get("refs"):
            scopes.append("外部证据：" + _joined(external.get("refs")))
        detail = "；".join([explanation, *scopes, "缺口：" + gaps])
        rows.append(
            f'<tr><td><strong>{esc(item.get("id"))}</strong><br>{esc(item.get("title"))}</td>'
            f'<td>{badge(boundary)}</td><td>{esc(_joined(item.get("scenarios"), "未映射场景"))}</td>'
            f'<td>{badge(item.get("execution_status"))}</td><td>{badge(item.get("verdict"))}</td>'
            f'<td>{esc(detail)}</td></tr>'
        )
    return "".join(rows) or '<tr><td colspan="6" class="empty">无验收项</td></tr>'


def _interface_rows(interfaces):
    rows = []
    for item in interfaces:
        purposes = _joined(item.get("purposes"), "无结构化检查项明细")
        rows.append(
            f'<tr><td><code>{esc(item.get("method") or "-")} {esc(item.get("path") or "-")}</code></td>'
            f'<td>{esc(_joined(item.get("scenarios"), "未分类场景"))}</td>'
            f'<td>{esc(item.get("case_count"))}</td><td>{esc(purposes)}</td>'
            f'<td>{badge(item.get("result"))}<br><span class="small">{esc(item.get("exchange_count"))} 条请求证据</span></td></tr>'
        )
    return "".join(rows) or '<tr><td colspan="5" class="empty">未从 plan/evidence 解析到 HTTP 接口</td></tr>'


def _assertion_rows(case):
    rows = []
    for item in case.get("assertions") or []:
        status = item.get("status") or "unknown"
        rows.append(
            f'<tr class="check-{esc(status)}"><td><strong>{esc(item.get("name"))}</strong><br>'
            f'<span class="small">{esc(item.get("type"))}</span></td>'
            f'<td>{esc(_display(item.get("expected")))}</td><td>{esc(_display(item.get("actual")))}</td>'
            f'<td>{badge(status)}</td><td><code>{esc(item.get("evidence_ref") or "未关联")}</code></td></tr>'
        )
    if not rows:
        return '<tr><td colspan="5" class="empty">无结构化检查项明细</td></tr>'
    return "".join(rows)


def _exchange_blocks(case):
    blocks = []
    for index, item in enumerate(case.get("exchanges") or [], 1):
        flags = []
        if item.get("truncated"):
            flags.append("已截断")
        if item.get("request_missing") or item.get("response_missing"):
            flags.append("报文不完整")
        meta = []
        if item.get("status") is not None:
            meta.append(f"HTTP {item['status']}")
        if item.get("elapsed_ms") is not None:
            meta.append(f"{item['elapsed_ms']}ms")
        blocks.append(
            f'<details class="exchange"><summary class="exchange-summary"><div class="summary-title">'
            f'<strong>EX-{index:02d} · {esc(item.get("label"))} · {esc(item.get("method") or "-")} '
            f'{esc(item.get("path") or "-")}</strong><span>{esc(" · ".join(meta) or "原始 HTTP 证据")}</span></div>'
            f'<span class="chip s-neutral">{esc(" / ".join(flags) or "原始报文")}</span></summary>'
            f'<div class="exchange-body"><p class="exchange-purpose">证据引用：<code>{esc(item.get("evidence_ref"))}</code></p>'
            f'<div class="http-grid"><div><span class="http-label">Request</span><pre>{esc(item.get("request"))}</pre></div>'
            f'<div><span class="http-label">Response</span><pre>{esc(item.get("response"))}</pre></div></div></div></details>'
        )
    return "".join(blocks) or '<p class="empty">原始请求/响应报文缺失；未伪造替代内容。</p>'


def _scenario_blocks(scenarios):
    blocks = []
    for index, scenario in enumerate(scenarios, 1):
        cases = []
        for case in scenario.get("cases") or []:
            requests = _joined(
                [f"{item.get('method')} {item.get('path')}" for item in case.get("planned_requests") or []],
                "未声明 HTTP 请求",
            )
            cases.append(
                f'<details class="case" data-state="{esc(case.get("status"))}"><summary class="case-summary">'
                f'<div class="summary-title"><strong>{esc(case.get("id"))} · {esc(case.get("name"))}</strong>'
                f'<span>来源 {esc(case.get("run_id"))} · {esc(requests)}</span></div>{badge(case.get("status"))}</summary>'
                f'<div class="case-body"><div class="case-facts">'
                f'<div class="fact"><span>验收引用</span><strong>{esc(_joined(case.get("acceptance_refs"), "未映射"))}</strong></div>'
                f'<div class="fact"><span>结构化检查项</span><strong>{esc(len(case.get("assertions") or []))}</strong></div>'
                f'<div class="fact"><span>关联请求证据</span><strong>{esc(len(case.get("exchanges") or []))}</strong></div></div>'
                f'<h4>检查了什么</h4><div class="table-wrap"><table><thead><tr><th>名称 / 类型</th><th>预期</th>'
                f'<th>实际 / 观测</th><th>结果</th><th>证据</th></tr></thead><tbody>{_assertion_rows(case)}</tbody></table></div>'
                f'<h4>原始请求与响应</h4>{_exchange_blocks(case)}</div></details>'
            )
        blocks.append(
            f'<details class="scenario" data-state="{esc(scenario.get("state"))}"><summary class="scenario-summary">'
            f'<div class="summary-title"><strong>场景 {index:02d} · {esc(scenario.get("name"))}</strong>'
            f'<span>{esc(scenario.get("case_count"))} 条用例 · {esc(scenario.get("assertion_count"))} 个检查项 · '
            f'覆盖 {esc(_joined(scenario.get("acceptance_refs"), "未映射 AC"))}</span></div>{badge(scenario.get("state"))}</summary>'
            f'<div class="scenario-body">{"".join(cases) or "<p class=\"empty\">无用例</p>"}</div></details>'
        )
    return "".join(blocks) or '<p class="empty">未找到可展示的 plan task；验收结论仍以 manifest 为准。</p>'


def _issue_cards(items, label):
    cards = []
    for item in items:
        cards.append(
            f'<article class="issue-card">{badge(item.get("status") or label)}'
            f'<h3>{esc(item.get("id") or item.get("run_id") or label)} · {esc(item.get("title") or item.get("kind") or label)}</h3>'
            f'<p>{esc(item.get("actual") or item.get("detail") or item.get("reason") or "未提供详情")}</p>'
            f'<span class="small">影响：{esc(item.get("impact") or "未标注")} · 验收项：'
            f'{esc(_joined(item.get("acceptance_refs"), "未映射"))}</span></article>'
        )
    return "".join(cards) or f'<article class="issue-card"><h3>无{esc(label)}</h3><p>当前没有已登记条目。</p></article>'


def _run_rows(view):
    rows = []
    for item in view.get("run_index") or []:
        rows.append(
            f'<tr><td><code>{esc(item.get("id"))}</code></td><td>{badge(item.get("kind"))}</td>'
            f'<td>{esc(item.get("status"))}</td><td>{esc(_joined(item.get("notes"), "已读取"))}</td></tr>'
        )
    for item in view.get("unmapped_evidence") or []:
        rows.append(
            f'<tr><td><code>{esc(item.get("run_id"))}</code></td><td>{badge("unmapped")}</td>'
            f'<td><code>{esc(item.get("ref"))}</code></td><td>{esc(item.get("reason"))}</td></tr>'
        )
    return "".join(rows) or '<tr><td colspan="4" class="empty">无运行索引</td></tr>'


def render(data, base_dir=None):
    req = data["requirement"]
    baseline = data.get("baseline") or {}
    items = data["acceptance_items"]
    aggregation = data.get("aggregation") or {}
    view = _VIEW_ADAPTER.build_report_view(data, base_dir=base_dir)
    summary = view["summary"]
    verdict, verdict_reason = overall_verdict(data)
    generated = str(data.get("generated_at") or "").strip() or "未标注（manifest 未提供 generated_at）"
    report_id = data.get("report_id") or f"AR-{req.get('id', '-')}"
    release = data.get("release_recommendation") or "未提供发布建议"
    all_issues = view["issues"] + view["gaps"]
    actions = _critical_actions(items, all_issues, view["residual_risks"])
    action_html = "".join(f'<li>{esc(item)}</li>' for item in actions) or "<li>当前无额外行动；保持证据不变。</li>"
    reason_detail = next((item.get("actual") for item in items if item.get("verdict") == "failed" and item.get("actual")), verdict_reason)
    css = r'''
__PANDA_REPORT_BASE_CSS__
body{margin:0;background:var(--canvas);color:var(--ink);line-height:1.6}.shell{max-width:1240px;margin:auto;padding:24px}.report-header,.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}.report-header{padding:28px}.panel{margin-top:16px;padding:22px}h1{font-size:28px;line-height:1.3;margin:6px 0 4px}h2{font-size:19px;margin:0}h3{font-size:15px;margin:8px 0 5px}h4{font-size:14px;margin:18px 0 8px}.eyebrow{color:var(--blue);font-size:12px;font-weight:750;letter-spacing:.08em}.muted,.small,.section-head p,.summary-title span,.fact span,.http-label{color:var(--muted)}.small{font-size:12px}.header-main{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:24px}.report-meta{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:18px;color:var(--muted);font-size:13px}.decision{border:1px solid var(--line);border-radius:12px;padding:18px;background:var(--canvas)}.decision-label{font-size:12px;font-weight:700}.verdict{font-size:28px;font-weight:800}.v-PASS{color:var(--green)}.v-FAIL{color:var(--red)}.v-BLOCKED,.v-INCONCLUSIVE{color:var(--amber)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:18px}.metric{border:1px solid var(--line);background:var(--canvas);border-radius:11px;padding:15px}.metric span{font-size:12px;color:var(--muted)}.metric strong{display:block;font-size:22px}.headline-grid{display:grid;grid-template-columns:1.15fr .85fr;gap:12px;margin-top:16px}.headline-box{padding:15px;background:var(--canvas);border:1px solid var(--line);border-radius:11px}.headline-box strong{display:block}.headline-box ul{margin:5px 0 0;padding-left:20px}.section-head{display:flex;justify-content:space-between;gap:16px;margin-bottom:16px}.section-head p{margin:3px 0 0;font-size:13px}.chips{display:flex;gap:7px;flex-wrap:wrap}.chip{display:inline-block;padding:4px 9px;border-radius:99px;font-size:12px;white-space:nowrap}.s-passed,.s-complete,.s-fixed,.s-closed{color:var(--green);background:var(--green-soft)}.s-failed,.s-error,.s-defect{color:var(--red);background:var(--red-soft)}.s-blocked,.s-inconclusive,.s-partial,.s-open,.s-test_gap,.s-none{color:var(--amber);background:var(--amber-soft)}.s-direct,.s-business_execution,.s-external_evidence{color:var(--blue);background:var(--blue-soft)}.s-not_run,.s-neutral,.s-gate,.s-unmapped,.s-unknown{color:var(--muted);background:var(--canvas)}.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:11px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:12px 13px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:var(--canvas);font-size:12px;color:var(--muted);white-space:nowrap}tr:last-child td{border-bottom:0}.empty{color:var(--muted)}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}.filter-btn{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:6px 10px;cursor:pointer}.filter-btn.active{color:var(--blue);background:var(--blue-soft)}details{border:1px solid var(--line);border-radius:12px;background:var(--panel)}details+details{margin-top:10px}summary{list-style:none;cursor:pointer}summary::-webkit-details-marker{display:none}.scenario-summary,.case-summary,.exchange-summary{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:15px}.scenario-summary:before,.case-summary:before,.exchange-summary:before{content:"+";width:21px;height:21px;flex:0 0 auto;border-radius:50%;color:var(--blue);background:var(--blue-soft);text-align:center;line-height:20px;font-weight:700}details[open]>summary:before{content:"−"}.summary-title{flex:1;min-width:0}.summary-title strong,.summary-title span{display:block}.summary-title span{font-size:12px}.scenario-body{border-top:1px solid var(--line);padding:16px;background:var(--canvas)}.case-body{border-top:1px solid var(--line);padding:16px}.case-facts{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}.fact{background:var(--canvas);border-radius:8px;padding:10px}.fact span,.fact strong{display:block}.check-failed td,.check-error td{background:var(--red-soft)}.exchange{margin-top:8px;background:var(--canvas)}.exchange-body{padding:0 13px 13px}.exchange-purpose{padding:9px 11px;background:var(--blue-soft);border-radius:8px;font-size:12px}.http-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.http-label{display:block;font-size:11px;margin-bottom:5px}pre{margin:0;padding:13px;overflow:auto;border-radius:9px;background:var(--navy);color:#e5e7eb;font-size:12px;line-height:1.55;white-space:pre-wrap;overflow-wrap:anywhere}.issue-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.issue-card{border:1px solid var(--line);border-radius:11px;padding:15px;background:var(--canvas)}.next-action{border:1px solid var(--line);background:var(--amber-soft);border-radius:11px;padding:15px}.audit{padding:18px 2px 8px;color:var(--muted);font-size:11px;text-align:center}@media(max-width:900px){.header-main,.headline-grid,.http-grid{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}.issue-grid{grid-template-columns:1fr}}@media(max-width:620px){.shell{padding:12px}.report-header,.panel{padding:16px;border-radius:11px}h1{font-size:22px}.metrics,.case-facts{grid-template-columns:1fr}.scenario-summary,.case-summary,.exchange-summary{align-items:flex-start}}@media print{body{background:#fff}.shell{max-width:none;padding:0}.report-header,.panel{box-shadow:none;break-inside:avoid}.toolbar{display:none}details{break-inside:avoid}details>*{display:block!important}pre{white-space:pre-wrap}}
'''
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(req.get("id"))} 需求接口验收报告</title><style>{css}</style></head><body><main class="shell">
<header class="report-header"><div class="header-main"><div><div class="eyebrow">需求接口验收报告 · {esc(report_id)}</div><h1>{esc(req.get("id"))} · {esc(req.get("title"))}</h1><p class="muted">合并展示需求覆盖、涉及接口、场景、用例、检查项和原始请求/响应；不构造执行步骤或接口顺序。</p><div class="report-meta"><span>环境：{esc(baseline.get("environment") or "-")}</span><span>代码基线：{esc(baseline.get("branch") or "-")} · {esc(baseline.get("commit") or "-")}</span><span>聚合运行：{esc(aggregation.get("run_count") or summary["run_count"])}</span><span>Report View {esc(view["report_view_version"])}</span><span>生成时间：{esc(generated)}</span></div></div><aside class="decision"><div class="decision-label">总体结论</div><div class="verdict v-{esc(verdict)}">{esc(verdict)}</div><p>{esc(verdict_reason)}</p><strong>{esc(release)}</strong></aside></div>
<div class="metrics"><div class="metric"><span>验收项触达率</span><strong>{pct(summary["touched"],summary["acceptance_total"])}</strong><small>{summary["touched"]} / {summary["acceptance_total"]}</small></div><div class="metric"><span>完整覆盖率</span><strong>{pct(summary["complete"],summary["acceptance_total"])}</strong><small>{summary["complete"]} / {summary["acceptance_total"]}</small></div><div class="metric"><span>验收通过率</span><strong>{pct(summary["passed"],summary["acceptance_total"])}</strong><small>{summary["passed"]} / {summary["acceptance_total"]}</small></div><div class="metric"><span>接口执行覆盖</span><strong>{pct(summary["interface_executed"],summary["interface_total"])}</strong><small>{summary["interface_executed"]} / {summary["interface_total"]}</small></div></div>
<div class="headline-grid"><div class="headline-box"><strong>为什么是这个结论</strong><span class="muted">{esc(reason_detail)}</span></div><div class="headline-box"><strong>优先下一步</strong><ul>{action_html}</ul></div></div></header>
<section class="panel" id="coverage"><div class="section-head"><div><h2>1. 需求覆盖</h2><p>三项覆盖指标均保持验收项 AC 分母；完整执行但失败不计为通过。</p></div></div><div class="table-wrap"><table><thead><tr><th>验收项</th><th>验证边界</th><th>覆盖场景</th><th>执行情况</th><th>结论</th><th>事实与缺口</th></tr></thead><tbody>{_coverage_rows(view["coverage"]["items"])}</tbody></table></div></section>
<section class="panel" id="interfaces"><div class="section-head"><div><h2>2. 需求涉及接口</h2><p>按 method + 规范化 path 聚合；接口仅作为场景和用例的关联证据，不表达执行顺序。</p></div><span class="chip s-neutral">{len(view["interfaces"])} 个接口 · {summary["exchange_count"]} 条请求证据</span></div><div class="table-wrap"><table><thead><tr><th>接口</th><th>场景</th><th>用例数</th><th>检查用途</th><th>结果</th></tr></thead><tbody>{_interface_rows(view["interfaces"])}</tbody></table></div></section>
<section class="panel" id="details"><div class="section-head"><div><h2>3. 场景、用例与检查证据</h2><p>优先采用 scenario_refs、scenario_type、quality dimension；否则按业务引用/AC 分组，最后归入未分类场景。</p></div></div><div class="toolbar"><button class="filter-btn active" data-filter="all">全部</button><button class="filter-btn" data-filter="failed">仅失败</button><button class="filter-btn" data-filter="passed">仅通过</button><button class="filter-btn" data-filter="open">仅未闭合</button><button class="filter-btn" data-filter="blocked">仅阻塞</button></div><div class="scenario-list">{_scenario_blocks(view["scenarios"])}</div></section>
<section class="panel" id="issues"><div class="section-head"><div><h2>4. 问题、缺口与残留风险</h2><p>产品缺陷、测试缺口和运行残留分别呈现，不以证据缺失冒充产品缺陷。</p></div></div><div class="issue-grid">{_issue_cards(view["issues"], "产品缺陷")}{_issue_cards(view["gaps"], "测试缺口")}{_issue_cards(view["residual_risks"], "残留风险")}</div></section>
<section class="panel" id="next"><div class="section-head"><div><h2>5. 下一步</h2><p>仅依据 manifest 中的未关闭问题、验收缺口和已记录残留生成，不猜测事实。</p></div></div><div class="next-action"><ol>{action_html}</ol></div></section>
<section class="panel" id="evidence-index"><div class="section-head"><div><h2>证据与降级索引</h2><p>Run 文件只读；路径不直接展示。缺失、损坏和未映射证据均显式标记。</p></div></div><div class="table-wrap"><table><thead><tr><th>Run</th><th>类型</th><th>状态 / 引用</th><th>说明</th></tr></thead><tbody>{_run_rows(view)}</tbody></table></div></section>
<footer class="audit">Report {esc(report_id)} · Template {TEMPLATE_ID}@{TEMPLATE_VERSION} · base-css:{BASE_CSS_SHA256[:12]} · Report View {esc(view["report_view_version"])} · generated_at:{esc(generated)} · 历史运行证据未修改</footer>
</main><script>const buttons=[...document.querySelectorAll('[data-filter]')],scenarios=[...document.querySelectorAll('.scenario')];buttons.forEach(button=>button.addEventListener('click',()=>{{const filter=button.dataset.filter;buttons.forEach(item=>item.classList.toggle('active',item===button));scenarios.forEach(item=>{{item.hidden=filter!=='all'&&item.dataset.state!==filter}})}}));</script></body></html>'''
    return page.replace("__PANDA_REPORT_BASE_CSS__", BASE_CSS)


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
