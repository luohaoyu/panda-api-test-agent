# 扫描源 → 字段映射表

本文件规定 L1 适配层生成器的扫描范围、证据来源和字段落点。扫描结果应区分“命中”“未命中”和“待确认”，不得用默认值掩盖缺口。

## 生成 `test-config.md` 的扫描映射

| 扫什么 | 在哪扫 | 得到字段 |
|---|---|---|
| 应用配置 | `application*.yml/properties`、`.env.example` | `base_url`、`timeout`、`gateway` |
| 数据源配置 | datasource 段 | `db.type`、`db.host`、`db.port`、`db.database` |
| 缓存配置 | redis 段 | `redis.host`、`redis.port`、`redis.db` |
| 鉴权实现 | `Filter`/`Interceptor`/`SecurityConfig`/`@PreAuthorize` | `auth.type` |
| 登录接口 | `LoginController` 及入出参 | `login-flow`（token 取哪个字段） |
| 签名实现 | 签名工具类 | `sign-config` 骨架（密钥写 `${SIGN_SECRET}`） |
| 部署编排 | `docker-compose.yml`、CI 配置 | 环境拓扑、多环境差异 |
| 路由依赖 | Controller 间服务调用 | 链路骨架 |

## 生成 `interface-profile` 的扫描映射

| 扫什么 | 在哪扫 | 得到字段 |
|---|---|---|
| 路由定义 | `@RequestMapping`/`@GetMapping`/`@PostMapping` | `path`、`method` |
| 入参结构 | DTO/`@RequestBody`/`@RequestParam` | `required-params`、request schema |
| 参数校验 | `@NotNull`/`@Valid`/JSR303 | 参数约束（来源=代码） |
| 出参结构 | 响应包装类（如 `Result<T>`） | response schema |
| 业务码 | code 常量类/枚举/异常类 | `success-code`、`error-codes` |
| 鉴权范围 | 接口上的鉴权注解 | `auth`（继承或覆盖） |
| 前置数据 | 接口依赖的 Service/DAO 调用 | `pre-data` 草稿（待验证） |

## 扫描优先级

1. 先扫 Controller 层或路由入口，建立接口清单和模块边界。
2. 再追 Service/DAO，补充链路依赖、前置数据和响应来源。
3. 最后补应用配置、数据源、缓存、鉴权、部署和多环境差异。

## 证据与缺口记录规则

- **代码**：直接来自注解、类、方法、字段、常量、校验器或调用关系。
- **文档**：来自项目内 API 文档、部署说明、配置说明或接口契约。
- **推断**：由可见证据推导出的初步结构；必须同时保留推断依据，并标为“待确认”。
- **未命中**：扫描范围内没有找到可支持该字段的证据；字段值写“待确认”，汇总报告记录扫描位置。
- **敏感信息**：密码、密钥、token、账号和连接凭据不得复制；只保留变量名，例如 `${DB_PASSWORD}`、`${SIGN_SECRET}`。
- **环境信息**：无法确认是否为测试环境时，写“环境用途待确认”；数据库章节始终注明“只连测试库”。

## 建议扫描文件模式

以下模式用于建立初始扫描范围，实际以项目语言和框架为准：

- 配置：`application*.yml`、`application*.yaml`、`application*.properties`、`.env.example`、`config/*`。
- Java：`*Controller.java`、`*Service.java`、`*Repository.java`、`*Dao.java`、`*Config.java`、`*Filter.java`、`*Interceptor.java`。
- Node.js：`routes/**`、`controllers/**`、`services/**`、`middleware/**`、`config/**`、`app.*`、`server.*`。
- Python：`routers/**`、`views/**`、`controllers/**`、`services/**`、`middleware/**`、`settings.*`、`main.*`。
- 部署：`docker-compose*.yml`、`Dockerfile*`、`.github/workflows/**`、`.gitlab-ci.yml`、`Jenkinsfile`。

模式只是检索入口，不代表文件一定存在。对不存在、不可读或未命中的模式必须形成缺口记录。
