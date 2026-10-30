# TestPackage 规范

## 目的

将测试设计保存为与执行器无关的结构化资产。确定性 Runner、pytest 薄适配器和其他执行器消费同一份包，避免由 Agent 临时重写请求代码或计算状态。

## 顶层结构

```json
{
  "schema_version": "1.0",
  "package_id": "PKG-project-module-001",
  "project": {
    "workspace_id": "project-id",
    "system_id": "system-id",
    "requirement_refs": ["REQ-001"]
  },
  "mode": "PROBE|EXECUTE",
  "contract": {
    "profile_path": "docs/interface-profile/module.md",
    "fingerprint": "sha256:<64 hex>",
    "lifecycle": "executable"
  },
  "environment": {
    "environment_id": "test",
    "base_url_env": "BASE_URL",
    "auth_env": ["ACCESS_TOKEN"],
    "allowed_origins_env": "API_TEST_ALLOWED_ORIGINS",
    "production": false,
    "timeout_seconds": 10,
    "max_response_bytes": 1048576
  },
  "risk": {
    "side_effect_level": "none|read|write|destructive",
    "cleanup_capability": "none|api|database|external"
  },
  "requirements": [],
  "business_rules": [],
  "scenarios": [],
  "gates": [],
  "fixtures": [],
  "cases": [],
  "known_gaps": []
}
```

不得把带敏感查询参数的 URL、token、cookie、密码或连接串写入包。Base URL 与凭据优先只保存环境变量名。生产标志必须为 `false`；Base URL 不得包含用户信息、查询参数或片段。非本机地址必须通过 `allowed_origins_env` 指向的安全环境变量提供可信非生产 origin 白名单，逗号分隔并精确匹配 `scheme://host[:port]`；包内自报 `production=false` 不能单独放行写请求。

## 已知缺口声明

`known_gaps` 必须区分"阻断执行"和"仅说明"，不得写成平铺字符串。每条使用对象：

```json
{
  "gap_id": "GAP-PROBE-01",
  "blocking": true,
  "stage": "probe",
  "description": "画像未提供明确无副作用的探测路由",
  "impact": "无法执行环境诊断，业务用例保持 not_run",
  "next_action": "确认一个只读探测入口，或改为只做连通性检查"
}
```

- `blocking=true` 的缺口必须对应用例状态为 `not_run`，不得出现"阻断项标记 blocking 但用例仍 passed"。
- `blocking=false` 的缺口只表示覆盖边界，不影响 verdict，但必须出现在报告"已知覆盖缺口"中。
- 禁止把"用例设计说明""占位请求解释"等实现细节写进 `known_gaps`；这类内容属于用例字段或证据，不属于缺口。

## 需求、业务规则与场景追踪

`requirements`、`business_rules` 和 `scenarios` 是正式执行包的必需结构化事实源。禁止由 Runner 或报告根据用例名称、URL 或自然语言描述猜测需求关系。

```json
{
  "requirements": [
    {
      "requirement_id": "REQ-001",
      "title": "创建后可查询",
      "description": "创建成功后可按返回标识查询同一资源。",
      "source_refs": ["prd.md#创建资源"],
      "required": true
    }
  ],
  "business_rules": [
    {
      "rule_id": "BR-001",
      "requirement_id": "REQ-001",
      "description": "创建成功必须返回资源标识。",
      "required": true
    }
  ],
  "scenarios": [
    {
      "scenario_id": "SC-001",
      "name": "正常创建并查询",
      "scenario_type": "positive",
      "requirement_refs": ["REQ-001"],
      "business_rule_refs": ["BR-001"],
      "case_refs": ["TC-001"],
      "required": true
    }
  ]
}
```

`project.requirement_refs` 必须与 `requirements[].requirement_id` 完全一致。每条必需业务规则至少关联一个用例；每条用例必须声明有效的 `requirement_refs`、`business_rule_refs` 和 `scenario_refs`。每条列表式断言必须声明唯一 `assertion_id`、至少一个有效 `business_rule_refs`，可用 `description` 说明业务含义。需求、规则或场景未覆盖、未执行或证据不足时，需求结论不得为通过。

Runner 在发送任何请求前读取 `profile_path` 文件并重新计算 SHA-256；与 fingerprint 不一致时阻断。Runner 只执行 `PROBE` 与 `EXECUTE`。`PROMOTE` 是编排模式，由 pytest 薄适配器复用已验证包和公共 Runner，不作为 Runner 的业务执行模式。

## 运行时门禁

除内建的非生产、鉴权变量和清理能力门禁外，包至少声明一个可实际执行的运行时门禁，例如健康检查或无副作用接口探测：

```json
{
  "gate_id": "GATE-HEALTH",
  "name": "服务健康检查",
  "classification": "environment",
  "request": {"method": "GET", "path": "/health"},
  "assertions": {
    "http_status": [200],
    "business_code": {"path": "$.code", "equals": 200},
    "required_fields": ["$.data.status"]
  }
}
```

任一门禁失败或错误时，Runner 禁止发送业务请求，并把所有业务用例标记为 `not_run`。

## 用例结构

支持列表式断言：

```json
{
  "case_id": "TC-001",
  "name": "创建并查询资源",
  "requirement_refs": ["AC-001"],
  "operation_refs": ["POST /resource"],
  "depends_on": [],
  "request": {
    "method": "POST",
    "path": "/resource",
    "headers": {"Authorization": "Bearer ${ACCESS_TOKEN}"},
    "json": {"name": "test-${run_id}"}
  },
  "assertions": [
    {"type": "http_status", "source": "http_status", "operator": "equals", "expected": 201},
    {"type": "business_code", "source": "response_json", "path": "$.code", "operator": "equals", "expected": 200},
    {"type": "key_fields", "source": "response_json", "path": "$.data.id", "operator": "exists", "expected": true}
  ],
  "extract": {"resource_id": "$.data.id"},
  "cleanup_extract": {"resource_id": "$.data.id"},
  "side_effect": "write",
  "object_ref": {"type": "resource", "id": "${resource_id}"},
  "teardown": {
    "type": "http",
    "request": {"method": "DELETE", "path": "/resource/${resource_id}"},
    "success_statuses": [200, 204]
  }
}
```

也兼容简化三重断言对象：

```json
{
  "http_status": [200],
  "business_code": {"path": "$.code", "equals": 200},
  "required_fields": ["$.data.items", "$.data.totalCount"]
}
```

正式通过至少包含 `http_status`、`business_code`、`key_fields` 三类必需断言。支持运算符：`equals`、`not_equals`、`exists`、`all_present`、`contains`、`in_range`。

## 变量与 JSONPath

- 使用 `${variable_name}` 引用运行变量或白名单环境变量。
- `${run_id}` 由 Runner 自动生成。
- 支持 `$.field`、`$.nested.field`、`$.items[0].id` 形式的受限 JSONPath。
- `sensitive_json_paths` 显式声明响应中语义敏感但字段名不敏感的路径，例如 `$.data.value`；Runner 在证据和断言中脱敏这些值。
- `extract` 只在用例必需断言全部通过后执行，供后继用例使用。
- `cleanup_extract` 在写请求收到响应后、业务断言之前执行，只用于保证失败场景仍能清理。
- 上一步变量不可跨运行复用；未知变量或未列入 `auth_env/base_url_env` 的环境变量必须拒绝。

## DAG

- 使用 `depends_on` 声明依赖，不依赖列表顺序隐式串联。
- Runner 按确定性拓扑顺序执行。
- 依赖用例不是 `passed` 时，后继用例为 `not_run`。
- 检测到循环依赖或未知依赖时拒绝执行整个包。

## Fixture

静态夹具：

```json
{
  "fixture_id": "FIX-STATIC",
  "type": "static",
  "values": {"resource_name": "test-${run_id}"}
}
```

HTTP 夹具会产生外部副作用，因此必须声明 `cleanup_extract`、`object_ref` 和 `teardown`。无 teardown 的 HTTP fixture 必须被拒绝。

## 副作用与清理

- `none`：健康检查、静态门禁。
- `read`：业务查询，不修改状态。
- `write`：创建、编辑、状态变更；必须有 teardown，或使用严格校验的 case-level `cleanup_waiver` 结构化风险豁免。
- `destructive`：删除、回滚、批量修改；除 teardown 外还必须 `destructive_confirmed=true`，永不允许 `cleanup_waiver`。

Runner 会根据 HTTP 方法和声明共同判定副作用：`POST/PUT/PATCH` 默认按写操作，`DELETE` 默认按破坏性操作。业务用例中的非安全方法只有命中 `read_only_operations_env` 指向的安全环境变量操作白名单（格式为 `METHOD /path`）才能降级为只读；包内布尔字段不得自证只读。运行时门禁只允许 `GET/HEAD/OPTIONS`，禁止以门禁名义发送任何非安全方法。普通写用例必须声明：

1. `cleanup_extract`：提取精确清理键；
2. `object_ref`：待处理对象登记信息；
3. `teardown`：使用静态写方法的清理请求、允许状态码，以及使用 `GET/HEAD/OPTIONS` 的清理后验证请求和三重断言。

无 teardown 的写用例只能声明 `cleanup_waiver`，且包级 `risk.cleanup_waiver_enabled=true`、`environment.production=false`、case `max_executions=1`。豁免必须包含 `waiver_id`、`scope=case`、`reason`、`residual_risk`、`approved_by`、带时区的 `approved_at`/`expires_at`（后者晚于前者且执行时未过期）、`max_requests=1`、非空 `data_tag`；必须有静态 `object_ref`，不得同时存在 `teardown` 或 `cleanup_extract`，请求体递归包含相同明确标签。该结构化例外不是静默跳过；destructive、write_attempt、批量、生产 URL 和 PROMOTE 永不放行。

Runner 在写请求发送前登记 teardown，并在执行前校验清理与清理后验证结构。即使业务断言失败或后继步骤异常，也执行已登记清理。只有清理请求成功且清理后验证断言全部通过，cleanup 才可为 `passed`；证据由校验器重新计算。清理失败进入 `cleanup.pending_objects`，整体 verdict 不得为 `passed`。

## 执行入口

```bash
python scripts/runner.py package.json --output-dir .workbuddy/panda-api-test-agent/runs/<run-id>
python scripts/validate_result.py <run-dir>/result.json --normalize
python scripts/render_report.py <run-dir>/result.json --output <run-dir>/report.html
```

或调用跨平台入口：

```bash
python scripts/run_pipeline.py package.json <run-dir> <report.html>
```

Bash 环境也可使用：

```bash
bash scripts/run_tests.sh package.json <run-dir> <report.html>
```

## 晋级 pytest

只有已有 `TestResult` 满足 `mode=EXECUTE`、`verdict=passed`、全部用例真实通过、无失败门禁、清理为 `passed/not_required` 且无 pending object、契约 `lifecycle=executable` 且有 fingerprint、无 blocking gap，才允许加入回归。使用独立入口 `python scripts/promote.py <existing-result.json> <generated-tests>`；PROMOTE 不重跑业务请求。pytest 适配器从包中参数化读取并调用公共 Runner，不复制 HTTP、断言、证据或清理逻辑。pytest collect 成功不等于业务通过。
