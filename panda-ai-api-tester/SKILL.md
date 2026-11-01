---
name: panda-ai-api-tester
description: 读取工程 L1 适配层，以结构化 TestPackage 为核心执行接口门禁、即时验证和稳定回归晋级，输出确定性的 TestResult 与 HTML 报告。用户要求验证接口、测试业务链路、诊断接口环境或将稳定用例纳入 pytest/CI 时使用。
agent_created: true
---

# AI API Tester

## 定位

作为 L2 通用接口测试执行层，只读项目级多环境 `test-config.md` Schema 2.1、接口画像和需求 context，生成绑定配置 revision、画像 revision/生命周期/双指纹与环境的 TestPackage 2.1，执行门禁或业务测试，保存结构化 TestResult，再通过固定报告器生成 HTML。运行验证只输出 runtime status、契约差异观察和证据，不回写项目配置或接口画像。

将结构化测试设计、执行证据和结果模型作为一等资产。仅在稳定用例需要本地复现、CI 或周期回归时导出 pytest；禁止把 pytest 代码生成当成接口测试完成。

跨项目时只复用公共模型、Runner、校验器和报告器。禁止跨项目复用接口契约、环境、账号、数据、清理器或凭据。

## 输入与固定产物

必须读取：

- `docs/test-config.md`：唯一支持项目级多环境 Schema 2.1；提供配置 revision、目标环境的变量引用和通用安全约束。
- `docs/interface-profile/<module>.md`：面向人和 AI 的半结构化接口知识契约，包含唯一治理块、画像 revision、生命周期、Owner、副作用、清理边界和双指纹。
- `.workbuddy/panda-api-test-agent/requirements/<requirement-id>/context.json`：需求、固定基线、验收项和专项边界。
- 用户确认的目标环境、范围和副作用边界；临时 URL/凭据仅通过宿主构造的 RuntimeContext 运行时注入，不写入静态资产。

按需生成：

- `.workbuddy/panda-api-test-agent/runs/<run-id>/package.json`：结构化 `TestPackage`。
- `.workbuddy/panda-api-test-agent/runs/<run-id>/result.json`：结构化 `TestResult`。
- `.workbuddy/panda-api-test-agent/runs/<run-id>/evidence/`：脱敏请求、响应和断言证据。
- `reports/<run-id>.html`：由 `scripts/render_report.py` 从 `result.json` 确定性生成。
- `tests/generated/`：仅 `PROMOTE` 模式生成 pytest 薄适配器。

## 两种运行模式与一个晋级动作

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
- 非生产普通写操作默认执行；执行前在 TestPackage 中声明数据准备、对象引用、数据标签、副作用范围和清理/残留处置，不再因“会产生持久化副作用”单独等待用户确认。
- 完成 HTTP、业务 code、关键字段及当前用例启用的 DB/Redis/审计断言。
- 任一步失败后，依赖步骤标记 `not_run`，不得以空值继续。
- 无可靠清理能力时，普通单次非生产写用例可以执行，但必须记录 `pending_objects`、残留风险和清理状态，结果不得宣称完整通过；批量回归、PROMOTE、destructive、生产/疑似生产和真实外部动作仍阻断或要求确认。`cleanup_waiver` 仅用于明确受控的单次残留，不替代风险分级。

### PROMOTE（独立晋级动作）

消费已真实执行并稳定的 EXECUTE 结果，晋级为 pytest/CI 回归资产；它不是 Runner mode，也不生成 `mode=PROMOTE` 的 TestResult。

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
2. 按当前目标读取项目配置和接口画像的最小必需字段；优先消费 Schema 2.1，缺失或旧格式时由 L1 按需补齐，或在不改文件的前提下使用可审计临时运行输入。仅当当前用例依赖的配置/画像字段无法取得时阻断；完整治理身份仍是正式 `PROMOTE` 和完整回归资产的准入条件。
3. 选择 `PROBE` 或 `EXECUTE`；允许 `minimally_usable` 画像支持受控、局部 EXECUTE，但必须在结果中标记治理未完整确认的边界。用户明确要求晋级时，另行消费既有 EXECUTE 结果执行 PROMOTE。
4. 生成或读取 TestPackage 2.1，记录配置 path/revision/environment ID/SHA-256、画像身份与 `environment.runtime_bindings`；不得保存 URL/凭据值或把临时文字当作唯一资产。旧 `_env`/`_key` 字段必须拒绝并重建测试包。
5. 执行自动门禁：环境、契约、鉴权、授权、请求上限、数据和清理能力。
6. 门禁未通过时只停止越界步骤，将受影响用例标记 `not_run` 并保存原因；不依赖该门禁的用例继续执行。
7. 当前用例所需门禁满足后，可信宿主必须调用 `execute_test_package(..., runtime_values=values)` Python API；宿主在内存中构造 RuntimeContext 2.0 并调用 `run_pipeline(..., runtime_context=context)`，按包内 DAG 执行请求、变量提取、断言、数据准备和 teardown。CLI 默认拒绝执行，禁止由 Agent 直接发送正式业务请求或自行计算用例状态。普通非生产持久化副作用默认允许，Runner 必须记录对象、范围、标签、清理和残留风险。
8. Runner 原子写入结构化 `TestResult`；通过 `scripts/validate_result.py` 校验状态、证据和统计一致性。
9. 调用 `scripts/render_report.py` 从 `result.json` 生成单文件 HTML；由可信宿主通过同进程 `run_pipeline` API 串联 Runner、校验器和报告器。任何无法安全提供 RuntimeContext 的 CLI/包装入口必须 fail-closed。
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

带凭据的包必须在顶层声明 `auth_probe`：`strategy=declared` 指向一个只读探测门禁并附无副作用确认记录，`strategy=blocked` 引用 blocking gap 并在 Package 级 Preflight 阻断。`GATE-AUTH` 只证明凭据存在，不证明有效；未通过 `auth_probe` 的运行不得给出 `passed` 结论，也不得声称认证已验证。

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
- Base URL、认证、Origin 白名单和只读操作白名单只允许由 `environment.runtime_bindings` 声明；宿主用相同 key 构造 RuntimeContext 2.0。值不得进入包、证据或报告。
- HTTP、认证、DB、Redis、MQ 必须来自同一环境节点；禁止跨环境资源拼接。
- `config_status` 和画像 lifecycle/revision/指纹由 L1 维护；本 skill 只输出 `runtime_status` 与结构化契约差异观察，认证/连接失败不得自动标为配置或画像漂移。
- 禁止创建、修改、修正或覆盖 `docs/interface-profile/*.md`。实际响应与画像不一致时，记录期望、实际、证据引用、候选分类和责任人，并交由主 Agent 调度 L1 复核；不得为了让用例通过而修改预期。
- DB/Redis/审计未启用时记录 `not_enabled` 或覆盖缺口，不得记为通过。
- 普通非生产写操作默认允许，但优先使用可验证 teardown；无法清理时必须进入结果和待处理对象清单，标记残留风险，不能将运行标记为完整通过。批量、destructive、生产/疑似生产和真实外部动作必须阻断或确认。
- 清理失败必须进入结果和待处理清单；风险豁免必须标记为 `waived`/“已风险豁免”，不能称为清理通过，且有豁免或残留对象时最终 verdict 不得为 `passed`。
- 请求、响应和证据必须脱敏。
- `PROBE` 和 `EXECUTE` 的正式请求必须由确定性 Runner 执行；Agent 只生成/审核 TestPackage、由宿主显式构造并传入已授权 RuntimeContext 2.0、解释结果。Runner 和 pipeline 禁止读取 `os.environ`、stdin、argv 或临时文件补齐运行值。
- HTTP 写夹具仍必须声明 `cleanup_extract`、`object_ref` 与 teardown；普通单次写用例优先如此。若业务链路无法提供 teardown，可使用严格 case-level `cleanup_waiver` 或运行级残留登记：必须是非生产、非批量、单次、带有效 `data_tag` 和静态 `object_ref`，并在结果中标记残留风险；不得用于 destructive、生产/疑似生产、真实外部动作或 PROMOTE。
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
- `test_case`：画像消费、数据、断言或清理逻辑错误；若疑似画像事实变化，先标记 `contract_observation`，确认前不得断言画像已漂移。
- `product_defect`：环境、用例和数据有效，且服务可复现地违反契约。
- `inconclusive`：证据不足。

## 参考资料与脚本

- 用例设计：`references/case-design-rules.md`
- TestPackage：`references/test-package-spec.md`
- TestResult：`references/test-result-spec.md`
- 断言：`references/assertion-rules.md`
- 数据与清理：`references/data-fixture-rules.md`
- 报告：`references/report-spec.md`（单次运行报告，事实子页）
- 需求验收报告：`references/acceptance-report-spec.md`（结论层，六层架构、AC 状态模型、证据等级、度量口径）
- 确定性执行内核：`scripts/runner.py`（仅同进程 Python API）
- 可信宿主入口：`scripts/trusted_host.py`（接收内存运行值、构造 RuntimeContext 2.0、记录阶段耗时与安全阻断结果）
- 流水线入口：`scripts/run_pipeline.py`（仅可信宿主传入 RuntimeContext 2.0；CLI fail-closed）
- 运行时上下文：`scripts/runtime_context.py`
- Bash 包装入口：`scripts/run_tests.sh`
- 结果校验：`scripts/validate_result.py`
- 报告生成：`scripts/render_report.py`
- pytest 晋级校验：`scripts/promote.py`
- pytest 晋级模板：`templates/pytest_package_adapter.py.tpl`

## 完成判定

- `PROBE`：门禁事实、受影响用例的 `not_run` 状态、结构化结果和 HTML 报告齐全。
- `EXECUTE`：实际请求、断言、证据、清理状态、结构化结果和 HTML 报告齐全；普通非生产写入即使存在残留，也必须记录 `pending_objects` 和风险，不得伪装为完整清理通过。
- `PROMOTE`：满足晋级条件，pytest 薄适配器生成并通过语法/collect；不能用 collect 代替业务执行结果。

任何模式都不得把设计完成、脚本生成、collect、skip、mock、门禁请求成功或部分执行描述为完整业务通过。
