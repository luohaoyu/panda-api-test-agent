# TestPackage 规范（2.2，兼容 2.1）

## 目的

保存单次可执行测试计划，并显式绑定项目配置、接口画像、需求上下文、目标环境及已通过的静态可行性规划。新包使用 TestPackage 2.2；Runner 继续兼容已存在的 TestPackage 2.1，但 2.1 不得作为新建流程绕过 `GATE-FEASIBILITY`。项目配置仍只接受 Schema 2.1；不兼容旧版 `test-config.md`。

生成顺序固定为：`REQUIREMENT → CASE DESIGN（候选用例）→ FEASIBILITY / DATA PLAN → HUMAN REVIEW → FINAL GATE-FEASIBILITY → PACKAGE BUILD → PROBE/EXECUTE`。详细可行性结构见 `feasibility-plan-spec.md`。

## 顶层结构

```json
{
  "schema_version": "2.2",
  "package_id": "PKG-project-001",
  "project": {
    "workspace_id": "project-id",
    "system_id": "system-id",
    "requirement_refs": ["REQ-001"],
    "requirement_context_path": ".workbuddy/.panda-api-test-agent/requirements/REQ-001/context.json"
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

- `schema_version=2.2`：新包标准；必须携带有效 `feasibility` 与 `package_build`，且 `review.approved_scope` 必须与 `feasibility.executable_case_ids` 完全一致。
- `schema_version=2.1`：只兼容既有可执行资产；报告标记为 legacy，不能声称已经经过新的可行性规划。
- 数据规划允许存在 `blocked` 依赖并以 `pending_review` 状态完成；人工审核决定全量批准、批准依赖已闭合的子集、暂缓或要求补充。
- 修改候选用例、批准范围或执行依赖后，必须重新生成 FeasibilityPlan；禁止手工修改嵌入快照或只更新指纹。
- 最终 `GATE-FEASIBILITY` 在人工审核后、Package Build 前验证；批准范围仍含未闭合依赖时不得建包。已编译包仍在 RuntimeContext、Secret envelope 和 Runner 之前复核该快照，失败不得发送 HTTP。

## 项目配置绑定

Runner 在发送请求前必须：

1. 要求 `test_config.schema_version="2.1"` 和正整数 `config_revision`。
2. 读取 `test_config.path`，要求唯一 `yaml test-config` 块、Schema 2.1、相同 project ID 和 revision。
3. 重新计算文件 SHA-256，与 `test_config.fingerprint` 比较。
4. 要求 `test_config.environment_id == environment.environment_id`。
5. 拒绝内联 `environment.base_url`；Base URL、认证、Origin 白名单和只读操作白名单只能通过 `environment.runtime_bindings` 声明。旧 `_env`/`_key` 字段和顶层 `authentication` 一律拒绝，必须重新生成 TestPackage。

旧格式返回 `TEST_CONFIG_SCHEMA_UNSUPPORTED`，不迁移、不兼容。

## 接口画像绑定

接口画像仍是面向人和 AI 的半结构化知识契约，不要求 Runner 解析全部业务正文；但每份画像必须有唯一 `yaml interface-profile-governance` 块。生成包时只允许从经 L1 维护且校验通过的治理块复制身份字段。

Runner 在发送请求前必须：

1. 读取 `contract.profile_path`，要求唯一治理块且 `schema_version="1.0"`。
2. 核对 `profile_id`、正整数 `profile_revision`、`code_baseline` 和 `lifecycle`；普通需求/链路执行要求画像与包内均为 `executable`，`execution_profile=single_api_quality` 可使用 `minimally_usable`、`confirmed` 或 `executable`，但必须在结果中披露治理边界。
3. 重新计算画像的 `document_fingerprint`。计算前将治理块中的两个 fingerprint 值规范化为固定占位符，避免指纹自引用。
4. 从画像治理块读取并核对 `contract_fingerprint`；该指纹由 L1 对规范化契约事实计算，Runner 不从自然语言正文重新推导业务语义。
5. 任一身份字段、revision、生命周期或指纹不一致时阻断旧包并要求重新生成；包内自报 `executable` 不能覆盖画像生命周期。

纯排版/说明变化可以只更新 `document_fingerprint`，不会伪造契约语义变化，但会使绑定旧文档版本的运行包失效并要求重建，以保证运行证据可追溯。

### 局部画像执行

画像生命周期允许渐进使用：

- `discovered|drafted`：仅允许继续扫描和设计缺口，不允许业务执行；
- `minimally_usable`：当前目标所需的路由、方法、参数、鉴权和基本响应事实已具备，允许受控 PROBE 或局部 EXECUTE；
- `confirmed`：已完成指定范围确认，但完整治理仍需补齐；按当前用例需要决定是否允许局部执行；
- `executable`：允许完整 EXECUTE、PROMOTE 和正式回归资产沉淀。

使用 `minimally_usable` 或 `confirmed` 执行时，TestPackage 必须在 `known_gaps` 中披露未完成的 Owner、指纹、清理、外部边界或 Oracle，并将其绑定到具体 case/AC。不得把局部执行结果描述为完整需求通过；PROMOTE 仍只接受 `executable`。

## 环境隔离与安全

同一包的 HTTP、认证、DB、Redis、MQ 和可观测性必须来自一个环境节点。TestPackage 只保存 `runtime_bindings` 元数据，不保存变量值。每个绑定必须声明 `key/required/secret/format`；`production` 必须为 false；非本机 origin 必须命中 `runtime_bindings.allowed_origins` 指向的白名单。

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

RuntimeContext 2.0 仅存在于单次运行内存中，不属于 TestPackage。它必须包含与包一致的 `environment_id`，并由可信宿主以显式 Python 对象构造。实际 URL、Token、Cookie 和其他 Secret 不得进入 package、argv、stdin、result、evidence、报告、`os.environ` 或任意普通临时文件。唯一例外是 `.panda-api-test-agent/local/runtime/secrets/{run_id}.secret.json` 的 Host-private ephemeral secret file：只允许包含当前包已声明为 Secret 的 binding，必须绑定 run ID、environment ID、当前包指纹、`single_use=true` 和不超过 10 分钟的 TTL；可信宿主按精确 run ID 原子认领，禁止目录扫描、glob、任意路径与历史回退，并在构造 RuntimeContext 前删除，删除失败必须 fail-closed。Runner 只能读取包声明的 binding key；未声明值、Secret 分类不一致、必填值缺失或格式非法时，必须在创建 HTTP Executor 前阻断。

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
    runtime_values=runtime_values,
    outcome_path=host_outcome_path,
)
```

可信宿主在内存中调用 `build_runtime_context`，再进入 `run_pipeline(..., runtime_context=context)`。可选 `host_outcome` 只记录绑定名称、非敏感 Context 元数据、阶段耗时、停止阶段和产物路径，不保存运行值。`trusted_host.py`、`run_pipeline.py` 与 `runner.py` 均不提供携带凭据的命令行入口；pipeline 和 Runner 的 CLI 默认拒绝执行。校验器和报告器只读取脱敏后的结果文件，不接触运行时 Secret。新包必须使用 TestPackage 2.1、`environment.runtime_bindings` 和完整画像绑定；旧包不迁移、不允许新执行或晋级。
