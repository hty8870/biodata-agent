# -*- coding: utf-8 -*-
"""本地语义模型隔离 worker。

本文件会被 PyInstaller 作为普通 data 复制到 ``_internal/tools/model_worker.py``，
再由 data_root/model-runtime/venv 的独立 Python 执行。它刻意不 import 主项目，避免
在线安装的 torch/transformers 依赖进入 FastAPI/pywebview 主进程。

两种模式：
- ``--download DIR``：从固定 ModelScope id 下载，失败回退固定 HuggingFace id；
- ``--serve DIR``：加载本地 CrossEncoder，以 JSONL stdin/stdout 提供有界打分服务。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

MODEL_ID = "BAAI/bge-reranker-v2-m3"
IGNORE_PATTERNS = ["*.onnx", "onnx/*", "*openvino*", "*.gguf", "*imatrix*", "*.h5", "*.msgpack"]
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_PAIRS = 5000
MAX_QUERY_CHARS = 4096
MAX_DOCUMENT_CHARS = 8192


def model_file_manifest(target: Path) -> "dict[str, int]":
    """记录关键文件大小：配置、分词器与真权重（非空）。供 READY manifest 完整性核对。"""
    root = Path(target)
    manifest: dict[str, int] = {}
    for name in ("config.json",):
        path = root / name
        if path.is_file():
            manifest[name] = path.stat().st_size
    for name in ("tokenizer.json", "tokenizer_config.json"):
        path = root / name
        if path.is_file():
            manifest[name] = path.stat().st_size
    for pattern in ("*.safetensors", "*.bin"):
        for path in sorted(root.rglob(pattern)):
            if path.is_file() and path.stat().st_size > 0:
                manifest[path.relative_to(root).as_posix()] = path.stat().st_size
    return manifest


def model_files_ready(target: Path) -> bool:
    """目录必须同时有配置、分词器与真权重；非空目录绝不是完成证据。"""
    files = model_file_manifest(target)
    if "config.json" not in files:
        return False
    if not any(name in files for name in ("tokenizer.json", "tokenizer_config.json")):
        return False
    return any(name.endswith((".safetensors", ".bin")) for name in files)


def _copy_snapshot(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)


def download_model(target: Path) -> bool:
    target = Path(target)
    if model_files_ready(target):
        print(json.dumps({"stage": "model", "message": "模型已经就绪，无需重复下载。"}, ensure_ascii=False), flush=True)
        return True
    target.mkdir(parents=True, exist_ok=True)

    try:
        from modelscope import snapshot_download as modelscope_download

        print(json.dumps({"stage": "model", "message": "正在从 ModelScope 下载模型权重…"}, ensure_ascii=False), flush=True)
        source = Path(modelscope_download(MODEL_ID, ignore_file_pattern=IGNORE_PATTERNS))
        _copy_snapshot(source, target)
        if model_files_ready(target):
            print(json.dumps({"stage": "model", "message": "ModelScope 模型下载完成。"}, ensure_ascii=False), flush=True)
            return True
        print("ModelScope 返回目录缺少完整权重，尝试 HuggingFace。", file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001（下载源失败必须回退另一官方镜像）
        print(f"ModelScope 下载失败（{type(exc).__name__}），尝试 HuggingFace。", file=sys.stderr, flush=True)

    try:
        from huggingface_hub import snapshot_download as huggingface_download

        print(json.dumps({"stage": "model", "message": "正在从 HuggingFace 下载模型权重…"}, ensure_ascii=False), flush=True)
        huggingface_download(repo_id=MODEL_ID, local_dir=str(target), ignore_patterns=IGNORE_PATTERNS)
        if model_files_ready(target):
            print(json.dumps({"stage": "model", "message": "HuggingFace 模型下载完成。"}, ensure_ascii=False), flush=True)
            return True
        print("HuggingFace 下载完成但模型文件不完整。", file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"HuggingFace 下载失败（{type(exc).__name__}）。", file=sys.stderr, flush=True)
    return False


def _valid_pairs(value: Any) -> "list[tuple[str, str]] | None":
    if not isinstance(value, list) or not value or len(value) > MAX_PAIRS:
        return None
    out: "list[tuple[str, str]]" = []
    for row in value:
        if not isinstance(row, list) or len(row) != 2:
            return None
        query, document = row
        if not isinstance(query, str) or not isinstance(document, str):
            return None
        if len(query) > MAX_QUERY_CHARS or len(document) > MAX_DOCUMENT_CHARS:
            return None
        out.append((query, document))
    return out


def serve(model_dir: Path) -> int:
    if not model_files_ready(model_dir):
        print("模型文件不完整。", file=sys.stderr)
        return 2
    try:
        from sentence_transformers import CrossEncoder

        try:
            model = CrossEncoder(str(model_dir), max_length=512, local_files_only=True)
        except TypeError:
            model = CrossEncoder(str(model_dir), max_length=512)
    except Exception as exc:  # noqa: BLE001
        print(f"模型加载失败：{type(exc).__name__}", file=sys.stderr)
        return 3

    print(json.dumps({"ready": True, "model": MODEL_ID}), flush=True)
    for raw in sys.stdin.buffer:
        if len(raw) > MAX_REQUEST_BYTES:
            print(json.dumps({"id": "", "ok": False, "error": "request_too_large"}), flush=True)
            continue
        try:
            request = json.loads(raw.decode("utf-8"))
            request_id = str(request.get("id", ""))[:96]
            pairs = _valid_pairs(request.get("pairs"))
            if not request_id or pairs is None:
                raise ValueError("bad_request")
            scores = model.predict(pairs, batch_size=32, show_progress_bar=False)
            payload = {"id": request_id, "ok": True, "scores": [float(value) for value in scores]}
        except Exception as exc:  # noqa: BLE001（协议错误必须留在 worker，不炸主应用）
            payload = {"id": str(locals().get("request_id", "")), "ok": False, "error": type(exc).__name__}
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="BioData Agent 本地模型隔离 worker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--download", metavar="DIR")
    group.add_argument("--serve", metavar="DIR")
    args = parser.parse_args(argv)
    if args.download:
        return 0 if download_model(Path(args.download)) else 1
    return serve(Path(args.serve))


if __name__ == "__main__":
    raise SystemExit(main())
