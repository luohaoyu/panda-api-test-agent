#!/usr/bin/env python3
"""Environment-scoped persistent Secret Store for trusted host execution."""
import json
import os
from pathlib import Path
import re
import uuid

from runtime_context import package_secret_keys
from runtime_secret_bridge import project_root_for_package


class EnvironmentSecretStoreError(ValueError):
    """Raised when the persistent environment Secret Store is unsafe or invalid."""


ENVIRONMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SCHEMA_VERSION = "1.0"
MAX_STORE_BYTES = 256 * 1024
REQUIRED_FIELDS = {"schema_version", "environment_id", "values"}


def _validate_environment_id(environment_id):
    if not isinstance(environment_id, str) or not ENVIRONMENT_ID_PATTERN.fullmatch(environment_id):
        raise EnvironmentSecretStoreError(
            "environment_id must contain only ASCII letters, digits, '-' or '_'"
        )
    return environment_id


def _validated_project_root(package_path, package):
    root = project_root_for_package(package_path)
    if not root.is_dir() or root.is_symlink():
        raise EnvironmentSecretStoreError("project root must be an existing non-symlink directory")
    environment_id = (package.get("environment") or {}).get("environment_id")
    _validate_environment_id(environment_id)
    return root, environment_id


def environment_secret_path(package_path, environment_id):
    """Return the only allowed persistent Secret Store path for an environment."""
    safe_environment_id = _validate_environment_id(environment_id)
    root = project_root_for_package(package_path)
    secret_root = (
        root / ".panda-api-test-agent" / "local" / "secrets" / "environments"
    ).resolve(strict=False)
    candidate = (secret_root / f"{safe_environment_id}.secret.json").resolve(strict=False)
    try:
        secret_root.relative_to(root)
        candidate.relative_to(secret_root)
    except ValueError as exc:
        raise EnvironmentSecretStoreError(
            "environment Secret Store path escapes the project-local fixed directory"
        ) from exc
    return candidate


def _validate_values(package, values, *, require_non_empty=True):
    if not isinstance(values, dict) or (require_non_empty and not values):
        qualifier = "a non-empty" if require_non_empty else "an"
        raise EnvironmentSecretStoreError(f"environment Secret Store values must be {qualifier} object")
    if any(not isinstance(key, str) or not key for key in values):
        raise EnvironmentSecretStoreError("environment Secret Store binding names must be non-empty strings")
    declared_secrets = package_secret_keys(package)
    undeclared = sorted(set(values) - declared_secrets)
    if undeclared:
        raise EnvironmentSecretStoreError(
            "environment Secret Store contains non-secret or undeclared bindings: " + ", ".join(undeclared)
        )
    return dict(values)


def write_environment_secrets(package_path, package, secret_values):
    """Atomically replace one environment's fixed-path persistent Secret Store."""
    root, environment_id = _validated_project_root(package_path, package)
    values = _validate_values(package, secret_values)
    target = environment_secret_path(package_path, environment_id)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise EnvironmentSecretStoreError("environment Secret Store is outside the project root") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise EnvironmentSecretStoreError("environment Secret Store must be a regular non-symlink file")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "environment_id": environment_id,
        "values": values,
    }
    temporary = target.with_name(f".{environment_id}.{uuid.uuid4().hex}.writing.json")
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


def read_environment_secrets(package_path, package):
    """Read exactly the package environment's fixed store without directory discovery."""
    _root, environment_id = _validated_project_root(package_path, package)
    target = environment_secret_path(package_path, environment_id)
    if not target.exists():
        return {}
    if target.is_symlink() or not target.is_file():
        raise EnvironmentSecretStoreError("environment Secret Store must be a regular non-symlink file")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise EnvironmentSecretStoreError("environment Secret Store metadata cannot be read") from exc
    if size <= 0 or size > MAX_STORE_BYTES:
        raise EnvironmentSecretStoreError("environment Secret Store size is invalid")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EnvironmentSecretStoreError("environment Secret Store could not be parsed") from exc
    if not isinstance(payload, dict):
        raise EnvironmentSecretStoreError("environment Secret Store must be a JSON object")
    unknown = sorted(set(payload) - REQUIRED_FIELDS)
    missing = sorted(REQUIRED_FIELDS - set(payload))
    if unknown:
        raise EnvironmentSecretStoreError(
            "environment Secret Store has unsupported fields: " + ", ".join(unknown)
        )
    if missing:
        raise EnvironmentSecretStoreError(
            "environment Secret Store is missing fields: " + ", ".join(missing)
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise EnvironmentSecretStoreError(
            f"environment Secret Store schema_version must be {SCHEMA_VERSION}"
        )
    if payload.get("environment_id") != environment_id:
        raise EnvironmentSecretStoreError(
            "environment Secret Store environment_id does not match the package"
        )
    return _validate_values(package, payload.get("values"), require_non_empty=False)
