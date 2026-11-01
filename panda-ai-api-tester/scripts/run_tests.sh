#!/bin/bash
set -euo pipefail

echo '{"status":"blocked","error":"run_tests.sh cannot safely inject RuntimeContext 2.0; call the trusted host Python API run_pipeline(..., runtime_context=context)"}' >&2
exit 2
