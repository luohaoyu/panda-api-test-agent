#!/bin/bash
set -euo pipefail

usage() {
  echo "Usage: run_tests.sh <package.json> <run-dir> <report.html> [--promote-dir <path>]"
}

if [ "$#" -lt 3 ]; then
  usage
  exit 2
fi

PACKAGE_PATH="$1"
RUN_DIR="$2"
REPORT_PATH="$3"
shift 3
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

python "$SCRIPT_DIR/run_pipeline.py" "$PACKAGE_PATH" "$RUN_DIR" "$REPORT_PATH" "$@"
