import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from plan_compiler import PlanCompileError, compile_plan


def base_intent(tasks, mode="EXECUTE"):
    return {
        "requirement_id": "REQ-1",
        "mode": mode,
        "acceptance_criteria": [{"ac_id": "AC-1", "text": "business result"}],
        "tasks": tasks,
    }


class PlanCompilerTests(unittest.TestCase):
    def test_compiles_levels_and_appends_reduce(self):
        plan = compile_plan(base_intent([
            {"task_id": "AUTH", "type": "probe", "method": "GET", "side_effect": "read"},
            {"task_id": "READ-A", "type": "business", "method": "GET", "depends_on": ["AUTH"], "side_effect": "read"},
            {"task_id": "READ-B", "type": "business", "method": "GET", "depends_on": ["AUTH"], "side_effect": "read"},
        ]))
        self.assertEqual([item["slice_id"] for item in plan["slices"]], ["S1", "S2", "S3"])
        self.assertEqual(plan["slices"][1]["execution"], "parallel")
        self.assertEqual(plan["slices"][-1]["type"], "reduce")
        self.assertTrue(plan["plan_fingerprint"].startswith("sha256:"))

    def test_rejects_unknown_dependency_and_cycle(self):
        with self.assertRaisesRegex(PlanCompileError, "unknown dependencies"):
            compile_plan(base_intent([{ "task_id": "A", "depends_on": ["MISSING"], "method": "GET" }]))
        with self.assertRaisesRegex(PlanCompileError, "cycle"):
            compile_plan(base_intent([
                {"task_id": "A", "depends_on": ["B"], "method": "GET"},
                {"task_id": "B", "depends_on": ["A"], "method": "GET"},
            ]))

    def test_rejects_sensitive_headers_and_probe_write(self):
        with self.assertRaisesRegex(PlanCompileError, "sensitive"):
            compile_plan(base_intent([{ "task_id": "A", "method": "GET", "request": {"headers": {"Authorization": "inline"}} }]))
        with self.assertRaisesRegex(PlanCompileError, "read-only"):
            compile_plan(base_intent([{ "task_id": "A", "type": "probe", "method": "POST", "side_effect": "write" }], mode="PROBE"))

    def test_write_requires_cleanup_or_resource_tracking_and_destructive_approval(self):
        with self.assertRaisesRegex(PlanCompileError, "requires teardown"):
            compile_plan(base_intent([{ "task_id": "A", "type": "business", "method": "POST", "side_effect": "write" }]))
        plan = compile_plan(base_intent([{ "task_id": "A", "type": "business", "method": "DELETE", "side_effect": "destructive", "object_ref": "order-1" }]))
        self.assertTrue(plan["tasks"][0]["approval"]["required"])
        self.assertIn("destructive", plan["tasks"][0]["approval"]["reasons"])

    def test_rejects_candidate_and_unresolved_semantic_decisions(self):
        candidate = base_intent([{"task_id": "A", "method": "GET", "applicability": {"status": "pending_ai_decision", "candidate_only": True}}])
        candidate["entry_type"] = "single_api_quality"
        candidate["scope"] = {"dimension_decision_status": "pending_ai_decision"}
        with self.assertRaisesRegex(PlanCompileError, "unresolved semantic decision"):
            compile_plan(candidate)

        unresolved = base_intent([{"task_id": "A", "method": "GET", "applicability": {"status": "ai_decided", "decision_owner": "ai", "candidate_only": False}}])
        unresolved["entry_type"] = "single_api_quality"
        with self.assertRaisesRegex(PlanCompileError, "decision_evidence_refs"):
            compile_plan(unresolved)

    def test_requires_acceptance_decision_evidence_for_semantic_task(self):
        intent = base_intent([{"task_id": "A", "method": "GET", "acceptance_refs": ["AC-1"]}])
        intent["entry_type"] = "requirement_validation"
        intent["acceptance_criteria"][0].update({"interface_verifiability": "direct", "verifiability_decision_status": "ai_decided", "verifiability_decision_owner": "ai"})
        with self.assertRaisesRegex(PlanCompileError, "decision_evidence_refs"):
            compile_plan(intent)

    def test_accepts_auditable_semantic_task(self):
        intent = base_intent([{"task_id": "A", "method": "GET", "acceptance_refs": ["AC-1"], "applicability": {"status": "ai_decided", "decision_owner": "ai", "decision_evidence_refs": ["profile:orders@1"], "candidate_only": False}}])
        intent["entry_type"] = "single_api_quality"
        intent["acceptance_criteria"][0].update({"interface_verifiability": "direct", "verifiability_decision_status": "ai_decided", "verifiability_decision_owner": "ai", "decision_evidence_refs": ["prd:REQ-1#AC-1"]})
        plan = compile_plan(intent)
        self.assertEqual(plan["tasks"][0]["task_id"], "A")

    def test_fixture_oracle_cleanup_tasks_have_explicit_contracts(self):
        plan = compile_plan(base_intent([
            {"task_id": "FIX", "type": "fixture", "fixture_name": "order", "action": "create"},
            {"task_id": "BUS", "type": "business", "method": "GET", "side_effect": "read", "depends_on": ["FIX"]},
            {"task_id": "ORACLE", "type": "oracle", "oracle_type": "db", "verification": {"query": "order"}, "depends_on": ["BUS"]},
            {"task_id": "CLEAN", "type": "cleanup", "object_ref": "order-1", "action": "delete", "depends_on": ["ORACLE"]},
        ]))
        by_id = {task["task_id"]: task for task in plan["tasks"]}
        self.assertEqual(by_id["FIX"]["evidence_kind"], "fixture")
        self.assertEqual(by_id["ORACLE"]["evidence_kind"], "oracle_verification")
        self.assertEqual(by_id["CLEAN"]["evidence_kind"], "cleanup")
        self.assertIn("CLEAN", plan["cleanup_obligations"]["task_ids"])

    def test_special_task_requires_its_declared_contract(self):
        with self.assertRaisesRegex(PlanCompileError, "fixture_name"):
            compile_plan(base_intent([{ "task_id": "FIX", "type": "fixture" }]))
        with self.assertRaisesRegex(PlanCompileError, "oracle_type"):
            compile_plan(base_intent([{ "task_id": "ORACLE", "type": "oracle" }]))
        with self.assertRaisesRegex(PlanCompileError, "object_ref"):
            compile_plan(base_intent([{ "task_id": "CLEAN", "type": "cleanup" }]))


if __name__ == "__main__":
    unittest.main()
