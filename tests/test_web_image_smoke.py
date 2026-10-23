# -*- coding: utf-8 -*-
"""共享容器冒烟的编排测试：mock Docker/HTTP，不联网、不启动真实容器。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import smoke_web_image as smoke  # noqa: E402


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


class FakeDocker:
    def __init__(
        self,
        *,
        image_rc: int = 0,
        run_rc: int = 0,
        port: str | None = "127.0.0.1:49153",
        rm_rc: int = 0,
        container_exists: bool = True,
    ) -> None:
        self.image_rc = image_rc
        self.run_rc = run_rc
        self.port = port
        self.rm_rc = rm_rc
        self.container_exists = container_exists
        self.label_override: str | None = None
        self.inspect_error = "No such container"
        self.label = ""
        self.name = ""
        self.run_args: list[str] = []
        self.removed: str | None = None
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if args[:2] == ["image", "inspect"]:
            return _proc(self.image_rc, "", "" if self.image_rc == 0 else "No such image")
        if args[0] == "run":
            self.name = args[args.index("--name") + 1]
            self.run_args = list(args)
            prefix = smoke.LABEL_KEY + "="
            self.label = next((item[len(prefix):] for item in args if item.startswith(prefix)), "")
            if self.run_rc != 0:
                self.container_exists = False
            return _proc(
                self.run_rc,
                "container-id\n" if self.run_rc == 0 else "",
                "" if self.run_rc == 0 else "run failed",
            )
        if args[0] == "port":
            if self.run_rc != 0 or self.port is None:
                return _proc(1, "", "no port")
            return _proc(0, self.port + "\n", "")
        if args[:2] == ["container", "inspect"]:
            if not self.container_exists:
                return _proc(1, "", self.inspect_error)
            return _proc(0, (self.label_override or self.label) + "\n", "")
        if args[:2] == ["rm", "-f"]:
            self.removed = args[2]
            return _proc(self.rm_rc, "", "" if self.rm_rc == 0 else "rm failed")
        if args[0] == "logs":
            return _proc(0, "fixture log line\n", "")
        raise AssertionError(f"unexpected docker call: {args}")


class FakeHTTP:
    def __init__(
        self,
        *,
        health: tuple[int | None, bytes] = (200, _json_bytes({"ok": True, "version": "1.2.3"})),
        protected: tuple[int | None, bytes] = (401, _json_bytes({"ok": False, "error": "auth_required"})),
    ) -> None:
        self.health = health
        self.protected = protected
        self.calls: list[str] = []

    def __call__(self, url: str) -> tuple[int | None, bytes]:
        self.calls.append(url)
        return self.protected if url.endswith(smoke.PROTECTED_PATH) else self.health


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.01)


def _run(docker: FakeDocker, http: FakeHTTP, **kwargs):
    clock = Clock()
    return smoke.run_smoke(
        "biodata-web:test",
        docker_runner=docker,
        fetch=http,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )


def test_health_success_uses_dynamic_loopback_port_and_own_cleanup() -> None:
    docker = FakeDocker()
    http = FakeHTTP()
    result = _run(docker, http, timeout=2)

    assert result["ok"] is True
    assert docker.run_args[-1] == "biodata-web:test"
    assert docker.run_args[docker.run_args.index("--publish") + 1] == "127.0.0.1::8510"
    assert "--env-file" not in docker.run_args
    assert not any(arg in {"-v", "--volume", "--mount"} for arg in docker.run_args)
    assert "BIODATA_REQUIRE_ACCOUNT=1" not in docker.run_args
    assert "BIODATA_DATA_ROOT=/tmp/biodata-web-smoke" in docker.run_args
    assert http.calls == ["http://127.0.0.1:49153/api/health"]
    assert ["rm", "-f", docker.name] in docker.calls
    assert docker.removed == docker.name


def test_require_account_checks_health_flag_and_protected_401() -> None:
    docker = FakeDocker()
    http = FakeHTTP(
        health=(200, _json_bytes({"ok": True, "account": {"required": True}, "version": "1.2.3"}))
    )
    _run(docker, http, require_account=True, timeout=2)

    assert "BIODATA_REQUIRE_ACCOUNT=1" in docker.run_args
    assert "BIODATA_INVITE_CODE=ci-smoke-invite" in docker.run_args
    assert http.calls == [
        "http://127.0.0.1:49153/api/health",
        "http://127.0.0.1:49153" + smoke.PROTECTED_PATH,
    ]
    assert ["rm", "-f", docker.name] in docker.calls


def test_guard_mode_requires_account_required_true() -> None:
    docker = FakeDocker()
    http = FakeHTTP(health=(200, _json_bytes({"ok": True, "account": {"required": False}})))
    with pytest.raises(smoke.SmokeError, match="account.required"):
        _run(docker, http, require_account=True, timeout=2)
    assert ["rm", "-f", docker.name] in docker.calls


def test_guard_mode_rejects_a_protected_api_that_is_not_401() -> None:
    docker = FakeDocker()
    http = FakeHTTP(
        health=(200, _json_bytes({"ok": True, "account": {"required": True}})),
        protected=(200, _json_bytes({"ok": True})),
    )
    with pytest.raises(smoke.SmokeError, match="expected 401"):
        _run(docker, http, require_account=True, timeout=2)
    assert ["rm", "-f", docker.name] in docker.calls


def test_guard_mode_requires_the_auth_required_error_body() -> None:
    docker = FakeDocker()
    http = FakeHTTP(
        health=(200, _json_bytes({"ok": True, "account": {"required": True}})),
        protected=(401, _json_bytes({"ok": False})),
    )
    with pytest.raises(smoke.SmokeError, match="error != auth_required"):
        _run(docker, http, require_account=True, timeout=2)
    assert ["rm", "-f", docker.name] in docker.calls


def test_health_timeout_is_a_nonzero_failure_and_still_cleans_up() -> None:
    docker = FakeDocker()
    http = FakeHTTP(health=(None, b""))
    with pytest.raises(smoke.SmokeError, match="did not return 200"):
        _run(docker, http, timeout=2)
    assert ["rm", "-f", docker.name] in docker.calls


def test_health_200_without_ok_true_is_a_failure() -> None:
    docker = FakeDocker()
    http = FakeHTTP(health=(200, _json_bytes({"ok": False})))
    with pytest.raises(smoke.SmokeError, match=".ok is not true"):
        _run(docker, http, timeout=2)
    assert ["rm", "-f", docker.name] in docker.calls


def test_cleanup_never_removes_a_container_with_a_foreign_label() -> None:
    docker = FakeDocker()
    docker.label_override = "another-run"
    http = FakeHTTP()
    with pytest.raises(smoke.SmokeError, match="was not removed"):
        _run(docker, http, timeout=2)
    assert not any(args[:2] == ["rm", "-f"] for args in docker.calls)


def test_cleanup_failure_after_healthy_run_is_nonzero() -> None:
    docker = FakeDocker(rm_rc=1)
    http = FakeHTTP()
    with pytest.raises(smoke.SmokeError, match="was not removed"):
        _run(docker, http, timeout=2)


def test_missing_container_during_cleanup_counts_as_already_gone() -> None:
    docker = FakeDocker(container_exists=False)
    http = FakeHTTP()
    _run(docker, http, timeout=2)
    assert not any(args[:2] == ["rm", "-f"] for args in docker.calls)


@pytest.mark.parametrize("error", [
    "Cannot connect to the Docker daemon",
    "dial unix /var/run/docker.sock: connect: no such file or directory",
    "docker endpoint not found",
])
def test_cleanup_reports_docker_unavailable_instead_of_fake_clean(error: str) -> None:
    docker = FakeDocker(container_exists=False)
    docker.inspect_error = error
    http = FakeHTTP()
    with pytest.raises(smoke.SmokeError, match="was not removed"):
        _run(docker, http, timeout=2)
    assert not any(args[:2] == ["rm", "-f"] for args in docker.calls)


def test_timed_out_docker_run_still_cleans_its_created_container() -> None:
    class TimedOutDocker(FakeDocker):
        def __call__(self, args):
            result = super().__call__(args)
            if args[0] == "run":
                raise smoke.SmokeError("docker run timed out")
            return result

    docker = TimedOutDocker()
    with pytest.raises(smoke.SmokeError, match="docker run timed out"):
        _run(docker, FakeHTTP(), timeout=2)
    assert docker.removed == docker.name


def test_missing_image_fails_before_running_a_container() -> None:
    docker = FakeDocker(image_rc=1)
    http = FakeHTTP()
    with pytest.raises(smoke.SmokeError, match="not available"):
        _run(docker, http, timeout=2)
    assert not any(args[0] == "run" for args in docker.calls)
    assert not any(args[:2] == ["rm", "-f"] for args in docker.calls)


def test_failed_docker_run_is_not_reported_as_success() -> None:
    docker = FakeDocker(run_rc=1)
    http = FakeHTTP()
    with pytest.raises(smoke.SmokeError, match="docker run failed"):
        _run(docker, http, timeout=2)
    assert not any(args[:2] == ["rm", "-f"] for args in docker.calls)


def test_docker_binary_missing_is_a_clear_failure(monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(smoke.subprocess, "run", missing)
    with pytest.raises(smoke.SmokeError, match="docker CLI not found"):
        smoke._docker(["image", "inspect", "biodata-web:test"])


def test_container_names_are_unique_per_run() -> None:
    first = FakeDocker()
    _run(first, FakeHTTP(), timeout=2)
    second = FakeDocker()
    _run(second, FakeHTTP(), timeout=2)
    assert first.name != second.name


def test_cli_requires_an_image_and_a_positive_timeout() -> None:
    with pytest.raises(SystemExit) as missing:
        smoke.main([])
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as timeout:
        smoke.main(["--image", "biodata-web:test", "--timeout", "0"])
    assert timeout.value.code == 2


def test_cli_passes_the_guard_flag_and_timeout(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake(image, *, require_account, timeout):
        seen.update(image=image, require_account=require_account, timeout=timeout)
        return {}

    monkeypatch.setattr(smoke, "run_smoke", fake)
    assert smoke.main(["--image", "biodata-web:test", "--require-account", "--timeout", "5"]) == 0
    assert seen == {"image": "biodata-web:test", "require_account": True, "timeout": 5.0}


def test_cli_maps_smoke_failure_to_exit_one(monkeypatch, capsys) -> None:
    def fail(*args, **kwargs):
        raise smoke.SmokeError("boom")

    monkeypatch.setattr(smoke, "run_smoke", fail)
    assert smoke.main(["--image", "biodata-web:test"]) == 1
    assert "boom" in capsys.readouterr().err


def test_web_image_workflow_uses_the_shared_smoke_script() -> None:
    workflow = (ROOT / ".github" / "workflows" / "web-image.yml").read_text(encoding="utf-8")
    assert "python3 scripts/smoke_web_image.py --image biodata-web:ci --require-account" in workflow
    assert "  workflow_dispatch:" in workflow
    assert "\n  push:" not in workflow
    assert "\n  pull_request:" not in workflow
    # 旧 inline smoke 的固定容器名与 curl 轮询已退役，避免第二份实现回流。
    assert "biodata-web-smoke" not in workflow
    assert "curl -sf" not in workflow
