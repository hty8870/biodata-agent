"""
可选 LLM 重排层（默认关，零权重下载，复用现有 LLM 通道）。

设计约束（与 实现 + 内部验证收敛一致）：
- 只对**已过硬过滤的存活集**重排：输入恒为 survivors 的子集，输出是其**排列/截断**，
  绝不引入集合外记录 → 0% 硬违规由 retriever 的终检(passes_hard_filter)结构性保证，
  本层的 ID 交集守卫是额外冗余（defense-in-depth）。
- 后端 `off`：原样返回（与未启用时**字节等价**）。
- 后端 `llm`：编号候选 → LLM 返回编号排列 → 交集存活集 + 补回遗漏 + 截 top_k；
  任何异常/空/解析失败 → 回退传入顺序（永不报错、永不违规）。

**关键词审核扩展（rerank_audit，opt-in，默认关）**：
- 传入 `audit_ctx`（in/out dict）时，重排那次 LLM 调用的 prompt 额外拼进"对照原句审核规则抽词
  是否正确完整、不完整则改写原句"的指令，一次输出同时给【排序】+【审核/改写】。
- 审核结果写回 `audit_ctx`（verdict/rewrite），供 workflow 决定是否拿改写句重搜一次。
- `audit_ctx=None`（默认）时**整条 audit 路径不触发**，prompt 与输出与今天**逐位一致** →
  既有 rerank=llm 用户与冻结评测（rerank=off）都不受影响。
- LLM 只改**文本**、只给排序，从不决定给哪条数据；改写句仍要过确定性规则解析 + 硬过滤，
  规则层仍是唯一守门员。任何解析失败 → 退回原序、无改写（fail-open）。

本模块不下载任何模型，不新增运行时依赖；LLM 调用复用 llm_client 的现成通道。
本模块**不 import vocabulary/query_parser**（保持检索层解耦）：审核所需的 keywords / vocab_hint
由上层（workflow）算好后经 audit_ctx 传入。
"""
from __future__ import annotations

import contextvars
import json
import re
import sys
import time
from dataclasses import asdict
from typing import Callable, Sequence

from ..llm.llm_client import ZHIPU_PROVIDER_ALIASES, LLMConfig, call_openai_compatible, call_zhipuai, is_auth_error
from .retriever import RetrievedCandidate

RERANK_BACKENDS = ("off", "llm")
DEFAULT_RERANK_TOP_N = 12
# 改写长度上限（字符）：防 LLM 把整段解释塞进 rewrite；超限视为无效改写（择优仍由 workflow 兜底）。
_MAX_REWRITE_LEN = 200


def _candidate_line(idx: int, cand: RetrievedCandidate) -> str:
    r = cand.record
    desc = (r.description or "").strip().replace("\n", " ")
    if len(desc) > 120:
        desc = desc[:120] + "…"
    fields = [
        f"物种:{r.species or '-'}",
        f"组织:{r.tissue or '-'}",
        f"疾病:{r.disease or '-'}",
        f"平台:{r.platform_family or '-'}",
        f"实验:{r.assay or '-'}",
    ]
    return f"{idx}. {r.dataset_name or '(未命名)'} | " + " | ".join(fields) + (f" | {desc}" if desc else "")


def build_rerank_prompt(
    query: str,
    candidates: Sequence[RetrievedCandidate],
    *,
    audit_keywords: str | None = None,
    vocab_hint: str | None = None,
) -> str:
    """构造重排 prompt。

    `audit_keywords is None`（默认）→ **纯重排 prompt**，输出与历史**逐位一致**（要求 JSON 整数数组）。
    `audit_keywords is not None`（含空串）→ **重排 + 关键词审核 prompt**：额外给出规则抽取的关键词与
    （可选）规则可识别规范词表，要求一次输出一个 JSON 对象 {order, keywords_ok, rewrite}。
    """
    lines = [_candidate_line(i + 1, c) for i, c in enumerate(candidates)]
    n = len(candidates)
    if audit_keywords is None:
        # —— 纯重排（逐位不变，勿改此分支字面）——
        return (
            "你是一个检索结果重排器。下面是针对用户查询检索到的候选数据集"
            "（它们已经通过了全部硬性筛选条件，都合规）。\n"
            "请**仅根据与查询的语义相关性**，把它们从最相关到最不相关重新排序。\n\n"
            f"用户查询：{query}\n\n"
            "候选（每行前是编号）：\n" + "\n".join(lines) + "\n\n"
            f"要求：只输出一个 JSON 整数数组，包含 1 到 {n} 的**全部**编号、不重复、不新增，"
            "按相关性从高到低排列，例如 [3,1,2,...]。除这个数组外不要输出任何其它文字。"
        )
    # —— 重排 + 关键词审核 ——
    hint_block = ""
    if vocab_hint and vocab_hint.strip():
        hint_block = (
            "\n\n系统的规则解析器只认识下列规范词（改写时**尽量落到**这些词面上，"
            "让规则能正确识别）：\n" + vocab_hint.strip()
        )
    kw_text = audit_keywords.strip() if audit_keywords.strip() else "（规则未抽到任何结构化关键词）"
    return (
        "你同时是检索结果重排器和查询解析审核员。下面是针对用户查询检索到的候选数据集"
        "（它们已经通过了全部硬性筛选条件，都合规）。\n"
        "系统先用**规则**从用户原始查询里抽取了结构化关键词，规则可能抽错或抽漏。请完成两件事：\n"
        "1) **排序**：仅根据与查询的语义相关性，把候选从最相关到最不相关重新排序。\n"
        "2) **审核关键词**：对照用户原始查询，判断规则抽取的关键词是否**正确且完整**。"
        "若有明显遗漏或错误，请把**用户原始查询**改写成一句规则更容易正确解析的中文查询；"
        "改写必须**语义等价**——只把用户已经表达的意思换成规则更认识的说法，"
        "**绝不新增用户没有表达的物种/组织/疾病/技术等任何条件**。"
        "改写还必须**真正改变规则能抽到的关键词**：若遗漏的是规则**未建模**的概念"
        "（例如'免疫细胞''T细胞'这类具体细胞类型、'肿瘤微环境''发育'等），你无法把它落到"
        "规则维度上，就让 rewrite 为空字符串——**不要只换措辞或加'数据/相关/研究'之类填充词**（那是无意义改写）。"
        "若关键词已正确且完整，则无需改写。\n\n"
        f"用户原始查询：{query}\n"
        f"规则抽取的关键词：{kw_text}"
        f"{hint_block}\n\n"
        "候选（每行前是编号）：\n" + "\n".join(lines) + "\n\n"
        "只输出**一个 JSON 对象**，格式严格如下（不要输出任何其它文字、不要用代码块包裹）：\n"
        '{"order": [排序后的全部编号], "keywords_ok": true 或 false, "rewrite": "改写后的查询或空字符串"}\n'
        f"其中 order 必须是 1 到 {n} 的**全部**编号、不重复、不新增，按相关性从高到低；"
        "keywords_ok=true 表示关键词已正确完整（此时 rewrite 给空字符串 \"\"）；"
        "keywords_ok=false 表示需要改写（此时 rewrite 给改写后的完整查询句）。"
    )


def _sanitize_order(values, n: int) -> list[int]:
    """把一串（可能混杂）编号规整成 **0-based 索引**排列：转 int、1-based→0-based、去重、限 [0,n)、保序。"""
    order: list[int] = []
    seen: set[int] = set()
    for token in values:
        try:
            one_based = int(token)
        except (ValueError, TypeError, OverflowError):
            # OverflowError：JSON 里的 1e400 / Infinity 被 json.loads 解析成 float('inf')，
            # int(inf) 抛 OverflowError（非 ValueError/TypeError 子类）——不接住会穿透 fail-open 链。
            continue
        zero_based = one_based - 1
        if 0 <= zero_based < n and zero_based not in seen:
            seen.add(zero_based)
            order.append(zero_based)
    return order


def parse_order(text: str, n: int) -> list[int]:
    """从 LLM 文本抽出编号排列，返回 **0-based 索引**（去重、限定在 [0,n) 内、按出现顺序）。

    对 LLM 的输出格式极其宽容：抽取所有整数，1-based → 0-based，丢弃越界/重复。
    """
    return _sanitize_order(re.findall(r"\d+", text or ""), n)


def _first_json_object(text: str) -> str | None:
    """截取文本里第一个 `{...}` 块（首个 '{' 到最后一个 '}'）。找不到 → None。"""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    return text[start : end + 1]


# ==================== 解析失败的错误回灌（B3 调研六候选批）====================
# BioChatter ResponderWithRetries 的本落化：LLM 输出机械结构无效时，把失败原因回灌重问
# **一次**（上限硬顶），仍败照旧走既有 fail-open——「闸住之后给一次自修机会」，
# 与本仓 repair（violations 回灌）/ decide（非法重问）同一哲学的检索侧落地。
# 纪律（验证）：
#   · 共享层只是 transport 外壳；每条路径自带 typed validator 判 valid/partial/invalid，互不共用；
#   · 只重试「非空输出且机械结构无效」；空输出 / 异常 / 鉴权错误绝不重试（原语义逐位不变）；
#   · partial（可用但走形：缺号排列、rewrite-only、markers-only）**不重试**——
#     既有的交集守卫 / 宽容解析本就是为它们准备的；
#   · 「10x」这类字母粘连文本不算数字（standalone 判定）——畸形应答不许被当成候选 10。
# 可观测：重试结局落 `_LAST_PARSE_RETRY`（channel → "recovered" / "failed"）；
# `rerank_candidates` 把它拷进 trace 的**附加字段** parse_retry——不覆盖既有 reason。
# ContextVar 而不是模块级 dict（触发点审计 D-01，与下方 _LAST_LLM_ERROR 同型）：
# `/api/recommend` 是 sync def 走 anyio 线程池，模块级 dict 让两路并发请求互踩
# （A 写 "recovered" → B 覆写 "failed" → A pop 到 B 的结局，trace 张冠李戴）；
# 且 query_audit/action_audit/drop_terms 三渠道写入后无人消费，模块级残留会跨请求存活，
# ContextVar 每请求各拿一份默认值，残留出不了本请求。
# 读写必须「拷贝-改写-set」：ContextVar 的 default dict 对象本身跨上下文共享，
# 原地 mutate 就等于又变回模块级共享。
_LAST_PARSE_RETRY: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "rerank_last_parse_retry", default={}
)


def _parse_retry_put(channel: str, outcome: str) -> None:
    _LAST_PARSE_RETRY.set({**_LAST_PARSE_RETRY.get(), channel: outcome})


def _parse_retry_take(channel: str) -> "str | None":
    """读出并清除**本上下文**里该渠道的重试结局（无 → None）。"""
    outcomes = dict(_LAST_PARSE_RETRY.get())
    outcome = outcomes.pop(channel, None)
    _LAST_PARSE_RETRY.set(outcomes)
    return outcome


def _standalone_numbers(text: str) -> list[str]:
    """standalone 数字（两侧都不粘连单词字符）：「3, 1, 2」命中；「10x」「GSE123」不命中。"""
    return re.findall(r"(?<![\w])\d+(?![\w])", text or "")


def _maybe_retry_parse(first_text, prompt: str, *, caller, validate, contract_zh: str, channel: str):
    """非空输出经 validate 判 invalid → 形状契约回灌重问一次；返回最终应采用的那段文本。

    任何不重试的情形（首答非 invalid / caller 抛异常 / 二答为空或非字符串 / 二答仍 invalid）
    都原样返回首答——下游宽容解析链行为与历史逐位一致。
    """
    if not isinstance(first_text, str) or not first_text.strip():
        return first_text
    if validate(first_text) != "invalid":
        _parse_retry_take(channel)
        return first_text
    feedback = (
        prompt + "\n\n（系统回执：你上一次的输出机械结构无效，无法解析。"
        + contract_zh + "除这个 JSON 外不要输出任何其它文字。）"
    )
    try:
        second = caller(feedback)
    except Exception:
        second = None
    if isinstance(second, str) and second.strip() and validate(second) != "invalid":
        _parse_retry_put(channel, "recovered")
        return second
    _parse_retry_put(channel, "failed")
    return first_text


def _grade_order_text(text: str, n: int) -> str:
    """纯重排输出分级：JSON 数组且 1..n 全覆盖 → valid；有 standalone 数字（宽容链可消费）
    → partial；啥都提不出 → invalid。"""
    if not _standalone_numbers(text):
        return "invalid"
    try:
        arr = json.loads((text or "").strip())
        if isinstance(arr, list):
            got = {int(x) for x in arr if isinstance(x, (int, float)) and not isinstance(x, bool)}
            if len(got) == n:
                return "valid"
    except Exception:
        pass
    return "partial"


def _grade_audit_text(text: str, n: int) -> str:
    """重排+审核版输出分级：JSON 对象拿到 order 列表 / keywords_ok 布尔 / 非空 rewrite →
    partial（可消费，order 缺失时既有链会兜底抽数字）；纯数字残留 → partial；啥都没有 → invalid。"""
    obj = None
    for candidate in (text, _first_json_object(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            obj = parsed
            break
    if obj is not None:
        if isinstance(obj.get("order"), list):
            return "valid"
        if isinstance(obj.get("keywords_ok"), bool):
            return "partial"
        if isinstance(obj.get("rewrite"), str) and obj["rewrite"].strip():
            return "partial"
    return "partial" if _standalone_numbers(text) else "invalid"


def _grade_action_audit_text(text: str) -> str:
    """执行侧核对输出：is_action 布尔 → valid；markers-only（可消费降级）→ partial；空 → invalid。"""
    is_action, markers, _reason = parse_action_audit_response(text)
    if is_action is not None:
        return "valid"
    return "partial" if markers else "invalid"


def _grade_drop_terms_text(text: str) -> str:
    """未收录词把关输出：drop_ok 布尔 → valid；否则 invalid（fail-closed 闸消费不了 reason-only）。"""
    drop_ok, _reason = parse_drop_terms_response(text)
    return "valid" if drop_ok is not None else "invalid"


def parse_audit_response(text: str, n: int) -> tuple[list[int], bool | None, str]:
    """从 audit 版输出解析 (order, keywords_ok, rewrite)。**极其宽容、绝不抛异常**。

    - 优先当作 JSON 对象解析（原文 → 截取的 `{...}` 块，两次尝试）。
    - 拿到合法 order（JSON 里的 list）→ 用之；否则**退化**用 parse_order 从整段文本兜底抽排列
      （保住重排能力，即便审核字段没解析出来）。
    - keywords_ok 仅接受布尔；rewrite 仅接受非空字符串（strip 后）。
    """
    order: list[int] = []
    keywords_ok: bool | None = None
    rewrite = ""

    obj = None
    for candidate in (text, _first_json_object(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            obj = parsed
            break

    if obj is not None:
        raw_order = obj.get("order")
        if isinstance(raw_order, list):
            order = _sanitize_order(raw_order, n)
        ok = obj.get("keywords_ok")
        if isinstance(ok, bool):
            keywords_ok = ok
        rw = obj.get("rewrite")
        if isinstance(rw, str):
            rewrite = rw.strip()

    if not order:
        # JSON 未给出合法 order → 从整段文本兜底抽数字排列（重排不因审核格式问题而失效）
        order = parse_order(text, n)

    return order, keywords_ok, rewrite


def apply_order(
    order: list[int], candidates: Sequence[RetrievedCandidate]
) -> list[RetrievedCandidate]:
    """按解析出的顺序重排；**补回任何被 LLM 遗漏的候选**（按原顺序追加）。

    输出严格是输入 candidates 的一个排列（长度不变、元素不变），保证不丢存活集成员、
    不引入集合外成员。截断由调用方决定。
    """
    picked = [candidates[i] for i in order]
    included = set(order)
    for i, cand in enumerate(candidates):
        if i not in included:
            picked.append(cand)
    return picked


def _default_llm_call_with_error(prompt: str, config: LLMConfig | None) -> "tuple[str | None, str | None]":
    """同 _default_llm_call 的真身，但把 provider 错误串一并带回（成功时错误为 None）。

    错误串的用途只有一个：回退归因把「密钥无效（401/403）」与「临时故障（超时/5xx/空回）」
    分开（C3）——前者重试不自愈，用户该去改设置；混成一句「调用失败」时，
    密钥坏了的人会对着「稍后再试」干等。
    """
    if config is None or not getattr(config, "api_key", None):
        return None, None
    cfg = LLMConfig(**asdict(config))
    cfg.temperature = 0.0          # 求确定性（注意：DeepSeek 服务端仍不完全可复现）
    cfg.enable_llm = True
    cfg.mock_llm = False
    provider = (config.provider or "").strip().lower()
    if provider in ZHIPU_PROVIDER_ALIASES:   # 别名集单一真源在 llm_client，勿再抄字面量
        result = call_zhipuai(prompt, cfg)
    else:
        result = call_openai_compatible(prompt, cfg)
    return (result.text if (result.succeeded and result.text) else None), result.error


# 最近一次**真实**默认通道调用的 provider 错误串（无 → None）。只服务回退归因分档；
# 私有、调用前清、随调用写——测试用替身换掉 _default_llm_call 时没有错误面（保持 None），
# 归因自动落回临时故障档，与既有钉死行为逐位一致。
# ContextVar 而不是模块级槽（验证-arch）：`/api/recommend` 是 sync def
# 走 anyio 线程池，两路并发检索共享一个模块全局会互踩——A 清槽 → B 清槽 → A 写 → B 写 → A 读，
# A 的 401 可能被读成 B 的超时，「密钥无效」误标成「临时故障」（用户被指路去白等）。
# ContextVar 随 anyio 的上下文快照走，每个请求各拿一份，互不串；同一线程内清→写→读是同步连续
# 执行、无 await 让出，也不可能自踩。
_LAST_LLM_ERROR: contextvars.ContextVar["str | None"] = contextvars.ContextVar(
    "rerank_last_llm_error", default=None
)


def _default_llm_call(prompt: str, config: LLMConfig | None) -> str | None:
    """用现有 provider 通道跑一次原始 chat；失败/无 key → None（触发回退）。

    本名字是**补丁缝**（audit/降级测试的 monkeypatch 都打在这里）——rerank 的默认通道
    必须经它调用，替身才生效；错误串走 _LAST_LLM_ERROR 旁路，不改动本函数的返回契约。"""
    text, error = _default_llm_call_with_error(prompt, config)
    _LAST_LLM_ERROR.set(error)
    return text


def _default_llm_call_capture_error(prompt: str, config: LLMConfig | None) -> "tuple[str | None, str | None]":
    """默认通道的调用入口（rerank 专用）：经补丁缝 _default_llm_call 取文本，
    同时把真身写下的错误串一并读出。调用前清槽，缝上替身时错误恒 None。"""
    _LAST_LLM_ERROR.set(None)
    text = _default_llm_call(prompt, config)
    return text, _LAST_LLM_ERROR.get()


# 改写"空转"识别用的填充词（都**不是**规则会建模的实义维度词——物种/组织/疾病/平台/技术/模态/时间）。
# 去掉它们 + 标点空白后若与原句同核，说明 LLM 只是换措辞/加"数据"这类废话、没改变规则能抽到的关键词。
# 顺序敏感：长词在前（"数据集""我想找"必须先于"数据""想找"replace，否则残留碎片）。
_REWRITE_FILLER = (
    "数据集", "数据资料", "数据", "资料", "信息", "相关", "研究", "图谱",
    "我想找", "我想", "想找", "帮我找", "帮我", "请帮", "一些", "关于", "有关",
    "请", "帮", "找一下", "找找", "找", "的", "了", "吧", "呢",
)


def _rewrite_core(s: str) -> str:
    """把查询归一成"规则关心的核心"：小写 → 去填充词 → 只留字母/数字/汉字。
    仅用于**空转改写**判定（同核=没改变规则可抽的东西），绝不参与真实解析。"""
    s = (s or "").strip().lower()
    for f in _REWRITE_FILLER:
        s = s.replace(f, "")
    return re.sub(r"[^0-9a-z一-鿿]", "", s)


def _validated_rewrite(rewrite: str, query: str) -> str:
    """把 LLM 给的改写规整成"可用改写或空串"。
    空 / 与原句等价 / 超长 / **空转（去填充词后同核）** → 视为无改写。"""
    rw = (rewrite or "").strip()
    if not rw:
        return ""
    if rw == (query or "").strip():
        return ""
    if len(rw) > _MAX_REWRITE_LEN:
        return ""
    # 空转改写：LLM 改不进规则维度（如"免疫细胞""肿瘤微环境"这类未建模概念）时，常憋出"原句+数据"
    # 之类的伪改写——去掉填充词/标点/空白后核心与原句一致。采纳它只会白跑一次检索 + 弹误导横幅、
    # 结果集不变，故判为无改写（fail-open：宁可不改也不做无意义改写）。
    if _rewrite_core(rw) == _rewrite_core(query):
        return ""
    return rw


# 运行期异常留痕（与 vector_recall._warn_once 同款纪律）：宽 except 兜底绝不打断请求，
# 但异常本体必须至少留一行 stderr——否则「重排静默失效」事后无从归因（审计 D-02）。
_WARNED: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """同一原因只在 stderr 提示一次；绝不抛异常、绝不打断请求。"""
    if key not in _WARNED:
        _WARNED.add(key)
        print(f"[rerank] {message}", file=sys.stderr)


def rerank_candidates(
    query: str,
    candidates: Sequence[RetrievedCandidate],
    backend: str = "off",
    top_k: int | None = None,
    config: LLMConfig | None = None,
    llm_call: Callable[[str], str | None] | None = None,
    *,
    audit_ctx: dict | None = None,
    trace: dict | None = None,
) -> list[RetrievedCandidate]:
    """可选重排（+ 可选关键词审核）。

    backend="off"（默认）→ 原样返回（截断到 top_k，如未传则不截）。
    backend="llm"       → LLM 编号排列 → 交集守卫 → 补回遗漏 → 截 top_k；任何问题回退原序。

    `audit_ctx`（in/out dict，默认 None）：
      - None → **纯重排**，行为与历史逐位一致（下方 audit 分支完全不触发）。
      - dict → 在**同一次**重排 LLM 调用里附带关键词审核。输入键：
            `keywords`（规则抽取的关键词文本，必需）、`vocab_hint`（规则规范词表，可选）。
        审核结果写回同一 dict 的输出键：
            `verdict`（True/False/None）、`rewrite`（已校验的改写句或 ""）、`attempted`（是否真的跑了审核调用）。
        改写句是否采纳（重搜）由上层 workflow 决定，本层只如实surface。

    llm_call 可注入（便于单测）：签名 (prompt)->str|None。默认用 config 走真实通道。
    """
    items = list(candidates)
    started_at = time.perf_counter()

    def _duration_ms() -> int:
        return max(0, int(round((time.perf_counter() - started_at) * 1000)))

    def _mark(status: str, reason: str) -> None:
        if trace is not None:
            trace.update({"status": status, "reason": reason, "duration_ms": _duration_ms()})

    if trace is not None:
        trace.update({
            "backend": backend, "status": "skipped", "reason": "disabled",
            "candidate_count": len(items), "duration_ms": 0,
        })
    if not items or backend == "off" or backend not in RERANK_BACKENDS:
        if trace is not None and not items:
            trace.update({"status": "skipped", "reason": "no_candidates", "duration_ms": _duration_ms()})
        return items[:top_k] if top_k is not None else items

    audit_on = audit_ctx is not None
    if audit_on:
        # 输出键先给安全默认，保证 workflow 无论如何都能读到确定形状。
        audit_ctx.setdefault("attempted", False)
        audit_ctx.setdefault("verdict", None)
        audit_ctx.setdefault("rewrite", "")

    # backend == "llm"
    order_text: str | None = None
    llm_error: str | None = None   # provider 错误串：仅默认通道带回（注入的 llm_call 没有错误面）
    try:
        if audit_on:
            prompt = build_rerank_prompt(
                query, items,
                audit_keywords=str(audit_ctx.get("keywords") or ""),
                vocab_hint=str(audit_ctx.get("vocab_hint") or ""),
            )
        else:
            prompt = build_rerank_prompt(query, items)
        if llm_call is not None:
            order_text = llm_call(prompt)
        else:
            # 默认通道经补丁缝 _default_llm_call（audit/降级测试的替身才生效），
            # 错误串走 _LAST_LLM_ERROR 旁路带出（替身没有错误面 → None → 临时故障档）。
            order_text, llm_error = _default_llm_call_capture_error(prompt, config)
    except Exception as exc:
        order_text = None
        _warn_once(f"rerank_exc::{type(exc).__name__}", f"LLM 重排主调用异常，回退原序：{exc!r}")

    if not order_text:
        # 回退原序、无改写（fail-open）。audit_ctx 保持安全默认。
        # **「没配」与「配了但没成」必须分成两个 reason**：前者对用户是「未启用」（如实，不必当故障报），
        # 后者是真故障（模型名被服务端拒、超时、限流、返回空…）。把两者合成一个 reason 的代价实测过：
        # provider 连着几天返 400，用户看到的摘要却写着「AI 重排本次未启用」——谁都看不出它坏了。
        # 判据是「这一次到底有没有真去调」：注入了 llm_call 就是调用方自带 provider（视为已调用）；
        # 否则看 config 里有没有真 key（load_llm_config 已把 placeholder 脱敏成 None）。
        # 真故障再分两档（C3）：401/403=密钥无效/无权（llm_auth_failed，重试不自愈，
        # 指路去改设置）；超时/5xx/空回=临时故障（llm_call_failed，稍后重试即可）。
        attempted = llm_call is not None or bool(config is not None and getattr(config, "api_key", None))
        if not attempted:
            _mark("fallback", "llm_not_configured")
        else:
            _mark("fallback", "llm_auth_failed" if is_auth_error(llm_error) else "llm_call_failed")
        return items[:top_k] if top_k is not None else items

    # B3（调研六候选批）：非空但机械结构无效的输出 → 错误回灌重问一次；
    # 仍败则拿着首答原文走下方既有宽容解析/回退，行为与历史一致。
    _retry_caller = llm_call if llm_call is not None else (lambda p: _default_llm_call(p, config))
    if audit_on:
        order_text = _maybe_retry_parse(
            order_text, prompt, caller=_retry_caller,
            validate=lambda t: _grade_audit_text(t, len(items)),
            contract_zh=('只输出一个 JSON 对象：{"order": [排序后的全部编号], '
                         '"keywords_ok": true 或 false, "rewrite": "改写后的查询或空字符串"}。'),
            channel="rerank_audit")
    else:
        order_text = _maybe_retry_parse(
            order_text, prompt, caller=_retry_caller,
            validate=lambda t: _grade_order_text(t, len(items)),
            contract_zh=(f"只输出一个 JSON 整数数组，包含 1 到 {len(items)} 的全部编号、"
                         "不重复、不新增，按相关性从高到低排列。"),
            channel="rerank")
    _parse_retry = _parse_retry_take("rerank_audit" if audit_on else "rerank")
    if _parse_retry and trace is not None:
        trace["parse_retry"] = _parse_retry   # 附加字段：不覆盖既有 status/reason

    if audit_on:
        audit_ctx["attempted"] = True
        order, keywords_ok, rewrite = parse_audit_response(order_text, len(items))
        audit_ctx["verdict"] = keywords_ok
        audit_ctx["rewrite"] = _validated_rewrite(rewrite, query)
    else:
        order = parse_order(order_text, len(items))

    if not order:
        _mark("fallback", "invalid_order")
        return items[:top_k] if top_k is not None else items

    reordered = apply_order(order, items)
    _mark("used", "completed")
    return reordered[:top_k] if top_k is not None else reordered


# ==================== 查询级审核（空池 / 规则弃权档）====================
# 空池独立审核档（build_query_audit_prompt / parse_query_audit_response / audit_query_only /
# _grade_query_audit_text） 随「检索工具化」删除——空池救回改由 search.rerun 工具承担
# （agent 显式调用 + 机械择优闸），
# 审核不再脱离重排静默单发。存活集非空的 ride-along 审核（rerank_candidates）保留不变。


# ==================== 执行侧（下载 / 打包 / 导出）关键词命中的 LLM 核对 ====================
# 用户的执行诉求（打包 / 下载脚本 / 导出引文）此前只由规则表 `query_parser.detect_action_markers` 认，
# LLM 开着也不参与。规则表是**裸词匹配**：换个说法（「帮我存成压缩包」「把这几个导出来」）就漏认。
# 这里在 LLM 开启时对**执行侧关键词的命中**做一次核对：LLM 独立判断这句话是不是在要求下载 / 打包 /
# 导出，并列出它据以判断的原文说法，供上层与规则命中对照（漏认 → 也能指路到打包入口）。
#   · 本函数只**核对 + 上报**，不执行任何动作。这是**分层**、不是产品哲学：
# 起「只指路、不代劳」已不再是本项目的底线（该做就做、做了就报），
#     真正的「说了就做」在 `action_plan.plan_action` + 前端 `act.js` 那条独立链路上；
#     检索侧保持「只回意图、不夹带产物」，是为了让自动执行不必改动检索侧任何既有契约与门。
#   · fail-open：无 key / 异常 / 解析不出 → (None, [], "")，规则命中原样保留、行为不变。

def build_action_audit_prompt(query: str, rule_markers: "list[str]") -> str:
    """问 LLM：这句话是不是在要求下载 / 打包 / 导出？据以判断的原文说法有哪些？"""
    rule_text = "、".join(rule_markers) if rule_markers else "（规则没有认出任何执行类说法）"
    return (
        "你在核对一个数据集检索系统的「执行侧关键词命中」。用户用中文提需求；系统除了检索，"
        "还能对检索结果**打包**（生成数据集清单 + 下载脚本 + FAIR 自检 + 引文）。系统先用规则表匹配"
        "用户是否在要求这类执行动作（打包 / 下载脚本 / 导出引文 等），但规则是裸词匹配、换个说法就会漏。\n"
        "请你独立判断这句话里**有没有**在要求「把数据下载 / 打包 / 导出 / 存下来 / 生成下载脚本或引文」"
        "这类执行动作，并列出你据以判断的**原文说法**。\n"
        "判据：\n"
        "· 只有明确要「拿到 / 下载 / 打包 / 导出 / 存下来 / 生成脚本或引文」才算 is_action=true；"
        "只是描述要找什么数据（检索意图）不算。\n"
        "· markers 只填**用户原文里**真正表达执行动作的词或短语，不要编造，也不要填检索条件词。\n\n"
        f"用户原始查询：{query}\n"
        f"规则认到的执行说法：{rule_text}\n\n"
        "只输出**一个 JSON 对象**（不要任何其它文字、不要代码块）：\n"
        '{"is_action": true 或 false, "markers": ["原文说法", ...], "reason": "一句中文理由，20 字以内"}'
    )


def parse_action_audit_response(text: str) -> "tuple[bool | None, list[str], str]":
    """解析执行侧核对输出 → (is_action, markers, reason)。**极其宽容、绝不抛异常**；解析不出 → (None, [], "")。"""
    is_action: "bool | None" = None
    markers: "list[str]" = []
    reason = ""
    for candidate in (text, _first_json_object(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            val = parsed.get("is_action")
            if isinstance(val, bool):
                is_action = val
            mk = parsed.get("markers")
            if isinstance(mk, list):
                markers = [str(m).strip() for m in mk if str(m).strip()][:12]
            why = parsed.get("reason")
            if isinstance(why, str):
                reason = why.strip()[:80]
            break
    return is_action, markers, reason


def audit_action_markers(
    query: str,
    *,
    rule_markers: "list[str] | None" = None,
    config: LLMConfig | None = None,
    llm_call: "Callable[[str], str | None] | None" = None,
) -> "tuple[bool | None, list[str], str]":
    """一次「这句话是不是要下载 / 打包 / 导出」的 LLM 核对 → (is_action, markers, reason)。

    无 key / 异常 / 空输出 → `(None, [], "")`（fail-open）。llm_call 可注入（便于单测）：
    签名 (prompt)->str|None；默认用 config 走真实通道。**本函数只判断、不执行任何下载。**
    """
    prompt = build_action_audit_prompt(query, list(rule_markers or []))
    try:
        caller = llm_call if llm_call is not None else (lambda p: _default_llm_call(p, config))
        text = caller(prompt)
    except Exception:
        text = None
    if not text:
        return None, [], ""
    # B3：非空但解析不出 → 错误回灌重问一次；仍败照旧 (None, [], "") fail-open。
    text = _maybe_retry_parse(
        text, prompt, caller=caller, validate=_grade_action_audit_text,
        contract_zh=('只输出一个 JSON 对象：{"is_action": true 或 false, '
                     '"markers": ["原文说法"], "reason": "一句中文理由，20 字以内"}。'),
        channel="action_audit")
    return parse_action_audit_response(text)


# ======================= 未收录词降级的 LLM 把关（想法 1+2） =======================
# 用户的想法是：LLM 开启时自动放宽规则、并给 LLM「执行零返回」的否决权。
# 这里落成它的**fail-closed 镜像**：默认不降级，LLM 说可以才降级。
#   · LLM 正常工作时，两种写法的结果完全一样（同一个判断点、同一个判据）；
#   · LLM 挂了 / 超时 / 输出解析不出来时，差别是决定性的——
#     「默认降级 + 可否决」会返回 3473 条无关数据，「默认不降级 + 需批准」返回诚实的弃权。
# 本项目的产品底线（冻结评测 nr/adv 组）是后者，所以按后者实现，并在文档里写清这个取舍。

def build_drop_terms_prompt(query: str, ignored_terms: "list[str]", surviving: str, count: int) -> str:
    """问 LLM 一件事：忽略这几个系统不认识的词之后，剩下的检索对用户还有没有意义。"""
    terms = "、".join(f"「{t}」" for t in ignored_terms)
    return (
        "你在给一个数据集检索系统把关。用户的中文查询里有几个词，系统的受控词表里没有收录，"
        "因此系统**没有做检索**（宁可返回空，也不返回违背用户意图的结果）。\n"
        "现在要决定：**忽略这几个词**、只按剩下的条件检索，对用户是否还有意义。\n\n"
        f"用户原始查询：{query}\n"
        f"系统不认识的词：{terms}\n"
        f"忽略之后实际会生效的条件：{surviving or '（没有任何条件）'}\n"
        f"忽略之后的命中条数：{count}\n\n"
        "判据（按这个顺序想）：\n"
        f"· 如果 {terms} 是这次检索的**核心限定**（比如某个具体器官、疾病或物种），"
        "忽略它之后返回的东西和用户要的不是一回事 → 不该忽略。\n"
        f"· 如果 {terms} 只是修饰、研究主题或系统本来就没有对应筛选维度的说法"
        "（如「微环境」「发育」「再生」这类），忽略它不改变检索目标 → 可以忽略。\n"
        "· 剩下的条件太宽泛（比如只剩一个物种）导致命中成百上千条时，"
        "忽略等于把半个库倒给用户 → 不该忽略。\n\n"
        "只输出**一个 JSON 对象**（不要任何其它文字、不要代码块）：\n"
        '{"drop_ok": true 或 false, "reason": "一句中文理由，20 字以内"}'
    )


def parse_drop_terms_response(text: str) -> "tuple[bool | None, str]":
    """→ (drop_ok, reason)。**极其宽容、绝不抛异常**；解析不出 → (None, "") → 上层保持弃权。"""
    drop_ok: "bool | None" = None
    reason = ""
    for candidate in (text, _first_json_object(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            value = parsed.get("drop_ok")
            if isinstance(value, bool):
                drop_ok = value
            why = parsed.get("reason")
            if isinstance(why, str):
                reason = why.strip()[:80]
            break
    return drop_ok, reason


def judge_drop_terms(
    query: str,
    *,
    ignored_terms: "list[str]",
    surviving: str = "",
    count: int = 0,
    config: LLMConfig | None = None,
    llm_call: "Callable[[str], str | None] | None" = None,
) -> "tuple[bool | None, str]":
    """一次「能不能忽略这几个词」的 LLM 调用 → (drop_ok, reason)。

    无 key / 异常 / 空输出 / 解析失败 → `(None, "")`。上层把 None 与 False 一样对待
    （**保持弃权**）——这就是 fail-closed：把关的人不在场时不放行。
    """
    prompt = build_drop_terms_prompt(query, list(ignored_terms), surviving, count)
    try:
        caller = llm_call if llm_call is not None else (lambda p: _default_llm_call(p, config))
        text = caller(prompt)
        if not text:
            return None, ""
        # B3：非空但解析不出 → 错误回灌重问一次；仍败照旧 (None, "") 保持弃权
        # （fail-closed 语义不变——重试只是给把关人多一次说清的机会，不是放宽闸门）。
        text = _maybe_retry_parse(
            text, prompt, caller=caller, validate=_grade_drop_terms_text,
            contract_zh='只输出一个 JSON 对象：{"drop_ok": true 或 false, "reason": "一句中文理由，20 字以内"}。',
            channel="drop_terms")
        # 解析也必须在 try 里：调用方返回**非字符串**（dict / list / 对象）时，
        # parse_drop_terms_response 里的字符串操作会抛 AttributeError 一路冒泡出去，
        # 与本函数「绝不抛异常、失败即 fail-closed」的承诺相反 —— 而 fail-closed 层
        # 自己把整个请求炸掉，比它要防的问题更糟。
        return parse_drop_terms_response(text)
    except Exception:
        return None, ""
