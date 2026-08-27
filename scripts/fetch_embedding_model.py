# -*- coding: utf-8 -*-
"""一次性把「向量召回」用的本地模型下到项目内 models/ 目录，之后运行时**不再联网**。

两类模型（都存到 models/ 下的固定子目录，vector_recall 从那里离线加载）：
- **cross_encoder（默认，推荐）**：bge-reranker-v2-m3 → models/cross_encoders/bge-reranker-v2-m3
  语义重排器（MIT 许可、短中文查询表现好）。
- **dense（可选）**：多语稠密嵌入 → models/embeddings/<name>（备选后端）。

下载源优先级：**ModelScope（魔搭，国内直连快）** → 失败回退 HuggingFace。
主项目 requirements 刻意零重依赖；本脚本与模型只在启用向量召回时需要。未下载时向量召回
自动回退为规则顺序，不报错、不影响结果正确性。

用法：
  pip install -r requirements/requirements-embeddings.txt
  py scripts/fetch_embedding_model.py                    # 下 cross-encoder（默认）
  py scripts/fetch_embedding_model.py --cross-encoder    # 同上（显式）
  py scripts/fetch_embedding_model.py --dense            # 下稠密嵌入（备选后端）
  py scripts/fetch_embedding_model.py --all              # 两者都下
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# sys.path 锚定真实源码位置（本文件上两级 = 仓库根）；模型落盘目录由
# vector_recall.default_*_dir 经 runtime_paths.model_root 解析（source/portable = 项目根/models，
# 历史逐字节一致；frozen 由新启动器接管模型安装，本脚本不服务 frozen）。
_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC_ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dataset_recommender.retrieval.vector_recall import (  # noqa: E402
    DEFAULT_CROSS_ENCODER_MODEL, DEFAULT_EMBEDDING_MODEL,
    default_cross_encoder_dir, default_model_dir,
)
from dataset_recommender.retrieval.model_worker import (  # noqa: E402
    IGNORE_PATTERNS as CROSS_IGNORE,
    MODEL_ID as CROSS_MODEL_ID,
)

# 本地子目录名 -> (ModelScope id, HuggingFace id)
CROSS_MS, CROSS_HF = CROSS_MODEL_ID, CROSS_MODEL_ID
DENSE_MS = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DENSE_HF = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
IGNORE = list(CROSS_IGNORE)


def _fetch(ms_id: str, hf_id: str, target: Path) -> bool:
    if target.exists() and any(target.iterdir()):
        print(f"[fetch] 已存在：{target}（如需重下，先删该目录）")
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    # 1) ModelScope 优先
    try:
        import os
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(k, None)  # 国产站直连绕过慢代理
        from modelscope import snapshot_download as ms_dl
        print(f"[fetch] ModelScope 下载 {ms_id} → {target} …（首次几十~几百 MB）")
        src = ms_dl(ms_id, ignore_file_pattern=IGNORE)
        target.mkdir(parents=True, exist_ok=True)
        for item in Path(src).iterdir():
            (shutil.copytree if item.is_dir() else shutil.copy2)(item, target / item.name)
        print(f"[fetch] 完成（ModelScope）：{target}")
        return True
    except Exception as exc:
        print(f"[fetch] ModelScope 失败（{exc}），回退 HuggingFace …", file=sys.stderr)
    # 2) HuggingFace 回退
    try:
        from huggingface_hub import snapshot_download as hf_dl
        hf_dl(repo_id=hf_id, local_dir=str(target), ignore_patterns=IGNORE)
        print(f"[fetch] 完成（HuggingFace）：{target}")
        return True
    except Exception as exc:
        print(f"[fetch] HuggingFace 也失败（{exc}）。中国网络可设 $env:HF_ENDPOINT=\"https://hf-mirror.com\" 重试。", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="下载向量召回用的本地模型到 models/")
    ap.add_argument("--cross-encoder", action="store_true", help="下 cross-encoder 重排器（默认）")
    ap.add_argument("--dense", action="store_true", help="下稠密嵌入（备选后端）")
    ap.add_argument("--all", action="store_true", help="两者都下")
    args = ap.parse_args()

    do_cross = args.cross_encoder or args.all or not (args.dense or args.cross_encoder or args.all)
    do_dense = args.dense or args.all

    ok = True
    if do_cross:
        ok &= _fetch(CROSS_MS, CROSS_HF, default_cross_encoder_dir(DEFAULT_CROSS_ENCODER_MODEL))
    if do_dense:
        ok &= _fetch(DENSE_MS, DENSE_HF, default_model_dir(DEFAULT_EMBEDDING_MODEL))

    if ok:
        print("[fetch] 就绪。Web 设置里开「实验：向量召回」，或 CLI 加 --recall cross_encoder。")
        print("[fetch] 验证：py scripts/evaluate_recall.py")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
