"""单接口测试用例模板：读取 L1 画像，发请求并执行三重断言。"""
import os

import pytest

from api_client import ApiClient


@pytest.mark.api
@pytest.mark.order
class TestCreateOrder:
    """示例接口；路径、字段和预期值应由 interface-profile 替换。"""

    def test_create_order_success(self, api_client: ApiClient, test_data_factory):
        profile = test_data_factory.load_interface_profile("create_order")
        payload = test_data_factory.build_pre_data(profile)

        response = api_client.post(profile["path"], json=payload)
        body = response.json()

        # 三重断言：HTTP 状态码 + 业务 code + 关键字段。
        assert response.status_code == profile["expected_http_status"]
        assert body["code"] == profile["expected_business_code"]
        order_id = body.get("data", {}).get("orderId")
        assert order_id, "data.orderId 不得为空"

        if os.getenv("ENABLE_DB_ASSERT", "false").lower() == "true":
            row = test_data_factory.find_order(order_id)
            assert row is not None, "订单未落库"
            assert row["order_id"] == order_id
