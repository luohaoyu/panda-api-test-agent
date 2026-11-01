# Schema 2.1 扫描源与字段映射

## 1. 项目级 `test-config.md`

| 扫描对象 | 建议来源 | 配置落点 | 默认确认人 |
|---|---|---|---|
| 项目标识与模块 | 构建文件、workspace、应用入口 | `metadata.project_id`、`project.modules` | 研发负责人 |
| API Prefix/响应包络 | Web 配置、响应类 | `project.*` | 后端研发 |
| 环境清单 | `application-*`、部署描述、CI/CD、环境文档 | `environments.<id>` | 环境负责人 |
| HTTP/Gateway/Proxy/TLS | 部署配置、网关说明、变量示例 | `environments.<id>.http` | 环境负责人 |
| 认证机制 | Security/Filter/Interceptor/登录入口 | `authentication` | 研发/安全负责人 |
| HTTP/认证运行绑定 | Secret key、部署模板 | `environments.<id>.runtime_bindings` | 环境负责人 |
| DB | datasource、部署变量 | `environments.<id>.db` | DBA |
| Redis | redis 配置、部署变量 | `environments.<id>.redis` | 中间件负责人 |
| MQ/配置中心 | 客户端与部署配置 | `mq`、`config_center` | 中间件负责人 |
| 日志/轨迹入口 | 运维文档、配置 | `observability` | 可观测性负责人 |
| 安全策略 | 项目规则、测试规范 | `safety` | 测试/安全负责人 |

只保存 RuntimeContext binding 元数据（`key/required/secret/format`），不复制值，不生成 `_env` 字段。无法证明环境为非生产时，保持 `drafted` 并形成 `probe/hard` 缺口。

## 2. 禁止写入项目配置的扫描结果

以下信息即使扫描到也不得进入 `test-config.md`：

- PRD、需求 ID、需求标题和验收项。
- 目标发布分支、固定 Commit。
- 需求目标接口、专项调用链、专项错误码。
- 运单、任务、用户或其他单次样本。
- 需求专项授权、风险、SLA、Gate、用例和结果。

这些内容分别进入需求 `context.json`、`interface-profile` 或运行 `package.json`。

## 3. 接口画像

| 扫描对象 | 来源 | 画像字段 |
|---|---|---|
| 路由 | Controller/Router | path、method |
| 入参与校验 | DTO、Validator | request schema、required params |
| 响应与业务码 | 包装类、枚举、异常 | response schema、success/error codes |
| 接口鉴权 | 注解和安全规则 | auth requirement |
| 前置数据与副作用 | Service/DAO/MQ/外部调用 | pre-data、side-effect |
| Owner 与确认范围 | CODEOWNERS、模块责任说明、人工确认 | owners、confirmation |
| 副作用与清理 | Service/DAO/MQ/外部调用、清理入口 | side-effect、cleanup、async consistency |
| 文档指纹 | 完整画像 Markdown（fingerprint 值规范化后） | document_fingerprint |
| 契约指纹 | 规范化契约事实 | contract_fingerprint |

接口画像生命周期独立于项目配置生命周期。L1 是画像唯一技术写入者；责任人确认事实，主 Agent、AI Tester 和 Runner 只读。

## 4. 增量与漂移

1. 先读取 Schema 2.1 配置与画像治理块，比较字段级差异及代码基线；旧 `_env` 配置不得继续消费。
2. 未变化且已确认的配置节点和画像事实保持原状态。
3. 配置声明或拓扑变化时，只将对应配置节点标记 `drifted` 或 `reconfirm_required`，递增 `metadata.config_revision`。
4. 路由、方法、DTO/校验、响应、错误码、鉴权、Service 调用链、副作用、清理或异步收敛变化时，只将受影响画像标记 `drifted` 或 `reconfirm_required`。
5. 契约语义变化时递增 `profile_revision`，更新 `contract_fingerprint` 与 `document_fingerprint`；纯排版/说明变化只更新 `document_fingerprint`。
6. Token 值过期、网络临时失败等仅属于 `runtime_status`，不构成配置或画像漂移。
7. 单条运行结果与画像不一致时先形成差异观察，回查代码基线、部署版本和责任人结论；不得直接用实际响应覆盖画像。
8. 新需求、分支、样本和运行参数不触发项目配置或画像更新。

## 5. 证据规则

- `high`：用户/责任人明确确认、源码、Git、部署配置或真实探测。
- `medium`：多项静态证据一致但未探测。
- `low`：推断或待确认。

始终区分：发现不等于确认，源码存在不等于环境部署，配置验证不等于业务测试通过，运行差异不等于画像已经错误。

## 6. 画像准入

- `discovered|drafted`：允许继续扫描、设计缺口和生成局部配置，不允许完整 EXECUTE/PROMOTE。
- `minimally_usable`：当前目标所需的路由、方法、参数、鉴权和基本响应事实已具备；允许受控 PROBE 或局部 EXECUTE，不代表完整画像治理通过。
- `confirmed`：已完成指定范围确认；仍需检查当前执行所需 Owner、边界和指纹后才能转为完整 `executable`。
- `executable`：允许 AI Tester 生成绑定该 revision 与双指纹的完整 TestPackage。
- `drifted|reconfirm_required|retired`：阻断新 EXECUTE/PROMOTE，并使旧包失效。
- 写操作不存在可靠清理能力时不得因画像声明而放行；单次 cleanup waiver 只属于 TestPackage 风险例外。

## 7. 建议扫描入口


- 配置：`application*.yml|yaml|properties`、`.env.example`、`config/**`。
- Java：Controller、Service、Repository、Config、Filter、Interceptor。
- Node/Python：routes、controllers、services、middleware、settings。
- 部署：Docker、Compose、CI/CD、Helm/Kubernetes（若存在）。

模式只是入口；未命中必须形成结构化缺口，不得用默认值补齐。
