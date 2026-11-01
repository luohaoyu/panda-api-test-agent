# panda-api-test-agent-v2 自我评测用例

## 用例：自然语言“帮我测试订单”

文件：`order-natural-language-order-test.json`

该用例来自一次真实的测试环境整合运行，已将真实 URL、认证信息、业务个人信息和真实订单号全部替换为合成数据。它不是业务接口回归用例，而是 **主 Agent 编排能力和结果语义的技能自评测用例**。

## 评测目标

验证以下链路是否正确：

```text
“帮我测试订单”
→ 识别为测试环境订单创建
→ 生成 EXECUTE 计划
→ 受控执行业务写入
→ 记录订单对象和清理义务
→ 生成不可变证据
→ 归约最终结果
```

## 预期结果

- 业务任务：`passed`
- HTTP 状态：`200`
- 业务 `code`：`200`
- 订单号、运单号：均存在
- 运行时凭证：只存在于内存，不进入 package、events、result、evidence 或报告
- 不访问真实外部接口，只允许使用 stub/mock RuntimeContext
- 没有自动清理能力时：必须记录 pending object 和 residual risk
- 最终统一 Run/需求级 verdict：`inconclusive`，不得误报为 `passed`

## 使用边界

1. 该文件默认只作为自评测输入和回归契约，不会自行发送 HTTP 请求。
2. 执行自评测时必须使用本地 stub 或注入的假响应，并设置 `network_policy=stub_only`。
3. 禁止把真实 curl、Authorization、Basic 凭证、真实订单数据复制进该用例。
4. 如果后续已经实现可验证的自动清理和完整 Oracle，可在不改变原始场景语义的前提下新增版本化用例，不能直接把本用例的最终 verdict 改成无条件 PASS。

## 重点防回归问题

`DEF-REDUCER-CLEANUP-SYNC`：Slice Executor 已发现清理残留，而 Result Reducer 丢失该状态并输出 `passed`。自评测必须覆盖这一状态，确保业务成功和完整验收通过被严格区分。
