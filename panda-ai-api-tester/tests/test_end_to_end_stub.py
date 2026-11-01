"""Offline contract harness: real HTTP round-trip through the trusted host.

This test proves the plumbing (trusted host -> RuntimeContext -> gate -> case ->
result -> validate_result -> render_report) against a local stub server. It is a
link-level self check, not acceptance of any real environment behaviour.
"""
import hashlib
import json
import re
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import runner  # noqa: E402
from trusted_host import execute_test_package  # noqa: E402

TOKEN = "stub-bearer-token-must-not-leak"
ITEM_PATH = "/items/STUB-ITEM-1"
PROBE_PATH = "/probe/auth"
READ_ONLY = f"GET {PROBE_PATH},GET /health,GET {ITEM_PATH}"


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output clean
        return

    def _respond(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server contract
        server = self.server
        server.requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
        authorization = self.headers.get("Authorization")
        if self.path == PROBE_PATH:
            if authorization == f"Bearer {TOKEN}":
                self._respond(200, {"code": 200, "data": {"valid": True, "principal": "stub-user"}})
            else:
                self._respond(401, {"code": 401, "msg": "unauthenticated"})
            return
        if authorization != f"Bearer {TOKEN}":
            self._respond(401, {"code": 401, "msg": "unauthenticated"})
            return
        if self.path == "/health":
            self._respond(200, {"code": 200, "data": {"status": "ok"}})
            return
        if self.path == ITEM_PATH:
            self._respond(200, {"code": 200, "data": {"id": "STUB-ITEM-1", "name": "stub item"}})
            return
        self._respond(404, {"code": 404, "msg": "not found"})


class _StubEnvironment:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.server.requests = []
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base_url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self):
        return self.server.requests


class EndToEndContractTests(unittest.TestCase):
    def _profile(self, temp_dir):
        profile = Path(temp_dir) / "interface-profile.md"
        placeholder = "sha256:" + ("0" * 64)
        contract_fingerprint = "sha256:" + hashlib.sha256(b"stub-contract-v1").hexdigest()
        raw = (
            "# 离线桩接口画像\n\n```yaml interface-profile-governance\n"
            'schema_version: "1.0"\n'
            "profile_id: stub-workspace:stub-module\n"
            "profile_revision: 1\n"
            "module_id: stub-module\n"
            "code_baseline: stub-commit\n"
            "lifecycle: executable\n"
            "owners:\n"
            "  code_owner: backend-owner\n"
            "  business_owner: product-owner\n"
            "  test_owner: qa-owner\n"
            "confirmation:\n"
            "  confirmed_by: backend-owner\n"
            "  confirmed_at: 2026-10-30T20:00:00+08:00\n"
            "  scope: [contract, side_effect, cleanup]\n"
            "fingerprints:\n"
            f"  document_fingerprint: {placeholder}\n"
            f"  contract_fingerprint: {contract_fingerprint}\n"
            "```\n\n## Contract\n\nGET /health\nGET /items/{id}\nGET /probe/auth\n"
        )
        document_fingerprint = "sha256:" + hashlib.sha256(
            runner._normalized_profile_document(raw).encode("utf-8")
        ).hexdigest()
        profile.write_text(raw.replace(placeholder, document_fingerprint, 1), encoding="utf-8")
        return profile

    def _test_config(self, temp_dir):
        config = Path(temp_dir) / "test-config.md"
        config.write_text(
            "# Project test config\n\n```yaml test-config\n"
            'metadata:\n  schema_version: "2.1"\n  project_id: stub-workspace\n  config_revision: 1\n'
            "environments:\n  local-stub:\n    production: false\n```\n",
            encoding="utf-8",
        )
        return config

    def _assertion(self, assertion_id, assertion_type, path, expected):
        return {
            "assertion_id": assertion_id,
            "type": assertion_type,
            "source": "response_json",
            "path": path,
            "operator": "equals",
            "expected": expected,
            "business_rule_refs": ["BR-STUB"],
        }

    def _case(self, case_id, name, path, key_path):
        return {
            "case_id": case_id,
            "name": name,
            "requirement_refs": ["REQ-STUB"],
            "business_rule_refs": ["BR-STUB"],
            "scenario_refs": ["SC-STUB"],
            "depends_on": [],
            "request": {"method": "GET", "path": path},
            "assertions": [
                {
                    "assertion_id": f"{case_id}-A-HTTP",
                    "type": "http_status",
                    "source": "http_status",
                    "operator": "equals",
                    "expected": 200,
                    "business_rule_refs": ["BR-STUB"],
                },
                self._assertion(f"{case_id}-A-CODE", "business_code", "$.code", 200),
                {
                    "assertion_id": f"{case_id}-A-KEY",
                    "type": "key_fields",
                    "source": "response_json",
                    "path": key_path,
                    "operator": "exists",
                    "expected": True,
                    "business_rule_refs": ["BR-STUB"],
                },
            ],
            "side_effect": "read",
            "scenario_type": "positive",
            "expected": "只读接口返回业务成功",
        }

    def _package(self, temp_dir, base_url):
        profile = self._profile(temp_dir)
        config = self._test_config(temp_dir)
        profile_identity = runner.load_interface_profile_identity(profile)
        approved_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        cases = [
            self._case("TC-STUB-HEALTH", "桩服务健康检查", "/health", "$.data.status"),
            self._case("TC-STUB-ITEM", "桩服务只读详情", ITEM_PATH, "$.data.id"),
        ]
        package = {
            "schema_version": "2.1",
            "package_id": "pkg-stub-e2e-001",
            "project": {
                "workspace_id": "stub-workspace",
                "module_id": "stub-module",
                "requirement_refs": ["REQ-STUB"],
            },
            "test_config": {
                "path": str(config),
                "schema_version": "2.1",
                "config_revision": 1,
                "environment_id": "local-stub",
                "fingerprint": "sha256:" + hashlib.sha256(config.read_bytes()).hexdigest(),
            },
            "mode": "EXECUTE",
            "plan": {"plan_id": "PLAN-STUB-001", "title": "离线链路自证"},
            "contract": {
                "profile_path": str(profile),
                "governance_schema_version": profile_identity["governance_schema_version"],
                "profile_id": profile_identity["profile_id"],
                "profile_revision": profile_identity["profile_revision"],
                "code_baseline": profile_identity["code_baseline"],
                "lifecycle": profile_identity["lifecycle"],
                "document_fingerprint": profile_identity["document_fingerprint"],
                "contract_fingerprint": profile_identity["contract_fingerprint"],
            },
            "environment": {
                "environment_id": "local-stub",
                "production": False,
                "timeout_seconds": 5,
                "max_response_bytes": 65536,
                "runtime_bindings": {
                    "base_url": {"key": "STUB_BASE_URL", "required": True, "secret": False, "format": "url"},
                    "allowed_origins": {"key": "STUB_ALLOWED_ORIGINS", "required": True, "secret": False, "format": "origin_list"},
                    "read_only_operations": {"key": "STUB_READ_ONLY_OPERATIONS", "required": True, "secret": False, "format": "operation_list"},
                    "auth": {
                        "type": "bearer",
                        "header": "Authorization",
                        "credential": {"key": "STUB_BEARER_TOKEN", "required": True, "secret": True, "format": "string"},
                    },
                },
            },
            "auth_probe": {
                "strategy": "declared",
                "gate_id": "GATE-AUTH-PROBE",
                "side_effect_free": {
                    "confirmed_by": "qa-owner",
                    "confirmed_at": "2026-10-30T20:00:00+08:00",
                    "reference": "离线桩：/probe/auth 仅回显鉴权结果，不写任何状态",
                    "rationale": "stub endpoint has no persistence",
                },
            },
            "risk": {"side_effect_level": "read_only"},
            "requirements": [{"requirement_id": "REQ-STUB", "title": "离线链路自证", "required": True}],
            "business_rules": [
                {"rule_id": "BR-STUB", "requirement_id": "REQ-STUB", "title": "只读接口返回业务成功码", "required": True}
            ],
            "scenarios": [
                {
                    "scenario_id": "SC-STUB",
                    "name": "只读查询",
                    "requirement_refs": ["REQ-STUB"],
                    "business_rule_refs": ["BR-STUB"],
                    "case_refs": ["TC-STUB-HEALTH", "TC-STUB-ITEM"],
                    "required": True,
                }
            ],
            "gates": [
                {
                    "gate_id": "GATE-AUTH-PROBE",
                    "name": "认证探测",
                    "classification": "environment",
                    "request": {"method": "GET", "path": PROBE_PATH},
                    "assertions": {
                        "http_status": [200],
                        "business_code": {"path": "$.code", "equals": 200},
                        "required_fields": ["$.data.valid"],
                    },
                }
            ],
            "fixtures": [],
            "cases": cases,
            "known_gaps": [],
        }
        package["review"] = {
            "status": "approved",
            "review_id": "REVIEW-STUB-001",
            "reviewer": "offline-harness",
            "approved_at": approved_at.isoformat(),
            "expires_at": (approved_at + timedelta(hours=1)).isoformat(),
            "approved_scope": ["TC-STUB-HEALTH", "TC-STUB-ITEM"],
            "accepted_gaps": [],
        }
        package["review"]["plan_fingerprint"] = runner.calculate_plan_fingerprint(package)
        return package

    def _run(self, temp_dir, base_url, token):
        package = self._package(temp_dir, base_url)
        package_path = Path(temp_dir) / "package.json"
        package_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        run_dir = Path(temp_dir) / "run"
        report_path = Path(temp_dir) / "report.html"
        outcome_path = Path(temp_dir) / "host-outcome.json"
        outcome = execute_test_package(
            package_path,
            run_dir,
            report_path,
            runtime_values={
                "STUB_BASE_URL": base_url,
                "STUB_ALLOWED_ORIGINS": base_url,
                "STUB_READ_ONLY_OPERATIONS": READ_ONLY,
                "STUB_BEARER_TOKEN": token,
            },
            context_id="ctx-stub-e2e",
            outcome_path=outcome_path,
        )
        return outcome, run_dir, report_path, outcome_path

    def test_pipeline_passes_and_auth_is_verified_against_real_http(self):
        with tempfile.TemporaryDirectory() as temp_dir, _StubEnvironment() as stub:
            outcome, run_dir, report_path, outcome_path = self._run(temp_dir, stub.base_url, TOKEN)
            self.assertEqual(outcome["status"], "completed", outcome)
            result_path = run_dir / "result.json"
            self.assertTrue(result_path.is_file())
            self.assertTrue(report_path.is_file())
            result = json.loads(result_path.read_text(encoding="utf-8"))

            self.assertEqual(result["verdict"], "passed", json.dumps(result["verdict"]))
            self.assertEqual(result["environment"]["runtime_status"]["auth"], "verified")
            probe = [gate for gate in result["gate_results"] if gate.get("role") == "auth_probe"]
            self.assertEqual(len(probe), 1)
            self.assertEqual(probe[0]["status"], "passed")
            self.assertEqual(probe[0]["operation"], f"GET {PROBE_PATH}")
            self.assertEqual([item["status"] for item in result["case_results"]], ["passed", "passed"])
            self.assertEqual(result["cleanup"]["status"], "not_required")

            # The probe must be the first HTTP request so an invalid token cannot
            # reach a business case.
            self.assertEqual([item["path"] for item in stub.requests], [PROBE_PATH, "/health", ITEM_PATH])
            self.assertTrue(all(item["authorization"] == f"Bearer {TOKEN}" for item in stub.requests))

            # Timed stages prove each pipeline step actually ran.
            self.assertEqual(outcome["stages"]["pipeline"]["status"], "completed")
            self.assertIn("validation", outcome["stages"]["pipeline"]["details"]["stages"])
            self.assertIn("report", outcome["stages"]["pipeline"]["details"]["stages"])

    def test_runtime_values_never_reach_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir, _StubEnvironment() as stub:
            outcome, run_dir, report_path, outcome_path = self._run(temp_dir, stub.base_url, TOKEN)
            self.assertEqual(outcome["status"], "completed", outcome)
            self.assertIn("报告", report_path.read_text(encoding="utf-8"))
            artifacts = [run_dir / "result.json", report_path, outcome_path]
            artifacts.extend(sorted((run_dir / "evidence").glob("*.json")))
            artifacts.append(run_dir / "progress.json")
            for path in artifacts:
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(TOKEN, text, f"runtime secret leaked into {path}")

    def test_invalid_token_blocks_before_business_cases(self):
        with tempfile.TemporaryDirectory() as temp_dir, _StubEnvironment() as stub:
            outcome, run_dir, report_path, _ = self._run(temp_dir, stub.base_url, "wrong-token")
            self.assertEqual(outcome["status"], "completed", outcome)
            result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["verdict"], "blocked")
            self.assertEqual(result["environment"]["runtime_status"]["auth"], "authentication_failed")
            self.assertEqual([item["status"] for item in result["case_results"]], ["not_run", "not_run"])
            self.assertEqual([item["path"] for item in stub.requests], [PROBE_PATH])
            self.assertTrue(report_path.is_file())


if __name__ == "__main__":
    unittest.main()
