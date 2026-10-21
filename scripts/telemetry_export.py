#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""遥测落库数据 → 推荐评测 benchmark 原料导出管线。

消费 services/telemetry-receiver 的 ingest_packets（join ingest_packet_receipts 账本去重，
只处理已完整落库的主包），把自动上传的 usage / benchfb / mcp 数据还原成可进评测构造流程的原料。

产物（--out 目录，全部 UTF-8 JSONL / Markdown / HTML）：
  impressions.jsonl           检索曝光 {iid,tid,sid,profile,q,policy,items[{uid,pos,score}],ts}：
                              主来源 = usage kind:"imp" 事件与自带 iid 的 v2/v3 search 事件
                              （join_status:"ok"）；view 事件按 (install_id,sid,tid,iid) 补 seen/dwell_ms；
                              无 iid 的旧 search 事件合成 iid 并标 join_status:"legacy"
  interactions.jsonl          交互：open/dl/fav/cite/view 事件 {iid,tid,uid,pos,type,ts}。
                              自带 iid 的事件按 (install_id,sid,tid,iid) **跨包**精确 join
                              （激进上传后曝光与交互常分属不同包；uid 优先取事件自带值，pos 只作校验，
                              对不上计 pos_mismatch）；join 不上标 join_status:"orphan"（iid/tid/uid
                              仍按事件自带值透出）。无 iid 的旧事件走同 install 时间近邻法并标
                              join_status:"legacy_inferred"
  turns.jsonl                 benchfb 轮次（隐私遮蔽后全量；benchfb 只作 turn/label 来源，
                              不再制造 impression）
  explicit_labels.jsonl       显式标注双源合并：usage kind:"label" 事件
                              （{tid,recId,completion,reasons,useful_uids,useful_idx,comment,rev,
                              rating_fields}，同 (tid,recId) 多 rev 取最高 rev；rating_fields 记录
                              评分键是否真的出现过——回填候选时 provided 空值覆盖、缺键继承）优先；
                              benchfb 记录内嵌 rating（兼容旧
                              {stars,useful_idx,comment} 与新 {completion,reasons,useful_idx,comment}
                              两种形状）兜底——同键 (install_id,tid,recId) 两源都有时只留 label 事件行
                              （label_source 区分来源；recId 为 新增，老数据缺失时空串兜底、
                              与 benchfb 侧不匹配属预期降级）
  mcp_calls.jsonl             mcp_records 摊平（call_id 幂等键保留，其余遮蔽）
  benchmark_candidates.jsonl  benchmark 候选：字段兼容 scripts/benchfb_ingest.py 的 candidates，
                              并加 tid/policy；iid 仅按 (install_id,tid) 精确关联已有曝光回填
  agent_trajectories.jsonl    由现有 benchfb route/action/search 真轨迹生成的有界步骤序列；带
                              prompt/experiment/policy/model/outcome/training_consent，不扩采客户端字段
  quality_report.md           关联完整率 / 可标注率 / 重复率 / 上传延迟分布 / schema 漂移 / 分布统计
  review.html                 人工审阅页（风格沿 benchfb_ingest.py：可按 kind/rating 过滤看原话与结果摘要）
  quarantine.jsonl            疑似敏感或无法处理的记录（只进这里，不进正常产物）

隐私（字符串值正则遮蔽）：
  手机号 1[3-9]\\d{9} → [手机号]；身份证 \\d{17}[\\dXx] → [证件号]；邮箱 → [邮箱]。
  记录内出现证件号、或任一字符串值整体即手机号 → 整条记录进 quarantine（疑似敏感）。

用法：
  python scripts/telemetry_export.py --dsn <dsn> --out 输出目录 \
      [--since ISO] [--until ISO] [--incremental] [--accepted ids.txt]
  --dsn          PG 连接串（postgresql+psycopg2://...）或 SQLite 文件路径；也可读 env BIODATA_TELEMETRY_DSN。
  --since/--until 按 received_at 过滤（ISO 时间串）。
  --incremental  状态文件记录 watermark（最大已见 ingest_packets.id，信息性）；幂等靠产物
                 行键 merge——join 正确性要求**全量扫描**所有包建曝光索引（激进上传后曝光与
                 交互常分属不同包），不再按 watermark 过滤包；重复跑不重复产出。
  --accepted     ids.txt 人工接受名单（每行一个记录 id，# 开头为注释）——只把名单内候选写进
                 benchmark_candidates.final.jsonl。

设计纪律：只读不写数据库；坏数据不炸批（记 quarantine/报告继续）；纯标准库 + SQLAlchemy。
known keys 集合仅用于 schema 漂移统计，不含未出现在集合里的键的丢弃语义——未知键照常进入遮蔽后产物。

表结构（镜像自 services/telemetry-receiver/app.py 与 telemetry_idempotency.py，只读消费）。
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, Table, Text, create_engine, func, select
from sqlalchemy.types import JSON

SCHEMA = "biodata-telemetry/1"
STATE_FILE = ".telemetry_export_state.json"
FINAL_CANDIDATES = "benchmark_candidates.final.jsonl"

# --- 表结构（只读镜像接收端；列类型只需在 SQLite/PG 两侧都可读）---
metadata = MetaData()

ingest_packets = Table(
    "ingest_packets",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("received_at", DateTime(timezone=True)),
    Column("install_id", Text),
    Column("schema", Text),
    Column("ua", Text),
    Column("cache_generation", Text),
    Column("n_usage", Integer),
    Column("n_benchfb", Integer),
    Column("payload", JSON),
)

ingest_packet_receipts = Table(
    "ingest_packet_receipts",
    metadata,
    Column("packet_id", Text, primary_key=True),
    Column("received_at", DateTime(timezone=True)),
    Column("identity", Text),
    Column("row_id", BigInteger),
)

# --- 隐私遮蔽 ---
# 规则顺序即优先级：身份证最先（身份证串内常含手机号式数字子串，先替换长规则才不会互相吞）；
# 手机号其次（整体即手机号的字符串单独判定为疑似敏感）；邮箱最后。
# 追加 API Key 形态（sk-…/AKIA…/Bearer token/ghp_… → [API Key]，与接收端 app.py
# _API_KEY_MASK_RULES、客户端 usage_core.js _MASK_PATTERNS 逐字同源）——feedback 已由接收端
# 遮蔽，本层是导出物化的防御纵深（旧数据/直插库兜底）。
_MASK_RULES = (
    (re.compile(r"\d{17}[\dXx]"), "idcard", "[证件号]"),
    (re.compile(r"1[3-9]\d{9}"), "phone", "[手机号]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "email", "[邮箱]"),
    (re.compile(r"(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}(?=[^A-Za-z0-9_-]|$)"), "api_key", "[API Key]"),
    (re.compile(r"(^|[^A-Za-z0-9])AKIA[0-9A-Z]{16}(?=[^A-Za-z0-9]|$)"), "api_key", "[API Key]"),
    (re.compile(r"(^|[^A-Za-z0-9])(?:[Bb]earer)\s+[A-Za-z0-9._~+/=-]{20,}"), "api_key", "[API Key]"),
    (re.compile(r"(^|[^A-Za-z0-9])ghp_[A-Za-z0-9]{36}(?=[^A-Za-z0-9]|$)"), "api_key", "[API Key]"),
)


def mask_value(v: Any) -> tuple[Any, set[str]]:
    """递归遮蔽字符串值；返回 (遮蔽后的值, 命中的类别集合)。

    带前导边界捕获组的规则（API Key 形态）用 lambda 保留下文再拼回——与接收端
    app.py / 客户端 usage_core.js 同一写法（rx.sub 字符串 repl 会把捕获组一并吞掉，
    导致「key 是 sk-…」丢空格变成「key 是[API Key]」）。"""
    if isinstance(v, str):
        hits: set[str] = set()
        out = v
        for rx, cat, repl in _MASK_RULES:
            if rx.search(out):
                hits.add(cat)
                out = rx.sub(lambda m: (m.group(1) if m.lastindex else "") + repl, out)
        return out, hits
    if isinstance(v, dict):
        out: dict[str, Any] = {}
        hits = set()
        for k, val in v.items():
            mv, mh = mask_value(val)
            out[k] = mv
            hits |= mh
        return out, hits
    if isinstance(v, list):
        out = []
        hits = set()
        for it in v:
            mv, mh = mask_value(it)
            out.append(mv)
            hits |= mh
        return out, hits
    return v, set()


def _suspicious_phone_only(value: str) -> bool:
    """字符串整体就是手机号（无其他内容）→ 疑似敏感。"""
    return bool(re.fullmatch(r"1[3-9]\d{9}", value.strip()))


def classify_record(record: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, set[str]]:
    """返回 (遮蔽后的记录, quarantine 原因或 None, 命中类别)。

    证件号出现在记录任意位置，或任一字符串值整体即手机号 → quarantine（不进正常产物）；
    其余（含普通文本里夹手机号/邮箱）→ 遮蔽后放行。
    """
    masked, hits = mask_value(record)
    if "idcard" in hits:
        return masked, "idcard", hits
    for v in _walk_strings(record):
        if _suspicious_phone_only(v):
            return masked, "phone-only", hits
    return masked, None, hits


def _walk_strings(node: Any) -> Iterable[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for val in node.values():
            yield from _walk_strings(val)
    elif isinstance(node, list):
        for it in node:
            yield from _walk_strings(it)


# --- 解析辅助 ---


def _imp_id(install_id: str, event_id: Any) -> str:
    return "imp-" + hashlib.sha256(f"{install_id}|{event_id}".encode("utf-8")).hexdigest()[:16]


def _dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)) and v:  # 客户端 ms epoch
        return datetime.fromtimestamp(float(v) / 1000.0, tz=timezone.utc)
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _ts_ms(v: Any) -> Any:
    """客户端时间戳原样透出（usage/benchfb 的 t 是 ms epoch）；缺失给 null。"""
    if isinstance(v, (int, float)) and v:
        return int(v)
    return None


# --- 已知键集合（仅用于 schema 漂移统计；未知键不丢弃，照常遮蔽进产物）---
KNOWN_PACKET_KEYS = {"schema", "packet_id", "install_id", "client_id", "profile_id",
                     "exported_at", "app", "usage_events", "benchfb_records", "mcp_records",
                     "feedback_records", "contract_version", "prompt_version", "experiment_id",
                     "experiment_arm", "propensity", "training_consent", "drop_report"}
KNOWN_USAGE_KEYS = {"event_id", "t", "k", "q", "n", "shown", "abstain", "unused", "cached", "ms",
                    "what", "r", "d", "mode", "step", "ok", "why", "msg",
                    # schema v2 中央注入与 v3 自描述事件（imp/view/label/交互自带关联键）
                    "sid", "tid", "iid", "policy", "items", "pos", "uid",
                    "seen", "dwell_ms", "rev", "recId", "completion", "reasons", "useful_uids", "useful_idx", "comment",
                    "contract_version", "prompt_version", "experiment_id", "experiment_arm", "propensity"}
KNOWN_BENCHFB_KEYS = {"id", "t", "kind", "q", "route", "search", "rating", "action", "env", "ms", "err",
                      # 当前客户端的轮次关联/来源/时长/上传压缩字段；这些字段会进入 turns/candidates，
                      # 不是 schema 漂移。policy* 为向后兼容的直接策略载体。
                      "tid", "iid", "src", "conv", "end", "route_ms", "truncated",
                      "policy", "policy_id", "policy_id_str",
                      "contract_version", "prompt_version", "experiment_id", "experiment_arm", "propensity",
                      # 任务卡/chip 生成文本提交轮次标记（bool）
                      "template_originated"}
KNOWN_MCP_KEYS = {"call_id", "t", "ts", "tool", "step", "args", "result", "ok", "error", "ms", "session",
                  "model", "provider", "legacy", "contract_version", "prompt_version", "experiment_id",
                  "experiment_arm", "propensity"}
KNOWN_FEEDBACK_KEYS = {"feedback_id", "identity", "with_diag", "authorized_at", "text", "diag"}
KNOWN_APP_KEYS = {"cache_generation", "ua", "lang"}

INTERACTION_KINDS = {"open", "dl", "fav"}
SEARCH_KIND = "search"
IMP_KIND = "imp"      # v3 曝光事件：{iid,tid,policy,items}（impressions 主来源）
VIEW_KIND = "view"    # 曝光追踪事件：{tid,iid,seen,dwell_ms}（补到 impression 上）
LABEL_KIND = "label"  # v3 显式标注事件：{tid,completion,reasons,useful_uids,useful_idx,comment,rev}
_RATING_OLD_KEYS = ("stars", "useful_idx", "comment")
_RATING_NEW_KEYS = ("completion", "reasons")
#: 评分字段全集：label 行合并按「字段是否提供」判定（provided 空值覆盖、缺键继承）。
_RATING_FIELD_KEYS = ("stars", "useful_idx", "useful_uids", "completion", "reasons", "comment")
POLICY_UNKNOWN = "policy_unknown"


def _provided_rating_fields(source: dict[str, Any]) -> list[str]:
    """标注来源里**真实出现过**的评分键（不是值非空）——presence 决定覆盖，值决定内容。"""
    return [key for key in _RATING_FIELD_KEYS if key in source]


def interaction_type(event: dict[str, Any]) -> str | None:
    """usage 事件 → 交互类型：open(去原站)→open、open{what:files}(看文件)→view、
    dl{what:cite}→cite、dl(其余)→dl、fav→fav；非交互事件返回 None。"""
    k = event.get("k")
    what = event.get("what")
    if k == "open":
        return "view" if what == "files" else "open"
    if k == "dl":
        return "cite" if what == "cite" else "dl"
    if k == "fav":
        return "fav"
    return None


def _rating_shape(rating: dict[str, Any]) -> str:
    """按锚点键判定评分形状：completion/reasons → new；stars → old；两者都有 → hybrid。"""
    has_new = any(k in rating for k in ("completion", "reasons"))
    has_stars = "stars" in rating
    if has_new and has_stars:
        return "hybrid"
    if has_new:
        return "new"
    if has_stars or any(k in rating for k in ("useful_idx", "comment")):
        return "old"
    return "empty"


def _rating_nonempty(rating: dict[str, Any]) -> bool:
    return any(rating.get(k) not in (None, "", []) for k in _RATING_OLD_KEYS + _RATING_NEW_KEYS)


def _norm_items(raw: Any) -> list[dict[str, Any]]:
    """事件自带 items → 规范 [{uid,pos,score}]；缺 pos 按数组序补（1-based），坏元素跳过。"""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for i, it in enumerate(raw):
        if not isinstance(it, dict):
            continue
        pos = it.get("pos")
        if not isinstance(pos, (int, float)) or pos <= 0:
            pos = i + 1
        score = it.get("score")
        out.append({"uid": str(it.get("uid") or ""), "pos": int(pos),
                    "score": float(score) if isinstance(score, (int, float)) else None})
    return out


def _norm_int_list(raw: Any) -> list[int]:
    out: list[int] = []
    if not isinstance(raw, list):
        return out
    for x in raw:
        if isinstance(x, (int, float)) and int(x) > 0:
            out.append(int(x))
        elif isinstance(x, str) and x.isdigit() and int(x) > 0:
            out.append(int(x))
    return out


def _as_dict(value: Any) -> dict[str, Any]:
    """把不可信的嵌套字段收窄为对象；旧版/畸形标量不能拖垮整批导出。"""
    return value if isinstance(value, dict) else {}


def _normalise_policy(value: Any) -> str | None:
    """把策略身份收窄为可审计字符串，绝不把 JS 对象隐式压成 ``[object Object]``。

    已落库的历史包不能删除；其中的污染值统一降级为 ``policy_unknown``。新服务端会下发
    ``policy_id_str``，旧的结构化 ``policy_id`` 则没有安全的跨语言规范串，宁可降级也不伪造。
    """
    if not isinstance(value, str):
        return POLICY_UNKNOWN if isinstance(value, dict) else None
    value = value.strip()
    if not value:
        return None
    return POLICY_UNKNOWN if value == "[object Object]" else value


def _first_policy(*values: Any) -> str | None:
    """优先返回真实策略；只有没有真实值时才保留 unknown 降级信号。"""
    unknown = False
    for value in values:
        policy = _normalise_policy(value)
        if policy and policy != POLICY_UNKNOWN:
            return policy
        unknown = unknown or policy == POLICY_UNKNOWN
    return POLICY_UNKNOWN if unknown else None


def _norm_str_list(raw: Any) -> list[str]:
    return [str(value) for value in raw if isinstance(value, str)] if isinstance(raw, list) else []


def _record_policy(rec: dict[str, Any], route: dict[str, Any], search: dict[str, Any], res: dict[str, Any]) -> str | None:
    """从 benchfb 记录里取真实策略身份（新串优先，旧对象安全降级）。"""
    return _first_policy(
        rec.get("policy_id_str"), rec.get("policy"), rec.get("policy_id"),
        search.get("policy_id_str"), search.get("policy"),
        res.get("policy_id_str"), res.get("policy"), res.get("policy_id"),
        route.get("policy_id_str"), route.get("policy"), route.get("policy_id"),
    )


def _resolve_useful_uids(rec: dict[str, Any]) -> list[str]:
    """rating.useful_idx（1-based 名次）→ 检索结果 dataset_uid 列表（对齐前端 benchfbResolveUseful）。

    截断降级记录（前端 `_shrinkBenchfbRecord` 形态）把截断点之外的标注条目浅拷贝加
    `_preserved_idx:<原名次>` 追加在尾部；这里按原名次优先命中保留条目，再退回压缩后的
    位置下标。否则第 23 条这类明确正例解析成空，冻结器会把它当负例（grade 0）。
    """
    rating = _as_dict(rec.get("rating"))
    idxs = [int(x) for x in (rating.get("useful_idx") or []) if str(x).isdigit()]
    search = _as_dict(rec.get("search"))
    results = _as_dict(search.get("res")).get("results") or []
    positional: dict[int, str] = {}
    preserved: dict[int, str] = {}
    for position, item in enumerate(results, 1):
        if not isinstance(item, dict):
            continue
        uid = str(item.get("dataset_uid") or "")
        positional.setdefault(position, uid)
        raw = item.get("_preserved_idx")
        if isinstance(raw, int) and not isinstance(raw, bool):
            preserved.setdefault(raw, uid)
    out: list[str] = []
    for i in idxs:
        uid = preserved.get(i) or positional.get(i) or ""
        if uid:
            out.append(uid)
    return out


def _candidate(rec: dict[str, Any], *, install_id: str, packet_id: str,
               packet_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """一条 benchfb 记录 → 一行 benchmark 候选（字段兼容 scripts/benchfb_ingest.py::_candidate，加 tid/policy）。

     起 benchfb 不再制造 impression，也不再从记录 id 合成 iid——候选的 iid
    只能在全量曝光索引建完后按 (install_id,tid) 精确回填；
     起与 turns/labels 的关联键 tid = 记录内 tid 字段（轮次 id，与 usage/label 事件同语义），
    原记录 id 保留在 record_id 字段；老数据记录无 tid 字段时 tid 为 null（--accepted 名单仍可命中
    record_id）。
    """
    rating = _as_dict(rec.get("rating"))
    route = _as_dict(rec.get("route"))
    search = _as_dict(rec.get("search"))
    res = _as_dict(search.get("res"))
    env = _as_dict(rec.get("env"))
    req = _as_dict(search.get("req"))
    action = _as_dict(rec.get("action"))
    kind = rec.get("kind")
    packet_context = packet_context or {}
    return {
        "record_id": rec.get("id"),
        "install_id": install_id,
        "packet_id": packet_id,
        "contract_version": rec.get("contract_version") or packet_context.get("contract_version") or 1,
        "prompt_version": rec.get("prompt_version") or packet_context.get("prompt_version"),
        "experiment_id": rec.get("experiment_id") or packet_context.get("experiment_id"),
        "experiment_arm": rec.get("experiment_arm") or packet_context.get("experiment_arm"),
        "propensity": rec.get("propensity") if rec.get("propensity") is not None else packet_context.get("propensity"),
        # 训练授权只认严格 bool true；历史缺字段与 truthy 字符串一律 false。
        "training_consent": packet_context.get("training_consent") is True,
        "ts": rec.get("t"),
        "kind": kind,
        "query": rec.get("q"),
        "effective_query": route.get("query") or req.get("query") or "",
        "route": route.get("route") or "",
        "via": route.get("via") or "",
        "resolution_status": res.get("resolution_status") or "",
        "result_total": res.get("result_total"),
        "system_topk_uids": [str(it.get("dataset_uid") or "") for it in (res.get("results") or []) if isinstance(it, dict)],
        "rating": {
            "stars": rating.get("stars"),
            "completion": rating.get("completion"),
            "reasons": _norm_str_list(rating.get("reasons")),
            "useful_uids": _resolve_useful_uids(rec),
            "useful_idx": rating.get("useful_idx") or [],
            "comment": rating.get("comment") or "",
        },
        "action_verb": action.get("verb") or "",
        "action_cancelled": bool(action.get("cancelled")),
        "timing_ms": {"route": rec.get("route_ms") or 0, "search": search.get("ms") or 0},
        "cached": bool(search.get("cached")),
        "env": {"model": env.get("model") or "", "provider": env.get("provider") or "", "endpoint_host": env.get("endpoint_host") or ""},
        "error": rec.get("err") or "",
        # 新字段：与 turns 的关联键；iid 不再从 benchfb 合成。
        # tid 改取记录内轮次 id（与 turns/labels 的 tid 同语义），record_id 仍是记录 id。
        "tid": rec.get("tid") or None,
        "iid": None,
        # route 是路由分类，不是排序/召回策略。真实策略先从记录自身找；跨包精确曝光回填在
        # _backfill_candidate_context 完成（此时完整 usage 索引才已建立）。
        "policy": _record_policy(rec, route, search, res),
        # 模板轮次标记随候选携带（生成时默认已排除
        # template_originated=true 的轮次；加字段保证名单/事后审计仍可分辨）。
        "template_originated": rec.get("template_originated"),
    }


def _trace_step(step: Any, *, source: str, index: int) -> dict[str, Any] | None:
    """把真实 agent/search/action step 规范成训练可用且有界的记录。

    小 step 保留完整的已遮蔽结构；超过 16k 字符时只留稳定摘要和完整内容摘要，避免一个工具
    observation 撑爆分析产物。这里接收的是 ``classify_record`` 后的数据，不会绕过 PII 遮蔽。
    """
    if not isinstance(step, dict):
        return None
    canonical = json.dumps(step, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    common = {
        key: step.get(key) for key in (
            "id", "verb", "verb_zh", "tool", "status", "state", "ok", "error_code",
            "ms", "duration_ms", "attempt", "cached",
        ) if key in step
    }
    row: dict[str, Any] = {
        "source": source, "index": index, "sha256": digest,
        "payload_chars": len(canonical), **common,
    }
    if len(canonical) <= 16_000:
        row["payload"] = step
        row["truncated"] = False
    else:
        row["payload"] = None
        row["truncated"] = True
    return row


def _agent_trajectory(rec: dict[str, Any], *, install_id: str, packet_id: str,
                      packet_context: dict[str, Any]) -> dict[str, Any]:
    """从现有 benchfb 真轨迹生成 agent/model 评测行，不增加客户端采集面。"""
    route = _as_dict(rec.get("route"))
    plan = _as_dict(route.get("plan"))
    agent = _as_dict(route.get("agent"))
    action = _as_dict(rec.get("action"))
    search = _as_dict(rec.get("search"))
    response = _as_dict(search.get("res"))
    search_trace = _as_dict(response.get("search_trace"))
    sources = (
        ("route.plan.trace", plan.get("trace")),
        ("route.plan.steps", plan.get("steps")),
        ("route.agent.trace", agent.get("trace")),
        ("action.trace", action.get("trace")),
        ("search.trace", search_trace.get("steps")),
    )
    steps: list[dict[str, Any]] = []
    for source, raw_steps in sources:
        if not isinstance(raw_steps, list):
            continue
        for index, step in enumerate(raw_steps):
            compact = _trace_step(step, source=source, index=index)
            if compact is not None:
                steps.append(compact)
    rating = _as_dict(rec.get("rating"))
    env = _as_dict(rec.get("env"))
    material = f"{install_id}\x1f{packet_id}\x1f{rec.get('id') or ''}"
    return {
        "trajectory_id": "traj-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24],
        "record_id": rec.get("id"), "tid": rec.get("tid") or None, "iid": rec.get("iid") or None,
        "install_id": install_id, "packet_id": packet_id, "ts": rec.get("t"),
        "contract_version": rec.get("contract_version") or packet_context.get("contract_version") or 1,
        "prompt_version": rec.get("prompt_version") or packet_context.get("prompt_version"),
        "experiment_id": rec.get("experiment_id") or packet_context.get("experiment_id"),
        "experiment_arm": rec.get("experiment_arm") or packet_context.get("experiment_arm"),
        "propensity": rec.get("propensity") if rec.get("propensity") is not None else packet_context.get("propensity"),
        "training_consent": packet_context.get("training_consent") is True,
        "training_eligible": packet_context.get("training_consent") is True and _rating_nonempty(rating),
        "query": rec.get("q"), "kind": rec.get("kind"),
        "route": route.get("route") or "", "via": route.get("via") or "",
        "policy": _record_policy(rec, route, search, response),
        "model": env.get("model") or "", "provider": env.get("provider") or "",
        "outcome": {
            "completion": rating.get("completion"), "reasons": _norm_str_list(rating.get("reasons")),
            "stars": rating.get("stars"), "action_cancelled": bool(action.get("cancelled")),
            "error": rec.get("err") or "",
        },
        "steps": steps, "step_count": len(steps),
    }


def _merged_label_rating(label: dict[str, Any]) -> dict[str, Any]:
    """label 行（事件或 benchfb 兜底）→ 候选 rating 字段（键与 _candidate 产出保持一致）。"""
    return {
        "stars": label.get("stars"),
        "completion": label.get("completion"),
        "reasons": _norm_str_list(label.get("reasons")),
        "useful_uids": [str(u) for u in (label.get("useful_uids") or []) if u],
        "useful_idx": _norm_int_list(label.get("useful_idx")),
        "comment": label.get("comment") or "",
    }


def _merge_label_into_rating(base: dict[str, Any], label: dict[str, Any]) -> dict[str, Any]:
    """label 行并入候选 rating：**按字段是否提供**合并——provided 空值覆盖、缺键才继承。

    改口常只带变化的那部分（真 missing 键继承，兼容只发 stars/useful_idx/comment 的旧形状），
    但完整快照会用显式空值清空（rev2 的 useful_uids=[]/comment=''）：若按值过滤空值，显式
    清空会被丢弃、旧 grade2 正例残留，冻结器把已撤销的标注继续当正例。
    label 行没有 rating_fields（旧版产物 / 老数据）时退回旧语义：非空覆盖、缺省保留；
    此时整条字段全空仍视为高 rev 明确清空 → 候选 rating 归空（冻结器按"无人类标注"排除）。
    """
    normalized = _merged_label_rating(label)
    provided = label.get("rating_fields")
    if not isinstance(provided, list):
        overlay = {k: v for k, v in normalized.items() if v not in (None, "", [])}
        if not overlay:
            return normalized
        merged = dict(base) if isinstance(base, dict) else {}
        merged.update(overlay)
        return merged
    if not provided:
        return normalized                       # 显式空快照 / 无评分字段：归空
    merged = dict(base) if isinstance(base, dict) else {}
    for key, value in normalized.items():
        merged.setdefault(key, value)
    for key in provided:
        if key in normalized:
            merged[key] = normalized[key]
    return merged


#: 字段 rev 下限哨兵：从未提供（低于 benchfb 兜底的 -1 与事件的 ≥0）。
_FIELD_REV_ABSENT = -2


def _label_field_rev(row: dict[str, Any], field: str) -> int:
    """单字段的修订号：rating_field_revs 记录为准；legacy 行（无该键）保守用
    rating_fields + 行 rev——只认已知提供的字段，不猜未提供的，也不丢历史。"""
    frevs = row.get("rating_field_revs")
    if isinstance(frevs, dict):
        rev = frevs.get(field)
        if isinstance(rev, int) and not isinstance(rev, bool):
            return rev
        return _FIELD_REV_ABSENT
    provided = row.get("rating_fields")
    if isinstance(provided, list):
        return _row_rev(row, "rev") if field in provided else _FIELD_REV_ABSENT
    return _FIELD_REV_ABSENT


def _valid_src_pos(pos: Any) -> list[int] | None:
    """接收序来源的合法形状 [packet 行 id, 包内数组位置]；不符一律 None（未知）。"""
    if (isinstance(pos, list) and len(pos) == 2
            and all(isinstance(x, int) and not isinstance(x, bool) for x in pos)
            and pos[0] > 0 and pos[1] >= 0):
        return pos
    return None


def _label_field_src(row: dict[str, Any], field: str) -> list[int] | None:
    """单字段的接收序来源：逐字段记录（rating_field_srcs）为准；无逐字段记录时退回行级
    rev_src（扫描来源行的全部字段同源）；旧产物没有此信息 → None（未知，不参与并列反转）。
    绝不从行 packet_id/ts/最高 rev 元信息猜字段来源。"""
    srcs = row.get("rating_field_srcs")
    if isinstance(srcs, dict):
        return _valid_src_pos(srcs.get(field))
    return _valid_src_pos(row.get("rev_src"))


def _accumulate_label_fields(acc: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """把一条标注来源（label 事件 / benchfb 内嵌兜底 / 旧产物种子行）按字段并入累积行——
    唯一的字段级修订累积机制（扫描合包、双源合并、窗口增量种子三处共用，不平行第二路）。

    每字段独立取最高 rev：更高 rev 覆盖（显式 None/[]/'' 同样覆盖——那是真撤销），低 rev
    迟到不复活。**同 rev 的「先到先留」以 DB 原始接收顺序决胜**：逐字段来源
    (ingest_packets 行 id, 事件在包内数组的位置) 更小者胜——判据与并入顺序无关，正常
    分窗/重叠旧窗/反向窗口与全库扫描（接收序上的折叠）终态一致；同一事件经重叠窗口
    重扫时来源相等，自然幂等。**来源未知的并列没有反转权**：旧产物行（legacy，无
    rating_field_srcs/rev_src）与现值任一侧来源缺失时一律保留现值——只有严格更高 rev
    或双方可信的更早来源才能改旧值，绝不从行 packet_id/ts/最高 rev 元信息猜字段来源；
    legacy 无法推出全局接收序，只能保守保存已知，确定性重建靠全库重扫。行级 rev 只记
    见过的最高 rev（元信息与 upsert 排序键），绝不因此抹掉低 rev 提供的有效字段。
    rating_fields=[] 是显式空快照：清空全部字段并以本 rev 钉住全部字段 rev（低 rev
    迟到不得复活）。benchfb 兜底（rev None→-1）只补缺、不冒充事件 provided：累积行的
    rating_fields 只含事件来源字段（rev≥0）；一个事件都没有时才退回 benchfb 的提供集
    （legacy 兼容）。更老的行连 rating_fields 都没有：只按非空值保守判 provided（与
    _merge_label_into_rating 的 legacy 口径一致），显式空值信息已不可考。
    持久化只带每字段 (rev, 来源) 与行级 (rev, 来源) 有限元信息，不保留事件历史。"""
    inc_rev = _row_rev(incoming, "rev")
    raw_provided = incoming.get("rating_fields")
    values = _merged_label_rating(incoming)
    if isinstance(raw_provided, list):
        provided = [f for f in raw_provided if f in _RATING_FIELD_KEYS]
        whole_clear = not provided and inc_rev >= 0
    else:
        provided = [f for f in _RATING_FIELD_KEYS if values[f] not in (None, "", [])]
        whole_clear = False
    if acc is None:
        acc = dict(incoming)
        acc["rating_field_revs"] = {}
        acc["rating_field_srcs"] = {}
    else:
        acc = dict(acc)
        acc["rating_field_revs"] = dict(acc.get("rating_field_revs") or {})
        acc["rating_field_srcs"] = dict(acc.get("rating_field_srcs") or {})
    acc_rev = _row_rev(acc, "rev")
    frevs = acc["rating_field_revs"]
    srcs = acc["rating_field_srcs"]
    # 并入范围 = incoming 有 rev 信息的全部字段：扫描事件 = 其 rating_fields；旧产物种子 =
    # 其 rating_field_revs（含 benchfb 兜底的 -1 字段——它们不在 rating_fields 里，但种子
    # 合并必须带上，否则窗口链会丢掉 benchfb 兜底值，与全库扫描分叉）。
    if whole_clear:
        targets = list(_RATING_FIELD_KEYS)
    elif isinstance(incoming.get("rating_field_revs"), dict):
        targets = [f for f in _RATING_FIELD_KEYS if f in incoming["rating_field_revs"]]
    else:
        targets = provided
    for field in targets:
        cur = frevs.get(field, _label_field_rev(acc, field))
        cur_src = srcs.get(field)
        new = inc_rev if whole_clear else _label_field_rev(incoming, field)
        if new == _FIELD_REV_ABSENT:
            new = inc_rev                     # legacy-overlay 行：字段在提供集内，行 rev 即字段 rev
        new_src = _label_field_src(incoming, field)
        if (new > cur
                or (new == cur and cur != _FIELD_REV_ABSENT
                    and cur_src is not None and new_src is not None and new_src < cur_src)):
            acc[field] = values[field]
            frevs[field] = new
            if new_src is None:
                srcs.pop(field, None)         # 更高 rev 但来源未知：只记 rev，后续并列不再反转
            else:
                srcs[field] = new_src
    # 行级 rev/meta 同判据：更高 rev 胜；同 rev 且双方来源已知取更早接收序；来源未知不反转。
    inc_rsrc = _valid_src_pos(incoming.get("rev_src"))
    acc_rsrc = _valid_src_pos(acc.get("rev_src"))
    if (inc_rev > acc_rev
            or (inc_rev == acc_rev and acc_rsrc is not None and inc_rsrc is not None
                and inc_rsrc < acc_rsrc)):
        acc["rev"] = incoming.get("rev")
        if inc_rsrc is None:
            acc.pop("rev_src", None)
        else:
            acc["rev_src"] = inc_rsrc
        for meta in ("tid", "recId", "iid", "install_id", "packet_id", "event_id",
                     "kind", "q", "ts", "label_source"):
            if meta in incoming:
                acc[meta] = incoming.get(meta)
    # frevs/srcs 也按评分字段全集定序：字段插入路径（事件先到/种子先到）不影响产物字节。
    acc["rating_field_revs"] = frevs = {f: frevs[f] for f in _RATING_FIELD_KEYS if f in frevs}
    acc["rating_field_srcs"] = {f: srcs[f] for f in _RATING_FIELD_KEYS if f in srcs}
    event_fields = [f for f in _RATING_FIELD_KEYS if frevs.get(f, _FIELD_REV_ABSENT) >= 0]
    if event_fields:
        acc["rating_fields"] = event_fields
    else:
        acc["rating_fields"] = [f for f in _RATING_FIELD_KEYS if frevs.get(f, _FIELD_REV_ABSENT) == -1]
    acc["label_shape"] = _rating_shape(
        {k: v for k, v in ((key, acc.get(key)) for key in
                           ("completion", "reasons", "stars", "useful_idx", "comment"))
         if v is not None})
    # 规范化键序：benchfb 先行 / 事件先行 / 旧产物种子三条路径累积出的行必须逐字节一致
    # （窗口增量与全库扫描同结果、重跑幂等都依赖字节级稳定）。
    ordered = {k: acc[k] for k in _LABEL_ROW_KEY_ORDER if k in acc}
    for k in acc:
        if k not in ordered:
            ordered[k] = acc[k]
    return ordered


def _label_merge_key(install_id: str, tid: str, packet_id: str, event_id: str,
                     ts: Any, rec_id: str) -> tuple[str, str, str]:
    """显式标签合并键 (install_id, tid, recId) 唯一真源：扫描合包、旧产物种子、
    写出去重三处同键（tid 缺失的老数据退 packet:event|ts 兜底，两侧一致才不串键）。"""
    return (install_id, tid or f"{packet_id}:{event_id or ts}", rec_id)


#: 显式标签行的规范键序（累积输出的唯一排列，保证路径无关的字节稳定）。
_LABEL_ROW_KEY_ORDER = ("tid", "recId", "iid", "install_id", "packet_id", "event_id", "kind",
                        "q", "ts", "stars", "useful_idx", "useful_uids", "completion", "reasons",
                        "comment", "rating_fields", "rev", "rev_src", "label_shape", "label_source",
                        "rating_field_revs", "rating_field_srcs")


def _backfill_candidate_labels(candidates: list[dict[str, Any]],
                               labels: dict[tuple[str, str, str], dict[str, Any]]) -> int:
    """把合并后的显式标注回填进候选 rating（键 = (install_id, tid, recId)，与标签合并同源）。

    自动上传常先于评分：候选落盘时 rating 还是空的，用户随后给出的 label 事件只进
    explicit_labels；不回填则冻结器按 no_human_label 把这条样本整个丢掉（改口同理，
    还会一直用旧分）。这里消费的是**字段级累积完成**的标签行（事件按各自 rev 覆盖、
    benchfb 仅补缺），再按 label 的 rating_fields 合并——provided 空值覆盖、缺键继承
    （旧版行无 rating_fields 时退回非空覆盖语义）；候选携带 rating_rev/rating_source，
    跨轮 upsert 才能按 rev 判新旧。
    """
    filled = 0
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        key = (str(cand.get("install_id") or ""), str(cand.get("tid") or ""),
               str(cand.get("record_id") or ""))
        label = labels.get(key)
        if not label:
            continue
        cand["rating"] = _merge_label_into_rating(cand.get("rating") or {}, label)
        cand["rating_source"] = label.get("label_source")
        cand["rating_rev"] = label.get("rev")
        filled += 1
    return filled


def _backfill_candidate_context(candidates: list[dict[str, Any]], impressions: list[dict[str, Any]]) -> None:
    """用已有曝光安全补齐 benchmark 候选的 iid/policy。

    只接受同 install_id 且 tid **完全相等**的曝光，不做时间近邻或记录 id 合成。一个 tid
    指向多个 iid/多个真实策略时保守降级，避免把一条反馈错归因到任意一次检索。
    """
    by_turn: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for imp in impressions:
        install_id = str(imp.get("install_id") or "")
        tid = str(imp.get("tid") or "")
        iid = str(imp.get("iid") or "")
        if install_id and tid and iid:
            by_turn[(install_id, tid)].append(imp)

    for candidate in candidates:
        install_id = str(candidate.get("install_id") or "")
        tid = str(candidate.get("tid") or "")
        if not install_id or not tid:
            continue
        matches = by_turn.get((install_id, tid), [])
        iids = {str(imp.get("iid")) for imp in matches if imp.get("iid")}
        if len(iids) == 1:
            candidate["iid"] = next(iter(iids))

        # 记录自身有真实策略时保留；否则从精确关联的曝光中取唯一真实策略。污染对象不会
        # 再以 [object Object] 流出；找不到唯一真值时明确降级，而不是错误猜测。
        current = _normalise_policy(candidate.get("policy"))
        if current and current != POLICY_UNKNOWN:
            candidate["policy"] = current
            continue
        observed = {_normalise_policy(imp.get("policy")) for imp in matches}
        observed.discard(None)
        real = {policy for policy in observed if policy != POLICY_UNKNOWN}
        if len(real) == 1:
            candidate["policy"] = next(iter(real))
        elif current == POLICY_UNKNOWN or POLICY_UNKNOWN in observed or len(real) > 1:
            candidate["policy"] = POLICY_UNKNOWN
        else:
            candidate["policy"] = None


# --- 报告与审阅页 ---


def _delay_seconds(received_at: datetime | None, client_ts: datetime | None) -> float | None:
    if received_at is None or client_ts is None:
        return None
    delta = received_at - client_ts
    return max(delta.total_seconds(), 0.0)


def _pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:.1f}%" if b else "—"


def _quality_report(report: dict[str, Any]) -> str:
    e = report
    lines: list[str] = [
        "# 遥测导出质量报告",
        "",
        f"- 导出时间：{e['generated_at']}",
        f"- 数据范围：ingest_packets id {e['min_packet_id']}–{e['max_packet_id']}"
        f"（{e['packets_processed']} 包，received_at {e['range_start'] or '—'} ~ {e['range_end'] or '—'}）",
        f"- schema：`{SCHEMA}` 匹配 {e['packets_schema_ok']} 包；缺失/其他值 {e['packets_schema_other']} 包（容错按旧 pending 处理）",
        "",
        "## 关联完整率（交互事件 → 曝光 join）",
        "",
        f"- 交互事件共 {e['interactions_total']}；精确 join（事件自带 iid）{e['interactions_joined']}（{_pct(e['interactions_joined'], e['interactions_total'])}）；",
        f"  legacy 时间近邻推断 {e['interactions_legacy_inferred']}；orphan（join 不上）{e['interactions_orphan']}（{_pct(e['interactions_orphan'], e['interactions_total'])}）",
        f"- join 成功且 uid 解析出 {e['interactions_uid_ok']}（{_pct(e['interactions_uid_ok'], e['interactions_total'])}）；"
        f"事件自带 uid 与曝光 pos 校验不符 {e['interactions_pos_mismatch']}",
        f"- 曝光共 {e['impressions_total']}（imp 事件 {e['impressions_from_imp']} · 自描述 search {e['impressions_from_usage']} · legacy 合成 {e['impressions_legacy']}）；"
        f"跨包重复曝光键 {e['impressions_dup']}",
        f"- view 曝光追踪事件 {e['views_total']}：补 seen/dwell 成功 {e['views_joined']} · 找不到对应曝光 {e['views_orphan']}",
        "",
        "## 可标注率（benchfb 轮次中带评分比例 + label 事件）",
        "",
        f"- benchfb 轮次 {e['turns_total']}；显式标注合并后 {e['labels_total']}"
        f"（label 事件 {e['labels_from_events']} · benchfb 内嵌 rating {e['labels_from_benchfb']} · 同 tid 双源去重掉 benchfb {e['labels_benchfb_deduped']}）",
        f"- 评分形状：old {e['labels_shape_old']} · new {e['labels_shape_new']} · hybrid {e['labels_shape_hybrid']} · empty {e['labels_shape_empty']}",
        f"- 候选 rating 由后到 label 事件回填 {e.get('candidates_label_backfilled', 0)} 条"
        f"（未回填的候选冻结时按无人类标注排除，不按负例）",
        "",
        "## 重复率",
        "",
        f"- 事件级重复（同 install+event_id 跨包出现）{e['dup_events']} / 事件总数 {e['events_total']}（{_pct(e['dup_events'], e['events_total'])}）",
        "",
        "## 上传延迟分布（received_at − 客户端时间戳，秒）",
        "",
    ]
    d = e["delay"]
    if d["n"]:
        lines += [f"- n={d['n']} · min={d['min']:.0f}s · p50={d['p50']:.0f}s · p90={d['p90']:.0f}s · max={d['max']:.0f}s"]
    else:
        lines += ["- 无可用客户端时间戳"]
    lines += [
        "",
        "## schema 漂移（未知键统计，未知键不丢弃、照常遮蔽进产物）",
        "",
    ]
    if e["unknown_keys"]:
        for where, keys in sorted(e["unknown_keys"].items()):
            lines.append(f"- `{where}`：{', '.join(sorted(keys)) or '—'}")
    else:
        lines.append("- 未发现已知集合之外的键")
    lines += [
        "",
        "## 事件类型分布",
        "",
        f"- usage kinds：{_fmt_counter(e['usage_kinds'])}",
        f"- benchfb kinds：{_fmt_counter(e['benchfb_kinds'])}",
        f"- 交互类型：{_fmt_counter(e['interaction_types'])}",
        f"- 模板轮次候选（默认排除）：{e.get('candidates_excluded_template', 0)}",
        "",
        "## mcp 调用统计",
        "",
        f"- 调用总数 {e['mcp_total']}；按 install：{_fmt_counter(e['mcp_by_install'])}",
        "",
        "## 意见反馈（接收端已解密并遮蔽，这里只读遮蔽后形态）",
        "",
        f"- 意见共 {e['feedback_total']} 条（附诊断 {e['feedback_with_diag']}）→ feedback.jsonl",
    ]
    if e["quarantine_total"]:
        reasons = Counter(r.get("reason", "?") for r in e["quarantine_reasons"])
        lines += [
            "",
            "## quarantine（疑似敏感/无法处理，未进正常产物）",
            "",
            f"- 共 {e['quarantine_total']} 条：{_fmt_counter(reasons)}",
        ]
    lines.append("")
    return "\n".join(lines)


def _fmt_counter(c: dict[str, int]) -> str:
    if not c:
        return "—"
    return " · ".join(f"{k}:{v}" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))


def _review_html(impressions: list[dict], interactions: list[dict], turns: list[dict],
                 mcp_calls: list[dict], quarantined: list[dict], generated_at: str) -> str:
    """人工审阅页：每条 benchfb 轮次一卡（原话遮蔽后 + 结果摘要 + 评分/关联），
    JS 过滤条可按 kind / 只看已评分 / 只看关联完整过滤。风格沿 benchfb_ingest.py 的 review 输出。"""
    e = html.escape
    cards: list[str] = []
    for rec in reversed(turns):  # 新的在前
        rating = _as_dict(rec.get("rating"))
        try:
            stars_n = int(rating.get("stars"))
            stars = stars_n if 1 <= stars_n <= 5 else None
        except (TypeError, ValueError):
            stars = None
        star_txt = "★" * stars + "☆" * (5 - stars) if stars else "未评分"
        rows = []
        search = _as_dict(rec.get("search"))
        res = _as_dict(search.get("res"))
        for i, it in enumerate((res.get("results") or []), 1):
            if not isinstance(it, dict):
                continue
            rows.append(f"<tr><td>{i}</td><td>{e(str(it.get('dataset_uid') or ''))}</td>"
                        f"<td>{e(str(it.get('dataset_name') or it.get('title') or ''))}</td>"
                        f"<td>{e(str(it.get('source') or ''))}</td></tr>")
        results_html = ("<table><thead><tr><th>#</th><th>编号</th><th>名称</th><th>来源</th></tr></thead><tbody>"
                        + "".join(rows) + "</tbody></table>") if rows else "<p class='dim'>（无检索结果段）</p>"
        route = _as_dict(rec.get("route"))
        env = _as_dict(rec.get("env"))
        comment = str(rating.get("comment") or "")
        err = str(rec.get("err") or "")
        mcp_note = ""
        if rec.get("mcp_calls"):
            mcp_note = f"<div class='mcp'>mcp 调用 {len(rec['mcp_calls'])} 次</div>"
        cards.append(f"""
<section class="card" data-kind="{e(str(rec.get('kind') or 'unknown'))}"
     data-rated="{1 if stars else 0}" data-joined="{1 if rec.get('iid') else 0}">
  <header>
    <span class="kind k-{e(str(rec.get('kind') or 'unknown'))}">{e(str(rec.get('kind') or '?'))}</span>
    <span class="q">{e(str(rec.get('q') or ''))}</span>
    <span class="stars">{e(star_txt)}</span>
  </header>
  <div class="meta">tid {e(str(rec.get('tid') or '—'))} · iid {e(str(rec.get('iid') or '—'))}
    · 路由 {e(str(route.get('route') or '—'))} · 检索 {e(str(search.get('ms') or 0))}ms
    · 模型 {e(str(env.get('model') or '—'))} · {e(str(rec.get('_src') or ''))}</div>
  {f'<div class="comment">评语：{e(comment)}</div>' if comment else ''}
  {f'<div class="err">错误：{e(err)}</div>' if err else ''}
  {mcp_note}
  {results_html}
</section>""")
    quarr_html = "".join(
        f"<li><b>{e(str(q.get('reason') or ''))}</b> · {e(str(q.get('install_id') or ''))} · "
        f"{e(str(q.get('record_id') or q.get('packet_id') or ''))}：{e(json.dumps(q.get('sample'), ensure_ascii=False)[:200])}</li>"
        for q in quarantined) if quarantined else ""
    kinds = sorted({str(r.get("kind") or "unknown") for r in turns})
    kind_btns = "".join(
        f'<button class="fk" data-kind="{e(k)}">{e(k)}</button>' for k in kinds)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>遥测 benchmark 审阅 · {len(turns)} 轮</title>
<style>
body{{font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;max-width:1080px;margin:24px auto;padding:0 16px;color:#1c1f24}}
h1{{font-size:20px}} .sum{{color:#5b6068;margin-bottom:18px}}
#filters{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0 4px}}
button.fk{{border:1px solid #c9d2dc;background:#fff;border-radius:999px;padding:2px 12px;cursor:pointer}}
button.fk.on{{background:#0d9488;color:#fff;border-color:#0d9488}}
label{{color:#5b6068;font-size:13px;display:flex;gap:4px;align-items:center}}
.card{{border:1px solid #dde3ea;border-radius:12px;padding:14px 16px;margin:14px 0;background:#fffdf9}}
.card.hidden{{display:none}}
.card header{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}
.kind{{flex:0 0 auto;padding:1px 10px;border-radius:999px;font-size:12px;font-weight:700;color:#fff;background:#8a8f98}}
.k-search{{background:#0d9488}} .k-tool{{background:#7c5cd6}} .k-none{{background:#b78900}} .k-error{{background:#c0392b}}
.q{{font-weight:700;font-size:15px}} .stars{{margin-left:auto;color:#e8a33d;font-size:16px}}
.meta{{color:#8a8f98;font-size:12px;margin:4px 0 8px}}
.comment{{background:#fff7e6;border-left:3px solid #e8a33d;padding:6px 10px;margin:8px 0;font-size:13px}}
.err{{background:#fdecea;border-left:3px solid #c0392b;padding:6px 10px;margin:8px 0;font-size:13px}}
.mcp{{background:#eef4fb;border-left:3px solid #3b82f6;padding:6px 10px;margin:8px 0;font-size:13px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}}
td,th{{border:1px solid #e6ebf0;padding:4px 8px;text-align:left}}
.dim{{color:#8a8f98}} .quarr{{background:#fdecea;padding:10px 14px;border-radius:10px}}
</style></head><body>
<h1>遥测 benchmark 审阅</h1>
<p class="sum">共 {len(turns)} 条 benchfb 轮次 · {len(impressions)} 条曝光 · {len(interactions)} 条交互 ·
{len(mcp_calls)} 次 mcp 调用 · 生成于 {e(generated_at)}</p>
<div id="filters">{kind_btns}
<label><input type="checkbox" id="onlyRated"> 只看已评分</label>
<label><input type="checkbox" id="onlyJoined"> 只看关联完整（有 iid）</label>
<button class="fk" id="clearF">全部</button></div>
{f'<div class="quarr"><strong>quarantine（{len(quarantined)} 条，未进正常产物）：</strong><ul>{quarr_html}</ul></div>' if quarantined else ''}
<div id="cards">{''.join(cards)}</div>
<script>
const kindSel = new Set();
const cards = Array.from(document.querySelectorAll('.card'));
function apply() {{
  cards.forEach(c => {{
    const showKind = kindSel.size === 0 || kindSel.has(c.dataset.kind);
    const showRated = !ratedOnly.checked || c.dataset.rated === '1';
    const showJoined = !joinedOnly.checked || c.dataset.joined === '1';
    c.classList.toggle('hidden', !(showKind && showRated && showJoined));
  }});
}}
const ratedOnly = document.getElementById('onlyRated');
const joinedOnly = document.getElementById('onlyJoined');
document.querySelectorAll('button.fk[data-kind]').forEach(b => b.addEventListener('click', () => {{
  const k = b.dataset.kind;
  if (kindSel.has(k)) {{ kindSel.delete(k); b.classList.remove('on'); }}
  else {{ kindSel.add(k); b.classList.add('on'); }}
  apply();
}}));
ratedOnly.addEventListener('change', apply);
joinedOnly.addEventListener('change', apply);
document.getElementById('clearF').addEventListener('click', () => {{
  kindSel.clear(); ratedOnly.checked = false; joinedOnly.checked = false;
  document.querySelectorAll('button.fk[data-kind]').forEach(b => b.classList.remove('on'));
  apply();
}});
</script>
</body></html>"""


# --- 导出主流程 ---


def _missing_sqlite_source(dsn: str) -> str | None:
    """SQLite 文件型 DSN 指向不存在的文件时返回该路径（源缺失，调用方 fail-loud）。

    导出只读消费：SQLAlchemy 打开不存在的 SQLite 文件会静默新建空库，把"路径写错/源缺失"
    伪装成"合法的 0 包扫描"，进而触发全量退场。内存库、非 SQLite、已存在的文件返回 None。
    """
    raw = dsn.strip()
    url = raw if raw.startswith(("sqlite", "postgresql")) else "sqlite:///" + raw
    if not url.startswith("sqlite"):
        return None
    path = url.split("?", 1)[0].removeprefix("sqlite+pysqlite:///").removeprefix("sqlite:///")
    path = path.removeprefix("sqlite://")
    if ":memory:" in path:
        return None
    if not path:                    # 没给库路径（sqlite://）也是配置错：不能当合法空库
        return raw
    return None if Path(path).is_file() else path


def _make_engine(dsn: str):
    dsn = dsn.strip()
    if dsn.startswith(("sqlite", "postgresql")):
        url = dsn
    else:
        url = "sqlite:///" + dsn  # 裸路径当 SQLite 文件
    kwargs: dict[str, Any] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def _iter_packets(engine, *, since: datetime | None, until: datetime | None) -> Iterable[dict[str, Any]]:
    """用服务端游标分批读取已完整落库的主包，避免把整张 payload 表先 ``.all()`` 进内存。

    跨包关联仍会在导出阶段建立必要索引，但数据库读取是有界 500 行批次；PostgreSQL 使用
    server-side cursor，SQLite 也逐行迭代。只读 join receipts，按主包 id 稳定升序。
    """
    stmt = (
        select(ingest_packets)
        .join(ingest_packet_receipts, ingest_packet_receipts.c.row_id == ingest_packets.c.id)
        .order_by(ingest_packets.c.id)
    )
    if since is not None:
        stmt = stmt.where(ingest_packets.c.received_at >= since)
    if until is not None:
        stmt = stmt.where(ingest_packets.c.received_at <= until)
    with engine.connect() as conn:
        result = conn.execution_options(stream_results=True, yield_per=500).execute(stmt).mappings()
        for row in result:
            yield dict(row)


def _export(engine, out_dir: Path, *, since: datetime | None, until: datetime | None,
            incremental: bool, accepted_ids: set[str] | None) -> dict[str, Any]:
    """执行导出，返回汇总（供测试断言与报告使用）。

     起为**两阶段全量扫描**：先把所有包的事件扫成曝光/标注索引（跨包 join——
    激进上传后 impression 与交互常分属不同包），再统一产出 artifacts。--incremental 不再按
    watermark 过滤包；产物按稳定行键 upsert + 原子替换保证幂等（重复跑不重复产出），
    watermark 仅信息性记录。

    合并取舍：全库扫描时本次扫描即真源（已从库中清除的行真正退场，含保留期清理过的旧包；
    合法的 0 包全库扫描——最后一包过期或全库清空——同样原子退场全部产物数据）。只有带
    --since/--until 的窗口增量才保留窗口外的既有行（未扫描 ≠ 已删除）：显式标签与候选
    借旧产物作种子，经字段级修订累积（每字段取各自最高 rev，显式空覆盖、缺键继承）与
    稳定键合并后统一回填重写；窗口模式下"已删除数据"的退场责任在运维的定期全库扫描。
    源不可访问（SQLite 文件缺失、连接/读取失败）由 CLI 前置检查与异常路径 fail-loud：
    报错退出且既有产物保持原样，绝不把"读不到"当成"空"。
    """
    prev_watermark: int | None = None
    state_path = out_dir / STATE_FILE
    if incremental and state_path.exists():
        try:
            prev_watermark = int(json.loads(state_path.read_text(encoding="utf-8"))["watermark"])
        except (ValueError, KeyError, OSError):
            prev_watermark = None

    packets = _iter_packets(engine, since=since, until=until)

    # 全量索引（跨包）：自描述曝光按 (install_id,sid,tid,iid) 精确键；显式标注双源按
    # (install_id,tid,recId) 合并（recId 区分同一轮次下的多条记录/多 rev，两侧键空间统一，
    # 双源去重才真正生效；老数据缺 recId 空串兜底，与 benchfb 侧不匹配属预期降级）
    impressions_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    imp_event_kind: dict[tuple[str, str, str, str], str] = {}   # 精确键 → 来源事件 kind（统计用）
    legacy_impressions: list[dict[str, Any]] = []               # 无 iid 的旧 search 事件（合成 iid）
    view_events: list[dict[str, Any]] = []
    label_events: dict[tuple[str, str, str], dict[str, Any]] = {}
    benchfb_labels: dict[tuple[str, str, str], dict[str, Any]] = {}
    pending_interactions: list[dict[str, Any]] = []

    interactions: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    mcp_calls: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    agent_trajectories: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    feedback_rows: list[dict[str, Any]] = []   # 意见反馈（payload 内为解密遮蔽后形态）

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "packets_processed": 0, "packets_schema_ok": 0, "packets_schema_other": 0,
        "min_packet_id": None, "max_packet_id": None, "range_start": None, "range_end": None,
        "impressions_total": 0, "impressions_from_imp": 0, "impressions_from_usage": 0,
        "impressions_legacy": 0, "impressions_dup": 0,
        "views_total": 0, "views_joined": 0, "views_orphan": 0,
        "interactions_total": 0, "interactions_joined": 0, "interactions_legacy_inferred": 0,
        "interactions_orphan": 0, "interactions_uid_ok": 0, "interactions_pos_mismatch": 0,
        "turns_total": 0, "labels_total": 0,
        "labels_from_events": 0, "labels_from_benchfb": 0, "labels_benchfb_deduped": 0,
        "labels_shape_old": 0, "labels_shape_new": 0, "labels_shape_hybrid": 0, "labels_shape_empty": 0,
        "events_total": 0, "dup_events": 0,
        "delay": {"n": 0, "min": 0.0, "p50": 0.0, "p90": 0.0, "max": 0.0},
        "unknown_keys": defaultdict(set),
        "usage_kinds": Counter(), "benchfb_kinds": Counter(), "interaction_types": Counter(),
        "mcp_total": 0, "mcp_by_install": Counter(),
        "quarantine_total": 0, "quarantine_reasons": [],
        "feedback_total": 0, "feedback_with_diag": 0,   #
        "agent_trajectories_total": 0,
        "candidates_excluded_template": 0,   # 默认排除的模板轮次候选计数
        "candidates_label_backfilled": 0,    # 候选 rating 被后到 label 事件回填的条数
    }
    seen_events: set[tuple[str, str]] = set()
    delays: list[float] = []
    # accepted 三态：None=未指定（final 不创建/不改写）；空集=显式撤销全员（final 重写为空）；
    # 非空=名单过滤。绝不把 None 折叠成空集。
    accepted = accepted_ids

    for p in packets:
        report["packets_processed"] += 1
        pid = int(p["id"])
        payload = p["payload"]
        packet_id = (payload.get("packet_id") if isinstance(payload, dict) else None) or f"pkt-{pid}"
        if not isinstance(payload, dict):
            quarantined.append({"reason": "unprocessable", "packet_id": packet_id, "install_id": p["install_id"],
                                "sample": None})
            continue
        report["min_packet_id"] = pid if report["min_packet_id"] is None else min(report["min_packet_id"], pid)
        report["max_packet_id"] = pid if report["max_packet_id"] is None else max(report["max_packet_id"], pid)
        report["range_start"] = report["range_start"] or p["received_at"]
        report["range_end"] = p["received_at"]
        if payload.get("schema") == SCHEMA:
            report["packets_schema_ok"] += 1
        else:
            report["packets_schema_other"] += 1  # 容错：缺 schema / 旧版本按 pending 数据处理

        install_id = str(payload.get("install_id") or p["install_id"] or "")
        profile_id = str(payload.get("profile_id") or "")
        received_at = _dt(p["received_at"])
        received_at_iso = received_at.isoformat() if received_at else None
        exported_at = _dt(payload.get("exported_at"))
        delay_client_ts = exported_at
        pkg_unknown = set(payload.keys()) - KNOWN_PACKET_KEYS
        if pkg_unknown:
            report["unknown_keys"]["packet"].update(pkg_unknown)
        app = payload.get("app") or {}
        if isinstance(app, dict):
            app_unknown = set(app.keys()) - KNOWN_APP_KEYS
            if app_unknown:
                report["unknown_keys"]["app"].update(app_unknown)

        usage = payload.get("usage_events") or []
        benchfb = payload.get("benchfb_records") or []
        mcp = payload.get("mcp_records") or []
        if not isinstance(usage, list) or not isinstance(benchfb, list) or not isinstance(mcp, list):
            quarantined.append({"reason": "unprocessable", "packet_id": packet_id, "install_id": install_id,
                                "sample": {"schema": payload.get("schema")}})
            continue

        # 事件级去重统计 + 客户端时间戳（用于延迟分布）
        client_ts_min: datetime | None = None
        first_usage = usage[0] if usage else None
        for ev in list(usage) + list(benchfb):
            if not isinstance(ev, dict):
                continue
            key = (install_id, str(ev.get("event_id") or ev.get("id") or ""))
            if key[1]:
                report["events_total"] += 1
                if key in seen_events:
                    report["dup_events"] += 1
                seen_events.add(key)
            t = _dt(ev.get("t"))
            if t is None and ev is first_usage:
                t = exported_at  # 旧客户端无事件时间戳：退化用包的 exported_at
            if t and (client_ts_min is None or t < client_ts_min):
                client_ts_min = t
        if delay_client_ts is None and client_ts_min is not None:
            delay_client_ts = client_ts_min
        d_sec = _delay_seconds(received_at, delay_client_ts)
        if d_sec is not None:
            delays.append(d_sec)

        # --- benchfb 轮次：只作 turn / label / candidate 来源（不再制造 impression）---
        for rec_index, rec in enumerate(benchfb):
            if not isinstance(rec, dict):
                quarantined.append({"reason": "unprocessable", "packet_id": packet_id, "install_id": install_id,
                                    "sample": {"benchfb": rec}})
                continue
            masked_rec, qreason, _ = classify_record(rec)
            if qreason:
                quarantined.append({"reason": qreason, "packet_id": packet_id, "install_id": install_id,
                                    "record_id": rec.get("id"), "sample": masked_rec})
                continue
            kind = str(rec.get("kind") or "unknown")
            report["benchfb_kinds"][kind] += 1
            rid = rec.get("id")
            turns.append({
                "tid": rid, "install_id": install_id, "packet_id": packet_id,
                "received_at": received_at_iso,
                # 记录自带 iid 才透传（v3 打点）；旧记录不再从 id 合成（那是重复制造曝光的残留）
                "iid": masked_rec.get("iid") or None,
                **_masked_flatten(masked_rec),
            })
            agent_trajectories.append(_agent_trajectory(
                masked_rec, install_id=install_id, packet_id=packet_id, packet_context=payload,
            ))
            report["agent_trajectories_total"] += 1
            report["turns_total"] += 1
            rating = _as_dict(rec.get("rating"))
            if isinstance(rating, dict):
                shape = _rating_shape(rating)
                report[f"labels_shape_{shape}"] += 1
                if rid and _rating_nonempty(rating):
                    # benchfb 内嵌 rating 是显式标注的**兜底源**；同 (install_id,tid,recId) 键有 label
                    # 事件时被事件覆盖（键 = 记录内 tid + record id，与 label 事件键空间统一；
                    # 记录无 tid 字段的老数据 rec_tid 为空串，与带 tid 的新事件不匹配属预期降级）
                    rec_tid = str(masked_rec.get("tid") or "")
                    benchfb_labels[(install_id, rec_tid, str(rid))] = {
                        # tid 是使用轮次 id；rid 只是 benchfb 记录/recId，二者绝不能互换。
                        "tid": rec_tid or None, "recId": str(rid), "iid": masked_rec.get("iid") or None,
                        "install_id": install_id, "packet_id": packet_id,
                        "kind": kind, "q": masked_rec.get("q"), "ts": _ts_ms(rec.get("t")),
                        "stars": rating.get("stars"),
                        "useful_idx": rating.get("useful_idx") or [],
                        "useful_uids": rating.get("useful_uids") or _resolve_useful_uids(rec),
                        "completion": rating.get("completion"),
                        "reasons": rating.get("reasons"),
                        "comment": masked_rec.get("rating", {}).get("comment"),
                        # 兜底源同样记录「哪些评分键真的出现过」：合并时 provided 空值覆盖、
                        # 缺键继承，不把客户端只发了部分字段的旧形状当完整快照清空。
                        "rating_fields": _provided_rating_fields(rating),
                        "rev": None,
                        # 接收序来源（packet 行 id, 包内位置）：同 rev 并列以 DB 接收序决胜
                        "rev_src": [pid, rec_index],
                        "label_shape": shape,
                        "label_source": "benchfb",
                    }
            # 无稳定 record id 的旧数据（legacy）只进 turns/labels，不进 candidates——
            # 候选必须能被 --accepted 名单精确命中
            if rid:
                # benchmark 候选**默认排除**「未经编辑直接发送」的模板轮次
                # （template_originated=true——模板句不是用户自己组织的检索词，进候选会污染
                # 真实用户查询分布）；turns.jsonl 仍全量保留该键，事后审计/白名单仍可命中。
                if masked_rec.get("template_originated") is True:
                    report["candidates_excluded_template"] += 1
                    continue
                cand = _candidate(masked_rec, install_id=install_id, packet_id=packet_id,
                                  packet_context=payload)
                candidates.append(cand)

        # --- usage 事件：imp/search → 曝光索引；view → 曝光补充；label → 显式标注；open/dl/fav → 交互 ---
        for ev_index, ev in enumerate(usage):
            if not isinstance(ev, dict):
                quarantined.append({"reason": "unprocessable", "packet_id": packet_id, "install_id": install_id,
                                    "sample": {"usage": ev}})
                continue
            masked_ev, qreason, _ = classify_record(ev)
            if qreason:
                quarantined.append({"reason": qreason, "packet_id": packet_id, "install_id": install_id,
                                    "record_id": ev.get("event_id"), "sample": masked_ev})
                continue
            kind = str(ev.get("k") or "unknown")
            report["usage_kinds"][kind] += 1
            ev_unknown = set(ev.keys()) - KNOWN_USAGE_KEYS
            if ev_unknown:
                report["unknown_keys"]["usage_events"].update(ev_unknown)
            ev_iid = str(ev.get("iid") or "")
            ev_sid = str(ev.get("sid") or "")
            ev_tid = str(ev.get("tid") or "")
            if kind == IMP_KIND or (kind == SEARCH_KIND and ev_iid):
                # v3 imp 事件 / v2+ 自描述 search 事件：曝光的**主来源**，跨包精确键去重
                key = (install_id, ev_sid, ev_tid, ev_iid)
                row = {
                    "iid": ev_iid, "tid": ev_tid or None, "sid": ev_sid or None,
                    "profile": profile_id or None,
                    "q": masked_ev.get("q"), "policy": _normalise_policy(masked_ev.get("policy")),
                    "items": _norm_items(ev.get("items")), "ts": _ts_ms(ev.get("t")),
                    "install_id": install_id, "packet_id": packet_id,
                    "join_status": "ok",
                }
                prev = impressions_by_key.get(key)
                if prev is None or (not prev["items"] and row["items"]):
                    # 同键重复（重叠上传窗口绕过事件级去重）：留信息更全的一行（有 items 优先）
                    impressions_by_key[key] = row
                    imp_event_kind[key] = kind
                if prev is not None:
                    report["impressions_dup"] += 1
            elif kind == SEARCH_KIND:
                # 无 iid 的旧 search 事件：合成 iid 进曝光；交互关联走 legacy 时间近邻
                legacy_impressions.append({
                    "iid": _imp_id(install_id, ev.get("event_id")), "tid": ev_tid or None,
                    "sid": ev_sid or None, "profile": profile_id or None,
                    "q": masked_ev.get("q"), "policy": _normalise_policy(masked_ev.get("policy")),
                    "items": _norm_items(ev.get("items")), "ts": _ts_ms(ev.get("t")),
                    "install_id": install_id, "packet_id": packet_id,
                    "join_status": "legacy",
                })
            elif kind == VIEW_KIND:
                report["views_total"] += 1
                view_events.append({
                    "install_id": install_id, "sid": ev_sid, "tid": ev_tid, "iid": ev_iid,
                    "seen": _norm_int_list(ev.get("seen")),
                    "dwell_ms": ev.get("dwell_ms") if isinstance(ev.get("dwell_ms"), (int, float)) else None,
                })
            elif kind == LABEL_KIND:
                rev = ev.get("rev")
                rev_i = int(rev) if isinstance(rev, (int, float)) else 0
                # 键 = (install_id, tid, recId)——同一轮次下多条记录的评分互不吞并（1c），
                # 且与 benchfb 记录键空间统一（1a 双源去重真正生效）；老数据无 recId → 空串兜底，
                # 与 benchfb 侧键不匹配属预期降级。无 tid 的孤儿事件沿用 packet 内唯一化兜底
                # （与旧产物种子/写出去重同一 _label_merge_key 推导，重跑不串键）。
                ev_recid = str(ev.get("recId") or "")
                lkey = _label_merge_key(install_id, ev_tid, packet_id,
                                        str(ev.get("event_id") or ""), _ts_ms(ev.get("t")), ev_recid)
                rating_like = {"completion": masked_ev.get("completion"), "reasons": masked_ev.get("reasons"),
                               "stars": masked_ev.get("stars"), "useful_idx": masked_ev.get("useful_idx"),
                               "comment": masked_ev.get("comment")}
                # 字段级修订累积（唯一机制 _accumulate_label_fields）：每字段各自取最高 rev，
                # 不再整行只留最高 rev——否则 rev2 的显式清空会被 rev3 的部分修订整条丢掉，
                # 候选 rating 从 benchfb 记录复活已撤销的旧正例。
                label_events[lkey] = _accumulate_label_fields(label_events.get(lkey), {
                    "tid": ev_tid or None, "recId": ev_recid or None, "iid": ev_iid or None,
                    "install_id": install_id, "packet_id": packet_id,
                    "event_id": str(ev.get("event_id") or ""),
                    "kind": LABEL_KIND, "q": masked_ev.get("q"), "ts": _ts_ms(ev.get("t")),
                    "stars": masked_ev.get("stars"),
                    "useful_idx": _norm_int_list(ev.get("useful_idx")),
                    "useful_uids": [str(u) for u in (ev.get("useful_uids") or []) if u] if isinstance(ev.get("useful_uids"), list) else [],
                    "completion": masked_ev.get("completion"),
                    "reasons": masked_ev.get("reasons"),
                    "comment": masked_ev.get("comment"),
                    # 事件真实带过的评分键（含显式空值）：合并时 provided 空值覆盖、
                    # 缺键继承——完整快照的清空必须落盘，部分更新的缺键不能误清。
                    "rating_fields": _provided_rating_fields(masked_ev),
                    "rev": rev_i,
                    # 接收序来源（packet 行 id, 包内位置）：同 rev 并列以 DB 接收序决胜
                    "rev_src": [pid, ev_index],
                    "label_shape": _rating_shape({k: v for k, v in rating_like.items() if v is not None}),
                    "label_source": "event",
                })
            itype = interaction_type(ev)
            if itype is not None:
                report["interaction_types"][itype] += 1
                pos = ev.get("pos", ev.get("r"))
                pos_i = int(pos) if isinstance(pos, (int, float)) and pos > 0 else (int(pos) if isinstance(pos, str) and pos.isdigit() else None)
                self_uid = masked_ev.get("uid")
                pending_interactions.append({
                    "iid": None, "tid": None, "uid": None, "pos": pos_i, "type": itype,
                    "ts": _ts_ms(ev.get("t")), "event_id": str(ev.get("event_id") or ""),
                    "install_id": install_id, "packet_id": packet_id, "sid": ev_sid,
                    "_self_iid": ev_iid, "_self_tid": ev_tid,
                    "_self_uid": str(self_uid) if self_uid not in (None, "") else None,
                    "_ev_ts": ev.get("t"),
                })

        for mrec in mcp:
            if not isinstance(mrec, dict) or not isinstance(mrec.get("call_id"), str):
                quarantined.append({"reason": "unprocessable", "packet_id": packet_id, "install_id": install_id,
                                    "sample": {"mcp": mrec}})
                continue
            masked_mrec, qreason, _ = classify_record(mrec)
            if qreason:
                quarantined.append({"reason": qreason, "packet_id": packet_id, "install_id": install_id,
                                    "record_id": mrec.get("call_id"), "sample": masked_mrec})
                continue
            mcp_unknown = set(mrec.keys()) - KNOWN_MCP_KEYS
            if mcp_unknown:
                report["unknown_keys"]["mcp_records"].update(mcp_unknown)
            mcp_calls.append({
                "call_id": mrec.get("call_id"),
                "install_id": install_id, "packet_id": packet_id,
                "received_at": received_at_iso,
                **masked_mrec,
            })
            report["mcp_total"] += 1
            report["mcp_by_install"][install_id] += 1

        # --- 意见反馈（接收端已解密并过 API Key/值级遮蔽；这里只读遮蔽后形态）---
        feedback = payload.get("feedback_records") or []
        if not isinstance(feedback, list):
            quarantined.append({"reason": "unprocessable", "packet_id": packet_id, "install_id": install_id,
                                "sample": {"feedback": feedback}})
        else:
            for fbk in feedback:
                if not isinstance(fbk, dict):
                    quarantined.append({"reason": "unprocessable", "packet_id": packet_id,
                                        "install_id": install_id, "sample": {"feedback": fbk}})
                    continue
                masked_fbk, qreason, _ = classify_record(fbk)
                if qreason:
                    quarantined.append({"reason": qreason, "packet_id": packet_id, "install_id": install_id,
                                        "record_id": fbk.get("feedback_id"), "sample": masked_fbk})
                    continue
                fb_unknown = set(fbk.keys()) - KNOWN_FEEDBACK_KEYS
                if fb_unknown:
                    report["unknown_keys"]["feedback_records"].update(fb_unknown)
                feedback_rows.append({
                    "feedback_id": str(masked_fbk.get("feedback_id") or ""),
                    "identity": str(masked_fbk.get("identity") or ""),
                    "with_diag": bool(masked_fbk.get("with_diag")),
                    "authorized_at": masked_fbk.get("authorized_at"),
                    "text": masked_fbk.get("text"),
                    "diag": masked_fbk.get("diag"),
                    "install_id": install_id, "packet_id": packet_id,
                    "received_at": received_at_iso,
                })
                report["feedback_total"] += 1
                if masked_fbk.get("with_diag"):
                    report["feedback_with_diag"] += 1

    # ================= 第二阶段：跨包 join（全量索引建完再解析） =================
    # view 事件 → 给对应曝光补 seen/dwell_ms（同 iid 多次上报取 seen 并集、dwell 最大值）
    for vev in view_events:
        imp = impressions_by_key.get((vev["install_id"], vev["sid"], vev["tid"], vev["iid"])) if vev["iid"] else None
        if imp is None:
            report["views_orphan"] += 1
            continue
        merged_seen = sorted(set(imp.get("seen") or []) | set(vev["seen"]))
        if merged_seen:
            imp["seen"] = merged_seen
        dwell = [d for d in (imp.get("dwell_ms"), vev["dwell_ms"]) if isinstance(d, (int, float))]
        if dwell:
            imp["dwell_ms"] = int(max(dwell))
        report["views_joined"] += 1

    impressions = list(impressions_by_key.values()) + legacy_impressions
    report["impressions_total"] = len(impressions)
    report["impressions_from_imp"] = sum(1 for k in imp_event_kind.values() if k == IMP_KIND)
    report["impressions_from_usage"] = sum(1 for k in imp_event_kind.values() if k == SEARCH_KIND)
    report["impressions_legacy"] = len(legacy_impressions)

    # candidates 在首轮扫描中先保留自身现场；只有这里完整曝光索引已经建立后，才可以做
    # 跨包的 exact-tid 关联。这样不会把先到的 benchfb 记录错误地绑定到后来任意一条曝光。
    _backfill_candidate_context(candidates, impressions)
    _backfill_candidate_context(agent_trajectories, impressions)

    # legacy 时间近邻池：同 install、ts 不晚于交互的最近曝光；ts 相同时优先带 items 的
    legacy_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for imp in impressions:
        if isinstance(imp["ts"], (int, float)):
            legacy_pool[imp["install_id"]].append(imp)
    for pool_rows in legacy_pool.values():
        pool_rows.sort(key=lambda imp: (imp["ts"], 1 if imp["items"] else 0))

    for inter in pending_interactions:
        ev_ts = inter.pop("_ev_ts")
        self_iid = inter.pop("_self_iid")
        self_tid = inter.pop("_self_tid")
        self_uid = inter.pop("_self_uid")
        joined = None
        if self_iid:
            # v2/v3 自描述事件：跨包精确 join（install_id,sid,tid,iid）
            joined = impressions_by_key.get((inter["install_id"], inter["sid"], self_tid, self_iid))
        uid = self_uid  # 事件自带 uid 优先
        if joined is not None and inter["pos"] is not None:
            pos_uid = next((it["uid"] for it in joined["items"] if it["pos"] == inter["pos"]), None)
            if uid and pos_uid and uid != pos_uid:
                report["interactions_pos_mismatch"] += 1  # pos 只作校验：对不上计数，uid 仍取事件自带值
            if not uid:
                uid = pos_uid
        legacy_inferred = False
        if joined is None and not self_iid:
            # 无 iid 的旧事件：同 install 时间近邻（跨包），标 legacy_inferred
            for imp in reversed(legacy_pool.get(inter["install_id"], [])):
                if ev_ts is not None and imp["ts"] <= ev_ts:
                    joined = imp
                    break
            if joined is not None and inter["pos"] is not None and not uid:
                uid = next((it["uid"] for it in joined["items"] if it["pos"] == inter["pos"]), None)
            legacy_inferred = joined is not None
        inter.update({
            # join 不上（orphan）也按事件自带值透出 iid/tid/uid——自描述数据不丢
            "iid": joined["iid"] if joined else (self_iid or None),
            "tid": (joined.get("tid") if joined else None) or self_tid or None,
            "uid": uid,
            "join_status": "ok" if (joined and not legacy_inferred)
                           else ("legacy_inferred" if legacy_inferred else "orphan"),
            "uid_ok": bool(uid),
        })
        interactions.append(inter)
        report["interactions_total"] += 1
        if joined is not None and not legacy_inferred:
            report["interactions_joined"] += 1
        elif legacy_inferred:
            report["interactions_legacy_inferred"] += 1
        else:
            report["interactions_orphan"] += 1
        if uid:
            report["interactions_uid_ok"] += 1

    # 显式标注双源合并：benchfb 内嵌 rating 兜底，label 事件按字段并入（键 = (install_id,tid,recId)，
    # 双源去重真正生效，同一轮次多条记录/多 rev 不再互相吞并；事件未提供的字段保留 benchfb
    # 兜底值，事件提供的字段（含显式空）按各自 rev 覆盖——整行替换会复活已撤销的旧正例）。
    report["labels_from_benchfb"] = len(benchfb_labels)
    report["labels_from_events"] = len(label_events)
    report["labels_benchfb_deduped"] = len(set(benchfb_labels) & set(label_events))
    merged_labels: dict[tuple[str, str, str], dict[str, Any]] = {}
    for lkey, brow in benchfb_labels.items():
        merged_labels[lkey] = _accumulate_label_fields(None, brow)
    for lkey, erow in label_events.items():
        merged_labels[lkey] = _accumulate_label_fields(merged_labels.get(lkey), erow)
    report["labels_from_benchfb"] -= report["labels_benchfb_deduped"]

    # --- 写产物：按稳定行键 upsert + 整份原子替换 ---
    # 只有带 --since/--until 的窗口增量才保留旧文件行：窗口外的数据不在本次扫描范围内，
    # 存量导出无法判定它是"仍在库"还是"已删除"，所以不把未扫描当已删除（保留，退场责任在
    # 运维的定期全库扫描，见 --incremental 帮助）。全库扫描时本次扫描即真源：被保留期清理
    # 或显式删除的数据随之**原子退场**——包括合法的最后一包过期/全库清空到 0 包，绝不把
    # 旧查询/评语永久拼在产物里（否则接收端的 mtime 清理永远删不掉它）。
    # 源不可访问（DSN 路径缺失/权限/连接失败）由 main 的 fail-loud 前置检查与异常路径兜住：
    # 报错退出、产物保持原样，绝不把"读不到"当成"空"。
    if report["packets_processed"] == 0 and since is None and until is None:
        print("[telemetry-export] 本次全库扫描 0 包：按扫描结果原子退场既有产物中的全部数据",
              file=sys.stderr)
    elif report["packets_processed"] == 0:
        print("[telemetry-export] 本次窗口扫描 0 包：窗口外既有数据保留；"
              "要退场已删除数据请跑一次全库扫描", file=sys.stderr)
    merge_old = incremental and (since is not None or until is not None)
    out_dir.mkdir(parents=True, exist_ok=True)
    if merge_old:
        # 窗口增量唯一借旧产物的两粒种子（全库扫描不借，本次扫描即真源）：
        # ① 旧 explicit_labels 作标签字段累积的种子——同一 _accumulate_label_fields 机制。
        #    种子先规范化为初始 acc，再把本轮扫描结果并入：双方来源已知的同 rev 并列取
        #    更早接收位置（判据与并入顺序无关，反向窗口同库一致）；任一侧来源未知的并列
        #    一律保留旧值——旧产物是已存历史，未知来源没有反转权（只有严格更高 rev 或
        #    双方可信的更早来源才能改旧值；legacy 无法推出全局接收序，只能保守保存已知，
        #    确定性重建靠全库重扫）。每字段 rev 各自比较，缺字段继承旧行，不会再因
        #    「同 highest rev 整行 prefer_rev」把新窗口累积出的字段吞掉。
        # ② 旧 benchmark_candidates 作候选稳定键合并的种子——窗口外候选行不会重新生成。
        if (out_dir / "explicit_labels.jsonl").is_file():
            seed_labels: dict[tuple[str, str, str], dict[str, Any]] = {}
            for ln in (out_dir / "explicit_labels.jsonl").read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                old = json.loads(ln)
                if not isinstance(old, dict):
                    continue
                skey = _label_merge_key(str(old.get("install_id") or ""), str(old.get("tid") or ""),
                                        str(old.get("packet_id") or ""), str(old.get("event_id") or ""),
                                        old.get("ts"), str(old.get("recId") or ""))
                seed_labels[skey] = _accumulate_label_fields(None, old)
            for skey, srow in seed_labels.items():
                cur = merged_labels.get(skey)
                merged_labels[skey] = _accumulate_label_fields(srow, cur) if cur is not None else srow
        if (out_dir / "benchmark_candidates.jsonl").is_file():
            prior_candidates = [json.loads(ln) for ln in
                                (out_dir / "benchmark_candidates.jsonl").read_text(encoding="utf-8").splitlines()
                                if ln.strip()]
            by_key = {("cand", str(r.get("packet_id")), str(r.get("record_id"))): r
                      for r in prior_candidates if isinstance(r, dict)}
            for r in candidates:      # 窗口内重扫到的记录以本次扫描行为准（同键原位替换）
                by_key[("cand", str(r.get("packet_id")), str(r.get("record_id")))] = r
            candidates = list(by_key.values())
    explicit_labels = sorted(merged_labels.values(),
                             key=lambda r: (r.get("ts") or 0, r["install_id"], str(r.get("tid") or "")))
    report["labels_total"] = len(explicit_labels)
    # 统一回填：标签字段累积与候选稳定键合并都完成后，把终态标签一次性回填进终态候选——
    # 晚到的评分/改口/窗口外历史修订都进候选，冻结器才不丢样本、不复活已撤销正例。
    report["candidates_label_backfilled"] = _backfill_candidate_labels(candidates, merged_labels)

    # --- 统计收尾 ---
    if delays:
        delays.sort()
        report["delay"] = {
            "n": len(delays), "min": delays[0], "max": delays[-1],
            "p50": delays[len(delays) // 2], "p90": delays[int(len(delays) * 0.9) - 1],
        }
    report["quarantine_total"] = len(quarantined)
    report["quarantine_reasons"] = [{"reason": q.get("reason")} for q in quarantined]
    report["unknown_keys"] = {k: sorted(v) for k, v in report["unknown_keys"].items()}

    products = [
        # (文件名, 行, 稳定键, 同键取舍, rev 字段)
        # impressions 用 prefer_enriched：view 事件的 seen/dwell 可能晚于曝光本身落库，
        # 后跑的全量扫描行信息更全时允许覆盖旧行（其余产物行同键同形，先到先得即可）
        ("impressions.jsonl", impressions, lambda r: ("imp", r["install_id"], r["iid"]), "prefer_enriched", None),
        ("interactions.jsonl", interactions,
         lambda r: ("int", r["install_id"], r.get("event_id") or f"{r['packet_id']}:{r['type']}:{r.get('ts')}"),
         "keep_old", None),
        ("turns.jsonl", turns,
         lambda r: ("turn", r["packet_id"], r.get("id") or r.get("tid")), "keep_old", None),
        # 显式标签：字段级累积在写出前已完成（含窗口种子），本次合并结果即真源——
        # 改口（rev2）/高 rev 清空评分已真正落盘，不被旧行钉死
        ("explicit_labels.jsonl", explicit_labels,
         lambda r: ("lab", r["install_id"], r.get("tid") or f"{r['packet_id']}:{r.get('event_id') or r.get('ts')}",
                    r.get("recId") or ""), "prefer_rev", "rev"),
        # mcp 幂等键跨包稳定（中继 CAS 重传会同 call_id 进新包）：按 install+call_id 去重
        ("mcp_calls.jsonl", mcp_calls, lambda r: ("mcp", r["install_id"], r.get("call_id")), "keep_old", None),
        # 意见反馈按 feedback_id 去重（同一 feedback_id 重传只留首次，与接收端幂等同键）
        ("feedback.jsonl", feedback_rows,
         lambda r: ("fb", r["install_id"], r.get("feedback_id")), "keep_old", None),
        # 候选：稳定键合并 + 统一回填在写出前已完成（含窗口种子），本次合并结果即真源
        ("benchmark_candidates.jsonl", candidates, lambda r: ("cand", r["packet_id"], r.get("record_id")),
         "prefer_rev", "rating_rev"),
        ("agent_trajectories.jsonl", agent_trajectories,
         lambda r: ("traj", r["packet_id"], r.get("record_id")), "keep_old", None),
        ("quarantine.jsonl", quarantined,
         lambda r: ("q", r["packet_id"], r.get("record_id"), r["reason"]), "keep_old", None),
    ]
    merged_views: dict[str, list[dict[str, Any]]] = {}
    for fname, rows, key_fn, on_conflict, rev_key in products:
        # 标签/候选的旧行已在上面作种子并入终态，不再二次 merge（二次 merge 的同 rev
        # 整行取舍会吞掉新累积字段）；其余产物维持窗口增量的旧行保留语义。
        product_merge = merge_old and fname not in ("explicit_labels.jsonl", "benchmark_candidates.jsonl")
        merged_views[fname] = _write_product(out_dir / fname, rows, key_fn,
                                             merge=product_merge, on_conflict=on_conflict,
                                             rev_key=rev_key)
    if accepted is not None:
        # final 从终态候选过滤后整份重写（不 merge 旧 final）：显式空名单 = 撤销全员 → 空 final；
        # 未指定（None）= 不创建/不改写既有 final。
        final = [c for c in merged_views["benchmark_candidates.jsonl"]
                 if str(c.get("record_id")) in accepted or c.get("tid") in accepted]
        _write_jsonl(out_dir / FINAL_CANDIDATES, final)
    (out_dir / "quality_report.md").write_text(_quality_report(report), encoding="utf-8")
    (out_dir / "review.html").write_text(
        _review_html(merged_views["impressions.jsonl"], merged_views["interactions.jsonl"],
                     merged_views["turns.jsonl"], merged_views["mcp_calls.jsonl"],
                     merged_views["quarantine.jsonl"], report["generated_at"]),
        encoding="utf-8")

    # --- 增量状态（watermark 信息性：全量扫描不据此过滤；供运维观测最近一次跑到哪）---
    if incremental:
        watermark = max(int(report["max_packet_id"] or 0), prev_watermark or 0)
        state_path.write_text(json.dumps({"watermark": watermark,
                                          "last_run": report["generated_at"]}, ensure_ascii=False),
                              encoding="utf-8")
    return {"report": report, "impressions": impressions, "interactions": interactions, "turns": turns,
            "explicit_labels": explicit_labels, "mcp_calls": mcp_calls, "candidates": candidates,
            "agent_trajectories": agent_trajectories,
            "quarantined": quarantined, "feedback": feedback_rows}


def _masked_flatten(rec: dict[str, Any]) -> dict[str, Any]:
    """遮蔽后 benchfb 记录的扁平视图：去掉顶层已抽出的包字段（保留业务字段原样）。"""
    return {k: v for k, v in rec.items() if k not in ("_install", "_src")}


def _row_rev(row: dict[str, Any], rev_key: str | None) -> int:
    """行上的修订号；缺省/非整数视为 -1（benchfb 内嵌兜底没有 rev）。"""
    if not rev_key:
        return -1
    value = row.get(rev_key)
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _write_product(path: Path, rows: list[dict[str, Any]], key_fn, *, merge: bool,
                   on_conflict: str = "keep_old",
                   rev_key: str | None = None) -> list[dict[str, Any]]:
    """写单个 JSONL 产物：按稳定行键 upsert，整份原子替换。

    merge=True 时先读旧文件参与合并（仅窗口增量需要保留窗口外的既有行）；merge=False
    时本次扫描即真源——已从库里清除的行随之真正退场，而不是永远拼在旧文件后面。

    on_conflict：同键新旧行取舍——"keep_old" 先到先得（同键行同形，无信息差）；
    "prefer_enriched" 时新行带旧行没有的 seen 键才覆盖（impressions 的 view 补充晚到场景）；
    "prefer_rev" 时按 rev_key（rev / rating_rev）取最高 rev，改口/纠正的新分必须真正落盘。
    返回最终写出的行列表（供 review.html / final candidates 复用）。
    """
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    if merge and path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            r = json.loads(ln)
            merged[key_fn(r)] = r
    for r in rows:
        k = key_fn(r)
        if k not in merged:
            merged[k] = r
        elif on_conflict == "prefer_enriched" and "seen" in r and "seen" not in merged[k]:
            merged[k] = r
        elif on_conflict == "prefer_rev" and _row_rev(r, rev_key) > _row_rev(merged[k], rev_key):
            merged[k] = r
    out_rows = list(merged.values())
    _write_jsonl(path, out_rows)
    return out_rows


def _replace_with_retry(temp_name: str, path: Path, *, attempts: int = 5) -> None:
    """os.replace 的有界重试：Windows 上扫描器/索引器可能瞬时持有目标句柄（拒绝访问）。

    重试同一替换不改变原子性（读者看到的仍是旧文件或新文件）；用尽后原样抛出。
    """
    for attempt in range(attempts):
        try:
            os.replace(temp_name, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """临时文件 + os.replace 原子替换；显式 LF（产物跨平台字节可比，读者不见半截文件）。"""
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        _replace_with_retry(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="遥测落库数据 → benchmark 原料导出（只读消费 ingest_packets）")
    ap.add_argument("--dsn", default=None, help="PG 连接串或 SQLite 文件路径；缺省读 env BIODATA_TELEMETRY_DSN")
    ap.add_argument("--out", default="telemetry_export_out", help="输出目录（默认 ./telemetry_export_out）")
    ap.add_argument("--since", default=None, help="received_at 下限（ISO 时间串）")
    ap.add_argument("--until", default=None, help="received_at 上限（ISO 时间串）")
    ap.add_argument("--incremental", action="store_true",
                    help="增量续跑：产物按稳定行键 upsert + 原子替换（重复跑不重复产出）；"
                         "全库扫描以本次扫描为真源——库中已清除的行（含合法的空库/最后一包过期）"
                         "原子退场；带 --since/--until 时保留窗口外既有行（未扫描≠已删除，"
                         "退场责任在定期全库扫描）；watermark 仅信息性记录（跨包 join 要求全量扫描）")
    ap.add_argument("--accepted", default=None, help="人工接受名单文件（每行一个记录 id；# 开头为注释）→ 只写 benchmark_candidates.final.jsonl")
    args = ap.parse_args(argv)

    dsn = args.dsn or os.environ.get("BIODATA_TELEMETRY_DSN")
    if not dsn:
        print("error: 需要 --dsn 或 env BIODATA_TELEMETRY_DSN", file=sys.stderr)
        return 2
    missing = _missing_sqlite_source(dsn)
    if missing is not None:
        # 源缺失 fail-loud：不打开、不建库、不动既有产物——"读不到"绝不等价于"空库"。
        print(f"error: SQLite 源文件不存在或 DSN 未给出库路径：{missing}（拒绝导出，既有产物保持原样）",
              file=sys.stderr)
        return 2
    accepted: set[str] | None = None
    if args.accepted:
        try:
            accepted = {ln.strip() for ln in Path(args.accepted).read_text(encoding="utf-8").splitlines()
                        if ln.strip() and not ln.strip().startswith("#")}
        except OSError as exc:
            print(f"error: 读接受名单失败：{exc}", file=sys.stderr)
            return 2

    engine = _make_engine(dsn)
    try:
        out = _export(engine, Path(args.out), since=_parse_iso(args.since), until=_parse_iso(args.until),
                      incremental=args.incremental, accepted_ids=accepted)
    except Exception as exc:  # noqa: BLE001 —— 连接/查询失败统一报错退出
        print(f"error: 导出失败：{exc}", file=sys.stderr)
        return 1

    r = out["report"]
    print(f"[telemetry-export] 处理 {r['packets_processed']} 包 → 曝光 {r['impressions_total']}、"
          f"交互 {r['interactions_total']}（join {r['interactions_joined']}）、轮次 {r['turns_total']}、"
          f"标注 {r['labels_total']}、agent 轨迹 {r['agent_trajectories_total']}、"
          f"mcp {r['mcp_total']}、意见 {r['feedback_total']}、"
          f"quarantine {r['quarantine_total']}。")
    print(f"[telemetry-export] 产物：{Path(args.out) / 'impressions.jsonl'} 等（见 quality_report.md）。")
    if accepted is not None:
        print(f"[telemetry-export] --accepted：名单 {len(accepted)} 个 id → {FINAL_CANDIDATES}。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
