# FeasibilityPlan 规范（1.1，兼容 1.0 最终快照）

## 目的

`FeasibilityPlan` 位于候选用例设计与可执行 TestPackage 构建之间。它只处理静态事实，不读取运行时凭据、不准备数据、不发送 HTTP。

固定顺序：

```text
REQUIREMENT
→ CASE DESIGN（候选用例）
→ FEASIBILITY / DATA PLAN（覆盖与依赖矩阵）
→ HUMAN REVIEW（决定批准范围）
→ FINAL GATE-FEASIBILITY
→ PACKAGE BUILD（可执行 TestPackage）
→ PROBE / EXECUTE
→ REPORT / PROMOTE
```

不得在没有候选用例时空泛规划数据；也不得把候选用例直接当成可执行用例。缺少真实数据、凭证、fixture、Oracle 或 cleanup 时，必须记录依赖和 known gap 并继续完成规划，不得在规划阶段停止流程。候选用例只有经过人工审核，且批准范围内执行依赖全部闭合后，才能进入 Package Build。

## 顶层结构

```json
{
  "schema_version": "1.1",
  "plan_id": "FEAS-REQ-001",
  "requirement_id": "REQ-001",
  "design_fingerprint": "sha256:<64 hex>",
  "planning_status": "ready|invalid|finalized",
  "review_binding": {
    "status": "pending_review|approved|partially_approved",
    "review_id": null,
    "approved_case_ids": []
  },
  "candidate_case_ids": ["CASE-001", "CASE-002"],
  "approved_case_ids": [],
  "ready_case_ids": ["CASE-001"],
  "executable_case_ids": [],
  "blocked_case_ids": ["CASE-002"],
  "out_of_scope_case_ids": [],
  "coverage_map": {
    "requirements": [],
    "business_rules": [],
    "full_requirement_coverage": false
  },
  "dependency_matrix": {
    "cases": [],
    "unknown_dependencies": [],
    "cycles": []
  },
  "gate": {
    "gate_id": "GATE-FEASIBILITY",
    "status": "pending_review",
    "evaluated_against_review": false,
    "blocking_reasons": []
  },
  "plan_fingerprint": "sha256:<64 hex>"
}
```

## 候选用例执行依赖

每条进入批准范围的候选用例必须显式声明 `execution_dependencies`；没有额外依赖时写空数组，禁止通过字段缺失表达“无依赖”。

```json
{
  "case_id": "CASE-001",
  "execution_dependencies": [
    {
      "dependency_id": "DATA-WAYBILL-001",
      "kind": "data",
      "status": "blocked",
      "description": "需要能够匹配到在途任务且会被状态校验拒绝的真实运单",
      "required_from": "测试数据负责人",
      "next_action": "提供运单号和 taskId"
    }
  ]
}
```

`kind` 只允许：

- `data`：业务数据、对象状态、唯一标识。
- `credential`：账号、角色、权限凭据。
- `environment`：环境能力和依赖服务。
- `fixture`：可重复的数据准备能力。
- `oracle`：查询入口、时效窗口和判定依据。
- `cleanup`：teardown、残留追踪或明确风险边界。

`status` 只允许：

- `resolved`：依赖事实已绑定且可审计。
- `not_required`：经设计判断当前用例不需要。
- `blocked`：尚缺事实、数据或凭证。规划继续完成；只有人工审核把该用例纳入批准范围后，它才会使最终门禁失败并阻止 Package Build。

## Coverage map

`coverage_map` 同时保留候选覆盖和当前可执行覆盖：

- `candidate_case_ids`：由需求/验收点推导出的候选用例。
- `executable_case_ids`：在本次批准范围内且依赖闭合的用例。
- `full_requirement_coverage`：全部 required requirement 和 business rule 是否均有可执行覆盖。

人工审核允许只批准一个依赖已闭合的可行子集。此时最终 `GATE-FEASIBILITY` 可以通过，但 `full_requirement_coverage=false`，报告必须明确“当前子集可执行，不代表完整需求可验收”。

## Dependency matrix

每个候选用例必须记录：

- 是否在批准范围；
- 是否仍为 `candidate_only`；
- `depends_on` 用例关系；
- 数据、凭证、环境、fixture、Oracle、cleanup 依赖；
- 当前 `resolved|blocked` 状态；
- 阻塞原因。

未知 case 依赖和依赖环属于设计结构错误，使规划状态为 `invalid`。数据、凭证、环境、fixture、Oracle 和 cleanup 缺口属于规划事实，记录为 `blocked`/known gap，不使规划流程失败。

## GATE-FEASIBILITY

这是静态门禁，不属于 Runner HTTP gate，不得发送请求。规划完成但尚未人工审核时，门禁状态必须为 `pending_review`；数据缺口不得直接使规划失败。人工审核后，以下任一情况使最终门禁失败：

1. 批准范围内用例仍为 `candidate_only`。
2. 批准范围内用例未显式声明 `execution_dependencies`。
3. 批准范围内存在 `status=blocked` 的执行依赖。
4. 用例依赖引用未知 case。
5. 用例依赖图存在环。
6. FeasibilityPlan 与当前设计指纹或批准范围不一致。
7. FeasibilityPlan 内容在规划后被修改，导致指纹失配。

人工审核后门禁失败时停止在 Package Build 之前：不构造 RuntimeContext、不消费 Secret envelope、不创建 Runner、不发送 HTTP。门禁仍为 `pending_review` 时只等待审核决策，不要求先补齐所有数据；审核人可批准依赖已闭合的子集、暂缓部分用例或要求补充。

## Package Build

仅 HUMAN REVIEW 已完成且最终 `GATE-FEASIBILITY=passed` 时调用 `compile_approved_package(..., feasibility_plan=plan)`。新编译包使用 TestPackage 2.2，并携带：

- `feasibility`：完整不可变可行性快照；
- `package_build.status=compiled`；
- 源设计指纹；
- 源可行性计划指纹。

Package Build 只转换表示，不改变用例行为、批准范围或执行依赖；行为变化必须回到 CASE DESIGN 重新审核和规划。

## 兼容性

- TestPackage 2.1：视为 legacy executable package，Runner 可继续执行已存在资产；不会伪造 FeasibilityPlan。
- TestPackage 2.2：新建包标准；必须携带有效 `feasibility` 和 `package_build`。
- 旧包不能通过修改版本号升级；必须从候选设计重新生成 FeasibilityPlan 并重新建包。
