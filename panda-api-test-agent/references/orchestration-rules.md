# API Test Agent 编排规则（Schema 2.1）

主 Agent 只判断、调度和聚合，不创建项目配置、不发送正式请求、不回写运行状态。

## 1. 四层资产

| 层级 | 资产 | 职责 |
|---|---|---|
| 项目级 | `docs/test-config.md` | 多环境 HTTP、认证、DB、Redis、MQ、可观测性与安全策略 |
| 接口级 | `docs/interface-profile/*.md` | 路径、Schema、错误码、前置数据、副作用和契约指纹 |
| 需求级 | `.workbuddy/panda-api-test-agent/requirements/<id>/context.json` | PRD、代码基线、目标接口、AC、专项数据/风险/授权 |
| 运行级 | `runs/<run-id>/package.json`、`result.json`、evidence | 单次计划与不可变执行事实 |

组合关系：项目配置 + 接口画像 + 需求 context + 单次参数 = TestPackage。

## 2. 按需配置与局部门禁

进入运行前按当前测试目标检查最小必需集合，不要求无关资源全部就绪：

1. 优先检查 `docs/test-config.md`；不存在、旧格式或字段不完整时，调度 `panda-l1-adapter-generator` 从源码、部署配置、环境变量、用户输入和已有测试资产补齐当前目标所需节点。不得自动迁移或覆盖既有配置。
2. 当前 HTTP 请求至少需要可验证的环境身份、Base URL、认证变量、超时和响应大小限制；DB、Redis、MQ、日志、轨迹和外部推送只在当前 AC 需要时获取。
3. `environment_id` 必须可识别且不得是生产或疑似生产；无法确认环境安全时阻断，而不是用默认值猜测。
4. 所有实际使用资源必须来自同一环境节点，禁止 HTTP/Token/DB/Redis/MQ 跨环境拼接。
5. 接口画像按需检查：路由/方法、请求参数、响应/业务码、鉴权、副作用和当前用例需要的 Oracle 足够即可执行；完整 Owner、双指纹、清理和外部边界在对应正式 EXECUTE/PROMOTE 或资产沉淀时补齐。
6. 需求 context 缺失时，先由主 Agent 根据 PRD、用户输入和实际采用基线创建最小上下文；不得因无关 AC 或未使用资源缺失阻断当前可执行 AC。

配置或画像缺口必须映射到具体 `stage`、AC 或 case。只阻断依赖该缺口的项，其余项继续执行。临时 URL/Token 仅运行时注入，不写入配置。旧格式仍不能被消费或伪装为长期 Schema 2.1；如需继续当前测试，必须由 L1 依据可验证证据生成独立、可审计的临时 Schema 2.1 运行配置，不修改旧文件；正式资产由 L1 按需补齐。

## 3. 状态模型

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

正式 `EXECUTE` 增加设计审核门：先生成 TestPackage 和标题级预览，再进入 `REVIEW_PENDING`；只有审核状态为 `approved` 或 `approved_with_gaps` 且审核指纹匹配当前计划时，才允许调度业务执行。审核退回、仅保存计划、审核过期或计划变化时不得发送业务请求。`PROBE` 仍可用于环境、鉴权和只读探测。

| 阶段 | 调度 | 条件 |
|---|---|---|
| 准备期 | `panda-l1-adapter-generator` | 当前目标所需配置/画像缺失，先按需扫描和补齐 |
| 差异确认期 | `panda-l1-adapter-generator` + 责任人 | 影响当前执行的契约、环境、权限或风险范围发生变化 |
| `PROBE` | `panda-ai-api-tester` | 已有最小 HTTP/认证输入，需验证运行状态或受控探测 |
| `EXECUTE` | `panda-ai-api-tester` | 设计审核已通过且 plan fingerprint/审核范围匹配；当前用例所需门禁满足；普通非生产写入默认允许并记录副作用 |
| 晋级 | `promote.py` | 用户明确要求，既有 EXECUTE 结果真实通过且无阻断 |

`PROMOTE` 是结果消费动作，不是 Runner 的业务执行模式。

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

包内不得保存真实 URL、Token、数据库密码或连接串，只保存 RuntimeContext 绑定元数据。可信宿主根据这些绑定显式构造 RuntimeContext 2.0，并通过同进程 pipeline 传给 Runner。旧 `_env`/`_key` 字段、顶层 authentication、`os.environ` 回退以及从 argv/stdin/临时文件注入凭据均禁止。Runner 在创建 HTTP Executor 前必须完成 Schema、环境身份、必填值、Secret 分类和格式校验。

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

## 8. 失败分类

- 旧 Schema/缺配置：先尝试按需生成或构造临时运行输入；仅当前目标依赖且无法补齐时阻塞相关项。
- Token 失效、连接失败、权限不足：仅阻塞依赖该环境或权限的用例，其他不相关 AC 可继续。
- 测试数据、查询入口、SLA 未验证：测试缺口。
- 有效环境和数据下违反契约：产品缺陷。
- 证据不足：inconclusive。

## 9. 用户反馈

每次反馈必须回答：现在能不能跑、卡在哪里、用户要做什么。最多呈现 3 个当前硬阻塞，并收敛为一个决策点。不得把配置生成、PROBE 成功、Mock、skip、collect 或部分执行称为需求通过。

## 10. 运行差异与画像复核闭环

当 AI Tester 返回 `contract_observations`：

1. 保留期望、实际、证据引用、候选分类、decider 和 next action；不得修改历史结果或画像。
2. 若环境、数据和执行证据不足，结论保持 `inconclusive` 或测试缺口，不得宣称产品缺陷或画像漂移。
3. 调度 L1 回查固定代码基线、当前部署版本和相关源码/文档。
4. 将路由/参数/错误码/鉴权/代码副作用交接口研发负责人确认，将纯业务语义交产品/业务负责人确认，将测试充分性和 Oracle 交测试负责人确认。
5. 确认画像变化时由 L1 增量更新、递增 profile revision、更新双指纹并使旧包失效；确认产品缺陷时保留画像并登记缺陷；环境版本不一致时按环境问题处理。
6. 重新进入执行前必须生成绑定新画像身份的 TestPackage。

## 11. 异常回退

- L1 失败：停留准备期。
- 旧格式：明确报错并要求重建，不自动迁移。
- PROBE 失败：业务用例 `not_run`，报告实际请求数。
- 运行错误：保留已产生证据，不回写项目配置。
- 契约或配置指纹变化：阻断旧包，重新生成。
- 凭据永不回显、落盘或写入报告。
