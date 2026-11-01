"""pytest fixtures：DB/Redis 只读 .env，连接失败必须显式报错。"""
import os
from contextlib import closing

import pytest
from dotenv import load_dotenv
from redis import Redis

from api_client import ApiClient

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}，请在测试环境 .env 中配置")
    return value


@pytest.fixture(scope="session")
def db_session():
    """session 级数据库连接；必须是测试库，连接失败不得 silent pass。"""
    if os.getenv("ENABLE_DB_ASSERT", "false").lower() != "true":
        yield None
        return

    database_url = _required("TEST_DATABASE_URL")
    if "prod" in database_url.lower():
        raise RuntimeError("拒绝连接疑似生产数据库，请仅配置测试库")
    try:
        import psycopg2
        connection = psycopg2.connect(database_url)
        yield connection
    except Exception as exc:
        raise RuntimeError(f"测试数据库连接失败: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()


@pytest.fixture(scope="session")
def redis_client():
    """session 级 Redis 连接；使用测试前缀避免污染其他数据。"""
    if os.getenv("ENABLE_REDIS_ASSERT", "false").lower() != "true":
        yield None
        return

    redis_url = _required("TEST_REDIS_URL")
    try:
        client = Redis.from_url(redis_url, socket_timeout=10, decode_responses=True)
        client.ping()
        yield client
    except Exception as exc:
        raise RuntimeError(f"测试 Redis 连接失败: {exc}") from exc
    finally:
        if "client" in locals():
            client.close()


@pytest.fixture
def api_client():
    return ApiClient()


@pytest.fixture
def test_data_factory(db_session, redis_client):
    factory = TestDataFactory(db_session=db_session, redis_client=redis_client)
    try:
        yield factory
    finally:
        factory.cleanup_test_data()


class TestDataFactory:
    """示例工厂；工程实现应按 L1 profile 扩展，所有数据带 TEST 标识。"""

    def __init__(self, db_session, redis_client):
        self.db_session = db_session
        self.redis_client = redis_client
        self.created_ids = []
        self.created_keys = []

    def load_interface_profile(self, name):
        raise NotImplementedError(f"请从 L1 interface-profile 加载接口: {name}")

    def load_chain_profiles(self, name):
        raise NotImplementedError(f"请从 L1 interface-profile 加载链路: {name}")

    def build_pre_data(self, profile):
        return profile.get("pre_data", {})

    def login_data(self):
        return {"username": _required("TEST_USERNAME"), "password": _required("TEST_PASSWORD")}

    def order_data(self):
        return {"userId": _required("TEST_USER_ID"), "items": [{"skuId": "TEST-SKU", "quantity": 1}]}

    def find_order(self, order_id):
        if self.db_session is None:
            return None
        with closing(self.db_session.cursor()) as cursor:
            cursor.execute("SELECT order_id FROM orders WHERE order_id = %s", (order_id,))
            row = cursor.fetchone()
            return {"order_id": row[0]} if row else None

    def cleanup_test_data(self):
        # 实现必须按本次 run id / tag=TEST 精确清理，禁止模糊删除。
        pass
