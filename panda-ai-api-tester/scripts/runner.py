#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from plan_fingerprint import calculate_plan_fingerprint
from validate_review import validate_review
from progress import ProgressWriter
from runtime_context import RuntimeContext, RuntimeContextError, auth_config, auth_probe_gate, auth_probe_operation, auth_probe_strategy, binding_spec, package_binding_keys, package_execution_preflight, preflight


SUPPORTED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
SENSITIVE_KEYS = {
    "authorization", "cookie", "setcookie", "token", "accesstoken", "refreshtoken",
    "password", "secret", "clientsecret", "apikey", "xapikey", "phone", "principal",
}
PRODUCTION_HOST_HINTS = {"prod", "production", "live"}
WRITE_METHODS = {"POST", "PUT", "PATCH"}
DESTRUCTIVE_METHODS = {"DELETE"}
VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")
TEST_CONFIG_BLOCK_PATTERN = re.compile(r"```yaml\s+test-config\s*\n(?P<body>[\s\S]*?)\n```", re.IGNORECASE)
PROFILE_GOVERNANCE_BLOCK_PATTERN = re.compile(
    r"```yaml\s+interface-profile-governance\s*\n(?P<body>[\s\S]*?)\n```",
    re.IGNORECASE,
)
SHA256_PATTERN = re.compile(r"sha256:[0-9a-fA-F]{64}")
PROFILE_LIFECYCLES = {
    "discovered", "drafted", "minimally_usable", "confirmed", "executable", "drifted", "reconfirm_required", "retired",
}


def parse_iso8601_with_timezone(value, field_name):
    if not isinstance(value, str) or not value.strip():
        raise RunnerError(f"{field_name} must be a non-empty ISO8601 datetime with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunnerError(f"{field_name} must be a valid ISO8601 datetime with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RunnerError(f"{field_name} must include a timezone")
    return parsed


def contains_variable(value):
    if isinstance(value, dict):
        return any(contains_variable(key) or contains_variable(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_variable(item) for item in value)
    return isinstance(value, str) and VARIABLE_PATTERN.search(value) is not None


def body_contains_tag(request, data_tag):
    if "json" in request:
        body = request.get("json")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                return data_tag in body
        return contains_explicit_tag(body, data_tag)
    if "body" not in request:
        return False
    body = request.get("body")
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return data_tag in body
    return contains_explicit_tag(body, data_tag)


def contains_explicit_tag(value, data_tag):
    if isinstance(value, dict):
        return any(contains_explicit_tag(key, data_tag) or contains_explicit_tag(item, data_tag) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_explicit_tag(item, data_tag) for item in value)
    return isinstance(value, str) and data_tag in value


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def load_test_config_identity(path):
    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise RunnerError(f"test config not found: {config_path}")
    raw = config_path.read_text(encoding="utf-8")
    blocks = list(TEST_CONFIG_BLOCK_PATTERN.finditer(raw))
    if len(blocks) != 1:
        raise RunnerError("TEST_CONFIG_SCHEMA_UNSUPPORTED: test-config.md requires exactly one yaml test-config block")
    body = blocks[0].group("body")
    schema = re.search(r"(?m)^\s*schema_version:\s*[\"']?([^\"'\s#]+)", body)
    project = re.search(r"(?m)^\s*project_id:\s*[\"']?([^\"'\s#]+)", body)
    revision = re.search(r"(?m)^\s*config_revision:\s*(\d+)\s*(?:#.*)?$", body)
    environments = re.search(r"(?m)^environments:\s*$", body)
    if not schema or schema.group(1) != "2.1" or not project or not revision or int(revision.group(1)) < 1 or not environments:
        raise RunnerError("TEST_CONFIG_SCHEMA_UNSUPPORTED: only project-level multi-environment Schema 2.1 is supported")
    return {
        "path": str(config_path),
        "schema_version": schema.group(1),
        "project_id": project.group(1),
        "config_revision": int(revision.group(1)),
        "fingerprint": "sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest(),
    }


def _governance_scalar(body, field, required=True):
    match = re.search(rf"(?m)^\s*{re.escape(field)}:\s*[\"']?([^\"'\n#]+?)[\"']?\s*(?:#.*)?$", body)
    if not match:
        if required:
            raise RunnerError(f"interface profile governance missing {field}")
        return None
    return match.group(1).strip()


def _normalized_profile_document(raw):
    return re.sub(
        r"(?m)^(\s*(?:document_fingerprint|contract_fingerprint):\s*)sha256:[0-9a-fA-F]{64}(\s*(?:#.*)?)$",
        r"\1sha256:<normalized>\2",
        raw,
    )


def load_interface_profile_identity(path):
    profile_path = Path(path).resolve()
    if not profile_path.is_file():
        raise RunnerError(f"contract profile not found: {profile_path}")
    raw = profile_path.read_text(encoding="utf-8")
    blocks = list(PROFILE_GOVERNANCE_BLOCK_PATTERN.finditer(raw))
    if len(blocks) != 1:
        raise RunnerError("INTERFACE_PROFILE_GOVERNANCE_UNSUPPORTED: profile requires exactly one yaml interface-profile-governance block")
    body = blocks[0].group("body")
    schema_version = _governance_scalar(body, "schema_version")
    profile_id = _governance_scalar(body, "profile_id")
    revision_raw = _governance_scalar(body, "profile_revision")
    code_baseline = _governance_scalar(body, "code_baseline")
    lifecycle = _governance_scalar(body, "lifecycle")
    document_fingerprint = _governance_scalar(body, "document_fingerprint")
    contract_fingerprint = _governance_scalar(body, "contract_fingerprint")
    try:
        profile_revision = int(revision_raw)
    except (TypeError, ValueError) as exc:
        raise RunnerError("interface profile profile_revision must be a positive integer") from exc
    if schema_version != "1.0" or profile_revision < 1:
        raise RunnerError("INTERFACE_PROFILE_GOVERNANCE_UNSUPPORTED: schema_version=1.0 and positive profile_revision are required")
    if lifecycle not in PROFILE_LIFECYCLES:
        raise RunnerError("interface profile lifecycle is invalid")
    if not SHA256_PATTERN.fullmatch(document_fingerprint or "") or not SHA256_PATTERN.fullmatch(contract_fingerprint or ""):
        raise RunnerError("interface profile requires document_fingerprint and contract_fingerprint sha256 values")
    actual_document_fingerprint = "sha256:" + hashlib.sha256(
        _normalized_profile_document(raw).encode("utf-8")
    ).hexdigest()
    if actual_document_fingerprint.lower() != document_fingerprint.lower():
        raise RunnerError("interface profile document_fingerprint mismatch")
    owners_block = re.search(r"(?ms)^owners:\s*\n(?P<body>(?:^[ \t]+[^\n]+\n?)+)", body)
    if not owners_block:
        raise RunnerError("interface profile governance requires owners")
    owner_body = owners_block.group("body")
    for owner_field in ("code_owner", "business_owner", "test_owner"):
        owner = _governance_scalar(owner_body, owner_field)
        if not owner or owner.lower() in {"pending", "unknown", "待确认"}:
            raise RunnerError(f"interface profile governance requires confirmed {owner_field}")
    return {
        "profile_path": str(profile_path),
        "governance_schema_version": schema_version,
        "profile_id": profile_id,
        "profile_revision": profile_revision,
        "code_baseline": code_baseline,
        "lifecycle": lifecycle,
        "document_fingerprint": document_fingerprint,
        "contract_fingerprint": contract_fingerprint,
    }


def slug(value):
    raw = str(value)
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-.").lower() or "item"
    normalized = normalized[:80]
    reserved = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
    if normalized.split(".", 1)[0] in reserved:
        normalized = "id-" + normalized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{normalized}-{digest}"


def canonical_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def redact_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    query = []
    for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        query.append((key, "***REDACTED***" if canonical_key(key) in SENSITIVE_KEYS else item))
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, urllib.parse.urlencode(query), ""))


def redact_embedded_urls(value):
    return re.sub(r"https?://[^\s\"'<>]+", lambda match: redact_url(match.group(0)), value)


def redact(value, key=None, secrets=None):
    secrets = [item for item in (secrets or []) if item]
    if key and canonical_key(key) in SENSITIVE_KEYS:
        return "***REDACTED***"
    if isinstance(value, dict):
        return {item_key: redact(item_value, item_key, secrets) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        result = redact_url(value) if value.startswith(("http://", "https://")) else redact_embedded_urls(value)
        if result.lower().startswith("bearer "):
            result = "Bearer ***REDACTED***"
        for secret in secrets:
            result = result.replace(secret, "***REDACTED***")
        if len(result) > 4000:
            return result[:4000] + "...[TRUNCATED]"
        return result
    return value


def json_path_get(document, path):
    if path in {"$", "", None}:
        return document
    if not isinstance(path, str) or not path.startswith("$."):
        raise ValueError(f"unsupported JSON path: {path}")
    current = document
    tokens = re.findall(r"([^.[\]]+)|\[(\d+)\]", path[2:])
    for name, index in tokens:
        if name:
            if not isinstance(current, dict) or name not in current:
                raise KeyError(path)
            current = current[name]
        else:
            position = int(index)
            if not isinstance(current, list) or position >= len(current):
                raise KeyError(path)
            current = current[position]
    return current


def redact_json_paths(document, paths):
    result = deepcopy(document)
    for path in paths or []:
        if not isinstance(path, str) or not path.startswith("$."):
            continue
        tokens = [(name, int(index) if index else None) for name, index in re.findall(r"([^.[\]]+)|\[(\d+)\]", path[2:])]
        current = result
        for position, (name, index) in enumerate(tokens):
            last = position == len(tokens) - 1
            if name:
                if not isinstance(current, dict) or name not in current:
                    break
                if last:
                    current[name] = "***REDACTED***"
                else:
                    current = current[name]
            else:
                if not isinstance(current, list) or index >= len(current):
                    break
                if last:
                    current[index] = "***REDACTED***"
                else:
                    current = current[index]
    return result


def sanitize_exchange(exchange, sensitive_json_paths):
    safe = deepcopy(exchange)
    if safe.get("response_json") is not None:
        safe["response_json"] = redact_json_paths(safe["response_json"], sensitive_json_paths)
        safe["response_body"] = deepcopy(safe["response_json"])
    return safe


def sanitize_assertions(assertions, sensitive_json_paths=None):
    sanitized = []
    for assertion in assertions:
        item = deepcopy(assertion)
        path = str(item.get("path", ""))
        terminal = re.split(r"[.\[\]'\"]+", path)[-1] if path else ""
        if canonical_key(terminal) in SENSITIVE_KEYS or path in set(sensitive_json_paths or []):
            passed = item.get("status") == "passed"
            item["original_operator"] = item.get("operator")
            item["operator"] = "equals"
            item["expected"] = True
            item["actual"] = passed
            item["redacted"] = True
        sanitized.append(item)
    return sanitized


def assertion_passes(operator, expected, actual):
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "exists":
        return actual is not None and actual is not False
    if operator == "all_present":
        return isinstance(expected, list) and isinstance(actual, list) and set(expected).issubset(set(actual))
    if operator == "contains":
        return isinstance(actual, (list, str)) and expected in actual
    if operator == "in_range":
        return (
            isinstance(expected, dict)
            and isinstance(actual, (int, float))
            and expected.get("min") is not None
            and expected.get("max") is not None
            and expected["min"] <= actual <= expected["max"]
        )
    return False


class RunnerError(Exception):
    pass


class VariableResolver:
    def __init__(self, initial=None, allowed_bindings=None, runtime_context=None):
        self.values = dict(initial or {})
        self.allowed_bindings = set(allowed_bindings or [])
        self.runtime_context = runtime_context
        if runtime_context is None:
            raise RunnerError("explicit RuntimeContext is required")

    def set(self, name, value):
        if name in self.runtime_context.values:
            raise RunnerError(f"runtime binding is read-only: {name}")
        self.values[name] = value

    def get(self, name):
        if name in self.values:
            return self.values[name]
        if name in self.allowed_bindings:
            try:
                return self.runtime_context.require(name)
            except RuntimeContextError as exc:
                raise RunnerError(str(exc)) from exc
        raise RunnerError(f"unknown or unauthorized variable: {name}")

    def resolve(self, value):
        if isinstance(value, dict):
            return {key: self.resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if not isinstance(value, str):
            return value
        full = VARIABLE_PATTERN.fullmatch(value)
        if full:
            return deepcopy(self.get(full.group(1)))

        def replace(match):
            resolved = self.get(match.group(1))
            if isinstance(resolved, (dict, list)):
                return json.dumps(resolved, ensure_ascii=False, separators=(",", ":"))
            return str(resolved)

        return VARIABLE_PATTERN.sub(replace, value)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def auth_binding_names(config):
    if not isinstance(config, dict):
        return set()
    auth_type = str(config.get("type", "none")).strip().lower()
    if auth_type == "basic":
        return {item.get("key") for item in (config.get("username"), config.get("password")) if isinstance(item, dict) and item.get("key")}
    if auth_type == "headers":
        return {item.get("key") for item in (config.get("headers") or {}).values() if isinstance(item, dict) and item.get("key")}
    credential = config.get("credential")
    return {credential.get("key")} if isinstance(credential, dict) and credential.get("key") else set()


class AuthInjector:
    """Build deterministic request authentication from runtime-only variables."""

    def __init__(self, package, resolver):
        self.config = auth_config(package)
        self.resolver = resolver

    @property
    def required_bindings(self):
        return auth_binding_names(self.config)

    def headers(self):
        config = self.config
        if not isinstance(config, dict) or config.get("enabled", True) is False:
            return {}
        auth_type = str(config.get("type", "none")).strip().lower()
        if auth_type in {"", "none", "anonymous"}:
            return {}

        configured_headers = config.get("headers") or {}
        if auth_type == "headers":
            return {str(name): str(self.resolver.get(spec["key"])).strip() for name, spec in configured_headers.items()}

        header = config.get("header")
        if auth_type == "basic":
            username_key = config["username"]["key"]
            password_key = config["password"]["key"]
            username = str(self.resolver.get(username_key)).strip()
            password = str(self.resolver.get(password_key)).strip()
            import base64
            encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            return {str(header or "Authorization"): f"Basic {encoded}"}

        credential_key = config["credential"]["key"]
        credential = self.resolver.get(credential_key)
        if config.get("trim", True) and isinstance(credential, str):
            credential = credential.strip()
        template = config.get("value_template")
        if template is None:
            template = {
                "bearer": "Bearer ${" + credential_key + "}",
                "token": "${" + credential_key + "}",
                "header": "${" + credential_key + "}",
                "cookie": "${" + credential_key + "}",
            }.get(auth_type)
        if not isinstance(template, str) or not template:
            raise RunnerError(f"unsupported or incomplete auth type: {auth_type}")
        if template == "${" + credential_key + "}":
            value = str(credential)
        else:
            value = str(self.resolver.resolve(template))
        default_header = "Cookie" if auth_type == "cookie" else "Authorization"
        return {str(header or default_header): value}


class HttpExecutor:
    def __init__(self, base_url, timeout=10.0, max_response_bytes=1024 * 1024, auth_provider=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self.allowed_origin = self._origin(self.base_url)
        self.auth_provider = auth_provider

    @staticmethod
    def _origin(url):
        parsed = urllib.parse.urlsplit(url)
        default = (parsed.scheme == "http" and parsed.port in {None, 80}) or (parsed.scheme == "https" and parsed.port in {None, 443})
        port = "" if default else f":{parsed.port}"
        return f"{parsed.scheme}://{parsed.hostname or ''}{port}"

    def execute(self, request_spec):
        method = str(request_spec.get("method", "GET")).upper()
        if method not in SUPPORTED_METHODS:
            raise RunnerError(f"unsupported HTTP method: {method}")
        path = request_spec.get("path", "")
        if not isinstance(path, str) or not path.startswith("/"):
            raise RunnerError("request path must start with /")
        query = request_spec.get("query") or {}
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = {str(key): str(value) for key, value in (request_spec.get("headers") or {}).items()}
        if self.auth_provider:
            injected = self.auth_provider.headers()
            existing = {key.lower(): key for key in headers}
            for key, value in injected.items():
                # Explicit case-insensitive request headers win over defaults.
                if key.lower() not in existing:
                    headers[key] = value
        body = None
        if "json" in request_spec:
            body = json.dumps(request_spec["json"], ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif "body" in request_spec:
            raw = request_spec["body"]
            body = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        started = time.perf_counter()
        request = urllib.request.Request(url=url, data=body, headers=headers, method=method)
        opener = urllib.request.build_opener(NoRedirectHandler())
        try:
            with opener.open(request, timeout=float(request_spec.get("timeout", self.timeout))) as response:
                raw_body = response.read(self.max_response_bytes + 1)
                if len(raw_body) > self.max_response_bytes:
                    raise RunnerError("response exceeds configured size limit")
                status = response.status
                response_headers = dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise RunnerError(f"HTTP redirect is not allowed: {exc.code} {(exc.headers or {}).get('Location', '')}") from exc
            raw_body = exc.read(self.max_response_bytes + 1)
            if len(raw_body) > self.max_response_bytes:
                raise RunnerError("error response exceeds configured size limit") from exc
            status = exc.code
            response_headers = dict(exc.headers.items()) if exc.headers else {}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = RunnerError(f"HTTP transport error: {exc}")
            error.request_delivery = "unknown"
            raise error from exc
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        charset = "utf-8"
        content_type = response_headers.get("Content-Type", "")
        match = re.search(r"charset=([^; ]+)", content_type, flags=re.IGNORECASE)
        if match:
            charset = match.group(1).strip('"')
        text = raw_body.decode(charset, errors="replace")
        parsed_json = None
        if text:
            try:
                parsed_json = json.loads(text)
            except json.JSONDecodeError:
                parsed_json = None
        if 300 <= status < 400:
            raise RunnerError(f"HTTP redirect is not allowed: {status} {response_headers.get('Location', '')}")
        if self._origin(url) != self.allowed_origin:
            raise RunnerError("request origin differs from validated base URL")
        return {
            "method": method,
            "url": url,
            "request_headers": headers,
            "request_body": request_spec.get("json", request_spec.get("body")),
            "status": status,
            "response_headers": response_headers,
            "response_body": parsed_json if parsed_json is not None else text,
            "response_json": parsed_json,
            "elapsed_ms": elapsed_ms,
        }


class AssertionEngine:
    def evaluate(self, spec, exchange):
        if isinstance(spec, dict):
            return self._evaluate_legacy(spec, exchange)
        if not isinstance(spec, list):
            raise RunnerError("assertions must be an object or a list")
        results = []
        for item in spec:
            results.append(self._evaluate_item(item, exchange))
        return results

    def _evaluate_legacy(self, spec, exchange):
        results = []
        allowed_statuses = spec.get("http_status", [200])
        if not isinstance(allowed_statuses, list):
            allowed_statuses = [allowed_statuses]
        status_actual = exchange["status"]
        status_expected = status_actual if status_actual in allowed_statuses else allowed_statuses
        results.append(self._result("http_status", "equals", status_expected, status_actual, True))

        business = spec.get("business_code")
        if not isinstance(business, dict):
            raise RunnerError("business_code assertion is required")
        business_path = business.get("path", "$.code")
        business_expected = business.get("equals")
        business_actual = self._safe_json_path(exchange.get("response_json"), business_path)
        results.append(self._result("business_code", "equals", business_expected, business_actual, True, business_path))

        required_fields = spec.get("required_fields")
        if not isinstance(required_fields, list) or not required_fields:
            raise RunnerError("required_fields assertion is required")
        present = [path for path in required_fields if self._path_exists(exchange.get("response_json"), path)]
        results.append(self._result("key_fields", "all_present", required_fields, present, True))
        return results

    def _evaluate_item(self, item, exchange):
        if not isinstance(item, dict):
            raise RunnerError("each assertion must be an object")
        assertion_type = item.get("type")
        operator = item.get("operator", "equals")
        expected = item.get("expected")
        source = item.get("source")
        path = item.get("path")
        if source == "http_status" or assertion_type == "http_status":
            actual = exchange["status"]
        elif source == "response_header":
            actual = exchange["response_headers"].get(path)
        elif source == "response_json" or path:
            actual = self._safe_json_path(exchange.get("response_json"), path)
        else:
            raise RunnerError(f"assertion {assertion_type} requires a supported source")
        result = self._result(assertion_type, operator, expected, actual, item.get("required", True), path)
        for key in ("assertion_id", "business_rule_refs", "description", "role"):
            if key in item:
                result[key] = deepcopy(item[key])
        return result

    def _result(self, assertion_type, operator, expected, actual, required, path=None):
        passed = assertion_passes(operator, expected, actual)
        result = {
            "type": assertion_type,
            "operator": operator,
            "expected": expected,
            "actual": actual,
            "required": bool(required),
            "status": "passed" if passed else "failed",
        }
        if path:
            result["path"] = path
        return result

    @staticmethod
    def _safe_json_path(document, path):
        try:
            return json_path_get(document, path)
        except (KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _path_exists(document, path):
        try:
            json_path_get(document, path)
            return True
        except (KeyError, ValueError, TypeError):
            return False


class EvidenceWriter:
    def __init__(self, run_dir, secrets=None):
        self.run_dir = Path(run_dir).resolve()
        self.evidence_dir = self.run_dir / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.secrets = secrets if secrets is not None else []

    def write(self, identifier, payload):
        name = slug(identifier) + ".json"
        path = self.evidence_dir / name
        data = json.dumps(redact(payload, secrets=self.secrets), ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        path.write_bytes(data)
        return f"evidence/{name}", sha256_bytes(data)


class FixtureManager:
    def __init__(self, resolver, http_executor, evidence_writer, teardown_manager):
        self.resolver = resolver
        self.http = http_executor
        self.evidence = evidence_writer
        self.teardown = teardown_manager

    def prepare(self, fixtures):
        records = []
        for fixture in fixtures or []:
            fixture_id = fixture.get("fixture_id")
            fixture_type = fixture.get("type", "static")
            if not fixture_id:
                raise RunnerError("fixture_id is required")
            cleanup = fixture.get("teardown")
            if fixture_type == "http" and not cleanup:
                raise RunnerError(f"HTTP fixture {fixture_id} requires teardown")
            if fixture_type == "static":
                values = self.resolver.resolve(fixture.get("values") or {})
                for key, value in values.items():
                    self.resolver.set(key, value)
                record = {"fixture_id": fixture_id, "status": "passed", "type": fixture_type, "keys": sorted(values)}
            elif fixture_type == "http":
                request = self.resolver.resolve(fixture.get("request") or {})
                method = str(request.get("method", "POST")).upper()
                if method in DESTRUCTIVE_METHODS and fixture.get("destructive_confirmed") is not True:
                    raise RunnerError(f"HTTP fixture {fixture_id} destructive request requires destructive_confirmed=true")
                action = self.teardown.register(
                    f"fixture-{fixture_id}", cleanup, fixture.get("object_ref"), fixture.get("cleanup_extract") or {}
                )
                try:
                    exchange = self.http.execute(request)
                except Exception:
                    raise
                self.teardown.bind_response(action, exchange.get("response_json"))
                if exchange["status"] not in fixture.get("success_statuses", [200, 201, 204]):
                    raise RunnerError(f"fixture {fixture_id} returned HTTP {exchange['status']}")
                self._extract(fixture.get("extract") or {}, exchange.get("response_json"))
                ref, digest = self.evidence.write(f"fixture-{fixture_id}", {"fixture_id": fixture_id, "exchange": exchange})
                record = {"fixture_id": fixture_id, "status": "passed", "type": fixture_type, "evidence_ref": ref, "evidence_sha256": digest}
            else:
                raise RunnerError(f"unsupported fixture type: {fixture_type}")
            records.append(record)
        return records

    def _extract(self, mapping, response_json):
        for variable, path in mapping.items():
            try:
                value = json_path_get(response_json, path)
                self.resolver.set(variable, value)
                if variable.startswith("secret_") and isinstance(value, str) and len(value) >= 4:
                    self.evidence.secrets.append(value)
            except (KeyError, ValueError, TypeError) as exc:
                raise RunnerError(f"fixture extraction failed for {variable} from {path}") from exc


class TeardownManager:
    def __init__(self, resolver, http_executor, evidence_writer):
        self.resolver = resolver
        self.http = http_executor
        self.evidence = evidence_writer
        self.actions = []

    def register(self, case_id, teardown, object_ref=None, cleanup_extract=None):
        action = {
            "case_id": case_id,
            "teardown": deepcopy(teardown),
            "object_ref": deepcopy(object_ref),
            "cleanup_extract": deepcopy(cleanup_extract or {}),
            "cleanup_error": None,
            "bound": not bool(cleanup_extract),
        }
        self.actions.append(action)
        return action

    def bind_response(self, action, response_json):
        try:
            for variable, path in action.get("cleanup_extract", {}).items():
                value = json_path_get(response_json, path)
                self.resolver.set(variable, value)
                if variable.startswith("secret_") and isinstance(value, str) and len(value) >= 4:
                    self.evidence.secrets.append(value)
            action["bound"] = True
        except (KeyError, ValueError, TypeError) as exc:
            action["cleanup_error"] = f"cleanup extraction failed for {variable} from {path}: {exc}"
        return action

    def execute_all(self):
        if not self.actions:
            return {"status": "not_required", "pending_objects": [], "actions": [], "waived_cases": []}
        action_results = []
        pending = []
        for item in reversed(self.actions):
            case_id = item["case_id"]
            object_ref = None
            try:
                if item.get("cleanup_error"):
                    raise RunnerError(item["cleanup_error"])
                if not item.get("bound"):
                    raise RunnerError("cleanup variables unavailable because request delivery/response is unknown")
                object_ref = self.resolver.resolve(item.get("object_ref")) if item.get("object_ref") is not None else None
                spec = item["teardown"]
                teardown_type = spec.get("type", "http")
                if teardown_type != "http":
                    raise RunnerError(f"unsupported teardown type: {teardown_type}")
                request = self.resolver.resolve(spec.get("request") or {})
                method = str(request.get("method", "DELETE")).upper()
                if method not in {"DELETE", "POST", "PUT", "PATCH"}:
                    raise RunnerError("teardown must use DELETE, POST, PUT or PATCH")
                exchange = self.http.execute(request)
                allowed = spec.get("success_statuses", [200, 204])
                response_ok = exchange["status"] in allowed
                verify = spec.get("verify")
                if not verify:
                    raise RunnerError("teardown requires verification request and assertions")
                verify_request = self.resolver.resolve(verify.get("request") or {})
                verify_method = str(verify_request.get("method", "GET")).upper()
                if verify_method not in {"GET", "HEAD", "OPTIONS"}:
                    raise RunnerError("teardown verification must use GET, HEAD or OPTIONS")
                verify_exchange = self.http.execute(verify_request)
                verify_assertions = AssertionEngine().evaluate(self.resolver.resolve(verify.get("assertions")), verify_exchange)
                required_verify = [item for item in verify_assertions if item.get("required", True)]
                verified = bool(required_verify) and all(item.get("status") == "passed" for item in required_verify)
                status = "passed" if response_ok and verified else "failed"
                ref, digest = self.evidence.write(f"teardown-{case_id}", {"case_id": case_id, "exchange": exchange, "verification_exchange": verify_exchange, "verification_assertions": verify_assertions, "status": status})
                record = {"case_id": case_id, "status": status, "verified": verified, "evidence_ref": ref, "evidence_sha256": digest}
                if status != "passed":
                    pending.append({"case_id": case_id, "object_ref": redact(object_ref or item.get("object_ref"), secrets=self.evidence.secrets), "reason": f"HTTP {exchange['status']}"})
            except Exception as exc:
                actual = redact(str(exc), secrets=self.evidence.secrets)
                record = {"case_id": case_id, "status": "failed", "actual": actual}
                pending.append({"case_id": case_id, "object_ref": redact(object_ref or item.get("object_ref"), secrets=self.evidence.secrets), "reason": actual})
            action_results.append(record)
        return {"status": "failed" if pending else "passed", "pending_objects": pending, "actions": action_results}


class GateExecutor:
    def __init__(self, package, base_url, resolver, http_executor, evidence_writer, runtime_context=None):
        self.package = package
        self.base_url = base_url
        self.resolver = resolver
        self.http = http_executor
        self.evidence = evidence_writer
        self.runtime_context = runtime_context
        self.auth_probe_gate_id = (auth_probe_gate(package) or {}).get("gate_id") if auth_probe_strategy(package) == "declared" else None

    def execute(self):
        results = [self._environment_gate(), self._auth_gate(), self._cleanup_gate()]
        if any(item["status"] != "passed" for item in results):
            return results
        custom_gates = list(self.package.get("gates") or [])
        if not custom_gates:
            results.append(self._gate_result("GATE-RUNTIME", "运行时可达性", "failed", "test_case", "at least one executable runtime gate is required"))
            return results
        if self.auth_probe_gate_id and not any(gate.get("gate_id") == self.auth_probe_gate_id for gate in custom_gates):
            results.append(self._gate_result("GATE-AUTH-PROBE", "认证探测", "failed", "environment", f"declared auth probe gate is missing: {self.auth_probe_gate_id}"))
            return results
        # The authentication probe must run before any other runtime gate so an
        # unverified credential can never reach business cases.
        custom_gates.sort(key=lambda gate: 0 if gate.get("gate_id") == self.auth_probe_gate_id else 1)
        for gate in custom_gates:
            result = self._custom_gate(gate)
            if self.auth_probe_gate_id and gate.get("gate_id") == self.auth_probe_gate_id:
                result["role"] = "auth_probe"
                result["operation"] = auth_probe_operation(gate)
            results.append(result)
        return results

    def _environment_gate(self):
        gate_id = "GATE-ENV-SAFETY"
        parsed = urllib.parse.urlparse(self.base_url)
        production = bool((self.package.get("environment") or {}).get("production", False))
        host_tokens = set(re.split(r"[.\-_]", (parsed.hostname or "").lower()))
        origin = self._origin(parsed)
        allowed_origins_binding = binding_spec(self.package, "allowed_origins")
        allowed_origins_value = self.runtime_context.optional(allowed_origins_binding["key"], "") if allowed_origins_binding else ""
        allowed_origins = {
            item.strip().rstrip("/") for item in str(allowed_origins_value).split(",") if item.strip()
        } if allowed_origins_binding else set()
        local_host = (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
        safe = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not production
            and not (host_tokens & PRODUCTION_HOST_HINTS)
            and (local_host or (bool(allowed_origins) and origin in allowed_origins))
        )
        actual = "non-production URL accepted" if safe else "URL missing, invalid, or production-like"
        return self._gate_result(gate_id, "非生产环境", "passed" if safe else "failed", "environment", actual)

    @staticmethod
    def _origin(parsed):
        default = (parsed.scheme == "http" and parsed.port in {None, 80}) or (parsed.scheme == "https" and parsed.port in {None, 443})
        port = "" if default else f":{parsed.port}"
        return f"{parsed.scheme}://{parsed.hostname or ''}{port}"

    def _auth_gate(self):
        gate_id = "GATE-AUTH"
        auth_bindings = auth_binding_names(auth_config(self.package))
        missing = [name for name in sorted(auth_bindings) if not self.runtime_context.has(name)]
        if not auth_bindings:
            actual = "no credential required"
        elif missing:
            actual = "missing auth bindings: " + ", ".join(missing)
        else:
            actual = "credential bindings provided (presence only; validity requires an auth probe)"
        return self._gate_result(gate_id, "鉴权变量", "passed" if not missing else "failed", "environment", actual)

    def _cleanup_gate(self):
        gate_id = "GATE-CLEANUP"
        write_cases = [case for case in self.package.get("cases", []) if case.get("side_effect") in {"write_attempt", "write", "destructive"}]
        missing = [case.get("case_id") for case in write_cases if case.get("side_effect") == "write" and not case.get("teardown") and not case.get("cleanup_waiver")]
        destructive = [case.get("case_id") for case in write_cases if case.get("side_effect") == "destructive" and not case.get("destructive_confirmed")]
        waived = [case.get("cleanup_waiver", {}).get("waiver_id") for case in write_cases if case.get("cleanup_waiver")]
        if destructive:
            return self._gate_result(gate_id, "清理能力", "failed", "test_case", "destructive action not confirmed: " + ", ".join(destructive))
        actual = "write cleanup requirements satisfied"
        if waived:
            actual += "; cleanup waiver ids: " + ", ".join(waived)
        return self._gate_result(gate_id, "清理能力", "passed", "test_case", actual)

    def _custom_gate(self, gate):
        gate_id = gate.get("gate_id")
        if not gate_id:
            raise RunnerError("custom gate requires gate_id")
        try:
            request = self.resolver.resolve(gate.get("request") or {})
            exchange = self.http.execute(request)
            assertions = AssertionEngine().evaluate(self.resolver.resolve(gate.get("assertions") or {"http_status": [200], "business_code": {"path": "$.code", "equals": 200}, "required_fields": ["$.code"]}), exchange)
            required_assertions = [item for item in assertions if item.get("required", True)]
            passed = bool(required_assertions) and all(item["status"] == "passed" for item in required_assertions)
            sensitive_paths = gate.get("sensitive_json_paths") or []
            assertions = sanitize_assertions(assertions, sensitive_paths)
            payload = {"gate_id": gate_id, "exchange": sanitize_exchange(exchange, sensitive_paths), "assertions": assertions}
            ref, digest = self.evidence.write(f"gate-{gate_id}", payload)
            result = self._gate_result(gate_id, gate.get("name", gate_id), "passed" if passed else "failed", gate.get("classification", "environment"), f"HTTP {exchange['status']}", [ref], digest)
            result["runtime"] = True
            return result
        except Exception as exc:
            payload = {"gate_id": gate_id, "error": redact(str(exc), secrets=self.evidence.secrets), "assertions": []}
            ref, digest = self.evidence.write(f"gate-{gate_id}", payload)
            result = self._gate_result(gate_id, gate.get("name", gate_id), "error", "environment", payload["error"], [ref], digest)
            result["runtime"] = True
            return result

    @staticmethod
    def _gate_result(gate_id, name, status, classification, actual, evidence_refs=None, evidence_sha256=None):
        result = {"gate_id": gate_id, "name": name, "status": status, "classification": classification, "actual": actual, "evidence_refs": evidence_refs or []}
        if evidence_sha256:
            result["evidence_sha256"] = evidence_sha256
        return result


class ResultWriter:
    def __init__(self, run_dir, secrets=None):
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.secrets = secrets if secrets is not None else []

    def write(self, result):
        path = self.run_dir / "result.json"
        temporary = self.run_dir / ".result.json.tmp"
        safe_result = redact(result, secrets=self.secrets)
        temporary.write_text(json.dumps(safe_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        return path


class ApiTestRunner:
    def __init__(self, package, run_dir, runtime_context):
        self.package = package
        self.run_dir = Path(run_dir).resolve()
        self.environment = package.get("environment") or {}
        execution_errors = package_execution_preflight(package)
        if execution_errors:
            raise RunnerError("package execution preflight blocked: " + "; ".join(execution_errors))
        if runtime_context is None:
            raise RunnerError("explicit RuntimeContext is required")
        self.runtime_context = runtime_context
        context_errors = preflight(package, runtime_context)
        if context_errors:
            raise RunnerError("runtime preflight blocked: " + "; ".join(context_errors))
        self.allowed_bindings = package_binding_keys(package)
        base_url_binding = binding_spec(package, "base_url")
        self.base_url = str(runtime_context.require(base_url_binding["key"]))
        initial = {"run_id": f"run-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"}
        self.resolver = VariableResolver(initial, self.allowed_bindings, runtime_context)
        self.auth = AuthInjector(package, self.resolver)
        secrets = runtime_context.redaction_values()
        self.http = HttpExecutor(self.base_url, self.environment.get("timeout_seconds", 10), self.environment.get("max_response_bytes", 1024 * 1024), self.auth)
        self.evidence = EvidenceWriter(self.run_dir, secrets)
        self.teardown = TeardownManager(self.resolver, self.http, self.evidence)
        self.fixtures = FixtureManager(self.resolver, self.http, self.evidence, self.teardown)
        self.assertions = AssertionEngine()
        self.result_writer = ResultWriter(self.run_dir, secrets)
        self.write_attempt_risks = []
        self.residual_risks = []
        self.plan_fingerprint = None
        self.review = self.package.get("review") or {}
        plan = self.package.get("plan") or {}
        self.progress = ProgressWriter(self.run_dir, self.resolver.get("run_id"), len(self.package.get("cases") or []), plan.get("plan_id"))

    def run(self):
        started_at = now_iso()
        run_id = self.resolver.get("run_id")
        self.progress.update("initializing", "validate", "校验测试包")
        self._validate_package()
        self.progress.update("gate_running", "gates", "环境与鉴权门禁")
        gate_results = GateExecutor(self.package, self.base_url, self.resolver, self.http, self.evidence, self.runtime_context).execute()
        case_results = []
        fixture_records = []
        cleanup = {"status": "not_required", "pending_objects": [], "waived_cases": []}
        known_gaps = list(self.package.get("known_gaps") or [])

        failed_gates = [gate for gate in gate_results if gate["status"] in {"failed", "error"}]
        if failed_gates:
            failed_ids = [gate["gate_id"] for gate in failed_gates]
            case_results = [self._not_run_case(case, failed_ids, "自动门禁未通过") for case in self.package.get("cases", [])]
            self.progress.update("blocked", "gates", "门禁阻断", blocked_cases=len(case_results), completed_cases=len(case_results), completed=len(case_results))
        else:
            try:
                self.progress.update("fixture_running", "fixtures", "前置数据准备")
                fixture_records = self.fixtures.prepare(self.package.get("fixtures") or [])
                if fixture_records:
                    gate_results.append({"gate_id": "GATE-FIXTURES", "name": "测试夹具", "status": "passed", "classification": "test_case", "actual": f"prepared {len(fixture_records)} fixture(s)", "evidence_refs": [item["evidence_ref"] for item in fixture_records if item.get("evidence_ref")]})
            except Exception as exc:
                gate = {"gate_id": "GATE-FIXTURES", "name": "测试夹具", "status": "error", "classification": "test_case", "actual": redact(str(exc), secrets=self.evidence.secrets), "evidence_refs": []}
                gate_results.append(gate)
                case_results = [self._not_run_case(case, [gate["gate_id"]], "测试夹具准备失败") for case in self.package.get("cases", [])]
                cleanup = self.teardown.execute_all()

            if not case_results:
                case_results = self._run_cases()
                cleanup = self.teardown.execute_all()
                self.progress.update("cleanup_running", "cleanup", "清理测试副作用", completed_cases=len(case_results), completed=len(case_results))

        waived_cases = self._waived_cleanup_cases(case_results)
        if waived_cases:
            cleanup["waived_cases"] = waived_cases
            cleanup.setdefault("actions", [])
            cleanup.setdefault("pending_objects", [])
            cleanup["pending_objects"].extend({"case_id": item["case_id"], "object_ref": item["object_ref"], "reason": "risk accepted", "risk": "accepted"} for item in waived_cases)
            if cleanup.get("status") not in {"failed", "error"}:
                cleanup["status"] = "waived"
            cleanup["details"] = "存在明确批准的无 teardown 风险豁免；残留对象已登记，不能视为清理通过。"
        if self.write_attempt_risks:
            cleanup = {
                "status": "failed",
                "pending_objects": deepcopy(self.write_attempt_risks) + cleanup.get("pending_objects", []),
                "actions": cleanup.get("actions", []),
                "waived_cases": cleanup.get("waived_cases", []),
                "details": "至少一个 write_attempt 未满足预期拒绝；只读 verification 已执行，仍需人工核对副作用风险。",
            }
        if self.residual_risks:
            cleanup.setdefault("pending_objects", []).extend(deepcopy(self.residual_risks))
            cleanup["details"] = (cleanup.get("details") or "") + " 存在已登记但未自动清理的普通非生产测试残留；不得将结果描述为完整清理通过。"
            if cleanup.get("status") == "not_required":
                cleanup["status"] = "not_run"
        requirement_trace = self._build_requirement_trace(case_results)
        coverage = self._build_coverage(requirement_trace, case_results)
        verdict = self._verdict(gate_results, case_results, cleanup, requirement_trace)
        execution_trace = self._build_execution_trace(gate_results, fixture_records, case_results, cleanup, verdict)
        summary = {
            "total": len(case_results),
            "passed": sum(1 for item in case_results if item.get("status") == "passed"),
            "failed": sum(1 for item in case_results if item.get("status") == "failed"),
            "errors": sum(1 for item in case_results if item.get("status") == "error"),
            "skipped": sum(1 for item in case_results if item.get("status") == "skipped"),
            "not_run": sum(1 for item in case_results if item.get("status") == "not_run"),
        }
        summary["executed"] = summary["passed"] + summary["failed"] + summary["errors"]
        self.progress.update("validating", "validate_result", "校验运行结果", completed_cases=len(case_results), completed=len(case_results))
        result = {
            "schema_version": "1.1",
            "run_id": run_id,
            "package_id": self.package.get("package_id"),
            "mode": self.package.get("mode"),
            "plan": {"plan_fingerprint": self.plan_fingerprint, "title": (self.package.get("plan") or {}).get("title") or self.package.get("title")},
            "review": {key: value for key, value in self.review.items() if key not in {"comment", "findings"}},
            "contract": self.package.get("contract") or {},
            "test_config": self.package.get("test_config") or {},
            "project": redact(self.package.get("project") or {}, secrets=self.evidence.secrets),
            "environment": {"environment_id": self.environment.get("environment_id", "unknown"), "base_url": redact_url(self.base_url), "production": bool(self.environment.get("production", False)), "runtime_status": self._environment_runtime_status(gate_results), "runtime_context": self.runtime_context.metadata()},
            "started_at": started_at,
            "finished_at": now_iso(),
            "gate_results": gate_results,
            "fixture_results": fixture_records,
            "case_results": case_results,
            "requirement_trace": requirement_trace,
            "coverage": coverage,
            "execution_trace": execution_trace,
            "cleanup": cleanup,
            "known_gaps": known_gaps,
            "summary": summary,
            "verdict": verdict,
        }
        result_path = self.result_writer.write(result)
        self.progress.update("completed", "completed", "运行完成", completed_cases=len(case_results), completed=len(case_results), current_case_id=None, current_case_title=None)
        return result_path

    @staticmethod
    def _environment_runtime_status(gate_results):
        """Authentication is only 'verified' when an executable auth probe passed."""
        status = {"http": "unknown", "auth": "unknown"}
        credential_gate_passed = None
        for gate in gate_results:
            gate_id = gate.get("gate_id")
            gate_status = gate.get("status")
            if gate_id == "GATE-ENV-SAFETY":
                status["http"] = "verified" if gate_status == "passed" else "unavailable"
            elif gate.get("role") == "auth_probe":
                status["auth"] = "verified" if gate_status == "passed" else "authentication_failed"
            elif gate_id == "GATE-AUTH":
                credential_gate_passed = gate_status == "passed"
        if status["auth"] == "unknown":
            status["auth"] = "declared_only" if credential_gate_passed else "authentication_failed"
            if status["auth"] == "declared_only":
                status["auth_detail"] = "credential presence confirmed; no auth probe executed"
        return status

    def _validate_package(self):
        required = ["schema_version", "package_id", "project", "test_config", "mode", "contract", "environment", "risk", "cases"]
        missing = [field for field in required if field not in self.package]
        if missing:
            raise RunnerError("package missing required fields: " + ", ".join(missing))
        if self.package.get("schema_version") != "2.1":
            raise RunnerError("package schema_version must be 2.1")
        if self.package.get("mode") not in {"PROBE", "EXECUTE"}:
            raise RunnerError("runner executes only PROBE or EXECUTE packages")
        self.plan_fingerprint = calculate_plan_fingerprint(self.package)
        config_ref = self.package.get("test_config") or {}
        required_config = ("path", "schema_version", "config_revision", "environment_id", "fingerprint")
        missing_config = [field for field in required_config if config_ref.get(field) in (None, "")]
        if missing_config:
            raise RunnerError("test_config missing required fields: " + ", ".join(missing_config))
        if config_ref.get("schema_version") != "2.1" or not isinstance(config_ref.get("config_revision"), int) or config_ref.get("config_revision") < 1:
            raise RunnerError("TEST_CONFIG_SCHEMA_UNSUPPORTED: test_config must reference Schema 2.1 with positive config_revision")
        if config_ref.get("environment_id") != self.environment.get("environment_id"):
            raise RunnerError("test_config.environment_id must match environment.environment_id")
        if not SHA256_PATTERN.fullmatch(str(config_ref.get("fingerprint", ""))):
            raise RunnerError("test_config.fingerprint must be sha256:<64 hex>")
        config_identity = load_test_config_identity(config_ref.get("path"))
        for field in ("schema_version", "config_revision", "fingerprint"):
            if str(config_identity.get(field)).lower() != str(config_ref.get(field)).lower():
                raise RunnerError(f"test config {field} mismatch")
        project_id = (self.package.get("project") or {}).get("workspace_id")
        if project_id and config_identity.get("project_id") != project_id:
            raise RunnerError("test config project_id does not match package project.workspace_id")
        contract = self.package.get("contract") or {}
        required_contract = (
            "profile_path", "governance_schema_version", "profile_id", "profile_revision",
            "code_baseline", "lifecycle", "document_fingerprint", "contract_fingerprint",
        )
        missing_contract = [field for field in required_contract if contract.get(field) in (None, "")]
        if missing_contract:
            raise RunnerError("contract missing required interface profile identity fields: " + ", ".join(missing_contract))
        allowed_execution_lifecycles = {"minimally_usable", "confirmed", "executable"}
        if contract.get("lifecycle") not in allowed_execution_lifecycles:
            raise RunnerError("contract lifecycle must be minimally_usable, confirmed, or executable for EXECUTE")
        profile_identity = load_interface_profile_identity(contract.get("profile_path"))
        for field in required_contract[1:]:
            if str(profile_identity.get(field)).lower() != str(contract.get(field)).lower():
                raise RunnerError(f"interface profile {field} mismatch")
        if profile_identity.get("lifecycle") not in allowed_execution_lifecycles:
            raise RunnerError("interface profile lifecycle does not allow EXECUTE")
        if not isinstance(self.package.get("cases"), list) or not self.package["cases"]:
            raise RunnerError("package requires at least one case")
        review_errors = validate_review(self.package, require_execute=self.package.get("mode") == "EXECUTE")
        if review_errors:
            raise RunnerError("; ".join(review_errors))
        risk = self.package.get("risk") or {}
        if any(case.get("cleanup_waiver") for case in self.package["cases"]) and risk.get("cleanup_waiver_enabled") is not True:
            raise RunnerError("risk.cleanup_waiver_enabled=true is required when cleanup_waiver is used")
        if any(case.get("cleanup_waiver") for case in self.package["cases"]) and self.environment.get("production") is not False:
            raise RunnerError("cleanup_waiver requires environment.production=false")
        case_ids = [case.get("case_id") for case in self.package["cases"]]
        if any(not item for item in case_ids) or len(case_ids) != len(set(case_ids)):
            raise RunnerError("case_id values must be non-empty and unique")
        self._validate_traceability()
        read_only_binding = binding_spec(self.package, "read_only_operations")
        read_only_value = self.runtime_context.optional(read_only_binding["key"], "") if read_only_binding else ""
        allowed_read_only = {
            item.strip() for item in str(read_only_value).split(",") if item.strip()
        } if read_only_binding else set()
        known = set(case_ids)
        dependencies = {}
        for case in self.package["cases"]:
            case_id = case["case_id"]
            depends_on = set(case.get("depends_on") or [])
            unknown = depends_on - known
            if unknown:
                raise RunnerError(f"case {case_id} has unknown dependencies: {sorted(unknown)}")
            dependencies[case_id] = depends_on
            assertions = case.get("assertions")
            if not isinstance(assertions, list) or not assertions:
                raise RunnerError(f"case {case_id} requires list assertions with stable assertion_id values")
            verification = case.get("verification") or {}
            verification_assertions = verification.get("assertions") or []
            all_assertions = assertions + verification_assertions
            assertion_ids = [item.get("assertion_id") for item in all_assertions if isinstance(item, dict)]
            if len(assertion_ids) != len(all_assertions) or any(not item for item in assertion_ids) or len(assertion_ids) != len(set(assertion_ids)):
                raise RunnerError(f"case {case_id} assertion_id values must be non-empty and unique")
            known_rules = {item.get("rule_id") for item in self.package.get("business_rules") or []}
            for assertion in all_assertions:
                rule_refs = set(assertion.get("business_rule_refs") or [])
                if not rule_refs or not rule_refs.issubset(known_rules):
                    raise RunnerError(f"case {case_id} assertion {assertion.get('assertion_id')} requires valid business_rule_refs")
            side_effect = case.get("side_effect")
            if side_effect not in {"none", "read", "write_attempt", "write", "destructive"}:
                raise RunnerError(f"case {case_id} has invalid or missing side_effect")
            raw_method = (case.get("request") or {}).get("method", "GET")
            if not isinstance(raw_method, str) or VARIABLE_PATTERN.search(raw_method):
                raise RunnerError(f"case {case_id} HTTP method must be a static string")
            method = raw_method.upper()
            inferred = "destructive" if method in DESTRUCTIVE_METHODS else "write" if method in WRITE_METHODS else "read"
            operation = f"{method} {(case.get('request') or {}).get('path', '')}"
            read_only_override = operation in allowed_read_only
            if inferred in {"write", "destructive"} and side_effect in {"none", "read"} and not read_only_override:
                raise RunnerError(f"case {case_id} non-safe operation is not allowlisted as read-only: {operation}")
            effective_effect = "destructive" if inferred == "destructive" else side_effect
            if effective_effect == "write_attempt":
                if method not in WRITE_METHODS:
                    raise RunnerError(f"case {case_id} write_attempt requires POST, PUT or PATCH")
                if case.get("teardown") or case.get("cleanup_extract"):
                    raise RunnerError(f"case {case_id} write_attempt must use read-only verification, not teardown")
                verify = case.get("verification") or {}
                verify_request = verify.get("request") or {}
                verify_method = verify_request.get("method")
                verify_operation = f"{str(verify_method).upper()} {verify_request.get('path', '')}"
                verify_is_safe = (
                    isinstance(verify_method, str)
                    and not VARIABLE_PATTERN.search(verify_method)
                    and (verify_method.upper() in {"GET", "HEAD", "OPTIONS"} or verify_operation in allowed_read_only)
                )
                if not verify_is_safe:
                    raise RunnerError(f"case {case_id} write_attempt verification requires a safe or allowlisted read-only operation")
                if not verification_assertions:
                    raise RunnerError(f"case {case_id} write_attempt verification assertions are required")
                if case.get("acceptance_guard") != "response_must_reject_before_persistence":
                    raise RunnerError(f"case {case_id} write_attempt requires explicit rejection acceptance_guard")
            waiver = case.get("cleanup_waiver")
            if waiver is not None:
                if not isinstance(waiver, dict):
                    raise RunnerError(f"case {case_id} cleanup_waiver must be an object")
                if effective_effect != "write":
                    raise RunnerError(f"case {case_id} cleanup_waiver is only allowed for side_effect=write")
                if case.get("batch") is True or case.get("batch_operation") is True or (self.package.get("risk") or {}).get("side_effect_level") == "batch":
                    raise RunnerError(f"case {case_id} cleanup_waiver is forbidden for batch operations")
                if case.get("teardown") or case.get("cleanup_extract"):
                    raise RunnerError(f"case {case_id} cleanup_waiver is mutually exclusive with teardown/cleanup_extract")
                required_waiver = ("waiver_id", "scope", "reason", "residual_risk", "approved_by", "approved_at", "expires_at", "max_requests", "data_tag")
                missing_waiver = [field for field in required_waiver if field not in waiver or waiver.get(field) in (None, "")]
                if missing_waiver or waiver.get("scope") != "case" or waiver.get("max_requests") != 1 or not isinstance(waiver.get("data_tag"), str) or not waiver.get("data_tag"):
                    raise RunnerError(f"case {case_id} cleanup_waiver has invalid required fields")
                approved_at = parse_iso8601_with_timezone(waiver.get("approved_at"), "approved_at")
                expires_at = parse_iso8601_with_timezone(waiver.get("expires_at"), "expires_at")
                if expires_at <= approved_at or expires_at <= datetime.now(timezone.utc):
                    raise RunnerError(f"case {case_id} cleanup_waiver is expired or has invalid validity range")
                if not case.get("object_ref") or contains_variable(case.get("object_ref")):
                    raise RunnerError(f"case {case_id} cleanup_waiver requires a static object_ref")
                if case.get("max_executions") != 1:
                    raise RunnerError(f"case {case_id} cleanup_waiver requires max_executions=1")
                request = case.get("request") or {}
                if not body_contains_tag(request, waiver.get("data_tag")):
                    raise RunnerError(f"case {case_id} request body must contain cleanup_waiver.data_tag")
            if effective_effect in {"write", "destructive"} and waiver is None:
                if effective_effect == "destructive":
                    for field in ("cleanup_extract", "object_ref", "teardown"):
                        if not case.get(field):
                            raise RunnerError(f"case {case_id} destructive request requires {field}")
                    self._validate_teardown(case_id, case.get("teardown"))
                elif case.get("teardown"):
                    for field in ("cleanup_extract", "object_ref"):
                        if not case.get(field):
                            raise RunnerError(f"case {case_id} write request with teardown requires {field}")
                    self._validate_teardown(case_id, case.get("teardown"))
                elif not case.get("object_ref"):
                    raise RunnerError(f"case {case_id} write request without teardown requires object_ref for residual tracking")
            if effective_effect == "destructive" and case.get("destructive_confirmed") is not True:
                raise RunnerError(f"case {case_id} destructive request requires destructive_confirmed=true")
        pending = set(dependencies)
        resolved = set()
        while pending:
            ready = {case_id for case_id in pending if dependencies[case_id].issubset(resolved)}
            if not ready:
                raise RunnerError("case dependency graph contains a cycle")
            pending -= ready
            resolved |= ready

        gate_ids = [gate.get("gate_id") for gate in self.package.get("gates") or []]
        if any(not item for item in gate_ids) or len(gate_ids) != len(set(gate_ids)):
            raise RunnerError("gate_id values must be non-empty and unique")
        for gate in self.package.get("gates") or []:
            raw_method = (gate.get("request") or {}).get("method", "GET")
            if not isinstance(raw_method, str) or VARIABLE_PATTERN.search(raw_method):
                raise RunnerError(f"gate {gate.get('gate_id')} HTTP method must be a static string")
            method = raw_method.upper()
            operation = f"{method} {(gate.get('request') or {}).get('path', '')}"
            if method not in {"GET", "HEAD", "OPTIONS"}:
                raise RunnerError(f"gate {gate.get('gate_id')} must use GET, HEAD or OPTIONS: {operation}")

        fixture_ids = [fixture.get("fixture_id") for fixture in self.package.get("fixtures") or []]
        if any(not item for item in fixture_ids) or len(fixture_ids) != len(set(fixture_ids)):
            raise RunnerError("fixture_id values must be non-empty and unique")
        for fixture in self.package.get("fixtures") or []:
            if fixture.get("type", "static") == "http":
                for field in ("cleanup_extract", "object_ref", "teardown"):
                    if not fixture.get(field):
                        raise RunnerError(f"HTTP fixture {fixture.get('fixture_id')} requires {field}")
                self._validate_teardown(f"fixture {fixture.get('fixture_id')}", fixture.get("teardown"))

    def _validate_traceability(self):
        requirements = self.package.get("requirements")
        rules = self.package.get("business_rules")
        scenarios = self.package.get("scenarios")
        if not isinstance(requirements, list) or not requirements:
            raise RunnerError("package requires non-empty requirements")
        if not isinstance(rules, list) or not rules:
            raise RunnerError("package requires non-empty business_rules")
        if not isinstance(scenarios, list) or not scenarios:
            raise RunnerError("package requires non-empty scenarios")

        def unique_ids(items, key, label):
            values = [item.get(key) for item in items if isinstance(item, dict)]
            if len(values) != len(items) or any(not value for value in values) or len(values) != len(set(values)):
                raise RunnerError(f"{label} IDs must be non-empty and unique")
            return set(values)

        requirement_ids = unique_ids(requirements, "requirement_id", "requirement")
        rule_ids = unique_ids(rules, "rule_id", "business rule")
        scenario_ids = unique_ids(scenarios, "scenario_id", "scenario")
        case_ids = {case.get("case_id") for case in self.package.get("cases") or []}
        project_refs = set((self.package.get("project") or {}).get("requirement_refs") or [])
        if requirement_ids != project_refs:
            raise RunnerError("project.requirement_refs must exactly match declared requirement IDs")
        for rule in rules:
            if rule.get("requirement_id") not in requirement_ids:
                raise RunnerError(f"business rule {rule.get('rule_id')} references unknown requirement")
        for scenario in scenarios:
            if not set(scenario.get("requirement_refs") or []).issubset(requirement_ids):
                raise RunnerError(f"scenario {scenario.get('scenario_id')} references unknown requirement")
            if not set(scenario.get("business_rule_refs") or []).issubset(rule_ids):
                raise RunnerError(f"scenario {scenario.get('scenario_id')} references unknown business rule")
            refs = set(scenario.get("case_refs") or [])
            if not refs or not refs.issubset(case_ids):
                raise RunnerError(f"scenario {scenario.get('scenario_id')} requires valid case_refs")
        for case in self.package.get("cases") or []:
            if not set(case.get("requirement_refs") or []).issubset(requirement_ids) or not case.get("requirement_refs"):
                raise RunnerError(f"case {case.get('case_id')} requires valid requirement_refs")
            if not set(case.get("business_rule_refs") or []).issubset(rule_ids) or not case.get("business_rule_refs"):
                raise RunnerError(f"case {case.get('case_id')} requires valid business_rule_refs")
            if not set(case.get("scenario_refs") or []).issubset(scenario_ids) or not case.get("scenario_refs"):
                raise RunnerError(f"case {case.get('case_id')} requires valid scenario_refs")
        linked_rules = {ref for case in self.package.get("cases") or [] for ref in case.get("business_rule_refs") or []}
        missing_required = [rule.get("rule_id") for rule in rules if rule.get("required", True) and rule.get("rule_id") not in linked_rules]
        if missing_required:
            raise RunnerError("required business rules lack case coverage: " + ", ".join(missing_required))

    @staticmethod
    def _validate_teardown(identifier, teardown):
        if not isinstance(teardown, dict) or teardown.get("type", "http") != "http":
            raise RunnerError(f"{identifier} teardown must be HTTP")
        request = teardown.get("request") or {}
        method = request.get("method")
        if not isinstance(method, str) or VARIABLE_PATTERN.search(method) or method.upper() not in {"DELETE", "POST", "PUT", "PATCH"}:
            raise RunnerError(f"{identifier} teardown requires a static write method")
        verify = teardown.get("verify") or {}
        verify_request = verify.get("request") or {}
        verify_method = verify_request.get("method")
        if not isinstance(verify_method, str) or VARIABLE_PATTERN.search(verify_method) or verify_method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            raise RunnerError(f"{identifier} teardown verification requires a static safe method")
        assertions = verify.get("assertions")
        if not assertions:
            raise RunnerError(f"{identifier} teardown verification assertions are required")

    def _run_cases(self):
        cases = {case["case_id"]: case for case in self.package["cases"]}
        pending = set(cases)
        results = {}
        ordered = []
        while pending:
            ready = sorted(case_id for case_id in pending if set(cases[case_id].get("depends_on") or []).issubset(results))
            if not ready:
                raise RunnerError("case dependency graph contains a cycle")
            for case_id in ready:
                case = cases[case_id]
                self.progress.update("case_running", "execute_cases", "业务用例执行", current_case_id=case_id, current_case_title=case.get("name", case_id))
                case_started = time.monotonic()
                dependencies = case.get("depends_on") or []
                blocked = [item for item in dependencies if results[item]["status"] != "passed"]
                if blocked:
                    result = self._not_run_case(case, blocked, "依赖用例未通过")
                elif case.get("enabled", True) is False:
                    result = {"case_id": case_id, "name": case.get("name", case_id), "status": "skipped", "classification": "test_case", "request_sent": False, "expected": "case enabled", "actual": case.get("skip_reason", "case disabled"), "assertions": [], "evidence_refs": [], "blocked_by": [], "requirement_refs": list(case.get("requirement_refs") or []), "business_rule_refs": list(case.get("business_rule_refs") or []), "scenario_refs": list(case.get("scenario_refs") or []), "scenario_type": case.get("scenario_type", "positive"), "execution_trace": {"status": "skipped", "steps": [{"step_id": f"{case_id}-S01", "sequence": 1, "kind": "skipped", "status": "skipped", "reason": case.get("skip_reason", "case disabled")}]}}
                else:
                    result = self._run_case(case)
                results[case_id] = result
                ordered.append(result)
                self.progress.record_case(result.get("status"), time.monotonic() - case_started, case_id, case.get("name", case_id))
                pending.remove(case_id)
        return ordered

    def _run_case(self, case):
        case_id = case["case_id"]
        name = case.get("name", case_id)
        request_sent = False
        steps = []

        def step(kind, status, **details):
            record = {"step_id": f"{case_id}-S{len(steps) + 1:02d}", "sequence": len(steps) + 1, "kind": kind, "status": status}
            record.update(details)
            steps.append(record)
            return record

        try:
            request = self.resolver.resolve(case.get("request") or {})
            step("resolve_request", "passed", method=request.get("method", "GET"), path=request.get("path", ""))
            action = None
            waiver = case.get("cleanup_waiver")
            if case.get("side_effect") in {"write", "destructive"} and waiver is None and case.get("teardown"):
                action = self.teardown.register(
                    case_id, case.get("teardown"), case.get("object_ref"), case.get("cleanup_extract") or {}
                )
                step("register_teardown", "passed")
            exchange = self.http.execute(request)
            request_sent = True
            if case.get("side_effect") == "write" and waiver is None and not case.get("teardown"):
                self.residual_risks.append({"case_id": case_id, "object_ref": redact(case.get("object_ref"), secrets=self.evidence.secrets), "reason": "普通非生产写入未配置 teardown；残留对象需人工处置。", "risk": "accepted_pending_cleanup"})
                step("register_residual_risk", "passed", object_ref=case.get("object_ref"), reason="no teardown configured")
            if action is not None:
                self.teardown.bind_response(action, exchange.get("response_json"))
                step("bind_cleanup_variables", "passed" if not action.get("cleanup_error") else "failed", variables=sorted((case.get("cleanup_extract") or {}).keys()))
            assertion_spec = self.resolver.resolve(case.get("assertions"))
            assertions = self.assertions.evaluate(assertion_spec, exchange)
            required = [item for item in assertions if item.get("required", True)]
            primary_passed = bool(required) and all(item["status"] == "passed" for item in required)
            verification_exchange = None
            if case.get("side_effect") == "write_attempt":
                verification = case.get("verification") or {}
                verification_request = self.resolver.resolve(verification.get("request") or {})
                verification_exchange = self.http.execute(verification_request)
                verification_assertions = self.assertions.evaluate(
                    self.resolver.resolve(verification.get("assertions")), verification_exchange
                )
                assertions.extend(verification_assertions)
                step("read_only_verification", "passed" if all(item.get("status") == "passed" for item in verification_assertions if item.get("required", True)) else "failed", method=verification_exchange.get("method"), url=redact_url(verification_exchange.get("url", "")), http_status=verification_exchange.get("status"))
                if not primary_passed:
                    self.write_attempt_risks.append({"case_id": case_id, "reason": "rejection assertion failed; read-only verification executed"})
            required = [item for item in assertions if item.get("required", True)]
            passed = bool(required) and all(item["status"] == "passed" for item in required)
            sensitive_paths = case.get("sensitive_json_paths") or []
            if passed:
                self._extract(case.get("extract") or {}, exchange.get("response_json"), case_id)
                if case.get("extract"):
                    step("extract_variables", "passed", variables=sorted(case.get("extract")))
            assertions = sanitize_assertions(assertions, sensitive_paths)
            evidence_payload = {"case_id": case_id, "name": name, "exchange": sanitize_exchange(exchange, sensitive_paths), "verification_exchange": sanitize_exchange(verification_exchange, []) if verification_exchange else None, "assertions": assertions}
            evidence_ref, digest = self.evidence.write(f"case-{case_id}", evidence_payload)
            step("http_exchange", "passed", method=exchange.get("method"), url=redact_url(exchange.get("url", "")), http_status=exchange.get("status"), elapsed_ms=exchange.get("elapsed_ms"), evidence_refs=[evidence_ref], evidence_sha256=digest)
            if waiver is not None:
                step("cleanup_waiver", "passed", waiver_id=waiver.get("waiver_id"), object_ref=case.get("object_ref"), data_tag=waiver.get("data_tag"), reason=waiver.get("reason"), residual_risk=waiver.get("residual_risk"))
            for assertion in assertions:
                assertion["evidence_ref"] = evidence_ref
                assertion["evidence_sha256"] = digest
                step("assertion", assertion.get("status"), assertion_id=assertion.get("assertion_id"), assertion_type=assertion.get("type"), evidence_refs=[evidence_ref], evidence_sha256=digest)
            status = "passed" if passed else "failed"
            step("case_complete", status)
            return {
                "case_id": case_id,
                "name": name,
                "status": status,
                "classification": None if passed else case.get("failure_classification", "product_defect"),
                "request_sent": True,
                "expected": self._expected_summary(assertions),
                "actual": f"HTTP {exchange['status']}; required assertions {'passed' if passed else 'failed'}",
                "assertions": assertions,
                "evidence_refs": [evidence_ref],
                "blocked_by": [],
                "requirement_refs": list(case.get("requirement_refs") or []),
                "business_rule_refs": list(case.get("business_rule_refs") or []),
                "scenario_refs": list(case.get("scenario_refs") or []),
                "scenario_type": case.get("scenario_type", "positive"),
                "cleanup_disposition": "waived" if waiver is not None else "teardown",
                "cleanup_waiver_id": waiver.get("waiver_id") if waiver is not None else None,
                "execution_trace": {"status": status, "steps": steps},
            }
        except Exception as exc:
            request_delivery = "confirmed" if request_sent else getattr(exc, "request_delivery", "not_sent")
            if case.get("side_effect") == "write_attempt" and request_delivery in {"confirmed", "unknown"}:
                self.write_attempt_risks.append({"case_id": case_id, "reason": f"write_attempt execution error with request_delivery={request_delivery}; side effect requires review"})
            cleanup_registered = any(item.get("case_id") == case_id for item in self.teardown.actions)
            step("case_error", "error", actual=redact(str(exc), secrets=self.evidence.secrets), request_delivery=request_delivery)
            return {"case_id": case_id, "name": name, "status": "error", "classification": "environment" if isinstance(exc, RunnerError) else "test_case", "request_sent": request_sent, "request_delivery": request_delivery, "cleanup_registered": cleanup_registered, "expected": "request executes and deterministic assertions complete", "actual": redact(str(exc), secrets=self.evidence.secrets), "assertions": [], "evidence_refs": [], "blocked_by": [], "requirement_refs": list(case.get("requirement_refs") or []), "business_rule_refs": list(case.get("business_rule_refs") or []), "scenario_refs": list(case.get("scenario_refs") or []), "scenario_type": case.get("scenario_type", "positive"), "execution_trace": {"status": "error", "steps": steps}}

    def _waived_cleanup_cases(self, case_results):
        waived = []
        for result in case_results:
            case = next(item for item in self.package.get("cases") or [] if item.get("case_id") == result.get("case_id"))
            waiver = case.get("cleanup_waiver")
            if waiver and result.get("request_sent") is True:
                waived.append({
                    "case_id": case.get("case_id"),
                    "status": "waived",
                    "waiver_id": waiver.get("waiver_id"),
                    "reason": waiver.get("reason"),
                    "residual_risk": waiver.get("residual_risk"),
                    "approved_by": waiver.get("approved_by"),
                    "approved_at": waiver.get("approved_at"),
                    "expires_at": waiver.get("expires_at"),
                    "data_tag": waiver.get("data_tag"),
                    "object_ref": deepcopy(case.get("object_ref")),
                })
        return waived

    def _extract(self, mapping, response_json, case_id):
        for variable, path in mapping.items():
            try:
                value = json_path_get(response_json, path)
                self.resolver.set(variable, value)
                if variable.startswith("secret_") and isinstance(value, str) and len(value) >= 4:
                    self.evidence.secrets.append(value)
            except (KeyError, ValueError, TypeError) as exc:
                raise RunnerError(f"case {case_id} extraction failed for {variable} from {path}") from exc

    @staticmethod
    def _not_run_case(case, blocked_by, reason):
        case_id = case.get("case_id")
        return {"case_id": case_id, "name": case.get("name", case_id), "status": "not_run", "classification": "environment", "request_sent": False, "expected": "business request executed", "actual": reason, "assertions": [], "evidence_refs": [], "blocked_by": list(blocked_by), "requirement_refs": list(case.get("requirement_refs") or []), "business_rule_refs": list(case.get("business_rule_refs") or []), "scenario_refs": list(case.get("scenario_refs") or []), "scenario_type": case.get("scenario_type", "positive"), "execution_trace": {"status": "not_run", "steps": [{"step_id": f"{case_id}-S01", "sequence": 1, "kind": "blocked", "status": "not_run", "blocked_by": list(blocked_by), "reason": reason}]}}

    @staticmethod
    def _expected_summary(assertions):
        return "; ".join(f"{item.get('type')} {item.get('operator')} {item.get('expected')}" for item in assertions)

    def _build_requirement_trace(self, case_results):
        case_map = {case.get("case_id"): case for case in case_results}

        def combined_status(statuses):
            if not statuses:
                return "uncovered"
            if "error" in statuses:
                return "error"
            if "failed" in statuses:
                return "failed"
            if "not_run" in statuses or "skipped" in statuses:
                return "not_run"
            if all(status == "passed" for status in statuses):
                return "passed"
            return "inconclusive"

        rule_results = []
        for rule in self.package.get("business_rules") or []:
            rule_id = rule.get("rule_id")
            linked = [case for case in case_results if rule_id in (case.get("business_rule_refs") or [])]
            assertion_refs = []
            evidence_refs = []
            for case in linked:
                evidence_refs.extend(case.get("evidence_refs") or [])
                for assertion in case.get("assertions") or []:
                    if rule_id in (assertion.get("business_rule_refs") or []):
                        assertion_refs.append({"case_id": case.get("case_id"), "assertion_id": assertion.get("assertion_id"), "status": assertion.get("status")})
            status = combined_status([case.get("status") for case in linked])
            if status == "passed" and (not assertion_refs or any(item.get("status") != "passed" for item in assertion_refs)):
                status = "inconclusive"
            rule_results.append({**deepcopy(rule), "status": status, "case_refs": [case.get("case_id") for case in linked], "assertion_refs": assertion_refs, "evidence_refs": sorted(set(evidence_refs))})

        scenario_results = []
        for scenario in self.package.get("scenarios") or []:
            refs = scenario.get("case_refs") or []
            linked = [case_map[case_id] for case_id in refs if case_id in case_map]
            evidence_refs = sorted({ref for case in linked for ref in case.get("evidence_refs") or []})
            scenario_results.append({**deepcopy(scenario), "status": combined_status([case.get("status") for case in linked]), "evidence_refs": evidence_refs})

        requirement_results = []
        for requirement in self.package.get("requirements") or []:
            requirement_id = requirement.get("requirement_id")
            linked_rules = [rule for rule in rule_results if rule.get("requirement_id") == requirement_id]
            linked_scenarios = [scenario for scenario in scenario_results if requirement_id in (scenario.get("requirement_refs") or [])]
            required_statuses = [rule.get("status") for rule in linked_rules if rule.get("required", True)]
            status = combined_status(required_statuses)
            evidence_refs = sorted({ref for rule in linked_rules for ref in rule.get("evidence_refs") or []})
            requirement_results.append({**deepcopy(requirement), "status": status, "business_rule_refs": [rule.get("rule_id") for rule in linked_rules], "scenario_refs": [scenario.get("scenario_id") for scenario in linked_scenarios], "evidence_refs": evidence_refs})

        required_requirements = [item for item in requirement_results if item.get("required", True)]
        return {
            "requirements": requirement_results,
            "business_rules": rule_results,
            "scenarios": scenario_results,
            "summary": {
                "requirements_total": len(requirement_results),
                "requirements_passed": sum(1 for item in requirement_results if item.get("status") == "passed"),
                "rules_total": len(rule_results),
                "rules_passed": sum(1 for item in rule_results if item.get("status") == "passed"),
                "required_complete": bool(required_requirements) and all(item.get("status") == "passed" for item in required_requirements),
            },
        }

    @staticmethod
    def _build_coverage(requirement_trace, case_results):
        requirements = requirement_trace.get("requirements") or []
        total = len(requirements)
        touched = sum(1 for item in requirements if item.get("status") in {"passed", "failed", "error", "inconclusive"})
        complete = sum(1 for item in requirements if item.get("status") in {"passed", "failed", "error"})
        passed = sum(1 for item in requirements if item.get("status") == "passed")
        def rate(numerator):
            return {"numerator": numerator, "denominator": total, "percent": round(numerator / total * 100, 1) if total else None}
        executed = sum(1 for item in case_results if item.get("status") in {"passed", "failed", "error"})
        planned = len(case_results)
        return {
            "design": rate(sum(1 for item in requirements if item.get("business_rule_refs"))),
            "execution_touch": rate(touched),
            "complete_scenario": rate(complete),
            "acceptance_pass": rate(passed),
            "case_execution": {"numerator": executed, "denominator": planned, "percent": round(executed / planned * 100, 1) if planned else None},
        }

    @staticmethod
    def _build_execution_trace(gates, fixtures, cases, cleanup, verdict):
        steps = []

        def add(kind, status, ref=None, evidence_refs=None, **details):
            item = {"step_id": f"RUN-S{len(steps) + 1:03d}", "sequence": len(steps) + 1, "kind": kind, "status": status}
            if ref:
                item["ref"] = ref
            if evidence_refs:
                item["evidence_refs"] = evidence_refs
            item.update(details)
            steps.append(item)

        add("package_validation", "passed")
        for gate in gates:
            add("gate", gate.get("status"), gate.get("gate_id"), gate.get("evidence_refs"))
        for fixture in fixtures:
            add("fixture", fixture.get("status"), fixture.get("fixture_id"), [fixture.get("evidence_ref")] if fixture.get("evidence_ref") else None)
        for case in cases:
            add("case", case.get("status"), case.get("case_id"), case.get("evidence_refs"), blocked_by=case.get("blocked_by") or [])
        for action in cleanup.get("actions") or []:
            add("teardown", action.get("status"), action.get("case_id"), [action.get("evidence_ref")] if action.get("evidence_ref") else None, verified=bool(action.get("verified")))
        for item in cleanup.get("waived_cases") or []:
            add("cleanup_waiver", "waived", item.get("case_id"), waiver_id=item.get("waiver_id"), object_ref=item.get("object_ref"), data_tag=item.get("data_tag"), reason=item.get("reason"), residual_risk=item.get("residual_risk"))
        add("verdict", verdict)
        return {"trace_version": "1.0", "status": "completed", "steps": steps}

    @staticmethod
    def _verdict(gates, cases, cleanup, requirement_trace):
        if any(gate.get("status") in {"failed", "error"} for gate in gates):
            return "blocked"
        statuses = [case.get("status") for case in cases]
        if "error" in statuses or cleanup.get("status") == "failed":
            return "error"
        if "failed" in statuses or any(item.get("status") == "failed" for item in (requirement_trace.get("requirements") or [])):
            return "failed"
        if "not_run" in statuses or "skipped" in statuses or (requirement_trace.get("summary") or {}).get("required_complete") is not True:
            return "partial"
        if cleanup.get("waived_cases") or cleanup.get("pending_objects"):
            return "inconclusive"
        if statuses and all(status == "passed" for status in statuses) and cleanup.get("status") not in {"failed", "not_run", "waived"} and (requirement_trace.get("summary") or {}).get("required_complete") is True:
            return "passed"
        return "inconclusive"


def main():
    parser = argparse.ArgumentParser(description="Runner is an in-process API only")
    parser.parse_args()
    print(json.dumps({"status": "blocked", "error": "runner.py requires an in-memory RuntimeContext; use the host Python API"}, ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
