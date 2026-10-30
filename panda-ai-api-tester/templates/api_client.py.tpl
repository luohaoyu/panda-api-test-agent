"""统一 HTTP 客户端：从 .env 读取地址和 token，集中处理日志与业务断言。"""
import logging
import os
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


class ApiClient:
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        self.base_url = (base_url or os.getenv("BASE_URL", "")).rstrip("/")
        self.token = token or os.getenv("API_TOKEN")
        self.timeout = 10
        if not self.base_url:
            raise RuntimeError("缺少 BASE_URL，请在测试环境 .env 中配置")

    def set_token(self, token: str) -> None:
        self.token = token

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("headers", self._headers())
        logger.info("%s %s", method.upper(), path)
        response = requests.request(method, url, **kwargs)
        logger.info("response status=%s", response.status_code)
        return response

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", path, **kwargs)

    @staticmethod
    def assert_business_code(resp: requests.Response, expected: Any) -> Dict[str, Any]:
        """三重断言入口：HTTP、JSON、业务 code；关键字段由用例按画像断言。"""
        assert resp.status_code < 600, f"HTTP 响应无效: {resp.status_code}"
        body = resp.json()
        assert "code" in body, "响应缺少业务 code"
        assert body["code"] == expected, f"业务 code 不符: expected={expected}, actual={body['code']}"
        return body
