"""Run browser lifecycle regressions with controlled asynchronous IO in Node."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_lifecycle_spec():
    node = os.environ.get("BIODATA_NODE") or shutil.which("node") or shutil.which("node.exe")
    if not node:
        pytest.skip("Node is unavailable")
    result = subprocess.run(
        [node, str(ROOT / "tests/js/frontend_lifecycle_spec.mjs")], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)
