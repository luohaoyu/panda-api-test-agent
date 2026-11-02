---
name: panda-ai-api-tester-v2
description: 读取工程 L1 适配层，以结构化 TestPackage 为核心执行接口门禁、即时验证和稳定回归晋级，输出确定性的 TestResult 与 HTML 报告。用户要求验证接口、测试业务链路、诊断接口环境或将稳定用例纳入 pytest/CI 时使用。；v2 架构升级版
agent_created: true
version: 2.5.0
---

# AI API Tester

## 定位

作为 L2 通用接口测试执行层，只读项目级多环境 `test-config.md` Schema 2.1、接口画像和需求 context，先从需求设计候选用例，再规划覆盖与执行依赖；数据、凭证、fixture、Oracle 或 cleanup 缺口只记录为规划事实，不在规划阶段停止流程。完成人工审核后，按审核批准范围执行最终可行性门禁，通过后构建 TestPackage 2.2，执行运行门禁或业务测试，保存结构化 TestResult，再通过固定报告器生成 HTML。Runner 仅兼容执行既有 TestPackage 2.1，不允许新建流程借 2.1 绕过规划与审核。

遵循 **AI 指令约束优先，脚本能力辅助**：AI/Skill 负责理解接口语义、判断质量维度适用性、设计断言和解释缺口；脚本只把 AI 已确认的设计结构化，执行请求，校验审核范围，保护凭据，统计结果和生成证据。没有 AI 语义决策时，脚本输出的质量维度只能是候选设计，不得直接作为已确认测试范围。运行验证只输出 runtime status、契约差异观察和证据，不回写项目配置或接口画像。

`scripts/single_api_quality.py` 的 `build_single_api_quality_intent(...)` 只把 AI/Skill 已选择的质量维度物化为可审核的结构化测试设计，不读取 secret、不执行请求；未提供 `ai_dimension_decisions` 时只能生成候选用例并登记 `pending_ai_decision`，不得直接进入执行包。输出可交给 `plan_compiler.compile_plan` 前仍需完成人工设计审核。已有 curl 可直接作为设计输入，不因此强制扫描工程。

将结构化测试设计、执行证据和结果模型作为一等资产。仅在稳定用例需要本地复现、CI 或周期回归时导出 pytest；禁止把 pytest 代码生成当成接口测试完成。

跨项目时只复用公共模型、Runner、校验器和报告器。禁止跨项目复用接口契约、环境、账号、数据、清理器或凭据。

## 输入与固定产物

必须读取：

- `docs/test-config.md`：唯一支持项目级多环境 Schema 2.1；提供配置 revision、目标环境的变量引用和通用安全约束。
- `docs/interface-profile/<module>.md`：面向人和 AI 的半结构化接口知识契约，包含唯一治理块、画像 revision、生命周期、Owner、副作用、清理边界和双指纹。
- `.workbuddy/.panda-api-test-agent/requirements/<requirement-id>/context.json`：需求、固定基线、验收项和专项边界。
- 用户确认的目标环境、范围和副作用边界；临时 URL/凭据仅通过宿主构造的 RuntimeContext 运行时注入，不写入静态资产。

按需生成：

- `.workbuddy/.panda-api-test-agent/requirements/<requirement-id>/case-design.json`：由需求/AC 推导的候选用例设计。
- `.workbuddy/.panda-api-test-agent/requirements/<requirement-id>/feasibility.json`：覆盖矩阵、执行依赖矩阵、待人工审核状态或审核后 `GATE-FEASIBILITY` 结果。
- `.workbuddy/.panda-api-test-agent/runs/<run-id>/package.json`：人工审核后，批准范围通过最终可行性门禁才构建的结构化 `TestPackage 2.2`。
- `.workbuddy/.panda-api-test-agent/runs/<run-id>/result.json`：结构化 `TestResult`。
- `.workbuddy/.panda-api-test-agent/runs/<run-id>/evidence/`：脱敏请求、响应和断言证据。
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

用于用户当前要求的真实接口或业务链路验证。`api_debug` 入口默认只允许一个最小复现用例，必须携带 `debug_context`（observed/expected、请求摘要、诊断假设、trace/correlation 引用和 `no_automatic_retry`），不得自动扩展为全量质量维度。调通后如需质量测试，必须重新路由到 `single_api_quality` 并生成新的设计指纹。

- 先生成结构化 `TestPackage`，再由通用执行逻辑解释执行。
- 默认不生成 pytest。
- 非生产普通写操作默认执行；执行前在 TestPackage 中声明数据准备、对象引用、数据标签、副作用范围和清理/残留处置，不再因“会产生持久化副作用”单独等待用户确认。
- 完成 HTTP、业务 code、关键字段及当前用例启用的 DB/Redis/审计断言。
- 任一步失败后，依赖步骤标记 `not_run`，不得以空值继续。
- 无可靠清理能力时，普通单次非生产写用例可以执行，但必须记录 `pending_objects`、残留风险和清理状态，结果不得宣称完整通过；批量回归、PROMOTE、destructive、生产/疑似生产和真实外部动作仍阻断或要求确认。`cleanup_waiver` 仅用于明确受控的单次残留，不替代风险分级。
- 审核门 fail-closed：运行状态缺失、留空或出现无法识别的 `review_status` 时必须阻断执行（`slice_blocked_unreviewed`），不得因字段缺失而放行；`approved`/`partially_approved` 之外的任何状态都不可执行。
- 清理义务与业务通过分离：写入任务的 `passed` 只是业务事实，不是清理证据。`cleanup_obligations` 同时记录 cleanup 任务（`task_ids`）和留下对象的写入任务（`residual_task_ids`）；只有资源事实为 `cleaned`/`verified_absent` 或存在已通过的 cleanup 任务覆盖该对象时，Reducer 才可给出 `cleanup.status=complete` 与 `passed`。
- Runner 声明的 `cleanup.pending_objects` 必须由 Slice Executor 写入 `resources.json` 并登记 `pending_cleanup`，不得只留在子运行结果里。

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

1. **REQUIREMENT**：绑定当前工作区、项目标识、需求 ID、运行边界和输出目录；提取 AC、业务规则、风险与专项限制。
2. **CASE DESIGN**：先从需求/AC/业务规则设计候选用例。候选用例描述验证目标、业务前置状态、请求、断言和 Oracle；不得因尚无真实数据就省略用例，也不得把候选用例称为已可执行。
3. **FEASIBILITY / DATA PLAN**：逐条分析候选用例的数据、凭证、环境、fixture、Oracle 和 cleanup 依赖，生成 `coverage_map` 与 `dependency_matrix`。每条候选用例必须显式声明 `execution_dependencies`；无额外依赖时写空数组。缺少数据或执行事实时记录 `status=blocked`、`known_gap`、责任人和下一步，但规划必须继续完成；此时门禁为 `pending_review`，不得把数据缺口误报为流程失败。
4. **HUMAN REVIEW**：向审核人提交完整候选范围、依赖矩阵和覆盖边界。审核人决定全量批准、只批准依赖已闭合的子集、暂缓部分用例、驳回或要求补充。审核范围外用例保留为 `not_run`/未覆盖边界，不得静默删除。
5. **FINAL GATE-FEASIBILITY**：在任何 RuntimeContext、Secret envelope、fixture 或 HTTP 之前，按人工审核批准范围计算最终静态门禁。只有批准范围仍包含 `candidate_only`、未声明依赖、未解决依赖、未知 case 依赖或依赖环时，才停止在 Package Build 前。若人工只批准依赖已闭合的子集，则门禁通过并继续该子集，但不得宣称完整需求通过。
6. **PACKAGE BUILD**：只有人工审核完成且最终门禁通过后，才调用 `compile_approved_package(..., feasibility_plan=plan)` 构建 TestPackage 2.2，并绑定配置 revision、画像身份、审核范围、可行性快照及源指纹。既有 2.1 包只做兼容执行；新包不得降级到 2.1 绕过规划、审核或门禁。
7. 选择 `PROBE` 或 `EXECUTE`；允许 `minimally_usable` 画像支持受控、局部执行，但必须披露治理和完整覆盖边界。
8. 执行运行时门禁：环境、契约、鉴权、授权、请求上限、fixture 和清理能力。门禁未通过时只停止受影响步骤，将用例标记 `not_run` 并保存原因。
9. 当前范围门禁满足后，可信宿主调用 `execute_test_package(..., runtime_values=values)`；宿主在内存中构造 RuntimeContext 2.0 并调用 `run_pipeline(...)`，按 DAG 执行请求、变量提取、断言、数据准备和 teardown。
10. Runner 原子写入结构化 `TestResult`，包含最终可行性门禁和 Package Build 快照；通过 `validate_result.py` 校验并由 `render_report.py` 确定性生成 HTML。报告必须区分候选用例、审核批准用例、当前可执行用例、审核范围外用例和完整需求覆盖。
11. **REPORT / PROMOTE**：先报告当前运行事实；`PROMOTE` 不重跑业务请求，只消费已有通过结果，验证稳定契约、清理和阻断缺口后执行语法/collect。

## 候选用例与 TestPackage 要求

候选用例先于 FeasibilityPlan，FeasibilityPlan 先于人工审核，人工审核先于最终 TestPackage。不得颠倒为“先有 PLAN 再设计用例”，不得在数据规划阶段因缺少真实数据而停止流程，也不得跳过规划或人工审核直接建包。

每条进入批准范围的候选用例至少声明：

- `case_id`、需求/规则/场景引用和 `depends_on`。
- `execution_dependencies`：数据、凭证、环境、fixture、Oracle、cleanup；状态为 `resolved|not_required|blocked`。`blocked` 在规划阶段只表示当前不可执行，不等于整个流程失败。
- 候选适用性；`candidate_only=true` 时不得进入人工批准的可执行范围。

TestPackage 中每条可执行用例至少声明：

- `case_id`、名称、需求与接口引用。
- 前置条件和依赖用例。
- 请求方法、路径、参数和凭据变量名。
- HTTP、JSON 结构、业务 code、关键字段断言。
- 可选 DB、Redis、审计和跨域只读 Oracle。
- 变量提取和作用域。
- 副作用等级、数据标签、teardown 责任。
- 证据等级和未覆盖项。

详细结构见 `references/feasibility-plan-spec.md` 和 `references/test-package-spec.md`。

带凭据的包必须在顶层声明 `auth_probe`：`strategy=declared` 指向一个只读探测门禁并附无副作用确认记录，`strategy=blocked` 引用 blocking gap 并在 Package 级 Preflight 阻断。审核状态允许 `pending_review/approved/partially_approved/rejected/plan_only/expired/superseded`；只有当前审核下未过期、未被取代的已批准范围内用例可执行，其他用例固定为 `not_run`。`GATE-AUTH` 只证明凭据存在，不证明有效；未通过 `auth_probe` 的运行不得给出 `passed` 结论，也不得声称认证已验证。

## TestResult 要求

用例状态允许：

- `passed`：实际执行且所有必需断言通过。
- `failed`：实际执行且至少一个必需断言失败。
- `error`：执行基础设施或用例实现异常，并说明 `request_delivery`。
- `blocked`：本用例受影响门禁或安全策略阻断。
- `inconclusive`：请求已送达但证据不足。
- `passed_with_gaps`：API 断言通过但存在非阻塞质量/治理缺口。
- `not_api_verifiable`：请求已送达但目标结论不能由 API 层可靠验证。
- `skipped`：依据明确规则有意跳过。
- `not_run`：因门禁、依赖、未授权、审核范围外或事实不足而未发送业务请求。未批准用例不得执行。

门禁结果与业务用例结果分开记录。连接失败不得自动计为全部业务用例失败；未发送请求的业务用例必须为 `not_run`。

详细结构见 `references/test-result-spec.md`。

## 执行与断言纪律

- 禁止 mock 被测对象。
- 禁止只断 HTTP 200；至少断 HTTP、业务 code 和关键字段。
- Base URL、认证、Origin 白名单和只读操作白名单只允许由 `environment.runtime_bindings` 声明；宿主用相同 key 构造 RuntimeContext 2.0。值不得进入包、证据或报告。
- HTTP、认证、DB、Redis、MQ 必须来自同一环境节点；禁止跨环境资源拼接。
- `config_status` 和画像 lifecycle/revision/指纹由 L1 维护；本 skill 只输出 `runtime_status` 与结构化契约差异观察，认证/连接失败不得自动标为配置或画像漂移。
- 凭据默认经宿主内存注入；允许固定 `.panda-api-test-agent/local/runtime/secrets/{run_id}.secret.json` 作为唯一的 Host-private ephemeral secret file 例外。该文件必须绑定运行、环境、包指纹与短 TTL，只含已声明 Secret binding，由可信宿主校验并删除后才构造 RuntimeContext；其他落盘方式仍禁止。生产/疑似生产隔离；写请求未知交付状态禁止自动重试。
- 禁止创建、修改、修正或覆盖 `docs/interface-profile/*.md`。实际响应与画像不一致时，记录期望、实际、证据引用、候选分类和责任人，并交由主 Agent 调度 L1 复核；不得为了让用例通过而修改预期。
- DB/Redis/审计未启用时记录 `not_enabled` 或覆盖缺口，不得记为通过。
- 普通非生产写操作默认允许，但优先使用可验证 teardown；无法清理时必须进入结果和待处理对象清单，标记残留风险，不能将运行标记为完整通过。批量、destructive、生产/疑似生产和真实外部动作必须阻断或确认。
- 清理失败必须进入结果和待处理清单；风险豁免必须标记为 `waived`/“已风险豁免”，不能称为清理通过，且有豁免或残留对象时最终 verdict 不得为 `passed`。
- 请求、响应和证据必须脱敏。
- 任何会发送 HTTP 请求的 `PROBE` 和 `EXECUTE` 正式请求必须由确定性 Runner 执行，且先通过人类测试设计审核；Agent 只生成/审核 TestPackage、由宿主显式构造并传入已授权 RuntimeContext 2.0、解释结果。pipeline 和 ApiTestRunner 继续只接收显式 RuntimeContext；可信宿主可在 pipeline 前消费固定 local Secret bridge。`os.environ`、stdin、argv、任意文件、目录扫描和历史文件回退仍禁止。
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

- 统一入口状态模型：由主 Agent 的 `scripts/entry_state.py` 维护；L2 接收 `entry_type`、`review`、`approved_case_ids` 和设计指纹，不自行改变入口语义。
- 调试设计：`scripts/api_debug.py`（默认一个最小复现用例，不自动扩展质量测试）
- 用例设计：`references/case-design-rules.md`
- 可行性规划：`references/feasibility-plan-spec.md`
- TestPackage：`references/test-package-spec.md`
- TestResult：`references/test-result-spec.md`
- 断言：`references/assertion-rules.md`
- 数据与清理：`references/data-fixture-rules.md`
- 报告：`references/report-spec.md`（单次运行报告，事实子页）
- 需求验收报告：`references/acceptance-report-spec.md`（结论层，六层架构、AC 状态模型、证据等级、度量口径）
- 可行性规划器：`scripts/feasibility_plan.py`（纯本地静态计算，不接触运行时值）
- 测试包编译器：`scripts/package_compiler.py`（只接受已审核且可行性门禁通过的设计）
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
