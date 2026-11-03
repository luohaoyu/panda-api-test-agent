# TestPackage 规范（2.3，兼容 2.2 / 2.1）

## 目的

保存单次可执行测试计划，并显式绑定项目配置、接口画像、需求上下文、目标环境及已通过的静态可行性规划。新包使用 TestPackage 2.3；Runner 继续兼容已存在的 TestPackage 2.2 / 2.1，但旧版本不得作为新建流程绕过 `GATE-FEASIBILITY`。项目配置仍只接受 Schema 2.1；不兼容旧版 `test-config.md`。

生成顺序固定为：`REQUIREMENT → CASE DESIGN（候选用例）→ FEASIBILITY / DATA PLAN → HUMAN REVIEW → FINAL GATE-FEASIBILITY → PACKAGE BUILD → PROBE/EXECUTE`。详细可行性结构见 `feasibility-plan-spec.md`。

## 固定目录约定

所有路径均相对于项目工作区根目录：

```text
docs/
├── test-config.md
└── interface-profile/
    └── <module>.md

.panda-api-test-agent/
├── requirements/
│   └── <requirement-id>/
├── runs/
│   └── <run-id>/
└── local/
    ├── secrets/
    │   └── environments/
    │       └── <environment-id>.secret.json
    └── runtime/
        └── secrets/
            └── <run-id>.secret.json
```

项目配置和接口画像固定保存在 `docs/`；需求、运行和本机临时运行资产固定保存在工作区根目录的 `.panda-api-test-agent/`。禁止使用 `.workbuddy/.panda-api-test-agent/` 或 `.workbuddy/panda-api-test-agent/`，禁止复制 `docs/` 资产到状态目录形成第二事实源。

## 顶层结构

```json
{
  "schema_version": "2.2",
  "package_id": "PKG-project-001",
  "project": {
    "workspace_id": "project-id",
    "system_id": "system-id",
    "requirement_refs": ["REQ-001"],
    "requirement_context_path": ".panda-api-test-agent/requirements/REQ-001/context.json"
  },
  "test_config": {
    "path": "docs/test-config.md",
    "schema_version": "2.1",
    "config_revision": 3,
    "environment_id": "uat",
    "fingerprint": "sha256:<64 hex>"
  },
  "mode": "PROBE|EXECUTE",
  "plan": {
    "title": "验证目标接口核心链路",
    "plan_version": 1
  },
  "review": {
    "status": "pending_review",
    "review_id": "REVIEW-20261030-001",
    "plan_fingerprint": "sha256:<64 hex>",
    "reviewer": null,
    "approved_at": null,
    "approved_scope": [],
    "reviewed_fields": {},
    "accepted_gaps": []
  },
  "contract": {
    "profile_path": "docs/interface-profile/module.md",
    "governance_schema_version": "1.0",
    "profile_id": "project:module",
    "profile_revision": 3,
    "code_baseline": "<commit-or-release>",
    "lifecycle": "executable",
    "document_fingerprint": "sha256:<64 hex>",
    "contract_fingerprint": "sha256:<64 hex>"
  },
  "contracts": [
    {
      "profile_path": "docs/interface-profile/route.md",
      "governance_schema_version": "1.0",
      "profile_id": "project:route",
      "profile_revision": 3,
      "code_baseline": "<commit-or-release>",
      "lifecycle": "executable",
      "document_fingerprint": "sha256:<64 hex>",
      "contract_fingerprint": "sha256:<64 hex>",
      "role": "trigger"
    },
    {
      "profile_path": "docs/interface-profile/operation-log.md",
      "governance_schema_version": "1.0",
      "profile_id": "project:operation-log",
      "profile_revision": 5,
      "code_baseline": "<commit-or-release>",
      "lifecycle": "executable",
      "document_fingerprint": "sha256:<64 hex>",
      "contract_fingerprint": "sha256:<64 hex>",
      "role": "oracle",
      "interface_ref": "scanOperationLog.pageList"
    },
    {
      "profile_path": "docs/interface-profile/track.md",
      "governance_schema_version": "1.0",
      "profile_id": "project:track",
      "profile_revision": 2,
      "code_baseline": "<commit-or-release>",
      "lifecycle": "executable",
      "document_fingerprint": "sha256:<64 hex>",
      "contract_fingerprint": "sha256:<64 hex>",
      "role": "negative_oracle",
      "interface_ref": "track.public.list"
    }
  ],
  "environment": {
    "environment_id": "uat",
    "production": false,
    "runtime_bindings": {
      "base_url": {
        "key": "UAT_BASE_URL",
        "required": true,
        "secret": false,
        "format": "url"
      },
      "allowed_origins": {
        "key": "UAT_ALLOWED_ORIGINS",
        "required": true,
        "secret": false,
        "format": "origin_list"
      },
      "read_only_operations": {
        "key": "UAT_READ_ONLY_OPERATIONS",
        "required": false,
        "secret": false,
        "format": "operation_list"
      },
      "auth": {
        "type": "bearer",
        "header": "Authorization",
        "credential": {
          "key": "UAT_ACCESS_TOKEN",
          "required": true,
          "secret": true,
          "format": "string"
        }
      },
      "databases": {
        "orders": {
          "key": "UAT_ORDERS_DB",
          "required": false,
          "secret": true,
          "format": "db_config"
        }
      },
      "redis": {
        "session": {
          "key": "UAT_SESSION_REDIS",
          "required": false,
          "secret": true,
          "format": "redis_config"
        }
      }
    },
    "timeout_seconds": 10,
    "max_response_bytes": 1048576
  },
  "risk": {},
  "feasibility": {
    "schema_version": "1.1",
    "plan_id": "FEAS-REQ-001",
    "design_fingerprint": "sha256:<64 hex>",
    "candidate_case_ids": [],
    "approved_case_ids": [],
    "executable_case_ids": [],
    "blocked_case_ids": [],
    "coverage_map": {},
    "dependency_matrix": {},
    "gate": {"gate_id": "GATE-FEASIBILITY", "status": "passed", "blocking_reasons": []},
    "plan_fingerprint": "sha256:<64 hex>"
  },
  "package_build": {
    "status": "compiled",
    "source_design_fingerprint": "sha256:<64 hex>",
    "source_feasibility_fingerprint": "sha256:<64 hex>"
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

## 版本与可行性绑定

- `schema_version=2.3`：新包标准；支持 `contracts` 多契约绑定，必须携带有效 `feasibility` 与 `package_build`，且 `review.approved_scope` 必须与 `feasibility.executable_case_ids` 完全一致。`contract` 仍必须存在并等于 `role=trigger` 的那一条，作为单契约消费方（报告、PROMOTE、旧结果校验）的兼容视图。
- `schema_version=2.2`：只兼容既有单契约可执行资产；报告标记为 legacy，不能声称已完成多接口契约治理。
- `schema_version=2.1`：只兼容既有可执行资产；报告标记为 legacy，不能声称已经经过新的可行性规划。
- `contracts` 缺省时，`contract` 按一条 `role=trigger` 绑定解释，行为与旧包完全一致；声明 `contracts` 时，每条绑定的 `profile_id/profile_revision/lifecycle/facts/双指纹` 均独立校验，任一不通过即整包阻断，不得只校验触发接口。
- 数据规划允许存在 `blocked` 依赖并以 `pending_review` 状态完成；人工审核决定全量批准、批准依赖已闭合的子集、暂缓或要求补充。
- 修改候选用例、批准范围或执行依赖后，必须重新生成 FeasibilityPlan；禁止手工修改嵌入快照或只更新指纹。
- 最终 `GATE-FEASIBILITY` 在人工审核后、Package Build 前验证；批准范围仍含未闭合依赖时不得建包。已编译包仍在 RuntimeContext、Secret envelope 和 Runner 之前复核该快照，失败不得发送 HTTP。

## 项目配置绑定

新建 TestPackage 必须固定声明 `test_config.path="docs/test-config.md"`；不得改用状态目录、绝对路径或其他兼容别名。Runner 在发送请求前必须：

1. 要求 `test_config.schema_version="2.1"` 和正整数 `config_revision`。
2. 读取 `test_config.path`，要求路径为 `docs/test-config.md`，并要求唯一 `yaml test-config` 块、Schema 2.1、相同 project ID 和 revision。
3. 重新计算文件 SHA-256，与 `test_config.fingerprint` 比较。
4. 要求 `test_config.environment_id == environment.environment_id`。
5. 拒绝内联 `environment.base_url`；Base URL、认证、Origin 白名单和只读操作白名单只能通过 `environment.runtime_bindings` 声明。旧 `_env`/`_key` 字段和顶层 `authentication` 一律拒绝，必须重新生成 TestPackage。

旧格式返回 `TEST_CONFIG_SCHEMA_UNSUPPORTED`，不迁移、不兼容。

## 接口画像绑定

接口画像仍是面向人和 AI 的半结构化知识契约，不要求 Runner 解析全部业务正文；但每份画像必须有唯一 `yaml interface-profile-governance` 块。生成包时只允许从经 L1 维护且校验通过的治理块复制身份字段。新建 TestPackage 的 `contract.profile_path` 必须匹配 `docs/interface-profile/<module>.md`，其中 `<module>` 为实际模块文件名；不得引用状态目录或该目录以外的画像副本。

Runner 在发送请求前必须：

1. 读取 `contract.profile_path`，要求路径匹配 `docs/interface-profile/<module>.md`，并要求唯一治理块且 `schema_version="1.0"`。
2. 核对 `profile_id`、正整数 `profile_revision`、`code_baseline` 和 `lifecycle`；普通需求/链路执行要求画像与包内均为 `executable`，`execution_profile=single_api_quality` 可使用 `minimally_usable`、`confirmed` 或 `executable`，但必须在结果中披露治理边界。
3. 重新计算画像的 `document_fingerprint`。计算前将治理块中的两个 fingerprint 值规范化为固定占位符，避免指纹自引用。
4. 从画像治理块读取并核对 `contract_fingerprint`；该指纹由 L1 对规范化契约事实计算，Runner 不从自然语言正文重新推导业务语义。
5. 任一身份字段、revision、生命周期或指纹不一致时阻断旧包并要求重新生成；包内自报 `executable` 不能覆盖画像生命周期。
6. 读取治理块中可选的 `facts:` 引用。`lifecycle=executable` 时每条引用必须为 `confirmed` 且带 `confirmed_by`；候选事实、`needs_reviewer` 或未知状态一律阻断（`PROFILE_FACTS_UNCONFIRMED`）。画像未声明 `facts:` 时保持向后兼容，不额外阻断。

`facts:` 是 L1 事实账本 `docs/scan-facts/ledger.jsonl` 的引用投影，不是第二份事实源：Runner 只校验状态是否已结算，不根据它推导业务语义，也不回写账本。

纯排版/说明变化可以只更新 `document_fingerprint`，不会伪造契约语义变化，但会使绑定旧文档版本的运行包失效并要求重建，以保证运行证据可追溯。

### 局部画像执行

画像生命周期允许渐进使用：

- `discovered|drafted`：仅允许继续扫描和设计缺口，不允许业务执行；
- `minimally_usable`：当前目标所需的路由、方法、参数、鉴权和基本响应事实已具备，允许受控 PROBE 或局部 EXECUTE；
- `confirmed`：已完成指定范围确认，但完整治理仍需补齐；按当前用例需要决定是否允许局部执行；
- `executable`：允许完整 EXECUTE、PROMOTE 和正式回归资产沉淀。

使用 `minimally_usable` 或 `confirmed` 执行时，TestPackage 必须在 `known_gaps` 中披露未完成的 Owner、指纹、清理、外部边界或 Oracle，并将其绑定到具体 case/AC。不得把局部执行结果描述为完整需求通过；PROMOTE 仍只接受 `executable`。

### 多契约角色

`contracts` 中每条绑定声明 `role`，取值固定：

- `trigger`：本包主动触发业务行为的主接口，有且仅有一条；
- `oracle`：用于断言结果的查询接口，必须声明 `interface_ref`；
- `negative_oracle`：用于断言不可见/不应出现的反证接口，必须声明 `interface_ref`。

约束：角色化绑定不得重复（同一 `profile_id + role` 只出现一次）；Oracle 与 trigger 受同一套生命周期与事实门槛约束，不得因为“只是查询接口”降级校验；`fast_single_api`（`execution_profile=single_api_quality`）沿用既有放宽规则，但放宽同样逐条生效，不因主接口合格而豁免 Oracle。

运行结果必须回写全部绑定：`result.json` 的 `contracts` 为数组，逐条记录 `role/interface_ref/profile_id/profile_revision/code_baseline/lifecycle/contract_fingerprint`；`contract` 保留为 trigger 视图，保证旧消费方与历史结果可比。证据归因不得把整次运行只记成主画像版本。

## 环境隔离与安全

同一包的 HTTP、认证、DB、Redis、MQ 和可观测性必须来自一个环境节点。TestPackage 只保存 `runtime_bindings` 元数据，不保存变量值。每个绑定必须声明 `key/required/secret/format`；命名 DB 资源声明在 `runtime_bindings.databases.<name>` 且格式固定为 `db_config`，命名 Redis 资源声明在 `runtime_bindings.redis.<name>` 且格式固定为 `redis_config`。结构值只在 RuntimeContext 内存中使用并深层只读/复制隔离；`production` 必须为 false；非本机 origin 必须命中 `runtime_bindings.allowed_origins` 指向的白名单。

运行值来源优先级固定为：① canonical `docs/test-config.md` 目标环境中 `secret=false` binding 的稳定 `value`；② `.panda-api-test-agent/local/secrets/environments/<environment-id>.secret.json` 中包已声明的 Secret；③ 当前 run bridge 中同名 Secret，覆盖持久 Secret；④可信宿主显式传入的非敏感 `runtime_values`，覆盖 config value。`runtime_values` 不得携带 Secret；所有来源中的未声明 key 均拒绝。缺失值由 RuntimeContext preflight 按 `required` 判定。

## 需求追踪

`requirements`、`business_rules`、`scenarios` 来自需求 context，不从项目配置或用例名称猜测。`project.requirement_refs` 必须与 requirements ID 一致。每条用例必须声明有效 `requirement_refs`、`business_rule_refs`、`scenario_refs`；每条断言必须有稳定 `assertion_id` 和有效规则引用。

## 已知缺口

每项为对象：

```json
{
  "gap_id": "GAP-PROBE-01",
  "blocking": true,
  "stage": "probe",
  "description": "缺少无副作用探测入口",
  "impact": "无法验证环境，业务用例保持 not_run",
  "next_action": "确认只读探测入口"
}
```

禁止字符串形式。blocking gap 与实际执行状态必须一致。

## 门禁

包级 `execution_control` 用于在 RuntimeContext 构造和 HTTP Executor 创建前表达正式请求授权。若当前存在已确认的硬阻断，必须声明：

```json
{
  "execution_control": {
    "fail_closed_before_http": true,
    "formal_http_allowed": false,
    "stop_stage": "auth_probe_path_preflight",
    "blocking_gap_ids": ["GAP-AUTH-PROBE-01"],
    "reason": "未确认无副作用认证探测入口"
  }
}
```

`blocking_gap_ids` 必须引用 `known_gaps` 中 `blocking=true` 的现有缺口。可信宿主必须先消费 `execution_control`；`formal_http_allowed=false` 时不得要求 RuntimeContext 值，不得创建运行目录、HTTP Executor、结果或报告。只有 `formal_http_allowed=true` 且无 blocking gap 时才能继续 RuntimeContext Preflight。

除内建非生产、鉴权绑定、配置绑定和清理门禁外，放行包至少声明一个可执行的只读运行时门禁。门禁仅允许 GET/HEAD/OPTIONS；失败后业务用例全部 `not_run`。`request=null` 或 `preflight_only=true` 的声明不能进入 HTTP GateExecutor，只能通过包级 `execution_control` 阻断。

## 认证探测（auth_probe）

`GATE-AUTH` 只校验凭据绑定的存在性，不能证明 Token 有效。带凭据的包可以在顶层声明 `auth_probe`；没有独立只读探测入口时，认证验证并入已审核的业务请求。

```json
{
  "auth_probe": {
    "strategy": "declared",
    "gate_id": "GATE-AUTH-PROBE",
    "side_effect_free": {
      "confirmed_by": "环境安全负责人",
      "confirmed_at": "2026-10-31T10:00:00+08:00",
      "reference": "接口确认记录链接或工单号",
      "rationale": "该 GET 入口只读，不写库、不改 Redis 登录态"
    }
  }
}
```

- `strategy=declared`：`gate_id` 必须引用 `gates` 中已声明的门禁；该门禁只允许 GET/HEAD/OPTIONS、必须有静态绝对路径和断言；`side_effect_free` 的 `confirmed_by/reference/rationale` 不得为 `pending/unknown/待确认`。声明门禁必须在其他运行时门禁之前执行，结果带 `role=auth_probe`；通过后 `environment.runtime_status.auth=verified`。
- `strategy=none`：仅适用于 `auth.type=none|anonymous`；带凭据时声明该值会被拒绝。
- `strategy=blocked`：必须在 `blocked` 中给出 `gap_id`（引用 `known_gaps` 中 `blocking=true` 的缺口）和 `reason`；该声明本身在 Package 级 Preflight 确定性阻断，不发任何 HTTP、不创建运行目录。

带凭据但未声明 `auth_probe` 的包不再一律阻断；执行器通过已审核业务请求观察认证结果：401/403 记为认证失败，已送达的其他业务结果记为认证已观察。若声明 `auth_probe`，`read_only_operations` 绑定为必填时，声明门禁的 `"<METHOD> <path>"` 必须出现在只读白名单中；否则 Runtime Preflight 阻断。`execution_control.formal_http_allowed=false` 与 `strategy=declared` 互斥。

`validate_result.py` 要求 `verdict=passed` 的运行具有 `environment.runtime_status.auth=verified` 或 `declared_only`；其中 `verified` 可以来自 auth probe，也可以来自已审核业务请求的认证观察，`declared_only` 仅表示凭据存在且不应单独支撑完整通过。

## DB Oracle（只读）

统一 Run 中 DB 核对通过 `oracle` 类型任务声明（`oracle_type: "db"`），由 `scripts/db_oracle.py` 的 `db_task_executor` 经可信宿主注入执行。声明在编译期静态校验（`plan_compiler` 调用 `validate_db_oracle`），非法声明 fail-closed。

声明字段：

- `oracle_id`：稳定标识。
- `kind: "db"`、`oracle_type: "db"`。
- `binding`：`runtime_bindings.databases.<name>.key` 指向的绑定名；运行值兼容旧 DSN 字符串和结构化 `db_config`。结构化配置至少声明 `driver`（`sqlite|mysql|postgresql`）及对应连接字段，也可用 `dsn`；执行器只在内存构造驱动参数/DSN，不回写包、配置或其他文件。连接信息属于 Secret，只经 RuntimeContext 注入，不得写入包或证据。
- `statement`：单条 `SELECT`；必须包含 `WHERE`，必须显式列字段（禁止 `SELECT *`），禁止注释、多语句、分号拼接及任何数据变更关键字（INSERT/UPDATE/DELETE/DDL/PRAGMA/ATTACH/INTO OUTFILE 等）。`${var}` 占位符在执行期以安全字面量替换（字符串自动加引号转义，未知变量报错）。
- `max_rows`：1..500，默认 100；结果集超限记 `error`，不得静默截断。
- `assertions`：`source` 仅允许 `row_count`、`scalar`、`row[<index>].<column>`；算子复用 Runner 词表，`row_count` 不得使用 `exists/all_present/contains`。

执行语义：

- 绑定缺失、驱动未安装（MySQL 需 pymysql、PostgreSQL 需 psycopg2）或连接失败 → `not_enabled`，属于覆盖缺口，不得记为通过；
- 数据不符 → 断言 `failed`，计入用例事实；
- 结果集超限或未知变量 → `error`；
- SQLite 以 read-only URI 打开；驱动层即只读，任何写语句在声明层已被拒绝；
- 证据写入 `evidence/oracle-<id>.json`，行数据按原样写入，不做掩码或 Secret 替换。

## Redis Oracle（只读）

统一 Run 中 Redis 核对通过 `oracle` 类型任务声明（`oracle_type: "redis"`），由可信宿主向 `slice_executor` 注入 `scripts/composite_oracle_executor.py`，再分派到 `scripts/redis_oracle.py`。既有 `run_pipeline` 继续用于旧 HTTP 直跑，不直接接管 unified run。

声明字段：

- `oracle_id`、`kind: "redis"`、`oracle_type: "redis"`。
- `binding`：`runtime_bindings.redis.<name>.key` 指向的 `redis_config` 绑定名；可包含 `url`/`dsn`，或 `host/port/db/username/password` 等驱动参数，均只在内存使用。
- `command`：仅允许 `GET/MGET/EXISTS/TYPE/TTL/PTTL/HGET/HMGET/HGETALL/SCARD/SMEMBERS/ZSCORE/ZRANGE/LRANGE`；写命令、`KEYS`、`EVAL/EVALSHA` 和其他未列命令全部在编译期拒绝。
- `args`：非空标量列表，支持 `${var}` 运行时变量。
- `max_items`：1..1000，默认 100；`max_bytes`：1..10485760，默认 1048576；超限记 `error`，不得静默截断。
- `assertions`：算子复用 Runner；`source` 支持 `result|value|item_count|result[index]|item[index]|field.<name>`。

绑定缺失、连接失败或 Redis driver 缺失返回 `not_enabled`，数据不符返回 `failed`。证据写入 `evidence/oracle-<id>.json`，Redis 结果按原样记录，不改变当前 Oracle 证据策略。

消息队列与日志/轨迹查询 Oracle 尚未内建执行器；未知 `oracle_type` 或旧 free-form Oracle 保持现有兼容，但不得标记 `builtin_oracle_executor`，也不得误走 DB/Redis 内建执行器。声明存在但无对应执行能力时，结果保持 `not_run`/`not_enabled`，不得包装为通过。

## 用例要求

每条候选用例在 CASE DESIGN 阶段至少包含 `execution_dependencies`；没有额外数据、凭证、环境、fixture、Oracle 或 cleanup 依赖时显式写空数组。通过可行性门禁并进入 TestPackage 后，每条可执行用例至少包含：

- `case_id`、name、需求/规则/场景/接口引用。
- `depends_on`。
- 静态 method、path、参数和变量引用。
- 列表式断言；至少覆盖 HTTP、业务 code、关键字段。
- `side_effect`：`none|read|write_attempt|write|destructive`。
- 写操作的 `cleanup_extract`、`object_ref` 和 teardown，或严格 case-level waiver。

变量仅允许 `${run_id}`、前序提取变量和包白名单运行时绑定 key。未知变量拒绝。RuntimeContext 中的绑定是只读的，不能被 fixture、extract 或 case 变量覆盖。

## RuntimeContext 运行时绑定

RuntimeContext 2.0 仅存在于单次运行内存中，不属于 TestPackage。它必须包含与包一致的 `environment_id`，并由可信宿主以显式 Python 对象构造；映射、列表等结构运行值必须复制隔离并转换为深层只读视图。实际 URL、Token、Cookie 和其他 Secret 不得进入 package、argv、stdin、result、报告或 `os.environ`。用户已授权 DB/Redis/认证 Secret 明文进入项目本机 `.panda-api-test-agent/local/secrets/environments/<environment-id>.secret.json`：该持久 Store 只能由可信宿主使用，必须校验项目根和安全 `environment_id`，只接受当前包声明为 `secret=true` 的 binding，原子替换写入，按包目标环境精确读取，防路径逃逸，不进行目录扫描、glob 或历史回退。`.panda-api-test-agent/local/runtime/secrets/{run_id}.secret.json` 继续作为 Host-private ephemeral run bridge：只允许当前包已声明的 Secret，绑定 run ID、environment ID、当前包指纹、`single_use=true` 和不超过 10 分钟的 TTL；可信宿主按精确 run ID 原子认领并在构造 RuntimeContext 前删除，删除失败 fail-closed。可信宿主必须先完成 package execution preflight，再读取任何持久或 run Secret。Runner 只能读取包声明的 binding key；未声明值、Secret 分类不一致、必填值缺失或格式非法时，必须在创建 HTTP Executor 前阻断。

## 副作用

- `none/read`：无业务状态修改。
- `write_attempt`：预期在持久化前拒绝；必须提供只读 verification 和 acceptance guard，不允许 waiver。
- `write`：普通非生产单次业务写入默认允许；优先提供可靠 teardown。无法清理时必须使用可审计的残留对象登记，保留 `pending_objects` 和残余风险，不能给出完整通过结论。
- `destructive`：必须 teardown 和显式确认；永不允许 waiver。
- 批量写入、生产/疑似生产、真实外部动作：不因普通测试默认策略放行，必须阻断或请求确认。

写请求未知结果禁止自动重试。清理失败进入 pending objects，整体不能 passed。

## 设计审核

`EXECUTE` 必须携带 `review` 元数据并通过 `validate_review.py` 校验。审核状态允许 `pending_review|approved|partially_approved|rejected|plan_only|expired|superseded`；只有未过期且未被取代的 `approved` 或 `partially_approved` 能进入执行。`approved` 覆盖全部用例，`partially_approved` 只执行 `approved_scope`，其他用例必须 `not_run`。每个批准用例必须有非空 `reviewed_fields`。审核指纹必须等于当前包重新计算的计划指纹；指纹变化自动使审核失效，Runner 不发送未批准业务请求。

任何会发送 HTTP 请求的 `PROBE` 同样必须经过设计审核；只有纯本地静态检查可以免于审核。其请求仍必须遵守只读门禁和环境安全约束。详细审核字段、计划指纹和标题级预览见 `references/review-spec.md`。

## 模式

Runner 只接受 `PROBE` 和 `EXECUTE`。CASE DESIGN、FEASIBILITY PLAN 和 PACKAGE BUILD 是执行前生命周期阶段，不是 Runner mode；`PROMOTE` 是独立结果消费动作，也不是 TestPackage mode。

## 执行入口

正式入口是可信宿主中的 Python API：

```python
execute_test_package(
    package_path,
    run_dir,
    report_path,
    runtime_values=non_secret_runtime_overrides,
    local_secret_run_id=optional_run_id,
    outcome_path=host_outcome_path,
)
```

可信宿主在内存中调用 `build_runtime_context`，再进入 `run_pipeline(..., runtime_context=context)`。可选 `host_outcome` 只记录绑定名称、非敏感 Context 元数据、阶段耗时、停止阶段和产物路径，不保存运行值。`trusted_host.py`、`run_pipeline.py` 与 `runner.py` 均不提供携带凭据的命令行入口；pipeline 和 Runner 的 CLI 默认拒绝执行。校验器和报告器读取结果文件，其中报文与运行值按原样保留。新包必须使用 TestPackage 2.1、`environment.runtime_bindings` 和完整画像绑定；旧包不迁移、不允许新执行或晋级。
