# -*- coding: utf-8 -*-
"""网页版镜像的共享容器冒烟（CI 与手工发布同一份实现）。

对**已存在的镜像**创建唯一命名的临时容器：
- 只发布到宿主 loopback 的动态端口，绝不占用生产 8510，也不挂载任何宿主卷；
- 只注入本测试自己的非敏感环境变量，不读 ``.env``、不传真实密钥；
- 断言 ``/api/health`` 的 ``.ok``；``--require-account`` 时再断言
  ``account.required=true`` 且受保护 API 未登录返回 401；
- 无论成功失败，只删除本脚本创建、且带本次唯一 label 的容器。

Docker 服务端由调用方（CI runner 或服务器管理员）提供；本脚本内部不 sudo、不 shell
插值。没有 Docker 或镜像不存在时明确失败，不伪造成功。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

CONTAINER_PORT = 8510
HEALTH_PATH = "/api/health"
PROTECTED_PATH = "/api/domain/manifest"
LABEL_KEY = "io.biodata.web-smoke"
MAX_LOG_CHARS = 4000
# 显式忽略宿主代理环境：冒烟只允许走容器发布的 loopback 端口。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SmokeError(RuntimeError):
    """容器冒烟未通过。"""


def _docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    """唯一 Docker 调用出口：参数列表、无 shell、固定上限超时。"""
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except FileNotFoundError as exc:
        raise SmokeError("docker CLI not found; cannot smoke the image") from exc
    except subprocess.TimeoutExpired as exc:
        raise SmokeError(f"docker {args[0]} timed out") from exc


def _require_ok(proc: subprocess.CompletedProcess[str], action: str) -> None:
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-MAX_LOG_CHARS:]
        raise SmokeError(f"{action} failed (exit {proc.returncode}): {detail}")


def _fetch(url: str) -> tuple[int | None, bytes]:
    try:
        with _OPENER.open(url, timeout=3) as response:
            return response.status, response.read(65536)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536)
    except (urllib.error.URLError, OSError):
        return None, b""


def _json(body: bytes) -> dict | None:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _published_port(name: str, docker_runner, sleep, monotonic) -> int:
    deadline = monotonic() + 10.0
    last = "no mapping"
    while monotonic() < deadline:
        proc = docker_runner(["port", name, f"{CONTAINER_PORT}/tcp"])
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                _, separator, port = line.strip().rpartition(":")
                if separator and port.isdigit():
                    return int(port)
        last = (proc.stderr or proc.stdout or "").strip()[-200:] or last
        sleep(0.2)
    raise SmokeError(f"docker did not publish a loopback port for {name}: {last}")


def _wait_health(base_url: str, timeout: float, require_account: bool, fetch, sleep, monotonic) -> dict:
    deadline = monotonic() + timeout
    last = "no response"
    while monotonic() < deadline:
        status, body = fetch(base_url + HEALTH_PATH)
        if status == 200:
            health = _json(body)
            if health is None or health.get("ok") is not True:
                raise SmokeError("/api/health returned 200 but .ok is not true")
            if require_account and (health.get("account") or {}).get("required") is not True:
                raise SmokeError(
                    "/api/health returned 200 but account.required is not true in guard mode"
                )
            return health
        last = f"status={status}" if status is not None else "connection failed"
        sleep(1.0)
    raise SmokeError(f"/api/health did not return 200 within {timeout:g}s (last {last})")


def _assert_account_gate(base_url: str, fetch) -> None:
    status, body = fetch(base_url + PROTECTED_PATH)
    if status != 401:
        raise SmokeError(
            f"protected API {PROTECTED_PATH} returned {status!r}; expected 401 without a session"
        )
    payload = _json(body)
    if payload is None or payload.get("error") != "auth_required":
        raise SmokeError(
            f"protected API {PROTECTED_PATH} returned 401 but error != auth_required"
        )


def _dump_logs(name: str, docker_runner) -> None:
    try:
        proc = docker_runner(["logs", "--tail", "40", name])
    except Exception:  # 诊断不得覆盖真正的失败原因
        return
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if text:
        print("[smoke] container logs (tail 40):", file=sys.stderr)
        print(text[-MAX_LOG_CHARS:], file=sys.stderr)


def _cleanup(name: str, run_id: str, docker_runner) -> bool:
    """只删除 label 仍等于本次 run_id 的容器；已不存在视为干净。"""
    try:
        inspect = docker_runner(
            [
                "container",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{LABEL_KEY}" }}}}',
                name,
            ]
        )
        if inspect.returncode != 0:
            detail = (inspect.stderr or inspect.stdout or "").lower()
            if re.search(r"\bno such (?:container|object)(?::|\s*$)", detail):
                return True
            print(
                f"[smoke] cannot verify ownership before cleanup of {name}: "
                f"{(inspect.stderr or inspect.stdout or '').strip()[-500:]}",
                file=sys.stderr,
            )
            return False
        if inspect.stdout.strip() != run_id:
            print(
                f"[smoke] refusing to remove {name}: ownership label does not match this run",
                file=sys.stderr,
            )
            return False
        removed = docker_runner(["rm", "-f", name])
        if removed.returncode != 0:
            print(
                f"[smoke] failed to remove own container {name}: "
                f"{(removed.stderr or removed.stdout or '').strip()[-500:]}",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as exc:  # Docker 已不可用时仍要如实返回清理失败
        print(f"[smoke] cleanup error for {name}: {exc}", file=sys.stderr)
        return False


def run_smoke(
    image: str,
    *,
    require_account: bool = False,
    timeout: float = 120.0,
    docker_runner=_docker,
    fetch=_fetch,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> dict:
    if not image or image.startswith("-"):
        raise SmokeError(f"invalid image reference: {image!r}")
    _require_ok(docker_runner(["image", "inspect", image]), f"image {image!r} is not available")

    run_id = uuid.uuid4().hex[:12]
    name = f"biodata-web-smoke-{run_id}"
    args = [
        "run",
        "-d",
        "--name",
        name,
        "--label",
        f"{LABEL_KEY}={run_id}",
        # 动态宿主端口，只绑 loopback：不占生产 8510，也不暴露到公网。
        "--publish",
        f"127.0.0.1::{CONTAINER_PORT}",
        "--env",
        f"PORT={CONTAINER_PORT}",
        "--env",
        "BIODATA_WEB_HOST=0.0.0.0",
        # 临时数据根在容器 /tmp 内；不挂载生产 /data，不读任何 .env。
        "--env",
        "BIODATA_DATA_ROOT=/tmp/biodata-web-smoke",
    ]
    if require_account:
        args += [
            "--env",
            "BIODATA_REQUIRE_ACCOUNT=1",
            # 以下均为冒烟专用非敏感值，与真实部署凭据无关。
            "--env",
            "BIODATA_INVITE_CODE=ci-smoke-invite",
            "--env",
            "BIODATA_TRUSTED_HOSTS=127.0.0.1",
            "--env",
            "BIODATA_LLM_DAILY_PER_USER=1",
            "--env",
            "BIODATA_LLM_DAILY_GLOBAL=1",
        ]
    args.append(image)

    created = False
    failure: BaseException | None = None
    health: dict = {}
    try:
        created = True
        started = docker_runner(args)
        _require_ok(started, "docker run")
        if not (started.stdout or "").strip():
            raise SmokeError("docker run did not return a container id")
        port = _published_port(name, docker_runner, sleep, monotonic)
        base_url = f"http://127.0.0.1:{port}"
        print(f"[smoke] container {name} listening on {base_url}")
        health = _wait_health(base_url, timeout, require_account, fetch, sleep, monotonic)
        print(f"[smoke] health ok: version={health.get('version')}")
        if require_account:
            _assert_account_gate(base_url, fetch)
            print(f"[smoke] account gate ok: {PROTECTED_PATH} -> 401")
    except BaseException as exc:
        failure = exc
        if created:
            _dump_logs(name, docker_runner)
    cleanup_ok = _cleanup(name, run_id, docker_runner) if created else True
    if failure is not None:
        if not cleanup_ok:
            print(f"[smoke] warning: temporary container {name} was not removed", file=sys.stderr)
        raise failure
    if not cleanup_ok:
        raise SmokeError(f"temporary container {name} was not removed")
    print(f"[smoke] PASS {image}")
    return health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smoke_web_image.py",
        description="Smoke an existing biodata-web image in an isolated loopback container.",
    )
    parser.add_argument("--image", required=True, help="existing local image reference")
    parser.add_argument(
        "--require-account",
        action="store_true",
        help="also assert account.required=true and a 401 on a protected API (deploy guard shape)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="seconds to wait for /api/health (default: 120)",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        run_smoke(args.image, require_account=args.require_account, timeout=args.timeout)
    except SmokeError as exc:
        print(f"[smoke] FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
