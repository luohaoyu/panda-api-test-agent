# API Test Agent 编排规则（Schema 2.1）

主 Agent 只判断、调度和聚合，不创建项目配置、不发送正式请求、不回写运行状态。`intent_router.py` 只做四类入口的本地设计路由；`single_api_quality.py`（L2）只做单接口结构化设计。二者都不读取 secret、不发请求；任何实际执行必须经过人工审核和 L2 trusted host/Runner。用户已提供 curl 时可直接进入设计输入，不强制扫描工程。

依赖布局：本技能与 `panda-ai-api-tester-v2`、`panda-report-theme` 必须平级安装在同一个 skills 目录下，脚本按相对位置解析，缺失即 fail-closed 报错。

## 1. 四层资产

| 层级 | 资产 | 职责 |
|---|---|---|
| 项目级 | `.panda-api-test-agent/test-config.md`、`.panda-api-test-agent/adapter/` | 多环境 HTTP、认证、DB、Redis、MQ、可观测性、安全策略和项目适配规则 |
| 接口级 | `.panda-api-test-agent/interface-profile/*.md` | 路径、Schema、错误码、前置数据、副作用和契约指纹 |
| 需求级 | `.panda-api-test-agent/requirements/<id>/context.json` | PRD、代码基线、目标接口、AC、专项数据/风险/授权 |
| 运行级 | `.panda-api-test-agent/runs/<run-id>/package.json`、`result.json`、evidence | 单次计划与不可变执行事实 |

组合关系：项目配置 + 接口画像 + 需求 context + 单次参数 = TestPackage。

## 2. 按需配置与局部门禁

进入运行前按当前测试目标检查最小必需集合，不要求无关资源全部就绪：

1. 优先检查 `.panda-api-test-agent/test-config.md` 和 `.panda-api-test-agent/adapter/`；配置不存在、旧格式或字段不完整，或适配根目录缺失时，调度 `panda-l1-adapter-generator-v2` 从源码、部署配置、环境变量、用户输入和已有测试资产按当前目标初始化或增量维护。首次只创建 `.panda-api-test-agent/adapter/` 根目录及当前目标必需内容，不预创建无关子目录；不得自动迁移或覆盖既有配置。
2. 当前 HTTP 请求至少需要可验证的环境身份、Base URL、认证变量、超时和响应大小限制；DB、Redis、MQ、日志、轨迹和外部推送只在当前 AC 需要时获取。
3. `environment_id` 必须可识别且不得是生产或疑似生产；无法确认环境安全时阻断，而不是用默认值猜测。
4. 所有实际使用资源必须来自同一环境节点，禁止 HTTP/Token/DB/Redis/MQ 跨环境拼接。
5. 接口画像按需检查：路由/方法、请求参数、响应/业务码、鉴权、副作用和当前用例需要的 Oracle 足够即可执行；完整 Owner、双指纹、清理和外部边界在对应正式 EXECUTE/PROMOTE 或资产沉淀时补齐。
6. 需求 context 缺失时，先由主 Agent 根据 PRD、用户输入和实际采用基线创建最小上下文；不得因无关 AC 或未使用资源缺失阻断当前可执行 AC。

配置或画像缺口必须映射到具体 `stage`、AC 或 case。只阻断依赖该缺口的项，其余项继续执行。临时 URL/Token 仅运行时注入，不写入配置。旧格式仍不能被消费或伪装为长期 Schema 2.1；如需继续当前测试，必须由 L1 依据可验证证据生成独立、可审计的临时 Schema 2.1 运行配置，不修改旧文件；正式资产由 L1 按需补齐。

## 2.1 AI 与脚本职责边界

- AI/Skill 负责自然语言入口理解、工程按需扫描、业务语义解释、需求可验证性判断、测试维度选择和结果解释；项目适配层内容由 L1 负责初始化和维护，主 Agent 只负责判断和调度。
- L1 脚本只提供可选的固定格式候选发现；其输出必须标记为候选，不等于已部署、已确认或可执行事实。
- L2 设计脚本只结构化 AI 已作出的语义选择；无 AI 决策时，候选设计必须停在 `pending_ai_decision`，不得进入可执行范围。
- 审核、凭据隔离、请求执行、请求次数、脱敏、指纹、结果统计和报告生成必须由确定性脚本兜底。

## 3. 状态模型

四类入口共用两套明确分工的模型，不要混用：

- **入口状态**（`scripts/entry_state.py`）：`entry_type`、`phase`、`review_status`、`approved_case_ids`、`design_fingerprint`、`plan_fingerprint`、`blocking_gap_ids`、`last_action`、`next_action`、`request_count_expected`、`request_count_sent`、`failure_class`、`verdict` 的规范化与校验。
- **运行状态**（L2 `run_state` 持久化）：`status`（`ready`/`awaiting_approval`/`blocked`/`interrupted`/`reduced`/`reported`）、`review_status`、`completed_slices`、`pending_cleanup` 等实际落盘字段。

两者关系：`phase` 只描述流程位置，**执行许可由 `review_status` 决定**。因此 `pending_review → executing` 这个 phase 跃迁在状态机上是允许的，但同一时刻若 `review_status` 不是 `approved`/`partially_approved`，执行器一律 fail-closed 阻断（`slice_blocked_unreviewed`）。不存在“phase 到了 executing 就能跑”的情况。

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
- 普通非生产 `write`、日志、轨迹和异步消息属于默认可执行副作用；`destructive`、批量、真实外部动作、删除/清理已有数据仍需确认。
- 无法清理不再自动阻断普通单次业务写入；必须记录精确对象、数据标签、残留风险和清理状态。只有 PROMOTE、批量回归或需要“完整通过”结论时，可靠 teardown 才是强制门禁。

## 4. 阶段与调度

所有入口先进入以下流程：

```text
intent_received -> preparing -> designing -> pending_review -(审核决定)-> executing -> reducing -> reported
```

`pending_review` 之后的括号不是 phase，而是 `review_status`：`approved`/`partially_approved` 才能进入 `executing`；`rejected`/`plan_only`/`expired`/`superseded` 停在原地并输出 `review_not_executable`。

- AI 生成设计后只能进入 `pending_review`，不得跳过审核进入 `executing`。
- `approved` 允许执行审核范围内全部具体用例；`partially_approved` 只允许执行 `approved_case_ids`，其余保持 `not_run`。
- 用例、请求摘要、断言、数据依赖、副作用、环境、缺口或执行范围变化时，原审核置为 `superseded`，新设计回到 `pending_review`。
- 设计审核与高风险动作审批分别校验，任一不满足都不得执行对应用例。

v2 运行入口为 `scripts/run_orchestrator.py`，主 Agent 每回合只执行一个确定性动作：

1. `compile_and_create_run()`：编译并创建统一 Run。已是 `approved`/`partially_approved` 时直接用规范化后的审核创建可执行 Run；`pending_review` 时创建不可执行 Run 并返回 `request_approval`；可执行但校验失败时拒绝创建。
2. `record_review()`：把人工决定写回**同一 Run**（`pending_review` → `approved`/`partially_approved`/`rejected`…），推进 `review_status`/`status`/`next_action` 并追加 `review_recorded` 事件。fail-closed 拒绝：已 `reduced`/`reported` 的 Run、指纹不匹配、范围含未知用例、范围小于已执行用例。
3. `next_action()`：读取 `state.json`、校验事件账本并返回下一步动作；不得在运行期重新排序任务。
4. `execute_slice()`：只执行计划指定的一个切片。
5. `reduce_run()`：从计划、事件和资源重算 `RunResult`，结果文件存在且内容不同时拒绝覆盖。
6. `build_requirement_manifest()` / `build_requirement_manifest_for_runs()`：把单个/多个已归约 Run 投影为需求报告清单，最终仍由固定报告器渲染。

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

1. 状态只能为 `approved` 或 `partially_approved`；
2. 规范化后的指纹等于当前编译计划的 `plan_fingerprint`（别名解析后仍不等即视为设计已变）；
3. `approved_case_ids`/`approved_scope` 非空、均存在于当前计划，且 `approved` 覆盖全部可执行用例、`partially_approved` 为严格子集；
4. `reviewed_fields` 能覆盖每个获准用例（数组形式自动扇出到全部获准用例）；
5. 审核未过期、未被新设计取代；
6. 高风险动作仍有独立审批；
7. 未批准用例保持 `not_run`，不能被调度器补跑或自动重试。

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

包内不得保存真实 URL、Token、数据库密码或连接串，只保存 RuntimeContext 绑定元数据。可信宿主根据这些绑定显式构造 RuntimeContext 2.0，并通过同进程 pipeline 传给 Runner。默认运行值由宿主内存提供；唯一文件例外是 `.panda-api-test-agent/local/runtime/secrets/{run_id}.secret.json` 的一次性 local Secret bridge。它必须绑定当前 run ID、environment ID、包指纹和不超过 10 分钟的 TTL，只包含包已声明且标记为 Secret 的 binding，并由可信宿主原子认领、校验、删除后才构造 RuntimeContext；删除失败必须阻断。旧 `_env`/`_key` 字段、顶层 authentication、`os.environ` 回退、argv/stdin、任意路径文件、目录扫描或历史文件回退均禁止。Runner 在创建 HTTP Executor 前必须完成 Schema、环境身份、必填值、Secret 分类、格式和设计审核校验。

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

1. `build_requirement_manifest_for_runs([run_dir...])` 读取 `requirement_aggregation` 的 `current` 投影（含 `current_cases`、`defects`、`residual_risks`、`external_evidence`），产出 `schema_version=1.0` 的报告清单；`report_id` 固定为 `AR-<requirement-id>-CURRENT`，同一需求只维护一个身份。
2. `render_requirement_report.py <manifest> --output <report>` 确定性渲染，校验失败不得交付。

结论一致性：报告总体结论（PASS/BLOCKED/FAIL/INCONCLUSIVE）与清单里的 `release_recommendation` 必须由同一组 AC 事实推导。`BLOCKED` 由“存在被门禁阻塞且未执行的 AC”触发；只要有一条 `none` 或未执行 AC，结论就不会是 PASS，发布建议也不得写“可进入下一阶段”。

## 8. 失败分类

- 旧 Schema/缺配置：先尝试按需生成或构造临时运行输入；仅当前目标依赖且无法补齐时阻塞相关项。
- Token 失效、连接失败、权限不足：仅阻塞依赖该环境或权限的用例，其他不相关 AC 可继续。
- 测试数据、查询入口、SLA 未验证：测试缺口。
- 有效环境和数据下违反契约：产品缺陷。
- 证据不足：inconclusive。

## 9. 用户反馈与阻断自诊断

每次反馈必须回答：现在能不能跑、卡在哪里、用户要做什么。最多呈现 3 个当前硬阻塞，并收敛为一个决策点。不得把配置生成、PROBE 成功、Mock、skip、collect 或部分执行称为需求通过。

任何阻断都必须先完成本地自诊断，再对外反馈；禁止只报“门禁不通过”、只抛原始异常或等待用户追问。诊断结果至少包含：

- `code`：稳定的机器可读原因码；
- `category`：`skill_state_sync`、`skill_integrity`、`skill_integration`、`review_gate`、`execution_binding`、`runtime_precondition` 或 `execution_environment`；
- `owner`：技能/宿主、L1/运行环境、审核人或用户；
- `severity` 与 `recoverable`：区分严重性和是否可以安全自动恢复；
- `evidence`：脱敏事实，不得包含 Token、密码、真实订单号或完整敏感地址；
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
- 凭据永不回显、落盘或写入报告。
