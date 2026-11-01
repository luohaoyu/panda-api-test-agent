# 用例设计规则

本文件规定 AI API Tester 的四维度覆盖模型。每个目标接口至少形成一组正向用例，并根据画像和风险补齐异常、边界、鉴权用例。

| 维度 | 覆盖内容 | 例子 |
|---|---|---|
| **正向** | 正常参数、正常流程 | 合法 `userId` + `items` → HTTP 200，业务 `code == 0`，返回 `orderId` |
| **异常** | 参数缺失、类型错误、格式错误、越权 | 缺少 `items` → HTTP 400，业务码 `4001`；`quantity="x"` → 参数类型错误 |
| **边界** | 上限、下限、空值、长度、数量和分页临界点 | `quantity=最大值` 可接受；`quantity=0` 被拒绝；空字符串按契约处理 |
| **鉴权** | 无 token、过期 token、错误 token、资源越权 | 无 token → HTTP 401；用户 A 访问用户 B 订单 → HTTP 403 |

## 设计原则

1. 以 `interface-profile` 的请求参数、响应结构和鉴权要求为事实来源；只读画像，不自行发明业务规则，也不得在测试设计或执行期间修改画像。
2. 每个用例声明输入前置条件、请求、预期 HTTP 码、预期业务 code、关键字段和清理标识。
3. 异常与边界用例不因服务端返回 5xx 而自动判定为缺陷；先核对画像契约和错误处理约定。
4. 正向用例验证完整结果；不能只验证请求可达或 HTTP 200。
5. 敏感字段使用环境变量或脱敏样例，禁止写入真实凭据。
6. 实际响应与画像不一致时生成结构化 `contract_observation`：记录 operation、expected、actual、evidence_refs、candidate_classification、decider 和 next_action；确认前不得把差异自动归类为画像漂移或用实际值覆盖预期。

## 多接口链路变量传递

链路用例按步骤声明变量作用域和来源：

```text
登录响应 data.token      -> 下一步 Authorization: Bearer ${token}
下单响应 data.orderId    -> 支付请求体 orderId: ${order_id}
支付响应 data.paymentId  -> 查询请求路径 /payments/${payment_id}
```

规则如下：
- 上一步响应字段先通过 HTTP、业务 code 和关键字段断言，再写入链路上下文。
- 只传递画像中明确声明的字段；字段缺失时链路立即失败，不使用空值继续请求。
- 变量名使用 `snake_case`，在 pytest fixture 或测试函数作用域内显式保存。
- 每条链路记录步骤编号、输入来源和清理责任，避免跨测试共享可变数据。
- 链路中任一步失败，后续步骤标记为未执行；报告保留首个失败步骤及其断言差异。
