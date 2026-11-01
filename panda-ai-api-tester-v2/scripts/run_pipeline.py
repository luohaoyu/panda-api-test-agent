#!/usr/bin/env python3
"""In-process TestPackage pipeline requiring an explicit RuntimeContext 2.0.

The host owns runtime values and passes one immutable RuntimeContext object. This
module never reads credentials from files, argv, stdin, or process environment.
"""
import argparse
from datetime import datetime, timezone
import json
import subprocess
import sys
import time
from pathlib import Path

from runner import ApiTestRunner
from runtime_context import RuntimeContext, package_execution_preflight, preflight


class PipelineBlocked(RuntimeError):
    pass


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _duration_ms(started):
    return round((time.perf_counter() - started) * 1000, 3)


def execute(command):
    completed = subprocess.run(command)
    if completed.returncode != 0:
        raise PipelineBlocked(f"pipeline command failed with exit code {completed.returncode}")


def run_pipeline(package_path, run_dir, report_path, *, runtime_context, package_override=None, request_overrides=None):
    """Execute one package using an explicit context and optional in-memory package.

    ``package_override`` is used by the trusted host to bind reviewed runtime
    request data after the on-disk package has been created. It is never written
    to disk; the package path remains the source of the non-sensitive baseline.
    """
    overall_started = time.perf_counter()
    timing = {"started_at": _now_iso(), "stages": {}}
    if not isinstance(runtime_context, RuntimeContext):
        raise PipelineBlocked("explicit RuntimeContext 2.0 is required")
    package_path = Path(package_path).resolve()
    run_dir = Path(run_dir).resolve()
    report_path = Path(report_path).resolve()
    script_dir = Path(__file__).resolve().parent

    stage_started = time.perf_counter()
    package = package_override if package_override is not None else json.loads(package_path.read_text(encoding="utf-8"))
    if not isinstance(package, dict):
        raise PipelineBlocked("package must be an object")
    timing["stages"]["load_package"] = {"status": "completed", "duration_ms": _duration_ms(stage_started)}

    stage_started = time.perf_counter()
    execution_errors = package_execution_preflight(package)
    timing["stages"]["package_execution_preflight"] = {
        "status": "blocked" if execution_errors else "completed",
        "duration_ms": _duration_ms(stage_started),
    }
    if execution_errors:
        raise PipelineBlocked("package execution preflight blocked: " + "; ".join(execution_errors))

    stage_started = time.perf_counter()
    errors = preflight(package, runtime_context)
    timing["stages"]["runtime_preflight"] = {
        "status": "blocked" if errors else "completed",
        "duration_ms": _duration_ms(stage_started),
    }
    if errors:
        raise PipelineBlocked("runtime preflight blocked: " + "; ".join(errors))

    stage_started = time.perf_counter()
    result_path = ApiTestRunner(package, run_dir, runtime_context=runtime_context).run()
    timing["stages"]["runner"] = {"status": "completed", "duration_ms": _duration_ms(stage_started)}

    stage_started = time.perf_counter()
    execute([sys.executable, str(script_dir / "validate_result.py"), str(result_path)])
    timing["stages"]["validation"] = {"status": "completed", "duration_ms": _duration_ms(stage_started)}

    stage_started = time.perf_counter()
    execute([sys.executable, str(script_dir / "render_report.py"), str(result_path), "--output", str(report_path)])
    timing["stages"]["report"] = {"status": "completed", "duration_ms": _duration_ms(stage_started)}
    timing["finished_at"] = _now_iso()
    timing["duration_ms"] = _duration_ms(overall_started)
    return {"result": str(result_path), "report": str(report_path), "timing": timing}


def main():
    parser = argparse.ArgumentParser(description="Pipeline is an in-process API requiring RuntimeContext 2.0")
    parser.add_argument("package", nargs="?")
    parser.add_argument("run_dir", nargs="?")
    parser.add_argument("report", nargs="?")
    parser.parse_args()
    print(
        json.dumps(
            {
                "status": "blocked",
                "error": "run_pipeline.py requires an in-memory RuntimeContext; call run_pipeline(..., runtime_context=context) from the trusted host",
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
