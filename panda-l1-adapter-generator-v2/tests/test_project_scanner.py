import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from project_scanner import scan_project


class ProjectScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_discovers_java_express_and_fastapi_routes(self):
        self.write("src/Orders.java", '''
@RestController
@RequestMapping("/api")
class Orders {
  @GetMapping("/orders/{id}")
  Object get() { return null; }
  @PostMapping(path = "/orders")
  Object post() { return null; }
}
''')
        self.write("routes.js", "router.delete('/orders/:id', handler);\n")
        self.write("main.py", '@app.put("/orders/{id}")\ndef update(): pass\n')
        report = scan_project(self.root).to_dict()
        routes = {(item["method"], item["path_pattern"]) for item in report["source"]}
        self.assertIn(("GET", "/api/orders/{id}"), routes)
        self.assertIn(("POST", "/api/orders"), routes)
        self.assertIn(("DELETE", "/orders/:id"), routes)
        self.assertIn(("PUT", "/orders/{id}"), routes)
        self.assertTrue(all({"file", "line", "evidence", "confidence"} <= item.keys() for item in report["source"]))
        self.assertTrue(all(item["details"]["candidate_only"] for item in report["source"]))
        self.assertEqual(report["summary"]["semantic_decision_owner"], "ai_skill")
        self.assertEqual(report["decision_policy"], "ai_skill_semantic_decision_script_candidate_discovery")

    def test_parses_openapi_and_merges_source_candidate(self):
        self.write("openapi.json", json.dumps({
            "openapi": "3.0.0",
            "paths": {
                "/pets/{id}": {
                    "get": {
                        "operationId": "getPet",
                        "parameters": [{"name": "id", "in": "path", "required": True}],
                        "responses": {"200": {"description": "ok"}},
                    },
                    "post": {
                        "requestBody": {"required": True},
                        "responses": {"201": {"description": "created"}},
                    },
                }
            },
        }))
        self.write("app.py", '@app.get("/pets/{id}")\ndef get_pet(): pass\n')
        contracts = scan_project(self.root).to_dict()["contracts"]
        get_contract = next(item for item in contracts if item["method"] == "GET")
        self.assertEqual(get_contract["status"], "inferred")
        self.assertEqual(get_contract["source"], "openapi+source")
        self.assertEqual(get_contract["details"]["operationId"], "getPet")
        self.assertEqual(get_contract["details"]["parameters"][0]["name"], "id")
        self.assertIn("responses", get_contract["details"])
        post_contract = next(item for item in contracts if item["method"] == "POST")
        self.assertEqual(post_contract["status"], "discovered")
        self.assertTrue(post_contract["details"]["requestBody"]["required"])

    def test_deployment_candidates_never_leak_secret_values(self):
        secret = "never-print-this-secret"
        self.write("compose.yaml", '''services:
  api:
    image: demo
    ports:
      - "8080:8080"
    environment:
      API_TOKEN: ${API_TOKEN}
''')
        self.write(".env.example", f"API_TOKEN={secret}\nBASE_URL=https://user:password@example.test/api?token={secret}\n")
        serialized = scan_project(self.root).to_json()
        self.assertNotIn(secret, serialized)
        self.assertNotIn("password@", serialized)
        report = json.loads(serialized)
        names = {item.get("name") for item in report["deployment"]}
        self.assertIn("api", names)
        self.assertIn("8080", names)
        self.assertIn("API_TOKEN", names)
        token = next(item for item in report["deployment"] if item.get("name") == "API_TOKEN")
        self.assertTrue(token["details"]["secret"])
        self.assertEqual(token["details"]["environment"], "unverified")

    def test_recognizes_docker_and_application_config_without_values(self):
        secret = "docker-secret-value"
        self.write("Dockerfile", f"FROM python:3.12\nARG API_TOKEN={secret}\nEXPOSE 9000\n")
        self.write("application-test.yml", f"spring:\n  application:\n    name: orders-api\nadminPassword: {secret}\n")
        serialized = scan_project(self.root).to_json()
        self.assertNotIn(secret, serialized)
        report = json.loads(serialized)
        assets = {item.get("name") for item in report["deployment"] if item["kind"] == "deployment_asset"}
        names = {item.get("name") for item in report["deployment"]}
        self.assertIn("Dockerfile", assets)
        self.assertIn("application-test.yml", assets)
        self.assertIn("9000", names)
        self.assertIn("orders-api", names)
        self.assertIn("adminPassword", names)

    def test_ignored_directories_are_not_scanned(self):
        self.write("node_modules/pkg/routes.js", "router.get('/ignored', handler);\n")
        self.write("target/Generated.java", '@GetMapping("/ignored-java")\n')
        self.write("src/app.py", '@app.get("/visible")\ndef visible(): pass\n')
        paths = {item.path_pattern for item in scan_project(self.root).source}
        self.assertEqual(paths, {"/visible"})

    def test_empty_project_has_structured_gaps(self):
        report = scan_project(self.root).to_dict()
        codes = {gap["code"] for gap in report["gaps"]}
        self.assertIn("source_routes_not_found", codes)
        self.assertIn("deployment_assets_not_found", codes)
        self.assertIn("openapi_not_found", codes)
        self.assertIn("contracts_not_discovered", codes)

    def test_cli_json_has_no_global_gate_fields(self):
        command = [sys.executable, str(SCRIPTS / "project_scanner.py"), "scan", str(self.root), "--output", "json"]
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        report = json.loads(completed.stdout)
        self.assertEqual(report["summary"]["mode"], "read_only_discovery")
        serialized = json.dumps(report)
        for forbidden in ("allow_execute", "allow_promote", "global_gate"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
