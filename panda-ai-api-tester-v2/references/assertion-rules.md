# 三重断言与扩展校验规范

接口测试的最低断言集合为三重断言：

1. **HTTP 状态码**：验证传输层状态是否符合画像，例如 `resp.status_code == 200`。
2. **业务 code**：验证响应体业务结果，例如 `body["code"] == 0`。
3. **关键字段**：验证核心结果存在且满足约束，例如 `data.orderId` 非空、类型正确。

启用数据校验时追加：

4. **DB 落库结果**：使用内建只读 DB Oracle 验证状态、金额、关联 ID 等关键列已持久化；运行绑定可为旧 DSN 字符串或结构化 `db_config`。
5. **缓存一致性**：使用内建只读 Redis Oracle 验证关键值、集合成员、排序分值或 TTL 与 DB/响应一致。第一版只允许 `GET/MGET/EXISTS/TYPE/TTL/PTTL/HGET/HMGET/HGETALL/SCARD/SMEMBERS/ZSCORE/ZRANGE/LRANGE`，禁止写命令、`KEYS` 和 `EVAL`；结果受 `max_items/max_bytes` 限制。

## 断言纪律

“**只断 200 = 假绿**”。HTTP 200 只能证明请求在协议层完成，不能证明业务成功。除非画像明确规定，否则任何接口用例不得只断言 `status_code == 200`。

断言顺序固定为：HTTP → JSON 结构 → 业务 code → 关键字段 → DB/Redis。前置断言失败时停止读取依赖字段，并在报告中保留实际响应摘要。

## 错误响应

异常、边界和鉴权用例同样执行 HTTP 与业务 code 断言，并验证错误字段或错误消息结构。预期错误码必须来自 L1 画像或配置，不凭经验替换。

## 安全与可诊断性

断言失败消息应包含字段路径、期望值和实际值；实际值按原样记录，token、cookie、密码、连接串与个人数据不再遮蔽，因此报告本身即敏感文件。DB/Redis 断言复用 Runner 的 `equals/not_equals/exists/all_present/contains/in_range` 语义；绑定/driver/连接不可用记 `not_enabled`，不得记为通过。DB/Redis 证据继续按原样记录，断言失败应明确标注数据源和查询范围，不打印完整凭据。
