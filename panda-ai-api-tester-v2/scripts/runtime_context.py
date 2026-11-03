#!/usr/bin/env python3
"""RuntimeContext 2.0 and TestPackage runtime binding validation.

TestPackages declare binding metadata under environment.runtime_bindings. A host
creates one immutable RuntimeContext from in-memory values and passes it to the
in-process pipeline. Runtime values are never loaded from os.environ here.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping
import re
import urllib.parse

from feasibility_plan import validate_package_feasibility


class RuntimeContextError(ValueError):
    pass


LEGACY_ENVIRONMENT_FIELDS = {
    "base_url_env",
    "base_url_key",
    "auth_env",
    "allowed_origins_env",
    "allowed_origins_key",
    "read_only_operations_env",
    "read_only_operations_key",
    "auth",
}
LEGACY_AUTH_FIELDS = {
    "credential_env",
    "credential_envs",
    "username_env",
    "password_env",
    "required_env",
}
SUPPORTED_FORMATS = {"string", "url", "origin_list", "operation_list"}
SUPPORTED_AUTH_TYPES = {"none", "anonymous", "bearer", "token", "header", "cookie", "basic", "headers"}
BINDING_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
AUTH_PROBE_STRATEGIES = {"declared", "none", "blocked"}
AUTH_PROBE_FIELDS = {"strategy", "gate_id", "side_effect_free", "blocked"}
SAFE_PROBE_METHODS = {"GET", "HEAD", "OPTIONS"}
UNCONFIRMED_VALUES = {"", "pending", "unknown", "tbd", "待确认"}


@dataclass(frozen=True)
class RuntimeContext:
    schema_version: str
    context_id: str
    environment_id: str
    values: Mapping[str, object]
    secret_keys: frozenset[str]
    source: str = "host"

    def __post_init__(self):
        if self.schema_version != "2.0":
            raise RuntimeContextError("runtime context schema_version must be 2.0")
        if not self.context_id:
            raise RuntimeContextError("runtime context context_id is required")
        if not self.environment_id:
            raise RuntimeContextError("runtime context environment_id is required")
        if self.source != "host":
            raise RuntimeContextError("runtime context source must be host")
        values = dict(self.values or {})
        secrets = frozenset(self.secret_keys or ())
        unknown_secrets = secrets - set(values)
        if unknown_secrets:
            raise RuntimeContextError("secret_keys must reference provided runtime values")
        object.__setattr__(self, "values", MappingProxyType(values))
        object.__setattr__(self, "secret_keys", secrets)

    @classmethod
    def from_mapping(cls, environment_id, values, secret_keys=(), context_id="ctx-runtime"):
        return cls("2.0", context_id, environment_id, dict(values or {}), frozenset(secret_keys or ()), "host")

    def has(self, key):
        return key in self.values and self.values[key] not in (None, "")

    def require(self, key):
        if key not in self.values or self.values[key] in (None, ""):
            raise RuntimeContextError(f"required runtime binding is missing: {key}")
        return self.values[key]

    def optional(self, key, default=""):
        if not key:
            return default
        value = self.values.get(key, default)
        return default if value in (None, "") else value

    def redaction_values(self):
        return [str(self.values[key]) for key in self.secret_keys if self.values.get(key) not in (None, "")]

    def metadata(self):
        return {
            "schema_version": self.schema_version,
            "context_id": self.context_id,
            "environment_id": self.environment_id,
            "provided_bindings": sorted(self.values),
            "secret_bindings": sorted(self.secret_keys),
            "values_persisted": False,
            "source": self.source,
        }


def _environment(package):
    environment = package.get("environment")
    if not isinstance(environment, dict):
        raise RuntimeContextError("environment must be an object")
    return environment


def runtime_bindings(package):
    environment = _environment(package)
    legacy = sorted(LEGACY_ENVIRONMENT_FIELDS & set(environment))
    if legacy:
        raise RuntimeContextError("legacy runtime binding fields are forbidden: " + ", ".join(legacy))
    if "authentication" in package:
        raise RuntimeContextError("legacy top-level authentication is forbidden")
    bindings = environment.get("runtime_bindings")
    if not isinstance(bindings, dict):
        raise RuntimeContextError("environment.runtime_bindings must be an object")
    return bindings


def _validate_binding_spec(path, spec, *, default_required=False, default_secret=False, default_format="string"):
    if not isinstance(spec, dict):
        raise RuntimeContextError(f"{path} must be a binding object")
    allowed = {"key", "required", "secret", "format"}
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise RuntimeContextError(f"{path} has unsupported fields: {', '.join(unknown)}")
    key = spec.get("key")
    if not isinstance(key, str) or not BINDING_KEY_PATTERN.fullmatch(key):
        raise RuntimeContextError(f"{path}.key must be a valid runtime binding key")
    required = spec.get("required", default_required)
    secret = spec.get("secret", default_secret)
    value_format = spec.get("format", default_format)
    if not isinstance(required, bool):
        raise RuntimeContextError(f"{path}.required must be boolean")
    if not isinstance(secret, bool):
        raise RuntimeContextError(f"{path}.secret must be boolean")
    if value_format not in SUPPORTED_FORMATS:
        raise RuntimeContextError(f"{path}.format is unsupported: {value_format}")
    return {
        "path": path,
        "key": key,
        "required": required,
        "secret": secret,
        "format": value_format,
    }


def binding_specs(package):
    bindings = runtime_bindings(package)
    specs = []
    base_url = bindings.get("base_url")
    if base_url is None:
        raise RuntimeContextError("environment.runtime_bindings.base_url is required")
    base_spec = _validate_binding_spec(
        "environment.runtime_bindings.base_url",
        base_url,
        default_required=True,
        default_format="url",
    )
    if not base_spec["required"] or base_spec["secret"] or base_spec["format"] != "url":
        raise RuntimeContextError("base_url binding must use required=true, secret=false, format=url")
    specs.append(base_spec)

    optional_bindings = {
        "allowed_origins": "origin_list",
        "read_only_operations": "operation_list",
    }
    for name, expected_format in optional_bindings.items():
        if name not in bindings:
            continue
        spec = _validate_binding_spec(
            f"environment.runtime_bindings.{name}",
            bindings[name],
            default_required=False,
            default_format=expected_format,
        )
        if spec["secret"] or spec["format"] != expected_format:
            raise RuntimeContextError(f"{name} binding must use secret=false, format={expected_format}")
        specs.append(spec)

    auth = bindings.get("auth", {"type": "none"})
    if not isinstance(auth, dict):
        raise RuntimeContextError("environment.runtime_bindings.auth must be an object")
    legacy_auth = sorted(LEGACY_AUTH_FIELDS & set(auth))
    if legacy_auth:
        raise RuntimeContextError("legacy authentication binding fields are forbidden: " + ", ".join(legacy_auth))
    auth_type = str(auth.get("type", "none")).strip().lower()
    if auth_type not in SUPPORTED_AUTH_TYPES:
        raise RuntimeContextError(f"unsupported runtime auth type: {auth_type}")
    common_allowed = {"type", "enabled", "header", "trim", "value_template"}
    if auth_type == "basic":
        allowed = common_allowed | {"username", "password", "credential"}
        unknown = sorted(set(auth) - allowed)
        if unknown:
            raise RuntimeContextError("runtime auth has unsupported fields: " + ", ".join(unknown))
        has_pair = "username" in auth or "password" in auth
        has_credential = "credential" in auth
        if has_pair and has_credential:
            raise RuntimeContextError("basic auth must use either username/password or credential, not both")
        if has_pair:
            if "username" not in auth or "password" not in auth:
                raise RuntimeContextError("basic auth username and password bindings must be provided together")
            specs.append(_validate_binding_spec("environment.runtime_bindings.auth.username", auth["username"], default_required=True))
            specs.append(_validate_binding_spec("environment.runtime_bindings.auth.password", auth["password"], default_required=True, default_secret=True))
        elif has_credential:
            specs.append(_validate_binding_spec("environment.runtime_bindings.auth.credential", auth["credential"], default_required=True, default_secret=True))
        else:
            raise RuntimeContextError("basic auth requires username/password or a credential binding")
    elif auth_type == "headers":
        allowed = common_allowed | {"headers"}
        unknown = sorted(set(auth) - allowed)
        if unknown:
            raise RuntimeContextError("runtime auth has unsupported fields: " + ", ".join(unknown))
        headers = auth.get("headers")
        if not isinstance(headers, dict) or not headers:
            raise RuntimeContextError("headers auth requires a non-empty headers object")
        for header_name, header_spec in headers.items():
            if not isinstance(header_name, str) or not header_name.strip():
                raise RuntimeContextError("auth header names must be non-empty strings")
            specs.append(_validate_binding_spec(f"environment.runtime_bindings.auth.headers.{header_name}", header_spec, default_required=True, default_secret=True))
    elif auth_type not in {"none", "anonymous"}:
        allowed = common_allowed | {"credential"}
        unknown = sorted(set(auth) - allowed)
        if unknown:
            raise RuntimeContextError("runtime auth has unsupported fields: " + ", ".join(unknown))
        if "credential" not in auth:
            raise RuntimeContextError(f"{auth_type} auth requires a credential binding")
        specs.append(_validate_binding_spec("environment.runtime_bindings.auth.credential", auth["credential"], default_required=True, default_secret=True))
    else:
        unknown = sorted(set(auth) - (common_allowed - {"header", "trim", "value_template"}))
        if unknown:
            raise RuntimeContextError("anonymous auth has unsupported fields: " + ", ".join(unknown))

    seen = {}
    for spec in specs:
        previous = seen.get(spec["key"])
        if previous and previous != (spec["secret"], spec["format"]):
            raise RuntimeContextError(f"runtime binding {spec['key']} has conflicting declarations")
        seen[spec["key"]] = (spec["secret"], spec["format"])
    return specs


def binding_spec(package, name):
    bindings = runtime_bindings(package)
    if name == "base_url":
        raw = bindings.get(name)
        return _validate_binding_spec(f"environment.runtime_bindings.{name}", raw, default_required=True, default_format="url")
    expected = {"allowed_origins": "origin_list", "read_only_operations": "operation_list"}.get(name)
    if not expected or name not in bindings:
        return None
    return _validate_binding_spec(f"environment.runtime_bindings.{name}", bindings[name], default_format=expected)


def auth_config(package):
    bindings = runtime_bindings(package)
    config = bindings.get("auth", {"type": "none"})
    if not isinstance(config, dict):
        raise RuntimeContextError("environment.runtime_bindings.auth must be an object")
    return config


def package_binding_keys(package):
    return {spec["key"] for spec in binding_specs(package)}


def package_secret_keys(package):
    return {spec["key"] for spec in binding_specs(package) if spec["secret"]}


def credential_binding_keys(package):
    """Runtime binding keys that carry a credential for the declared auth type."""
    config = auth_config(package)
    auth_type = str(config.get("type", "none")).strip().lower()
    if auth_type == "basic":
        names = set()
        for field in ("username", "password", "credential"):
            spec = config.get(field)
            if isinstance(spec, dict) and spec.get("key"):
                names.add(spec["key"])
        return names
    if auth_type == "headers":
        return {
            spec["key"]
            for spec in (config.get("headers") or {}).values()
            if isinstance(spec, dict) and spec.get("key")
        }
    credential = config.get("credential")
    if isinstance(credential, dict) and credential.get("key"):
        return {credential["key"]}
    return set()


def auth_probe_config(package):
    """Return the declared auth probe object, or None when it is not declared."""
    probe = package.get("auth_probe")
    return probe if isinstance(probe, dict) else None


def auth_probe_strategy(package):
    probe = auth_probe_config(package)
    if probe is None:
        return None
    return str(probe.get("strategy", "")).strip().lower()


def auth_probe_gate(package):
    """Return the gate object declared as the executable auth probe, if any."""
    if auth_probe_strategy(package) != "declared":
        return None
    gate_id = (auth_probe_config(package) or {}).get("gate_id")
    for gate in package.get("gates") or []:
        if isinstance(gate, dict) and gate.get("gate_id") == gate_id:
            return gate
    return None


def auth_probe_operation(gate):
    """Return the "<METHOD> /path" identity of a declared auth probe gate."""
    request = (gate or {}).get("request") or {}
    method = str(request.get("method", "GET")).upper()
    return f"{method} {request.get('path', '')}"


def _auth_probe_preflight(package):
    """Validate the declared authentication probe protocol before any HTTP."""
    errors = []
    probe = package.get("auth_probe")
    needs_credential = bool(credential_binding_keys(package))
    if probe is None:
        # Authentication can be verified by the approved business request itself.
        # A dedicated read-only probe is optional; requiring one here would turn
        # an unavailable convenience endpoint into a false execution blocker.
        return errors
    if not isinstance(probe, dict):
        return ["auth_probe must be an object"]
    unknown = sorted(set(probe) - AUTH_PROBE_FIELDS)
    if unknown:
        errors.append("auth_probe has unsupported fields: " + ", ".join(unknown))
    strategy = str(probe.get("strategy", "")).strip().lower()
    if strategy not in AUTH_PROBE_STRATEGIES:
        errors.append(f"auth_probe.strategy is unsupported: {probe.get('strategy')}")
        return errors

    control = package.get("execution_control") or {}
    http_blocked = isinstance(control, dict) and control.get("formal_http_allowed") is False

    if strategy == "declared":
        if not needs_credential:
            errors.append("auth_probe strategy=declared requires credential-bearing auth")
        gate_id = probe.get("gate_id")
        if not isinstance(gate_id, str) or not gate_id.strip():
            errors.append("auth_probe strategy=declared requires gate_id")
            gate_id = None
        gate = auth_probe_gate(package)
        if gate_id and gate is None:
            errors.append(f"auth_probe.gate_id does not reference a declared gate: {gate_id}")
        if gate is not None:
            request = gate.get("request") or {}
            method = str(request.get("method", "GET")).upper()
            path = request.get("path", "")
            if method not in SAFE_PROBE_METHODS:
                errors.append(f"auth probe gate {gate_id} must use GET, HEAD or OPTIONS")
            if not isinstance(path, str) or not path.startswith("/"):
                errors.append(f"auth probe gate {gate_id} requires a static absolute path")
            if not gate.get("assertions"):
                errors.append(f"auth probe gate {gate_id} requires assertions")
        evidence = probe.get("side_effect_free")
        if not isinstance(evidence, dict):
            errors.append("auth_probe strategy=declared requires side_effect_free evidence")
        else:
            unknown_fields = sorted(set(evidence) - {"confirmed_by", "confirmed_at", "reference", "rationale"})
            if unknown_fields:
                errors.append("auth_probe.side_effect_free has unsupported fields: " + ", ".join(unknown_fields))
            for field in ("confirmed_by", "reference", "rationale"):
                value = evidence.get(field)
                if not isinstance(value, str) or value.strip().lower() in UNCONFIRMED_VALUES:
                    errors.append(f"auth_probe.side_effect_free.{field} must be confirmed")
        if http_blocked:
            errors.append("execution_control blocks formal HTTP but auth_probe strategy=declared is executable")
    elif strategy == "none":
        if needs_credential:
            errors.append("auth_probe strategy=none is invalid for credential-bearing auth")
    else:
        # Explicitly declared blocked probes are still honored, but absence of a
        # probe declaration is not a blocker. The business case can carry the
        # authentication check and report 401/403 as an observed result.
        blocked = probe.get("blocked")
        if not isinstance(blocked, dict):
            errors.append("auth_probe strategy=blocked requires a blocked object")
            return errors
        unknown_fields = sorted(set(blocked) - {"gap_id", "reason", "required_from"})
        if unknown_fields:
            errors.append("auth_probe.blocked has unsupported fields: " + ", ".join(unknown_fields))
        gap_id = blocked.get("gap_id")
        reason = blocked.get("reason")
        if not isinstance(gap_id, str) or not gap_id.strip():
            errors.append("auth_probe.blocked.gap_id is required")
        else:
            known = {
                item.get("gap_id"): item
                for item in package.get("known_gaps") or []
                if isinstance(item, dict) and item.get("gap_id")
            }
            gap = known.get(gap_id)
            if gap is None:
                errors.append(f"auth_probe.blocked.gap_id is unknown: {gap_id}")
            elif gap.get("blocking") is not True:
                errors.append(f"auth_probe.blocked.gap_id must reference a blocking gap: {gap_id}")
        if not isinstance(reason, str) or not reason.strip():
            errors.append("auth_probe.blocked.reason is required")
        if not errors:
            errors.append(f"auth probe path not confirmed: {reason.strip()}; blocking gap: {gap_id}")
    return errors


def _auth_probe_runtime_errors(package, context):
    """A declared probe must be explicitly allowlisted as a read-only operation."""
    if auth_probe_strategy(package) != "declared":
        return []
    spec = binding_spec(package, "read_only_operations")
    if not spec or not spec["required"]:
        return []
    operation = auth_probe_operation(auth_probe_gate(package))
    declared = {
        item.strip() for item in str(context.optional(spec["key"], "")).split(",") if item.strip()
    }
    if operation not in declared:
        return [f"auth probe operation must be declared in read_only_operations: {operation}"]
    return []



def _validate_value_format(spec, value):
    text = str(value).strip()
    if spec["format"] == "string":
        return None if text else f"runtime binding {spec['key']} must not be empty"
    if spec["format"] == "url":
        try:
            parsed = urllib.parse.urlsplit(text)
        except ValueError:
            return f"runtime binding {spec['key']} must be a valid URL"
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return f"runtime binding {spec['key']} must be an origin-safe http(s) URL"
        return None
    if spec["format"] == "origin_list":
        origins = [item.strip().rstrip("/") for item in text.split(",") if item.strip()]
        if not origins:
            return f"runtime binding {spec['key']} must contain at least one origin"
        for origin in origins:
            parsed = urllib.parse.urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
                return f"runtime binding {spec['key']} contains an invalid origin"
        return None
    if spec["format"] == "operation_list":
        operations = [item.strip() for item in text.split(",") if item.strip()]
        for operation in operations:
            parts = operation.split(" ", 1)
            if len(parts) != 2 or parts[0].upper() not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"} or not parts[1].startswith("/"):
                return f"runtime binding {spec['key']} contains an invalid operation"
        return None
    return f"runtime binding {spec['key']} has unsupported format"


def build_runtime_context(package, values, context_id="ctx-run"):
    """Build an explicit host RuntimeContext; implicit environment loading is forbidden."""
    if values is None:
        raise RuntimeContextError("explicit runtime values are required; environment bootstrap is forbidden")
    environment = _environment(package)
    environment_id = environment.get("environment_id")
    if not environment_id:
        raise RuntimeContextError("environment.environment_id is required before runtime context creation")
    declared = package_binding_keys(package)
    supplied = set((values or {}).keys())
    extra = supplied - declared
    if extra:
        raise RuntimeContextError("undeclared runtime bindings are forbidden: " + ", ".join(sorted(extra)))
    secrets = package_secret_keys(package) & supplied
    context = RuntimeContext.from_mapping(environment_id, values, secrets, context_id=context_id)
    errors = preflight(package, context)
    if errors:
        raise RuntimeContextError("runtime preflight blocked: " + "; ".join(errors))
    return context


def package_execution_preflight(package):
    """Validate feasibility and HTTP authorization before RuntimeContext creation."""
    errors = validate_package_feasibility(package)
    errors.extend(_auth_probe_preflight(package))
    control = package.get("execution_control")
    if control is None:
        return errors
    if not isinstance(control, dict):
        return errors + ["execution_control must be an object"]
    allowed = control.get("formal_http_allowed")
    fail_closed = control.get("fail_closed_before_http")
    control_errors = []
    if not isinstance(allowed, bool):
        control_errors.append("execution_control.formal_http_allowed must be boolean")
    if not isinstance(fail_closed, bool):
        control_errors.append("execution_control.fail_closed_before_http must be boolean")
    blocking_ids = control.get("blocking_gap_ids") or []
    if not isinstance(blocking_ids, list) or any(not isinstance(item, str) or not item for item in blocking_ids):
        control_errors.append("execution_control.blocking_gap_ids must be a string list")
        blocking_ids = []
    known = {
        item.get("gap_id"): item
        for item in package.get("known_gaps") or []
        if isinstance(item, dict) and item.get("gap_id")
    }
    unknown = sorted(set(blocking_ids) - set(known))
    if unknown:
        control_errors.append("execution_control references unknown blocking gaps: " + ", ".join(unknown))
    nonblocking = sorted(gap_id for gap_id in blocking_ids if gap_id in known and known[gap_id].get("blocking") is not True)
    if nonblocking:
        control_errors.append("execution_control references non-blocking gaps: " + ", ".join(nonblocking))
    if allowed is False:
        if fail_closed is not True:
            control_errors.append("formal_http_allowed=false requires fail_closed_before_http=true")
        if not blocking_ids:
            control_errors.append("formal_http_allowed=false requires blocking_gap_ids")
        reason = control.get("reason")
        stop_stage = control.get("stop_stage")
        if not isinstance(reason, str) or not reason.strip():
            control_errors.append("formal_http_allowed=false requires a reason")
        if not isinstance(stop_stage, str) or not stop_stage.strip():
            control_errors.append("formal_http_allowed=false requires stop_stage")
        if not control_errors:
            control_errors.append(
                f"package execution blocked at {stop_stage}: {reason}; blocking gaps: {', '.join(blocking_ids)}"
            )
    elif allowed is True and blocking_ids:
        control_errors.append("formal_http_allowed=true cannot declare blocking_gap_ids")
    return errors + control_errors


def preflight(package, context):
    """Validate schema, context isolation, required values, secrets and formats."""
    errors = []
    try:
        environment = _environment(package)
        specs = binding_specs(package)
    except RuntimeContextError as exc:
        return [str(exc)]
    if not isinstance(context, RuntimeContext):
        return ["explicit RuntimeContext 2.0 is required"]
    if context.environment_id != environment.get("environment_id"):
        errors.append("runtime context environment_id does not match package environment")
    declared = {spec["key"] for spec in specs}
    extra = set(context.values) - declared
    if extra:
        errors.append("undeclared runtime bindings are forbidden: " + ", ".join(sorted(extra)))
    expected_secrets = {spec["key"] for spec in specs if spec["secret"] and context.has(spec["key"])}
    if context.secret_keys != expected_secrets:
        errors.append("runtime context secret classification does not match package declarations")
    for spec in specs:
        if spec["required"] and not context.has(spec["key"]):
            errors.append(f"required runtime binding is missing: {spec['key']}")
            continue
        if context.has(spec["key"]):
            error = _validate_value_format(spec, context.require(spec["key"]))
            if error:
                errors.append(error)
    errors.extend(_auth_probe_runtime_errors(package, context))
    return errors
