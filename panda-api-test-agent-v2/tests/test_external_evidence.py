"""Tests for the external evidence registration entry.

These lock the three rules the registration exists for:
1. API-layer facts cannot be registered as external evidence;
2. ``passed`` without a concrete reference is rejected;
3. a ``partial``/``none`` AC only closes when the external registry actually says so.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import external_evidence as ee
from run_orchestrator import (
    OrchestrationError,
    build_requirement_manifest_for_runs,
    refresh_requirement_report,
)


def _run(root, run_id, tasks, *, criteria, verdict="passed", requirement_id="REQ-EXT"):
    root.mkdir(parents=True, exist_ok=True)
    plan = {
        "plan_id": "PLAN-EXT",
        "plan_fingerprint": "sha256:ext",
        "requirement_id": requirement_id,
        "tasks": [
            {"task_id": task["task_id"], "case_id": task["task_id"], "acceptance_refs": task.get("acceptance_refs", [])}
            for task in tasks
        ],
        "acceptance_criteria": criteria,
    }
    (root / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (root / "result.json").write_text(json.dumps({
        "run_id": run_id,
        "requirement_id": requirement_id,
        "verdict": verdict,
        "task_results": [
            {
                "task_id": task["task_id"],
                "status": task.get("status", "passed"),
                "evidence_refs": task.get("evidence_refs", []),
            }
            for task in tasks
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "run.json").write_text(json.dumps({
        "run_id": run_id,
        "started_at": "2026-11-02T10:00:00+08:00",
        "environment_snapshot": {"environment_id": "test"},
    }, ensure_ascii=False), encoding="utf-8")
    return root


class RegistrationRuleTests(unittest.TestCase):
    def test_api_layer_facts_must_come_from_a_run(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ee.ExternalEvidenceError, "immutable Run result"):
                ee.record_external_evidence(temp, "REQ-1", "AC-1", layer="http", status="passed",
                                            refs=["evidence/x.json"], provided_by="qa")

    def test_pass_without_a_reference_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ee.ExternalEvidenceError, "verbal claim"):
                ee.record_external_evidence(temp, "REQ-1", "AC-1", layer="ui", status="passed",
                                            refs=[], provided_by="qa")

    def test_blocked_may_be_recorded_without_a_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            record = ee.record_external_evidence(temp, "REQ-1", "AC-1", layer="ui", status="blocked",
                                                 provided_by="qa", note="测试环境无浏览器")
            self.assertEqual(record["refs"], [])

    def test_unknown_layer_and_status_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ee.ExternalEvidenceError, "unsupported external evidence layer"):
                ee.record_external_evidence(temp, "REQ-1", "AC-1", layer="telepathy", status="passed",
                                            refs=["r"], provided_by="qa")
            with self.assertRaisesRegex(ee.ExternalEvidenceError, "unsupported external evidence status"):
                ee.record_external_evidence(temp, "REQ-1", "AC-1", layer="ui", status="probably",
                                            refs=["r"], provided_by="qa")

    def test_provider_and_acceptance_scope_are_required(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ee.ExternalEvidenceError, "provided_by"):
                ee.record_external_evidence(temp, "REQ-1", "AC-1", layer="ui", status="passed",
                                            refs=["r"], provided_by="  ")
            with self.assertRaisesRegex(ee.ExternalEvidenceError, "ac_id"):
                ee.record_external_evidence(temp, "REQ-1", "", layer="ui", status="passed",
                                            refs=["r"], provided_by="qa")


class LedgerTests(unittest.TestCase):
    def test_ledger_is_append_only_and_supersedes(self):
        with tempfile.TemporaryDirectory() as temp:
            first = ee.record_external_evidence(temp, "REQ-1", "AC-1", layer="ui", status="failed",
                                                refs=["shot-1.png"], provided_by="qa",
                                                recorded_at="2026-11-02T10:00:00Z")
            second = ee.record_external_evidence(temp, "REQ-1", "AC-1", layer="ui", status="passed",
                                                 refs=["shot-2.png"], provided_by="qa",
                                                 recorded_at="2026-11-02T11:00:00Z")
            records = ee.load_external_evidence(temp, "REQ-1")
            self.assertEqual(len(records), 2, "历史登记不得被覆盖")
            self.assertEqual(second["supersedes"], first["evidence_id"])

            summary = ee.summarize_external_evidence(records)
            self.assertEqual(summary["per_ac"]["AC-1"]["status"], "passed")
            self.assertEqual(summary["per_ac"]["AC-1"]["layers"], {"ui": "passed"})
            self.assertEqual(summary["per_ac"]["AC-1"]["evidence_ids"], [second["evidence_id"]])
            self.assertEqual(summary["supports"], ["AC-1"])

    def test_a_failed_layer_cannot_be_averaged_away(self):
        with tempfile.TemporaryDirectory() as temp:
            ee.record_external_evidence(temp, "REQ-1", "AC-1", layer="ui", status="passed",
                                        refs=["shot.png"], provided_by="qa")
            ee.record_external_evidence(temp, "REQ-1", "AC-1", layer="db", status="failed",
                                        refs=["query.log"], provided_by="qa")
            summary = ee.summarize_external_evidence(ee.load_external_evidence(temp, "REQ-1"))
            self.assertEqual(summary["per_ac"]["AC-1"]["status"], "failed")
            self.assertEqual(summary["status"], "failed")

    def test_corrupted_ledger_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = ee.ledger_path(temp, "REQ-1")
            path.parent.mkdir(parents=True)
            path.write_text('{"evidence_id": "EXT-000000000000"}\nnot-json\n', encoding="utf-8")
            with self.assertRaisesRegex(ee.ExternalEvidenceError, "corrupted"):
                ee.load_external_evidence(temp, "REQ-1")

    def test_absent_ledger_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(ee.load_external_evidence(temp, "REQ-404"), [])


class AcceptanceClosureTests(unittest.TestCase):
    PARTIAL_AC = [{
        "ac_id": "AC1",
        "text": "订单落库且页面展示正确",
        "interface_verifiability": "partial",
        "non_interface_scope": ["页面展示"],
    }]

    def _partial_run(self, root):
        return _run(
            root / "RUN-1", "RUN-1",
            [{"task_id": "T1", "acceptance_refs": ["AC1"], "status": "passed", "evidence_refs": ["evidence/t1.json"]}],
            criteria=self.PARTIAL_AC,
        )

    def test_partial_cannot_pass_on_the_api_layer_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._partial_run(Path(temp))
            manifest = build_requirement_manifest_for_runs([root], requirement_id="REQ-EXT")
            item = manifest["acceptance_items"][0]
            self.assertEqual(item["interface_verifiability"], "partial")
            self.assertEqual(item["verdict"], "inconclusive")
            self.assertEqual(item["execution_status"], "partial")
            self.assertIn("外部验证层证据缺失", item["actual"])
            self.assertIn("缺少外部验证层证据", item["gaps"])

    def test_partial_closes_when_external_evidence_is_registered(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._partial_run(Path(temp))
            ee.record_external_evidence(temp, "REQ-EXT", "AC1", layer="ui", status="passed",
                                        refs=["shot/order-detail.png"], provided_by="qa",
                                        recorded_at="2026-11-02T10:30:00Z")
            manifest = build_requirement_manifest_for_runs([root], requirement_id="REQ-EXT", store_root=temp)
            item = manifest["acceptance_items"][0]
            self.assertEqual(item["verdict"], "passed")
            self.assertEqual(item["execution_status"], "complete")
            self.assertEqual(item["external_evidence"]["status"], "passed")
            self.assertEqual(item["external_evidence"]["refs"], ["shot/order-detail.png"])
            self.assertIn("EXT-", "".join(item["evidence_refs"]))
            self.assertEqual(manifest["release_recommendation"], "可进入下一阶段")

    def test_external_failure_overrides_an_api_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._partial_run(Path(temp))
            ee.record_external_evidence(temp, "REQ-EXT", "AC1", layer="ui", status="failed",
                                        refs=["shot/broken-page.png"], provided_by="qa")
            manifest = build_requirement_manifest_for_runs([root], requirement_id="REQ-EXT", store_root=temp)
            item = manifest["acceptance_items"][0]
            self.assertEqual(item["verdict"], "failed")
            self.assertEqual(item["execution_status"], "complete")
            self.assertEqual(manifest["release_recommendation"], "阻断发布；修复失败项后补测")

    def test_none_acceptance_item_is_closed_only_by_external_evidence(self):
        criteria = [{"ac_id": "AC2", "text": "页面展示订单", "interface_verifiability": "none",
                     "recommended_validation_layers": ["UI"]}]
        with tempfile.TemporaryDirectory() as temp:
            root = _run(Path(temp) / "RUN-1", "RUN-1", [], criteria=criteria)
            baseline = build_requirement_manifest_for_runs([root], requirement_id="REQ-EXT")
            self.assertEqual(baseline["acceptance_items"][0]["verdict"], "not_run")

            ee.record_external_evidence(temp, "REQ-EXT", "AC2", layer="ui", status="passed",
                                        refs=["shot/list.png"], provided_by="qa")
            closed = build_requirement_manifest_for_runs([root], requirement_id="REQ-EXT", store_root=temp)
            self.assertEqual(closed["acceptance_items"][0]["verdict"], "passed")

            result = refresh_requirement_report(
                [root],
                requirement_id="REQ-EXT",
                store_root=temp,
                output_json=Path(temp) / "requirements" / "REQ-EXT" / "requirement-report.json",
                output_html=Path(temp) / "reports" / "REQ-EXT-acceptance-current.html",
            )
            self.assertEqual(result["verdict"], "PASS")
            self.assertTrue(Path(result["report"]).is_file())
            self.assertIn("shot/list.png", Path(result["report"]).read_text(encoding="utf-8"))

    def test_evidence_for_an_unknown_acceptance_item_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._partial_run(Path(temp))
            ee.record_external_evidence(temp, "REQ-EXT", "AC-999", layer="ui", status="passed",
                                        refs=["shot/x.png"], provided_by="qa")
            with self.assertRaisesRegex(OrchestrationError, "AC-999"):
                build_requirement_manifest_for_runs([root], requirement_id="REQ-EXT", store_root=temp)

    def test_partial_report_is_renderable_only_with_both_layers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._partial_run(Path(temp))
            manifest = build_requirement_manifest_for_runs([root], requirement_id="REQ-EXT")
            result = refresh_requirement_report(
                [root],
                requirement_id="REQ-EXT",
                store_root=temp,
                output_json=Path(temp) / "report.json",
                output_html=Path(temp) / "report.html",
            )
            self.assertEqual(result["verdict"], "INCONCLUSIVE")
            self.assertEqual(manifest["acceptance_items"][0]["verdict"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
