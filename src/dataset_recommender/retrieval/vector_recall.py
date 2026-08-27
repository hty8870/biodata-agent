"""可选「向量召回」层（默认关，本地稠密嵌入，确定性可复现）。

既定方向「规则过滤 + 向量召回 + llm 重排」里的**向量召回**通道。落地为：用本地稠密句向量
模型（sentence-transformers，权重预先下到本地目录、运行时不联网），对**已过硬过滤的存活集**
按与查询的语义相似度重排；可与词面分做融合（hybrid：`fusion="linear"` 为 min-max+α 线性融合，
`fusion="rrf"` 为 RRF 名次融合 k=60——尺度无关、零调参，见 RECALL_FUSIONS 注释）。

设计约束（与 rerank 层同源，defense-in-depth）：
- 只对存活集排序：输入恒为 survivors 子集，输出是其**排列/截断**，绝不引入集合外记录
  → 0% 硬违规由 retriever 终检(passes_hard_filter)结构性保证，本层的排列守卫是冗余。
- 后端 `off`：原样返回（与未启用时**字节等价**）。
- 后端 `dense`：本地嵌入 → 余弦 → 融合 → 稳定排序；模型缺失/依赖未装/任何异常一律回退原序
  （永不报错、永不违规、永不阻塞请求）。
- **可复现**：给定同一模型与输入，稠密嵌入确定 → 适合进分级评测（不像 LLM 重排不可复现）。

依赖边界：本模块**不在导入期引入任何重依赖**；sentence-transformers 仅在真正走 dense 且模型
可用时**惰性导入**。官方评测入口不传 recall 参数 → 结构性走 off，绝不加载模型。

「召回 vs 重排」的诚实说明：本架构里硬过滤已产出**完整**合法集，故稠密向量在此承担的是
「存活集内的语义**召回排序**」，而非「突破规则过滤的扩召回」——后者与 0% 硬违规不变量结构性
冲突（违规判定本身即词面约束匹配，纳入词面漏掉的记录=制造违规）。
"""
from __future__ import annotations

import math
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Sequence

from .retriever import RetrievedCandidate
from ..app.runtime_paths import get_app_paths

RECALL_BACKENDS = ("off", "dense", "cross_encoder")
DEFAULT_RECALL_ALPHA = 0.5
#: dense 融合法：
#: "linear" = min-max 归一化 + α 线性融合（历史默认，字节等价保留）；
#: "rrf" = Reciprocal Rank Fusion——只用两路**名次**、不看分值尺度（余弦 ∈[0,1]、词面分
#: 无上界，min-max 跨分布不稳定是被文献点名的那类；RRF 尺度无关、零调参，k=60 自
#: Cormack 2009 至今是生产默认）。确定性可复现，仍只排存活集（0% 违规不变量不碰）。
RECALL_FUSIONS = ("linear", "rrf")
DEFAULT_RECALL_FUSION = "linear"
RRF_K = 60
DEFAULT_EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
# cross-encoder 重排器：MIT 许可、短中文查询表现好、可审计。
DEFAULT_CROSS_ENCODER_MODEL = "bge-reranker-v2-m3"

# 嵌入器 = 把一批文本编码成（已 L2 归一化的）向量列表的可调用对象。
Embedder = Callable[[Sequence[str]], "list[list[float]]"]
# 交叉打分器 = 把一批 (query, doc) 文本对打成相关性分数的可调用对象（分数越大越相关）。
CrossScorer = Callable[["Sequence[tuple[str, str]]"], "list[float]"]

# 已加载模型缓存：键=解析后的本地模型目录字符串。避免每次请求/每条查询重复加载。
# 纪律：只缓存**成功**加载的模型（正缓存）；加载失败
# （缺目录/缺依赖/异常）**不入缓存**——负缓存会把一次瞬时故障永久化成"必须重启进程才
# 能恢复"。失败路径每次调用都重试，代价仅是一次目录存在性检查/导入探测，可忽略。
_EMBEDDER_CACHE: "dict[str, Embedder]" = {}
_CROSS_CACHE: "dict[str, CrossScorer]" = {}
_WARNED: "set[str]" = set()
_DETERMINISM_DONE = False
#: （并发分流）：模块级单飞锁——`_setup_determinism`（写全局
#: os.environ）与两个模型缓存的 check-then-load 全程持锁。该竞态今天多请求并发即存在
#: （顺带修）；配合 turn 层预热闭合，flight 线程从此不触碰 os.environ。
_MODEL_LOCK = threading.Lock()

# P-1（审计）：**候选文本 → 文档向量** LRU 缓存（键含模型目录）。
# 动机：dense 召回原来每查询把静态语料的候选文本全量重新编码——嵌入恰是最贵的一步，而
# 候选文本与查询无关、语料基本静态 → 按文本缓存即省下重复嵌入。设计要点（why）：
# - **只缓存默认加载路径**（embedder 参数为 None、经 load_embedder 取得编码器）：注入
#   embedder 是单测/评测的隔离接缝（同一文本在不同测试里对应不同 mock 向量），且其合同
#   是「单批 enc([query, *texts])」——过缓存既污染测试隔离又改注入路径调用形状，故逐位不动。
# - **键含模型目录**：不同模型对同一文本产出不同向量，缺目录键换模型即静默串味；解析口径
#   与 load_embedder 的 `cache_key = str(resolved)` 同源（None → default_model_dir()），
#   双处修改须同步。
# - **上限 2048**：基础语料 784 条 + 外部库扩展余量；每条 ~384 维 float 的 list（约 3KB），
#   满载 ~6MB，有界不膨胀。
# - **查询向量不缓存**（每次现编）：与查询强相关，缓存无意义且会跨查询污染。
# - **threading.Lock**：Web 线程池并发请求下 OrderedDict 的 move_to_end/popitem 及
#   「查-插」复合操作非原子，会竞态；锁内只做字典操作，**绝不持锁调 enc**（编码是重活，
#   持锁会把并发请求串成单列）。
_DOC_VECTOR_CACHE: "OrderedDict[tuple[str, str], list[list[float]]]" = OrderedDict()
_DOC_VECTOR_CACHE_MAX = 2048
_DOC_VECTOR_LOCK = threading.Lock()


def default_model_dir(model_name: str = DEFAULT_EMBEDDING_MODEL) -> Path:
    """dense 嵌入模型目录 = model_root/embeddings/<name>（经 runtime_paths 单一真源：
    source/portable = 项目根/models/…，frozen = data_root/models/…，历史路径逐字节一致）。"""
    return get_app_paths().model_root / "embeddings" / model_name


def default_cross_encoder_dir(model_name: str = DEFAULT_CROSS_ENCODER_MODEL) -> Path:
    """cross-encoder 重排模型目录 = model_root/cross_encoders/<name>（语义同上）。"""
    return get_app_paths().model_root / "cross_encoders" / model_name


def _setup_determinism() -> None:
    """best-effort 可复现（幂等）：eval 语义 + 关 TF32 + cudnn 确定。不启用会崩的严格算法开关，
    因为向量召回只做**排列**，确定性关乎结果可复现而非 0% 违规不变量（那由终检结构性保证）。
    check-then-set 全程持模块级单飞锁（并发下只有第一个线程执行初始化与 os.environ
    写入，其余线程看到 _DETERMINISM_DONE 即返回——flight 线程零 env 触碰）。"""
    global _DETERMINISM_DONE
    if _DETERMINISM_DONE:
        return
    with _MODEL_LOCK:
        if _DETERMINISM_DONE:
            return
        try:
            import os as _os
            _os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
            import torch
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
        except Exception:
            pass
        _DETERMINISM_DONE = True


def _warn_once(key: str, message: str) -> None:
    """同一原因只在 stderr 提示一次；绝不抛异常、绝不打断请求。"""
    if key not in _WARNED:
        _WARNED.add(key)
        print(f"[vector_recall] {message}", file=sys.stderr)


def load_embedder(
    model_dir: "str | Path | None" = None,
    model_name: str = DEFAULT_EMBEDDING_MODEL,
) -> "Embedder | None":
    """惰性加载本地稠密嵌入器；不可用时返回 None（触发回退，绝不下载、绝不抛错）。

    不可用的情形（都返回 None + 提示一次）：
    - 本地模型目录不存在（需先跑 scripts/fetch_embedding_model.py 一次性下载）。
    - 未安装 sentence-transformers（可选依赖）。
    - 加载过程任何异常。

    失败**不缓存**：本次返回 None 后，下次调用会重新尝试加载——故障消除（补装模型/
    装上依赖）后同进程即可恢复，无需重启。
    """
    resolved = Path(model_dir) if model_dir is not None else default_model_dir(model_name)
    cache_key = str(resolved)
    if cache_key in _EMBEDDER_CACHE:
        return _EMBEDDER_CACHE[cache_key]

    # （并发分流）：check-then-load 全程持模块级单飞锁——并发请求/线程
    # 首次加载模型时只有第一个线程真加载，其余等待后命中缓存（避免重复加载与
    # _setup_determinism 的 os.environ 写入竞态）。
    # 修复（死锁）：`_setup_determinism` **先无锁调用**（它自带模块级
    # 单飞锁、check-then-set 全程持锁）再持 `_MODEL_LOCK`——修复前若锁内再调它，会对
    # 不可重入的 `threading.Lock` 二次 acquire 自锁死锁（run_web.py 冷启动 warm 路径
    # 实测：CPU 0、>8 分钟无响应）。单飞语义逐位不变：并发首载仍只有一个线程真加载。
    _setup_determinism()
    with _MODEL_LOCK:
        if cache_key in _EMBEDDER_CACHE:
            return _EMBEDDER_CACHE[cache_key]
        embedder: "Embedder | None" = None
        try:
            if not resolved.exists():
                _warn_once(
                    cache_key,
                    f"本地嵌入模型不存在：{resolved} —— 向量召回回退为规则顺序。"
                    "先安装 requirements-embeddings.txt 再运行 scripts/fetch_embedding_model.py。",
                )
            else:
                try:
                    # 惰性导入：仅当 dense 且模型目录存在时才碰 sentence-transformers/torch。
                    from sentence_transformers import SentenceTransformer
                except Exception:
                    _warn_once(
                        cache_key,
                        "未安装 sentence-transformers（可选依赖）—— 向量召回回退为规则顺序。"
                        "安装：pip install -r requirements-embeddings.txt。",
                    )
                    SentenceTransformer = None  # type: ignore[assignment]

                if SentenceTransformer is not None:
                    model = SentenceTransformer(str(resolved))

                    def _encode(texts: "Sequence[str]") -> "list[list[float]]":
                        vecs = model.encode(
                            list(texts),
                            normalize_embeddings=True,   # 归一化 → 余弦即点积
                            convert_to_numpy=True,
                            show_progress_bar=False,
                        )
                        return [[float(x) for x in v] for v in vecs]

                    embedder = _encode
        except Exception as exc:  # 任何意外都不外泄，一律降级
            _warn_once(cache_key, f"加载嵌入模型失败（{exc}）—— 向量召回回退为规则顺序。")
            embedder = None

        if embedder is not None:
            # 失败不缓存：None 落缓存 = 负向永久缓存，一次瞬时故障就须重启才恢复。
            _EMBEDDER_CACHE[cache_key] = embedder
        return embedder


def load_cross_encoder(
    model_dir: "str | Path | None" = None,
    model_name: str = DEFAULT_CROSS_ENCODER_MODEL,
) -> "CrossScorer | None":
    """惰性加载本地 cross-encoder 重排器；不可用时返回 None（触发回退，绝不下载、绝不抛错）。

    返回一个打分器 (pairs)->list[float]。缺依赖/缺模型/异常 → None（提示一次）。
    失败**不缓存**：下次调用会重试加载，故障消除后同进程即恢复，无需重启。
    """
    resolved = Path(model_dir) if model_dir is not None else default_cross_encoder_dir(model_name)
    cache_key = "ce::" + str(resolved)
    if cache_key in _CROSS_CACHE:
        return _CROSS_CACHE[cache_key]

    # （并发分流）：同 load_embedder，check-then-load 全程持单飞锁。
    # 修复（死锁）：`_setup_determinism` 先无锁调用（自带单飞锁），
    # 再持 `_MODEL_LOCK` 做 check-then-load——锁内不再有对 `_MODEL_LOCK` 的二次 acquire
    # （threading.Lock 不可重入，修复前此处锁内调 `_setup_determinism` 必自锁死锁）。
    _setup_determinism()
    with _MODEL_LOCK:
        if cache_key in _CROSS_CACHE:
            return _CROSS_CACHE[cache_key]
        scorer: "CrossScorer | None" = None
        try:
            if not resolved.exists():
                _warn_once(
                    cache_key,
                    f"本地重排模型不存在：{resolved} —— 向量召回(cross_encoder)回退为规则顺序。"
                    "先装 requirements-embeddings.txt 再运行 scripts/fetch_embedding_model.py --cross-encoder。",
                )
            else:
                try:
                    from sentence_transformers import CrossEncoder  # 惰性
                except Exception:
                    CrossEncoder = None  # type: ignore[assignment]

                if CrossEncoder is not None:
                    # local_files_only=True：即便模型目录不完整也**绝不联网**（对抗审计加固点，
                    # 杜绝加载期网络等待）；老版本无此参数则回退（外层 try 仍兜底）。
                    try:
                        model = CrossEncoder(str(resolved), max_length=512, local_files_only=True)
                    except TypeError:
                        model = CrossEncoder(str(resolved), max_length=512)  # 默认 、eval：可复现

                    def _score(pairs: "Sequence[tuple[str, str]]") -> "list[float]":
                        scores = model.predict(list(pairs), batch_size=32, show_progress_bar=False)
                        return [float(x) for x in scores]

                    scorer = _score
                else:
                    # frozen 在线组件住独立 venv，绝不把 torch/transformers 注入主进程；
                    # 通过常驻 JSONL worker 打分。source/portable 仍优先走上面的进程内路径。
                    try:
                        from .model_runtime import external_cross_scorer, external_runtime_ready
                        if external_runtime_ready():
                            scorer = external_cross_scorer()
                    except Exception:
                        scorer = None
                    if scorer is None:
                        _warn_once(cache_key, "未安装可用的本地模型运行组件—— cross_encoder 回退为规则顺序。")
        except Exception as exc:
            _warn_once(cache_key, f"加载重排模型失败（{exc}）—— cross_encoder 回退为规则顺序。")
            scorer = None

        if scorer is not None:
            # 失败不缓存：同 _EMBEDDER_CACHE 纪律，见上。
            _CROSS_CACHE[cache_key] = scorer
        return scorer


def recall_backend_ready(backend: str) -> bool:
    """该 recall 后端是否**已就绪**（模型已在缓存里），**不触发任何加载**。
    'off' / 未知 → True（无需模型）。供 MCP 服务器在 piped-stdio 下判断能否安全使用：
    未预热的 torch 后端若在请求内首次加载会死锁，调用方据此回退规则序。"""
    b = (backend or "off").strip().lower()
    if b == "cross_encoder":
        return _CROSS_CACHE.get("ce::" + str(default_cross_encoder_dir())) is not None
    if b == "dense":
        return _EMBEDDER_CACHE.get(str(default_model_dir())) is not None
    return True


def recall_backend_available(backend: str) -> bool:
    """该 recall 后端是否**可加载**（本地模型目录存在 + sentence-transformers 已装），**不触发加载**。

    与 `recall_backend_ready` 的区别：
    - `ready`  = 模型**已在缓存**里（piped-stdio 安全，MCP 用它——未预热的 torch 后端在请求内首次加载会死锁）。
    - `available` = 模型**能加载**（有真 TTY 的 CLI/Web 用它——首次加载有开销但不死锁）。
    'off' / 未知后端 → False（无需/无从加载语义模型）。仅做 find_spec + 目录存在性检查，无重导入。
    """
    b = (backend or "off").strip().lower()
    if b not in ("cross_encoder", "dense"):
        return False
    target = default_cross_encoder_dir() if b == "cross_encoder" else default_model_dir()
    if not target.exists():
        return False
    try:
        import importlib.util
        if importlib.util.find_spec("sentence_transformers") is not None:
            return True
    except Exception:
        pass
    if b == "cross_encoder":
        try:
            from .model_runtime import external_runtime_ready
            return external_runtime_ready()
        except Exception:
            return False
    return False


def warm_recall_backend(backend: str) -> bool:
    """在**主线程、启动期**预热指定 recall 后端的本地模型（加载进缓存），返回是否就绪。
    'off' / 未知 → True（无需预热）。必须在事件循环 / 请求处理**之外**调用
    （piped-stdio 下唯一不死锁的加载时机）。"""
    b = (backend or "off").strip().lower()
    if b == "cross_encoder":
        return load_cross_encoder() is not None
    if b == "dense":
        return load_embedder() is not None
    return True


def expand_query_bilingual(query: str, intent: object) -> str:
    """确定性双语扩展：中文原 query 后附受控词表解析出的英文 display 术语，
    **仅进重排打分文本，不回灌硬过滤**。帮 cross-encoder 对齐「中文查询 ↔ 英文文档」。"""
    dm = getattr(intent, "display_map", None) or {}
    terms: "list[str]" = []
    for dim in ("species", "tissue", "disease", "platform", "assay"):
        for t in dm.get(dim, []) or []:
            if t and t not in terms:
                terms.append(t)
    if getattr(intent, "has_raw_data_required", None) is True:
        terms.append("with raw FASTQ data")
    return f"{query} ({', '.join(terms)})" if terms else query


def _candidate_text(cand: RetrievedCandidate) -> str:
    """候选序列化为打分文本：题名 + **带标签**的结构化字段 + 截断描述（≤400）。
    该序列化格式（带标签字段 + desc≤400）是评测基准的输入形态。"""
    r = cand.record
    desc = (r.description or "").strip().replace("\n", " ")
    if len(desc) > 400:
        desc = desc[:400]
    return (f"{r.dataset_name} | 物种 {r.species} | 组织 {r.tissue} | 疾病 {r.disease} "
            f"| 平台 {r.platform_family} | 实验 {r.assay} | {desc}")


def _cosine(a: "list[float]", b: "list[float]") -> float:
    """余弦相似度；对未归一化/退化向量稳健（归一化输入时等价点积）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def _minmax_norm(values: "list[float]") -> "list[float]":
    """min-max 归一化到 [0,1]；全相等时返回全 0（不引入词面偏好，交给稠密分/稳定序）。"""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo <= 1e-12:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _rrf_fused_order(lex_scores: "list[float]", dense_scores: "list[float]", k: int = RRF_K) -> "list[int]":
    """RRF 融合（k=60）：`score(d) = Σ 1/(k + rank_i(d))`——只用两路名次、零尺度假设。

    名次并列按原下标稳定（与 linear 路的稳定序同口径）；返回按融合分降序的下标序列
    （同分按下标，确定性可复现）。"""
    n = len(lex_scores)
    lex_order = sorted(range(n), key=lambda i: (-float(lex_scores[i]), i))
    dense_order = sorted(range(n), key=lambda i: (-float(dense_scores[i]), i))
    lex_rank = [0] * n
    dense_rank = [0] * n
    for r, i in enumerate(lex_order):
        lex_rank[i] = r
    for r, i in enumerate(dense_order):
        dense_rank[i] = r
    fused = [1.0 / (k + lex_rank[i]) + 1.0 / (k + dense_rank[i]) for i in range(n)]
    return sorted(range(n), key=lambda i: (-fused[i], i))


def _dense_vectors_cached(
    enc: Embedder,
    model_dir: "str | Path | None",
    query: str,
    texts: "list[str]",
) -> "list[list[float]] | None":
    """默认加载路径的稠密向量组装：查询向量现编，文档向量查 _DOC_VECTOR_CACHE（缺失才编码）。

    返回 [查询向量, *文档向量]；**仅当**文档批编码输出长度不符时返回 None（对齐外层
    invalid_vectors「畸形输出→回退」语义）。本函数不吞任何异常——查询批为空/畸形、enc 抛错
    一律上抛，交给 recall_rerank 外层宽 except 兜底（既定「永不阻塞」合同）。
    字节等价红线：缓存命中返回的是**当初编码出的同一个 list 对象**（绝不重算），融合数学与
    _cosine 一行未动 → linear 融合输出逐位不变。
    """
    # 查询向量每次现编（见 _DOC_VECTOR_CACHE 声明处注释）。空返回会让下面 qvecs[0] 抛
    # IndexError，由外层 except 回退——刻意不在函数内自行吞掉（外层才有留痕语义）。
    qvecs = enc([query])
    # 键的模型目录口径与 load_embedder 同源（cache_key = str(resolved)，None → 默认目录）。
    cache_key_model = str(Path(model_dir) if model_dir is not None else default_model_dir())
    doc_vecs: "list[list[float] | None]" = []
    missing: "list[int]" = []
    with _DOC_VECTOR_LOCK:
        for t in texts:
            key = (cache_key_model, t)
            if key in _DOC_VECTOR_CACHE:
                _DOC_VECTOR_CACHE.move_to_end(key)                # LRU：命中即续命
                doc_vecs.append(_DOC_VECTOR_CACHE[key])
            else:
                doc_vecs.append(None)                             # 占位，待批量补编
                missing.append(len(doc_vecs) - 1)
    if missing:
        # 缺失文本一次性批量编码：保批处理语义，批次顺序 = 候选原序（编码器无需感知缺口）。
        fresh = enc([texts[i] for i in missing])
        if len(fresh) != len(missing):
            return None                                           # 长度不符 → 外层 invalid_vectors 回退
        with _DOC_VECTOR_LOCK:
            for pos, vec in zip(missing, fresh):
                doc_vecs[pos] = vec
                _DOC_VECTOR_CACHE[(cache_key_model, texts[pos])] = vec
                while len(_DOC_VECTOR_CACHE) > _DOC_VECTOR_CACHE_MAX:
                    _DOC_VECTOR_CACHE.popitem(last=False)         # 超上限逐出最旧（LRU）
    return [qvecs[0], *doc_vecs]


def recall_rerank(
    query: str,
    candidates: "Sequence[RetrievedCandidate]",
    backend: str = "off",
    alpha: float = DEFAULT_RECALL_ALPHA,
    embedder: "Embedder | None" = None,
    cross_scorer: "CrossScorer | None" = None,
    intent: object | None = None,
    model_dir: "str | Path | None" = None,
    top_k: "int | None" = None,
    trace: "dict | None" = None,
    fusion: str = DEFAULT_RECALL_FUSION,
) -> "list[RetrievedCandidate]":
    """可选向量召回（存活集内语义重排）。三种后端，输出恒为输入 candidates 的**排列/截断**。

    backend="off"（默认）    → 原样返回（截断到 top_k，如未传则不截）。
    backend="dense"          → 本地稠密嵌入余弦 × 词面分融合排序：`fusion="linear"`（默认）
                               为 min-max 归一化 + alpha 线性融合（历史行为字节等价）；
                               `fusion="rrf"` 为 RRF 名次融合（k=60，尺度无关、零调参）。
    backend="cross_encoder"  → 本地 cross-encoder 对全存活集逐对打分，按**纯分**排序（词面原序仅同分 tie-break）；
                               中文 query 附英文别名（双语扩展）以对齐英文文档。**默认路径**。

    任一后端：模型缺失/依赖未装/任何异常/畸形输出 → 回退传入顺序（永不报错、永不违规、永不阻塞）。
    embedder / cross_scorer 可注入（便于单测/评测共享一次加载）。intent 用于双语扩展（cross_encoder）。
    """
    items = list(candidates)
    started_at = time.perf_counter()

    def _duration_ms() -> int:
        return max(0, int(round((time.perf_counter() - started_at) * 1000)))

    if trace is not None:
        trace.update({
            "backend": backend, "status": "skipped", "reason": "disabled",
            "candidate_count": len(items), "duration_ms": 0,
            "fusion": (str(fusion or "").strip().lower() if backend == "dense" else ""),
        })
    if not items or backend == "off" or backend not in RECALL_BACKENDS:
        if trace is not None and not items:
            trace.update({"status": "skipped", "reason": "no_candidates", "duration_ms": _duration_ms()})
        return items[:top_k] if top_k is not None else items

    def _mark(status: str, reason: str) -> None:
        if trace is not None:
            trace.update({"status": status, "reason": reason, "duration_ms": _duration_ms()})

    def _cut(seq: "list[RetrievedCandidate]") -> "list[RetrievedCandidate]":
        return seq[:top_k] if top_k is not None else seq

    try:
        if backend == "cross_encoder":
            # API 数据源分支（方案A 智谱 rerank；env BIODATA_RERANK_API=zhipu 才启用，
            # 缺省 off → 本地形态逐字节不变）。注入 cross_scorer 的测试/评测路径不受影响。
            api_scores: "list[float] | None" = None
            if cross_scorer is None:
                from . import recall_api  # 惰性导入：env off 时零开销零副作用
                if recall_api.api_rerank_enabled():
                    q_api = expand_query_bilingual(query, intent) if intent is not None else query
                    api_scores = recall_api.api_rerank_scores(
                        q_api, [_candidate_text(c) for c in items]
                    )
                    if api_scores is None:
                        # fail-closed：API 失败/超时/限流 → 直接回退规则序，不阻塞主路
                        _mark("fallback", "api_unavailable")
                        return _cut(items)
            if api_scores is not None:
                scores = api_scores
            else:
                score_fn = cross_scorer if cross_scorer is not None else load_cross_encoder(model_dir)
                if score_fn is None:
                    _mark("fallback", "model_or_dependency_unavailable")
                    return _cut(items)                                  # 优雅降级
                q = expand_query_bilingual(query, intent) if intent is not None else query
                pairs = [(q, _candidate_text(c)) for c in items]
                scores = score_fn(pairs)
            if not scores or len(scores) != len(items) or not all(math.isfinite(float(s)) for s in scores):
                # 畸形输出（长度不符或含 NaN/Inf）→ 回退。NaN 会让 sorted 静默产出乱序而非报错，
                # 与本模块「畸形输出→回退」合同不符，故显式挡掉并留痕。
                _warn_once("ce_bad_scores", "cross_encoder 打分畸形（长度不符或含 NaN/Inf）—— 回退规则序。")
                _mark("fallback", "invalid_scores")
                return _cut(items)
            # 纯 cross-encoder 分排序（实现）：词面原序只作完全同分时的稳定 tie-breaker。
            order = sorted(range(len(items)), key=lambda i: (-float(scores[i]), i))
            _mark("used", "completed")
            return _cut([items[i] for i in order])

        # backend == "dense"
        # API 数据源分支（方案A：语料向量文件 + 查询侧智谱嵌入；env BIODATA_EMBED_API=zhipu
        # 才启用，缺省 off → 本地形态逐字节不变）。注入 embedder 的测试/评测路径不受影响。
        api_vectors: "list[list[float]] | None" = None
        if embedder is None:
            from . import recall_api  # 惰性导入：env off 时零开销零副作用
            if recall_api.api_embed_enabled():
                api_vectors = recall_api.api_dense_vectors(query, items)
                if api_vectors is None:
                    # fail-closed：文件缺失/版本不匹配/API 失败/限流 → 直接回退规则序
                    _mark("fallback", "api_unavailable")
                    return _cut(items)
        enc: "Embedder | None" = None
        if api_vectors is None:
            enc = embedder if embedder is not None else load_embedder(model_dir)
            if enc is None:
                _mark("fallback", "model_or_dependency_unavailable")
                return _cut(items)                                      # 优雅降级
        use_rrf = str(fusion or "").strip().lower() == "rrf"
        a = max(0.0, min(1.0, float(alpha)))
        if api_vectors is not None:
            vectors = api_vectors
        elif embedder is None:
            # P-1：默认加载路径走文档向量 LRU 缓存（候选文本与查询无关、语料静态 → 嵌入是
            # 最贵一步，按文本缓存）；查询向量仍每次现编。见 _DOC_VECTOR_CACHE 声明处注释。
            texts = [_candidate_text(c) for c in items]
            vectors = _dense_vectors_cached(enc, model_dir, query, texts)
        else:
            # 注入路径（单测/评测共享一次加载）：行为逐位不变——仍单批 enc([query, *texts])，
            # 不过缓存（隔离即确定性：不同测试对同一文本的 mock 向量互不串味）。
            texts = [_candidate_text(c) for c in items]
            vectors = enc([query, *texts])
        if not vectors or len(vectors) != len(items) + 1:          # 长度不符 → 回退，绝不错位打分
            _mark("fallback", "invalid_vectors")
            return _cut(items)
        qv = vectors[0]
        dense = [_cosine(qv, v) for v in vectors[1:]]
        if not all(math.isfinite(d) for d in dense):               # NaN/Inf 稠密分 → 回退
            _warn_once("dense_bad_scores", "dense 稠密分含 NaN/Inf —— 回退规则序。")
            _mark("fallback", "invalid_scores")
            return _cut(items)
        if use_rrf:
            # RRF 名次融合：只吃两路原始分（词面分不归一化——RRF 不看分值尺度）。
            lex_raw = [float(getattr(c, "score", 0.0) or 0.0) for c in items]
            if not all(math.isfinite(v) for v in lex_raw):
                _warn_once("dense_bad_scores", "dense 词面分含 NaN/Inf —— 回退规则序。")
                _mark("fallback", "invalid_scores")
                return _cut(items)
            order = _rrf_fused_order(lex_raw, dense)
            if trace is not None:
                trace["fusion"] = "rrf"
        else:
            lex = _minmax_norm([float(getattr(c, "score", 0.0) or 0.0) for c in items])
            blended = [(1.0 - a) * lex[i] + a * dense[i] for i in range(len(items))]
            if not all(math.isfinite(b) for b in blended):            # NaN/Inf 融合分 → 回退，绝不静默乱序
                _warn_once("dense_bad_scores", "dense 融合分含 NaN/Inf —— 回退规则序。")
                _mark("fallback", "invalid_scores")
                return _cut(items)
            order = sorted(range(len(items)), key=lambda i: (-blended[i], i))
        _mark("used", "completed")
        return _cut([items[i] for i in order])
    except Exception as exc:
        # 任何异常都回退传入顺序（永不阻塞），但**留痕**：否则注入的打分器有 bug 或 predict 偶发
        # 抛错时，向量召回会每请求静默变 no-op，运行期无从察觉（宽 except 亦会吞掉真 bug）。
        _warn_once(f"recall_exc::{backend}", f"向量召回({backend})运行期异常，回退规则序：{exc!r}")
        _mark("fallback", "runtime_error")
        return _cut(items)
