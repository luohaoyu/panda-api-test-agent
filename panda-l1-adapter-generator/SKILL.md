---
name: panda-l1-adapter-generator
description: 扫描代码、部署与配置资产，创建或增量维护项目级多环境 test-config Schema 2.1 和接口画像。当用户要求接入新工程、重建项目测试配置、增加环境或复核配置漂移时使用。
agent_created: true
---

# L1 适配层生成器

## 定位

建立并维护项目级 L1 适配层：

- `docs/test-config.md`：项目级、多环境、长期复用的测试基础设施配置。
- `docs/interface-profile/*.md`：接口级契约、副作用和前置数据画像。

`test-config.md` 只描述项目如何连接和安全使用各测试环境，不承载 PRD、目标分支、固定 Commit、验收项、专项样本、需求风险、单次 Gate 或测试结果。

## 责任模型

- 项目测试负责人是 `test-config.md` 的 Owner，批准配置进入 `active`。
- 本 skill 是 `docs/test-config.md` 和 `docs/interface-profile/*.md` 的唯一技术写入者：负责创建草稿、增量更新、证据标注、revision/指纹维护和局部漂移标记，不替代责任人裁决。
- 接口研发负责人确认路由、参数、响应、错误码、鉴权和代码副作用；产品/业务负责人确认无法由代码唯一证明的业务语义；测试负责人确认画像是否足以支持测试设计、Oracle 和安全执行。
- 责任人提供确认结论，由本 skill 统一写入画像的确认记录；不得绕过本 skill 直接形成另一份可信画像。
- 找不到 Owner、关键契约事实仍为推断或目标环境安全性无法确认时，完整画像不得进入 `executable`；但可以生成 `discovered`/`drafted`/`minimally_usable` 的局部画像，支持当前目标所需的 PROBE 或受控 EXECUTE。普通非生产写操作的存在不再单独阻断画像执行；其对象、范围、数据标签、清理/残留处置必须进入当前运行包。
- `panda-api-test-agent` 只消费和调度；`panda-ai-api-tester` 与 Runner 只读画像、报告差异和验证运行状态；三者不得创建、修正或覆盖项目配置及接口画像。

## 何时使用

- 首次接入项目，创建 Schema 2.1 项目配置。
- 新增、修改或退役环境。
- HTTP、认证、DB、Redis、MQ、配置中心或可观测性发生变化。
- 主 Agent 检测到配置缺失、资源漂移或旧格式。
- 扫描或更新接口画像。

不用于生成需求上下文、测试用例、运行包、测试结果或真实凭据。

## Schema 与旧格式

唯一支持 `metadata.schema_version: "2.1"`；运行值仅通过 `runtime_bindings` 声明，旧 `_env` 字段不兼容。

发现旧版需求级 `test-config.md` 时：

1. 标记长期项目配置不可直接消费，不把旧格式当作 Schema 2.1。
2. 不兼容、不迁移、不猜测旧字段；先根据当前测试目标和可验证证据生成独立的临时 Schema 2.1 运行配置。
3. 未经明确授权不覆盖或删除旧文件；临时配置不得替代长期项目资产。
4. 临时配置满足最小运行门禁后，可以继续当前 PROBE/局部 EXECUTE；正式项目配置仍保持 `drafted` 或 `reconfirm_required`。

## 生命周期

配置级状态：`discovered`、`drafted`、`confirmed`、`verified`、`active`、`degraded`、`drifted`、`reconfirm_required`、`retired`。

画像生命周期：`discovered`、`drafted`、`minimally_usable`、`confirmed`、`executable`、`drifted`、`reconfirm_required`、`retired`。`minimally_usable` 表示已具备当前目标所需的路由、方法、参数、鉴权和基本响应事实，可支持受控 PROBE/EXECUTE，但不代表完整契约已确认；只有关键契约事实、Owner 和目标执行所需边界已确认的画像才能进入 `executable`。普通非生产写副作用不单独阻断 `minimally_usable`，必须在运行包记录对象、范围、数据标签和清理/残留处置。

运行级状态：`unknown`、`unverified`、`verified`、`reachable`、`authentication_failed`、`connection_failed`、`permission_denied`、`unavailable`。

必须分离配置状态、画像生命周期与运行状态。运行失败不能自动改写前两者；Token 失效只影响对应环境的认证运行状态。运行发现契约差异时保留证据并请求复核，不得直接改画像。

## 核心流程

1. 识别项目 ID、工作区、模块、语言、框架和构建边界。
2. 检查 `docs/test-config.md`：缺失、旧格式或字段不完整时，先按当前测试目标创建最小草稿或形成不落盘的临时运行输入；不得覆盖既有文件。Schema 2.1 配置则增量扫描。
3. 按需扫描项目通用约定、环境清单、HTTP、认证和当前目标所需的 Oracle；DB、Redis、MQ、配置中心、日志、轨迹和可观测性仅在当前 AC 或运行包需要时获取。
4. 按 `references/scan-mapping.md` 记录来源、证据等级、Owner、状态和变化范围。
5. 使用 `references/config-template.md` 创建或局部更新配置；未变化且已确认的节点保持原状态。
6. 只将受影响节点标为 `drifted` 或 `reconfirm_required`，递增 `config_revision`。
7. 按 `references/profile-template.md` 独立生成或增量更新 `docs/interface-profile/*.md`，维护治理块、Owner、确认记录、画像 revision、生命周期和双指纹；接口生命周期不得替代项目配置生命周期。
8. 只有契约语义变化才递增 `profile_revision` 和 `contract_fingerprint`；任何画像文件内容变化都更新 `document_fingerprint`。纯排版或说明文字变化不得伪造契约漂移。
9. 解析用户提供的分支、Tag、Commit 或版本号：先查本地/远程 ref、相近命名、提交对象和基线文件；优先使用隔离 worktree 或只读对象检查，不切换、重置或覆盖用户工作区；记录实际采用基线与证据。
10. 输出阻塞当前阶段、阻塞后续阶段和仅备查三层缺口；每个缺口必须绑定具体配置项、画像字段、AC 或 case，不得把局部缺口扩大为全局阻断。

## 配置边界

`test-config.md` 可以保存：

- 项目 ID、模块、通用 API Prefix、响应包络和成功规则。
- 多环境 HTTP、认证、DB、Redis、MQ、配置中心、日志和轨迹查询的 RuntimeContext binding 元数据。
- 项目通用安全策略、Owner、证据和状态。

禁止保存：

- PRD、需求 ID、需求标题、验收项。
- 目标分支、固定 Commit、专项接口清单。
- 运单、任务或单次运行样本。
- 需求专项授权、风险、Gate、用例和结果。
- Token、Cookie、密码、连接串或 Secret 的真实值。

需求信息应进入 `.workbuddy/panda-api-test-agent/requirements/<requirement-id>/context.json`；单次执行信息应进入运行级 `package.json`。

## 增量更新规则

项目配置触发更新：环境增删改、域名/Gateway/API Prefix、认证机制、DB/Redis/MQ 拓扑、变量名、Secret 引用、可观测性入口、响应包络或安全规则变化。

接口画像触发复核：Controller/Router 路径或方法、DTO/字段/校验注解、响应结构、异常映射或业务错误码、鉴权注解或权限逻辑、Service 调用链、DB/Redis/MQ/外部调用副作用、清理入口、异步轮询和最终一致性条件发生变化。只更新受影响接口；未变化且已确认的事实保持原状态。

运行差异不直接触发画像改写：先记录期望、实际、证据、候选分类和确认人，回查代码基线与部署版本；只有确认画像事实变化后才由本 skill 增量更新。产品缺陷、环境版本不一致和证据不足不得伪装成画像更新。

不触发更新：新 PRD、需求状态、测试分支、固定 Commit、样本变化、单条用例失败、Token 值轮换、单次请求数量或验收结论变化。

## 待确认项

每项必须包含：`id`、`block_stage`、`severity`、`impact`、`decider`、`unlocks`，可选 `evidence`。按「阻塞当前阶段 / 阻塞后续阶段 / 仅备查」分层。资源确认人必须匹配字段类型。

## 纪律

- 不编造扫描不到的信息；使用 `drafted`、`unverified` 或待确认。
- 敏感值不得写入配置；只记录 RuntimeContext binding 的 `key/required/secret/format` 元数据。
- DB 和 Redis 默认只读且只允许测试资源；只有当前 AC 明确需要时才获取连接或断言配置。
- 不把源码存在当作环境已部署，不把文件生成当作配置已验证。
- 不把局部漂移、缺失的无关资源或单个画像字段扩大为整个项目失效。
- 普通非生产业务写入、日志、轨迹和异步消息默认可作为测试副作用；记录精确对象、数据标签、清理状态和残留风险。删除、破坏性、批量或疑似生产动作必须单独确认。
- 不生成需求专项 `test-config.md`。

## 完成判定

- `docs/test-config.md` 符合 Schema 2.1 项目级多环境模板。
- 至少生成一个接口画像，或明确记录未发现路由。
- 配置包含 Owner、`config_revision`、资源级状态和安全规则。
- 每个画像包含唯一治理块、Owner、`profile_revision`、代码基线、确认记录、生命周期、副作用/清理边界、`document_fingerprint` 和 `contract_fingerprint`。
- 敏感字段全部为变量引用。
- 缺口已结构化和分层。
- 结果明确为草稿或增量维护结果，不宣称测试已通过。

## 参考资料

- 项目配置模板：`references/config-template.md`
- 扫描映射：`references/scan-mapping.md`
- 接口画像模板：`references/profile-template.md`
