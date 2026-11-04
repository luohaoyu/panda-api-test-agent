# API Test Agent 编排规则（Schema 2.1）

主 Agent 只判断、调度和聚合，不创建项目配置、不发送正式请求、不回写运行状态。`intent_router.py` 只做四类入口的本地设计路由；`single_api_quality.py`（L2）只做单接口结构化设计。二者都不读取 secret、不发请求；任何实际执行必须经过人工审核和 L2 trusted host/Runner。用户已提供 curl 时可直接进入设计输入，不强制扫描工程。

依赖布局：主链路要求 `panda-ai-api-tester-v2` 与本技能平级可用；`panda-report-theme` 仅在正式需求级 HTML 报告阶段按需加载，不能阻塞入口、设计、审核或运行结果。

## 1. 资产分层

| 层级 | 资产 | 职责 |
|---|---|---|
| 项目级 | `docs/test-config.md`、`.panda-api-test-agent/adapter/` | canonical 多环境 HTTP、认证绑定、DB、Redis、MQ、可观测性、安全策略和项目适配规则；Secret 值不得进入该层 |
| 接口级 | `docs/interface-profile/*.md` | canonical 路径、Schema、错误码、前置数据、副作用和契约指纹；Secret 值不得进入该层 |
| 持久 Secret 级 | `.panda-api-test-agent/local/secrets/environments/<environment-id>.secret.json` | 环境级持久 Secret Store；仅由可信宿主按 environment ID + 已声明 binding 精确存取，与一次性 Run bridge 并存 |
| 需求级 | `.panda-api-test-agent/requirements/<id>/context.json`、`external-evidence.jsonl` | PRD、代码基线、目标接口、AC、专项数据/风险/授权；以及 append-only 的外部验证层证据登记（UI/DB/MQ/审计/人工） |
| 数据源级 | `.panda-api-test-agent/data-sources.md` | 经责任人确认的造数通道登记：名称/工具（含 MCP 类如 datafactory）、支持环境、能力范围、数据标签规范、清理方式；设计期判"可构造"的证据来源，由 L1 或责任人确认后维护 |
| 运行级 | `.panda-api-test-agent/runs/<run-id>/package.json`、`result.json`、evidence；`.panda-api-test-agent/local/runtime/secrets/<run-id>.secret.json` | 单次计划与不可变执行事实；后者仅为 `ephemeral_secret` 的一次性 bridge |

组合关系：项目配置 + 接口画像 + 需求 context + 单次参数 = TestPackage。

## 2. 按需配置与局部门禁

进入运行前按当前测试目标检查最小必需集合，不要求无关资源全部就绪：

1. 简单单接口、调试和只读探测优先使用经审核的临时运行输入，不因缺少完整 `docs/test-config.md`、`docs/interface-profile/` 或适配根目录触发全工程前置扫描；只有当前用例确实需要长期配置、认证绑定、复杂数据或 Oracle 时，才调度 `panda-l1-adapter-generator-v2` 按目标局部初始化或增量维护。不得预创建无关子目录；不得自动迁移或覆盖既有配置。
2. 用户提供配置后先分类：`stable_non_secret` → canonical config/画像；`persistent_secret` → 环境级持久 Secret Store；`ephemeral_secret` → 活动 Run 宿主内存/一次性 bridge；`runtime_non_secret` → 活动 Run/RuntimeContext；`requirement_fact` → requirement context。分类记录只保存 key、environment ID、binding、来源与去向，不复制 Secret 值。
3. 当前 HTTP 请求至少需要可验证的环境身份、Base URL、认证变量、超时和响应大小限制；DB、Redis、MQ、日志、轨迹和外部推送只在当前 AC 需要时获取。运行级结论不要求用户理解这些内部资产名。
4. `environment_id` 必须可识别且不得是生产或疑似生产；无法确认环境安全时阻断，而不是用默认值猜测。
5. 所有实际使用资源必须来自同一环境节点，禁止 HTTP/Token/DB/Redis/MQ 跨环境拼接。
6. 接口画像按需检查：路由/方法、请求参数、响应/业务码、鉴权、副作用和当前用例需要的 Oracle 足够即可执行；完整 Owner、双指纹、清理和外部边界在对应正式 EXECUTE/PROMOTE 或资产沉淀时补齐。
7. 需求 context 缺失时，先由主 Agent 根据 PRD、用户输入和实际采用基线创建最小上下文；不得因无关 AC 或未使用资源缺失阻断当前可执行 AC。
8. 执行依赖证据闭环：对每个 data、credential、environment、fixture、oracle、cleanup 依赖，先定义最小必要事实，再检查直接相关来源，记录事实状态和 evidence_refs，并判定 `resolved|not_required|blocked`。`not_checked` 不得直接判为 `blocked`；`unavailable` 不等于事实不存在；`resolved` 必须有证据引用；`blocked` 必须绑定具体 case/dependency，记录缺失事实、已查来源、停止理由和最小 next_action。依赖证据已经足以判定，或后续来源不会改变状态、安全判断或批准范围时，必须停止无关取证；仅当新动作可能改变这些结论时继续。判测试数据缺口前，先查 `data-sources.md` 登记与当前会话可用的造数类 MCP/连接器（如 datafactory），核对支持环境与授权；存在且匹配当前目标环境时不判缺，改判为『需造数动作设计』，把造数建模为声明 `object_ref`/`teardown`/数据标签的 fixture 写入任务纳入设计。判缺必须登记 evidence（查过的来源与不可构造原因），未登记 evidence 的缺口视为诊断不完整。数据缺口只能影响本轮 `execution_case_ids`，必须保留候选设计、审核范围和覆盖分母，并将受影响用例标记 `planned_disposition=not_run`。依赖从 blocked 变为 resolved 时只更新 feasibility，不改写设计语义；只有请求、断言、依赖语义或副作用变化才回到设计审核。

配置、画像或运行 binding 判缺前，必须按“当前输入 → canonical `docs/test-config.md` / `docs/interface-profile/` → 目标环境持久 Secret Store → 活动 Run”检查并记录来源命中情况；任一来源已有仍有效值即复用，不得重复追问。四处均无值时，缺口才可映射到具体 `stage`、AC 或 case。只阻断依赖该缺口的项，其余项继续执行。临时 URL/Token 仅运行时注入，不写入配置。旧格式仍不能被消费或伪装为长期 Schema 2.1；只有当前获准用例确实依赖配置时，才由 L1 依据可验证证据生成独立、可审计的临时 Schema 2.1 运行配置，不修改旧文件；正式资产由 L1 按需补齐。

## 2.1 AI 与脚本职责边界

- AI/Skill 负责自然语言入口理解、工程按需扫描、业务语义解释、需求可验证性判断、测试维度选择和结果解释；数据源前置检查（核对 `data-sources.md` 登记与会话内造数类 MCP）属于 AI 语义工作，在判缺前完成；项目适配层内容由 L1 负责初始化和维护，主 Agent 只负责判断和调度。
- L1 脚本只提供可选的固定格式候选发现；其输出必须标记为候选，不等于已部署、已确认或可执行事实。
- L1 事实账本（`docs/scan-facts/ledger.jsonl`）由 L1 独占写入；主 Agent 只读引用 `fact_id` 与确认状态，不确认、不改写、不复制成第二事实源。画像 `lifecycle=executable` 时未确认事实由 L1 校验与执行侧 `runner` 双重阻断。
- L2 设计脚本只结构化 AI 已作出的语义选择；无 AI 决策时，候选设计必须停在 `pending_ai_decision`，不得进入可执行范围。
- 审核、凭据隔离、危险请求控制、请求执行、请求次数、指纹、结果统计和证据追溯必须由确定性脚本兜底；需求级 HTML 报告仅在触发需求报告条件时生成。

## 3. 状态模型

四类入口共用两套明确分工的模型，不要混用：

- **入口状态**（`scripts/entry_state.py`）：`entry_type`、`phase`、`review_status`、`approved_case_ids`、`design_fingerprint`、`plan_fingerprint`、`blocking_gap_ids`、`last_action`、`next_action`、`request_count_expected`、`request_count_sent`、`failure_class`、`verdict` 的规范化与校验。
- **运行状态**（L2 `run_state` 持久化）：`status`（`ready`/`awaiting_approval`/`blocked`/`interrupted`/`reduced`/`reported`）、`review_status`、`completed_slices`、`pending_cleanup` 等实际落盘字段。

两者关系：`phase` 只描述流程位置，执行许可由方案审核、feasibility 与独立执行授权共同决定。`review_status=approved|partially_approved` 只允许推进到可行性计算；新 TestPackage 只有在 `execution_authorization.status=approved|partially_approved`、授权范围非空且指纹匹配时才能进入 `executing`。缺方案审核记录为 `slice_blocked_unreviewed`，缺执行授权记录为 `slice_blocked_execution_authorization`。不存在“phase 到了 executing”或“方案已批”就能发送请求的情况。

在上述流程状态之外，入口状态增加独立的**决策状态**，不得与审核或运行许可混用：

- `FACT_GATHERING`：当前阶段事实仍不足，且存在安全、只读、可逆并可能改变完整设计、核心预期、安全判断或阻塞结论的取证动作，必须连续推进，不得向用户转嫁；
- `READY`：当前阶段最小事实闭合，可以推进到下一流程阶段，但不代表具备执行许可；事实充分后必须停止扩展取证；
- `DEGRADED_READY`：完整候选设计所需事实已闭合，但部分数据、凭证、fixture、Oracle、cleanup 或外部证据等执行依赖未闭合；候选设计不得缩减，未闭合部分进入可行性或证据闭环，不得把降级推进写成需求通过；
- `PENDING_REVIEW`：测试设计已形成，等待唯一的人工方案审核门；
- `BLOCKED_USER`：所有安全下一动作已耗尽，且已生成 `stop_proof`，只等待最小外部输入；
- `TERMINAL`：流程已完成、拒绝、取消或不可恢复结束。

每项关键事实使用 `confirmed/candidate/insufficient/conflicted/unavailable/not_checked/not_applicable`。`not_checked` 不得汇报成“没有”，`unavailable` 只描述对应来源，`insufficient` 不得被升级为 `none`。主 Agent 只在当前 Run/context 中保存影响决策的证据包络和来源引用，不复制 L1 事实账本，不保存完整思维过程。

`api_debug` 默认只生成一个最小复现用例，不自动扩展为质量维度；`requirement_validation` 的 `none` 验收项不生成 API 用例。

- `config_status`：项目环境与资源声明是否正确，由 L1 维护；运行技能不得修改。
- `profile lifecycle`：接口契约知识是否已确认且足以安全执行，由 L1 依据责任人确认维护；运行技能不得修改。
- `runtime_status`：当前资源是否可用，由 PROBE 输出证据；失败不自动等于配置或画像漂移。

漂移必须分型：

- 项目配置漂移：Gateway、认证、DB、Redis、MQ、可观测性或环境拓扑变化，交 L1 + 环境/资源责任人。
- 接口画像漂移：路由、参数、响应、错误码、鉴权、副作用、清理或异步一致性变化，交 L1 + 接口研发/业务责任人。

典型判断：

- HTTP/认证状态未知：允许进入受控 PROBE。
- 当前 Token 失败：当前环境 EXECUTE 阻断；认证配置仍可为 active。
- UAT DB 漂移：只阻断需要 UAT DB 的场景；其他环境和资源不受影响。
- 环境退役、生产或无法确认是否为非生产：阻断所有运行。
- 普通非生产 `write`、日志、轨迹和异步消息属于默认可执行副作用；`destructive`、批量、真实外部动作、删除/清理已有数据仍需确认，并且生产、破坏性、批量或真实外部动作必须再提供独立高风险授权，缺少授权时发送请求前阻断。
- 无法清理不再自动阻断普通单次业务写入；必须记录精确对象、数据标签、残留风险和清理状态。只有 PROMOTE、批量回归或需要“完整通过”结论时，可靠 teardown 才是强制门禁。

## 4. 阶段与调度

所有入口先执行证据驱动决策循环，再进入既有测试生命周期：

```text
识别当前目标与阶段最小事实
  -> 登记事实状态与来源引用
  -> 选择安全且有价值的下一动作
  -> FACT_GATHERING / READY / DEGRADED_READY / BLOCKED_USER
  -> 复评

intent_received -> preparing -> designing -> pending_review -(plan_review)-> feasibility -> pending_execution_approval -(execution_approval)-> executing -> reducing -> reported
```

决策循环的不变量：

1. 单一来源失败不得直接产生全局终止；先评估该事实是否可由其他当前授权来源、已有资产或确定性动作补齐。
2. 关键事实仍为 `not_checked` 时不得形成停止证明；仅当候选动作可能改变完整设计、核心预期、安全判断或阻塞结论时保持 `FACT_GATHERING`。查到不同结果不会改变当前设计或结论时必须停止；无法判断时只再检查一个最权威来源。
3. 当前事实已足以形成完整 coverage obligations 和明确、可诊断的候选用例时，必须停止扩展取证并进入设计。数据、凭证、fixture、运行时 Oracle、cleanup 或外部证据未闭合时进入 `DEGRADED_READY`，完整候选设计不得缩减，未闭合项进入可行性或证据闭环。
4. 只有无法形成完整设计、无法安全降级推进且不存在能改变当前结论的安全下一动作时才进入 `BLOCKED_USER`。`stop_proof` 至少包含缺失事实、已查来源、不可用/耗尽动作、最小用户输入、阻塞作用域和恢复阶段。
5. 事实补充但设计行为未变化时继续原 Run；用例、请求、断言、依赖、副作用、环境或执行范围变化时仍按原规则更新指纹并重新审核。
6. 决策状态只决定“是否还能自主推进”，不能批准设计、授权执行或放行高风险动作。

`plan_review` 通过后先计算 `executable = requested ∩ ready`，不能直接进入 `executing`；`rejected`/`plan_only`/`expired`/`superseded` 停在原地并输出 `review_not_executable`。0 executable 进入依赖解决且实际请求数固定为 0；非空 executable 才能申请独立 `execution_approval`。

- AI 生成设计后只能进入 `pending_review`，不得跳过审核进入 `executing`。
- `approved` 默认审核完整候选范围；`execution_case_ids` 单独表示本轮实际可执行子集。`partially_approved` 仍可表达用户主动缩小设计范围，但不能被技能用来掩盖单纯数据暂不可用；未执行候选保持 `planned_disposition=not_run`。
- 用例、请求摘要、断言、数据依赖、副作用、环境拓扑/URL/binding、缺口或执行范围变化时，原审核置为 `superseded`，新设计回到 `pending_review`。同一环境、同一 binding key 下的 Secret 值轮换只更新运行值，不改变设计指纹，不触发重审。
- 方案审核、执行授权与高风险动作审批分别校验，任一适用门禁不满足都不得执行对应用例。高风险 Token 不能替代 `execution_authorization`。
- 集合不变量固定为 `reviewed ⊆ candidate`、`requested ⊆ reviewed`、`executable = requested ∩ ready`、`authorized ⊆ executable`、`actually_executed ⊆ authorized`；显式空集合不得被默认值扩大。

v2 运行入口为 `scripts/run_orchestrator.py`。每次脚本调用只执行一个确定性动作，但主 Agent 在同一次执行中必须连续调度多个确定性动作，直到真实用户等待点、不可恢复阻断或终态；不得把“一个动作”解释为“一个用户回合”：

1. `compile_and_create_run()`：仅在不存在可复用的活动 Run 时编译并创建统一 Run。已是 `approved`/`partially_approved` 时直接用规范化后的审核创建可执行 Run；`pending_review` 时创建不可执行 Run 并返回设计审批请求；可执行但校验失败时拒绝创建。相同需求、环境和计划指纹已有未终结 Run 时禁止重建。
2. `record_review()`：把方案审核决定写回**同一 Run**（`pending_review` → `approved`/`partially_approved`/`rejected`…），推进到 feasibility 并追加 `review_recorded`。它同时保存完整 `reviewed_case_ids` 与 presence-sensitive 的 `execution_case_ids`，但清空旧执行授权；不得把审核范围回退成执行请求范围。
3. `record_execution_authorization()`：只在非空 `executable_case_ids` 上记录独立执行授权。授权必须绑定 plan、环境、预计请求数和副作用摘要的 `execution_scope_fingerprint`，并拒绝 blocked、未知、未请求或未就绪用例。等价授权可幂等返回，范围变化必须产生新指纹。
4. `next_action()`：读取 `state.json`、校验事件账本并返回下一步动作；不得在运行期重新排序任务。审批动作必须携带 `approval_kind=plan_review|execution_approval|high_risk_authorization`；三者不得互相替代。
4. `execute_slice()`：只执行计划指定的一个切片。执行成功后主 Agent立即再次调用 `next_action()`，普通 Slice 之间不询问用户。
5. `reduce_run()`：从计划、事件和资源重算 `RunResult`，结果文件存在且内容不同时拒绝覆盖。
6. `build_requirement_manifest()` / `build_requirement_manifest_for_runs()`：把单个/多个已归约 Run 投影为需求报告清单，最终仍由固定报告器渲染。

连续推进循环固定为：

```text
next_action
  -> execute_slice / execute_cleanup_first / replay_volatile_source / reduce
  -> next_action
  -> ...
  -> request_design_review / request_high_risk_authorization / request_runtime_input / blocked / complete
```

只有最后一行的真实等待点或终态才结束当前 Agent 执行。`blocked` 先按 `failure_class` 分类；缺风险授权、运行时绑定、Token 或 pending cleanup 不得使设计审核失效。

旧 TestPackage 管线保留为回退路径，不能在同一 Run 中混用两套调度状态。

所有会发送请求的设计都受人工审核门约束（唯一规则见 `SKILL.md`「测试设计人工审核门」）：先生成 TestPackage 和完整审核摘要，再进入 `pending_review`；只有审核状态可执行且指纹、用例范围匹配时才允许调度请求。

| 阶段 | 调度 | 条件 |
|---|---|---|
| 准备期 | `panda-l1-adapter-generator-v2` | 当前目标所需配置、画像或项目适配根目录缺失，先按需初始化、扫描和补齐；适配器只创建当前所需内容 |
| 设计期 | `panda-ai-api-tester-v2` | 按入口生成具体用例、请求摘要、断言、数据依赖、副作用和设计指纹 |
| `pending_review` | 主 Agent + 人工审核人 | 不发送请求；展示强制审核范围，收到决定后调用 `record_review()` |
| 差异确认期 | `panda-l1-adapter-generator-v2` + 责任人 | 影响当前执行的契约、环境、权限或风险范围发生变化；修改设计后重新审核 |
| `PROBE` | `panda-ai-api-tester-v2` | 对应 probe 用例已获审核且最小 HTTP/认证输入具备 |
| `EXECUTE` | `panda-ai-api-tester-v2` | 仅执行 `approved` 或 `partially_approved` 的具体用例；设计指纹/范围匹配且当前用例所需门禁满足 |
| 晋级 | `promote.py` | 用户明确要求，既有 EXECUTE 结果真实通过且无阻断 |

`PROMOTE` 是结果消费动作，不是 Runner 的业务执行模式。

### 4.1 审核契约

字段与别名以 `SKILL.md`「审核契约（字段唯一出处）」为准，本节只规定校验顺序。推荐写法：

```json
{
  "review": {
    "status": "pending_review|approved|partially_approved|rejected|plan_only|expired|superseded",
    "review_id": "REVIEW-...",
    "design_fingerprint": "sha256:...",
    "reviewer": "user-or-reviewer-id",
    "reviewed_at": "ISO8601",
    "approved_case_ids": ["TC-001"],
    "reviewed_fields": ["cases", "request_summaries", "assertions", "data_dependencies", "side_effects"],
    "comment": null,
    "expires_at": null
  }
}
```

执行器（`review_adapter.canonical_review` → L2 `validate_review`）按顺序 fail-closed 校验：

1. 方案审核状态只能为 `approved` 或 `partially_approved`；
2. 规范化后的方案指纹等于当前编译计划的 `plan_fingerprint`（别名解析后仍不等即视为设计已变）；
3. `reviewed_case_ids`/`approved_case_ids`/`approved_scope` 非空、均存在于当前计划；`execution_case_ids` 必须是审核范围子集且保持 presence-sensitive。`approved` 默认覆盖完整候选设计，`partially_approved` 只在用户明确缩小设计范围时使用；数据 blocked 不得强迫从审核范围移除；
4. `reviewed_fields` 能覆盖每个获准用例（数组形式自动扇出到全部获准用例）；
5. 审核未过期、未被新设计取代；
6. feasibility 计算后仅非空 executable 可申请执行授权；`authorized_case_ids` 必须是 executable 子集，授权指纹必须匹配；
7. 高风险动作仍有独立审批；
8. 未方案审核、未执行授权或未获高风险授权的用例保持 `not_run`，不能被调度器补跑或自动重试。

兼容性：字段别名映射已在本技能内实现（`scripts/review_adapter.py`），调用方不需要知道 L2 内部叫 `approved_scope`/`approved_at`。旧标记 `review_pending`/`approved_with_gaps` 不在支持范围内——它们丢失用例级范围，必须显式改写为 `pending_review` + `approved_case_ids` 后重新审核，不得仅改字符串放行。

## 5. TestPackage 绑定

每个新 TestPackage 必须使用 `schema_version = "2.1"` 并记录：

- `test_config.schema_version = "2.1"`
- `test_config.config_revision`
- `test_config.config_path`
- `test_config.environment_id`
- `test_config.environment_fingerprint`
- 接口画像 path、governance schema、profile ID/revision、code baseline、lifecycle、document fingerprint 与 contract fingerprint
- 需求 context 路径与引用
- `environment.runtime_bindings`，为 Base URL、认证、Origin 白名单和只读操作声明 `key/required/secret/format`

包内不得保存真实 URL、Token、数据库密码或连接串，只保存 RuntimeContext 绑定元数据。可信宿主根据这些绑定显式构造 RuntimeContext 2.0，并通过同进程 pipeline 传给 Runner。默认运行值由宿主内存提供；`persistent_secret` 可从 `.panda-api-test-agent/local/secrets/environments/<environment-id>.secret.json` 按当前 environment ID 和包已声明的 Secret binding 精确读取，`ephemeral_secret` 可从 `.panda-api-test-agent/local/runtime/secrets/{run_id}.secret.json` 的一次性 local Secret bridge 读取。持久 Store 不绑定单个 Run，不得被 Runner 删除；一次性 bridge 必须绑定当前 run ID、environment ID、包指纹和不超过 10 分钟的 TTL，只包含包已声明且标记为 Secret 的 binding，并由可信宿主原子认领、校验、删除后才构造 RuntimeContext，删除失败必须阻断。两者不得通过目录扫描、相近 environment ID、历史 Run 或旧文件回退。旧 `_env`/`_key` 字段、顶层 authentication、`os.environ` 回退、argv/stdin、任意其他路径文件均禁止。Runner 在创建 HTTP Executor 前必须完成 Schema、环境身份、必填值、Secret 分类、格式和设计审核校验。

Runner 禁止自动重试。连接中断、超时、5xx、写请求结果未知或断言失败都必须保留首次事实并停止当前用例；如需重试，创建新的具体动作/用例，评估幂等与重复副作用，生成新设计指纹并重新审核。

## 6. 环境切换

从一个环境切换到另一个环境时：

1. 选择目标环境完整节点。
2. 不复用源环境的凭据、DB、Redis、MQ、样本或运行结果。
3. 重新执行目标环境 PROBE。
4. PROBE 通过后才评估 EXECUTE。

环境切换不是项目配置漂移；只有声明或基础设施变化才触发 L1 增量维护。

## 7. 需求与报告

需求 AC、固定 Commit、专项接口、数据矩阵、SLA 和外部隔离要求来自需求 context，不从 `test-config.md` 推断。需求报告只纳入已校验 result 和明确人工判定，门禁/造数不计 AC 通过。

同一需求多个运行时：固定 AC 分母，映射到用例与 Oracle，分别计算执行触达率、完整场景覆盖率和验收通过率；缺陷与测试缺口分开。

生成路径固定为两步，不允许主 Agent 手工拼清单：

1. `build_requirement_manifest_for_runs([run_dir...], store_root=...)` 读取 `requirement_aggregation` 的 `current` 投影（含 `current_cases`、`defects`、`residual_risks`、`external_evidence`）与需求级外部证据账本，产出 `schema_version=1.0` 的报告清单；`report_id` 固定为 `AR-<requirement-id>-CURRENT`，同一需求只维护一个身份。
2. `refresh_requirement_report(...)` 完成校验、落盘和渲染；校验失败不得交付。等价于手工执行 `render_requirement_report.py <manifest> --output <report>`，但额外保证清单与报告同时更新、`release_recommendation` 与结论一致。

外部验证层证据只经 `external_evidence.py::record_external_evidence` 登记，登记后必须重算报告。接口层事实不得走该通道（`layer=api/http/run` 直接拒绝）；`passed/failed` 无具体引用直接拒绝。`partial` 缺少外部证据时保持 `inconclusive` 并列为缺口，不得因“用户说验证过了”而改写结论。

结论一致性：报告总体结论（PASS/BLOCKED/FAIL/INCONCLUSIVE）与清单里的 `release_recommendation` 必须由同一组 AC 事实推导。`BLOCKED` 由“存在被门禁阻塞且未执行的 AC”触发；只要有一条 `none` 或未执行 AC，结论就不会是 PASS，发布建议也不得写“可进入下一阶段”。

### 7.1 证据最低 schema（原始请求七要素）

business_execution 类证据的 `request` 必须包含完整原始请求七要素：`method / url / path / query / headers / json / requested_at`（`json` 可为 null，`query` 可为空对象）；多步证据（`steps`）中每个携带 `request` 的步骤按同一规则校验。`headers` 仅做结构校验、按原样记录，不做凭据遮蔽——凭据在运行产物中的保留策略以 `SKILL.md`「凭据与运行值」为准（按原样保留、不再遮蔽；仍不得写入项目配置、TestPackage 或接口画像等跨运行资产）。

校验时机为双重门：`slice_executor.execute_slice` 在任务通过后即时校验，违例任务置 `error`（事件带 `evidence_schema_errors`）；`reduce_run` 首次归约前对全部 passed 事实兜底校验，违例拒绝归约。校验实现唯一入口：`panda-ai-api-tester-v2/scripts/evidence_schema.py`。宿主自定义 task_executor 写证据同样受此约束——只有 `{method, path}` 的证据无法通过校验，报告因此永远无法闭合（2026-11-03 p919_2887 r1–r7 的根因）。

### 7.2 AC 级 verdict 独立判定

AC 的 verdict 只由该 AC scope 内用例的证据状态决定，并要求每条用例的证据文件通过 7.1 schema 校验；Run 级 verdict 不参与 AC 判定——Run 因无关候选 planned not_run 被判 inconclusive 时，证据齐全且通过校验的 direct AC 仍为 `passed`。Run verdict 保留为报告汇总字段。`generated_at` 取报告渲染时刻，不取 Run 开始时间。

### 7.3 settlement：业务任务终态结算

当 PRD 无删除能力、业务任务的成功执行即达成对象终态（如 `update-state` 停用即隔离）时，任务设计显式声明：

```json
{"settlement": {"on_success": true, "object_ref": "net_address:X", "as": "cleaned"}}
```

执行期：任务 passed 后由 `slice_executor` 自动结算该对象（写 `resource_settled` 事件），`cleanup_first` 门不再等待它。结算语义必须显式声明并经人工审核，禁止运行时按请求形态隐式推断。门等待 pending 对象时，`next_action` 返回 `guidance` 字段逐对象指明等待的 cleanup 任务或 settlement 声明，不得只报 `cleanup_first`。

## 8. 失败分类

- 旧 Schema/缺配置：先尝试按需生成或构造临时运行输入；仅当前目标依赖且无法补齐时阻塞相关项。
- Token 失效、连接失败、权限不足：仅阻塞依赖该环境或权限的用例，其他不相关 AC 可继续。
- 测试数据、查询入口、SLA 未验证：测试缺口。
- 有效环境和数据下违反契约：产品缺陷。
- 证据不足：inconclusive。

## 9. 用户反馈与阻断自诊断

每次反馈必须回答：现在能不能跑、卡在哪里、用户要做什么。最多呈现 3 个当前硬阻塞，并收敛为一个决策点。不得把配置生成、PROBE 成功、Mock、skip、collect 或部分执行称为需求通过。

任何阻断都必须先完成本地自诊断，再对外反馈；禁止只报“门禁不通过”、只抛原始异常或等待用户追问。准备期/设计期先调用通用证据闭环评估：若仍有安全动作则返回 `FACT_GATHERING`，若可受限推进则返回 `DEGRADED_READY`，只有生成有效 `stop_proof` 才能返回 `BLOCKED_USER`。执行期继续使用原门禁诊断，二者不得互相替代。诊断结果至少包含：

- `code`：稳定的机器可读原因码；
- `category`：`skill_state_sync`、`skill_integrity`、`skill_integration`、`review_gate`、`execution_binding`、`runtime_precondition` 或 `execution_environment`；
- `owner`：技能/宿主、L1/运行环境、审核人或用户；
- `severity` 与 `recoverable`：区分严重性和是否可以安全自动恢复；
- `evidence`：运行事实，请求/响应与业务值按原样记录，不做遮蔽；
- `impact`：阻塞当前阶段或哪些具体用例；
- `next_action`：唯一下一步，明确谁来做。

阻断后的固定处理顺序为：

```text
Gate/exception -> diagnose -> classify owner -> safe auto-recovery -> revalidate -> report
```

可自动恢复的范围仅限本地确定性动作：计算/校验指纹、规范化审核字段、构建 feasibility plan、补传编译器所需的显式参数和生成诊断报告。不得自动批准、猜测凭据/夹具/环境、使用旧配置回退或放行高风险副作用。自动恢复失败必须把原始异常和恢复结果一并写入诊断，保持 `execution_allowed=false`。

用户已在当前会话明确给出的决策必须被绑定到当前设计指纹并复用；若绑定失败，应归类为 `skill_state_sync`，不得再次询问同一个已回答问题。只有真正缺少用户/宿主输入时才追问，并把问题收敛为一个最小决策点。

## 10. 运行差异与画像复核闭环

当 AI Tester 返回 `contract_observations`：

1. 保留期望、实际、证据引用、候选分类、decider 和 next action；不得修改历史结果或画像。
2. 若环境、数据和执行证据不足，结论保持 `inconclusive` 或测试缺口，不得宣称产品缺陷或画像漂移。
3. 调度 L1 回查固定代码基线、当前部署版本和相关源码/文档。
4. 将路由/参数/错误码/鉴权/代码副作用交接口研发负责人确认，将纯业务语义交产品/业务负责人确认，将测试充分性和 Oracle 交测试负责人确认。
5. 确认画像变化时由 L1 增量更新、递增 profile revision、更新双指纹并使旧包失效；确认产品缺陷时保留画像并登记缺陷；环境版本不一致时按环境问题处理。
6. 重新进入执行前必须生成绑定新画像身份的 TestPackage。

## 11. 异常回退

- L1 失败：停留准备期；不得由主 Agent 或 L2 代写 `.panda-api-test-agent/adapter/`。
- 旧格式：明确报错并要求重建，不自动迁移。
- PROBE 失败：业务用例 `not_run`，报告实际请求数。
- 运行错误：保留已产生证据，不回写项目配置。
- 契约或配置指纹变化：阻断旧包，重新生成。
- 凭据仅经可信宿主从持久 Secret Store 或一次性 Run bridge 注入；不得写入 canonical 项目配置、TestPackage、接口画像或需求 context。运行产物（evidence、result、报告）的既有原样记录与展示策略保持不变。
