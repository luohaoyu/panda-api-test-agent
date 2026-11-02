"""Regression tests for the review contract, approval write-back and report closure.

These cover the four breaks found in review:
1. the documented review envelope must be accepted by the orchestrator;
2. a human approval must be recordable on the run it belongs to;
3. a gate-blocked acceptance item must reach the BLOCKED overall verdict;
4. many runs must project into one renderable ``current`` requirement manifest.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
L2_SCRIPTS = Path(__file__).resolve().parents[2] / "panda-ai-api-tester-v2" / "scripts"
sys.path.insert(0, str(L2_SCRIPTS))
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import render_requirement_report as report
from plan_compiler import compile_plan
from requirement_validation import build_requirement_validation_intent
from review_adapter import canonical_review
from run_orchestrator import (
    OrchestrationError,
    build_requirement_manifest,
    build_requirement_manifest_for_runs,
    compile_and_create_run,
    next_action,
    record_review,
    reduce_run,
)
from slice_executor import execute_slice


def _criteria(*extra):
    base = [{
        "ac_id": "AC1",
        "text": "接口返回订单摘要",
        "interface_verifiability": "direct",
        "decision_evidence_refs": ["prd#AC1"],
        "tasks": [{"task_id": "T1", "type": "business", "method": "GET", "side_effect": "read"}],
    }]
    return base + list(extra)


def _intent(criteria=None, requirement_id="REQ-CLOSE"):
    return build_requirement_validation_intent(
        {"requirement_id": requirement_id, "acceptance_criteria": criteria or _criteria()},
        "local-test",
    )


def _documented_review(intent, status="approved", **overrides):
    """Build the review exactly as references/orchestration-rules.md §4.1 documents it."""
    plan = compile_plan(intent)
    case_ids = [task["case_id"] for task in plan["tasks"]]
    review = {
        "status": status,
        "review_id": "REVIEW-DOC-1",
        "design_fingerprint": plan["plan_fingerprint"],
        "reviewer": "user",
        "reviewed_at": "2026-11-02T00:00:00+08:00",
        "approved_case_ids": case_ids,
        "reviewed_fields": ["cases", "request_summaries", "assertions", "data_dependencies", "side_effects"],
        "comment": None,
        "expires_at": None,
    }
    review.update(overrides)
    return review, plan, case_ids


class DocumentedReviewContractTests(unittest.TestCase):
    def test_documented_envelope_compiles_a_run(self):
        intent = _intent()
        review, plan, _ = _documented_review(intent)
        intent["review"] = review
        root = Path(tempfile.mkdtemp()) / "RUN-DOC"
        created = compile_and_create_run(intent, root, run_id="RUN-DOC")
        self.assertEqual(created["next_action"]["action"], "execute_slice")
        self.assertEqual(created["review"]["approved_at"], "2026-11-02T00:00:00+08:00")
        self.assertEqual(created["review"]["approval_field_source"], "reviewed_at")
        self.assertEqual(created["review"]["fingerprint_source"], "design_fingerprint")
        self.assertEqual(created["review"]["reviewed_fields"], {case: review["reviewed_fields"] for case in created["review"]["approved_case_ids"]})
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["review_status"], "approved")

    def test_executor_side_aliases_still_work(self):
        plan = compile_plan(_intent())
        case_ids = [task["case_id"] for task in plan["tasks"]]
        canonical = canonical_review({
            "status": "approved",
            "approved_scope": case_ids,
            "plan_fingerprint": plan["plan_fingerprint"],
            "review_id": "R",
            "reviewer": "qa",
            "approved_at": "2026-11-02T00:00:00+08:00",
            "reviewed_fields": {case_id: ["request", "assertions"] for case_id in case_ids},
        }, case_ids)
        self.assertTrue(canonical["execution_allowed"], canonical["errors"])
        self.assertEqual(canonical["review"]["reviewed_fields"], {case_id: ["request", "assertions"] for case_id in case_ids})
        self.assertEqual(canonical["review"]["fingerprint_source"], "plan_fingerprint")

    def test_wrapped_reviewed_fields_are_unwrapped(self):
        plan = compile_plan(_intent())
        case_ids = [task["case_id"] for task in plan["tasks"]]
        canonical = canonical_review({
            "status": "approved",
            "approved_case_ids": case_ids,
            "design_fingerprint": plan["plan_fingerprint"],
            "review_id": "R",
            "reviewer": "qa",
            "reviewed_at": "2026-11-02T00:00:00+08:00",
            "reviewed_fields": {"cases": {case_id: ["request"] for case_id in case_ids}},
        }, case_ids)
        self.assertTrue(canonical["execution_allowed"], canonical["errors"])

    def test_fingerprint_mismatch_is_refused(self):
        intent = _intent()
        review, _, _ = _documented_review(intent, design_fingerprint="sha256:" + "0" * 64)
        intent["review"] = review
        root = Path(tempfile.mkdtemp()) / "RUN-BAD-FP"
        with self.assertRaisesRegex(OrchestrationError, "fingerprint"):
            compile_and_create_run(intent, root, run_id="RUN-BAD-FP")
        self.assertFalse(root.exists())

    def test_unknown_case_in_scope_is_refused(self):
        intent = _intent()
        review, plan, case_ids = _documented_review(intent, approved_case_ids=["NOT-A-CASE"])
        intent["review"] = review
        root = Path(tempfile.mkdtemp()) / "RUN-BAD-SCOPE"
        with self.assertRaisesRegex(OrchestrationError, "unknown case"):
            compile_and_create_run(intent, root, run_id="RUN-BAD-SCOPE")

    def test_partial_approval_must_be_a_strict_subset(self):
        criteria = _criteria()
        criteria[0]["tasks"].append({"task_id": "T2", "type": "business", "method": "GET", "side_effect": "read"})
        intent = _intent(criteria)
        review, plan, case_ids = _documented_review(intent, status="partially_approved")
        intent["review"] = review
        root = Path(tempfile.mkdtemp()) / "RUN-PARTIAL-ALL"
        with self.assertRaisesRegex(OrchestrationError, "strict subset"):
            compile_and_create_run(intent, root, run_id="RUN-PARTIAL-ALL")


class ApprovalWriteBackTests(unittest.TestCase):
    def _pending_run(self, run_id="RUN-WB"):
        intent = _intent()
        root = Path(tempfile.mkdtemp()) / run_id
        compile_and_create_run(intent, root, run_id=run_id)
        return intent, root

    def test_approval_unlocks_the_same_run(self):
        intent, root = self._pending_run()
        self.assertEqual(next_action(root)["action"], "request_approval")
        review, _, _ = _documented_review(intent)
        decision = record_review(root, review)
        self.assertEqual(decision["next_action"], "execute_slice")
        self.assertEqual(next_action(root)["action"], "execute_slice")
        executed = execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/t1.json"})
        self.assertEqual(executed["status"], "passed")
        run = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["run_id"], "RUN-WB")
        events = [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        recorded = [event for event in events if event.get("type") == "review_recorded"]
        self.assertEqual(len(recorded), 2)
        self.assertEqual(recorded[-1]["review_status"], "approved")

    def test_rejection_keeps_the_run_blocked(self):
        intent, root = self._pending_run("RUN-WB-REJ")
        decision = record_review(root, {"status": "rejected", "review_id": "R-REJ", "reviewer": "qa", "comment": "退回"})
        self.assertEqual(decision["next_action"], "review_not_executable")
        self.assertEqual(next_action(root)["action"], "review_not_executable")
        with self.assertRaises(Exception):
            execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed"})

    def test_reduced_run_cannot_be_reopened(self):
        intent, root = self._pending_run("RUN-WB-REDUCED")
        review, _, _ = _documented_review(intent)
        record_review(root, review)
        execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/t1.json"})
        reduce_run(root)
        with self.assertRaisesRegex(OrchestrationError, "already reduced"):
            record_review(root, review)

    def test_scope_may_not_drop_executed_cases(self):
        criteria = _criteria()
        criteria[0]["tasks"].append({"task_id": "T2", "type": "business", "method": "GET", "side_effect": "read"})
        intent = _intent(criteria)
        review, plan, case_ids = _documented_review(intent)
        root = Path(tempfile.mkdtemp()) / "RUN-WB-SHRINK"
        compile_and_create_run({**intent, "review": review}, root, run_id="RUN-WB-SHRINK")
        execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/t.json"})
        narrowed = dict(review, status="partially_approved", approved_case_ids=[case_ids[0]])
        with self.assertRaisesRegex(OrchestrationError, "already executed"):
            record_review(root, narrowed)


class ReportClosureTests(unittest.TestCase):
    def test_gate_blocked_item_reaches_blocked_verdict(self):
        root = Path(tempfile.mkdtemp()) / "RUN-BLOCKED"
        root.mkdir(parents=True)
        (root / "plan.json").write_text(json.dumps({
            "plan_id": "P-BLOCKED",
            "plan_fingerprint": "sha256:x",
            "requirement_id": "REQ-BLOCKED",
            "tasks": [{"task_id": "T1", "case_id": "T1", "acceptance_refs": ["AC1"]}],
            "acceptance_criteria": [{"ac_id": "AC1", "text": "接口返回订单", "interface_verifiability": "direct"}],
        }), encoding="utf-8")
        (root / "result.json").write_text(json.dumps({
            "run_id": "RUN-BLOCKED",
            "verdict": "blocked",
            "task_results": [{"task_id": "T1", "status": "blocked", "evidence_refs": []}],
            "evidence_closure": {},
        }), encoding="utf-8")
        (root / "run.json").write_text(json.dumps({
            "run_id": "RUN-BLOCKED",
            "started_at": "2026-11-02T00:00:00+08:00",
            "environment_snapshot": {"environment_id": "local-test"},
        }), encoding="utf-8")
        manifest = build_requirement_manifest(root, title="阻塞场景")
        item = manifest["acceptance_items"][0]
        self.assertEqual(item["verdict"], "blocked")
        self.assertEqual(item["execution_status"], "not_run")
        self.assertEqual(report.overall_verdict(manifest)[0], "BLOCKED")
        self.assertEqual(manifest["release_recommendation"], "阻断发布；先解除阻塞项")

    def test_release_recommendation_cannot_contradict_the_report(self):
        criteria = _criteria({"ac_id": "AC2", "text": "页面展示订单", "interface_verifiability": "none", "recommended_validation_layers": ["UI"]})
        intent = _intent(criteria)
        review, _, _ = _documented_review(intent)
        root = Path(tempfile.mkdtemp()) / "RUN-NONE-AC"
        compile_and_create_run({**intent, "review": review}, root, run_id="RUN-NONE-AC")
        execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/t1.json"})
        reduce_run(root)
        manifest = build_requirement_manifest(root, title="none 验收项")
        self.assertEqual(manifest["acceptance_items"][0]["verdict"], "passed")
        self.assertEqual(manifest["acceptance_items"][1]["verdict"], "not_run")
        verdict = report.overall_verdict(manifest)[0]
        self.assertEqual(verdict, "INCONCLUSIVE")
        self.assertNotEqual(manifest["release_recommendation"], "可进入下一阶段")
        self.assertEqual(manifest["release_recommendation"], report_recommendation_for(verdict))

    def test_manifest_carries_a_stable_generated_at(self):
        criteria = _criteria()
        intent = _intent(criteria)
        review, _, _ = _documented_review(intent)
        root = Path(tempfile.mkdtemp()) / "RUN-TS"
        compile_and_create_run({**intent, "review": review}, root, run_id="RUN-TS")
        execute_slice(root, "S1", task_executor=lambda task, **_: {"status": "passed", "evidence_ref": "evidence/t1.json"})
        reduce_run(root)
        manifest = build_requirement_manifest(root, title="时间戳")
        self.assertTrue(manifest["generated_at"])
        self.assertEqual(manifest["generated_at"], build_requirement_manifest(root, title="时间戳")["generated_at"])

    def test_multiple_runs_project_into_one_renderable_current_manifest(self):
        roots = []
        for run_id, status in (("RUN-AGG-1", "failed"), ("RUN-AGG-2", "passed")):
            intent = _intent(requirement_id="REQ-AGG")
            review, _, _ = _documented_review(intent)
            root = Path(tempfile.mkdtemp()) / run_id
            compile_and_create_run({**intent, "review": review}, root, run_id=run_id)
            execute_slice(root, "S1", task_executor=lambda task, **_: {"status": status, "evidence_ref": f"evidence/{run_id}.json"})
            reduce_run(root)
            roots.append(root)
        manifest = build_requirement_manifest_for_runs(roots, title="多运行 current")
        self.assertEqual(report.validate(manifest), [])
        self.assertEqual(manifest["report_id"], "AR-REQ-AGG-CURRENT")
        self.assertEqual(manifest["acceptance_items"][0]["verdict"], "passed")
        self.assertEqual(report.overall_verdict(manifest)[0], "PASS")
        self.assertEqual(manifest["aggregation"]["run_count"], 2)
        self.assertEqual({run["id"] for run in manifest["evidence_runs"]}, {"RUN-AGG-1", "RUN-AGG-2"})
        fixed = [issue for issue in manifest["issues"] if issue["type"] == "defect" and issue["status"] == "fixed"]
        self.assertTrue(fixed)
        self.assertIn("多运行 current", report.render(manifest))

    def test_aggregation_requires_matching_requirement(self):
        intent = _intent(requirement_id="REQ-EMPTY")
        review, _, _ = _documented_review(intent)
        root = Path(tempfile.mkdtemp()) / "RUN-EMPTY"
        compile_and_create_run({**intent, "review": review}, root, run_id="RUN-EMPTY")
        with self.assertRaises(OrchestrationError):
            build_requirement_manifest_for_runs([], requirement_id="REQ-EMPTY")


def report_recommendation_for(verdict):
    return {
        "FAIL": "阻断发布；修复失败项后补测",
        "BLOCKED": "阻断发布；先解除阻塞项",
        "PASS": "可进入下一阶段",
        "INCONCLUSIVE": "暂不可发布；补齐证据或未覆盖验收项",
    }[verdict]


if __name__ == "__main__":
    unittest.main()
