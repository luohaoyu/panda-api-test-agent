#!/usr/bin/env python3
import argparse
import hashlib
import html
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


STATUS_LABELS = {
    "passed": "通过",
    "failed": "失败",
    "error": "错误",
    "skipped": "跳过",
    "not_run": "未执行",
    "not_required": "无需清理",
    "none": "无副作用",
    "waived": "已风险豁免",
}
VALIDATOR_SHA256 = "1d29f40613d3afc4a1b761dbce7523b41e62a9bb15bad54fd70910145bd15523"

VERDICT_LABELS = {
    "passed": ("PASS", "全部必需验证已通过"),
    "failed": ("FAIL", "发现经证据确认的验证失败"),
    "error": ("ERROR", "执行异常影响本次结论"),
    "blocked": ("BLOCKED", "门禁未通过，业务验证被安全阻断"),
    "partial": ("PARTIAL", "仅完成部分必需验证"),
    "inconclusive": ("INCONCLUSIVE", "现有证据不足以形成可靠结论"),
}

CLASSIFICATION_LABELS = {
    "environment": "环境",
    "test_case": "测试设计",
    "product_defect": "产品缺陷",
    "inconclusive": "待判定",
}

ASSERTION_LABELS = {
    "http_status": "HTTP 状态",
    "business_code": "业务码",
    "key_fields": "关键字段",
}


def esc(value):
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _render_gap_li(item):
    """渲染单条缺口；字符串按历史格式输出，对象展开 description/impact/next_action。"""
    if isinstance(item, str):
        return f'<li>{esc(item)}</li>'
    text = item.get("description") or item.get("gap_id") or ""
    detail = ""
    impact = item.get("impact")
    if impact:
        detail += f'<br><small>影响：{esc(impact)}</small>'
    action = item.get("next_action")
    if action:
        detail += f'<br><small>下一步：{esc(action)}</small>'
    return f'<li>{esc(text)}{detail}</li>'


def render_gap_items(known_gaps):
    """按 blocking 字段分层渲染缺口，避免阻塞项与覆盖边界混排。"""
    gaps = list(known_gaps or [])
    if any(isinstance(item, dict) for item in gaps):
        blocking = [item for item in gaps if isinstance(item, dict) and item.get("blocking")]
        others = [item for item in gaps if item not in blocking]
        parts = []
        if blocking:
            parts.append("<li><strong>阻塞项（未解除则无法推进）</strong></li>")
            parts.extend(_render_gap_li(item) for item in blocking)
        if others:
            parts.append("<li><strong>覆盖边界（不影响当前结论）</strong></li>")
            parts.extend(_render_gap_li(item) for item in others)
        return "".join(parts) or '<li class="all-clear">没有已登记的覆盖缺口</li>'
    return "".join(_render_gap_li(item) for item in gaps) or '<li class="all-clear">没有已登记的覆盖缺口</li>'


def display_value(value):
    if isinstance(value, (dict, list, bool)) or value is None:
        return esc(json.dumps(value, ensure_ascii=False, separators=(", ", ": ")))
    return esc(value)


def status_badge(status):
    normalized = status or "unknown"
    label = STATUS_LABELS.get(normalized, normalized)
    return f'<span class="status status-{esc(normalized)}"><i></i>{esc(label)}</span>'


def classification_badge(value):
    if not value:
        return '<span class="quiet">未分类</span>'
    return f'<span class="classification">{esc(CLASSIFICATION_LABELS.get(value, value))}</span>'


def evidence_links(refs):
    refs = refs or []
    if not refs:
        return '<span class="quiet">无独立证据文件</span>'
    links = []
    for ref in refs:
        name = Path(str(ref)).name
        links.append(
            f'<a class="evidence-link" href="{esc(ref)}" target="_blank" rel="noopener">'
            f'<span>JSON</span>{esc(name)}</a>'
        )
    return '<div class="evidence-list">' + "".join(links) + "</div>"


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def format_time(value):
    parsed = parse_time(value)
    if not parsed:
        return esc(value or "-")
    return esc(parsed.strftime("%Y-%m-%d %H:%M:%S %z"))


def format_duration(started_at, finished_at):
    started = parse_time(started_at)
    finished = parse_time(finished_at)
    if not started or not finished:
        return "-"
    seconds = max(0, (finished - started).total_seconds())
    if seconds < 1:
        return "< 1 秒"
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes} 分 {remainder} 秒"


def assertion_rows(assertions):
    if not assertions:
        return '<div class="empty-state compact">未记录断言；该用例可能未实际执行。</div>'
    rows = []
    for assertion in assertions:
        assertion_type = assertion.get("type")
        path = assertion.get("path")
        detail = ASSERTION_LABELS.get(assertion_type, assertion_type or "断言")
        if path:
            detail += f" · {path}"
        rows.append(
            '<div class="assertion-row">'
            f'<div><strong>{esc(detail)}</strong><small>{esc(assertion.get("operator", "-"))}</small></div>'
            f'<div><span>预期</span><code>{display_value(assertion.get("expected"))}</code></div>'
            f'<div><span>实际</span><code>{display_value(assertion.get("actual"))}</code></div>'
            f'<div>{status_badge(assertion.get("status"))}</div>'
            '</div>'
        )
    return '<div class="assertion-list">' + "".join(rows) + "</div>"


def render_gate_cards(gates):
    if not gates:
        return '<div class="empty-state">未记录门禁结果</div>'
    cards = []
    for gate in gates:
        runtime = '<span class="tag">运行时</span>' if gate.get("runtime") is True else '<span class="tag subtle">静态</span>'
        cards.append(
            f'<article class="result-card tone-{esc(gate.get("status", "unknown"))}">'
            '<div class="result-head">'
            f'<div class="result-index"><span>GATE</span><code>{esc(gate.get("gate_id"))}</code></div>'
            f'<div class="result-status">{runtime}{status_badge(gate.get("status"))}</div>'
            '</div>'
            f'<h3>{esc(gate.get("name") or "未命名门禁")}</h3>'
            f'<p class="actual">{esc(gate.get("actual") or "未记录实际结果")}</p>'
            '<div class="result-meta">'
            f'<div><span>分类</span>{classification_badge(gate.get("classification"))}</div>'
            f'<div><span>证据</span>{evidence_links(gate.get("evidence_refs"))}</div>'
            '</div>'
            '</article>'
        )
    return '<div class="result-grid">' + "".join(cards) + "</div>"


def render_trace_steps(steps, compact=False):
    if not steps:
        return '<div class="empty-state compact">未记录执行步骤</div>'
    labels = {
        "package_validation": "校验测试包",
        "gate": "执行门禁",
        "fixture": "准备夹具",
        "case": "执行业务用例",
        "teardown": "执行清理",
        "verdict": "生成最终结论",
        "resolve_request": "解析请求与变量",
        "register_teardown": "预登记清理动作",
        "bind_cleanup_variables": "绑定清理变量",
        "extract_variables": "提取后继变量",
        "http_exchange": "发送请求并接收响应",
        "assertion": "执行确定性断言",
        "case_complete": "完成用例",
        "case_error": "记录执行异常",
        "blocked": "业务请求被阻断",
        "skipped": "按规则跳过",
    }
    items = []
    for step in steps:
        details = []
        if step.get("ref"):
            details.append(f'<code>{esc(step.get("ref"))}</code>')
        if step.get("method") or step.get("url") or step.get("path"):
            details.append(f'<code>{esc(step.get("method", ""))} {esc(step.get("url") or step.get("path") or "")}</code>')
        if step.get("http_status") is not None:
            details.append(f'HTTP {esc(step.get("http_status"))}')
        if step.get("elapsed_ms") is not None:
            details.append(f'{esc(step.get("elapsed_ms"))} ms')
        if step.get("assertion_id"):
            details.append(f'<code>{esc(step.get("assertion_id"))}</code>')
        if step.get("blocked_by"):
            details.append("阻塞于 " + ", ".join(esc(item) for item in step.get("blocked_by")))
        if step.get("variables"):
            details.append("变量 " + ", ".join(esc(item) for item in step.get("variables")))
        items.append(
            '<div class="trace-step">'
            f'<span class="trace-seq">{int(step.get("sequence", 0)):02d}</span>'
            '<div class="trace-copy">'
            f'<strong>{esc(labels.get(step.get("kind"), step.get("kind") or "步骤"))}</strong>'
            f'<small>{" · ".join(details) or esc(step.get("reason") or step.get("actual") or "")}</small>'
            f'{evidence_links(step.get("evidence_refs")) if step.get("evidence_refs") else ""}'
            '</div>'
            f'{status_badge(step.get("status"))}'
            '</div>'
        )
    return f'<div class="trace-list{" compact" if compact else ""}">' + "".join(items) + "</div>"


def render_feasibility(feasibility, package_build):
    if not feasibility:
        return '<div class="empty-state compact">旧版 TestPackage 2.1 未携带可行性规划快照。</div>'
    coverage = feasibility.get("coverage_map") or {}
    matrix = (feasibility.get("dependency_matrix") or {}).get("cases") or []
    rows = []
    for item in matrix:
        dependencies = item.get("execution_dependencies") or []
        dependency_text = "<br>".join(
            f'<code>{esc(dep.get("kind"))}:{esc(dep.get("dependency_id"))}</code> {status_badge(dep.get("status"))}'
            for dep in dependencies
        ) or "无显式执行依赖"
        reasons = "；".join(str(reason) for reason in item.get("blocking_reasons") or []) or "-"
        rows.append(
            '<tr>'
            f'<td><code>{esc(item.get("case_id"))}</code></td>'
            f'<td>{"已批准" if item.get("approved") else "候选范围"}</td>'
            f'<td>{dependency_text}</td>'
            f'<td>{esc(reasons)}</td>'
            f'<td>{status_badge(item.get("status"))}</td>'
            '</tr>'
        )
    return (
        '<div class="coverage-cards">'
        f'<div class="coverage-card"><div><code>CASE DESIGN</code><strong>候选用例</strong><p>由需求与验收点推导</p></div><strong>{len(feasibility.get("candidate_case_ids") or [])}</strong></div>'
        f'<div class="coverage-card"><div><code>FEASIBILITY</code><strong>可执行用例</strong><p>批准范围且依赖闭合</p></div><strong>{len(feasibility.get("executable_case_ids") or [])}</strong></div>'
        f'<div class="coverage-card"><div><code>COVERAGE</code><strong>完整需求覆盖</strong><p>不等于当前子集可执行</p></div><strong>{"是" if coverage.get("full_requirement_coverage") else "否"}</strong></div>'
        f'<div class="coverage-card"><div><code>PACKAGE BUILD</code><strong>建包状态</strong><p>可行性门禁通过后固化</p></div>{status_badge(package_build.get("status"))}</div>'
        '</div>'
        '<div class="table-wrap"><table class="coverage-table"><thead><tr><th>候选用例</th><th>范围</th><th>执行依赖</th><th>阻塞原因</th><th>可行性</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_requirement_matrix(trace):
    requirements = trace.get("requirements") or []
    rules = trace.get("business_rules") or []
    scenarios = trace.get("scenarios") or []
    if not requirements:
        return '<div class="empty-state">未记录需求追踪关系</div>'
    requirement_map = {item.get("requirement_id"): item for item in requirements}
    scenario_map = {item.get("scenario_id"): item for item in scenarios}
    rows = []
    for rule in rules:
        requirement = requirement_map.get(rule.get("requirement_id"), {})
        linked_scenarios = [item for item in scenarios if rule.get("rule_id") in (item.get("business_rule_refs") or [])]
        assertion_text = "、".join(
            f'{item.get("case_id")}/{item.get("assertion_id")}' for item in rule.get("assertion_refs") or []
        ) or "-"
        rows.append(
            '<tr>'
            f'<td><code>{esc(requirement.get("requirement_id"))}</code><strong>{esc(requirement.get("title"))}</strong></td>'
            f'<td><code>{esc(rule.get("rule_id"))}</code><p>{esc(rule.get("description"))}</p></td>'
            f'<td>{"<br>".join(f"<code>{esc(item.get("scenario_id"))}</code> {esc(item.get("name"))}" for item in linked_scenarios) or "-"}</td>'
            f'<td>{"<br>".join(f"<code>{esc(item)}</code>" for item in rule.get("case_refs") or []) or "-"}</td>'
            f'<td><span class="trace-assertions">{esc(assertion_text)}</span></td>'
            f'<td>{evidence_links(rule.get("evidence_refs"))}</td>'
            f'<td>{status_badge(rule.get("status"))}</td>'
            '</tr>'
        )
    requirement_cards = "".join(
        '<div class="coverage-card">'
        f'<div><code>{esc(item.get("requirement_id"))}</code><strong>{esc(item.get("title"))}</strong><p>{esc(item.get("description"))}</p></div>'
        f'{status_badge(item.get("status"))}'
        '</div>' for item in requirements
    )
    return (
        f'<div class="coverage-cards">{requirement_cards}</div>'
        '<div class="table-wrap"><table class="coverage-table"><thead><tr><th>需求点</th><th>业务规则</th><th>场景</th><th>用例</th><th>关键断言</th><th>证据</th><th>结论</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_case_cards(cases):
    if not cases:
        return '<div class="empty-state">未记录业务用例</div>'
    cards = []
    for index, case in enumerate(cases, start=1):
        blocked = case.get("blocked_by") or []
        blocked_html = (
            '<div class="blocked-row"><span>阻塞来源</span>'
            + "".join(f'<code>{esc(item)}</code>' for item in blocked)
            + '</div>'
            if blocked else ""
        )
        evidence = evidence_links(case.get("evidence_refs"))
        trace = case.get("execution_trace") or {}
        traceability = (
            '<div class="traceability-row">'
            f'<span>需求 {"、".join(esc(item) for item in case.get("requirement_refs") or []) or "-"}</span>'
            f'<span>规则 {"、".join(esc(item) for item in case.get("business_rule_refs") or []) or "-"}</span>'
            f'<span>场景 {"、".join(esc(item) for item in case.get("scenario_refs") or []) or "-"}</span>'
            '</div>'
        )
        cards.append(
            f'<article class="case-card tone-{esc(case.get("status", "unknown"))}">'
            '<div class="case-summary">'
            f'<div class="case-number">{index:02d}</div>'
            '<div class="case-title">'
            f'<div><code>{esc(case.get("case_id"))}</code>{classification_badge(case.get("classification"))}</div>'
            f'<h3>{esc(case.get("name") or "未命名用例")}</h3>'
            '</div>'
            f'<div class="case-status">{status_badge(case.get("status"))}'
            f'<small>{"已发送请求" if case.get("request_sent") else "未发送请求"}</small></div>'
            '</div>'
            f'{traceability}'
            '<div class="compare-grid">'
            f'<div><span>预期结果</span><p>{esc(case.get("expected") or "-")}</p></div>'
            f'<div><span>实际结果</span><p>{esc(case.get("actual") or "-")}</p></div>'
            '</div>'
            f'{blocked_html}'
            '<details>'
            '<summary><span>查看执行过程、断言与证据</span><span class="details-hint">展开</span></summary>'
            '<div class="details-body">'
            '<h4>用例执行过程</h4>'
            f'{render_trace_steps(trace.get("steps") or [], compact=True)}'
            '<h4>断言结果</h4>'
            f'{assertion_rows(case.get("assertions") or [])}'
            f'<div class="case-evidence"><span>证据文件</span>{evidence}</div>'
            '</div>'
            '</details>'
            '</article>'
        )
    return '<div class="case-list">' + "".join(cards) + "</div>"


def render_cleanup(cleanup):
    status = cleanup.get("status", "none")
    actions = cleanup.get("actions") or []
    waived_cases = cleanup.get("waived_cases") or []
    pending = cleanup.get("pending_objects") or []
    action_rows = []
    for action in actions:
        verified = "已验证" if action.get("verified") else "未验证"
        refs = [action.get("evidence_ref")] if action.get("evidence_ref") else []
        action_rows.append(
            '<div class="cleanup-action">'
            f'<code>{esc(action.get("case_id") or "-")}</code>'
            f'<span>{verified}</span>{status_badge(action.get("status"))}'
            f'{evidence_links(refs)}'
            '</div>'
        )
    if not action_rows:
        action_rows.append('<div class="empty-state compact">本次运行没有清理动作</div>')
    pending_html = "".join(f'<li><code>{esc(item)}</code></li>' for item in pending)
    if not pending_html:
        pending_html = '<li class="all-clear">没有待处理对象</li>'
    waiver_html = "".join(
        '<div class="cleanup-action waiver">'
        f'<code>{esc(item.get("waiver_id"))}</code><span>已风险豁免</span>'
        f'<p>原因：{esc(item.get("reason"))}<br>残余风险：{esc(item.get("residual_risk"))}<br>审批者：{esc(item.get("approved_by"))}<br>有效期：{format_time(item.get("approved_at"))} 至 {format_time(item.get("expires_at"))}<br>标签：{esc(item.get("data_tag"))}<br>残留对象：{display_value(item.get("object_ref"))}</p>'
        '</div>' for item in waived_cases
    )
    return (
        '<div class="cleanup-overview">'
        f'<div class="cleanup-state"><span>清理状态</span>{status_badge(status)}'
        f'<p>{esc(cleanup.get("details") or "所有副作用均已按结构化结果处理。")}</p></div>'
        f'<div class="cleanup-pending"><span>待处理对象</span><ul>{pending_html}</ul></div>'
        '</div>'
        '<div class="cleanup-actions">' + waiver_html + "".join(action_rows) + '</div>'
    )


def main():
    parser = argparse.ArgumentParser(description="Render deterministic HTML from result.json")
    parser.add_argument("result", help="Path to result.json")
    parser.add_argument("--output", required=True, help="Output HTML path")
    args = parser.parse_args()

    result_path = Path(args.result).resolve()
    output_path = Path(args.output).resolve()
    validator = Path(__file__).with_name("validate_result.py").resolve()
    validator_hash = hashlib.sha256(validator.read_bytes()).hexdigest()
    if validator_hash != VALIDATOR_SHA256:
        sys.stderr.write(
            f"validator integrity check failed: expected {VALIDATOR_SHA256}, actual {validator_hash}\n"
        )
        return 4

    checked = subprocess.run(
        [sys.executable, str(validator), str(result_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if checked.returncode != 0:
        sys.stderr.write(checked.stdout or checked.stderr)
        return checked.returncode

    data = json.loads(result_path.read_text(encoding="utf-8"))
    summary = data.get("summary") or {"total": len(data.get("case_results") or []), "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "not_run": 0, "executed": 0}
    project = data.get("project", {})
    environment = data.get("environment", {})
    cleanup = data.get("cleanup", {})
    gates = data.get("gate_results", [])
    cases = data.get("case_results", [])
    known_gaps = data.get("known_gaps", [])
    contract = data.get("contract", {})
    requirement_trace = data.get("requirement_trace", {})
    execution_trace = data.get("execution_trace", {})
    coverage = data.get("coverage", {})
    plan = data.get("plan", {})
    review = data.get("review", {})
    feasibility = data.get("feasibility", {})
    package_build = data.get("package_build", {})
    runtime_status = environment.get("runtime_status") or {}
    auth_status_text = {
        "verified": "已验证（认证探测通过）",
        "declared_only": "仅确认凭据存在，未验证有效期",
        "authentication_failed": "认证失败",
    }.get(runtime_status.get("auth"), "未知")

    if summary["executed"]:
        pass_percent = summary["passed"] / summary["executed"] * 100
        pass_rate = f'{pass_percent:.1f}%'
        pass_fraction = f'{summary["passed"]}/{summary["executed"]}'
        progress_width = f'{pass_percent:.1f}%'
    else:
        pass_rate = "不计算"
        pass_fraction = "无实际执行用例"
        progress_width = "0%"

    generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    verdict = data.get("verdict", "inconclusive")
    verdict_title, verdict_description = VERDICT_LABELS.get(verdict, (verdict.upper(), "测试运行已完成"))
    requirement_refs = project.get("requirement_refs") or []
    requirement_text = "、".join(str(item) for item in requirement_refs) or "-"
    gap_items = render_gap_items(known_gaps)
    gate_passed = sum(1 for gate in gates if gate.get("status") == "passed")
    gate_problem = len(gates) - gate_passed
    contract_fingerprint = contract.get("contract_fingerprint") or "-"
    plan_fingerprint = plan.get("plan_fingerprint") or "-"
    if len(plan_fingerprint) > 30:
        plan_fingerprint = plan_fingerprint[:22] + "…" + plan_fingerprint[-8:]
    if len(contract_fingerprint) > 30:
        contract_fingerprint = contract_fingerprint[:22] + "…" + contract_fingerprint[-8:]

    body = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>{esc(data.get("run_id"))} 接口测试报告</title>
<style>
:root{{--ink:#182033;--muted:#667085;--soft:#98a2b3;--line:#e6eaf0;--panel:#ffffff;--canvas:#f3f6fa;--navy:#15253f;--blue:#3867d6;--blue-soft:#edf3ff;--green:#16835b;--green-soft:#e9f8f1;--red:#c23a43;--red-soft:#fff0f1;--amber:#b06b00;--amber-soft:#fff7e6;--shadow:0 12px 35px rgba(19,35,61,.08);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--ink);background:var(--canvas);line-height:1.55}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--canvas)}}a{{color:inherit}}code{{font-family:"SFMono-Regular",Consolas,"Liberation Mono",monospace}}.shell{{max-width:1240px;margin:0 auto;padding:34px 28px 64px}}.hero{{position:relative;overflow:hidden;background:linear-gradient(135deg,#14243e 0%,#203a67 58%,#315ca8 100%);color:#fff;border-radius:24px;padding:32px 34px 30px;box-shadow:0 22px 55px rgba(21,37,63,.2)}}.hero:after{{content:"";position:absolute;right:-90px;top:-150px;width:420px;height:420px;border:1px solid rgba(255,255,255,.15);border-radius:50%;box-shadow:0 0 0 58px rgba(255,255,255,.045),0 0 0 116px rgba(255,255,255,.025)}}.hero-top,.hero-main,.hero-meta{{position:relative;z-index:1}}.eyebrow{{display:flex;align-items:center;gap:10px;font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:#c9d6ed;font-weight:700}}.eyebrow:before{{content:"";width:28px;height:2px;background:#8fb5ff}}.hero-main{{display:flex;align-items:flex-end;justify-content:space-between;gap:30px;margin-top:22px}}.hero h1{{font-size:36px;line-height:1.15;margin:0 0 10px;letter-spacing:-.02em}}.hero-subtitle{{margin:0;color:#d8e2f3;font-size:14px}}.verdict{{min-width:300px;padding:17px 19px;border-radius:16px;border:1px solid rgba(255,255,255,.2);background:rgba(255,255,255,.1);backdrop-filter:blur(8px)}}.verdict span{{display:block;font-size:11px;letter-spacing:.14em;color:#c9d6ed;font-weight:700}}.verdict strong{{display:block;font-size:30px;line-height:1.1;margin:4px 0 7px}}.verdict p{{margin:0;color:#e3eaf6;font-size:13px}}.verdict-passed strong{{color:#71e0b5}}.verdict-failed strong,.verdict-error strong{{color:#ff9ca3}}.verdict-blocked strong,.verdict-partial strong{{color:#ffd37a}}.hero-meta{{display:flex;flex-wrap:wrap;gap:10px;margin-top:26px}}.hero-meta span{{font-size:12px;color:#dbe5f4;background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.12);padding:6px 10px;border-radius:8px}}.hero-meta code{{color:#fff}}
.section-nav{{position:sticky;top:12px;z-index:5;display:flex;gap:5px;margin:18px 0;padding:6px;background:rgba(255,255,255,.88);border:1px solid rgba(225,230,237,.95);border-radius:14px;box-shadow:0 8px 25px rgba(31,48,76,.08);backdrop-filter:blur(12px);overflow:auto}}.section-nav a{{white-space:nowrap;text-decoration:none;color:#566176;font-size:12px;font-weight:650;padding:8px 12px;border-radius:9px}}.section-nav a:hover{{color:var(--blue);background:var(--blue-soft)}}
.metrics{{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:18px 0}}.metric{{background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:16px 17px;box-shadow:0 5px 18px rgba(25,43,70,.04)}}.metric span{{display:block;color:var(--muted);font-size:12px;margin-bottom:5px}}.metric strong{{font-size:25px;line-height:1.15;letter-spacing:-.02em}}.metric small{{display:block;color:var(--soft);font-size:11px;margin-top:5px}}.metric.metric-pass strong{{color:var(--green)}}.metric.metric-fail strong{{color:var(--red)}}.metric.metric-warn strong{{color:var(--amber)}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:24px 25px;margin-top:16px;box-shadow:var(--shadow);scroll-margin-top:90px}}.section-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:19px}}.section-title{{display:flex;gap:13px;align-items:flex-start}}.section-number{{display:flex;align-items:center;justify-content:center;min-width:34px;height:34px;border-radius:10px;background:var(--navy);color:#fff;font-size:12px;font-weight:800}}.section-head h2{{font-size:19px;line-height:1.25;margin:1px 0 4px}}.section-head p{{margin:0;color:var(--muted);font-size:13px}}.section-kpi{{font-size:12px;color:var(--muted);padding:6px 10px;border:1px solid var(--line);border-radius:9px;background:#fafbfc}}
.info-grid{{display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--line);border-radius:14px;overflow:hidden}}.info-item{{padding:15px 17px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);min-width:0}}.info-item:nth-child(3n){{border-right:0}}.info-item:nth-last-child(-n+3){{border-bottom:0}}.info-item span,.compare-grid span,.result-meta>div>span,.case-evidence>span,.cleanup-state>span,.cleanup-pending>span{{display:block;color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;margin-bottom:5px}}.info-item strong,.info-item code{{font-size:13px;word-break:break-all}}
.progress-wrap{{display:grid;grid-template-columns:180px 1fr;align-items:center;gap:22px;padding:19px;border:1px solid var(--line);border-radius:15px;background:#fbfcfe;margin-bottom:16px}}.rate strong{{display:block;font-size:31px;line-height:1;color:var(--green)}}.rate span{{font-size:12px;color:var(--muted)}}.progress-bar{{height:12px;border-radius:99px;background:#e8edf3;overflow:hidden}}.progress-bar i{{display:block;height:100%;width:{progress_width};background:linear-gradient(90deg,#16835b,#43b889);border-radius:inherit}}.progress-caption{{display:flex;justify-content:space-between;color:var(--muted);font-size:11px;margin-top:7px}}
.result-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}.result-card,.case-card{{position:relative;border:1px solid var(--line);border-radius:15px;overflow:hidden;background:#fff}}.result-card:before,.case-card:before{{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:#9aa5b5}}.tone-passed:before{{background:var(--green)}}.tone-failed:before,.tone-error:before{{background:var(--red)}}.tone-not_run:before,.tone-skipped:before{{background:var(--amber)}}.result-card{{padding:17px 18px 16px 21px}}.result-head,.case-summary{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}}.result-index{{display:flex;gap:7px;align-items:center}}.result-index span,.tag{{font-size:10px;font-weight:800;letter-spacing:.07em;color:var(--blue);background:var(--blue-soft);padding:3px 6px;border-radius:5px}}.result-index code{{font-size:12px;color:#45526a}}.result-status{{display:flex;gap:6px;align-items:center}}.tag.subtle{{color:#697386;background:#f1f3f6}}.result-card h3{{font-size:15px;margin:14px 0 5px}}.actual{{font-size:13px;color:#566176;margin:0;min-height:40px}}.result-meta{{display:grid;grid-template-columns:100px 1fr;gap:12px;border-top:1px solid var(--line);margin-top:14px;padding-top:12px}}.classification{{display:inline-flex;color:#44516a;background:#f1f4f7;border-radius:6px;padding:3px 7px;font-size:11px;font-weight:650}}.quiet{{color:var(--soft);font-size:11px}}.status{{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;border-radius:99px;padding:4px 9px;font-size:11px;font-weight:750}}.status i{{width:6px;height:6px;border-radius:50%;background:currentColor}}.status-passed{{color:var(--green);background:var(--green-soft)}}.status-failed,.status-error{{color:var(--red);background:var(--red-soft)}}.status-skipped,.status-not_run{{color:var(--amber);background:var(--amber-soft)}}.status-not_required,.status-none{{color:#546176;background:#eef1f5}}.status-unknown{{color:#546176;background:#eef1f5}}.evidence-list{{display:flex;flex-wrap:wrap;gap:5px}}.evidence-link{{display:inline-flex;align-items:center;gap:5px;max-width:100%;color:#35558f;text-decoration:none;font-size:10px;background:#f4f7fc;border:1px solid #e2e9f5;border-radius:6px;padding:4px 6px}}.evidence-link span{{font-size:8px;font-weight:800;color:#fff;background:#5275b6;border-radius:3px;padding:1px 3px}}.evidence-link:hover{{border-color:#9eb6df;background:#edf3ff}}
.case-list{{display:grid;gap:11px}}.case-card{{padding-left:4px}}.case-summary{{padding:17px 18px 14px}}.case-number{{font-size:22px;font-weight:800;color:#d2d8e1;line-height:1}}.case-title{{flex:1;min-width:0}}.case-title>div{{display:flex;align-items:center;gap:8px}}.case-title code{{font-size:11px;color:#536078}}.case-title h3{{font-size:15px;margin:5px 0 0}}.case-status{{display:flex;flex-direction:column;align-items:flex-end;gap:5px}}.case-status small{{font-size:10px;color:var(--soft)}}.compare-grid{{display:grid;grid-template-columns:1fr 1fr;border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:#fbfcfe}}.compare-grid>div{{padding:13px 18px}}.compare-grid>div+div{{border-left:1px solid var(--line)}}.compare-grid p{{font-size:12px;color:#46536a;margin:0}}.blocked-row{{display:flex;gap:7px;align-items:center;padding:10px 18px;background:var(--amber-soft);font-size:11px;color:var(--amber)}}.blocked-row code{{background:#fff3d2;padding:2px 5px;border-radius:4px}}details summary{{display:flex;justify-content:space-between;align-items:center;cursor:pointer;padding:11px 18px;color:#536078;font-size:11px;font-weight:700;list-style:none}}details summary::-webkit-details-marker{{display:none}}details[open] .details-hint{{font-size:0}}details[open] .details-hint:after{{content:"收起";font-size:10px}}.details-hint{{color:var(--blue);font-size:10px}}.details-body{{border-top:1px solid var(--line);padding:14px 18px 17px}}.assertion-list{{border:1px solid var(--line);border-radius:10px;overflow:hidden}}.assertion-row{{display:grid;grid-template-columns:1.25fr 1fr 1fr auto;align-items:center;gap:12px;padding:10px 12px;font-size:11px}}.assertion-row+.assertion-row{{border-top:1px solid var(--line)}}.assertion-row strong{{display:block;font-size:11px}}.assertion-row small{{color:var(--soft)}}.assertion-row span{{display:block;color:var(--soft);font-size:9px;text-transform:uppercase}}.assertion-row code{{word-break:break-word}}.case-evidence{{display:grid;grid-template-columns:90px 1fr;gap:10px;margin-top:12px;align-items:start}}
.traceability-row{{display:flex;flex-wrap:wrap;gap:8px;padding:9px 18px;border-top:1px solid var(--line);background:#f7f9fc}}.traceability-row span{{font-size:10px;color:#526079;background:#fff;border:1px solid var(--line);border-radius:6px;padding:3px 7px}}.details-body h4{{font-size:12px;margin:2px 0 9px}}.details-body h4:not(:first-child){{margin-top:16px}}.trace-list{{display:grid;gap:0;border:1px solid var(--line);border-radius:13px;overflow:hidden}}.trace-step{{display:grid;grid-template-columns:32px minmax(0,1fr) auto;align-items:start;gap:11px;padding:11px 13px;background:#fff}}.trace-step+.trace-step{{border-top:1px solid var(--line)}}.trace-seq{{display:flex;align-items:center;justify-content:center;width:27px;height:27px;border-radius:8px;background:var(--navy);color:#fff;font-size:10px;font-weight:800}}.trace-copy strong{{display:block;font-size:12px}}.trace-copy small{{display:block;color:var(--muted);font-size:10px;margin-top:2px;word-break:break-word}}.trace-copy .evidence-list{{margin-top:6px}}.trace-list.compact .trace-step{{padding:9px 11px}}.coverage-cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px;margin-bottom:13px}}.coverage-card{{display:flex;justify-content:space-between;gap:14px;border:1px solid var(--line);border-radius:12px;padding:14px 15px;background:#fbfcfe}}.coverage-card code{{font-size:10px;color:var(--blue)}}.coverage-card strong{{display:block;font-size:13px;margin:3px 0}}.coverage-card p{{margin:0;color:var(--muted);font-size:11px}}.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:13px}}.coverage-table{{width:100%;border-collapse:collapse;font-size:11px;min-width:1000px}}.coverage-table th{{text-align:left;background:#f5f7fa;color:#5d687c;font-size:10px;letter-spacing:.04em;text-transform:uppercase;padding:10px;border-bottom:1px solid var(--line)}}.coverage-table td{{vertical-align:top;padding:11px 10px;border-bottom:1px solid var(--line)}}.coverage-table tr:last-child td{{border-bottom:0}}.coverage-table td strong{{display:block;font-size:11px;margin-top:4px}}.coverage-table td p{{margin:4px 0 0;color:var(--muted)}}.trace-assertions{{font-family:Consolas,monospace;color:#536078;word-break:break-word}}.cleanup-overview{{display:grid;grid-template-columns:1.3fr 1fr;border:1px solid var(--line);border-radius:14px;overflow:hidden}}.cleanup-state,.cleanup-pending{{padding:17px 18px}}.cleanup-pending{{border-left:1px solid var(--line)}}.cleanup-state p{{font-size:12px;color:var(--muted);margin:9px 0 0}}.cleanup-pending ul,.gap-list{{margin:3px 0 0;padding-left:18px;font-size:12px;color:#536078}}.all-clear{{color:var(--green)}}.cleanup-actions{{display:grid;gap:7px;margin-top:10px}}.cleanup-action{{display:grid;grid-template-columns:1fr auto auto 2fr;align-items:center;gap:12px;border:1px solid var(--line);border-radius:10px;padding:9px 12px;font-size:11px}}.empty-state{{border:1px dashed #d4dae3;background:#fafbfd;color:var(--soft);border-radius:12px;padding:24px;text-align:center;font-size:12px}}.empty-state.compact{{padding:11px}}
.footer{{display:flex;justify-content:space-between;gap:20px;padding:22px 4px 0;color:#7b879a;font-size:11px}}.footer code{{color:#536078}}.print-note{{display:none}}
@media(max-width:900px){{.metrics{{grid-template-columns:repeat(3,1fr)}}.hero-main{{align-items:flex-start;flex-direction:column}}.verdict{{min-width:0;width:100%}}.info-grid{{grid-template-columns:repeat(2,1fr)}}.info-item:nth-child(3n){{border-right:1px solid var(--line)}}.info-item:nth-child(2n){{border-right:0}}.info-item:nth-last-child(-n+3){{border-bottom:1px solid var(--line)}}.info-item:nth-last-child(-n+2){{border-bottom:0}}.result-grid{{grid-template-columns:1fr}}}}
@media(max-width:640px){{.shell{{padding:16px 12px 40px}}.hero{{padding:25px 22px;border-radius:18px}}.hero h1{{font-size:28px}}.metrics{{grid-template-columns:repeat(2,1fr)}}.section-nav{{top:5px}}.panel{{padding:19px 16px;border-radius:16px}}.info-grid{{grid-template-columns:1fr}}.info-item,.info-item:nth-child(2n),.info-item:nth-child(3n){{border-right:0;border-bottom:1px solid var(--line)}}.info-item:last-child{{border-bottom:0}}.progress-wrap{{grid-template-columns:1fr}}.case-summary{{display:grid;grid-template-columns:auto 1fr}}.case-status{{grid-column:2;align-items:flex-start}}.compare-grid{{grid-template-columns:1fr}}.compare-grid>div+div{{border-left:0;border-top:1px solid var(--line)}}.assertion-row{{grid-template-columns:1fr 1fr}}.cleanup-overview{{grid-template-columns:1fr}}.cleanup-pending{{border-left:0;border-top:1px solid var(--line)}}.cleanup-action{{grid-template-columns:1fr auto}}.trace-step{{grid-template-columns:28px 1fr}}.trace-step>.status{{grid-column:2;justify-self:start}}.footer{{flex-direction:column}}}}
@media print{{body{{background:#fff}}.shell{{max-width:none;padding:0}}.hero{{box-shadow:none;border-radius:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}}.section-nav{{display:none}}.panel{{box-shadow:none;break-inside:avoid}}details{{break-inside:avoid}}details .details-body{{display:block}}.evidence-link{{text-decoration:none}}.print-note{{display:block}}}}
</style>
</head>
<body>
<main class="shell">
<header class="hero">
  <div class="hero-top"><div class="eyebrow">API QUALITY REPORT</div></div>
  <div class="hero-main">
    <div><h1>接口测试执行报告</h1><p class="hero-subtitle">结构化结果驱动 · 校验后确定性生成 · 证据可追溯</p></div>
    <div class="verdict verdict-{esc(verdict)}"><span>FINAL VERDICT</span><strong>{esc(verdict_title)}</strong><p>{esc(verdict_description)}</p></div>
  </div>
  <div class="hero-meta">
    <span>运行 ID <code>{esc(data.get("run_id"))}</code></span>
    <span>模式 <code>{esc(data.get("mode"))}</code></span>
    <span>环境 <code>{esc(environment.get("environment_id") or "-")}</code></span>
    <span>审核 <code>{esc(review.get("status") or "未审核")}</code></span>
    <span>生成于 {format_time(generated_at)}</span>
  </div>
</header>
<nav class="section-nav" aria-label="报告导航">
  <a href="#overview">执行概览</a><a href="#context">项目环境</a><a href="#feasibility">可行性规划</a><a href="#coverage">需求覆盖</a><a href="#execution">执行过程</a><a href="#gates">门禁结果</a><a href="#cases">业务用例</a><a href="#cleanup">清理副作用</a><a href="#gaps">覆盖缺口</a>
</nav>
<section class="metrics" id="overview">
  <div class="metric"><span>业务用例</span><strong>{summary["total"]}</strong><small>计划验证总数</small></div>
  <div class="metric"><span>实际执行</span><strong>{summary["executed"]}</strong><small>不含跳过与未执行</small></div>
  <div class="metric metric-pass"><span>通过</span><strong>{summary["passed"]}</strong><small>{pass_fraction}</small></div>
  <div class="metric metric-fail"><span>失败 / 错误</span><strong>{summary["failed"] + summary["errors"]}</strong><small>{summary["failed"]} 失败 · {summary["errors"]} 错误</small></div>
  <div class="metric metric-warn"><span>未执行</span><strong>{summary["not_run"]}</strong><small>{summary["skipped"]} 条跳过</small></div>
  <div class="metric"><span>门禁</span><strong>{gate_passed}/{len(gates)}</strong><small>{gate_problem} 项异常</small></div>
</section>
<section class="panel" id="context">
  <div class="section-head"><div class="section-title"><span class="section-number">01</span><div><h2>项目与执行环境</h2><p>本次结果对应的系统、契约、目标环境和时间边界</p></div></div><span class="section-kpi">耗时 {esc(format_duration(data.get("started_at"), data.get("finished_at")))}</span></div>
  <div class="info-grid">
    <div class="info-item"><span>系统</span><strong>{esc(project.get("system_id") or "-")}</strong></div>
    <div class="info-item"><span>需求引用</span><strong>{esc(requirement_text)}</strong></div>
    <div class="info-item"><span>Package</span><code>{esc(data.get("package_id") or "-")}</code></div>
    <div class="info-item"><span>Base URL</span><code>{esc(environment.get("base_url") or "-")}</code></div>
    <div class="info-item"><span>环境安全性</span><strong>{"已确认非生产" if environment.get("production") is False else "未确认非生产"}</strong></div>
    <div class="info-item"><span>认证状态</span><strong>{esc(auth_status_text)}</strong></div>
    <div class="info-item"><span>计划标题</span><strong>{esc(plan.get("title") or "-")}</strong></div>
    <div class="info-item"><span>计划指纹</span><code title="{esc(plan.get("plan_fingerprint") or "-")}">{esc(plan_fingerprint)}</code></div>
    <div class="info-item"><span>审核人 / 审核时间</span><strong>{esc(review.get("reviewer") or "-")} · {format_time(review.get("approved_at"))}</strong></div>
    <div class="info-item"><span>契约指纹</span><code title="{esc(contract.get("contract_fingerprint") or "-")}">{esc(contract_fingerprint)}</code></div>
    <div class="info-item"><span>开始时间</span><strong>{format_time(data.get("started_at"))}</strong></div>
    <div class="info-item"><span>结束时间</span><strong>{format_time(data.get("finished_at"))}</strong></div>
    <div class="info-item"><span>报告生成</span><strong>{format_time(generated_at)}</strong></div>
  </div>
</section>
<section class="panel" id="feasibility">
  <div class="section-head"><div class="section-title"><span class="section-number">02</span><div><h2>候选用例与执行可行性</h2><p>先设计候选用例，再规划数据、凭证、环境、夹具、Oracle 和清理依赖；仅通过静态门禁的批准范围进入建包</p></div></div><span class="section-kpi">{esc((feasibility.get("gate") or {}).get("status") or "legacy")}</span></div>
  {render_feasibility(feasibility, package_build)}
</section>
<section class="panel" id="coverage">
  <div class="section-head"><div class="section-title"><span class="section-number">03</span><div><h2>需求与业务规则覆盖</h2><p>以需求点为起点，追踪到业务规则、测试场景、用例、关键断言和原始证据</p></div></div><span class="section-kpi">{(requirement_trace.get("summary") or {}).get("rules_passed", 0)}/{(requirement_trace.get("summary") or {}).get("rules_total", 0)} 规则通过</span></div>
  <div class="coverage-cards">
    <div class="coverage-card"><div><code>DESIGN</code><strong>设计覆盖</strong><p>有测试设计和 AC 映射</p></div><strong>{esc((coverage.get("design") or {}).get("numerator", 0))}/{esc((coverage.get("design") or {}).get("denominator", 0))}</strong></div>
    <div class="coverage-card"><div><code>TOUCHED</code><strong>执行触达</strong><p>至少发送业务请求</p></div><strong>{esc((coverage.get("execution_touch") or {}).get("numerator", 0))}/{esc((coverage.get("execution_touch") or {}).get("denominator", 0))}</strong></div>
    <div class="coverage-card"><div><code>COMPLETE</code><strong>完整场景覆盖</strong><p>关键 Oracle 均有证据</p></div><strong>{esc((coverage.get("complete_scenario") or {}).get("numerator", 0))}/{esc((coverage.get("complete_scenario") or {}).get("denominator", 0))}</strong></div>
    <div class="coverage-card"><div><code>PASS</code><strong>验收通过</strong><p>需求级结论为 passed</p></div><strong>{esc((coverage.get("acceptance_pass") or {}).get("numerator", 0))}/{esc((coverage.get("acceptance_pass") or {}).get("denominator", 0))}</strong></div>
  </div>
  <p class="print-note">用例执行通过率不等于需求验收通过率；以上需求覆盖率统一以需求验收项为分母。</p>
  {render_requirement_matrix(requirement_trace)}
</section>
<section class="panel" id="execution">
  <div class="section-head"><div class="section-title"><span class="section-number">04</span><div><h2>完整执行过程</h2><p>从测试包校验、门禁、夹具、业务用例到清理和最终结论的全链路入口</p></div></div><span class="section-kpi">{len(execution_trace.get("steps") or [])} 个步骤</span></div>
  {render_trace_steps(execution_trace.get("steps") or [])}
</section>
<section class="panel" id="gates">
  <div class="section-head"><div class="section-title"><span class="section-number">05</span><div><h2>执行门禁</h2><p>门禁独立于业务通过率，用于保证环境、鉴权、清理和运行条件可信</p></div></div><span class="section-kpi">{gate_passed} 通过 · {gate_problem} 异常</span></div>
  {render_gate_cards(gates)}
</section>
<section class="panel" id="cases">
  <div class="section-head"><div class="section-title"><span class="section-number">06</span><div><h2>业务用例</h2><p>逐条展示需求映射、执行过程、预期、实际、断言和证据引用</p></div></div><span class="section-kpi">{summary["executed"]}/{summary["total"]} 实际执行</span></div>
  <div class="progress-wrap"><div class="rate"><strong>{esc(pass_rate)}</strong><span>执行通过率</span></div><div><div class="progress-bar"><i></i></div><div class="progress-caption"><span>通过 {summary["passed"]}</span><span>分母仅含实际执行用例 {summary["executed"]}</span></div></div></div>
  {render_case_cards(cases)}
</section>
<section class="panel" id="cleanup">
  <div class="section-head"><div class="section-title"><span class="section-number">07</span><div><h2>清理与数据副作用</h2><p>确认写操作产生的数据已执行 teardown；若显示“已风险豁免”，仅表示结构化风险被接受，不表示清理通过</p></div></div></div>
  {render_cleanup(cleanup)}
</section>
<section class="panel" id="gaps">
  <div class="section-head"><div class="section-title"><span class="section-number">08</span><div><h2>已知覆盖缺口</h2><p>阻塞项会阻止推进，覆盖边界仅说明本次未覆盖范围</p></div></div><span class="section-kpi">{len(known_gaps)} 项</span></div>
  <ul class="gap-list">{gap_items}</ul>
</section>
<footer class="footer"><span>此报告由已校验的 <code>result.json</code> 确定性生成，业务结论不由页面脚本计算。</span><span>Schema {esc(data.get("schema_version"))} · Validator {esc(validator_hash[:12])}</span></footer>
</main>
</body>
</html>'''

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(body, encoding="utf-8")
    print(json.dumps({"report": str(output_path), "run_id": data.get("run_id"), "verdict": verdict, "summary": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
