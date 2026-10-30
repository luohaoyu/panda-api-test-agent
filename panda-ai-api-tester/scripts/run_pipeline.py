#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
from pathlib import Path


def execute(command):
    completed = subprocess.run(command)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main():
    parser = argparse.ArgumentParser(description="Run API TestPackage, validate result and render report")
    parser.add_argument("package")
    parser.add_argument("run_dir")
    parser.add_argument("report")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    package = Path(args.package).resolve()
    run_dir = Path(args.run_dir).resolve()
    report = Path(args.report).resolve()
    result = run_dir / "result.json"

    execute([sys.executable, str(script_dir / "runner.py"), str(package), "--output-dir", str(run_dir)])
    execute([sys.executable, str(script_dir / "validate_result.py"), str(result), "--normalize"])
    execute([sys.executable, str(script_dir / "render_report.py"), str(result), "--output", str(report)])
    print(json.dumps({"result": str(result), "report": str(report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
