# 需求级验收报告规范

## 1. 定位

需求级验收报告聚合同一需求的多次 `PROBE`、`EXECUTE` 和只读 Oracle 运行。运行级 `result.json`、HTML 和 evidence 保持不可变；需求报告只引用它们，不复制或覆盖原始证据。

正式需求报告必须由 `scripts/render_requirement_report.py` 从结构化 `requirement-report.json` 确定性生成，不得由 Agent 自由拼接 HTML。

## 2. 文件布局

```text
.panda-api-test-agent/requirements/<requirement-id>/requirement-report.json
.panda-api-test-agent/reports/<requirement-id>-acceptance-current.html
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
    "acceptance_total": 1
  },
  "baseline": {
    "branch": "release/release-V1.26.10.22",
    "commit": "...",
    "environment": "test3-cps",
    "base_url": "https://example.invalid"
  },
  "acceptance_items": [
    {
      "id": "AC01",
      "title": "示例验收项",
      "interface_verifiability": "partial",
      "verifiability_reason": "接口可验证后端状态，但无法验证客户端展示",
      "interface_scope": ["后端状态与响应契约"],
      "non_interface_scope": ["客户端展示"],
      "recommended_validation_layers": ["客户端/UI"],
      "execution_status": "partial",
      "verdict": "inconclusive",
      "actual": "接口层已验证后端状态",
      "gaps": ["客户端展示待验证"],
      "evidence_refs": [],
      "issue_refs": []
    }
  ],
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
- `interface_verifiability`：`direct`、`partial` 或 `none`。
- `verifiability_reason`：分类依据，不得只写枚举值。
- `interface_scope`：接口层可验证部分；`none` 时为空数组。
- `non_interface_scope`：接口层不可验证部分；`direct` 时可为空数组。
- `recommended_validation_layers`：对 `partial/none` 建议的 UI、客户端、人工、性能、安全或其他验证层。
- `execution_status`：`not_run`、`partial`、`complete`。
- `verdict`：`passed`、`failed`、`blocked`、`inconclusive`、`not_run`。
- `actual`：基于运行事实的实际结果。
- `gaps`：未满足条件数组。
- `evidence_refs`：证据引用数组，引用 `evidence_runs[].id` 或其运行内 case/evidence。
- `issue_refs`：关联缺陷或缺口编号数组。

分类定义：

- `direct`：验收目标及必需 Oracle 可由接口请求、响应和可用后端证据完整验证。
- `partial`：接口层只能验证验收目标的一部分；必须同时写明可验证与不可验证部分。
- `none`：接口请求/响应和后端 Oracle 无法验证该验收目标；报告必须显示“接口层不可验证”，不得为凑覆盖率生成伪接口用例。

纪律：

- `execution_status=complete` 只表示声明范围内验收步骤与 Oracle 完整，不代表通过。
- `verdict=passed` 必须同时满足 `execution_status=complete` 且所有必需预期成立。
- `partial` 的接口部分通过默认只能形成局部事实；除非其他验证层证据同时闭合整条 AC，否则整条 AC 不得标记 `passed`。
- `none` 不得由接口运行判定 `passed` 或 `failed`；保持 `not_run`/`inconclusive`，并提供建议验证层。
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
