# -*- coding: utf-8 -*-
""" · 一句话执行层：把用户随口说的一句话归一化成**封闭动词表里的一个动作**。

    「人类肺癌数据，打包前5条」 → pack.download{limit:5}
    「存成压缩包给我」           → pack.download          （规则表认不到，这是本层的净增能力）
    「下载量大的数据集有哪些」   → none                    （规则表会命中裸子串「下载」，本层否掉）
    「找肺癌数据，不要打包」     → pack.download + cancelled（极性门：动词照判、标记取消，不装没听懂）

## 本模块**不做**什么（这条分层是整个方案最值钱的判断）

**只出 plan，不执行任何动作。** 不检索、不落盘、不联网取数据、不碰排序。
产物由调用方（网页前端 / MCP 调用方）另一次显式请求产生。

好处是可机械验证的：`tests/test_action_markers.py` 断言 `/api/recommend` 的返回体里
永远没有 `plan_token/pack/zip/download_script`。执行层走**独立端点 + 前端派发**，
那条诚实性分层原样成立，自动执行落地不需要重基线任何既有的门。

## 三条硬约束（都配了测试，删一条就红）

1. **封闭词表**：`VERB_SPECS` 是唯一真源，prompt 里的动词清单**由它程序生成**
   （`build_action_prompt`）。词表外的动词一律进 `rejected[]`、不执行。
2. **`quoted` 必须是用户原话的字面子串**，且**执行类动词没有 `quoted` 就不执行**。
   于是回执里那行「依据你说的『…』」结构上不可能是编的；同时极性门永远有锚点可查。
3. **极性门**：执行词前 ≤4 字内出现否定语素（「不要打包」「别打包了」）→ **动词照判但标
   `cancelled: true`**——执行层据此不执行、只回音（「好，不打包」），而不是整计划降 none
   装没听懂（NLU 实验定稿语义）。
   不加这道门，`quoted="打包"` 会通过字面子串校验，回执就会把**用户明确否定的词**
   当成授权证据引用给他看。门带**征询掩码**：「能不能/要不要」是征询不是否定
   （「能不能上网检索一下」里的「不」不许误触发），掩码词表见 `_QUESTION_HEDGES`。

## `confidence` 的硬约束（堵死旧哲学的回插口）

`confidence == "low"` **仍然执行同一个 verb、同一套参数**，唯一差别是回执标注更醒目、
纠错控件排到最前。不这么钉死的话，「低置信度转确认」这个旧哲学最体面的马甲
会从这个悬空字段的空白处长出来。

## fail-open

LLM 关 / 无 key / mock / 超时 / 输出解析不出 → `rule_fallback_plan`：按规则表**只开清单面板**
（`pack.preview`），**不接落盘动作**，并带一句醒目的「这是按关键词猜的」。
这不是预防性弃权——规则表是裸子串匹配、实测 5 句误报（「去掉批量效应大的」→「批量」），
它确实不知道用户要什么；给了一步可达的出路，而不是把交付收回去。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from ..retrieval import vocabulary as V
from ..llm.llm_client import (
    LLMConfig,
    _normalize_provider,
    call_openai_compatible,
    call_zhipuai,
    load_llm_config,
)
from ..retrieval.query_parser import detect_action_verbs, detect_operation_markers
# 容错 JSON 提取复用 rerank 那一份（本仓库在两份手抄上栽过多次，不开第三份）。
from ..retrieval.rerank import _first_json_object


MAX_UTTERANCE_CHARS = 500
#: 真 LLM 瞬时失败（超时/断流/空回/一次没解析出 JSON）重试**一次**前的停顿秒数。
#: 执行侧「触发不稳定」实测多是一次性网络抖动，不是判不出来——重试一次直接治好一大半；
#: 只在生产路径（未注入 caller）发生，测试替身的调用次数仍是断言对象。
_RETRY_BACKOFF_SECONDS = 0.4
#: 一次最多打包多少条。与 `task_pack.ALLOWED_LIMITS` 的最高档一致（前端用「取上一档池子 +
#: 只勾前 N 条」精确兑现 1..50，见  的 L1 决策记录）。
MAX_LIMIT = 50
#: `rejected[]` 是**大模型自由生成的文本**，会原样渲染进回执那一行。上限只是不让一次跑飞的
#: 输出把回执刷成一屏（已 escapeHtml，不是 XSS 面）。
MAX_REJECTED = 6
MAX_REJECTED_CHARS = 40

EXEC = "exec"
ROUTE = "route"


#: `ActionPlanError.code` 的机器码全集（schema 加固顺手项：从本模块**实际 raise 点**
#: 逐处收集——empty_input=空句；too_large=超 MAX_UTTERANCE_CHARS）。纯类型标注：收窄
#: `__init__` 形参，运行时行为零变化。
ActionPlanCode = Literal["empty_input", "too_large"]


class ActionPlanError(Exception):
    """入参不合法。带 `code` 供上层映射成 HTTP / MCP 错误码。"""

    def __init__(self, code: ActionPlanCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VerbSpec:
    verb: str
    #: 中文动作名。**执行类必须是动词短语**——回执抬头是「已」+ 它（`act.js` 的 `ACT_LEAD.done`），
    #: 填名词短语就会渲染成「已可行性概览」这种不成话的句子。配 `_LEAD_VERBS` + 同名测试。
    zh: str
    kind: str                   #: EXEC（真去做一件事）/ ROUTE（交回别的管线，不算执行）
    when_zh: str                #: 什么时候该选它——**这段话会原样进 prompt**
    slots: tuple[str, ...]      #: 允许携带的槽位
    requires_results: bool      #: 屏上必须先有检索结果才谈得上做


#: 执行类 `zh` 允许的开头动词。加新动作时如果你的词不在这儿，先想清楚「已」+ 它读不读得通，
#: 再把词加进来——这张表存在的意义就是逼你在加词那一刻回头看一眼回执长什么样。
_LEAD_VERBS: tuple[str, ...] = ("打包", "打开", "导出", "整理", "统计", "生成", "下载",
                                "清点", "导入", "联网搜索", "移入", "移回", "检查", "汇报",
                                "检索",
                                # 优化（转正）：rerank 中文名「优化检索词重查」，
                                # 回执「已优化检索词重查」读得通，按测试指引补录开头动词。
                                "优化",
                                # 回滚：curate.rollback 中文名「回滚写操作」，
                                # 回执「已回滚写操作」读得通，按测试指引补录开头动词。
                                "回滚",
                                # 对比/查找（环内四工具批）：compare.datasets
                                # 「对比两个数据集」→ 回执「已对比两个数据集」；compat.find
                                # 「查找兼容数据集」→ 回执「已查找兼容数据集」。
                                "对比", "查找")


#: ====================== 封闭动词表 v1（唯一真源）======================
#:
#: 只收**已核实可执行、且产物可核验**的。刻意不进 v1 的几个，理由都是代码级的：
#:   · `fav.add` —— `core.js toggleFav` 是**开关不是 add**，逐条 toggle 会删掉已收藏的；
#:     localStorage 没有回收站 = 不可逆的数据丢失。
#:   · `compare.open` —— `writeComparePool` 在每次检索时无条件写入，与用户这句话无关；
#:     消费端 `/dataset` 是严格二元并排，带条数的回执必然是假的。
#:   · `scope="all"` —— `download_plan.py` 在 SCOPE_ALL 时不做 FASTQ 排除，而
#:     `download_script.primary_only_sentence()` 不看 scope、硬编码「只列 1 个代表性主文件」
#:     且四处必现。放开会让交付物内部出现写死的假话。v1 钉死 primary。
#:   · `upload_dataset` / `verify_local_assets` / 账户类 —— 不可逆或注入面。
#: 【决策变更记录】删除类原在此排除（2026-07 初版，理由：不可逆）。 经用户**明确授权
#: 推翻**：以**回收站式可逆删除**为前提纳入管护动词（curate.remove 移动进 `.userdata/recycle/` +
#: manifest 账本，curate.restore 可移回——不可逆前提不再成立）。
#: 历史教训保留：不可逆的删除类仍不得进表，
#: 管护对象的删除只准走回收站真源（corpus_curation），且本层依旧只出 plan、不执行。
#: 过夜批（M1 scoped 路由 / M2 RAG 工具组 / M3 多批结果）已**转正**：
#: rank / rerank / route.request 常驻动词表，原三个环境开关连同 OFF 分支一并摘除。


def _local_library_sources_zh() -> str:
    """本地库已收录来源名单（误调用修复：动态注入「联网搜索数据集」的 when_zh）。

    与 check/sync 的 source 候选**同一真源**（`corpus_curation.CHECK_UPDATE_SOURCES`）——
    名单变自动跟随，不手抄第二份拷贝。惰性 import：corpus_curation 体量大，只在生成
    动词表文本时才需要它。"""
    from ..corpus import corpus_curation as _cc
    return " / ".join(str(s["label"]) for s in _cc.CHECK_UPDATE_SOURCES.values())


VERB_SPECS: tuple[VerbSpec, ...] = (
    VerbSpec(
        "pack.download", "打包下载", EXEC,
        "用户要**拿到文件**：打包、下载、存下来、生成压缩包、导出成 zip、发我一份。",
        ("limit",), True,
    ),
    VerbSpec(
        "pack.preview", "打开打包清单", EXEC,
        "用户只想**先看看会打包哪些东西**，还没要文件：看看清单、先预览一下、都包含什么。"
        "（预览的是「将要打包的内容」；看本地**已经下载好的文件 / 下载目录**是 files.show。）",
        ("limit",), True,
    ),
    VerbSpec(
        "cite.export", "导出引文", EXEC,
        "用户要**引文文件**：导出引文、生成参考文献、要 RIS 或 BibTeX、给 EndNote 用。"
        "本动作导出的是**引文文本**（数据集条目，RIS TY-DATA / BibTeX @misc），"
        "不含数据集文件本身——用户要**拿到文件**（打包、压缩包、zip、下载数据文件）"
        "不是它，那是 pack.download。",
        ("limit", "uids"), True,
    ),
    VerbSpec(
        "reuse.pack", "整理投稿材料", EXEC,
        "用户要**投稿要用的那段出处说明**：数据可用性声明、复用出处清单、投稿材料、写进论文的那段。",
        ("limit",), True,
    ),
    VerbSpec(
        "feasibility.run", "统计可行性概览", EXEC,
        "用户在问**这批数据够不够、总量多少、缺什么**：数据量够吗、一共多少细胞、有什么缺口。",
        (), True,
    ),
    VerbSpec(
        "files.show", "打开文件清单", EXEC,
        "用户要看**文件**：某条结果里都有哪些文件、有哪些文件、文件列表、都能下载什么，"
        "或想看看**下载目录 / 已经下载到本地的文件**。"
        "（这一步只处理当前结果里的第 1 条，回执里会写清楚。）",
        (), True,
    ),
    VerbSpec(
        "none", "不是执行诉求", ROUTE,
        "这句话**不是**在要求执行。描述要找什么数据、问某个概念、闲聊、"
        "或者明确说了**不要**做某个动作，都填这个。",
        (), False,
    ),
    VerbSpec(
        "search.new", "当作一次新检索", ROUTE,
        "这是一整句**新的检索需求**，该重新查一次。",
        (), False,
    ),
    VerbSpec(
        "refine.conditions", "改检索条件", ROUTE,
        "用户在**改上一次的条件**：换成小鼠、再加一条、去掉组织限制、放宽疾病。"
        "（前提是现场已有检索——有结果或有当前查询；两者都没有时这句话不成立。）",
        (), False,
    ),
    VerbSpec(
        "lookup.identifier", "按编号直查", ROUTE,
        "用户贴了一个**数据集编号或直链**（GSE… / E-MTAB… / DOI / 来源页链接），要查那一条。",
        (), False,
    ),
    # ---- 管护动词（用户授权纳入；删除类以回收站可逆为前提，见上方决策变更记录）----
    # 只出 plan、不执行：真正执行由调用方走 `/api/curate/*` / MCP `curate_datasets` /
    # CLI `scripts/curate_datasets.py`（plan → confirm_token → apply 两步确认）。
    VerbSpec(
        "curate.list", "清点外部库", EXEC,
        "用户要**清点自己上传/管护的数据**：看看我上传了哪些、外部库里现在有什么、回收站里有什么。",
        (), False,
    ),
    VerbSpec(
        "curate.import", "导入本地数据", EXEC,
        "用户要把**一份本地数据集 JSON 导入外部库**：导入这个文件、把这份数据加进库。",
        (), False,
    ),
    VerbSpec(
        "curate.search_online", "联网搜索数据集", EXEC,
        "用户要**在线搜官方源**找新数据：上网搜一下、在线找找 ArrayExpress 有没有、"
        "看看 GEO 有没有新的人类肺数据。**检查已有来源有没有更新**不是它——"
        "那是 curate.check_updates（只读比对，不搜关键词也不入库）/ curate.sync_updates"
        "（检查+入库一步做完）。"
        f"本地库已收录来源：{_local_library_sources_zh()}——名单内来源的「有没有新发布/"
        "更新入库」不要选本工具（那是 check_updates / sync_updates）。",
        ("source", "keywords", "species"), False,
    ),
    # curate.check_updates：「检查更新」语义从 search_online 剥出、各自专职——
    # 此前动词表把它并进 search_online 的 when_zh，「检查10x是否有更新」被判成联网搜数据集，
    # 前端只能靠正则剥词打补丁（问题记录在案）。
    # 对应能力是只读的 corpus_curation.check_updates（在线比对限有适配器的源，离线快照源如实报告）。
    # 验证 v4 问题 j05：主题问句（「那个肺癌的网上有新的吗」）误选它——
    # when_zh 补对照句：问主题在网上有没有数据是 search_online，问来源有没有更新才是它。
    VerbSpec(
        "curate.check_updates", "检查来源更新", EXEC,
        "用户要**检查某个已有来源/库有没有更新或新数据**：检查10x是否有更新、看看 ENCODE 有没有更新、"
        "库里最近有没有新的。只读检查、不搜关键词、不入库；在线比对仅覆盖有在线适配器的来源，"
        "其余来源如实报告本地快照信息并指路官网核对。"
        "「检查更新，有的话**就直接下载/入库**」（不限定主题）不是它——那是 curate.sync_updates"
        "（检查+入库一步做完）；但**限定了主题**的「若有 X 数据就下载」（点了病种/组织/技术等）"
        "要先选它——检查后由后续步骤按主题联网搜。"
        "问某个**主题**在网上有没有新数据（「那个肺癌的网上有新的吗」——主题是数据、不是来源）"
        "不是它——那是 curate.search_online；问**来源**有没有更新才选它。",
        ("source",), False,
    ),
    # curate.sync_updates（「工作流即工具」批）：「检查更新，若有则下载/入库」这类
    # 固定两步流折叠成的复合工具——步骤顺序写死在 corpus_curation.sync_updates 里（先只读比对、
    # 再把能闭环来源的疑似新增逐编号搜回入库），LLM 只负责选它，不负责编排步骤。
    # 性能批：集成验证（eval/agent_live_report_v1/v2）坐实它有「磁吸效应」——
    # 限定主题的条件下载也被它抢（它不过滤主题，会把所有疑似新增都入库），when_zh 划清边界。
    # 验证 v4 问题 j03：劣质指令（无标点五件事）里「有新增的人类肺数据就搜来入库」
    # 仍被它抢——机械判定口径前置进**首句**（w2a3 复跑坐实缀在句尾时模型在 JSON 兜底档
    # 只读首句样例就磁吸）：原话出现任何主题词就是限定主题，一律两步流。
    VerbSpec(
        "curate.sync_updates", "检查更新并同步入库", EXEC,
        "用户要**检查更新、有新增就直接收进库**（**仅限不限定主题**——原话里出现任何主题词"
        "（疾病/物种/组织/技术等，如「人类肺」「mouse brain」）就是限定主题，限定主题一律 "
        "check_updates + search_online 两步，不选本工具）：检查有没有更新，有的话下载/入库/"
        "同步下来、把新数据加进来。只检查不动手是 curate.check_updates；按主题词在线搜是 "
        "curate.search_online；**限定了主题的「若有 X 数据就下载」也不是它**——那是 "
        "check_updates + search_online 两步（本工具不过滤主题，会把所有疑似新增都入库）。"
        "自动入库只覆盖能在线比对且有入库适配器的来源，其余来源如实写明哪段做不到。",
        ("source",), False,
    ),
    VerbSpec(
        "curate.remove", "移入回收站", EXEC,
        "用户要**删掉自己上传的某份数据**：把那个上传文件删掉、移除这份数据"
        "（回收站式可逆删除，之后还能恢复；官方快照不可经此删除）。",
        ("target",), False,
    ),
    VerbSpec(
        "curate.restore", "移回外部库", EXEC,
        "用户要把**回收站里的文件恢复回外部库**：把删掉的找回来、恢复那个文件、撤销删除。",
        (), False,
    ),
    # curate.db_status：通用化 agent 的第一个非更新检查用例——
    # 只读状态工具（corpus_status.db_status），agent 在 langgraph 图内经 LOOP_TOOLS 直接调用
    # （起 execute 节点；同份产出挂 plan.observation）；未装扩展时前端 runner 走
    # `/api/curate/status` 端点取同一份事实。
    VerbSpec(
        "curate.db_status", "汇报数据库状态", EXEC,
        "用户要知道**库里现在是什么状况**：汇报数据库的当前状态、库里现在有什么、"
        "各来源各有多少条、最近有什么变动、外部库和回收站里有什么。只读汇报，不改动任何文件。",
        (), False,
    ),
    # curate.rollback（回滚动词化）：环内专属写动词——把 trace 快照回退
    # （M4 波2 的 `_run_one` 快照锚 + `trace/rollback.py` 的 CLI 回退）接进 agent 环。
    # 零槽位：回退目标由工具内机械闸定（本轮 steps 里最新一条带 snapshot_id 且未回滚的
    # 写步），模型不许、也无法发明快照 id。
    VerbSpec(
        "curate.rollback", "回滚写操作", EXEC,
        "用户要**撤销/回退刚才做过的写操作**（「刚才那步入库弄错了，撤销掉」「回退到同步"
        "之前」）：把本轮最近一步写操作动过的文件恢复原样（新文件移入回收站、被改/被删的"
        "写回原字节）。只能回滚本轮已执行且留了快照的写步骤；没有可回滚的步会如实说明，"
        "不硬来。不支持『回滚回滚』；重复调用只会继续回退更早的正向写步骤。",
        (), False,
    ),
    # search.rerun（检索工具化 Phase 1）：「换一组查询词重跑检索」从 prompt 背稿
    # 升级为可调用工具——跑的是与主检索同一条本地管线（规则检索 + ride-along 重排），
    # 采纳与否由机械闸裁定（结果集与现状同集、或载荷弄丢已生效筛选条件才如实拒绝、保留
    # 原结果； 用户拍板：命中 0 条也采纳上屏——空结果集就是新条件的诚实答案，
    # 绝不「结果不如当前就否决、保持不变」）。
    VerbSpec(
        "search.rerun", "检索新查询", EXEC,
        "当前结果**零命中或明显跑偏**、或用户要**换个说法/换条件重查**时，换一组查询词把本地库"
        "重新检索一遍（只读语义：不改库；结果集没变会被机械闸拒绝并保留原结果；命中 0 条"
        "也如实采纳上屏——空结果集就是新条件的真实答案）。"
        "槽位 query 必填，填改写后的检索句。",
        ("query",), False,
    ),
)

# rank / rerank（scoped 路由批 M2； 转正常驻）：环内检索工具组。
#   · rank   = 裸新检索 + 可选上屏（display）——与 search.rerun 的界限：rerun 是带
#     机械闸的换词重检（不动），rank 不做择优、只跑标准管线如实回报；
#   · rerank = 坏 query 先经独立 LLM 改写（机械健全性检查兜底）再重检。
# 两者只读、环内专属（无前端 runner，见 FRONTEND_UNWIRED_EXEC_VERBS）。
VERB_SPECS = VERB_SPECS + (
    VerbSpec(
        "rank", "检索数据集", EXEC,
        "用户要**找数据**：检索一下、查一查有没有、搜某某主题/物种/疾病的数据集。"
        "用户等着看结果时 display=true（结果区还空着的首次检索更要上屏，"
        "收尾前至少上屏一次）；只是为后续动作探路时不填。",
        ("query", "display"), False,
    ),
    VerbSpec(
        "rerank", "优化检索词重查", EXEC,
        "当前检索句**质量差**（太口语化、实体写法不规范、中英错位）需要先**优化检索词**"
        "再重查时；改写不通过机械检查会如实退回原句重查。"
        "用户等着看结果时 display=true（收尾前至少上屏一次）。",
        ("query", "reason", "display"), False,
    ),
)

# route.request（M1 逃生口； 转正常驻）：decide 环内元动词——
# 执行中发现路线走错了请求换一条处理路线（search/action/general）。kind=ROUTE：它不是
# 执行诉求（无 quoted 要求），且只会出现在 decide 续步（understand 各套件面不装它），
# 首步 plan 永不命中——turn 的 ROUTE 分支因此不会被它触到。
VERB_SPECS = VERB_SPECS + (
    VerbSpec(
        "route.request", "切换处理路线", ROUTE,
        "执行中发现当前处理路线不对（检索诉求进了动作线 / 动作诉求进了检索线）时，"
        "请求换到正确的路线；每轮至多 1 次（机械闸）。",
        ("target_route", "reason"), False,
    ),
)

# 环内「结果处理」四工具（四工具批；蓝本 rank/rerank 的登记方式）：
# compare.datasets / cite.export（由 plan-only 补上环内执行）/ compat.find / fair.check。
# 三者新增工具环内专属（compare/compat/fair 无前端 runner，见 FRONTEND_UNWIRED_EXEC_VERBS
# 与 PLAN_ACTION_EXCLUDED_VERBS；cite.export 双通道：环内执行 + 保底通道仍走前端 runner）。
# 与 rank 的边界：这三个工具的默认对象是「当前结果第 N 条」/「前两条」——是**拿现有结果
# 做判断**，不重跑检索找新数据；要找新数据是 rank / search.rerun。
VERB_SPECS = VERB_SPECS + (
    VerbSpec(
        "compare.datasets", "对比两个数据集", EXEC,
        "用户要把**两个数据集放在一起对比**（对比前两条、比较这两个、它们有什么不同/"
        "区别、哪个更适合）：只做**元数据字段**的确定性对比（名称/来源/物种/组织/疾病/"
        "平台/技术/chemistry/模态/样本量/发表时间/文件数），结论由系统按字段差异生成，"
        "不评价哪个数据集更好。槽位 a/b 填用户点名的数据集编号或名称，没点名就不填"
        "（缺省 = 当前结果前两条，结论里会说明这个假设）。"
        "「换一批查询词重新检索找数据」不是它——那是 search.rerun / rank。",
        ("a", "b"), False,
    ),
    VerbSpec(
        "compat.find", "查找兼容数据集", EXEC,
        "用户要给某个数据集**找元数据上兼容的其它数据集**（找和它兼容的、能一起分析的、"
        "同物种同平台的）：按**元数据兼容判据**（共享物种 且 chemistry 或平台相同）找同伴，"
        "结论恒带「必要非充分」的诚实边界。槽位 uid 填用户点名的数据集编号或名称，"
        "没点名就不填（缺省 = 当前结果第一条）。"
        "「找某个主题/物种的数据集」是检索（rank / search.rerun）——本工具按兼容判据找"
        "同伴，不重跑检索。",
        ("uid",), False,
    ),
    VerbSpec(
        "fair.check", "检查 FAIR 就绪度", EXEC,
        "用户要**看某个数据集的 FAIR 就绪度 / 投稿能不能用 / 元数据够不够写方法学**："
        "做 13 项 FAIR 元数据自检（Findable/Accessible/Interoperable/Reusable，每项 "
        "pass/partial/unknown + 改进建议 + 投稿数据可用性声明）。衡量的是「这份公开元数据"
        "够不够引用/写方法学」，**不是**官方 FAIR 认证，也不是对数据质量的评价。"
        "槽位 uid 填用户点名的数据集编号或名称，没点名就不填（缺省 = 当前结果第一条）。",
        ("uid",), False,
    ),
)

#: EXEC 类里**前端 act.js 尚无派发器**的动词：此表是 `tests/test_act_frontend.py` 派发表闸的
#: 显式豁免清单——**只准加有独立执行入口的动词**。curate.* 六动词已全部「毕业」：
#: 前四动词 cur1 接线，`curate.restore` 同日随统一对话窗口（uni1）接线毕业，
#: `curate.check_updates`（新增）随同批前端改动接线毕业
#: （act.js `actRunCurateCheckUpdates`：只读、无问卷，POST /api/curate/check-updates → 结果卡）。
#: 豁免机制本身保留：常量仍在、必须是 EXEC 子集——将来新动词可凭独立执行入口加回，
#: 理由写进 `tests/test_action_curate_verbs.py`。
#: search.rerun：独立执行入口 = langgraph 图内 LOOP_TOOLS 工具 +
#: `/api/agent/search-rescue` 端点。**永久豁免、不毕业**（Phase 2 定）：
#: 前端只接了步骤卡渲染（act.js actSearchRerunCardHtml），**刻意不写前端 runner**——
#: 本工具的采纳与否由后端机械闸裁定（同集/条件丢失如实拒绝； 起改空也采纳），
#: 前端 runner 直接打 /api/recommend 会绕过这道闸，等于让 LLM 的改写跳过裁决直接换屏。
#: 环内专属动词，前端无任何独立触发路径。
#: rank / rerank（M2； 转正常驻）：同为环内专属检索工具——上屏
#: 与否由 display 槽与后端批次机制（M3）裁决，前端 runner 会绕过这层口径，同哲学豁免。
#: curate.rollback：回滚目标由机械闸从**本轮 steps 实录**里的快照锚
#: 现定——前端单步 runner 没有这个现场（也没有快照锚可传），同哲学永久豁免。
#: compare.datasets / compat.find / fair.check（四工具批）：环内专属「结果
#: 处理」工具——默认对象是「当前结果第 N 条/前两条」，前端单步 runner 没有当前结果集
#: 这个现场（后端经 ctx 重跑标准管线取数），同哲学永久豁免。cite.export 不上此表：
#: 它双通道（环内执行 + 保底通道前端 runner 仍可用，仅 .ris 缺口由环内执行补上）。
FRONTEND_UNWIRED_EXEC_VERBS: tuple[str, ...] = ("search.rerun", "rank", "rerank",
                                                "curate.rollback",
                                                "compare.datasets", "compat.find",
                                                "fair.check")

VERB_BY_NAME: dict[str, VerbSpec] = {s.verb: s for s in VERB_SPECS}
ACTION_VERBS: tuple[str, ...] = tuple(s.verb for s in VERB_SPECS)
EXEC_VERBS: tuple[str, ...] = tuple(s.verb for s in VERB_SPECS if s.kind == EXEC)
ROUTE_VERBS: tuple[str, ...] = tuple(s.verb for s in VERB_SPECS if s.kind == ROUTE)

#: 携带 `effective_query`（完整检索句）的路由动词——统一管线里「检索指令」的三种形态。
ROUTE_QUERY_VERBS: tuple[str, ...] = ("search.new", "refine.conditions", "lookup.identifier")

#: plan_action 保底通道的排除表（验证 高1）：闭环动词表里的
#: **环内专属**动词——search.rerun / rank / rerank（环内检索工具，前端无独立触发路径，
#: 见 FRONTEND_UNWIRED_EXEC_VERBS）、route.request（换线元动词，只存在于多步环的 decide
#: 裁决层）与 curate.rollback（：回滚目标依赖本轮 steps 实录的快照锚，
#: 单次分流没有这个现场）。plan_action 是单次分流分类（/api/action/plan、MCP plan_action、
#: agent 不可用兜底），没有环内上下文：这些动词进它的提示词就是教模型选一条这里没有
#: 执行面的路，选出来就是「计划说要做、没有任何一端会做」。提示词按 PLAN_ACTION_VERBS
#: 出表、`build_plan_from_raw(allowed_verbs=…)` 按同一张表机械拒（表外动词进 rejected
#: 并降 none，与未知 verb 同一条「不做，但要说」渠道）——提示不是围栏，闸才是。
PLAN_ACTION_EXCLUDED_VERBS: tuple[str, ...] = (
    "search.rerun", "rank", "rerank", "route.request", "curate.rollback",
    # 四工具批：compare.datasets / compat.find / fair.check 是环内专属结果
    # 处理工具——默认对象依赖环内现场（当前结果集 / steps 实录），单次分流没有这个
    # 现场；cite.export 不进此表（保底通道前端 runner 仍在，双通道各司其职）。
    "compare.datasets", "compat.find", "fair.check")
PLAN_ACTION_VERBS: tuple[str, ...] = tuple(
    s.verb for s in VERB_SPECS if s.verb not in PLAN_ACTION_EXCLUDED_VERBS)
_PLAN_ACTION_SPECS: tuple[VerbSpec, ...] = tuple(
    s for s in VERB_SPECS if s.verb not in PLAN_ACTION_EXCLUDED_VERBS)

#: ============ 下一步行动 suggested_recipe allowlist（engagement P6，设计 §5.2）============
#:
#: **单一真源**：结果页阶梯 chips（web/static/js/search/ladder_core.js 的 LADDER_RECIPES）携带
#: 的 recipe id 必须在本表内（前端 ⊆ 本表，契约门 `tests/test_suggested_recipe.py` 钉死）。
#: 每项只允许缩小到**既有已验证能力**——值必须是本文件封闭动词表 VERB_SPECS 里的动词（子集）；
#: 不在本表 → 服务端**忽略回普通路由并如实记录**（recipe_note），绝不发明新动词/新能力。
#: 收窄语义：suggested_recipe 只是「用户点了哪颗建议动作」的确定性 hint——只能缩小动词选择面，
#: 不得绕过参数校验/执行开关/安全闸（cancelled 极性门、agent 关、blocked_reason 一字不动）。
SUGGESTED_RECIPES: dict[str, tuple[str, ...]] = {
    # 结果处理三工具（环内专属，需要 agent 图执行；前端在 AI 执行关闭时隐藏对应 chips）。
    "compare_datasets": ("compare.datasets",),
    "fair_check": ("fair.check",),
    "compat_find": ("compat.find",),
    # 前端 runner 动词（AI 执行关闭也可经规则兜底路由，见 act.js 派发表）。
    "feasibility": ("feasibility.run",),
    "manifest": ("pack.download",),
    "file_list": ("files.show",),
    "reuse_pack": ("reuse.pack",),
}


def resolve_suggested_recipe(recipe: str | None) -> "frozenset[str] | None":
    """allowlist 校验（单一真源）：空/未知 recipe → None（调用方按「未提供」处理并如实记录）；
    合法 → 该 recipe 允许的动词集合（均为 VERB_SPECS 既有能力）。"""
    if not recipe:
        return None
    verbs = SUGGESTED_RECIPES.get(str(recipe).strip())
    if not verbs:
        return None
    return frozenset(verbs)

#: v1 只在**当前这批结果**上执行。`target="selected"` 刻意不做：`results.js` 的
#: `renderResults` 无条件调 `resetTaskPack()`，勾选在每次检索后必被清空，判据不成立。
TARGET_DEFAULT = "results"

#: 恒带的不确定标注。**与 LLM 自评的 confidence 无关**—— 的结论逐字是
#: 「填了字段 ≠ 已核验」：槽值不是本工具核对出来的，就必须说出来。
#:
#: 但**由谁读出来的必须说对**。 在集成截图里抓到：同一张回执上半句写着
#: 「大模型这次没有接上，这一步是按关键词猜的」，下半句写着「以上这几项是**大模型**从你这句话里
#: 读出来的」——因为这行标注当时无条件挂在所有执行类 plan 上，没看 `source`。
#: 一份回执里两句互相打架的话，比没有这句话更糟。
UNCERTAINTY_ZH = "以上这几项是大模型从你这句话里读出来的，本工具没有另外核对。"
UNCERTAINTY_RULE_ZH = "以上这几项是按关键词从你这句话里匹配出来的，本工具没有另外核对。"


def uncertainty_note(source: str) -> str:
    """执行类 plan 的不确定标注：**「没有另外核对」这半句恒在，「谁读的」这半句随 source 变。**"""
    return UNCERTAINTY_ZH if source == "llm" else UNCERTAINTY_RULE_ZH

PLAN_NOTE_ZH = (
    "本工具只说明这一步要做什么，不在这里执行；确认后才会真正产生文件。"
)


# ---------------------------------------------------------------- prompt（动词清单程序生成）

_RULES_ZH = (
    "你在帮一个**单细胞公开数据集检索工具**读懂用户这一句话到底要做什么。\n"
    "这个工具除了检索，还能对**当前这批检索结果**做几件具体的事（见下方动作表）。\n"
    "你的任务：从动作表里挑**恰好一个** verb，并指出你据以判断的**原文片段**。\n"
)

#: 铁律条体（不带序号）。`_CONSTRAINTS_ZH` 由本表程序装配（全表 10 条，与历史文本
#: **逐位一致**）；scoped 收窄面由 `_constraints_zh(verbs)` 按面裁剪——面里没有的动词
#: 不出现在铁律里（验证 高2）。
_CONSTRAINT_RULE_BODIES_ZH: tuple[str, ...] = (
    "verb 只能从上面那张表里选，**不要发明新的**。表里没有对应动作时选 none。",
    "quoted 必须是用户原话里**逐字出现**的一段连续文字，不要改写、不要加字、不要翻译。\n"
    "   选了执行类动作却给不出原文依据时，改选 none。",
    "用户**明确说不做**某个动作时（不要打包、别下载了、先不导入）→ verb **照判那个动作**，"
    "并加 \"cancelled\": true——界面要能回一句「好，不做了」，**不许**改填 none 装没听懂。\n"
    "   否定语素只作用于**其后 4 个字以内**的执行词：「不要了，帮我删掉吧」是否定收尾、"
    "删掉照旧执行，cancelled 填 false。\n"
    "   「能不能/要不要/要不…吧」是**征询**不是否定（「能不能上网检索一下」＝要检索），同样正向。",
    "limit 只在用户**明确说了条数**时填数字（「前5条」→5、「打包20个」→20），"
    "否则填 null。不要把年份、编号、版本号（2020、GSE123456、10x）当条数。",
    "只是在**描述要找什么数据**（哪怕句子里出现「下载量」「文件」「清单」这类词）"
    "→ 那是检索需求，不是执行诉求，verb 填 none 或 search.new。",
    "confidence 如实填：拿不准就填 low。**填 low 不会让这一步被跳过**，"
    "只会让界面把纠错入口排到最前，所以不要因为怕做错就乱填 none。",
    "规则匹配零命中或整句弃权 **不等于** 这句话无效——工具调用句往往零命中"
    "（「检查10x数据库是否有更新」里没有一个检索词）。按语义判断它到底是不是执行诉求，"
    "**不许**因为零命中就把一句执行/管护的话判成 search.new。",
    "选 search.new / refine.conditions / lookup.identifier 时必须同时给 effective_query："
    "一句**完整、可独立执行**的检索句。refine.conditions 是把改动合进「当前查询」后的整句"
    "（其余条件原样保留，只动用户点名的那一项）；search.new 一般就是用户原话"
    "（可剥掉「帮我/请」这类客套，不得增删实际条件）。"
    "effective_query 里**绝不**出现「去掉/换成/再加/放宽」这类操作词——它是检索句，不是指令；"
    "用户点名的条件若**不在**当前条件里，照当前查询原样填，不要把操作词硬写进查询。"
    "none 和执行类一律不填。",
    # 验证 v8 遗留 k08：多事项句的首步排序——模型偶发跳过原话第一件事直接做后面的。
    "一句话里说了**好几件事**（「搜X数据入库，然后检查Y更新，再告诉我库里多少条」）时，"
    "先做原话里**最前面**的那件事；后面的事会有后续步骤接着做，不用一次做完。",
    # 第三批任务6：a18/a21 repeat-10 验证坐实「缺现场」边界双向抖动——
    # a18（无结果要导出引文）2/10 被误判 none（以为没结果就不是执行诉求），
    # a21（无现场说「只看 X 的」）4/10 被误判 refine.conditions（以为改条件无前提）。
    # 规则 10 + 下方两条示例把不对称说破：缺结果不挡动作类；改条件必须有现场。
    "现场**没有检索结果**不改变动作类动词的选择：「把这批结果打包/导出引文/看看有哪些文件」"
    "照样选对应动作——没有结果由系统如实交代，不由你改判 none。但 refine.conditions 以现场"
    "已有检索（有结果或有当前查询）为前提，两者都没有时这句话不成立——选 none。",
)

_CONSTRAINTS_ZH = (
    "铁律（违反任一条都是错误）：\n"
    + "".join(f"{i}. {body}\n" for i, body in enumerate(_CONSTRAINT_RULE_BODIES_ZH, 1))
)

#: 收窄面变体（高2）：面里没有 ROUTE_QUERY 动词时，铁律 5/7/10 换不引用退役动词的
#: 口径、铁律 8 整条退役（面里没有携带 effective_query 的动词，规则留着只会教模型填
#: 一个没有任何动词消费的槽）。与全表条体同段落维护——同源哲学，禁手抄第二份漂移。
_RULE5_RETRIEVAL_TOOL_ZH = (
    "只是在**描述要找什么数据**（哪怕句子里出现「下载量」「文件」「清单」这类词）"
    "→ 那是检索需求，不是执行诉求——选表里的检索动词。"
)
_RULE5_NONE_ONLY_ZH = (
    "只是在**描述要找什么数据**（哪怕句子里出现「下载量」「文件」「清单」这类词）"
    "→ 那是检索需求，不是执行诉求，verb 填 none。"
)
_RULE7_NO_SEARCH_NEW_ZH = (
    "规则匹配零命中或整句弃权 **不等于** 这句话无效——工具调用句往往零命中"
    "（「检查10x数据库是否有更新」里没有一个检索词）。按语义判断它到底是不是执行诉求，"
    "**不许**因为零命中就把一句执行/管护的话误判成检索需求。"
)
_RULE10_NO_REFINE_ZH = (
    "现场**没有检索结果**不改变动作类动词的选择：「把这批结果打包/导出引文/看看有哪些文件」"
    "照样选对应动作——没有结果由系统如实交代，不由你改判 none。"
)


def _constraints_zh(verbs: Any = None) -> str:
    """铁律段。缺省 None = 全表 10 条（`_CONSTRAINTS_ZH`，与历史输出**逐位一致**）；
    显式给 VerbSpec 子集（scoped 收窄面）时按面生成——面里没有的动词不出现在铁律里
    （高2：scoped 的 JSON 兜底壳曾原样带全表铁律，铁律 5/7/8/10 指着
    search.new / refine.conditions / lookup.identifier 三个面内不存在的动词下指令，
    规则与动词表自相矛盾）。裁剪后重新编号，序不留洞。"""
    if verbs is None:
        return _CONSTRAINTS_ZH
    face = {getattr(s, "verb", None) or str(s) for s in verbs}
    has_route_query = bool(set(ROUTE_QUERY_VERBS) & face)
    bodies: list[str] = []
    for i, body in enumerate(_CONSTRAINT_RULE_BODIES_ZH, 1):
        if i == 5 and not has_route_query:
            body = _RULE5_RETRIEVAL_TOOL_ZH if "rank" in face else _RULE5_NONE_ONLY_ZH
        elif i == 7 and "search.new" not in face:
            body = _RULE7_NO_SEARCH_NEW_ZH
        elif i == 8 and not has_route_query:
            continue                    # 面里没有携带 effective_query 的动词，整条退役
        elif i == 10 and "refine.conditions" not in face:
            body = _RULE10_NO_REFINE_ZH
        bodies.append(body)
    return ("铁律（违反任一条都是错误）：\n"
            + "".join(f"{n}. {b}\n" for n, b in enumerate(bodies, 1)))

#: 两条 few-shot 示例（规则 10 的具象化）。**刻意不用验证原句**（a18「把这批结果的引用格式
#: 导出来」/ a21「只看小鼠的」的同义改写）——验证是唯一的集成基准，示例逐字等于泄题。
_EXAMPLES_ZH = (
    "示例一（没有现场检索时，「改条件」不成立）：\n"
    "现场情况：当前屏幕上**还没有**检索结果，也没有当前查询。\n"
    "用户说：只要人类的\n"
    '{"verb": "none", "limit": null, "quoted": "只要人类的", "effective_query": null, '
    '"confidence": "high", "reason": "没有现场检索，改条件不成立"}\n'
    "示例二（没有结果不挡动作类，缺结果由系统交代）：\n"
    "现场情况：当前屏幕上**还没有**检索结果。\n"
    "用户说：把引用格式导出来\n"
    '{"verb": "cite.export", "limit": null, "quoted": "把引用格式导出来", '
    '"effective_query": null, "confidence": "high", "reason": "要导出引文，没结果系统会说"}\n'
)


def _verb_table_zh(verbs: Any = None) -> str:
    """动词表的 prompt 文本。**由 `VERB_SPECS` 程序生成**——手抄一份必漂移。
    `verbs`（M1 scoped 路由）：显式给 VerbSpec 子集时按子集出表
    （understand 的套件收窄面用）；缺省 None = 全表，与历史输出**逐位一致**。"""
    lines = []
    for spec in (verbs if verbs is not None else VERB_SPECS):
        kind_zh = "执行类" if spec.kind == EXEC else "路由类"
        slot_zh = ("，可带 " + "/".join(spec.slots)) if spec.slots else ""
        lines.append(f"- {spec.verb}（{kind_zh}：{spec.zh}{slot_zh}）—— {spec.when_zh}")
    return "\n".join(lines)


def _filters_zh(current_filters: Any) -> str:
    """当前生效条件的人读摘要（prompt 用）。只读投影，格式不对就如实说「没有」。"""
    rows: list[str] = []
    for f in (current_filters or []):
        if not isinstance(f, dict):
            continue
        label = str(f.get("label") or f.get("dim") or "").strip()
        values = f.get("values")
        if isinstance(values, list):
            val_zh = "、".join(str(v) for v in values if str(v).strip())
        else:
            val_zh = str(values or "").strip()
        if label:
            rows.append(f"{label}={val_zh}" if val_zh else label)
    return "；".join(rows)


def _retrieval_zh(retrieval: Any) -> str:
    """规则匹配概览的人读摘要（prompt 用）。**零命中/弃权原样说**——它是路由信号，不是判决书。"""
    if not isinstance(retrieval, dict):
        return ""
    status = str(retrieval.get("status") or "")
    if status == "error":
        return "规则匹配这次没能跑（" + str(retrieval.get("note") or "内部原因") + "）。"
    if status == "abstained":
        terms = "、".join("「" + str(t) + "」" for t in (retrieval.get("unresolved_terms") or [])[:6])
        return ("规则匹配**整句弃权**（" + str(retrieval.get("abstain_reason") or "")
                + (("；未收录词：" + terms) if terms else "") + "）。")
    total = int(retrieval.get("total") or 0)
    if total <= 0:
        return "规则匹配**零命中**（库中没有同时满足所有条件的记录）。"
    titles = "；".join(str(t) for t in (retrieval.get("top_titles") or [])[:3] if str(t).strip())
    return f"规则匹配命中 {total} 条" + (f"，靠前的如：{titles}。" if titles else "。")


def build_action_prompt(utterance: str, *, has_results: bool, result_total: int,
                        retrieval: Any = None, current_query: str = "",
                        current_filters: Any = None, examples_zh: str = "",
                        verbs: Any = None) -> str:
    """据封闭动词表构造 user prompt（纯函数、可确定性测试）。

    护栏**写进 user prompt**、不是 system slot：`llm_client._call_chat_completions` 的
    system 消息是写死的通用策展人设、会覆盖任何自定义 system，把护栏放那儿就是不会发出去的死代码
    （N5 那一层第一版的真 bug，被确定性测试当场抓到）。

    `retrieval` 是规则匹配概览（turn.rule_match_summary 的产出）：统一管线的第一段
    （关键词匹配）**为检索服务、但一切指令都过**——它的产出连同原始查询一起喂给 LLM 分流，
    零命中/弃权绝不直接判死刑（工具调用句往往零命中）。

    `examples_zh`（成功经验库，默认空串）非空时在静态示例段后追加「历史成功操作」
    动态样例（agent_exec 从 `.userdata/curate_examples.jsonl` 检索注入）；空串时输出与历史
    **逐位一致**（离线钉与既有断言零漂移）。

    `verbs`（M1 scoped 路由）：显式给 VerbSpec 子集时「可选动作」段按子集出表
    （understand 的套件收窄面）；缺省 None = 全表，与历史输出**逐位一致**。
     高2 起铁律段与 JSON 模板的 effective_query 行也随 `verbs` 按面生成
    （`_constraints_zh`）——面里没有的动词不出现在规则里；缺省 None 仍逐位一致。
    """
    if has_results:
        ctx = f"当前屏幕上已经有一批检索结果（共 {int(result_total)} 条命中）。"
    else:
        ctx = "当前屏幕上**还没有**检索结果。"
    current_query = str(current_query or "").strip()
    if current_query:
        ctx += f"\n当前查询：「{current_query}」。"
        filters_zh = _filters_zh(current_filters)
        ctx += f"\n当前生效条件：{filters_zh or '（无）'}。"
    elif not has_results:
        # 第三批任务6：a18/a21 的 none 边界抖动在「规则+示例」后仍 ~7-8/10——
        # 把同一条边界**就近**放在现场事实旁边（模型读到现场时立刻看到判据），
        # 比埋在文末铁律里更拦得住「没结果→不是执行诉求」的先验。
        ctx += ("\n此时「改条件」无从谈起（选 none）；但对结果的动作类请求"
                "（打包/导出引文/看文件）**照样选对应动作**——没结果由系统如实交代，不由你改判 none。")
    retrieval_zh = _retrieval_zh(retrieval)
    if retrieval_zh:
        ctx += "\n**这句话**过规则匹配（关键词检索第一段）的结果：" + retrieval_zh
    # 高2：effective_query 的模板行同按面生成——面里没有 ROUTE_QUERY 动词时
    # 不再点名 search.new/refine.conditions/lookup.identifier（面内不存在，点了就是矛盾指令）。
    route_query_in_face = verbs is None or bool(
        set(ROUTE_QUERY_VERBS) & {s.verb for s in verbs})
    effective_query_line = (
        '"effective_query": "完整检索句（仅 search.new/refine.conditions/lookup.identifier 填，否则 null）, '
        if route_query_in_face else
        '"effective_query": null（本表动词一律不填）, '
    )
    return (
        _RULES_ZH
        + "\n----- 可选动作 -----\n"
        + _verb_table_zh(verbs)
        + "\n\n----- 现场情况 -----\n"
        + ctx
        + "\n\n----- 用户这一句 -----\n"
        + utterance
        + "\n\n"
        + _constraints_zh(verbs)
        + "\n----- 示例 -----\n"
        + _EXAMPLES_ZH
        + (("\n" + str(examples_zh).strip() + "\n") if str(examples_zh or "").strip() else "")
        + "\n只输出**一个 JSON 对象**（不要任何其它文字、不要代码块）：\n"
        '{"verb": "表里的一个 verb", "limit": 数字或 null, "quoted": "原文片段", '
        + effective_query_line
        + '"cancelled": true 或 false（可选，默认 false，见铁律 3）, '
        '"confidence": "high" 或 "low", "reason": "一句中文理由，20 字以内"}\n'
        "动词声明了字符串槽位的（source/keywords/species/target），按原话里的说法填，没有就不填。\n"
    )


# ---------------------------------------------------------------- 容错解析

def parse_action_response(text: str) -> dict[str, Any]:
    """解析 LLM 输出 → 原始字段 dict。**极其宽容、绝不抛异常**；解析不出 → `{}`。"""
    for candidate in (text, _first_json_object(text or "")):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


# ---------------------------------------------------------------- 极性门

#: **动作词不是否定语素**（模拟剧本当场抓到的真 bug）：`NEGATION_GUARDS_CN`
#: 是**检索查询**的弃权守卫表（「删掉 X」在检索句里意味着排除 X），里面因此收了
#: 「删掉/移除/过滤掉/拒收」。可极性门问的是另一件事——「这个**执行诉求**有没有被否定」——
#: 「删掉我上传的 10x 数据」里 quoted 锚在「我上传的…」，紧邻窗里只有动作词「删掉」本身，
#: 照单全收就会把一句删除指令误判成 cancelled=True（前端只回「好，不做了」，用户看到的就是
#: 「删除指令没有回应」）。「别删掉那个文件」依然能抓到——窗里还有真否定语素「别」。
_ACTION_WORDS_NOT_NEGATION: tuple[str, ...] = ("删掉", "移除", "过滤掉", "拒收")

_NEG_MORPHEMES_FOR_POLARITY_CN: tuple[str, ...] = tuple(
    m for m in V.NEG_MORPHEMES_CN if m not in _ACTION_WORDS_NOT_NEGATION
)

#: 英文侧同理剔除动作词 "remove"（"remove the uploaded file" 是删除指令，不是否定删除；
#: "don't remove it" 仍有 "don't" 可抓）。skip/omit/reject/avoid 不是本表动作词，保留作否定语素
#: （"skip the import" 确实是取消导入）。
_EN_NEG_FOR_POLARITY: tuple[str, ...] = tuple(
    w for w in V.NEG_MORPHEMES_EN if w != "remove"
)

_EN_NEG_RE = re.compile(
    r"(?<![a-z0-9_])(?:" + "|".join(re.escape(w) for w in _EN_NEG_FOR_POLARITY) + r")(?![a-z])",
    re.IGNORECASE,
)

#: 否定语素与执行词之间允许隔多远。「找肺癌数据，不要打包」是 0；
#: 放宽到 4 会把「不要小鼠的数据，打包前5条」误判（那里隔了「的数据，」4 个字，正好在窗外）。
_NEG_WINDOW_CN = 4
_NEG_WINDOW_EN = 12

#: **征询掩码**：「能不能/要不要」是征询不是否定——「能不能上网检索一下」里的「不」
#: 不是否定语素，不掩掉它极性门会把一句征询误判成取消（NLU 选型实验在规则验证
#: 验证侧曾当场抓到这个盲区；实验验证侧的孪生表是同型掩码，两处同步改）。
#: 按长度降序：「要不要」含「要不」，先消费长词。
_QUESTION_HEDGES: tuple[str, ...] = ("可不可以", "能不能", "要不要", "该不该", "行不行",
                                     "好不好", "可否", "要不")

#: 疑问/陈述用法的「没」**不是**否定语素（问题猎杀批实锤：j03/b12b/k02a/k09a
#: 族马拉松句——「检查下ArrayExpress更新没，有新增就搜来入库」里的疑问「没」落在续步
#: quoted 的紧邻左窗（≤4 字），被极性门误判 cancelled=true 把多步链当场掐死；
#: 31 例失败里 12 例同出一源）。命令式否定从不用「没」收尾（那是「别/不要/不用/取消」
#: 的活儿），掩掉不损失真阳性。三族：
#: ①「有没有 / 有没」疑问格式（「有没有更新」＝是否有）；
#: ②「了没（有）」（「查了没」＝查了没有）；
#: ③分句末尾的「没（有）」——后随标点/语气词/句尾（「更新没，要是…」＝更新了没有）。
#: 实验验证侧的孪生掩码同型，两处同步改。
_INTERROGATIVE_MEI_FIXED: tuple[str, ...] = ("有没有", "了没有", "有没", "了没")
_INTERROGATIVE_MEI_RE = re.compile(
    r"没(?:有)?(?=[，。；！？!?、…~哈嘛吗吧呢呀啊喔哦啦]|$)")

#: 顺承/条件句的「没」（触发点审计 C-4）：「没找到就联网搜」「没有就导出来」里
#: 「没」修饰的是**前一个**动词（找到/有），从不否定「就/才」后面的动作——上面三族疑问掩码
#: 盖不住这类句子，窗口里的「没」会把一句正向指令误判成 cancelled（前端只回「好，不做了」）。
#: 等长掩码整段「没（有）…就/才」（标点不跨段），锚点索引照常可用。
#: 实验验证侧的孪生掩码是同型规则，两处同步改。
_SEQUENTIAL_MEI_RE = re.compile(r"没(?:有)?[^，。；！？!?、…~]{0,5}?[就才]")


def _mask_hedges(text: str) -> str:
    """把征询格式词与疑问「没」等长掩码（换成同字数的全角空格）：**位置不变**，锚点索引照常可用。"""
    for hedge in _QUESTION_HEDGES:
        text = text.replace(hedge, "　" * len(hedge))
    for fixed in _INTERROGATIVE_MEI_FIXED:
        text = text.replace(fixed, "　" * len(fixed))
    text = _INTERROGATIVE_MEI_RE.sub(lambda m: "　" * len(m.group(0)), text)
    return _SEQUENTIAL_MEI_RE.sub(lambda m: "　" * len(m.group(0)), text)


def negation_before(text: str, index: int) -> str:
    """`text[index]` 之前的小窗口里出现的否定语素；没有则返回 ""。

    只看**紧邻**的窗口，不看整句：整句里出现「不要小鼠」不代表用户不要打包。
    扫描前先过征询掩码（等长替换，索引不漂移）。词表用 `_NEG_MORPHEMES_FOR_POLARITY_CN`
    （从检索守卫表里剔除了动作词，理由见该常量注释）。
    """
    if index <= 0:
        return ""
    masked = _mask_hedges(text)
    window = masked[max(0, index - _NEG_WINDOW_CN):index]
    for morpheme in _NEG_MORPHEMES_FOR_POLARITY_CN:  # 已按长度降序，长词优先
        if morpheme in window:
            return morpheme
    en_window = masked[max(0, index - _NEG_WINDOW_EN):index]
    match = _EN_NEG_RE.search(en_window)
    return match.group(0) if match else ""


def _anchors(utterance: str, quoted: str) -> list[int]:
    """极性门要检查的锚点位置：优先用 `quoted`，退而求其次用规则表认到的执行动作词。"""
    if quoted:
        i = utterance.find(quoted)
        if i >= 0:
            return [i]
    out: list[int] = []
    for verb in detect_action_verbs(utterance):
        i = utterance.lower().find(verb.lower())
        if i >= 0:
            out.append(i)
    return out


def polarity_blocked(utterance: str, quoted: str) -> str:
    """这句话里的执行诉求是不是被否定了。返回命中的否定语素，没有则 ""。

    判据是「**每一个**锚点都被否定」：「打包前5条，不要引文」里「打包」没被否定，
    整句仍然是一条执行指令，不该被那个「不要」连坐。
    """
    anchors = _anchors(utterance, quoted)
    if not anchors:
        return ""
    hits = [negation_before(utterance, i) for i in anchors]
    return hits[0] if all(hits) else ""


# ---------------------------------------------------------------- 槽位

_CJK_DIGITS = "零一二三四五六七八九"


def _cjk_numeral(n: int) -> str:
    """1..99 的中文写法（用于判断「用户到底说没说这个数」，不是给用户看的）。"""
    if n < 0 or n > 99:
        return ""
    if n < 10:
        return _CJK_DIGITS[n]
    tens, ones = divmod(n, 10)
    head = "十" if tens == 1 else _CJK_DIGITS[tens] + "十"
    return head + (_CJK_DIGITS[ones] if ones else "")


def _limit_was_said(utterance: str, value: int) -> bool:
    """这个数字在用户原话里**真的出现过**吗（阿拉伯数字或中文数字）。

    刻意**不**在后端再抄一份条数解析器：前端 `task_pack.js:tpCountFromUtterance` 已经是那一问的
    单一真源，后端再写一份必然漂移。这里只回答一个更弱、但足以支撑回执诚实性的问题——
    「系统用的这个数，是用户说过的，还是大模型替他填的」。

    判据是**数字边界**而不是裸子串（审计 C-5）：裸子串会把「2025」里的「25」、
    「GSE123456」里的「34」、「五月」里的「五」当成用户说过的条数——这是把系统的错算到
    用户头上的谎报方向。阿拉伯数字两侧不得再是数字、右侧不得是日期义项字（月/日/号）；
    中文数字两侧不得再是中文数字（「二十五」里的「五」不算）、右侧同样排除月/日/号。
    两侧**不**排字母：「top3」里的「3」是用户真说过的条数（test_agent_exec.py 既有钉）。
    """
    if value <= 0:
        return False
    if re.search(r"(?<!\d)" + str(value) + r"(?![\d月日号])", utterance):
        return True
    cjk = _cjk_numeral(value)
    if cjk:
        boundary = _CJK_DIGITS + "十"
        if re.search(r"(?<![" + boundary + "])" + cjk + r"(?![" + boundary + "月日号])", utterance):
            return True
    return False


def _resolve_limit(utterance: str, raw: Any) -> tuple[int, str, list[dict[str, str]]]:
    """→ (limit, slot_source, deltas)。`limit == 0` 表示「没说条数、走默认口径」。"""
    deltas: list[dict[str, str]] = []
    if raw is None or raw is False or raw == "":
        return 0, "default", deltas
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0, "dropped", [{
            "slot": "limit", "said": str(raw), "used": "默认口径",
            "why_zh": "没能把它读成一个条数，这次按默认口径处理。",
        }]
    if value < 1:
        return 0, "dropped", [{
            "slot": "limit", "said": str(value), "used": "默认口径",
            "why_zh": "条数至少是 1，这次按默认口径处理。",
        }]
    if value > MAX_LIMIT:
        deltas.append({
            "slot": "limit", "said": str(value), "used": str(MAX_LIMIT),
            "why_zh": f"一次最多处理 {MAX_LIMIT} 条，这次按 {MAX_LIMIT} 条办。",
        })
        return MAX_LIMIT, "clamped", deltas
    return value, ("said" if _limit_was_said(utterance, value) else "guessed"), deltas


# ---------------------------------------------------------------- 组装

def _blank_plan(**over: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "verb": "none",
        "verb_zh": VERB_BY_NAME["none"].zh,
        "kind": ROUTE,
        "requires_results": False,
        "slots": {},
        "slot_sources": {},
        "deltas": [],
        "quoted": "",
        #: 路由类检索动词（search.new/refine.conditions/lookup.identifier）携带的**完整检索句**
        #: （LLM 据当前查询+条件改写；空串 = 调用方按用户原话检索）。执行类与 none 恒空。
        "effective_query": "",
        "confidence": "high",
        "rejected": [],
        "reason_zh": "",
        "source": "none",
        "llm_status": "",
        "caveat_zh": "",
        "blocked_reason": "",
        "uncertainty_zh": "",
        #: 取消态（NLU 实验定稿语义）：否定取消时**动词照留**、此标记为 true，
        #: 执行层（act.js / MCP 调用方）据此**不执行、只回音**（「好，不导入」）。
        #: 只对 EXEC 类成立；路由类恒 false（见 `_finalize` 机械派生）。
        "cancelled": False,
        "note_zh": PLAN_NOTE_ZH,
    }
    plan.update(over)
    return plan


def raw_shape_violations(raw: dict[str, Any], utterance: str) -> list[str]:
    """公共 raw 形状校验（验证裁决落地：此前 `agent_exec._validate_raw`
    自称与本模块「口径一一对应」，实际那份镜像校验是私拷+私改，两路径已漂移）。

    这三条与 `build_plan_from_raw`/`_finalize` 的降级处置**一一对应**：
    缺 verb / 词表外 verb（→ 降 none 进 rejected）、quoted 非用户原话逐字子串（→ 清空）、
    EXEC 无 quoted（→ 整计划降 none）。返回人读 violations（空表 = 通过），
    供 agent 路径在降级**发生前**拦下、给 LLM 一次自修机会。
    agent_exec 在本表之上叠加多步执行专属闸（点名源/幻觉取消/sync 主题/keywords 接地）——
    那些刻意更严，不回流本函数（单步 fallback 错收取消无害，多步幻觉取消会杀链，语义分流是
    策略，见 agent_exec._validate_raw 文档串）。"""
    if not raw:
        return ["没有拿到可解析的输出（既不是工具调用，也不是 JSON）。"]
    violations: list[str] = []
    verb = str(raw.get("verb") or "").strip()
    if not verb:
        violations.append("缺少 verb。")
    elif verb not in VERB_BY_NAME:
        violations.append(f"verb「{verb}」不在封闭动词表里，不许发明新动作。")
    else:
        spec = VERB_BY_NAME[verb]
        quoted = str(raw.get("quoted") or "").strip()
        if quoted and quoted not in utterance:
            violations.append(f"quoted「{quoted}」不是用户原话里逐字出现的片段，不许改写或翻译。")
        if spec.kind == EXEC and not quoted:
            violations.append(f"执行类动作 {verb} 必须给出 quoted（用户原话里的依据片段）。")
    return violations


def _finalize(
    plan: dict[str, Any], *, has_results: bool, result_total: int, utterance: str
) -> dict[str, Any]:
    """收口：极性门（含征询掩码）→ 取消态派生 → 执行类必须有依据 → 屏上有没有东西可做 → 不确定标注。"""
    spec = VERB_BY_NAME.get(plan["verb"]) or VERB_BY_NAME["none"]
    plan["verb_zh"] = spec.zh
    plan["kind"] = spec.kind
    plan["requires_results"] = spec.requires_results

    if spec.kind != EXEC:
        # cancelled 是 EXEC 专属语义：路由类（none/search.new/…）本就不执行，挂取消态是空话。
        plan["cancelled"] = False
    else:
        # ① 极性门：把用户明确否定的词当授权证据引用给他看，是本层最恶劣的一种谎。
        # 起不再整计划降 none 装没听懂：**动词照判 + cancelled 标记**，
        #    执行层据此不执行、只回音（「好，不导入」）。机械门与 LLM 自报取**或**——
        #    门测到否定而 LLM 没标，以门为准（安全侧）；LLM 标了取消而门测不到，照收
        #    （取消只意味着「不做」，错收的最坏结果是用户再说一次，不是做错）。
        blocked = polarity_blocked(utterance, plan.get("quoted") or "")
        if blocked:
            plan["cancelled"] = True
            plan["reason_zh"] = f"你说了「{blocked}」，所以这次没有执行。"
        elif plan.get("cancelled"):
            plan["reason_zh"] = str(plan.get("reason_zh") or "").strip() or "你说先不做这一步，所以这次没有执行。"
        # ② 没有可定位的原文依据就不执行。这条让回执里「依据你说的『…』」结构上不可能是编的。
        if not plan.get("quoted"):
            return _finalize(
                _blank_plan(
                    source=plan.get("source", ""), llm_status=plan.get("llm_status", ""),
                    reason_zh="没能在你这句话里找到对应的原文依据，这一步没有执行。",
                    rejected=list(plan.get("rejected") or []),
                    # 审计 C-7：这是「读懂了但缺原文依据」的**降级 none**，不是真 none——
                    # additive 记下被降掉的 verb，路由层据此如实回音，不许谎称「没听懂」。
                    downgraded_from=str(plan.get("verb") or ""),
                ),
                has_results=has_results, result_total=result_total, utterance=utterance,
            )
        if not plan["cancelled"]:
            # ③ 屏上没结果：**如实说「没东西可做」**，不要谎报成「没听懂」——
            #    系统明明读懂了（verb 都判出来了），用户改说法也解决不了。
            #    取消态不挂这档：它本来就不执行，回执只需一句「好，不做了」。
            if spec.requires_results and not has_results:
                plan["blocked_reason"] = "no_results"
            # ④ 恒带的不确定标注：与 confidence 无关；但**归因随 source 走**，别把规则档说成大模型读的。
            #    取消态不带：槽值不会进任何后续动作，没有「这几项」需要免责。
            plan["uncertainty_zh"] = uncertainty_note(str(plan.get("source") or ""))

    plan["has_results"] = bool(has_results)
    plan["result_total"] = int(result_total)
    return plan


def rule_fallback_plan(
    utterance: str, *, has_results: bool, result_total: int, llm_status: str
) -> dict[str, Any]:
    """LLM 缺席时的规则档：**只开清单，不接落盘动作**。

    规则表是裸子串匹配，实测 5 句误报（「去掉批量效应大的」→「批量」）。所以这一档
    不做 `pack.download`，改做 `pack.preview` —— 用户看到清单后一键就能继续，
    交付没有被收回去，也不会在他没确认的情况下往硬盘写文件。

    动作词检出与 `rule_operation_marker` 共用同一道名词用法反向闸
    （`_action_verb_noun_usage`， 审计 收敛，与 C-1 同根）：「下载量大的
    数据集有哪些」「只保留能下载的」里的「下载」是名词/能愿语境，不许在这一档被
    裸子串开成打包面板；真操作句（「下载 GSE123456」）照旧命中。
    """
    low = utterance.lower()
    verbs = [v for v in detect_action_verbs(utterance) if not _action_verb_noun_usage(low, v)]
    if not verbs:
        return _finalize(
            _blank_plan(source="rule", llm_status=llm_status,
                        reason_zh="按关键词没有认出你要我做的事。"),
            has_results=has_results, result_total=result_total, utterance=utterance,
        )
    # `detect_action_verbs` 回的是小写标记；回执要引用的是**用户写下的那几个字**，
    # 所以按位置切回原文大小写（英文标记 `download script` 才不会被写成用户没打过的样子）。
    at = utterance.lower().find(verbs[0])
    quoted = utterance[at:at + len(verbs[0])] if at >= 0 else verbs[0]
    return _finalize(
        _blank_plan(
            verb="pack.preview", quoted=quoted, confidence="low",
            source="rule", llm_status=llm_status,
            slots={"target": TARGET_DEFAULT}, slot_sources={"target": "default"},
            reason_zh=f"按关键词认到「{quoted}」。",
            # 集成截图抓到的静默丢参：用户说「人类肺癌数据，打包前5条」，
            # 规则档只认出「打包」这个动作，「前5条」一个字都没读，面板照自己的默认口径开了 10 条，
            # 而回执里没有任何一行提到这件事。后端刻意不在这里再抄一份条数解析器
            #（前端 `tpCountFromUtterance` 是那一问的单一真源，抄第二份必漂移），
            # 但**「这一档根本不读参数」是这条路径的固有事实，不需要解析也能如实说**。
            caveat_zh=(
                "大模型这次没有接上，这一步是按关键词猜的：只认出了你要做哪件事，"
                "句子里的条数、范围这些参数一概没读。所以只打开清单给你看、不直接生成文件，"
                "条数和勾选请在面板里自己定。"
                # NLU 实验结论（FINDINGS §3）：规则兜底只兜得到 pack.preview 这一类，
                # 管护动词（导入/删除/联网搜/恢复/检查更新）这一档**结构性够不到**——词表里没有它们，
                # 认不到更做不了。这不是「这次没发挥好」，是这条路径的固有能力边界，如实说出。
                "另外按关键词只能认出「打包」这一类：导入、删除、联网搜、恢复、检查更新这些操作"
                "它认不到。是这类事的话，请接上大模型再说一次。"
            ),
        ),
        has_results=has_results, result_total=result_total, utterance=utterance,
    )


#: 动作词的**名词用法**语境尾随字（触发点审计 C-1）：ACTION_VERBS 是裸子串，
#: 「下载量大的数据集有哪些」「只保留能下载的」里的「下载」是名词/能愿语境，不是操作指令——
#: 裸子串把这类检索句拦成「需开启 AI 执行」气泡，检索永不可达（本模块 docstring 第 6 行
#: 自己就拿它当误报反面教材）。闸只作用于动作词那半：管护短语（CURATE_OP_MARKERS）的
#: 收录口径本就排除检索句裸词（vocabulary.py:557-559），不需要这道闸。
_ACTION_VERB_NOUN_SUFFIXES = ("量", "的")

#: `V.ACTION_VERBS` 的小写集合（`detect_operation_markers` 回的是小写标记，比对口径一致）。
#: 注意与本模块 `ACTION_VERBS`（:276，VERB_SPECS 的动词名）是两份不同的词表，别混。
_VOCAB_ACTION_VERBS = frozenset(m.lower() for m in V.ACTION_VERBS)


def _action_verb_noun_usage(low: str, marker: str) -> bool:
    """公共匹配助手（审计 收敛）：小写动作词 `marker` 在 `low`（小写原话）里
    **首次出现处**紧跟「量/的」→ 名词/能愿用法，是检索语境不是操作意图。

    C-1 修 agent_off 气泡时建的反向闸，与 `rule_fallback_plan` 的 detect_action_verbs 裸子串
    同根；现收敛成这一处判定，两处（`rule_operation_marker` / `rule_fallback_plan`）共用，
    只许收窄误触，不许放过真操作意图（「下载 GSE123456」的「下载」后随空格，照旧命中）。
    """
    at = low.find(marker)
    return at >= 0 and low[at + len(marker):at + len(marker) + 1] in _ACTION_VERB_NOUN_SUFFIXES


def rule_operation_marker(utterance: str) -> str:
    """「AI 执行」（维度 C）**关闭**时的规则操作意图检出——非 LLM、确定性子串，绝不执行任何动作。

    判据 = 执行动作词 ∪ 管护操作短语（`query_parser.detect_operation_markers` 单一真源，
    按出现位置保序——命中即返回**最靠左**那段标记原文，供降级气泡逐字引用）；没命中返回 ""。
    一道反向闸：**动作词**紧跟「量/的」是名词用法（「下载量」「能下载的」），是检索句不是
    操作指令，不算命中（审计 C-1；判定本体在 `_action_verb_noun_usage`）。
    用在 turn.route_turn 的两处：agent_off 分支检出的句子回「这是操作指令，需开启 AI 执行」
    降级气泡（降级语义）；LLM 缺席兜底用它拦住管护句，不许 fail-open 成检索
    （审计 C-2）。
    """
    hits = detect_operation_markers(utterance)
    if not hits:
        return ""
    low = utterance.lower()
    for marker in hits:
        at = low.find(marker)
        if marker in _VOCAB_ACTION_VERBS and _action_verb_noun_usage(low, marker):
            continue
        # 扫描回的是小写标记；气泡要引用用户写下的那几个字（同 rule_fallback_plan 的口径）。
        return utterance[at:at + len(marker)] if at >= 0 else marker
    return ""


def rule_curate_op_marker(utterance: str) -> str:
    """**管护操作短语**那半的检出（CURATE_OP_MARKERS，不含执行动作词）；没命中返回 ""。

    与 `rule_operation_marker` 同一扫描真源、同一「最靠左命中」口径，只过滤掉动作词那半。
    用在 turn.route_turn 的编号快速道闸（审计 C-3）：「编号 + 管护操作」
    （「把 GSE123456 从我上传的里删掉」「GSE123456 那套有没有更新」）不是检索诉求，
    不许被编号快速道静默吃掉。管护短语是**短语**（收录口径排除检索句裸词），
    不需要动作词那道名词用法反向闸。
    """
    low = utterance.lower()
    for marker in detect_operation_markers(utterance):
        if marker in _VOCAB_ACTION_VERBS:
            continue
        at = low.find(marker)
        return utterance[at:at + len(marker)] if at >= 0 else marker
    return ""


def build_plan_from_raw(
    raw: dict[str, Any], utterance: str, *, has_results: bool, result_total: int,
    llm_status: str = "ok",
    allowed_verbs: Any = None,
) -> dict[str, Any]:
    """把 LLM 的原始输出**校验**成一份可执行的 plan（纯函数，零网络，测试主入口）。

    `allowed_verbs`（高1）：调用方给的允许动词表（plan_action 保底通道
    传 `PLAN_ACTION_VERBS` 挡环内专属动词）；表外动词与未知 verb 同口径——进 rejected、
    降 none。缺省 None = 全表放行（agent 环内路径不变）。"""
    rejected: list[str] = []

    raw_verb = str(raw.get("verb") or "").strip()[:MAX_REJECTED_CHARS]
    if raw_verb and (raw_verb not in VERB_BY_NAME
                     or (allowed_verbs is not None and raw_verb not in allowed_verbs)):
        rejected.append(raw_verb)          # 封闭词表唯一的真实反馈渠道：不做，但要说
        raw_verb = "none"
    verb = raw_verb or "none"

    # v1 一次只办一件事；LLM 若额外提了别的动作，一律只报不做。
    also = raw.get("also")
    if isinstance(also, list):
        rejected.extend(
            str(x).strip()[:MAX_REJECTED_CHARS] for x in also[:MAX_REJECTED] if str(x).strip()
        )

    # `quoted` 必须逐字出现在原话里。不过就清空——清空后执行类会在 `_finalize` 里被降成 none。
    quoted = str(raw.get("quoted") or "").strip()
    if quoted and quoted not in utterance:
        quoted = ""

    spec = VERB_BY_NAME.get(verb) or VERB_BY_NAME["none"]
    slots: dict[str, Any] = {}
    slot_sources: dict[str, str] = {}
    deltas: list[dict[str, str]] = []
    if "limit" in spec.slots:
        limit, source, limit_deltas = _resolve_limit(utterance, raw.get("limit"))
        slots["limit"] = limit
        slot_sources["limit"] = source
        deltas.extend(limit_deltas)
    # 其余声明槽位（source/keywords/species/target 等字符串槽）：原样透传、截断上限。
    # 归因同 limit 的口径：值逐字出现在原话里才算「用户说的」，否则标 guessed——
    # 槽值不是本工具核对出来的就必须说出来（见 UNCERTAINTY_ZH 的注释）。
    for slot in spec.slots:
        if slot == "limit":
            continue
        raw_value = raw.get(slot)
        if slot == "display":
            # display 布尔槽（M2 rank/rerank）：认 JSON 布尔 true 与字符串
            # "true"（大小写不敏感——decide 的 JSON 兜底壳实测会给字符串），其余一律
            # 不进 slots（缺省 = 不上屏）。上屏与否是模型的判断、不是原话槽值 → guessed。
            if raw_value is True or str(raw_value or "").strip().lower() == "true":
                slots[slot] = True
                slot_sources[slot] = "guessed"
            continue
        if slot == "uids":
            # uids 数组槽（批，设计 §4）：cite.export 的编号清单——保留
            # **列表形状**（逐个清洗/去空/去重，上限 20），不许走下方「列表拍平成字符串」
            # 的旧逻辑（那是为 keywords 等字符串槽加的；拍平会把 uids 数组毁成空格拼接串，
            # 依赖占位的数组元素也会被一并毁形）。数组元素可为字面量编号或占位引用——
            # 占位引用在批内由 execute 解析层替换为真实 uid，走到本函数时已是实值。
            # 元素**不做 80 字符截断**（dataset_uid 可长至 100+ 字符，截断会毁形）。
            uid_items: list[str] = []
            for item in (raw_value if isinstance(raw_value, (list, tuple)) else [raw_value]):
                s = str(item or "").strip()
                if s and s not in uid_items:
                    uid_items.append(s)
            uid_items = uid_items[:20]
            if uid_items:
                slots["uids"] = uid_items
                slot_sources["uids"] = (
                    "said" if all(x in utterance for x in uid_items) else "guessed")
            continue
        if isinstance(raw_value, (list, tuple)):
            # 模型偶发把字符串槽给成列表（A/B 验证：v4-pro 约 2% 把 keywords
            # 给成 ["..."]）——拍平成空格连接的字符串再走统一口径；直接 str() 会把
            # Python 列表语法（引号、方括号）灌进 keywords，污染联网搜索词。
            raw_value = " ".join(
                str(item).strip() for item in raw_value if str(item).strip())
        value = str(raw_value or "").strip()[:80]
        if value:
            slots[slot] = value
            slot_sources[slot] = "said" if value in utterance else "guessed"
    if spec.kind == EXEC and spec.requires_results:
        # target="results" 只对「在当前这批结果上执行」的动词成立；curate.* 管护动词作用对象是
        # 外部库/回收站，与屏上结果无关，不带 target（管护动词纳入时细分）。
        slots["target"] = TARGET_DEFAULT
        slot_sources["target"] = "default"
        raw_scope = str(raw.get("scope") or "").strip().lower()
        if raw_scope and raw_scope != "primary":
            # 验证：旧文案「还不支持取全部文件」是假话（Web/MCP/provision
            # 的 scope=all 早已是公开能力）——对话入口这版按主文件打包是**入口口径**，
            # 不是产品能力边界；如实说清差别，不许把入口限制说成产品不支持。
            deltas.append({
                "slot": "scope", "said": raw_scope, "used": "primary",
                "why_zh": ("对话里这一版按每个数据集一个代表性主文件打包"
                           "（接口另有全部文件档，对话入口暂不能选）。"),
            })
            slot_sources["scope"] = "dropped"

    confidence = str(raw.get("confidence") or "").strip().lower()
    if confidence not in ("high", "low"):
        confidence = "low"

    # `effective_query`：路由类检索动词的完整检索句。只对这三个动词有意义（其余一律置空，
    # 防止 LLM 手滑把执行句塞进检索槽）；形状校验（非空、上限），语义正确性由
    # `/api/recommend` 的 fail-closed 门把关——改写句在那儿还要再过一遍解析。
    effective_query = ""
    if verb in ROUTE_QUERY_VERBS:
        effective_query = str(raw.get("effective_query") or "").strip()[:MAX_UTTERANCE_CHARS]

    # `cancelled` 只认 JSON 布尔 true（字符串「true」不算——LLM 手滑不该能取消一个动作）。
    # 与极性门是**或**关系，最终取值在 `_finalize` 机械派生（门测到否定而 LLM 没标，以门为准）。
    cancelled = raw.get("cancelled") is True

    return _finalize(
        _blank_plan(
            verb=verb, slots=slots, slot_sources=slot_sources, deltas=deltas,
            quoted=quoted, effective_query=effective_query,
            confidence=confidence, cancelled=cancelled,
            rejected=list(dict.fromkeys(rejected))[:MAX_REJECTED],
            reason_zh=str(raw.get("reason") or "").strip()[:80],
            source="llm", llm_status=llm_status,
        ),
        has_results=has_results, result_total=result_total, utterance=utterance,
    )


# ---------------------------------------------------------------- LLM 可用性 + 主入口

def should_use_llm(config: LLMConfig) -> tuple[bool, str]:
    """是否调用**真** LLM。返回 (是否, 原因短标签)。

    mock 一律判否：`llm_client.call_mock_llm` **忽略 prompt**、直吐 curator markdown 表，
    让执行层走它会「荒谬通过」——产出根本不是 plan（N5 同款结论）。
    """
    if config.mock_llm or _normalize_provider(config.provider) == "mock":
        return False, "mock_not_used"
    if not config.enable_llm:
        return False, "disabled"
    if not config.api_key:
        return False, "no_key"
    return True, "ready"


def _error_label(exc: BaseException) -> str:
    """诊断标签只取**异常类名**，绝不取异常文本。

    `llm_client._sanitize_provider_error` 只能脱敏它**已知的**那把 key 和 `Bearer …`；
    provider SDK 把裸 `sk-…` 拼进消息时它无能为力（本模块的确定性测试当场抓到过一次）。
    `llm_status` 只用于诊断「哪一档失败了」，类名足够，而类名结构上不可能含密钥。
    """
    return type(exc).__name__[:40]


def _default_llm_call(prompt: str, config: LLMConfig) -> str | None:
    provider = _normalize_provider(config.provider)
    result = call_zhipuai(prompt, config) if provider == "zhipuai" else call_openai_compatible(prompt, config)
    return result.text if result.succeeded else None


def normalize_utterance(utterance: str) -> str:
    text = str(utterance or "").strip()
    if not text:
        raise ActionPlanError("empty_input", "这句话不能为空。")
    if len(text) > MAX_UTTERANCE_CHARS:
        raise ActionPlanError("too_large", f"这句话太长（上限 {MAX_UTTERANCE_CHARS} 字）。")
    return text


def plan_action(
    utterance: str,
    *,
    has_results: bool = False,
    result_total: int = 0,
    config: LLMConfig | None = None,
    llm_call: Callable[[str], str | None] | None = None,
    retrieval: Any = None,
    current_query: str = "",
    current_filters: Any = None,
    allowed_verbs: Any = None,
) -> dict[str, Any]:
    """一句话 → 一份**只描述该做什么**的 plan。永不执行、永不抛（除入参非法）。

    `has_results` / `result_total` 由调用方自述，服务端无从核实——所以本函数
    只据它们置 `blocked_reason`，**不生成任何带这两个数字的成句断言**。

    `retrieval`（规则匹配概览）/ `current_query` / `current_filters` 是统一管线
    （turn.route_turn）喂给 LLM 分流的上下文：原始查询与规则匹配结果**一起**进 prompt，
    零命中/弃权不直接判死刑。`llm_call` 可注入（签名 `(prompt) -> str | None`），
    确定性测试据此完全避开网络。

    `allowed_verbs`（engagement P6，设计 §5.2）：suggested_recipe 收窄的动词
    子集（None=不额外收窄，沿用 PLAN_ACTION_VERBS 全表）。表外动词与未知 verb 同口径——
    build_plan_from_raw 机械拒（进 rejected 降 none）；提示层按同一子集出动词表。
    只缩小不扩权：参数校验 / 执行开关 / 安全闸一律不动。
    """
    text = normalize_utterance(utterance)
    total = max(0, int(result_total or 0))

    if llm_call is None:
        try:
            cfg = config or load_llm_config()
        except Exception as exc:                        # 配置加载异常也 fail-open
            return rule_fallback_plan(
                text, has_results=has_results, result_total=total,
                llm_status=f"config_error:{_error_label(exc)}",
            )
        ok, reason = should_use_llm(cfg)
        if not ok:
            return rule_fallback_plan(
                text, has_results=has_results, result_total=total, llm_status=reason
            )
        caller: Callable[[str], str | None] = lambda p: _default_llm_call(p, cfg)  # noqa: E731
    else:
        caller = llm_call

    # 收窄面：suggested_recipe 给出的动词集 ∩ 保底通道全表（环内专属动词本就不在
    # _PLAN_ACTION_SPECS）；交集为空 → 提示层动词表为空、闸层全拒 → 天然 none。
    narrow_specs = _PLAN_ACTION_SPECS
    narrow_allowed: Any = PLAN_ACTION_VERBS
    if allowed_verbs is not None:
        allowed = set(allowed_verbs)
        narrow_specs = tuple(s for s in _PLAN_ACTION_SPECS if s.verb in allowed)
        narrow_allowed = tuple(v for v in PLAN_ACTION_VERBS if v in allowed)

    prompt = build_action_prompt(
        text, has_results=has_results, result_total=total,
        retrieval=retrieval, current_query=current_query, current_filters=current_filters,
        verbs=narrow_specs,   # 高1：保底通道不出环内专属动词（提示层）；P6：suggested_recipe 再收窄
    )
    # 瞬时失败重试一次再认栽（见 _RETRY_BACKOFF_SECONDS）。注入的 caller 是测试替身：
    # 只调一次，调用次数留给测试断言。
    raw: dict[str, Any] = {}
    fail_status = ""
    for attempt in range(1 if llm_call is not None else 2):
        if attempt:
            time.sleep(_RETRY_BACKOFF_SECONDS)
        try:
            answer = caller(prompt)
        except Exception as exc:                            # provider 层任何异常 → 规则档
            fail_status = f"error:{_error_label(exc)}"
            continue
        if not answer:
            fail_status = "empty"
            continue
        raw = parse_action_response(answer)
        if raw:
            break
        fail_status = "unparsable"
    if not raw:
        return rule_fallback_plan(
            text, has_results=has_results, result_total=total, llm_status=fail_status
        )
    return build_plan_from_raw(
        raw, text, has_results=has_results, result_total=total, llm_status="ok",
        allowed_verbs=narrow_allowed,   # 高1：提示不是围栏，表外动词机械拒（闸层）；P6：suggested_recipe 再收窄
    )
