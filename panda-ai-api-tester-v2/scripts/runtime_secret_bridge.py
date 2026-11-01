#!/usr/bin/env python3
"""Consume one host-private local runtime envelope for an approved TestPackage.

The envelope is a narrowly scoped, single-use exception to the default rule that
runtime credentials do not enter files. It must live at the fixed project-local
path ``.panda-api-test-agent/local/runtime/secrets/{run_id}.secret.json``. The
consumer validates its run, environment, package fingerprint, lifetime and
binding names, deletes it, and only then returns values for RuntimeContext.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import uuid

from plan_fingerprint import calculate_plan_fingerprint
from runtime_context import package_binding_keys, package_secret_keys


class LocalSecretBridgeError(ValueError):
    """Raised when a local runtime envelope cannot be safely consumed."""


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SCHEMA_VERSION = "1.0"
MAX_ENVELOPE_BYTES = 64 * 1024
MAX_TTL = timedelta(minutes=10)
MAX_CLOCK_SKEW = timedelta(minutes=1)
REQUIRED_FIELDS = {
    "schema_version",
    "run_id",
    "environment_id",
    "package_fingerprint",
    "created_at",
    "expires_at",
    "single_use",
    "values",
}


def _parse_time(value, field):
    if not isinstance(value, str) or not value.strip():
        raise LocalSecretBridgeError(f"local runtime envelope {field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LocalSecretBridgeError(f"local runtime envelope {field} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise LocalSecretBridgeError(f"local runtime envelope {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_run_id(run_id):
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise LocalSecretBridgeError("run_id must contain only ASCII letters, digits, '-' or '_'")
    return run_id


def project_root_for_package(package_path):
    """Return the project root owning the package's .panda-api-test-agent tree."""
    package_file = Path(package_path).resolve(strict=False)
    for parent in package_file.parents:
        if parent.name == ".panda-api-test-agent":
            return parent.parent
    raise LocalSecretBridgeError("package must be located under .panda-api-test-agent")


def secret_envelope_path(package_path, run_id):
    """Calculate the only allowed local runtime envelope path for a run."""
    safe_run_id = _validate_run_id(run_id)
    root = project_root_for_package(package_path)
    secret_root = (root / ".panda-api-test-agent" / "local" / "runtime" / "secrets").resolve(strict=False)
    candidate = (secret_root / f"{safe_run_id}.secret.json").resolve(strict=False)
    try:
        candidate.relative_to(secret_root)
    except ValueError as exc:
        raise LocalSecretBridgeError("local runtime envelope path escapes the fixed secret directory") from exc
    return candidate


def _validate_envelope(payload, *, run_id, package, now):
    if not isinstance(payload, dict):
        raise LocalSecretBridgeError("local runtime envelope must be a JSON object")
    unknown = sorted(set(payload) - REQUIRED_FIELDS)
    missing = sorted(REQUIRED_FIELDS - set(payload))
    if unknown:
        raise LocalSecretBridgeError("local runtime envelope has unsupported fields: " + ", ".join(unknown))
    if missing:
        raise LocalSecretBridgeError("local runtime envelope is missing fields: " + ", ".join(missing))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise LocalSecretBridgeError(f"local runtime envelope schema_version must be {SCHEMA_VERSION}")
    if payload.get("run_id") != run_id:
        raise LocalSecretBridgeError("local runtime envelope run_id does not match the requested run")
    environment_id = (package.get("environment") or {}).get("environment_id")
    if payload.get("environment_id") != environment_id:
        raise LocalSecretBridgeError("local runtime envelope environment_id does not match the package")
    actual_fingerprint = calculate_plan_fingerprint(package)
    if payload.get("package_fingerprint") != actual_fingerprint:
        raise LocalSecretBridgeError("local runtime envelope package_fingerprint does not match the package")
    if payload.get("single_use") is not True:
        raise LocalSecretBridgeError("local runtime envelope must declare single_use=true")

    created_at = _parse_time(payload.get("created_at"), "created_at")
    expires_at = _parse_time(payload.get("expires_at"), "expires_at")
    if expires_at <= created_at:
        raise LocalSecretBridgeError("local runtime envelope expires_at must be after created_at")
    if expires_at - created_at > MAX_TTL:
        raise LocalSecretBridgeError("local runtime envelope lifetime must not exceed 10 minutes")
    if created_at > now + MAX_CLOCK_SKEW:
        raise LocalSecretBridgeError("local runtime envelope created_at is in the future")
    if now >= expires_at:
        raise LocalSecretBridgeError("local runtime envelope has expired")

    values = payload.get("values")
    if not isinstance(values, dict) or not values:
        raise LocalSecretBridgeError("local runtime envelope values must be a non-empty object")
    if any(not isinstance(key, str) or not key for key in values):
        raise LocalSecretBridgeError("local runtime envelope binding names must be non-empty strings")
    declared = package_binding_keys(package)
    unknown_bindings = sorted(set(values) - declared)
    if unknown_bindings:
        raise LocalSecretBridgeError("local runtime envelope contains undeclared bindings: " + ", ".join(unknown_bindings))
    secret_keys = package_secret_keys(package)
    non_secret_bindings = sorted(set(values) - secret_keys)
    if non_secret_bindings:
        raise LocalSecretBridgeError("local runtime envelope may contain only secret bindings: " + ", ".join(non_secret_bindings))
    missing_secrets = sorted(secret_keys - set(values))
    if missing_secrets:
        raise LocalSecretBridgeError("local runtime envelope is missing declared secret bindings: " + ", ".join(missing_secrets))
    return dict(values)


def consume_local_runtime_envelope(package_path, package, run_id, *, now=None):
    """Consume, delete and validate one fixed-path local runtime envelope.

    The file is atomically claimed before reading so concurrent consumers cannot
    use the same envelope. It is deleted after every read/validation attempt.
    Values are returned only when validation and deletion both succeed.
    """
    safe_run_id = _validate_run_id(run_id)
    secret_path = secret_envelope_path(package_path, safe_run_id)
    if not secret_path.exists():
        raise LocalSecretBridgeError("local runtime envelope does not exist for the requested run")
    if secret_path.is_symlink() or not secret_path.is_file():
        raise LocalSecretBridgeError("local runtime envelope must be a regular non-symlink file")
    try:
        size = secret_path.stat().st_size
    except OSError as exc:
        raise LocalSecretBridgeError("local runtime envelope metadata cannot be read") from exc
    if size <= 0 or size > MAX_ENVELOPE_BYTES:
        raise LocalSecretBridgeError("local runtime envelope size is invalid")

    claimed_path = secret_path.with_name(f".{safe_run_id}.{uuid.uuid4().hex}.consuming.json")
    try:
        os.replace(secret_path, claimed_path)
    except OSError as exc:
        raise LocalSecretBridgeError("local runtime envelope could not be claimed for single-use consumption") from exc

    validation_error = None
    values = None
    try:
        try:
            raw = claimed_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            reference_time = now or datetime.now(timezone.utc)
            if reference_time.tzinfo is None:
                reference_time = reference_time.replace(tzinfo=timezone.utc)
            values = _validate_envelope(
                payload,
                run_id=safe_run_id,
                package=package,
                now=reference_time.astimezone(timezone.utc),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, LocalSecretBridgeError) as exc:
            validation_error = exc
    finally:
        try:
            claimed_path.unlink()
        except OSError as cleanup_error:
            raise LocalSecretBridgeError("local runtime envelope cleanup failed; execution is blocked") from cleanup_error

    if validation_error is not None:
        if isinstance(validation_error, LocalSecretBridgeError):
            raise validation_error
        raise LocalSecretBridgeError("local runtime envelope could not be parsed") from validation_error
    return values


def write_local_runtime_envelope(
    package_path,
    package,
    run_id,
    secret_values,
    *,
    created_at=None,
    ttl_seconds=300,
):
    """Atomically create one fixed-path, short-lived local runtime envelope.

    This helper is intended for a trusted Agent Host. It accepts only bindings
    declared as secret by the package and never writes non-secret runtime values.
    """
    safe_run_id = _validate_run_id(run_id)
    if not isinstance(ttl_seconds, int) or ttl_seconds <= 0 or ttl_seconds > int(MAX_TTL.total_seconds()):
        raise LocalSecretBridgeError("ttl_seconds must be between 1 and 600")
    if not isinstance(secret_values, dict) or not secret_values:
        raise LocalSecretBridgeError("secret_values must be a non-empty object")
    declared_secrets = package_secret_keys(package)
    unknown = sorted(set(secret_values) - declared_secrets)
    missing = sorted(declared_secrets - set(secret_values))
    if unknown:
        raise LocalSecretBridgeError("secret_values contains non-secret or undeclared bindings: " + ", ".join(unknown))
    if missing:
        raise LocalSecretBridgeError("secret_values is missing declared secret bindings: " + ", ".join(missing))

    timestamp = created_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    target = secret_envelope_path(package_path, safe_run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise LocalSecretBridgeError("local runtime envelope already exists for the requested run")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": safe_run_id,
        "environment_id": (package.get("environment") or {}).get("environment_id"),
        "package_fingerprint": calculate_plan_fingerprint(package),
        "created_at": timestamp.isoformat(),
        "expires_at": (timestamp + timedelta(seconds=ttl_seconds)).isoformat(),
        "single_use": True,
        "values": dict(secret_values),
    }
    temporary = target.with_name(f".{safe_run_id}.{uuid.uuid4().hex}.writing.json")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return target
