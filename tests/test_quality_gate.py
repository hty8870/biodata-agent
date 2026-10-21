from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "automation" / "quality-gates.json"
PUBLIC_MANIFEST = ROOT / "automation" / "quality-gates.public.json"
RUNNER = ROOT / "scripts" / "quality_gate.py"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _run(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=cwd or ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _probe_manifest(tmp_path: Path, code: str, timeout_seconds: int = 10) -> Path:
    manifest = _manifest()
    gate = {
        "id": "probe",
        "description": "test-only subprocess behavior probe",
        "commands": [["{python}", "-c", code]],
        "required_tools": ["python"],
        "timeout_seconds": timeout_seconds,
        "offline": True,
        "network": "forbidden",
        "secrets": "forbidden",
        "model_downloads": "forbidden",
        "fallback": {
            "missing_tool": "fail",
            "timeout": "fail",
            "command_failure": "fail",
        },
    }
    manifest["gates"] = [gate]
    for profile in manifest["profiles"].values():
        profile["gates"] = ["probe"]
    path = tmp_path / "probe-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_manifest_is_explicit_offline_and_fail_closed():
    manifest = _manifest()
    assert manifest["schema_version"] == 1
    assert manifest["default_profile"] == "fast"
    assert set(manifest["profiles"]) == {"fast", "full"}
    policy = manifest["execution_policy"]
    assert policy["offline_after_dependency_install"] is True
    assert policy["network"] == "forbidden"
    assert "not an operating-system egress sandbox" in policy["network_enforcement"]
    assert policy["secrets"] == "forbidden-and-sanitized"
    assert policy["model_downloads"] == "forbidden"
    assert "upper/lower-case proxy" in policy["environment_injection"]
    assert "fail-closed" in policy["fallback"]

    ids = [gate["id"] for gate in manifest["gates"]]
    assert len(ids) == len(set(ids))
    assert set(manifest["profiles"]["fast"]["gates"]) < set(
        manifest["profiles"]["full"]["gates"]
    )
    for gate in manifest["gates"]:
        assert gate["offline"] is True
        assert gate["network"] == "forbidden"
        assert gate["secrets"] == "forbidden"
        assert gate["model_downloads"] == "forbidden"
        assert gate["timeout_seconds"] > 0
        assert gate["fallback"] == {
            "missing_tool": "fail",
            "timeout": "fail",
            "command_failure": "fail",
        }
        assert gate["commands"]
        assert all(isinstance(command, list) and command for command in gate["commands"])

    serialized = json.dumps(manifest).lower()
    assert "--llm-check" not in serialized
    assert "fetch_embedding_model" not in serialized
    assert not re.search(r"\$\{\{\s*secrets\.", serialized)


def _installer_manifest_sources() -> list[Path]:
    """installer-contracts 门的 manifest 真源集。

    私仓（`.deliveryignore` 分类材料在）要求**私有 + 公开两源**同测——公开变体是
    公开 manifest 的唯一真源，缺了就是 fail 而不是缩圈；真正的公开投影里 default
    manifest 就是公开变体映射后的唯一一份，只测它——不因缺变体而 skip 整个门。"""
    sources = [MANIFEST]
    if (ROOT / ".deliveryignore").is_file():
        assert PUBLIC_MANIFEST.is_file(), "私仓缺 automation/quality-gates.public.json（公开 manifest 唯一真源）"
        sources.append(PUBLIC_MANIFEST)
    return sources


@pytest.mark.parametrize("manifest_path", _installer_manifest_sources(), ids=lambda p: p.name)
def test_installer_contracts_gate_fails_closed_when_no_targets_exist(manifest_path):
    """installer-contracts 的 present 为空时必须显式失败：不能让 pytest 无目标裸跑全 suite。

    描述承诺 fail-closed（"when none exist the gate fails closed"），实现也必须如此：
    空选择 → exit 2 + 明确消息；绝不把「四个声明文件一个都没有」变成静默跑全量或假 skip。
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gate = next(g for g in manifest["gates"] if g["id"] == "installer-contracts")
    assert len(gate["commands"]) == 1 and len(gate["commands"][0]) == 3
    script = gate["commands"][0][2]
    assert "sys.exit(2)" in script, "缺 fail-closed 分支"
    declared = re.search(r"declared = (\[[^\]]*\])", script).group(1)
    missing_variant = script.replace(declared, "['tests/__missing_a__.py', 'tests/__missing_b__.py']")
    proc = subprocess.run([sys.executable, "-c", missing_variant], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 2, (proc.returncode, proc.stdout[-200:], proc.stderr[-200:])
    assert "none of the declared contract files exist" in proc.stderr
    assert "NOT covered by this run" in proc.stdout      # 部分缺口的表述不许虚声明 skip


def test_installer_contracts_command_is_identical_across_manifests():
    """私有/公开两 manifest 的 installer-contracts 执行字符串必须逐字节一致（防回潮）：
    公开变体曾长期缺 fail-closed 分支（空 targets 会裸跑全 suite）——两源各写一份必漂移。"""
    if not (ROOT / ".deliveryignore").is_file():
        pytest.skip("真正的公开投影里 default manifest 即公开变体的唯一映射，无双源可比")
    private_gate = next(g for g in _manifest()["gates"] if g["id"] == "installer-contracts")
    public = json.loads(PUBLIC_MANIFEST.read_text(encoding="utf-8"))
    public_gate = next(g for g in public["gates"] if g["id"] == "installer-contracts")
    assert private_gate["commands"] == public_gate["commands"]


def test_full_profile_covers_python_web_mcp_and_frozen_evaluation():
    full = set(_manifest()["profiles"]["full"]["gates"])
    assert {
        "pytest-suite",
        "project-smoke",
        "web-smoke",
        "mcp-protocol-selfcheck",
        "frozen-recommendation-evaluation",
    } <= full


def test_private_full_validates_both_actual_public_projections():
    manifest = _manifest()
    full = manifest["profiles"]["full"]["gates"]
    if not (ROOT / ".deliveryignore").is_file():
        # 公开投影没有私有源分类材料，不能把私仓检查命令带进公开 full。
        assert "public-mirror-source" not in full
        return
    assert "public-mirror-source" in full
    gate = next(g for g in manifest["gates"] if g["id"] == "public-mirror-source")
    assert gate["commands"] == [
        ["{python}", "scripts/build_public_mirror.py", "validate", "--allow-dirty-private"],
        ["{python}", "scripts/build_public_mirror.py", "--profile", "skeleton", "validate", "--allow-dirty-private"],
    ]
    public = json.loads((ROOT / "automation/quality-gates.public.json").read_text(encoding="utf-8"))
    assert "public-mirror-source" not in public["profiles"]["full"]["gates"]


def test_list_describes_profiles_and_safety_policy():
    result = _run("--list")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fast (default)" in result.stdout
    assert "full:" in result.stdout
    assert "network forbidden" in result.stdout
    assert "not an OS egress sandbox" in result.stdout
    assert "fallback fail-closed" in result.stdout


def test_default_profile_comes_from_manifest(tmp_path: Path):
    report_path = tmp_path / "default-profile.json"
    result = _run("--dry-run", "--report-json", str(report_path))
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["profile"] == _manifest()["default_profile"] == "fast"


def test_fast_dry_run_is_repo_rooted_and_writes_machine_report(tmp_path: Path):
    report_path = tmp_path / "quality-report.json"
    result = _run(
        "--profile",
        "fast",
        "--dry-run",
        "--report-json",
        str(report_path),
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["repository_root"] == str(ROOT)
    assert report["profile"] == "fast"
    assert report["dry_run"] is True
    assert report["status"] == "planned"
    assert report["environment"]["pythonpath"] == os.pathsep.join(
        (str(ROOT), str(ROOT / "src"))
    )
    assert {gate["id"] for gate in report["gates"]} == set(
        _manifest()["profiles"]["fast"]["gates"]
    )
    assert all(gate["status"] == "planned" for gate in report["gates"])
    assert all(gate["commands"] for gate in report["gates"])


def test_invalid_explicit_tool_override_fails_closed(tmp_path: Path):
    report_path = tmp_path / "failed.json"
    env = os.environ.copy()
    env["BIODATA_NODE"] = str(tmp_path / "node-does-not-exist")
    result = _run(
        "--profile",
        "fast",
        "--dry-run",
        "--report-json",
        str(report_path),
        env=env,
    )
    assert result.returncode == 2
    assert "never fall back silently" in result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "configuration-error"
    assert "node-does-not-exist" in report["error"]


def test_secret_is_cleared_in_real_child_and_never_appears_in_report(tmp_path: Path):
    marker = "TOP_SECRET_SHOULD_NOT_LEAK_7419"
    manifest = _probe_manifest(
        tmp_path,
        "import os,sys; sys.exit(0 if os.getenv('OPENAI_API_KEY') == '' else 9)",
    )
    report_path = tmp_path / "sanitized.json"
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = marker
    result = _run(
        "--profile",
        "fast",
        "--manifest",
        str(manifest),
        "--report-json",
        str(report_path),
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr + report_path.read_text(encoding="utf-8")
    assert marker not in combined
    assert json.loads(report_path.read_text(encoding="utf-8"))["environment"][
        "secret_values_sanitized"
    ] >= 1


def test_injection_variables_are_removed_from_real_child_process(tmp_path: Path):
    code = (
        "import os,sys; expected=os.pathsep.join((sys.argv[1], os.path.join(sys.argv[1], 'src'))); "
        "ok=(os.getenv('PYTHONPATH') == expected and not os.getenv('PYTEST_ADDOPTS') "
        "and not os.getenv('PYTEST_PLUGINS') and not os.getenv('NODE_OPTIONS') "
        "and os.getenv('http_proxy') == 'http://127.0.0.1:9' "
        "and os.getenv('https_proxy') == 'http://127.0.0.1:9' "
        "and os.getenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD') == '1'); sys.exit(0 if ok else 8)"
    )
    manifest = _probe_manifest(tmp_path, code)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["gates"][0]["commands"][0].append(str(ROOT))
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    report_path = tmp_path / "injection.json"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(tmp_path / "poison"),
            "PYTEST_ADDOPTS": "--collect-only",
            "PYTEST_PLUGINS": "poison_plugin",
            "NODE_OPTIONS": "--require poison.js",
            "http_proxy": "http://LOWERCASE_PROXY_MUST_NOT_SURVIVE_7419.invalid",
            "https_proxy": "http://LOWERCASE_PROXY_MUST_NOT_SURVIVE_7419.invalid",
        }
    )
    result = _run(
        "--manifest", str(manifest), "--profile", "fast", "--report-json", str(report_path), env=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cleared = {
        name.casefold()
        for name in report["environment"]["injection_variables_cleared"]
    }
    assert cleared >= {
        "pythonpath",
        "pytest_addopts",
        "pytest_plugins",
        "node_options",
        "http_proxy",
        "https_proxy",
    }
    assert "LOWERCASE_PROXY_MUST_NOT_SURVIVE_7419" not in (
        result.stdout + result.stderr + report_path.read_text(encoding="utf-8")
    )


def test_project_dotenv_cannot_repopulate_provider_secrets(tmp_path: Path):
    project = tmp_path / "project-with-env"
    project.mkdir()
    marker = "DOTENV_SECRET_MUST_NOT_LOAD_9317"
    (project / ".env").write_text(
        f"ZAI_API_KEY={marker}\nZHIPUAI_TOKEN={marker}\n", encoding="utf-8"
    )
    code = (
        "import os,sys; from pathlib import Path; "
        "from dataset_recommender.llm.config import load_env_candidates; "
        "loaded=load_env_candidates(Path(sys.argv[1])); "
        "ok=(loaded is not None and loaded.name == 'empty-llm.env' "
        "and os.getenv('ZAI_API_KEY') == '' and os.getenv('ZHIPUAI_TOKEN') == ''); "
        "sys.exit(0 if ok else 6)"
    )
    manifest = _probe_manifest(tmp_path, code)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["gates"][0]["commands"][0].append(str(project))
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    report_path = tmp_path / "dotenv.json"
    result = _run(
        "--manifest", str(manifest), "--profile", "fast", "--report-json", str(report_path)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr + report_path.read_text(encoding="utf-8")
    assert marker not in combined


def test_nonzero_child_fails_closed_and_records_exit_code(tmp_path: Path):
    manifest = _probe_manifest(tmp_path, "import sys; print('probe failed'); sys.exit(7)")
    report_path = tmp_path / "nonzero.json"
    result = _run(
        "--manifest", str(manifest), "--profile", "fast", "--report-json", str(report_path)
    )
    assert result.returncode == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    command = report["gates"][0]["commands"][0]
    assert command["status"] == "failed"
    assert command["exit_code"] == 7
    assert "probe failed" in command["stdout_tail"]


def test_timed_out_child_fails_closed_and_records_timeout(tmp_path: Path):
    manifest = _probe_manifest(tmp_path, "import time; time.sleep(3)", timeout_seconds=1)
    report_path = tmp_path / "timeout.json"
    result = _run(
        "--manifest", str(manifest), "--profile", "fast", "--report-json", str(report_path)
    )
    assert result.returncode == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    command = report["gates"][0]["commands"][0]
    assert command["status"] == "timeout"
    assert report["gates"][0]["failure"] == "timeout (fallback=fail)"


def test_quality_timeout_reaps_grandchild(tmp_path):
    import socket
    # 子孙持有监听 socket；超时返回后能重新绑定，证明资源随整树回收。
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    ready = tmp_path / "ready.txt"
    child = ("import socket,time,pathlib; s=socket.socket(); "
             f"s.bind(('127.0.0.1',{port})); s.listen(); "
             f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(30)")
    parent = ("import subprocess,sys,time; "
              f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)")
    manifest = _probe_manifest(tmp_path, parent, timeout_seconds=2)
    result = _run("--manifest", str(manifest), "--profile", "fast")
    assert result.returncode == 1 and ready.exists(), result.stderr
    deadline = time.monotonic() + 3
    while True:
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_posix_quality_timeout_terminates_owned_group(monkeypatch):
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT / "scripts"))
    import process_tree
    killed, spawned = [], []

    class Process:
        pid, returncode, text_mode = 12345, -9, False
        calls = 0

        def communicate(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["probe"], 1)
            return b"partial output", b""

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

    monkeypatch.setattr(process_tree, "os", SimpleNamespace(
        name="posix", fspath=os.fspath, killpg=lambda pid, sig: killed.append((pid, sig))))
    monkeypatch.setattr(process_tree, "signal", SimpleNamespace(SIGKILL=9))

    def popen(argv, **kwargs):
        spawned.append(kwargs)
        return Process()

    monkeypatch.setattr(process_tree.subprocess, "Popen", popen)
    import pytest
    with pytest.raises(subprocess.TimeoutExpired) as error:
        process_tree.run(["probe"], timeout=1)
    assert error.value.stdout == b"partial output"
    assert spawned[0]["start_new_session"] is True
    assert killed and all(item == (12345, 9) for item in killed)


def test_manifest_validator_rejects_network_enabled_gate(tmp_path: Path):
    manifest = _manifest()
    manifest["gates"][0]["network"] = "allowed"
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(manifest), encoding="utf-8")
    result = _run("--manifest", str(invalid), "--dry-run")
    assert result.returncode == 2
    assert "must forbid network" in result.stderr


def test_smoke_screenshot_output_is_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", "--", ".-shots/smoke.png"],
        cwd=ROOT, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, "冒烟截图不得作为未分类产物进入 Git"
