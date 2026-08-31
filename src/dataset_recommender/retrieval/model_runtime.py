# -*- coding: utf-8 -*-
"""隔离本地模型运行时的路径与 JSONL 客户端。

在线安装的 torch/transformers 全部住 data_root/model-runtime/venv，主 FastAPI 进程
不修改 sys.path。打分通过该 venv 的常驻 worker 进行，依赖冲突或 worker 崩溃只会让
vector_recall 回退规则顺序。
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Sequence

from ..app.runtime_paths import AppPaths, get_app_paths
from .model_worker import MODEL_ID, model_files_ready
from .vector_recall import DEFAULT_CROSS_ENCODER_MODEL

READY_SCHEMA = "biodata-model-runtime/v1"
STARTUP_TIMEOUT_S = 300.0
SCORE_TIMEOUT_S = 180.0


def runtime_root(paths: "AppPaths | None" = None) -> Path:
    return (paths or get_app_paths()).data_root / "model-runtime"


def runtime_python(paths: "AppPaths | None" = None) -> Path:
    return runtime_root(paths) / "venv" / "Scripts" / "python.exe"


def ready_manifest_path(paths: "AppPaths | None" = None) -> Path:
    return runtime_root(paths) / "READY.json"


def worker_script(paths: "AppPaths | None" = None) -> Path:
    resolved = paths or get_app_paths()
    if resolved.runtime_mode == "frozen":
        return resolved.resource_root / "tools" / "model_worker.py"
    return Path(__file__).resolve().with_name("model_worker.py")


def model_dir(paths: "AppPaths | None" = None) -> Path:
    return (paths or get_app_paths()).model_root / "cross_encoders" / DEFAULT_CROSS_ENCODER_MODEL


def read_ready_manifest(paths: "AppPaths | None" = None) -> dict:
    path = ready_manifest_path(paths)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(value, dict) or value.get("schema") != READY_SCHEMA or value.get("model_id") != MODEL_ID:
        return {}
    return value


def _manifest_files_intact(manifest: dict, paths: "AppPaths | None" = None) -> bool:
    """核对 READY manifest 记录的关键文件大小与磁盘实际一致。

    旧 manifest 若无 ``model_files`` 尺寸清单，退化为仅存在性检查（model_files_ready 已覆盖）。
    清单必须覆盖配置、分词器与真权重，缺项即视为不完整（防 manifest 被篡改/写坏）。"""
    files = manifest.get("model_files")
    if not isinstance(files, dict) or not files:
        return True
    target = model_dir(paths)
    for relative, expected in files.items():
        path = target / relative
        if not path.is_file():
            return False
        try:
            if path.stat().st_size != int(expected):
                return False
        except (OSError, ValueError, TypeError):
            return False
    if "config.json" not in files:
        return False
    if not any(name in files for name in ("tokenizer.json", "tokenizer_config.json")):
        return False
    if not any(name.endswith((".safetensors", ".bin")) for name in files):
        return False
    return True


def external_runtime_ready(paths: "AppPaths | None" = None) -> bool:
    resolved = paths or get_app_paths()
    manifest = read_ready_manifest(resolved)
    if not manifest:
        return False
    if not (runtime_python(resolved).is_file() and worker_script(resolved).is_file()):
        return False
    if not model_files_ready(model_dir(resolved)):
        return False
    return _manifest_files_intact(manifest, resolved)


class ExternalCrossScorer:
    def __init__(self, paths: "AppPaths | None" = None) -> None:
        self.paths = paths or get_app_paths()
        self.process: "subprocess.Popen[str] | None" = None
        self.responses: "queue.Queue[dict]" = queue.Queue()
        self.stderr_lines: "queue.Queue[str]" = queue.Queue(maxsize=40)
        self.lock = threading.Lock()

    def _reader(self, stream) -> None:
        for line in stream:
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                self.responses.put(payload)

    def _stderr_reader(self, stream) -> None:
        for line in stream:
            text = str(line).strip()[:500]
            if not text:
                continue
            if self.stderr_lines.full():
                try:
                    self.stderr_lines.get_nowait()
                except queue.Empty:
                    pass
            self.stderr_lines.put(text)

    def _start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        if not external_runtime_ready(self.paths):
            raise RuntimeError("model_runtime_not_ready")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [str(runtime_python(self.paths)), str(worker_script(self.paths)), "--serve", str(model_dir(self.paths))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
        )
        assert self.process.stdout is not None and self.process.stderr is not None
        threading.Thread(target=self._reader, args=(self.process.stdout,), daemon=True).start()
        threading.Thread(target=self._stderr_reader, args=(self.process.stderr,), daemon=True).start()
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("model_worker_start_failed")
            try:
                payload = self.responses.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if payload.get("ready") is True:
                return
        self.close()
        raise TimeoutError("model_worker_start_timeout")

    def __call__(self, pairs: "Sequence[tuple[str, str]]") -> "list[float]":
        with self.lock:
            self._start()
            assert self.process is not None and self.process.stdin is not None
            request_id = uuid.uuid4().hex
            request = {"id": request_id, "pairs": [[str(a), str(b)] for a, b in pairs]}
            self.process.stdin.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + SCORE_TIMEOUT_S
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("model_worker_exited")
                try:
                    payload = self.responses.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if payload.get("id") != request_id:
                    continue
                if payload.get("ok") is not True or not isinstance(payload.get("scores"), list):
                    raise RuntimeError("model_worker_bad_response")
                return [float(value) for value in payload["scores"]]
            self.close()
            raise TimeoutError("model_worker_score_timeout")

    def close(self) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:
                pass


_SCORERS: "list[ExternalCrossScorer]" = []


def external_cross_scorer(paths: "AppPaths | None" = None) -> ExternalCrossScorer:
    scorer = ExternalCrossScorer(paths)
    _SCORERS.append(scorer)
    return scorer


@atexit.register
def _close_workers() -> None:
    for scorer in list(_SCORERS):
        scorer.close()
