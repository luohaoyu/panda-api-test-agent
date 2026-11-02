# 需求级验收报告规范

## 1. 定位

需求级验收报告聚合同一需求的多次 `PROBE`、`EXECUTE` 和只读 Oracle 运行。运行级 `result.json`、HTML 和 evidence 保持不可变；需求报告只引用它们，不复制或覆盖原始证据。

正式需求报告必须由 `scripts/render_requirement_report.py` 从结构化 `requirement-report.json` 确定性生成，不得由 Agent 自由拼接 HTML。

清单来源固定为 `scripts/run_orchestrator.py` 的两个投影函数，不允许手工编写：

- `build_requirement_manifest(run_dir)`：单个已归约 Run 的投影，用于运行级清单。
- `build_requirement_manifest_for_runs([run_dir...])`：多 Run 的 `current` 投影，`report_id` 固定为 `AR-<requirement-id>-CURRENT`，内部消费 `requirement_aggregation` 的 `current_cases`/`defects`/`residual_risks`，不在主 Agent 侧重算。

`release_recommendation` 必须与报告总体结论由同一组 AC 事实推导，禁止用单次运行 verdict 直接替代；两者不一致的清单视为无效。

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

报告器在渲染前校验证据引用：`evidence_runs` 声明了 `result_path` / `report_path` 时，对应文件必须存在（相对路径自 manifest 目录逐级向上解析）；`kind=business_execution` 的运行必须声明 `result_path`，且该文件应能通过 `panda-ai-api-tester-v2` 的 `validate_result.py`。无法通过校验的运行只能标记 `result_source=manual` 并降级为参考证据，不得支撑任何 `passed` 结论。

## 8. 总体结论

报告器按以下优先级计算：

1. 存在 `severity=blocker` 且状态未关闭的产品缺陷，或任一完整执行 AC 失败 → `FAIL`。
2. 无失败但存在必需项被门禁阻塞 → `BLOCKED`。
3. 所有 AC 完整执行且通过 → `PASS`。
4. 其余情况 → `INCONCLUSIVE`。

不得仅依据某次运行的 verdict 直接代替需求级结论。

## 9. 确定性契约

正式报告必须是 manifest 的纯函数产物：同一份 `requirement-report.json` 在任何时间、任何机器、任何时区、任何换行习惯下渲染出的字节完全一致。做不到这一点的产物只能算草稿，不得作为验收证据引用。

| 维度 | 约束 |
|---|---|
| 生成时间 | 只能取自 manifest 的 `generated_at`；缺失时输出「未标注」占位，禁止读取系统时钟 |
| 时区 | 禁止调用 `astimezone()` 等本地化 API，禁止写入本地时区偏移 |
| 换行符 | 恒写 `\n`（`open(..., newline="\n")`），禁止随平台输出 CRLF |
| 排序 | 列表按 `id` 稳定排序，禁止依赖 `dict` / `set` 遍历顺序 |
| 模板 | 产物需可追溯 `template_id` 与 `template_version`；修改模板必须递增版本，不得原地改已发布模板 |
| 身份 | 报告应能回答「由哪份 manifest、哪版模板、哪个渲染器生成」 |

门禁（CI 必过）：

1. 同一 manifest 连续渲染两次，字节相等。
2. `TZ=UTC` / `TZ=Asia/Shanghai` / `TZ=America/New_York` 下渲染结果字节相等。
3. 产物不含 `\r\n`。
4. golden manifest 的渲染结果与快照 diff 为空。

模板只允许存在一份权威实现。其他入口若仍需保留，必须降级为转发到该实现的适配器，不得内联复制模板或样式。

设计令牌同受单一来源约束：全部报告渲染器从 `panda-report-theme/report-base.css` 加载共享令牌与基础样式（构建时内联，保证离线可打开），禁止在模板中重新定义 `:root` 令牌。主题文件缺失时渲染器必须报错拒绝渲染，不得静默降级；产物 footer 必须记录 `template_id@template_version` 与 `base-css:<sha256 前 12 位>`。可用 `PANDA_REPORT_THEME` 环境变量覆盖主题文件路径。令牌变更等价于模板变更，必须递增引用方 `template_version`。

`generated_at` 由 manifest 维护方在首次生成时写入（执行时刻一次定稿），此后不再修改——它属于 manifest 内容身份的一部分，不是渲染期信息。重渲染只能复用该值。


