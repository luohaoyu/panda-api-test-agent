import json
from pathlib import Path

import pytest

from api_test_runner import execute_case


PACKAGE_PATH = Path("{{ package_path }}")
PACKAGE = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
CASES = PACKAGE["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda item: item["case_id"])
def test_api_package_case(case, runtime_context):
    result = execute_case(case=case, context=runtime_context)
    assert result["status"] == "passed", result
