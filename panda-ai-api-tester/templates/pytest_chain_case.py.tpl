"""多接口链路模板：登录 -> 取 token -> 下单 -> 取 orderId -> 支付。"""
import pytest

from api_client import ApiClient


@pytest.mark.api
@pytest.mark.chain
class TestOrderPaymentChain:
    """链路变量只在当前测试内传递，任一步失败即停止后续步骤。"""

    def test_login_create_order_pay(self, api_client: ApiClient, test_data_factory):
        profiles = test_data_factory.load_chain_profiles("order_payment")

        login = api_client.post(profiles["login"]["path"], json=test_data_factory.login_data())
        assert login.status_code == profiles["login"]["expected_http_status"]
        login_body = login.json()
        assert login_body["code"] == profiles["login"]["expected_business_code"]
        token = login_body.get("data", {}).get("token")
        assert token, "登录响应 data.token 不得为空"

        api_client.set_token(token)
        order = api_client.post(
            profiles["create_order"]["path"],
            json=test_data_factory.order_data(),
        )
        assert order.status_code == profiles["create_order"]["expected_http_status"]
        order_body = order.json()
        assert order_body["code"] == profiles["create_order"]["expected_business_code"]
        order_id = order_body.get("data", {}).get("orderId")
        assert order_id, "下单响应 data.orderId 不得为空"

        payment = api_client.post(
            profiles["pay"]["path"],
            json={"orderId": order_id, "payMethod": "balance"},
        )
        assert payment.status_code == profiles["pay"]["expected_http_status"]
        payment_body = payment.json()
        assert payment_body["code"] == profiles["pay"]["expected_business_code"]
        assert payment_body.get("data", {}).get("paymentId"), "paymentId 不得为空"
