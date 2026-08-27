# -*- coding: utf-8 -*-
"""规则主力基线解析器（评测对照实现，**不进 src/**）。

把一句用户原话确定性地解析成统一对话窗口的路由 + 管护动作提议：

    {"id", "route", "action", "slots", "abstain", "confidence", "matched", "reason"}

route ∈ search / refine / curate.list / curate.import / curate.search_online /
curate.remove / curate.restore / clarify / oos。
action ∈ "plan" / null（两步确认纪律：解析层**只提议 plan**，绝不从单句原话直接提议 apply）。

fail-closed 设计（与 src/dataset_recommender/agent/action_plan.py 同口径、不造冲突）：
  - 管护路由必须命中显式关键词表；检索是默认管线，绝不反向把管护词塞进检索。
  - 极性门复刻 action_plan：执行关键词前 4 字窗口内出现否定语素（与
    vocabulary.NEG_MORPHEMES_CN 同一份，程序引用不手抄）→ 弃权（abstain=True）。
  - 多锚点时沿用「每一个锚点都被否定才判否定」（action_plan.polarity_blocked 的 all 语义）。
  - 指代缺失（remove 只有裸「那个」无限定语/无文件名）→ 弃权，不猜目标。
  - 判不清的一律 clarify + 弃权，宁可反问也不猜着执行。

用法：
  $Python eval/curate_nlu/rule_parser.py                      # cases.json → results_rule.jsonl
  $Python eval/curate_nlu/rule_parser.py --out results.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

try:  # 与 action_plan 极性门同一份语素表（程序引用，防手抄漂移）
    from dataset_recommender.retrieval import vocabulary as _V
    NEG_MORPHEMES_CN = _V.NEG_MORPHEMES_CN
except Exception:  # pragma: no cover - 仓库结构变化时的兜底（口径见 vocabulary.py:740）
    NEG_MORPHEMES_CN = ("不要", "不用", "先别", "没有", "别", "不", "没", "无")

ROUTES = (
    "search", "refine", "curate.list", "curate.import", "curate.search_online",
    "curate.remove", "curate.restore", "clarify", "oos",
)
#: 执行性路由：must_not_exec 案例被判成这些且未弃权 → 安全违规（curate.list 只读，不算执行）。
EXEC_ROUTES = ("curate.import", "curate.search_online", "curate.remove", "curate.restore")

_NEG_WINDOW_CN = 4  # 与 action_plan._NEG_WINDOW_CN 一致

# ---------------------------------------------------------------- 模式表（顺序即优先级）

#: 域外/闲聊/概念问答：最先判，宁可错放 clarify 不可进执行。
_OOS_PATTERNS = ("天气", "你是谁", "什么意思", "啥区别", "什么区别", "你好", "吃了吗")

#: 恢复：先于 remove 判（「把删掉的找回来」含「删」但诉求是恢复）。
_RESTORE_PATTERNS = ("恢复", "找回", "移回", "还原", "撤销删除", "反悔")

#: 删除（回收站式）：长词优先。
_REMOVE_PATTERNS = ("移入回收站", "删掉", "删除", "删了", "移除", "清空", "删")

#: remove 的可执行指代限定语：文件名或这些限定语至少有其一，否则视为裸指代 → 弃权。
_REMOVE_REFERENTS = ("刚传的", "我刚传的", "上次导入的", "昨天传的", "上传的", "入库文件",
                     "测试文件", "那个文件", "自己加的", "刚传的")

#: 清点：先于 import 判（「我之前导入的 JSON 还在吗」含「导入」但是问句）。全是疑问/列举句式。
_LIST_PATTERNS = ("上传过", "传过什么", "哪些来源", "清点", "列一下", "还在吗",
                  "回收站里", "自己加的")

#: 导入：刻意不收裸「加进来」（「搜到就加进来」是联网搜的自然结果，不是本地导入指令）。
_IMPORT_PATTERNS = ("导入", "入库", "加进库", "加进去", "加到外部库", "加到库里", "加到库")

#: 联网搜：在线标记，或显式官方源名（已注册适配器键）。本地「搜一下」没有这些标记 → search。
_ONLINE_MARKERS = ("上网", "联网", "在线", "网上", "网搜")
_SOURCE_MARKERS = {"arrayexpress": "arrayexpress"}

#: 连续对话改条件。
_REFINE_PATTERNS = ("只要", "去掉", "别要", "换成", "再加", "不限", "放宽", "改成", "只看", "排除")

#: 检索兜底：编号/DOI 直查，或 领域词 × 检索动词，或裸「数据集」诉求。
_ACCESSION_RE = re.compile(r"(GSE\d{3,}|E-MTAB-\d+|E-GEOD-\d+|10\.\d{4,9}/\S+)", re.I)
_DOMAIN_TERMS = ("数据集", "单细胞", "图谱", "数据", "肿瘤", "癌", "瘤", "脑", "肺", "肝",
                 "心脏", "肾", "结肠", "脾", "乳腺", "黑色素瘤", "空间转录组",
                 "fastq", "visium", "xenium", "10x")
_RETRIEVAL_VERBS = ("找", "搜", "查", "推荐", "看看", "调出", "检索", "有哪些", "有多少", "有没有")

# ---------------------------------------------------------------- 槽位

_FILENAME_RE = re.compile(r"[\w\-.一-鿿]+\.json", re.I)
_LIMIT_RE = re.compile(r"(\d+)\s*条")
_SPECIES_MAP = (("人类", "Human"), ("人的", "Human"), ("小鼠", "Mouse"),
                ("大鼠", "Rat"), ("斑马鱼", "Zebrafish"))

_QUERY_STRIP_TOKENS = (
    "能不能", "帮我", "麻烦", "请", "你",
    "去网上", "在网上", "上网", "联网", "在线", "网上", "网搜",
    "arrayexpress", "ebi",
    "搜搜", "搜索", "搜一下", "找找", "查找", "找一下", "检索一下", "检索", "查一下", "搜", "找", "查",
    "有没有", "新的", "一下",
    "人类", "人的", "小鼠的", "小鼠", "大鼠的", "斑马鱼的", "斑马鱼",
)
_QUERY_STRIP_CHARS = " 的上里在把个呢吧，。！？、：:；;"


def _extract_filename(text: str) -> str:
    m = _FILENAME_RE.search(text)
    return m.group(0) if m else ""


def _extract_species(text: str) -> str:
    for zh, en in _SPECIES_MAP:
        if zh in text:
            return en
    return ""


def _extract_limit(text: str) -> int | None:
    m = _LIMIT_RE.search(text)
    return int(m.group(1)) if m else None


def _extract_query(text: str) -> str:
    """search_online 的内容词：剥掉在线标记/源名/动词/物种条件后的残余短语（启发式，供包含式比对）。"""
    q = re.split(r"[，。！？；]", text)[0]
    q = re.sub(r"(给我|来)\s*\d+\s*条", "", q)
    for token in _QUERY_STRIP_TOKENS:
        q = q.replace(token, " " if token.isascii() else "")
    return q.strip(_QUERY_STRIP_CHARS)


# ---------------------------------------------------------------- 极性门（复刻 action_plan 语义）

#: 疑问格式重叠词（「能不能上网搜一下」是征询不是否定）：扫描前等长掩码，锚点位置不变。
#: 2026-08-10 再同步：与 `action_plan._QUESTION_HEDGES` **逐字一致**（含「要不」；按长度降序，
#: 「要不要」含「要不」先消费长词）——此前本表缺「要不」且注释误称生产侧没有这层掩码
#: （生产自 2026-08-01 就有），codex 架构评审实锤孪生漂移。两处同步改；
#: tests/test_curate_nlu_twin_parity.py 是机械差分门。
_QUESTION_HEDGES = ("可不可以", "能不能", "要不要", "该不该", "行不行",
                    "好不好", "可否", "要不")

#: 疑问/陈述用法的「没」不是否定语素（2026-08-09 与 action_plan 极性门同步修：
#: 「更新没，有新增就搜来入库」的疑问「没」会误触发紧邻窗）。口径与
#: `action_plan._INTERROGATIVE_MEI_FIXED/_INTERROGATIVE_MEI_RE` 逐字一致，两处同步改。
_INTERROGATIVE_MEI_FIXED = ("有没有", "了没有", "有没", "了没")
_INTERROGATIVE_MEI_RE = re.compile(
    r"没(?:有)?(?=[，。；！？!?、…~哈嘛吗吧呢呀啊喔哦啦]|$)")

#: 顺承/条件句的「没」（2026-08-15 与 action_plan 极性门同步修，审计 C-4）：
#: 「没找到就联网搜」里「没」修饰前一个动词，不否定「就/才」后面的动作。口径与
#: `action_plan._SEQUENTIAL_MEI_RE` 逐字一致，两处同步改。
_SEQUENTIAL_MEI_RE = re.compile(r"没(?:有)?[^，。；！？!?、…~]{0,5}?[就才]")


def _mask_hedges(text: str) -> str:
    for hedge in _QUESTION_HEDGES:
        text = text.replace(hedge, "　" * len(hedge))
    for fixed in _INTERROGATIVE_MEI_FIXED:
        text = text.replace(fixed, "　" * len(fixed))
    text = _INTERROGATIVE_MEI_RE.sub(lambda m: "　" * len(m.group(0)), text)
    return _SEQUENTIAL_MEI_RE.sub(lambda m: "　" * len(m.group(0)), text)


def _all_blocked(text: str, positions: list[int]) -> str:
    """每一个锚点前 4 字窗口都命中否定语素 → 返回该语素；否则 ""。多锚点 all 语义与
    action_plan.polarity_blocked 一致（「打包前5条，不要引文」里打包不该被连坐）。"""
    masked = _mask_hedges(text)
    hits: list[str] = []
    for i in positions:
        window = masked[max(0, i - _NEG_WINDOW_CN):i]
        hits.append(next((m for m in NEG_MORPHEMES_CN if m in window), ""))
    return hits[0] if hits and all(hits) else ""


def _positions(text: str, patterns: tuple[str, ...]) -> tuple[str, list[int]]:
    """返回（命中的首个模式， 全部命中位置）。长词优先已在表序里体现。"""
    pos: list[int] = []
    first = ""
    for pat in patterns:
        start = 0
        while True:
            i = text.lower().find(pat.lower(), start)
            if i < 0:
                break
            if not first:
                first = pat
            pos.append(i)
            start = i + 1
    return first, sorted(set(pos))


# ---------------------------------------------------------------- 主解析

def parse(utterance: str) -> dict:
    text = str(utterance or "").strip()
    result = {
        "route": "clarify", "action": None, "slots": {},
        "abstain": True, "confidence": "low", "matched": "", "reason": "",
    }

    def finish(route: str, *, matched: str = "", abstain: bool = False,
               confidence: str = "high", reason: str = "", slots: dict | None = None) -> dict:
        result.update({
            "route": route, "matched": matched, "abstain": abstain,
            "confidence": "low" if abstain else confidence, "reason": reason,
            "slots": slots or {},
            "action": ("plan" if route.startswith("curate.") and not abstain else None),
        })
        return result

    if not text:
        return finish("clarify", abstain=True, reason="空输入")

    lower = text.lower()

    # 1) 域外/闲聊/概念问答
    for pat in _OOS_PATTERNS:
        if pat in text:
            return finish("oos", matched=pat, abstain=True, reason=f"域外标记「{pat}」")

    # 2) 管护五类（restore → remove → list → import → search_online，顺序即消歧优先级）
    kw, pos = _positions(text, _RESTORE_PATTERNS)
    if kw:
        blocked = _all_blocked(text, pos)
        if blocked:
            return finish("curate.restore", matched=kw, abstain=True,
                          reason=f"「{kw}」前命中否定语素「{blocked}」")
        slots = {}
        fn = _extract_filename(text)
        if fn:
            slots["filename"] = fn
        return finish("curate.restore", matched=kw, reason=f"恢复标记「{kw}」", slots=slots)

    kw, pos = _positions(text, _REMOVE_PATTERNS)
    if kw:
        blocked = _all_blocked(text, pos)
        if blocked:
            return finish("curate.remove", matched=kw, abstain=True,
                          reason=f"「{kw}」前命中否定语素「{blocked}」")
        slots = {}
        fn = _extract_filename(text)
        if fn:
            slots["filename"] = fn
        if not fn and not any(r in text for r in _REMOVE_REFERENTS):
            return finish("curate.remove", matched=kw, abstain=True,
                          reason="只有裸指代（那个/它），无文件名也无限定语，目标不可定")
        return finish("curate.remove", matched=kw, reason=f"删除标记「{kw}」", slots=slots)

    kw, pos = _positions(text, _LIST_PATTERNS)
    if kw:
        return finish("curate.list", matched=kw, reason=f"清点标记「{kw}」（只读）")

    kw, pos = _positions(text, _IMPORT_PATTERNS)
    if kw:
        blocked = _all_blocked(text, pos)
        if blocked:
            return finish("curate.import", matched=kw, abstain=True,
                          reason=f"「{kw}」前命中否定语素「{blocked}」")
        slots = {}
        fn = _extract_filename(text)
        if fn:
            slots["filename"] = fn
        return finish("curate.import", matched=kw, reason=f"导入标记「{kw}」", slots=slots)

    online_kw, online_pos = _positions(text, _ONLINE_MARKERS)
    source_hit = next((key for key in _SOURCE_MARKERS if key in lower), "")
    if online_kw or source_hit:
        positions = online_pos or [lower.find(source_hit)]
        blocked = _all_blocked(text, positions)
        if blocked:
            return finish("curate.search_online", matched=online_kw or source_hit, abstain=True,
                          reason=f"联网标记前命中否定语素「{blocked}」")
        slots: dict = {}
        if source_hit:
            slots["source"] = _SOURCE_MARKERS[source_hit]
        sp = _extract_species(text)
        if sp:
            slots["species"] = sp
        lim = _extract_limit(text)
        if lim is not None:
            slots["limit"] = lim
        q = _extract_query(text)
        if q:
            slots["query"] = q
        return finish("curate.search_online", matched=online_kw or source_hit,
                      reason=f"联网标记「{online_kw or source_hit}」", slots=slots)

    # 3) 连续对话改条件
    for pat in _REFINE_PATTERNS:
        if pat in text:
            slots = {}
            sp = _extract_species(text)
            if sp:
                slots["species"] = sp
            return finish("refine", matched=pat, reason=f"改条件标记「{pat}」", slots=slots)

    # 4) 检索（默认管线）
    if _ACCESSION_RE.search(text):
        return finish("search", matched="accession", reason="编号/DOI 直查")
    if any(t in lower for t in _DOMAIN_TERMS) and any(v in text for v in _RETRIEVAL_VERBS):
        return finish("search", matched="domain+verb", reason="领域词 × 检索动词")
    if "数据集" in text:
        return finish("search", matched="数据集", confidence="low", reason="裸「数据集」诉求兜底")

    # 5) fail-closed：判不清 → 澄清，不猜着执行
    return finish("clarify", abstain=True, reason="无任何路由标记命中")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="规则主力基线解析器：cases.json → 解析结果 JSONL")
    ap.add_argument("--cases", default=str(_HERE / "cases.json"))
    ap.add_argument("--out", default=str(_HERE / "results_rule.jsonl"))
    args = ap.parse_args(argv)

    payload = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases = payload["cases"] if isinstance(payload, dict) else payload
    out_path = Path(args.out)
    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        for case in cases:
            res = parse(case["utterance"])
            fh.write(json.dumps({"id": case["id"], **res}, ensure_ascii=False) + "\n")
    print(f"已解析 {len(cases)} 条 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
