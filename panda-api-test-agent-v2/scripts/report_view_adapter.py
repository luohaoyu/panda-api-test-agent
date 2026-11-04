#!/usr/bin/env python3
"""Build a read-only, deterministic requirement report view from immutable runs."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

REPORT_VIEW_VERSION = "1.2"
_EXECUTED = {"passed", "failed", "error"}
_TEMPLATE_VAR = re.compile(r"\$\{([^}]+)\}|:([A-Za-z_][A-Za-z0-9_]*)")


def _read_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path.name} 读取失败：{type(exc).__name__}"
    if not isinstance(value, dict):
        return None, f"{path.name} 不是 JSON object"
    return value, None


def _resolve_result(path_value: Any, base_dir: str | Path | None) -> Path:
    path = Path(str(path_value))
    if path.is_absolute():
        return path.resolve()
    base = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
    for _ in range(7):
        candidate = (base / path).resolve()
        if candidate.is_file():
            return candidate
        if base.parent == base:
            break
        base = base.parent
    return (Path(base_dir).resolve() / path).resolve() if base_dir else path.resolve()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return [str(item) for item in _as_list(value) if item is not None and str(item).strip()]


def _display(value: Any) -> str:
    """Format evidence without truncation, masking, hashing, or semantic rewriting."""
    if value is None:
        return "报文缺失"
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) if isinstance(value, (dict, list)) else str(value)


def _normalized_path(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip().replace("\\", "/")
    parsed = urlsplit(raw if "://" in raw else "//placeholder" + (raw if raw.startswith("/") else "/" + raw))
    path = parsed.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    path = _TEMPLATE_VAR.sub(lambda match: "{" + (match.group(1) or match.group(2)) + "}", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def _request_spec(value: Any, fallback: dict[str, Any] | None = None) -> tuple[str | None, str | None]:
    source = value if isinstance(value, dict) else {}
    fallback = fallback or {}
    method = source.get("method") or fallback.get("method")
    path = source.get("path") or source.get("url") or fallback.get("path") or fallback.get("url")
    return (str(method).upper() if method else None, _normalized_path(path))


def _planned_requests(task: dict[str, Any]) -> list[dict[str, str]]:
    requests: list[dict[str, str]] = []
    method, path = _request_spec(task.get("request"), task)
    if method and path:
        requests.append({"role": "主请求", "method": method, "path": path})
    verification = task.get("verification") if isinstance(task.get("verification"), dict) else {}
    method, path = _request_spec(verification.get("request"), verification)
    if method and path:
        requests.append({"role": "只读复核", "method": method, "path": path})
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for item in requests:
        unique[(item["role"], item["method"], item["path"])] = item
    return list(unique.values())


def _result_records(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for key in ("task_results", "case_results"):
        for item in _as_list(result.get(key)):
            if not isinstance(item, dict):
                continue
            item_id = item.get("task_id") or item.get("case_id")
            if not item_id:
                continue
            current = records.setdefault(str(item_id), {})
            current.update(item)
            current.setdefault("task_id", str(item_id))
    return records


def _evidence_candidates(root: Path) -> list[Path]:
    candidates: set[Path] = set()
    direct = root / "evidence"
    if direct.is_dir():
        candidates.update(path.resolve() for path in direct.glob("*.json") if path.is_file())
    slices = root / "slice-results"
    if slices.is_dir():
        candidates.update(path.resolve() for path in slices.glob("*/evidence/*.json") if path.is_file())
    return sorted(candidates, key=lambda item: item.as_posix())


def _safe_ref_candidates(root: Path, ref: str, known: list[Path]) -> list[Path]:
    raw = Path(str(ref))
    direct = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    options = [direct]
    try:
        direct.relative_to(root.resolve())
    except ValueError:
        options = []
    basename = raw.name
    options.extend(path for path in known if path.name == basename and path not in options)
    return options


_CHECKPOINT_LAYERS = ("http", "db", "cache", "mq", "log", "other")


def _checkpoint_layer(item: dict[str, Any], source: str, evidence_ref: str | None) -> str:
    explicit = item.get("validation_layer") or item.get("layer") or item.get("oracle_layer")
    normalized = str(explicit or "").strip().lower()
    aliases = {"database": "db", "sql": "db", "redis": "cache", "kafka": "mq", "rabbitmq": "mq", "logs": "log"}
    normalized = aliases.get(normalized, normalized)
    if normalized in _CHECKPOINT_LAYERS:
        return normalized
    text = " ".join(str(value) for value in (
        item.get("type"), item.get("source"), item.get("name"), item.get("path"), source, evidence_ref,
    ) if value).lower()
    if re.search(r"\b(db|database|sql)\b|数据库", text):
        return "db"
    if re.search(r"\b(cache|redis)\b|缓存", text):
        return "cache"
    if re.search(r"\b(mq|kafka|rabbitmq)\b|消息队列", text):
        return "mq"
    if re.search(r"\b(log|logs)\b|日志", text):
        return "log"
    if re.search(r"\b(http|status|header|body|json|response|request)\b|响应|请求|业务码|状态码", text) or evidence_ref:
        return "http"
    return "other"


def _assertion(item: dict[str, Any], evidence_ref: str | None, *, source: str = "结构化断言") -> dict[str, Any]:
    name = (
        item.get("name") or item.get("assertion_id") or item.get("id") or item.get("path")
        or item.get("source") or item.get("type") or "未命名检查项"
    )
    assertion_type = item.get("type") or item.get("operator") or item.get("source") or "未标注"
    actual = item.get("actual") if "actual" in item else item.get("observed")
    resolved_ref = item.get("evidence_ref") or evidence_ref
    return {
        "name": str(name),
        "type": str(assertion_type),
        "expected": item.get("expected") if "expected" in item else None,
        "actual": actual,
        "status": item.get("status") or "未标注",
        "evidence_ref": resolved_ref,
        "source": source,
        "layer": _checkpoint_layer(item, source, resolved_ref),
    }


def _text_checkpoint(text: Any, evidence_ref: str | None, *, source: str, status: str = "未标注") -> dict[str, Any]:
    item = {"name": str(text), "type": source}
    return {
        "name": str(text),
        "type": source,
        "expected": str(text) if source == "计划检查点" else None,
        "actual": str(text) if source != "计划检查点" else None,
        "status": status,
        "evidence_ref": evidence_ref,
        "source": source,
        "layer": _checkpoint_layer(item, source, evidence_ref),
    }


def _shell_quote(value: Any) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def _request_url(request: dict[str, Any], fallback: Any = None) -> str:
    raw = request.get("url") or request.get("path") or fallback or ""
    query = request.get("query") if isinstance(request.get("query"), dict) else request.get("params")
    if not isinstance(query, dict) or not query or "?" in str(raw):
        return str(raw)
    pairs: list[tuple[str, Any]] = []
    for key, value in query.items():
        if isinstance(value, list):
            pairs.extend((str(key), item) for item in value)
        elif value is not None:
            pairs.append((str(key), value))
    encoded = urlencode(pairs, doseq=True)
    return str(raw) + (("?" + encoded) if encoded else "")


def _request_curl(request: dict[str, Any], fallback: Any = None) -> str | None:
    if not request:
        return None
    method = str(request.get("method") or "GET").upper()
    parts = [f"curl -X {method} {_shell_quote(_request_url(request, fallback))}"]
    headers = request.get("headers") if isinstance(request.get("headers"), dict) else {}
    for key, value in headers.items():
        parts.append(f"  -H {_shell_quote(f'{key}: {value}')}")
    body = request.get("json") if "json" in request else request.get("body") if "body" in request else request.get("data")
    if body is not None:
        raw = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"  --data-raw {_shell_quote(raw)}")
    return " \\\n".join(parts)


def _exchange(item: Any, label: str, evidence_ref: str, planned: dict[str, str] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    method = item.get("method") or (planned or {}).get("method")
    target = item.get("url") or item.get("path") or (planned or {}).get("path")
    request = {
        "method": method,
        "target": target,
        "headers": item.get("request_headers") if "request_headers" in item else item.get("headers"),
        "body": item.get("request_body") if "request_body" in item else item.get("body"),
    }
    response = {
        "status": item.get("status") if "status" in item else item.get("http"),
        "headers": item.get("response_headers"),
        "body": item.get("response_body") if "response_body" in item else item.get("response_json"),
    }
    request_present = any(value is not None for value in request.values())
    response_present = any(value is not None for value in response.values())
    request_text = _display(request) if request_present else "请求报文缺失"
    response_text = _display(response) if response_present else "响应报文缺失"
    return {
        "label": label,
        "method": str(method).upper() if method else None,
        "path": _normalized_path(target),
        "status": response.get("status"),
        "elapsed_ms": item.get("elapsed_ms"),
        "request": request_text,
        "response": response_text,
        "request_data": request if request_present else None,
        "response_data": response if response_present else None,
        "curl": _request_curl(request, target) if request_present else None,
        "request_missing": not request_present,
        "response_missing": not response_present,
        "truncated": False,
        "evidence_ref": evidence_ref,
    }


def _step_exchange(step: Any, label: str, evidence_ref: str, planned: dict[str, str] | None) -> dict[str, Any] | None:
    """Adapt evidence-schema ``steps[].request/response`` without losing fields."""
    if not isinstance(step, dict):
        return None
    request = step.get("request") if isinstance(step.get("request"), dict) else None
    response = step.get("response") if isinstance(step.get("response"), dict) else None
    if request is None and response is None:
        return None
    request = request or {}
    response = response or {}
    method = request.get("method") or (planned or {}).get("method")
    target = request.get("url") or request.get("path") or (planned or {}).get("path")
    status = response.get("http_status") if "http_status" in response else response.get("status")
    elapsed = response.get("elapsed_ms") if "elapsed_ms" in response else step.get("elapsed_ms")
    return {
        "label": label,
        "method": str(method).upper() if method else None,
        "path": _normalized_path(target),
        "status": status,
        "elapsed_ms": elapsed,
        "request": _display(request) if request else "请求报文缺失",
        "response": _display(response) if response else "响应报文缺失",
        "request_data": request if request else None,
        "response_data": response if response else None,
        "curl": _request_curl(request, target) if request else None,
        "request_missing": not bool(request),
        "response_missing": not bool(response),
        "truncated": False,
        "evidence_ref": evidence_ref,
    }


def _scenario_identity(task: dict[str, Any], acceptance: dict[str, dict[str, Any]]) -> tuple[str, str, str]:
    scenario_refs = _strings(task.get("scenario_refs"))
    if scenario_refs:
        return f"scenario:{scenario_refs[0]}", scenario_refs[0], "scenario_ref"
    scenario_type = task.get("scenario_type")
    if scenario_type:
        return f"scenario-type:{scenario_type}", str(scenario_type), "scenario_type"
    dimension = task.get("quality_dimension") or task.get("quality_dimension_id") or task.get("dimension")
    if dimension:
        return f"quality:{dimension}", str(dimension), "quality_dimension"
    business_refs = _strings(task.get("business_rule_refs"))
    if business_refs:
        return f"business:{business_refs[0]}", business_refs[0], "business_rule"
    ac_refs = _strings(task.get("acceptance_refs")) + _strings(task.get("requirement_refs"))
    if ac_refs:
        ac_id = ac_refs[0]
        ac = acceptance.get(ac_id) or {}
        title = ac.get("title") or ac.get("text")
        return f"acceptance:{ac_id}", f"{ac_id} · {title}" if title else ac_id, "acceptance"
    return "uncategorized", "未分类场景", "uncategorized"


def _state(statuses: list[str]) -> str:
    values = set(statuses)
    if values & {"failed", "error"}:
        return "failed"
    if "blocked" in values:
        return "blocked"
    if values and values <= {"passed"}:
        return "passed"
    return "open"


def _interface_result(statuses: list[str], exchanges: int) -> str:
    state = _state(statuses)
    if state == "failed":
        return "failed"
    if state == "blocked":
        return "blocked"
    if state == "passed" and exchanges:
        return "passed"
    return "open"


def _unique_strings(values: list[Any]) -> list[str]:
    return sorted({str(value) for value in values if value is not None and str(value).strip()})


def build_report_view(manifest: dict[str, Any], base_dir: str | Path | None = None) -> dict[str, Any]:
    """Create a report-only view without mutating the manifest or any Run file."""
    acceptance_items = [item for item in _as_list(manifest.get("acceptance_items")) if isinstance(item, dict)]
    acceptance = {str(item.get("id")): item for item in acceptance_items if item.get("id")}
    cases: list[dict[str, Any]] = []
    run_index: list[dict[str, Any]] = []
    unmapped: list[dict[str, Any]] = []
    residual_risks: list[dict[str, Any]] = []
    planned_interface_keys: set[tuple[str, str]] = set()
    executed_interface_keys: set[tuple[str, str]] = set()
    aggregation = manifest.get("aggregation") if isinstance(manifest.get("aggregation"), dict) else {}
    current_cases = aggregation.get("current_cases")
    if not isinstance(current_cases, dict):
        current_cases = manifest.get("current_cases") if isinstance(manifest.get("current_cases"), dict) else None
    current_case_sources = {
        str(task_id): str(record.get("source_run_id"))
        for task_id, record in (current_cases or {}).items()
        if isinstance(record, dict) and record.get("source_run_id")
    } if current_cases is not None else None

    for run in [item for item in _as_list(manifest.get("evidence_runs")) if isinstance(item, dict)]:
        run_id = str(run.get("id") or "未标注 Run")
        current_run_case_ids = {
            task_id for task_id, source_run_id in (current_case_sources or {}).items()
            if source_run_id == run_id
        } if current_case_sources is not None else None
        path_value = run.get("result_path")
        entry = {
            "id": run_id,
            "kind": run.get("kind"),
            "summary": run.get("summary"),
            "result_path": str(path_value) if path_value else None,
            "status": "unavailable",
            "notes": [],
        }
        if not path_value:
            entry["notes"].append("未声明 result_path；仅展示 manifest 摘要")
            run_index.append(entry)
            continue
        result_path = _resolve_result(path_value, base_dir)
        root = result_path.parent
        result, error = _read_object(result_path)
        if error:
            entry["notes"].append(error)
            run_index.append(entry)
            continue
        plan, plan_error = _read_object(root / "plan.json")
        if plan_error:
            entry["notes"].append(plan_error)
            plan = {"tasks": []}
        result = result or {}
        plan = plan or {"tasks": []}
        entry["status"] = result.get("verdict") or "loaded"
        entry["plan_task_count"] = len(_as_list(plan.get("tasks")))
        known_evidence = _evidence_candidates(root)
        evidence_payloads: dict[Path, dict[str, Any]] = {}
        evidence_errors: dict[Path, str] = {}
        for evidence_path in known_evidence:
            payload, evidence_error = _read_object(evidence_path)
            if payload is not None:
                evidence_payloads[evidence_path] = payload
            elif evidence_error:
                evidence_errors[evidence_path] = evidence_error
        records = _result_records(result)
        task_ids: set[str] = set()
        mapped_paths: set[Path] = set()

        for task in [item for item in _as_list(plan.get("tasks")) if isinstance(item, dict)]:
            task_id = str(task.get("task_id") or task.get("case_id") or "")
            if not task_id:
                continue
            if current_run_case_ids is not None and task_id not in current_run_case_ids:
                continue
            task_ids.add(task_id)
            record = records.get(task_id) or records.get(str(task.get("case_id") or "")) or {}
            case_id = str(task.get("case_id") or task_id)
            evidence_refs = _unique_strings(_strings(record.get("evidence_refs")))
            payloads: list[tuple[str, dict[str, Any], Path]] = []
            for ref in evidence_refs:
                for candidate in _safe_ref_candidates(root, ref, known_evidence):
                    if candidate in evidence_payloads:
                        payloads.append((ref, evidence_payloads[candidate], candidate))
                        mapped_paths.add(candidate)
                        break
            if not payloads:
                for candidate, payload in evidence_payloads.items():
                    if str(payload.get("case_id") or "") in {task_id, case_id}:
                        payloads.append((candidate.relative_to(root).as_posix(), payload, candidate))
                        mapped_paths.add(candidate)

            planned = _planned_requests(task)
            for request in planned:
                planned_interface_keys.add((request["method"], request["path"]))
            assertions: list[dict[str, Any]] = []
            exchanges: list[dict[str, Any]] = []
            plan_assertions = [item for item in _as_list(task.get("assertions")) if isinstance(item, dict)]
            assertions.extend(_assertion(item, None, source="计划检查点") for item in plan_assertions)
            if task.get("assertion_summary"):
                assertions.append(_text_checkpoint(task["assertion_summary"], None, source="计划检查点", status="planned"))
            for ref, payload, _ in payloads:
                source_assertions = _as_list(payload.get("assertions")) or _as_list(payload.get("verification_assertions"))
                assertions.extend(_assertion(item, ref, source="执行断言") for item in source_assertions if isinstance(item, dict))
                for check in _as_list(payload.get("checks")):
                    if isinstance(check, dict):
                        assertions.append(_assertion(check, ref, source="执行检查点"))
                    elif check is not None and str(check).strip():
                        assertions.append(_text_checkpoint(check, ref, source="执行检查说明", status=payload.get("status") or record.get("status") or "未标注"))
                main = _exchange(payload.get("exchange"), "主请求", ref, planned[0] if planned else None)
                verification = _exchange(
                    payload.get("verification_exchange"), "只读复核", ref,
                    next((item for item in planned if item["role"] == "只读复核"), None),
                )
                if main:
                    exchanges.append(main)
                    if planned:
                        executed_interface_keys.add((planned[0]["method"], planned[0]["path"]))
                if verification:
                    exchanges.append(verification)
                    verification_plan = next((item for item in planned if item["role"] == "只读复核"), None)
                    if verification_plan:
                        executed_interface_keys.add((verification_plan["method"], verification_plan["path"]))
                for index, step in enumerate(_as_list(payload.get("steps"))):
                    exchange = _step_exchange(step, f"步骤 {index + 1}", ref, planned[0] if planned else None)
                    if exchange:
                        exchanges.append(exchange)
                        if exchange.get("method") and exchange.get("path"):
                            executed_interface_keys.add((exchange["method"], exchange["path"]))
                if not _as_list(payload.get("steps")) and ("request" in payload or "response" in payload):
                    exchange = _step_exchange(payload, "主请求", ref, planned[0] if planned else None)
                    if exchange:
                        exchanges.append(exchange)
                        if exchange.get("method") and exchange.get("path"):
                            executed_interface_keys.add((exchange["method"], exchange["path"]))
            if not assertions:
                assertions.extend(
                    _assertion(item, evidence_refs[0] if evidence_refs else None, source="结果断言")
                    for item in _as_list(record.get("assertions")) if isinstance(item, dict)
                )
            scenario_key, scenario_name, scenario_source = _scenario_identity(task, acceptance)
            ac_refs = _unique_strings(
                _strings(task.get("acceptance_refs")) + _strings(task.get("requirement_refs"))
            )
            checkpoints_by_layer = {
                layer: [checkpoint for checkpoint in assertions if checkpoint.get("layer") == layer]
                for layer in _CHECKPOINT_LAYERS
            }
            cases.append({
                "id": case_id,
                "task_id": task_id,
                "name": task.get("name") or task.get("title") or record.get("name") or case_id,
                "run_id": run_id,
                "status": record.get("status") or "not_run",
                "acceptance_refs": ac_refs,
                "scenario_key": scenario_key,
                "scenario_name": scenario_name,
                "scenario_source": scenario_source,
                "scenario_refs": _strings(task.get("scenario_refs")),
                "method": planned[0]["method"] if planned else None,
                "path": planned[0]["path"] if planned else None,
                "planned_requests": planned,
                "assertions": assertions,
                "checkpoints": assertions,
                "checkpoints_by_layer": checkpoints_by_layer,
                "exchanges": exchanges,
                "evidence_refs": evidence_refs,
                "evidence_note": None if assertions else "无计划检查点、结构化断言或执行检查说明",
            })

        for candidate in known_evidence:
            if candidate in mapped_paths:
                continue
            payload = evidence_payloads.get(candidate)
            relative = candidate.relative_to(root).as_posix()
            unmapped.append({
                "run_id": run_id,
                "ref": relative,
                "case_id": payload.get("case_id") if payload else None,
                "reason": evidence_errors.get(candidate) or "证据未映射到 plan task/result case",
            })

        cleanup = result.get("cleanup") if isinstance(result.get("cleanup"), dict) else {}
        for item in _as_list(result.get("residual_risks")):
            if isinstance(item, dict):
                residual_risks.append({"run_id": run_id, **item})
        for item in _as_list(cleanup.get("pending_objects")):
            residual_risks.append({"run_id": run_id, "kind": "pending_object", "detail": item})
        for item in _as_list(cleanup.get("unresolved_obligations")):
            residual_risks.append({"run_id": run_id, "kind": "unresolved_cleanup", "detail": item})
        run_index.append(entry)

    scenario_map: dict[str, dict[str, Any]] = {}
    for case in cases:
        scenario = scenario_map.setdefault(case["scenario_key"], {
            "id": case["scenario_key"],
            "name": case["scenario_name"],
            "source": case["scenario_source"],
            "cases": [],
        })
        scenario["cases"].append(case)
    scenarios = []
    for scenario in scenario_map.values():
        scenario_cases = sorted(scenario["cases"], key=lambda item: (item["id"], item["run_id"]))
        scenario["cases"] = scenario_cases
        scenario["state"] = _state([item["status"] for item in scenario_cases])
        scenario["acceptance_refs"] = _unique_strings(
            [ref for item in scenario_cases for ref in item["acceptance_refs"]]
        )
        scenario["case_count"] = len(scenario_cases)
        scenario["assertion_count"] = sum(len(item["assertions"]) for item in scenario_cases)
        scenarios.append(scenario)
    scenarios.sort(key=lambda item: (item["id"] == "uncategorized", item["name"], item["id"]))

    coverage_items = []
    for item in acceptance_items:
        ac_id = str(item.get("id") or "")
        related = [scenario["name"] for scenario in scenarios if ac_id in scenario["acceptance_refs"]]
        closure = str(item.get("interface_verifiability") or "partial").lower()
        closure = closure if closure in {"direct", "partial", "none"} else "partial"
        coverage_items.append({**item, "interface_closure": closure, "scenarios": _unique_strings(related)})
    coverage_groups = [
        {"id": key, "items": [item for item in coverage_items if item["interface_closure"] == key]}
        for key in ("direct", "partial", "none")
    ]

    interface_map: dict[tuple[str, str], dict[str, Any]] = {}
    for case in cases:
        exchange_by_key: dict[tuple[str, str], int] = {}
        for exchange in case["exchanges"]:
            if exchange.get("method") and exchange.get("path"):
                key = (exchange["method"], exchange["path"])
                exchange_by_key[key] = exchange_by_key.get(key, 0) + 1
        for request in case["planned_requests"]:
            key = (request["method"], request["path"])
            row = interface_map.setdefault(key, {
                "method": request["method"], "path": request["path"], "scenarios": set(),
                "cases": set(), "statuses": [], "purposes": set(), "exchange_count": 0,
            })
            row["scenarios"].add(case["scenario_name"])
            row["cases"].add((case["run_id"], case["id"]))
            row["statuses"].append(case["status"])
            row["purposes"].update(assertion["name"] for assertion in case["assertions"])
            row["exchange_count"] += exchange_by_key.get(key, 0)
        for key, count in exchange_by_key.items():
            if key in interface_map:
                continue
            row = interface_map.setdefault(key, {
                "method": key[0], "path": key[1], "scenarios": set(), "cases": set(),
                "statuses": [], "purposes": set(), "exchange_count": 0,
            })
            row["scenarios"].add(case["scenario_name"])
            row["cases"].add((case["run_id"], case["id"]))
            row["statuses"].append(case["status"])
            row["purposes"].update(assertion["name"] for assertion in case["assertions"])
            row["exchange_count"] += count

    interfaces = []
    for key, row in sorted(interface_map.items()):
        interfaces.append({
            "method": row["method"],
            "path": row["path"],
            "scenarios": sorted(row["scenarios"]),
            "case_count": len(row["cases"]),
            "purposes": sorted(row["purposes"]),
            "exchange_count": row["exchange_count"],
            "result": _interface_result(row["statuses"], row["exchange_count"]),
            "planned": key in planned_interface_keys,
        })

    total = int((manifest.get("requirement") or {}).get("acceptance_total") or len(acceptance_items))
    touched = sum(item.get("execution_status") in {"partial", "complete"} for item in acceptance_items)
    complete = sum(item.get("execution_status") == "complete" for item in acceptance_items)
    passed = sum(item.get("verdict") == "passed" for item in acceptance_items)
    interface_total = len(planned_interface_keys)
    interface_executed = len(planned_interface_keys & executed_interface_keys)
    issues = [item for item in _as_list(manifest.get("issues")) if isinstance(item, dict)]
    gaps = [item for item in issues if item.get("type") == "test_gap"]
    defects = [item for item in issues if item.get("type") == "defect"]
    summary = {
        "acceptance_total": total,
        "touched": touched,
        "complete": complete,
        "passed": passed,
        "interface_total": interface_total,
        "interface_executed": interface_executed,
        "run_count": len(run_index),
        "case_count": len(cases),
        "exchange_count": sum(len(case["exchanges"]) for case in cases),
    }
    return {
        "report_view_version": REPORT_VIEW_VERSION,
        "summary": summary,
        "coverage": {"items": coverage_items, "groups": coverage_groups, "total": total},
        "interfaces": interfaces,
        "scenarios": scenarios,
        "issues": defects,
        "gaps": gaps,
        "residual_risks": residual_risks,
        "run_index": run_index,
        "unmapped_evidence": sorted(unmapped, key=lambda item: (item["run_id"], item["ref"])),
    }
