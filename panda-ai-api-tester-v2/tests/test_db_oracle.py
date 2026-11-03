import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from db_oracle import (
    DbOracleError,
    db_task_executor,
    declaration_from_task,
    execute_db_oracle,
    validate_db_oracle,
)
from plan_compiler import PlanCompileError, compile_plan
from runtime_context import RuntimeContext


def _oracle(**overrides):
    base = {
        "oracle_id": "OR-DB-ORDER-PERSIST",
        "kind": "db",
        "binding": "uat_db",
        "statement": "SELECT order_no, status, amount, phone FROM orders WHERE order_no = ${order_no}",
        "max_rows": 10,
        "assertions": [
            {"assertion_id": "A-DB-1", "source": "row_count", "operator": "in_range", "expected": {"min": 1, "max": 1}},
            {"assertion_id": "A-DB-2", "source": "row[0].status", "operator": "equals", "expected": "CREATED"},
        ],
    }
    base.update(overrides)
    return base


class ValidateDbOracleTests(unittest.TestCase):
    def test_accepts_valid_declaration(self):
        normalized = validate_db_oracle(_oracle())
        self.assertEqual(normalized["binding"], "uat_db")
        self.assertEqual(normalized["side_effect"], "read")
        self.assertEqual(len(normalized["assertions"]), 2)

    def test_rejects_non_select_and_writes(self):
        for statement in (
            "UPDATE orders SET status = 'X' WHERE order_no = 'A'",
            "INSERT INTO orders VALUES (1)",
            "DELETE FROM orders WHERE 1=1",
            "SELECT 1; DROP TABLE orders",
            "SELECT order_no FROM orders WHERE order_no = 'A' -- trick",
            "SELECT order_no INTO OUTFILE '/tmp/x' FROM orders WHERE 1=1",
            "SELECT * FROM orders WHERE order_no = 'A'",
            "SELECT order_no FROM orders",
        ):
            with self.assertRaisesRegex(DbOracleError, "SELECT|WHERE|forbidden|comments|statements"):
                validate_db_oracle(_oracle(statement=statement))

    def test_rejects_bad_binding_and_assertions(self):
        with self.assertRaises(DbOracleError):
            validate_db_oracle(_oracle(binding="1bad"))
        with self.assertRaisesRegex(DbOracleError, "unsupported operator"):
            validate_db_oracle(_oracle(assertions=[{"assertion_id": "A", "source": "row_count", "operator": "regex", "expected": 1}]))
        with self.assertRaisesRegex(DbOracleError, "not meaningful for row_count"):
            validate_db_oracle(_oracle(assertions=[{"assertion_id": "A", "source": "row_count", "operator": "exists", "expected": True}]))
        with self.assertRaisesRegex(DbOracleError, "max_rows"):
            validate_db_oracle(_oracle(max_rows=0))
        with self.assertRaisesRegex(DbOracleError, "max_rows"):
            validate_db_oracle(_oracle(max_rows=99999))


class ExecuteDbOracleTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.db_path = tmp / "orders.db"
        self.evidence_dir = tmp / "evidence"
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("CREATE TABLE orders (order_no TEXT, status TEXT, amount REAL, phone TEXT)")
            connection.execute("INSERT INTO orders VALUES ('ORD-1', 'CREATED', 99.5, '13800138000')")
            connection.execute("INSERT INTO orders VALUES ('ORD-2', 'SHIPPED', 10.0, '13900139000')")
            connection.commit()
        finally:
            connection.close()
        self.context = RuntimeContext.from_mapping(
            "test-env",
            {"uat_db": f"sqlite:///{self.db_path}", "order_no": "ORD-1"},
            secret_keys={"uat_db"},
        )

    def _run(self, overrides=None):
        return execute_db_oracle(_oracle(**(overrides or {})), self.context, evidence_dir=self.evidence_dir)

    def test_passes_and_writes_verbatim_evidence(self):
        fact = self._run()
        self.assertEqual(fact["status"], "passed")
        self.assertEqual(fact["row_count"], 1)
        self.assertTrue(fact["evidence_ref"] and Path(fact["evidence_ref"]).is_file())
        payload = json.loads(Path(fact["evidence_ref"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["rows"][0]["phone"], "13800138000")
        self.assertEqual(payload["binding"], "uat_db")

    def test_structured_sqlite_config_is_supported_without_mutation(self):
        config = {"driver": "sqlite", "path": str(self.db_path), "options": {"mode": "ro"}}
        context = RuntimeContext.from_mapping(
            "test-env", {"uat_db": config, "order_no": "ORD-1"}, secret_keys={"uat_db"}
        )
        fact = execute_db_oracle(_oracle(), context)
        self.assertEqual(fact["status"], "passed")
        self.assertEqual(config["options"]["mode"], "ro")

    def test_data_mismatch_is_failed_not_error(self):
        fact = self._run({"assertions": [{"assertion_id": "A", "source": "row[0].status", "operator": "equals", "expected": "SHIPPED"}]})
        self.assertEqual(fact["status"], "failed")
        self.assertEqual(fact["assertion_results"][0]["status"], "failed")

    def test_missing_binding_is_not_enabled(self):
        context = RuntimeContext.from_mapping("test-env", {"order_no": "ORD-1"})
        fact = execute_db_oracle(_oracle(), context)
        self.assertEqual(fact["status"], "not_enabled")

    def test_connection_failure_is_not_enabled(self):
        missing = self.db_path.parent / "missing.db"
        context = RuntimeContext.from_mapping("test-env", {"uat_db": f"sqlite:///{missing}", "order_no": "ORD-1"}, secret_keys={"uat_db"})
        fact = execute_db_oracle(_oracle(), context)
        self.assertEqual(fact["status"], "not_enabled")

    def test_unknown_variable_is_error(self):
        context = RuntimeContext.from_mapping("test-env", {"uat_db": f"sqlite:///{self.db_path}"}, secret_keys={"uat_db"})
        fact = execute_db_oracle(_oracle(), context)
        self.assertEqual(fact["status"], "error")

    def test_result_set_beyond_max_rows_is_error(self):
        context = RuntimeContext.from_mapping(
            "test-env", {"uat_db": f"sqlite:///{self.db_path}", "order_no": "ORD-"}, secret_keys={"uat_db"}
        )
        statement = "SELECT order_no, status FROM orders WHERE order_no LIKE ${order_no} || '%'"
        fact = execute_db_oracle(_oracle(statement=statement, max_rows=1), context)
        self.assertEqual(fact["status"], "error")

    def test_task_executor_handles_unified_oracle_task(self):
        task = {"task_id": "OR-1", "type": "oracle", "oracle_type": "db", **_oracle()}
        executor = db_task_executor(evidence_dir=self.evidence_dir)
        fact = executor(task, runtime_context=self.context)
        self.assertEqual(fact["status"], "passed")
        self.assertEqual(declaration_from_task(task)["binding"], "uat_db")
        with self.assertRaises(DbOracleError):
            executor({"task_id": "OR-2", "type": "oracle", "oracle_type": "manual"}, runtime_context=self.context)


class PlanCompilerDbOracleTests(unittest.TestCase):
    def _intent(self, oracle_task):
        return {
            "requirement_id": "REQ-DB-ORACLE",
            "entry_type": "api_debug",
            "acceptance_criteria": [
                {"ac_id": "AC-1", "text": "order persisted", "interface_verifiability": "direct", "verifiability_decision_status": "ai_decided", "verifiability_decision_owner": "ai", "decision_evidence_refs": ["debug:explicit-minimal-reproduction"]}
            ],
            "tasks": [
                {"task_id": "BIZ-1", "type": "business", "method": "POST", "request": {"method": "POST", "path": "/orders"}, "object_ref": "order:${order_no}"},
                {**oracle_task, "depends_on": ["BIZ-1"]},
            ],
        }

    def test_valid_db_oracle_compiles(self):
        intent = self._intent({"task_id": "OR-1", "type": "oracle", "oracle_type": "db", **_oracle()})
        plan = compile_plan(intent)
        oracle_task = next(task for task in plan["tasks"] if task["task_id"] == "OR-1")
        self.assertEqual(oracle_task["type"], "oracle")
        self.assertEqual(oracle_task["evidence_kind"], "oracle_verification")
        self.assertEqual(oracle_task["builtin_oracle_executor"], "db")

    def test_invalid_db_oracle_statement_is_refused_at_compile_time(self):
        intent = self._intent({"task_id": "OR-1", "type": "oracle", "oracle_type": "db", **_oracle(statement="DELETE FROM orders WHERE order_no = 'A'")})
        with self.assertRaisesRegex(PlanCompileError, "SELECT|forbidden"):
            compile_plan(intent)


if __name__ == "__main__":
    unittest.main()
