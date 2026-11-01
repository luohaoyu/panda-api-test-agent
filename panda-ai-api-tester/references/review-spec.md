# 测试设计审核规范（一期）

## 目标

`EXECUTE` 业务请求必须先完成测试设计审核。审核绑定当前 TestPackage 的行为指纹，避免审核后扩大用例范围、修改断言或改变副作用而继续执行。

## TestPackage 审核字段

```json
{
  "review": {
    "status": "review_pending|approved|approved_with_gaps|rejected|plan_only|expired|superseded",
    "review_id": "REVIEW-20261030-001",
    "plan_fingerprint": "sha256:<64 hex>",
    "reviewer": "user",
    "approved_at": "2026-10-30T23:30:00+08:00",
    "approved_scope": ["TC-001", "TC-002"],
    "accepted_gaps": [],
    "comment": null,
    "expires_at": null
  }
}
```

`review.status` 为 `approved` 或 `approved_with_gaps` 时才允许 EXECUTE。`PROBE` 可以没有审核记录，因为 PROBE 只做环境、鉴权和只读探测；`PROMOTE` 仍不属于 Runner 执行模式。

## 审核门禁

Runner 在发送第一个业务请求前重新计算 `plan_fingerprint`，并校验：

- 审核状态已通过；
- `review.plan_fingerprint` 与当前包一致；
- 审核人、审核时间、审核范围存在且审核未过期；
- 审核范围覆盖当前包全部用例；
- `accepted_gaps` 都存在于 `known_gaps`，且只能接受非阻塞缺口；
- 生产、删除、批量和真实外部动作仍按既有高风险规则确认或阻断。

失败时返回 `TEST_DESIGN_REVIEW_REQUIRED` 或 `TEST_PLAN_FINGERPRINT_MISMATCH`，不得发送业务请求。

## 计划指纹

计划指纹由 TestPackage 中会影响测试行为的字段按 JSON key 排序、紧凑序列化后计算 SHA-256。排除 `review` 元数据以及 `plan.fingerprint` / `plan.plan_fingerprint` 自引用字段。动态 Token、Cookie、密码、响应、运行时间和运行结果不得进入计划包。

测试包变更以下内容会使指纹变化并要求重新审核：环境身份、用例 ID/标题/范围、AC/规则/场景映射、请求方法/路径/参数变量、依赖、断言、副作用、数据标签、清理策略和已知缺口。

## 标题级预览

审核前默认只展示：需求、环境、执行模式、AC 设计覆盖、计划用例数、预计请求数、写入/只读数量、已知缺口和 `case_id + title + AC 映射 + 纳入状态`。请求体、断言表达式、响应、凭据和完整 DAG 仅在用户主动展开单条用例时展示，并保持脱敏。
