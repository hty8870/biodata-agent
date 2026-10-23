from __future__ import annotations

import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-candidate.yml"
EXPECTED_ACTIONS = {
    "actions/checkout": "de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-python": "a309ff8b426b58ec0e2a45f0f869d46889d02405",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
}


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_release_candidate_is_manual_only_and_has_no_deploy_authority() -> None:
    text = _workflow()
    assert re.search(r"(?m)^  workflow_dispatch:\s*$", text)
    assert not re.search(r"(?m)^  (push|pull_request|schedule):", text)
    assert "pull_request_target" not in text
    assert re.search(r"(?m)^permissions: \{\}\s*$", text)
    assert "cancel-in-progress: false" in text
    assert "environment:" not in text
    assert "id-token:" not in text
    assert "contents: write" not in text
    assert "secrets." not in text
    assert "gh release" not in text.lower()


def test_release_candidate_pins_every_action_and_disables_checkout_credentials() -> None:
    text = _workflow()
    uses = re.findall(r"(?m)^\s*uses: ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})", text)
    assert dict(uses) == EXPECTED_ACTIONS
    assert len(re.findall(r"(?m)^\s*uses:", text)) == len(EXPECTED_ACTIONS)
    assert "persist-credentials: false" in text


def test_release_candidate_builds_once_verifies_and_smokes_unpacked_artifact() -> None:
    text = _workflow()
    assert "--require-hashes --only-binary=:all: -r requirements/requirements-ci.lock" in text
    assert "scripts/quality_gate.py" in text and "--profile full" in text
    assert text.count("'scripts\\build_release.py', 'build'") == 1
    assert text.count("python @BuildArgs") == 1
    assert "$env:GITHUB_REF_TYPE -eq 'tag'" in text
    assert "$env:GITHUB_REF_NAME.Substring(1)" in text
    assert "@('--expected-version', $ExpectedVersion)" in text
    assert "scripts/build_release.py verify" in text
    assert "Expand-Archive" in text
    assert "$env:RUNNER_TEMP" in text
    assert "artifacts\\unpacked" not in text
    assert "Refusing to reuse extraction directory" in text
    assert "Remove-Item -LiteralPath $Unpacked -Recurse -Force" in text
    assert "scripts\\smoke_test.py" in text
    assert "scripts\\web_smoke_test.py" in text
    assert "mcp_server.py --selfcheck" in text
    assert "if-no-files-found: error" in text
    assert "retention-days: 30" in text


def test_all_workflows_use_only_reviewed_full_sha_references() -> None:
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        raw_uses = re.findall(r"(?m)^\s*uses:\s*([^\s#]+)", text)
        for reference in raw_uses:
            assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}", reference), (
                path,
                reference,
            )


@pytest.mark.parametrize("codes", [(7, 0, 0), (0, 7, 0), (0, 0, 7), (0, 0, 0)])
def test_unpacked_smoke_propagates_each_native_failure(codes) -> None:
    """执行 workflow 原 try 块；仅把三个外部 smoke 换成可控退出码子进程。"""
    shell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell unavailable")
    step = _workflow().split("- name: Smoke-test the unpacked artifact", 1)[1]
    body = re.search(r"try \{\n(.*?)\n\s*\}\n\s*finally", step, re.S).group(1)
    commands = re.findall(r"(?m)^\s*python -B .+$", body)
    assert len(commands) == len(codes)
    executable = sys.executable.replace("'", "''")
    for i, (command, code) in enumerate(zip(commands, codes)):
        body = body.replace(command, f"& '{executable}' -B -c \"import sys; print('smoke-{i}'); sys.exit({code})\"", 1)
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command",
         "$ErrorActionPreference = 'Stop'\n" + textwrap.dedent(body)],
        capture_output=True, text=True, timeout=30,
    )
    assert (result.returncode == 0) == (not any(codes)), result.stdout + result.stderr
    if any(codes):
        failed = next(i for i, code in enumerate(codes) if code)
        assert f"smoke-{failed}" in result.stdout
        assert all(f"smoke-{i}" not in result.stdout for i in range(failed + 1, 3))
