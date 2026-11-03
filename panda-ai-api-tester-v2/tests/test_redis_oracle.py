import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from composite_oracle_executor import CompositeOracleExecutorError, composite_oracle_executor
from redis_oracle import (
    RedisOracleError,
    execute_redis_oracle,
    redis_task_executor,
    validate_redis_oracle,
)
from runtime_context import RuntimeContext


def _oracle(**overrides):
    value = {
        "oracle_id": "OR-REDIS-SESSION",
        "kind": "redis",
        "binding": "uat_redis",
        "command": "HGETALL",
        "args": ["session:${session_id}"],
        "max_items": 10,
        "max_bytes": 4096,
        "assertions": [
            {"assertion_id": "A-R-1", "source": "field.status", "operator": "equals", "expected": "ACTIVE"},
            {"assertion_id": "A-R-2", "source": "item_count", "operator": "in_range", "expected": {"min": 1, "max": 5}},
        ],
    }
    value.update(overrides)
    return value


class FakeRedis:
    def __init__(self, values):
        self.values = values
        self.calls = []
        self.closed = False

    def execute_command(self, command, *args):
        self.calls.append((command, args))
        return self.values.get((command, args))

    def close(self):
        self.closed = True


class RedisOracleTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.context = RuntimeContext.from_mapping(
            "test-env",
            {
                "uat_redis": {"host": "127.0.0.1", "port": 6379, "db": 1},
                "session_id": "S-1",
            },
            secret_keys={"uat_redis"},
        )

    def test_validates_read_commands_and_rejects_unsafe_commands(self):
        self.assertEqual(validate_redis_oracle(_oracle())["command"], "HGETALL")
        for command in ("SET", "DEL", "KEYS", "EVAL", "FLUSHALL"):
            with self.assertRaisesRegex(RedisOracleError, "not allowed"):
                validate_redis_oracle(_oracle(command=command))

    def test_executes_with_client_factory_and_records_verbatim_evidence(self):
        client = FakeRedis({("HGETALL", ("session:S-1",)): {b"status": b"ACTIVE", b"owner": b"alice"}})
        fact = execute_redis_oracle(
            _oracle(), self.context, evidence_dir=self.temp, client_factory=lambda _config: client
        )
        self.assertEqual(fact["status"], "passed")
        self.assertEqual(client.calls, [("HGETALL", ("session:S-1",))])
        self.assertTrue(client.closed)
        payload = json.loads(Path(fact["evidence_ref"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["result"]["owner"], "alice")
        self.assertEqual(payload["binding"], "uat_redis")

    def test_mismatch_is_failed(self):
        client = FakeRedis({("HGETALL", ("session:S-1",)): {"status": "EXPIRED"}})
        fact = execute_redis_oracle(_oracle(), self.context, client_factory=lambda _config: client)
        self.assertEqual(fact["status"], "failed")

    def test_missing_binding_and_driver_failure_are_not_enabled(self):
        missing = RuntimeContext.from_mapping("test-env", {"session_id": "S-1"})
        self.assertEqual(execute_redis_oracle(_oracle(), missing)["status"], "not_enabled")

        def unavailable(_config):
            raise ConnectionError("redis unavailable")

        self.assertEqual(
            execute_redis_oracle(_oracle(), self.context, client_factory=unavailable)["status"],
            "not_enabled",
        )

    def test_unknown_variable_and_limits_are_errors(self):
        context = RuntimeContext.from_mapping("test-env", {"uat_redis": {"host": "localhost"}}, secret_keys={"uat_redis"})
        self.assertEqual(execute_redis_oracle(_oracle(), context, client_factory=lambda _config: FakeRedis({}))["status"], "error")
        client = FakeRedis({("SMEMBERS", ("large",)): {"a", "b", "c"}})
        fact = execute_redis_oracle(
            _oracle(command="SMEMBERS", args=["large"], max_items=2, assertions=[{"assertion_id": "A", "source": "item_count", "operator": "equals", "expected": 3}]),
            self.context,
            client_factory=lambda _config: client,
        )
        self.assertEqual(fact["status"], "error")
        client = FakeRedis({("GET", ("large",)): "x" * 100})
        fact = execute_redis_oracle(
            _oracle(command="GET", args=["large"], max_bytes=10, assertions=[{"assertion_id": "A", "source": "result", "operator": "exists", "expected": True}]),
            self.context,
            client_factory=lambda _config: client,
        )
        self.assertEqual(fact["status"], "error")

    def test_task_and_composite_executors_dispatch_only_explicit_builtins(self):
        client = FakeRedis({("HGETALL", ("session:S-1",)): {"status": "ACTIVE"}})
        task = {"task_id": "OR-R", "type": "oracle", "oracle_type": "redis", **_oracle()}
        fact = redis_task_executor(client_factory=lambda _config: client)(task, runtime_context=self.context)
        self.assertEqual(fact["status"], "passed")

        composite = composite_oracle_executor(redis_client_factory=lambda _config: FakeRedis({("HGETALL", ("session:S-1",)): {"status": "ACTIVE"}}))
        self.assertEqual(composite(task, runtime_context=self.context)["status"], "passed")
        with self.assertRaises(CompositeOracleExecutorError):
            composite({"task_id": "OR-X", "type": "oracle", "oracle_type": "manual", "verification": {"query": "x"}}, runtime_context=self.context)


if __name__ == "__main__":
    unittest.main()
