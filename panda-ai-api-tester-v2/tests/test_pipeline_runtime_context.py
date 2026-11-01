import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from run_pipeline import PipelineBlocked, run_pipeline
from runtime_context import RuntimeContext


class PipelineRuntimeContextTests(unittest.TestCase):
    def _package(self):
        return {
            "schema_version": "2.1",
            "environment": {
                "environment_id": "test3",
                "production": False,
                "runtime_bindings": {
                    "base_url": {
                        "key": "TEST3_BASE_URL",
                        "required": True,
                        "secret": False,
                        "format": "url",
                    },
                    "auth": {
                        "type": "bearer",
                        "credential": {
                            "key": "TEST3_TOKEN",
                            "required": True,
                            "secret": True,
                            "format": "string",
                        },
                    },
                },
            },
            "gates": [
                {
                    "gate_id": "GATE-AUTH-PROBE",
                    "name": "认证探测",
                    "classification": "environment",
                    "request": {"method": "GET", "path": "/probe/auth"},
                    "assertions": {"http_status": [200]},
                }
            ],
            "auth_probe": {
                "strategy": "declared",
                "gate_id": "GATE-AUTH-PROBE",
                "side_effect_free": {
                    "confirmed_by": "qa-owner",
                    "reference": "docs/probe-contract.md",
                    "rationale": "read-only probe",
                },
            },
        }

    def test_missing_context_blocks_before_output_creation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_path = root / "package.json"
            package_path.write_text(json.dumps(self._package()), encoding="utf-8")
            run_dir = root / "run"
            report_path = root / "report.html"
            with self.assertRaisesRegex(PipelineBlocked, "explicit RuntimeContext"):
                run_pipeline(package_path, run_dir, report_path, runtime_context=None)
            self.assertFalse(run_dir.exists())
            self.assertFalse(report_path.exists())

    def test_missing_required_binding_blocks_before_runner_and_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_path = root / "package.json"
            package_path.write_text(json.dumps(self._package()), encoding="utf-8")
            run_dir = root / "run"
            report_path = root / "report.html"
            context = RuntimeContext.from_mapping(
                "test3",
                {"TEST3_BASE_URL": "https://test.example"},
                secret_keys=set(),
            )
            with self.assertRaisesRegex(PipelineBlocked, "TEST3_TOKEN"):
                run_pipeline(package_path, run_dir, report_path, runtime_context=context)
            self.assertFalse(run_dir.exists())
            self.assertFalse(report_path.exists())

    def test_environment_mismatch_blocks_before_runner_and_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package_path = root / "package.json"
            package_path.write_text(json.dumps(self._package()), encoding="utf-8")
            run_dir = root / "run"
            report_path = root / "report.html"
            context = RuntimeContext.from_mapping(
                "other",
                {
                    "TEST3_BASE_URL": "https://test.example",
                    "TEST3_TOKEN": "secret-token-value",
                },
                secret_keys={"TEST3_TOKEN"},
            )
            with self.assertRaisesRegex(PipelineBlocked, "environment_id"):
                run_pipeline(package_path, run_dir, report_path, runtime_context=context)
            self.assertFalse(run_dir.exists())
            self.assertFalse(report_path.exists())


if __name__ == "__main__":
    unittest.main()
