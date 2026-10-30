#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
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


SUPPORTED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
SENSITIVE_KEYS = {
    "authorization", "cookie", "setcookie", "token", "accesstoken", "refreshtoken",
    "password", "secret", "clientsecret", "apikey", "xapikey", "phone", "principal",
}
PRODUCTION_HOST_HINTS = {"prod", "production", "live"}
WRITE_METHODS = {"POST", "PUT", "PATCH"}
DESTRUCTIVE_METHODS = {"DELETE"}
VARIABLE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")


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


def collect_sensitive_env_values():
    values = []
    for key, value in os.environ.items():
        canonical = canonical_key(key)
        sensitive_name = canonical in SENSITIVE_KEYS or any(canonical.endswith(marker) for marker in SENSITIVE_KEYS)
        if value and len(value) >= 8 and sensitive_name:
            values.append(value)
    return values


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
    def __init__(self, initial=None, allowed_env=None):
        self.values = dict(initial or {})
        self.allowed_env = set(allowed_env or [])

    def set(self, name, value):
        self.values[name] = value

    def get(self, name):
        if name in self.values:
            return self.values[name]
        if name in self.allowed_env:
            value = os.environ.get(name)
            if value is None:
                raise RunnerError(f"required environment variable is missing: {name}")
            return value
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


class HttpExecutor:
    def __init__(self, base_url, timeout=10.0, max_response_bytes=1024 * 1024):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self.allowed_origin = self._origin(self.base_url)

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
    def __init__(self, package, base_url, resolver, http_executor, evidence_writer):
        self.package = package
        self.base_url = base_url
        self.resolver = resolver
        self.http = http_executor
        self.evidence = evidence_writer

    def execute(self):
        results = [self._environment_gate(), self._auth_gate(), self._cleanup_gate()]
        if any(item["status"] != "passed" for item in results):
            return results
        custom_gates = list(self.package.get("gates") or [])
        if not custom_gates:
            results.append(self._gate_result("GATE-RUNTIME", "运行时可达性", "failed", "test_case", "at least one executable runtime gate is required"))
            return results
        for gate in custom_gates:
            results.append(self._custom_gate(gate))
        return results

    def _environment_gate(self):
        gate_id = "GATE-ENV-SAFETY"
        parsed = urllib.parse.urlparse(self.base_url)
        production = bool((self.package.get("environment") or {}).get("production", False))
        host_tokens = set(re.split(r"[.\-_]", (parsed.hostname or "").lower()))
        origin = self._origin(parsed)
        allowed_origins_env = (self.package.get("environment") or {}).get("allowed_origins_env")
        allowed_origins = {
            item.strip().rstrip("/") for item in os.environ.get(allowed_origins_env, "").split(",") if item.strip()
        } if allowed_origins_env else set()
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
        auth_env = (self.package.get("environment") or {}).get("auth_env") or []
        missing = [name for name in auth_env if not os.environ.get(name)]
        actual = "required auth variables available" if not missing else "missing auth variables: " + ", ".join(missing)
        return self._gate_result(gate_id, "鉴权变量", "passed" if not missing else "failed", "environment", actual)

    def _cleanup_gate(self):
        gate_id = "GATE-CLEANUP"
        write_cases = [case for case in self.package.get("cases", []) if case.get("side_effect") in {"write", "destructive"}]
        missing = [case.get("case_id") for case in write_cases if not case.get("teardown") and not case.get("cleanup_waiver")]
        destructive = [case.get("case_id") for case in write_cases if case.get("side_effect") == "destructive" and not case.get("destructive_confirmed")]
        waived = [case.get("cleanup_waiver", {}).get("waiver_id") for case in write_cases if case.get("cleanup_waiver")]
        if missing:
            return self._gate_result(gate_id, "清理能力", "failed", "test_case", "missing teardown: " + ", ".join(missing))
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
    def __init__(self, package, run_dir):
        self.package = package
        self.run_dir = Path(run_dir).resolve()
        self.environment = package.get("environment") or {}
        self.allowed_env = set(self.environment.get("auth_env") or [])
        base_url_env = self.environment.get("base_url_env")
        allowed_origins_env = self.environment.get("allowed_origins_env")
        read_only_operations_env = self.environment.get("read_only_operations_env")
        if allowed_origins_env:
            self.allowed_env.add(allowed_origins_env)
        if read_only_operations_env:
            self.allowed_env.add(read_only_operations_env)
        if base_url_env:
            self.allowed_env.add(base_url_env)
            self.base_url = os.environ.get(base_url_env, "")
        else:
            self.base_url = self.environment.get("base_url", "")
        initial = {"run_id": f"run-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"}
        self.resolver = VariableResolver(initial, self.allowed_env)
        secrets = [os.environ.get(name) for name in (self.environment.get("auth_env") or []) if os.environ.get(name)] + collect_sensitive_env_values()
        self.http = HttpExecutor(self.base_url, self.environment.get("timeout_seconds", 10), self.environment.get("max_response_bytes", 1024 * 1024))
        self.evidence = EvidenceWriter(self.run_dir, secrets)
        self.teardown = TeardownManager(self.resolver, self.http, self.evidence)
        self.fixtures = FixtureManager(self.resolver, self.http, self.evidence, self.teardown)
        self.assertions = AssertionEngine()
        self.result_writer = ResultWriter(self.run_dir, secrets)
        self.write_attempt_risks = []

    def run(self):
        started_at = now_iso()
        run_id = self.resolver.get("run_id")
        self._validate_package()
        gate_results = GateExecutor(self.package, self.base_url, self.resolver, self.http, self.evidence).execute()
        case_results = []
        fixture_records = []
        cleanup = {"status": "not_required", "pending_objects": [], "waived_cases": []}
        known_gaps = list(self.package.get("known_gaps") or [])

        failed_gates = [gate for gate in gate_results if gate["status"] in {"failed", "error"}]
        if failed_gates:
            failed_ids = [gate["gate_id"] for gate in failed_gates]
            case_results = [self._not_run_case(case, failed_ids, "自动门禁未通过") for case in self.package.get("cases", [])]
        else:
            try:
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
        requirement_trace = self._build_requirement_trace(case_results)
        verdict = self._verdict(gate_results, case_results, cleanup, requirement_trace)
        execution_trace = self._build_execution_trace(gate_results, fixture_records, case_results, cleanup, verdict)
        result = {
            "schema_version": "1.1",
            "run_id": run_id,
            "package_id": self.package.get("package_id"),
            "mode": self.package.get("mode"),
            "contract": self.package.get("contract") or {},
            "project": redact(self.package.get("project") or {}, secrets=self.evidence.secrets),
            "environment": {"environment_id": self.environment.get("environment_id", "unknown"), "base_url": redact_url(self.base_url), "production": bool(self.environment.get("production", False))},
            "started_at": started_at,
            "finished_at": now_iso(),
            "gate_results": gate_results,
            "fixture_results": fixture_records,
            "case_results": case_results,
            "requirement_trace": requirement_trace,
            "execution_trace": execution_trace,
            "cleanup": cleanup,
            "known_gaps": known_gaps,
            "verdict": verdict,
        }
        return self.result_writer.write(result)

    def _validate_package(self):
        required = ["schema_version", "package_id", "project", "mode", "contract", "environment", "risk", "cases"]
        missing = [field for field in required if field not in self.package]
        if missing:
            raise RunnerError("package missing required fields: " + ", ".join(missing))
        if self.package.get("mode") not in {"PROBE", "EXECUTE"}:
            raise RunnerError("runner executes only PROBE or EXECUTE packages")
        contract = self.package.get("contract") or {}
        profile_path = contract.get("profile_path")
        fingerprint = contract.get("fingerprint", "")
        if contract.get("lifecycle") != "executable" or not profile_path or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", fingerprint):
            raise RunnerError("contract requires lifecycle=executable, profile_path and sha256 fingerprint")
        profile = Path(profile_path).resolve()
        if not profile.is_file():
            raise RunnerError(f"contract profile not found: {profile}")
        actual_fingerprint = "sha256:" + hashlib.sha256(profile.read_bytes()).hexdigest()
        if actual_fingerprint.lower() != fingerprint.lower():
            raise RunnerError("contract fingerprint mismatch")
        if not isinstance(self.package.get("cases"), list) or not self.package["cases"]:
            raise RunnerError("package requires at least one case")
        risk = self.package.get("risk") or {}
        if any(case.get("cleanup_waiver") for case in self.package["cases"]) and risk.get("cleanup_waiver_enabled") is not True:
            raise RunnerError("risk.cleanup_waiver_enabled=true is required when cleanup_waiver is used")
        if any(case.get("cleanup_waiver") for case in self.package["cases"]) and self.environment.get("production") is not False:
            raise RunnerError("cleanup_waiver requires environment.production=false")
        case_ids = [case.get("case_id") for case in self.package["cases"]]
        if any(not item for item in case_ids) or len(case_ids) != len(set(case_ids)):
            raise RunnerError("case_id values must be non-empty and unique")
        self._validate_traceability()
        read_only_operations_env = self.environment.get("read_only_operations_env")
        allowed_read_only = {
            item.strip() for item in os.environ.get(read_only_operations_env, "").split(",") if item.strip()
        } if read_only_operations_env else set()
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
                for field in ("cleanup_extract", "object_ref", "teardown"):
                    if not case.get(field):
                        raise RunnerError(f"case {case_id} {effective_effect} request requires {field}")
                self._validate_teardown(case_id, case.get("teardown"))
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
            if case.get("side_effect") in {"write", "destructive"} and waiver is None:
                action = self.teardown.register(
                    case_id, case.get("teardown"), case.get("object_ref"), case.get("cleanup_extract") or {}
                )
                step("register_teardown", "passed")
            exchange = self.http.execute(request)
            request_sent = True
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
        if cleanup.get("waived_cases"):
            return "inconclusive"
        if statuses and all(status == "passed" for status in statuses) and cleanup.get("status") not in {"failed", "not_run", "waived"} and (requirement_trace.get("summary") or {}).get("required_complete") is True:
            return "passed"
        return "inconclusive"


def main():
    parser = argparse.ArgumentParser(description="Execute a deterministic API TestPackage")
    parser.add_argument("package", help="Path to package.json")
    parser.add_argument("--output-dir", required=True, help="Run output directory")
    args = parser.parse_args()
    package_path = Path(args.package).resolve()
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        result_path = ApiTestRunner(package, args.output_dir).run()
        print(json.dumps({"status": "completed", "result": str(result_path)}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": redact(str(exc), secrets=collect_sensitive_env_values())}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
