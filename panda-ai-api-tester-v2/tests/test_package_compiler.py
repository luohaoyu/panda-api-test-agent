import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from feasibility_plan import build_feasibility_plan  # noqa: E402
from package_compiler import (  # noqa: E402
    PackageCompileError,
    calculate_design_fingerprint,
    compile_approved_package,
)
from plan_fingerprint import calculate_plan_fingerprint  # noqa: E402


class ApprovedPackageCompilerTests(unittest.TestCase):
    def _design(self):
        design = {
            "schema_version": "2.1",
            "requirement_id": "REQ-ORDER-CHAIN",
            "mode": "EXECUTE",
            "project": {"workspace_id": "oms"},
            "cases": [
                {
                    "case_id": "CHAIN-001-CREATE",
                    "name": "create",
                    "request": {"method": "POST", "path": "/orders"},
                    "depends_on": [],
                    "assertions": [{"assertion_id": "A1", "business_rule_refs": ["BR-1"]}],
                    "side_effect": "write",
                    "object_ref": {"type": "runtime_order", "id": "${run_id}"},
                    "requirement_refs": ["REQ-ORDER-CHAIN"],
                    "business_rule_refs": ["BR-1"],
                    "execution_dependencies": [],
                },
                {
                    "case_id": "CHAIN-002-LABEL",
                    "name": "label",
                    "request": {"method": "GET", "path": "/labels?orderNo=${CHAIN-001-CREATE.waybillNo}"},
                    "depends_on": ["CHAIN-001-CREATE"],
                    "assertions": [{"assertion_id": "A2", "business_rule_refs": ["BR-1"]}],
                    "side_effect": "read",
                    "requirement_refs": ["REQ-ORDER-CHAIN"],
                    "business_rule_refs": ["BR-1"],
                    "execution_dependencies": [],
                },
            ],
            "requirements": [{"requirement_id": "REQ-ORDER-CHAIN"}],
            "business_rules": [{"rule_id": "BR-1"}],
            "scenarios": [],
            "known_gaps": [],
        }
        design["design_fingerprint"] = calculate_design_fingerprint(design)
        design["review"] = {
            "status": "approved",
            "approved_case_ids": ["CHAIN-001-CREATE", "CHAIN-002-LABEL"],
            "review_id": "REV-1",
            "reviewer": "user",
            "approved_at": "2026-11-01T10:00:00+08:00",
            "design_fingerprint": design["design_fingerprint"],
            "reviewed_fields": {
                "CHAIN-001-CREATE": ["request", "assertions", "side_effect"],
                "CHAIN-002-LABEL": ["request", "assertions", "side_effect"],
            },
        }
        design["package_template"] = {
            "project": {"workspace_id": "oms"},
            "test_config": {
                "path": "docs/test-config.md",
                "schema_version": "2.1",
                "config_revision": 1,
                "environment_id": "test",
                "fingerprint": "sha256:" + "1" * 64,
            },
            "contract": {
                "profile_path": "docs/interface-profile/orders.md",
                "governance_schema_version": "1.0",
                "profile_id": "oms:orders",
                "profile_revision": 1,
                "code_baseline": "commit",
                "lifecycle": "confirmed",
                "document_fingerprint": "sha256:" + "2" * 64,
                "contract_fingerprint": "sha256:" + "3" * 64,
            },
            "environment": {
                "environment_id": "test",
                "production": False,
                "runtime_bindings": {
                    "base_url": {"key": "TEST_BASE_URL", "required": True, "secret": False, "format": "url"},
                    "auth": {"type": "basic", "credential": {"key": "TEST_BASIC", "required": True, "secret": True, "format": "string"}},
                },
            },
            "risk": {"side_effect_level": "single"},
        }
        return design

    @staticmethod
    def _compile(design):
        return compile_approved_package(design, feasibility_plan=build_feasibility_plan(design))

    def test_approved_design_compiles_without_second_review(self):
        design = self._design()
        package = self._compile(design)
        self.assertEqual(package["schema_version"], "2.2")
        self.assertEqual(package["review"]["status"], "approved")
        self.assertTrue(package["review"]["review_reused"])
        self.assertFalse(package["package_build"]["semantic_review_required"])
        self.assertEqual(package["feasibility"]["gate"]["status"], "passed")
        self.assertEqual(package["review"]["design_fingerprint"], design["design_fingerprint"])
        self.assertEqual(package["review"]["plan_fingerprint"], calculate_plan_fingerprint(package))

    def test_internal_compilation_does_not_change_approved_scope(self):
        design = self._design()
        package = self._compile(design)
        self.assertEqual(package["review"]["approved_case_ids"], ["CHAIN-001-CREATE", "CHAIN-002-LABEL"])
        self.assertEqual(package["review"]["approved_scope"], package["review"]["approved_case_ids"])

    def test_behavior_change_requires_new_review(self):
        design = self._design()
        changed = copy.deepcopy(design)
        changed["cases"][1]["request"]["path"] = "/labels?orderNo=${CHAIN-001-CREATE.orderNo}"
        with self.assertRaisesRegex(PackageCompileError, "design_fingerprint"):
            self._compile(changed)

    def test_missing_execution_assets_is_internal_block_not_user_re_review(self):
        design = self._design()
        del design["package_template"]
        with self.assertRaisesRegex(PackageCompileError, "package_template or execution_assets"):
            self._compile(design)

    def test_package_build_requires_feasibility_plan(self):
        design = self._design()
        with self.assertRaisesRegex(PackageCompileError, "FEASIBILITY_PLAN_REQUIRED"):
            compile_approved_package(design)

    def test_package_build_requires_human_review_after_planning(self):
        design = self._design()
        del design["review"]
        plan = build_feasibility_plan(design)
        self.assertEqual(plan["gate"]["status"], "pending_review")
        with self.assertRaisesRegex(PackageCompileError, "HUMAN_REVIEW_REQUIRED"):
            compile_approved_package(design, feasibility_plan=plan)

    def test_blocked_dependency_prevents_package_build_after_review(self):
        design = self._design()
        design["cases"][0]["execution_dependencies"] = [
            {
                "dependency_id": "DATA-ORDER-001",
                "kind": "data",
                "status": "blocked",
                "description": "需要真实可写订单",
            }
        ]
        design["design_fingerprint"] = calculate_design_fingerprint(design)
        design["review"]["design_fingerprint"] = design["design_fingerprint"]
        plan = build_feasibility_plan(design)
        self.assertEqual(plan["gate"]["status"], "failed")
        with self.assertRaisesRegex(PackageCompileError, "PACKAGE_BUILD_BLOCKED_UNRESOLVED_DEPENDENCY"):
            compile_approved_package(design, feasibility_plan=plan)

    def test_partial_review_excluding_blocked_case_allows_package_build(self):
        design = self._design()
        design["cases"][1]["execution_dependencies"] = [
            {
                "dependency_id": "DATA-LABEL-001",
                "kind": "data",
                "status": "blocked",
                "description": "缺少标签查询数据",
            }
        ]
        design["design_fingerprint"] = calculate_design_fingerprint(design)
        design["review"] = {
            "status": "partially_approved",
            "approved_case_ids": ["CHAIN-001-CREATE"],
            "review_id": "REV-2",
            "reviewer": "user",
            "approved_at": "2026-11-01T10:00:00+08:00",
            "design_fingerprint": design["design_fingerprint"],
            "reviewed_fields": {"CHAIN-001-CREATE": ["request", "assertions", "side_effect"]},
        }
        package = self._compile(design)
        self.assertEqual(package["feasibility"]["gate"]["status"], "passed")
        self.assertEqual(package["feasibility"]["executable_case_ids"], ["CHAIN-001-CREATE"])
        self.assertIn("CHAIN-002-LABEL", package["feasibility"]["blocked_case_ids"])
        self.assertIn("CHAIN-002-LABEL", package["feasibility"]["out_of_scope_case_ids"])
        self.assertFalse(package["feasibility"]["coverage_map"]["full_requirement_coverage"])


if __name__ == "__main__":
    unittest.main()
