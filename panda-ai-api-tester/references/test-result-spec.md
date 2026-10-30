# TestResult 规范

## 目的

以结构化事实作为报告、审计和回归晋级的唯一输入，避免 Agent 根据自然语言自由计算状态。

## 顶层结构

```json
{
  "schema_version": "1.0",
  "run_id": "run-001",
  "package_id": "PKG-001",
  "mode": "PROBE",
  "project": {
    "system_id": "SYS-001",
    "requirement_refs": ["REQ-001"]
  },
  "environment": {
    "environment_id": "local-test",
    "base_url": "http://127.0.0.1:8080",
    "production": false
  },
  "started_at": "2026-10-30T00:00:00+08:00",
  "finished_at": "2026-10-30T00:00:02+08:00",
  "gate_results": [],
  "case_results": [],
  "requirement_trace": {
    "requirements": [],
    "business_rules": [],
    "scenarios": [],
    "summary": {}
  },
  "execution_trace": {
    "trace_version": "1.0",
    "status": "completed",
    "steps": []
  },
  "cleanup": {
    "status": "none|passed|failed|not_run|not_required|waived",
    "pending_objects": [],
    "waived_cases": []
  },
  "known_gaps": [],
  "summary": {},
  "verdict": "blocked"
}
```

## 已知缺口（known_gaps）

禁止把性质不同的缺口写成平铺字符串数组。每条必须是对象：

```json
{
  "gap_id": "GAP-ENV-01",
  "blocking": true,
  "stage": "probe",
  "description": "运行时未注入 base_url 与 bearer token",
  "impact": "环境与鉴权门禁失败，业务用例全部未执行",
  "next_action": "通过安全通道注入环境变量后重跑 PROBE"
}
```

字段要求：

| 字段 | 取值 | 必填 | 说明 |
|---|---|---|---|
| `gap_id` | 字符串 | 是 | 稳定标识 |
| `blocking` | `true` / `false` | 是 | `true` 表示阻止晋级 `PROMOTE` |
| `stage` | `probe` / `execute` / `promote` / `none` | 是 | 该缺口影响的阶段 |
| `description` | 字符串 | 是 | 事实描述，不含建议 |
| `impact` | 字符串 | 否 | 不解决会导致什么后果 |
| `next_action` | 字符串 | 否 | 解除该缺口需要做什么 |

`render_report.py` 取 `description` 渲染；`promote.py` 用 `blocking` 判定是否允许晋级。因此 `blocking` 为 `true` 时不得进入 `PROMOTE`。

允许保留字符串形式仅为兼容历史结果，但新生成的结果必须使用对象形式。

## 门禁结果

```json
{
  "gate_id": "GATE-ENV-001",
  "name": "服务可达性",
  "status": "passed|failed|error",
  "classification": "environment|test_case|inconclusive",
  "request_summary": "GET /actuator/health",
  "actual": "connection refused",
  "evidence_refs": ["evidence/gate-env-001.json"]
}
```

门禁不计入业务用例通过、失败或通过率。任一模式中只要存在失败或错误门禁，所有受影响业务用例必须为 `not_run`，并在 `blocked_by` 中引用对应失败门禁；不得使用 `skipped` 模糊门禁阻断，也不得在失败门禁后记录业务请求已执行。

## 用例结果

```json
{
  "case_id": "TC-001",
  "name": "分页查询正向",
  "status": "passed|failed|error|skipped|not_run",
  "classification": "environment|test_case|product_defect|inconclusive|null",
  "request_sent": false,
  "expected": "HTTP 200, code=200",
  "actual": "环境门禁失败，未发送请求",
  "assertions": [],
  "evidence_refs": [],
  "blocked_by": ["GATE-ENV-001"]
}
```

状态语义：

- `passed`：`request_sent=true`，必须包含 `http_status`、`business_code`、`key_fields` 三类必需断言；每项记录 `operator`、`expected`、`actual`、`status=passed`、`evidence_ref` 和 `evidence_sha256`。证据必须是运行目录 `evidence/` 下的 JSON，包含相同 `case_id` 和断言数据；校验器重新计算断言并核对文件 SHA-256，不能只信任输入的 `status=passed`。
- `failed`：`request_sent=true`，至少一个必需断言失败。
- `error`：运行基础设施或执行器异常；说明请求是否发出。
- `skipped`：依据明确规则有意跳过。
- `not_run`：没有发送业务请求，原因是门禁、依赖、未授权或范围限制。

禁止把 `request_sent=false` 的用例标记为 `passed` 或 `failed`。断言运算符只允许 `equals`、`not_equals`、`exists`、`all_present`、`contains`、`in_range`；不支持的复杂断言必须先扩展确定性校验器，不能由 Agent 自行宣称通过。

## 需求覆盖追踪

`requirement_trace` 由 Runner 根据 TestPackage 的需求、业务规则、场景声明以及实际 `case_results` 确定性生成。每条业务规则必须列出关联用例、稳定断言 ID、证据引用和结论；每个需求结论必须由其必需业务规则状态汇总。校验器重新计算这些关系，禁止直接相信输入的 `status`。

状态语义：

- `passed`：所有关联必需用例实际执行通过，且至少存在一个映射到该规则并通过的稳定断言。
- `failed`：关联用例或关键断言真实失败。
- `error`：执行错误影响结论。
- `not_run`：门禁、依赖或规则跳过导致未执行。
- `uncovered`：没有任何有效用例映射。
- `inconclusive`：结果与断言证据不足以形成可靠结论。

## 执行过程追踪

`execution_trace` 必须覆盖测试包校验、每个门禁、每个夹具、每个业务用例、每个 teardown 动作和最终 verdict，步骤顺序必须连续。每个 `case_result` 还必须包含本用例的 `execution_trace`，记录请求解析、清理预登记、HTTP exchange、变量提取、每条稳定断言以及完成、错误或阻断状态。

HTTP 步骤必须引用当前用例证据；断言步骤必须引用结果中的 `assertion_id` 并与断言状态一致；`request_sent=false` 的用例禁止出现 HTTP exchange 步骤。全局 trace 的 gate/case/teardown 状态和证据引用必须与相应结构化结果完全一致。

## 清理状态

只允许：

- `none`：没有产生需要清理的副作用。
- `passed`：teardown 实际执行并验证成功。
- `failed`：teardown 失败，必须列出对象。
- `not_run`：产生副作用但未执行清理；必须解释并登记对象。
- `not_required`：只读运行，不需要业务数据清理。
- `waived`：存在经严格校验的写用例 cleanup waiver；`waived_cases` 必须完整列出审批、有效期、标签和残留对象，`pending_objects` 必须标记 `risk=accepted`，不表示清理通过。

## 汇总

`summary` 必须由校验器计算或核对：

```json
{
  "total": 4,
  "passed": 0,
  "failed": 0,
  "errors": 0,
  "skipped": 0,
  "not_run": 4,
  "executed": 0
}
```

`executed = passed + failed + errors`。`skipped` 和 `not_run` 不计入实际执行。

只有 `failed=0`、`errors=0`、`not_run=0`、清理不是 `failed/not_run/waived`，且所有必需覆盖均实际执行时，才允许 verdict 为 `passed`。只要 `waived_cases` 非空，verdict 必须为 `inconclusive`，除非存在更严重的 gates/case failed/error 状态。

## Verdict

- `passed`：所有必需用例实际通过，清理成功或不需要。
- `failed`：存在经证据确认的业务失败或真实缺陷。
- `error`：执行器或基础设施异常影响结论。
- `blocked`：门禁、权限、环境或清理能力阻止业务执行。
- `partial`：部分执行，但仍有必需用例未执行。
- `inconclusive`：证据不足，无法可靠分类。
