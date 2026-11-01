#!/usr/bin/env python3
import argparse
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

EXECUTION = {"not_run", "partial", "complete"}
VERDICTS = {"passed", "failed", "blocked", "inconclusive", "not_run"}
ISSUE_TYPES = {"defect", "test_gap"}
SEVERITIES = {"blocker", "high", "medium", "low"}


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def pct(count, total):
    return f"{(count / total * 100):.1f}%" if total else "0.0%"


def validate(data):
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
            elif not any(run.get("kind") == "business_execution" for run in referenced):
                errors.append(f"{prefix}: passed requires business_execution evidence")
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
    return errors


def overall_verdict(data):
    items = data["acceptance_items"]
    issues = data.get("issues") or []
    open_blocker = any(i.get("type") == "defect" and i.get("severity") == "blocker" and i.get("status") not in {"fixed", "closed"} for i in issues)
    failed = any(i.get("execution_status") == "complete" and i.get("verdict") == "failed" for i in items)
    blocked = any(i.get("verdict") == "blocked" for i in items)
    all_passed = bool(items) and all(i.get("execution_status") == "complete" and i.get("verdict") == "passed" for i in items)
    if open_blocker or failed:
        return "FAIL", "存在阻断缺陷或完整执行的验收项失败"
    if blocked:
        return "BLOCKED", "必需验收项被门禁或外部条件阻塞"
    if all_passed:
        return "PASS", "全部验收项已完整执行并通过"
    return "INCONCLUSIVE", "覆盖或证据不足，尚不能形成通过结论"


def badge(value):
    labels = {"passed": "通过", "failed": "失败", "blocked": "阻塞", "inconclusive": "证据不足", "not_run": "未执行", "partial": "部分执行", "complete": "完整执行", "defect": "缺陷", "test_gap": "测试缺口"}
    return f'<span class="badge b-{esc(value)}">{esc(labels.get(value, value))}</span>'


def render(data):
    req = data["requirement"]
    baseline = data.get("baseline") or {}
    items = data["acceptance_items"]
    issues = data.get("issues") or []
    runs = data.get("evidence_runs") or []
    total = req["acceptance_total"]
    touched = sum(i.get("execution_status") in {"partial", "complete"} for i in items)
    complete = sum(i.get("execution_status") == "complete" for i in items)
    passed = sum(i.get("verdict") == "passed" for i in items)
    verdict, verdict_reason = overall_verdict(data)
    generated = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    item_rows = []
    for item in items:
        gaps = "；".join(item.get("gaps") or []) or "-"
        evidence = "、".join(item.get("evidence_refs") or []) or "-"
        issue_refs = "、".join(item.get("issue_refs") or []) or "-"
        item_rows.append(f'<tr data-state="{esc(item.get("execution_status"))}"><td><code>{esc(item.get("id"))}</code><strong>{esc(item.get("title"))}</strong></td><td>{badge(item.get("execution_status"))}</td><td>{badge(item.get("verdict"))}</td><td>{esc(item.get("actual") or "-")}</td><td>{esc(gaps)}</td><td><code>{esc(evidence)}</code></td><td><code>{esc(issue_refs)}</code></td></tr>')
    issue_cards = []
    for issue in issues:
        issue_cards.append(f'<article class="issue"><div><code>{esc(issue.get("id"))}</code>{badge(issue.get("type"))}<span class="severity">{esc(issue.get("severity"))}</span></div><h3>{esc(issue.get("title"))}</h3><p>{esc(issue.get("actual") or "")}</p><small>影响：{esc(issue.get("impact") or "-")} · 关联：{esc("、".join(issue.get("acceptance_refs") or []) or "-")}</small></article>')
    run_cards = []
    for run in runs:
        path = run.get("report_path") or run.get("result_path") or ""
        run_cards.append(f'<article class="run"><div><code>{esc(run.get("id"))}</code><span>{esc(run.get("kind"))}</span></div><p>{esc(run.get("summary") or "")}</p><small>{esc(path)}</small></article>')
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(req["id"])} 需求验收报告</title><style>
:root{{--ink:#182033;--muted:#667085;--line:#e4e7ec;--bg:#f7f8fa;--panel:#fff;--red:#b42318;--redbg:#fef3f2;--amber:#b54708;--amberbg:#fffaeb;--green:#027a48;--greenbg:#ecfdf3;--blue:#175cd3;--bluebg:#eff8ff;font-family:Inter,"Microsoft YaHei",sans-serif;color:var(--ink)}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg)}}main{{max-width:1240px;margin:auto;padding:28px}}header{{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:24px}}h1{{font-size:24px;margin:5px 0}}h2{{font-size:18px;margin:0 0 14px}}h3{{font-size:14px;margin:7px 0}}p{{line-height:1.6}}code{{font-family:Consolas,monospace}}.top{{display:flex;justify-content:space-between;gap:18px}}.eyebrow,.muted,small{{color:var(--muted)}}.verdict{{font-size:28px;font-weight:700;color:var(--red)}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0}}.metric{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}}.metric strong{{display:block;font-size:24px}}section{{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;margin-top:14px}}.baseline{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}.baseline div{{background:var(--bg);padding:10px;border-radius:8px}}.baseline span{{display:block;color:var(--muted);font-size:12px}}.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;min-width:1100px;font-size:12px}}th,td{{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid var(--line)}}td strong{{display:block;margin-top:4px}}.badge{{display:inline-block;padding:3px 7px;border-radius:99px;font-size:11px;white-space:nowrap}}.b-failed,.b-defect{{color:var(--red);background:var(--redbg)}}.b-partial,.b-blocked,.b-test_gap,.b-inconclusive{{color:var(--amber);background:var(--amberbg)}}.b-passed,.b-complete{{color:var(--green);background:var(--greenbg)}}.b-not_run{{color:var(--muted);background:var(--bg)}}.filters{{display:flex;gap:6px;margin-bottom:10px}}button{{border:1px solid var(--line);background:#fff;border-radius:8px;padding:7px 10px;cursor:pointer}}button.on{{color:var(--blue);background:var(--bluebg);border-color:#84adff}}.grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}}.issue,.run{{border:1px solid var(--line);border-radius:12px;padding:13px}}.issue p,.run p{{margin:6px 0}}.severity{{float:right;color:var(--muted);font-size:11px}}.recommend{{border-left:4px solid var(--red);background:var(--redbg);padding:12px;border-radius:8px}}footer{{color:var(--muted);font-size:11px;padding:16px 3px}}@media(max-width:700px){{main{{padding:12px}}.metrics,.baseline,.grid{{grid-template-columns:1fr 1fr}}.top{{display:block}}}}</style></head><body><main><header><div class="top"><div><div class="eyebrow">需求验收报告 · {esc(data.get("report_version") or "-")}</div><h1>{esc(req["id"])} {esc(req["title"])}</h1><div class="muted">PRD {esc(req.get("prd_version") or "-")} · {esc(baseline.get("branch") or "-")} · {esc(baseline.get("environment") or "-")}</div></div><div><div class="verdict">{verdict}</div><small>{esc(verdict_reason)}</small></div></div></header><div class="metrics"><div class="metric"><span>执行触达率</span><strong>{pct(touched,total)}</strong><small>{touched}/{total}</small></div><div class="metric"><span>完整覆盖率</span><strong>{pct(complete,total)}</strong><small>{complete}/{total}</small></div><div class="metric"><span>验收通过率</span><strong>{pct(passed,total)}</strong><small>{passed}/{total}</small></div><div class="metric"><span>开放问题</span><strong>{sum(i.get("status") not in {"fixed","closed"} for i in issues)}</strong><small>缺陷与测试缺口</small></div></div><section><h2>验收基线</h2><div class="baseline"><div><span>分支</span><code>{esc(baseline.get("branch") or "-")}</code></div><div><span>提交</span><code>{esc(baseline.get("commit") or "-")}</code></div><div><span>环境</span><code>{esc(baseline.get("environment") or "-")}</code></div><div><span>PRD</span><code>{esc(req.get("prd_path") or "-")}</code></div></div><p class="recommend"><strong>发布建议：</strong>{esc(data.get("release_recommendation") or "未提供")}</p></section><section><h2>AC 验收矩阵</h2><div class="filters"><button class="on" data-filter="all">全部 {total}</button><button data-filter="partial">已触达 {touched}</button><button data-filter="not_run">未触达 {total-touched}</button></div><div class="table-wrap"><table><thead><tr><th>验收项</th><th>执行状态</th><th>结论</th><th>实际结果</th><th>缺口</th><th>证据</th><th>问题</th></tr></thead><tbody>{''.join(item_rows)}</tbody></table></div></section><section><h2>缺陷与测试缺口</h2><div class="grid">{''.join(issue_cards) or '<p>无已登记问题</p>'}</div></section><section><h2>不可变证据索引</h2><div class="grid">{''.join(run_cards) or '<p>无证据索引</p>'}</div></section><footer>由结构化 requirement-report.json 确定性生成 · {esc(generated)} · 历史运行证据未被修改</footer></main><script>document.querySelectorAll('button[data-filter]').forEach(b=>b.onclick=()=>{{document.querySelectorAll('button[data-filter]').forEach(x=>x.classList.remove('on'));b.classList.add('on');const f=b.dataset.filter;document.querySelectorAll('tbody tr').forEach(r=>r.style.display=f==='all'||(f==='partial'?r.dataset.state!=='not_run':r.dataset.state===f)?'':'none')}})</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description="Render deterministic requirement acceptance report")
    parser.add_argument("manifest")
    parser.add_argument("--output")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    manifest = Path(args.manifest).resolve()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    errors = validate(data)
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
    output.write_text(render(data), encoding="utf-8")
    verdict, _ = overall_verdict(data)
    print(json.dumps({"report": str(output), "verdict": verdict, "requirement": data["requirement"]["id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
