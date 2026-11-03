# 项目接口测试配置模板（Schema 2.1）

> 作用域：项目级。一个项目只维护一份 canonical `docs/test-config.md`，统一描述多套测试环境；不得创建需求级、并行或“独立临时 Schema 配置”。禁止写入需求、分支、Commit、样本、用例、结果或真实凭据。

## 1. 机器配置块

配置消费者必须读取下面唯一的 `yaml test-config` 代码块；其余文字仅用于说明。字段未知时使用 `drafted`、`unverified`、空数组或 `${VAR}`，禁止猜值。`secret: false` binding 可保存经确认、稳定且非敏感的环境 `value`；`secret: true` binding 禁止 `value`，只保存 binding 元数据和可选 `secret_ref`。

```yaml test-config
metadata:
  schema_version: "2.1"
  project_id: ${PROJECT_ID}
  config_revision: 1
  status: drafted
  created_at: ${CREATED_AT}
  updated_at: ${UPDATED_AT}
  change_reason: initial_discovery
  owners:
    primary: ${PROJECT_TEST_OWNER}
    backup: ${PROJECT_BACKUP_OWNER}

project:
  modules: []
  api_prefix: ${API_PREFIX}
  response_envelope: ${RESPONSE_ENVELOPE}
  business_success_rule:
    field: ${BUSINESS_CODE_FIELD}
    expected: ${BUSINESS_SUCCESS_CODE}

authentication:
  type: bearer
  header: Authorization
  value_template: "Bearer ${TOKEN}"
  probe:
    method: GET
    path: ${AUTH_PROBE_PATH}

environments:
  ${ENVIRONMENT_ID}:
    status: drafted
    production: false
    owner: ${ENVIRONMENT_OWNER}

    runtime_bindings:
      base_url:
        key: ${ENV_BASE_URL_KEY}
        required: true
        secret: false
        format: url
        value: ${STABLE_ENV_BASE_URL}
      allowed_origins:
        key: ${ENV_ALLOWED_ORIGINS_KEY}
        required: true
        secret: false
        format: origin_list
      read_only_operations:
        key: ${ENV_READ_ONLY_OPERATIONS_KEY}
        required: false
        secret: false
        format: operation_list
      auth:
        type: bearer
        header: Authorization
        credential:
          key: ${ENV_AUTH_KEY}
          required: true
          secret: true
          format: string
          secret_ref: environments/${ENVIRONMENT_ID}/auth

    http:
      timeout_seconds: 30
      verify_tls: true
      config_status: drafted
      runtime_status: unknown
      last_verified_at: null
      evidence_ref: null

    db:
      runtime_bindings:
        enabled:
          key: ${ENV_DB_ENABLED_KEY}
          required: false
          secret: false
          format: string
        host:
          key: ${ENV_DB_HOST_KEY}
          required: false
          secret: false
          format: string
        port:
          key: ${ENV_DB_PORT_KEY}
          required: false
          secret: false
          format: string
        database:
          key: ${ENV_DB_NAME_KEY}
          required: false
          secret: false
          format: string
        username:
          key: ${ENV_DB_USER_KEY}
          required: false
          secret: true
          format: string
        password:
          key: ${ENV_DB_PASSWORD_KEY}
          required: false
          secret: true
          format: string
      driver: ${DB_DRIVER}
      readonly: true
      connect_timeout_seconds: 10
      query_timeout_seconds: 10
      allowed_schemas: []
      config_status: drafted
      runtime_status: unknown

    redis:
      runtime_bindings:
        enabled:
          key: ${ENV_REDIS_ENABLED_KEY}
          required: false
          secret: false
          format: string
        host:
          key: ${ENV_REDIS_HOST_KEY}
          required: false
          secret: false
          format: string
        port:
          key: ${ENV_REDIS_PORT_KEY}
          required: false
          secret: false
          format: string
        database:
          key: ${ENV_REDIS_DB_KEY}
          required: false
          secret: false
          format: string
        username:
          key: ${ENV_REDIS_USER_KEY}
          required: false
          secret: true
          format: string
        password:
          key: ${ENV_REDIS_PASSWORD_KEY}
          required: false
          secret: true
          format: string
      mode: standalone
      tls: false
      readonly: true
      allowed_key_prefixes: []
      config_status: drafted
      runtime_status: unknown

    mq:
      runtime_bindings:
        enabled:
          key: ${ENV_MQ_ENABLED_KEY}
          required: false
          secret: false
          format: string
        profile:
          key: ${ENV_MQ_PROFILE_KEY}
          required: false
          secret: true
          format: string
      readonly_observation: true
      config_status: drafted
      runtime_status: unknown

    config_center:
      runtime_bindings:
        enabled:
          key: ${ENV_CONFIG_CENTER_ENABLED_KEY}
          required: false
          secret: false
          format: string
        profile:
          key: ${ENV_CONFIG_PROFILE_KEY}
          required: false
          secret: true
          format: string
      readonly: true
      config_status: drafted
      runtime_status: unknown

    observability:
      runtime_bindings:
        log_query_url:
          key: ${ENV_LOG_QUERY_URL_KEY}
          required: false
          secret: false
          format: url
        trace_query_url:
          key: ${ENV_TRACE_QUERY_URL_KEY}
          required: false
          secret: false
          format: url
        operation_log_query_url:
          key: ${ENV_OPERATION_LOG_QUERY_URL_KEY}
          required: false
          secret: false
          format: url
      default_polling:
        interval_seconds: 5
        timeout_seconds: 60
      config_status: drafted
      runtime_status: unknown

safety:
  production_execution: forbidden
  cross_environment_resource_mix: forbidden
  credentials_persistence: forbidden
  runtime_value_sources: host_memory_only
  request_header_logging: redacted
  db_default_mode: readonly
  redis_default_mode: readonly
  write_retry: forbidden
  destructive_operation: forbidden

pending_items: []
```

## 2. Runtime Binding 规则

每个运行时绑定必须声明：

- `key`：可信宿主构造 RuntimeContext 时使用的唯一键；
- `required`：当前资源是否必须提供；
- `secret`：是否属于敏感值；
- `format`：`string|url|origin_list|operation_list`；
- `value`：仅允许在 `secret: false` 时保存经证据确认、跨运行稳定的非敏感环境值；
- `secret_ref`：仅用于 `secret: true`，引用可信宿主管理的 Secret，不能包含真实值。

`secret: true` binding 禁止 `value`，只保存 `key/required/secret/format` 和可选 `secret_ref`。Token、Cookie、账号、密码、连接串及资源 profile 等 Secret 真实值不得写入配置、TestPackage、argv、stdin、普通临时文件、结果或报告。可信宿主可将持久环境 Secret 保存在 `.panda-api-test-agent/local/secrets/environments/`，并继续使用 `.panda-api-test-agent/local/runtime/secrets/{run_id}.secret.json` 作为一次性 run bridge；前者是环境级持久 Secret store，后者必须按运行、环境、包指纹和短 TTL 校验后读取即删除。两者都不属于 Schema 2.1 配置或 L1 管理资产，L1 不创建、不读取、不修改、不迁移其中真实值。旧 `_env` 字段不属于 Schema 2.1。

## 3. 字段证据要求

关键字段应在相邻说明表中记录：字段路径、来源、证据等级、Owner、更新时间和证据引用。来源取 `code`、`deployment_config`、`user`、`probe`、`inferred`；证据等级取 `high`、`medium`、`low`。

## 4. 待确认项格式

```yaml
- id: GAP-UAT-AUTH-01
  block_stage: probe
  severity: hard
  impact: 无法验证 UAT 登录上下文，业务请求不能发送
  decider: 环境负责人
  unlocks: 可执行 UAT 鉴权探测
  evidence: deployment/config
```

## 5. 生命周期

- `config_status`：`discovered|drafted|confirmed|verified|active|degraded|drifted|reconfirm_required|retired`
- `runtime_status`：`unknown|unverified|verified|reachable|authentication_failed|connection_failed|permission_denied|unavailable`

运行探测不得直接修改本文件；由 L1 适配器依据运行证据和责任人确认执行增量更新。

## 6. 旧格式

缺少唯一 `yaml test-config` 块、`metadata.schema_version != "2.1"`、包含旧 `_env` 字段或包含需求级标题/结构时，返回：

```text
TEST_CONFIG_SCHEMA_UNSUPPORTED
检测到旧版 test-config.md。当前仅支持项目级多环境 Schema 2.1；请依据可验证证据重建项目配置。
```

不兼容、不迁移、不猜测，也不生成替代性的独立临时 Schema 配置。只重建或增量维护 canonical `docs/test-config.md`；未获覆盖授权时报告结构化缺口。

## 7. Revision 与最近变更摘要

稳定非敏感 `value`、binding 元数据、`secret_ref`、环境或资源拓扑变化时，递增 `metadata.config_revision` 并只记录最近一次有效变更；完整历史由 Git 管理。持久 Secret store 或一次性 run bridge 中仅 Secret 真实值轮换时，不修改 `docs/test-config.md`、不更新 `updated_at`、不递增 revision。初版只能标记为 `drafted`，不能标记为已验证或业务通过。
