---
name: panda-ai-api-tester
description: 读取工程 L1 适配层，以结构化 TestPackage 为核心执行接口门禁、即时验证和稳定回归晋级，输出确定性的 TestResult 与 HTML 报告。用户要求验证接口、测试业务链路、诊断接口环境或将稳定用例纳入 pytest/CI 时使用。
agent_created: true
---

# AI API Tester

## 定位

作为 L2 通用接口测试执行层，读取当前工程的 L1 `test-config.md` 和 `interface-profile`，生成结构化 `TestPackage`，执行门禁或业务测试，保存结构化 `TestResult`，再通过固定报告器生成 HTML 报告。

将结构化测试设计、执行证据和结果模型作为一等资产。仅在稳定用例需要本地复现、CI 或周期回归时导出 pytest；禁止把 pytest 代码生成当成接口测试完成。

跨项目时只复用公共模型、Runner、校验器和报告器。禁止跨项目复用接口契约、环境、账号、数据、清理器或凭据。

## 输入与固定产物

必须读取：

- `docs/test-config.md`：环境、鉴权、数据源、链路、风险和清理约束。
- `docs/interface-profile/<module>.md`：接口七字段画像、生命周期和证据等级。
- 用户确认的目标、环境、范围和副作用边界。

按需生成：

- `.workbuddy/panda-api-test-agent/runs/<run-id>/package.json`：结构化 `TestPackage`。
- `.workbuddy/panda-api-test-agent/runs/<run-id>/result.json`：结构化 `TestResult`。
- `.workbuddy/panda-api-test-agent/runs/<run-id>/evidence/`：脱敏请求、响应和断言证据。
- `reports/<run-id>.html`：由 `scripts/render_report.py` 从 `result.json` 确定性生成。
- `tests/generated/`：仅 `PROMOTE` 模式生成 pytest 薄适配器。

## 三种执行模式

### PROBE

用于环境诊断、健康检查、鉴权门禁、契约探测和无副作用探索。

- 不生成 pytest。
- 默认不准备或修改业务数据。
- 先检查环境、鉴权、契约指纹、请求上限和授权范围。
- 门禁失败时生成 `TestResult` 和阻断报告；业务用例标记 `not_run`。
- 门禁请求不得被计为业务用例通过。

### EXECUTE

用于用户当前要求的真实接口或业务链路验证。

- 先生成结构化 `TestPackage`，再由通用执行逻辑解释执行。
- 默认不生成 pytest。
- 写操作前必须确认数据准备、清理方式和副作用范围。
- 完成 HTTP、业务 code、关键字段及启用的 DB/Redis/审计断言。
- 任一步失败后，依赖步骤标记 `not_run`，不得以空值继续。
- 无可靠清理能力时默认禁止执行创建、编辑、状态变更等写用例；仅允许经过严格 package 校验的 case-level `cleanup_waiver` 结构化例外，且不得用于生产、destructive、write_attempt、批量或 PROMOTE。

### PROMOTE

用于将已真实执行并稳定的用例晋级为 pytest/CI 回归资产。

仅同时满足以下条件时执行：

1. 接口契约已确认且未漂移。
2. 环境和鉴权稳定。
3. 数据准备和 Oracle 可重复。
4. 写用例具有已验证的 teardown。
5. 用例已在 `EXECUTE` 模式至少真实通过一次。
6. 用户明确要求沉淀回归、生成 pytest 或接入 CI。

`PROMOTE` 只生成薄适配器：从 `TestPackage` 参数化加载用例并调用公共 Runner。禁止为每个用例复制 HTTP 客户端、脱敏和报告逻辑。生成后必须完成语法检查和 pytest collect；可用环境具备时再复跑，collect 成功不等于业务通过。

## 模式选择

- “检查环境”“服务通不通”“看看鉴权” → `PROBE`。
- “现在测试接口”“验证业务链路” → `EXECUTE`。
- “生成 pytest”“加入回归”“接入 CI” → `PROMOTE`。
- 用户未明确要求 pytest 时，禁止默认进入 `PROMOTE`。
- 环境门禁失败、鉴权缺失、契约漂移或清理不可用时，禁止生成 pytest。

## 核心流程

1. 绑定当前工作区、项目标识、运行 ID 和输出目录。
2. 读取 L1、目标画像、授权摘要和契约/风险指纹。
3. 选择 `PROBE`、`EXECUTE` 或 `PROMOTE`，记录选择依据。
4. 生成或读取结构化 `TestPackage`；不得把 Agent 临时文字当作唯一用例资产。
5. 执行自动门禁：环境、契约、鉴权、授权、请求上限、数据和清理能力。
6. 门禁未通过时停止越界步骤，将受影响用例标记 `not_run` 并保存原因。
7. 门禁通过后，必须调用 `scripts/runner.py <package.json> --output-dir <run-dir>` 按包内 DAG 执行请求、变量提取、断言、数据准备和 teardown；禁止由 Agent 直接发送正式业务请求或自行计算用例状态。
8. Runner 原子写入结构化 `TestResult`；通过 `scripts/validate_result.py` 校验状态、证据和统计一致性。
9. 调用 `scripts/render_report.py` 从 `result.json` 生成单文件 HTML；优先使用跨平台 `scripts/run_pipeline.py` 串联 Runner、校验器和报告器，`scripts/run_tests.sh` 仅作为 Bash 包装入口。
10. `PROMOTE` 不重跑业务请求；仅使用 `scripts/promote.py <existing-result.json> <generated-tests>` 消费已有通过结果，验证稳定契约、清理和阻断缺口后再执行语法/collect。

## TestPackage 要求

每条用例至少声明：

- `case_id`、名称、需求与接口引用。
- 前置条件和依赖用例。
- 请求方法、路径、参数和凭据变量名。
- HTTP、JSON 结构、业务 code、关键字段断言。
- 可选 DB、Redis、审计和跨域只读 Oracle。
- 变量提取和作用域。
- 副作用等级、数据标签、teardown 责任。
- 证据等级和未覆盖项。

详细结构见 `references/test-package-spec.md`。

## TestResult 要求

状态只允许：

- `passed`：实际执行且所有必需断言通过。
- `failed`：实际执行且至少一个必需断言失败。
- `error`：执行基础设施或用例实现异常。
- `skipped`：依据明确规则有意跳过。
- `not_run`：因门禁、依赖失败或未授权而未发送业务请求。

门禁结果与业务用例结果分开记录。连接失败不得自动计为全部业务用例失败；未发送请求的业务用例必须为 `not_run`。

详细结构见 `references/test-result-spec.md`。

## 执行与断言纪律

- 禁止 mock 被测对象。
- 禁止只断 HTTP 200；至少断 HTTP、业务 code 和关键字段。
- 凭据只从安全环境变量或授权配置读取，不写入包、证据或报告。
- DB/Redis/审计未启用时记录 `not_enabled` 或覆盖缺口，不得记为通过。
- 写操作必须具备可验证 teardown；没有清理能力时阻断写用例。
- 清理失败必须进入结果和待处理清单，不能将运行标记为完整通过；风险豁免必须标记为 `waived`/“已风险豁免”，不能称为清理通过，且有豁免时最终 verdict 不得为 `passed`。
- 请求、响应和证据必须脱敏。
- `PROBE` 和 `EXECUTE` 的正式请求必须由确定性 Runner 执行；Agent 只生成/审核 TestPackage、注入已授权环境变量并解释结果。
- HTTP 写夹具仍必须声明 `cleanup_extract`、`object_ref` 与 teardown；普通写用例必须如此。唯一例外是 case-level `cleanup_waiver`：包级 `risk.cleanup_waiver_enabled=true`，固定 `scope=case`、`max_requests=1`，有效审批与有效期、非空 `data_tag`、静态 `object_ref`，请求体递归含同一标签，且与 teardown/cleanup_extract 互斥。
- Agent 不得自由编写正式报告；正式 HTML 必须由固定报告器从已校验的 `result.json` 生成。

## 结果呈现与缺口分层

`TestResult` 是事实源，但直接把它整段交给用户会造成阅读负担。对外呈现时必须分层：

1. **先给结论**：能不能跑、跑出了什么、卡在哪一件事。门禁失败时明确"实际请求 0 次、业务用例全部 not_run"，避免被误读为已测试。
2. **缺口分层**：`known_gaps` 中 `blocking=true` 的项属于当前阻塞，最多列 3 条；`blocking=false` 的项收进覆盖边界说明，不得与阻塞项并列。
3. **收敛为决策点**：把阻塞项收敛成一个用户可执行的选项（A/B 及各自代价），不要要求用户逐条回复。
4. **禁止搬运**：不得把 `known_gaps` 或 `gate_results` 原样输出后让用户自行判断优先级。

`known_gaps` 必须是对象数组（字段见 `references/test-result-spec.md`「已知缺口」），不得写成平铺字符串。用例设计说明、占位请求解释等实现细节不属于缺口，应写入用例字段。

硬标准：用户读完能直接回答"现在能不能跑、卡在哪一件事、我该做什么"。需要追问即为呈现失败。

## 失败分类

依据证据分类：

- `environment`：DNS、连接拒绝、超时、依赖、DB/Redis 不可用。
- `test_case`：画像、数据、断言或清理逻辑错误。
- `product_defect`：环境、用例和数据有效，且服务可复现地违反契约。
- `inconclusive`：证据不足。

## 参考资料与脚本

- 用例设计：`references/case-design-rules.md`
- TestPackage：`references/test-package-spec.md`
- TestResult：`references/test-result-spec.md`
- 断言：`references/assertion-rules.md`
- 数据与清理：`references/data-fixture-rules.md`
- 报告：`references/report-spec.md`
- 确定性执行：`scripts/runner.py`
- 跨平台一键执行：`scripts/run_pipeline.py`
- Bash 包装入口：`scripts/run_tests.sh`
- 结果校验：`scripts/validate_result.py`
- 报告生成：`scripts/render_report.py`
- pytest 晋级校验：`scripts/promote.py`
- pytest 晋级模板：`templates/pytest_package_adapter.py.tpl`

## 完成判定

- `PROBE`：门禁事实、受影响用例的 `not_run` 状态、结构化结果和 HTML 报告齐全。
- `EXECUTE`：实际请求、断言、证据、清理状态、结构化结果和 HTML 报告齐全。
- `PROMOTE`：满足晋级条件，pytest 薄适配器生成并通过语法/collect；不能用 collect 代替业务执行结果。

任何模式都不得把设计完成、脚本生成、collect、skip、mock、门禁请求成功或部分执行描述为完整业务通过。
