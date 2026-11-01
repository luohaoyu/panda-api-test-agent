# 需求级验收报告规范

## 1. 定位

需求级验收报告聚合同一需求的多次 `PROBE`、`EXECUTE` 和只读 Oracle 运行。运行级 `result.json`、HTML 和 evidence 保持不可变；需求报告只引用它们，不复制或覆盖原始证据。

正式需求报告必须由 `scripts/render_requirement_report.py` 从结构化 `requirement-report.json` 确定性生成，不得由 Agent 自由拼接 HTML。

## 2. 文件布局

```text
.workbuddy/panda-api-test-agent/requirements/<requirement-id>/requirement-report.json
reports/<requirement-id>-acceptance-current.html
```

同一需求只维护一个 `current` 入口。补测时更新结构化清单并重新生成 `current`；历史运行报告不得修改。需要冻结里程碑时，复制结构化清单并使用显式版本名生成快照。

## 3. 最小结构

```json
{
  "schema_version": "1.0",
  "report_id": "AR-P943-4559-V1",
  "report_version": "1.0",
  "requirement": {
    "id": "P943-4559",
    "title": "加入包裹失败操作日志与内部失败轨迹",
    "prd_version": "0.5",
    "prd_path": "docs/business/prd/...md",
    "acceptance_total": 19
  },
  "baseline": {
    "branch": "release/release-V1.26.10.22",
    "commit": "...",
    "environment": "test3-cps",
    "base_url": "https://example.invalid"
  },
  "acceptance_items": [],
  "issues": [],
  "evidence_runs": [],
  "release_recommendation": "阻断发布；修复后补测",
  "generated_from": "validated-results"
}
```

## 4. 验收项模型

每条 AC 必须声明：

- `id`：稳定验收编号，例如 `AC02`。
- `title`：验收目标简述。
- `execution_status`：`not_run`、`partial`、`complete`。
- `verdict`：`passed`、`failed`、`blocked`、`inconclusive`、`not_run`。
- `actual`：基于运行事实的实际结果。
- `gaps`：未满足条件数组。
- `evidence_refs`：证据引用数组，引用 `evidence_runs[].id` 或其运行内 case/evidence。
- `issue_refs`：关联缺陷或缺口编号数组。

纪律：

- `execution_status=complete` 只表示验收步骤与 Oracle 完整，不代表通过。
- `verdict=passed` 必须同时满足 `execution_status=complete` 且所有必需预期成立。
- 部分证据不得写为通过；环境门禁、鉴权、造数和 collect 不计入验收项通过。
- 未发送业务请求或无运行证据的项必须为 `not_run`。

## 5. 三类覆盖率

以 `requirement.acceptance_total` 为统一分母：

- 执行触达率：`execution_status in {partial, complete}` 的 AC 数量 / 总数。
- 完整场景覆盖率：`execution_status=complete` 的 AC 数量 / 总数。
- 验收通过率：`verdict=passed` 的 AC 数量 / 总数。

报告器负责计算，清单不得手工提供百分比。显示百分比时保留一位小数，同时显示分子/分母。

## 6. 问题模型

`issues` 中每项必须声明：

- `id`：`DEF-*` 或 `GAP-*`。
- `type`：`defect` 或 `test_gap`。
- `severity`：`blocker`、`high`、`medium`、`low`。
- `title`、`actual`、`impact`。
- `acceptance_refs`、`evidence_refs`。
- `status`：`open`、`confirmed`、`fixed`、`closed`。

证据不足、缺账号、缺查询入口和未测 SLA 属于 `test_gap`，不得包装成产品缺陷。只有环境、数据和用例有效且产品行为违反预期时才登记 `defect`。

## 7. 证据索引

`evidence_runs` 每项至少包含：

- `id`：运行 ID 或外部造数运行 ID。
- `kind`：`gate`、`data_discovery`、`fixture`、`business_execution`、`oracle`。
- `result_path` 或 `report_path`。
- `summary`。
- `immutable: true`。

需求报告只做索引。认证头、令牌、密码及未脱敏响应不得进入清单或报告。

## 8. 总体结论

报告器按以下优先级计算：

1. 存在 `severity=blocker` 且状态未关闭的产品缺陷，或任一完整执行 AC 失败 → `FAIL`。
2. 无失败但存在必需项被门禁阻塞 → `BLOCKED`。
3. 所有 AC 完整执行且通过 → `PASS`。
4. 其余情况 → `INCONCLUSIVE`。

不得仅依据某次运行的 verdict 直接代替需求级结论。
