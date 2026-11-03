# 测试设计审核规范（一期）

## 目标

`EXECUTE` 业务请求必须先完成测试设计审核。人工审核位于 Feasibility/Data Plan 之后、Package Build 之前，审核候选用例、依赖矩阵、覆盖边界和测试行为。审核绑定当前设计的行为指纹，避免审核后扩大用例范围、修改断言或改变副作用而继续执行。

## TestPackage 审核字段

```json
{
  "review": {
    "status": "pending_review|approved|partially_approved|rejected|plan_only|expired|superseded",
    "review_id": "REVIEW-20261030-001",
    "plan_fingerprint": "sha256:<64 hex>",
    "reviewer": "user",
    "approved_at": "2026-10-30T23:30:00+08:00",
    "approved_scope": ["TC-001", "TC-002"],
    "reviewed_fields": {
      "TC-001": ["request", "assertions", "side_effect", "cleanup"]
    },
    "accepted_gaps": [],
    "comment": null,
    "expires_at": null
  }
}
```

`review.status` 只有 `approved` 或 `partially_approved` 时才可能进入 Package Build 和发送请求。审核批准不等于依赖自动闭合：如果批准范围仍包含 `blocked` 依赖或 `candidate_only` 用例，最终 `GATE-FEASIBILITY` 必须失败并阻止建包；如果只批准依赖已闭合的子集，则允许该子集建包，范围外用例保持 `not_run`/未覆盖。`approved` 表示当前设计中的全部具体用例获批；`partially_approved` 只允许执行 `approved_scope` 内用例，范围外用例必须为 `not_run`。`pending_review`、`rejected`、`plan_only`、`expired` 和 `superseded` 均不得执行。任何会发送 HTTP 请求的 `PROBE` 同样必须有审核记录；只有纯本地静态检查可以免于审核。`PROMOTE` 仍不属于 Runner 执行模式。

## 审核门禁

Package Build 先按审核批准范围计算最终 `GATE-FEASIBILITY`；Runner 在发送第一个业务请求前重新计算 `plan_fingerprint`，并校验：

- 审核状态已通过；
- `review.plan_fingerprint` 与当前包一致；
- 审核人、审核时间、审核范围存在且审核未过期；
- `approved` 审核范围覆盖当前包全部用例，`partially_approved` 仅覆盖允许执行的用例；
- 每个批准用例必须存在非空 `reviewed_fields`，记录实际审核的设计字段；
- `accepted_gaps` 都存在于 `known_gaps`，且只能接受非阻塞缺口；
- 生产、删除、批量和真实外部动作仍按既有高风险规则确认或阻断。

失败时返回 `TEST_DESIGN_REVIEW_REQUIRED` 或 `TEST_PLAN_FINGERPRINT_MISMATCH`，不得发送业务请求。仅审核状态/范围元数据变化不改变计划指纹；任何影响行为的设计字段变化都会导致指纹不匹配，审核自动失效。

## 计划指纹

计划指纹由 TestPackage 中会影响测试行为的字段按 JSON key 排序、紧凑序列化后计算 SHA-256。排除 `review` 元数据以及 `plan.fingerprint` / `plan.plan_fingerprint` 自引用字段。动态 Token、Cookie、密码、响应、运行时间和运行结果不得进入计划包。

测试包变更以下内容会使指纹变化并要求重新审核：环境身份、用例 ID/标题/范围、AC/规则/场景映射、请求方法/路径/参数变量、依赖、断言、副作用、数据标签、清理策略和已知缺口。

## 标题级预览

审核前默认只展示：需求、环境、执行模式、AC 设计覆盖、计划用例数、预计请求数、写入/只读数量、已知缺口和 `case_id + title + AC 映射 + 纳入状态`。请求体、断言表达式、响应、凭据和完整 DAG 仅在用户主动展开单条用例时展示，并保持脱敏。
