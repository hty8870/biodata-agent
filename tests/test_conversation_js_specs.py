"""CI executes the actual browser grouping and durable client specifications."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("script,marker", [
    ("workspaces_spec.mjs", "WORKSPACES_SPEC_OK"),
    ("conversation_client_spec.mjs", "CONVERSATION_CLIENT_SPEC_OK"),
])
def test_conversation_browser_specification(script, marker):
    node = shutil.which("node")
    assert node is not None, "The browser specifications require the configured Node runtime"
    result = subprocess.run([node, str(ROOT / "tests" / "js" / script)], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker in result.stdout
