# -*- coding: utf-8 -*-
"""成功经验 few-shot 库（Vanna auto_train 式）的确定性门——统一知识道闸链口径。

机械收录只进**候选提案**（统一 knowledge_changes 草稿，不注入）；用户在记忆模块
勾选后走完整真实道闸（离线关系评估接缝→真人确认提交）才成为生效知识条目
（注入侧只读已生效条目）。旧 JSONL 账本（curate_examples.jsonl /
curate_example_candidates.jsonl）是永久保留的迁移原档，不再写入；损坏原档行经
迁移隔离（quarantine）留可读原因，不再走旧「坏行跳过」读取缝。

钉死：一遍过才进候选（失败/取消/非管护/被闸修过不录）；同内容重复收录幂等；
勾选入库/驳回/分区隔离（无身份 fail-closed）；注入是分区完整投影（不 top-k
截断，超预算明确拒绝，见 tests/test_knowledge_gate.py 预算钉）；prompt 注入段
fail-open（空账 → 空串，build_action_prompt 与历史逐位一致）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.agent import action_plan as AP  # noqa: E402
from dataset_recommender.agent import agent_exec as AX  # noqa: E402
from dataset_recommender.app.conversation_runs import RunError  # noqa: E402
from dataset_recommender.app.conversation_store import ConversationStore  # noqa: E402
from dataset_recommender.app.knowledge_service import KnowledgeService  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

#: 评估接缝配置与分区键：道闸钉死「分区端点指纹 == 评估配置有效端点指纹」，
#: 测试分区键必须用真实推导指纹（空串分区是未绑定端点的本机/匿名区，跳过比对）。
_CFG1 = LLMConfig(enable_llm=True, api_key='synthetic-examples-gate-1',
                  base_url='https://examples-gate.example/v1', model='gate-1')
_CFG2 = LLMConfig(enable_llm=True, api_key='synthetic-examples-gate-2',
                  base_url='https://examples-gate.example/v1', model='gate-2')
EP1 = AX._endpoint_fp_from_config(_CFG1)
EP2 = AX._endpoint_fp_from_config(_CFG2)


def _service(root):
    return KnowledgeService(ConversationStore(root))


def _known(request):
    from test_memory_relations import decision
    return decision(request, 'overlap_incomparable')


def _commit_change(root, receipt, *, principal, endpoint_fp='', project_id=''):
    """草稿提案走完真实统一道闸：离线关系评估接缝 + 真人确认提交。"""
    change_id = receipt.get('change_id') or receipt['id']
    change_revision = receipt.get('change_revision') or receipt['revision']
    cfg = {EP1: _CFG1, EP2: _CFG2}.get(endpoint_fp, _CFG1)
    service = _service(root)
    assessed = service.assess(principal, endpoint_fp, project_id, change_id=change_id,
        expected_change_revision=change_revision, cfg=cfg, predictor=_known)
    assert assessed['ok'], assessed
    committed = service.commit(principal, endpoint_fp, project_id, change_id=change_id,
        expected_change_revision=assessed['change_revision'], assessment_id=assessed['assessment_id'],
        expected_catalog_generation=assessed['catalog_generation'], human_confirmed=True)
    assert committed['ok'], committed
    return committed


def _step(verb, ok=True, slots=None):
    return {"verb": verb, "ok": ok, "slots": dict(slots or {})}


def _cand_rows(root: Path, principal='anonymous', endpoint_fp='') -> list[dict]:
    """候选池读面：统一道闸里的 example 草稿/待决提案（旧 JSONL 候选池已封存）。"""
    return AX.list_example_candidates(root, principal=principal, endpoint_fp=endpoint_fp)


def _ledger_rows(root: Path, principal='anonymous', endpoint_fp='') -> list[dict]:
    """正式库读面：统一知识条目中的 example（任何状态；旧 JSONL 正式库已封存）。"""
    from dataset_recommender.agent.knowledge import legacy_examples
    service = _service(root)
    rows, offset = [], 0
    while True:
        page = service.entries(principal, endpoint_fp, kind='example', status='', offset=offset, limit=200)
        rows.extend(page['items'])
        offset = page['next_offset']
        if offset is None:
            return legacy_examples(rows)


def test_success_is_recorded(tmp_path):
    """干净成功 → 进候选提案（带 change id），**正式库保持空**（真人没提交就不注入）。"""
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                             [_step("curate.rollback", slots={"target": "upload_a.json"})])
    rows = _cand_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["utterance"] == "把 upload_a.json 删掉"
    assert rows[0]["steps"][0]["verb"] == "curate.rollback"
    assert "upload_a.json" in rows[0]["steps"][0]["args"]
    assert rows[0]["id"], "候选提案必须带 id（勾选/驳回凭它定位）"
    assert _ledger_rows(tmp_path) == [], "候选≠入库：真人提交前正式库必须是空的"


def test_failed_or_cancelled_or_non_curate_not_recorded(tmp_path):
    AX._maybe_record_success(tmp_path, "联网搜 human lung 入库", {},
                             [_step("curate.search_online", ok=False)])          # 失败步
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {"cancelled": True},
                             [_step("curate.rollback")])                          # 取消态
    AX._maybe_record_success(tmp_path, "把结果打包", {},
                             [_step("pack.download")])                            # 非管护动词
    AX._maybe_record_success(tmp_path, "", {}, [_step("curate.db_status")])       # 空原话
    AX._maybe_record_success(tmp_path, "库里有什么", {}, [])                      # 零步骤
    assert _cand_rows(tmp_path) == []


def test_readonly_curate_session_is_recorded(tmp_path):
    """纯只读管护会话（db_status）也是成功样例——「库里有什么」→ db_status 的表达对齐正缺它。"""
    AX._maybe_record_success(tmp_path, "库里现在有什么", {}, [_step("curate.db_status")])
    rows = _cand_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["steps"][0]["verb"] == "curate.db_status"


def test_verbatim_duplicate_is_not_recorded(tmp_path):
    for _ in range(2):
        AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                                 [_step("curate.rollback", slots={"target": "upload_a.json"})])
    assert len(_cand_rows(tmp_path)) == 1, "同内容重复收录按 request_id 幂等，不产生第二份提案"


def test_candidate_pool_is_durable_and_recording_idempotent(tmp_path):
    """旧 JSONL 候选池的旋转上界机制随统一 SQLite 提案表退役：迁移的存量候选完整保留
    （不再按行数旋掉旧行）；同内容重复收录幂等。容量边界改由提案严格输入限制与分页
    读面承担（见 tests/test_knowledge_gate.py 的预算钉）。"""
    path = tmp_path / ".userdata" / AX._EXAMPLE_CANDIDATES_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for i in range(AX._EXAMPLES_MAX_ROWS):
            fh.write(json.dumps({"id": f"legacy-{i}", "utterance": f"第{i}句",
                                 "steps": [{"verb": "curate.db_status"}]}, ensure_ascii=False) + "\n")
    AX._maybe_record_success(tmp_path, "最新一句", {}, [_step("curate.db_status")])
    AX._maybe_record_success(tmp_path, "最新一句", {}, [_step("curate.db_status")])
    rows = _cand_rows(tmp_path)
    utterances = [r["utterance"] for r in rows]
    assert utterances.count("最新一句") == 1
    assert "第0句" in utterances and f"第{AX._EXAMPLES_MAX_ROWS - 1}句" in utterances, \
        "统一提案表不旋转丢弃旧候选"
    assert len(rows) == AX._EXAMPLES_MAX_ROWS + 1


def test_complete_projection_replaces_top_k_retrieval(tmp_path):
    """旧 top-k 字组检索随统一完整投影退役：注入侧投影分区内**全部**已生效示例，
    不按查询相关性截断（预算超限明确拒绝而非静默截断）；分区外依旧不可见。"""
    path = tmp_path / ".userdata" / AX._EXAMPLES_LEDGER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = [
        {"utterance": "把 upload_a.json 删掉", "steps": [{"verb": "curate.rollback", "args": "curate.rollback（target=upload_a.json）"}]},
        {"utterance": "检查数据库来源有没有更新", "steps": [{"verb": "curate.check_updates", "args": "curate.check_updates"}]},
        {"utterance": "把 upload_b.json 也删掉", "steps": [{"verb": "curate.rollback", "args": "curate.rollback（target=upload_b.json）"}]},
    ]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in seed), encoding="utf-8")
    hits = AX._load_success_examples(tmp_path, "把 upload_c.json 删掉",
                                     principal="anonymous", endpoint_fp="")
    assert {h["utterance"] for h in hits} == {r["utterance"] for r in seed}, \
        "完整投影不 top-k 截断、不按重叠度过滤"
    assert AX._load_success_examples(tmp_path, "斑马鱼", principal="anonymous",
                                     endpoint_fp="") and True, "投影与查询无关（完整性优先）"
    assert AX._load_success_examples(tmp_path, "把 upload_c.json 删掉",
                                     principal="u-alice", endpoint_fp="") == [], "跨账户不可见"


def test_prompt_section_failopen_on_empty_account(tmp_path):
    """空账 → 空串（fail-open；每测试独立库，迁移一次性语义不干扰）。"""
    assert AX._examples_prompt_zh(tmp_path, "随便一句", principal="anonymous", endpoint_fp="") == ""


def test_prompt_section_format(tmp_path):
    path = tmp_path / ".userdata" / AX._EXAMPLES_LEDGER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"utterance": "把 upload_a.json 删掉",
         "steps": [{"verb": "curate.rollback", "args": "curate.rollback（target=upload_a.json）"}]},
        ensure_ascii=False) + "\n", encoding="utf-8")
    zh = AX._examples_prompt_zh(tmp_path, "把 upload_b.json 删掉",
                                principal="anonymous", endpoint_fp="")
    assert "已确认知识" in zh and "把 upload_a.json 删掉" in zh and "curate.rollback" in zh
    assert "与本轮明示冲突时以本轮为准" in zh, "投影必须自带「不凌驾本轮明示」框架"
    assert '"verified_success": false' in zh, "未核验成功必须如实标记，不得冒充成功事实"


def test_build_action_prompt_examples_zh_is_additive():
    base = AP.build_action_prompt("把结果打包", has_results=True, result_total=5)
    same = AP.build_action_prompt("把结果打包", has_results=True, result_total=5, examples_zh="")
    assert base == same, "examples_zh 缺省/空串必须与历史逐位一致"
    richer = AP.build_action_prompt("把结果打包", has_results=True, result_total=5,
                                    examples_zh="----- 历史成功操作 -----\n用户说：「x」→ 正确动作：curate.list")
    assert "历史成功操作" in richer and richer.startswith(base[:200])


def test_context_zh_places_examples_before_the_utterance():
    ctx = AX._context_zh("把 upload_b.json 删掉", has_results=False, result_total=0,
                         retrieval=None, current_query="", current_filters=None,
                         examples_zh="----- 历史成功操作 -----\n用户说：「a」→ 正确动作：curate.list")
    assert ctx.index("历史成功操作") < ctx.index("----- 用户这一句 -----")


# ---------------------------------------------------------------- 分区（验证）
#
# 原话是隐私面：知识条目打（principal 会话账户 + endpoint_fp 端点指纹）双键标，
# 候选/注入都只取同分区。存量行（分区前落盘、无字段）按 ("anonymous","") 计——宁可少注不泄漏。

def test_partitioned_record_and_load(tmp_path):
    """同分区才可见/可提交/可注入：换账户/换端点都看不见也提交不了；无身份 fail-closed。"""
    steps = [_step("curate.rollback", slots={"target": "upload_a.json"})]
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, steps,
                             principal="u-alice", endpoint_fp=EP1)
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                     principal="u-alice", endpoint_fp=EP1) == [], "勾选前注入为空"
    with pytest.raises(RunError) as exc:
        AX.approve_example_candidates(tmp_path,
            [r["id"] for r in _cand_rows(tmp_path, "u-alice", EP1)],
            principal="u-bob", endpoint_fp=EP1)
    # 跨账户提案先经 change 查找、再经迁移映射兜底，均不命中 → not_found（fail-closed）
    assert exc.value.code == 'knowledge_not_found'
    assert _ledger_rows(tmp_path, "u-alice", EP1) == [] and len(_cand_rows(tmp_path, "u-alice", EP1)) == 1
    ids = [r["id"] for r in AX.list_example_candidates(tmp_path, principal="u-alice", endpoint_fp=EP1)]
    out = AX.approve_example_candidates(tmp_path, ids, principal="u-alice", endpoint_fp=EP1)
    assert out["approved"] == 0 and out["stage"] == "draft" and len(out["changes"]) == 1, \
        "勾选只是提案；真人提交前不得生效"
    _commit_change(tmp_path, out["changes"][0], principal="u-alice", endpoint_fp=EP1)
    hit = AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                    principal="u-alice", endpoint_fp=EP1)
    assert hit and hit[0]["utterance"] == "把 upload_a.json 删掉"
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                     principal="u-bob", endpoint_fp=EP1) == [], "跨账户不得注入"
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                     principal="u-alice", endpoint_fp=EP2) == [], "跨端点不得注入"
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉") == [], \
        "无身份缺省 → 空表（fail-closed 新口径，取代旧内部直调不过滤）"
    rows = _ledger_rows(tmp_path, "u-alice", EP1)
    assert rows[0]["principal"] == "u-alice" and rows[0]["endpoint_fp"] == EP1
    assert _cand_rows(tmp_path, "u-alice", EP1) == [], "迁入后候选池不留残影（提案已 committed）"


def test_legacy_rows_only_serve_anonymous_noendpoint_partition(tmp_path):
    """分区前落盘的存量行按 ("anonymous","") 计：实名/有端点指纹的调用不注入它们
    （宁可少注不泄漏）；匿名 + 空端点分区仍可见。"""
    path = tmp_path / ".userdata" / AX._EXAMPLES_LEDGER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"utterance": "把 upload_a.json 删掉",
                                "steps": [{"verb": "curate.rollback", "args": "curate.rollback（target=upload_a.json）"}]},
                               ensure_ascii=False) + "\n", encoding="utf-8")
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                     principal="u-alice", endpoint_fp=EP1) == []
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                     principal="anonymous", endpoint_fp=""), "存量行只回灌匿名+空端点分区"


def test_same_utterance_different_partitions_both_recorded(tmp_path):
    """同一句原话分属两个分区 → 两份提案各录各的（跨分区的逐字重复不误杀）。"""
    steps = [_step("curate.db_status")]
    AX._maybe_record_success(tmp_path, "库里有什么", {}, steps, principal="u-alice", endpoint_fp=EP1)
    AX._maybe_record_success(tmp_path, "库里有什么", {}, steps, principal="u-bob", endpoint_fp=EP1)
    assert len(_cand_rows(tmp_path, "u-alice", EP1)) == 1
    assert len(_cand_rows(tmp_path, "u-bob", EP1)) == 1


def test_endpoint_fp_from_config_discriminates_and_ignores_key():
    """端点指纹：base_url/model 变则变；api_key 永不参与（换 key 不换分区）。"""

    class _Cfg:
        base_url = "https://api-a.example.com"
        model = "m1"
        api_key = "sk-" + "A" * 24

    fp = AX._endpoint_fp_from_config(_Cfg)
    assert AX._endpoint_fp_from_config(_Cfg) == fp
    class _Cfg2(_Cfg):
        api_key = "sk-" + "B" * 24
    assert AX._endpoint_fp_from_config(_Cfg2) == fp, "换 key 不得换分区"
    class _Cfg3(_Cfg):
        model = "m2"
    assert AX._endpoint_fp_from_config(_Cfg3) != fp
    class _Cfg4(_Cfg):
        base_url = "https://api-b.example.com"
    assert AX._endpoint_fp_from_config(_Cfg4) != fp


# ---------------------------------------------------------------- 收录质量闸
#
# 「跑通」不等于「干得漂亮」：被机械闸修好/打回/掐停/降级兜底的执行，其「原话→动作」
# 映射正是会教偏模型的毒样例——一遍过才录，宁可少录不录脏。kw-only 缺省全为「干净」，
# 旧有用例逐位不变。

def test_quality_gate_blocks_tainted_success(tmp_path):
    """六类「跑通但不漂亮」各试一次：全部连候选池都不进。"""
    good = [_step("curate.rollback", slots={"target": "upload_a.json"})]
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, good, repairs=1)        # 首步被修
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, good, finish_vetoes=1)  # 收尾被打回
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, good, reask_write_count=1)
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, good, truncated=True)   # 掐停
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, good, mode="json")      # 跌兜底
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, good, checklist_dropped=1)
    assert _cand_rows(tmp_path) == []


def test_quality_gate_clean_run_still_recorded(tmp_path):
    """全部质量信号显式为零 → 照录进候选池（与缺省旧行为逐位一致）。"""
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                             [_step("curate.rollback", slots={"target": "upload_a.json"})],
                             mode="tools", repairs=0, finish_vetoes=0,
                             reask_write_count=0, truncated=False, checklist_dropped=0)
    assert len(_cand_rows(tmp_path)) == 1


# ---------------------------------------------------------------- 用户挑选入库
#
# 机械收录只进候选提案；真人提交才生效（注入侧只读生效条目）。同内容幂等、驳回清池、
# 跨分区不可见也不可批。

def test_approve_proposes_draft_and_human_commit_activates(tmp_path):
    """勾选 → 只是草稿提案（approved 恒 0，诚实计数）；真人评估+提交后才生效可注入。"""
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                             [_step("curate.rollback", slots={"target": "upload_a.json"})])
    cand = AX.list_example_candidates(tmp_path, principal="anonymous", endpoint_fp="")
    assert len(cand) == 1
    out = AX.approve_example_candidates(tmp_path, [cand[0]["id"]], principal="anonymous", endpoint_fp="")
    assert out["approved"] == 0 and out["duplicated"] == 0 and out["stage"] == "draft"
    assert len(out["changes"]) == 1
    assert _ledger_rows(tmp_path) == [], "真人提交前正式库必须仍为空"
    _commit_change(tmp_path, out["changes"][0], principal="anonymous", endpoint_fp="")
    assert _cand_rows(tmp_path) == [] and len(_ledger_rows(tmp_path)) == 1
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                     principal="anonymous", endpoint_fp=""), "真人提交后才可注入"


def test_approve_requires_identity_fail_closed(tmp_path):
    """无身份的内部直调不再放行：提案定位必须带本人身份（与 MCP/Web 同口径）。"""
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                             [_step("curate.rollback", slots={"target": "upload_a.json"})])
    cid = AX.list_example_candidates(tmp_path, principal="anonymous", endpoint_fp="")[0]["id"]
    with pytest.raises(RunError):
        AX.approve_example_candidates(tmp_path, [cid])
    with pytest.raises(RunError):
        AX.dismiss_example_candidates(tmp_path, [cid])


def test_duplicate_recording_is_idempotent_and_committed_leave_pool(tmp_path):
    """旧「再勾计 duplicated」语义的新链等价：同内容重复收录幂等（候选池不增），
    已提交提案离开候选池；对已定案提案再评估被 CAS 拒绝，生效条目唯一。"""
    steps = [_step("curate.rollback", slots={"target": "upload_a.json"})]
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, steps)
    first = AX.list_example_candidates(tmp_path, principal="anonymous", endpoint_fp="")[0]["id"]
    out = AX.approve_example_candidates(tmp_path, [first], principal="anonymous", endpoint_fp="")
    committed = _commit_change(tmp_path, out["changes"][0], principal="anonymous", endpoint_fp="")
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, steps)
    assert AX.list_example_candidates(tmp_path, principal="anonymous", endpoint_fp="") == [], \
        "已提交提案离开候选池；同内容再收录幂等"
    assert len(_ledger_rows(tmp_path)) == 1
    service = _service(tmp_path)
    again = service.assess("anonymous", "", "", change_id=first,
        expected_change_revision=committed["change_revision"], cfg=_CFG1, predictor=_known)
    assert again["error_code"] == "knowledge_change_conflict", "已定案提案不能重评改写"
    assert len(_ledger_rows(tmp_path)) == 1


def test_dismiss_removes_from_pool_only(tmp_path):
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                             [_step("curate.rollback", slots={"target": "upload_a.json"})])
    cid = AX.list_example_candidates(tmp_path, principal="anonymous", endpoint_fp="")[0]["id"]
    out = AX.dismiss_example_candidates(tmp_path, [cid], principal="anonymous", endpoint_fp="")
    assert out["ok"] and out["dismissed"] == 1
    assert _cand_rows(tmp_path) == [] and _ledger_rows(tmp_path) == []
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                     principal="anonymous", endpoint_fp="") == [], "驳回的不得注入"


def test_candidates_invisible_across_partitions(tmp_path):
    """候选展示同区过滤：换账户/换端点都看不见（与注入侧同一套分区纪律）。"""
    AX._maybe_record_success(tmp_path, "库里有什么", {}, [_step("curate.db_status")],
                             principal="u-alice", endpoint_fp=EP1)
    assert AX.list_example_candidates(tmp_path, principal="u-alice", endpoint_fp=EP1)
    assert AX.list_example_candidates(tmp_path, principal="u-bob", endpoint_fp=EP1) == []
    assert AX.list_example_candidates(tmp_path, principal="u-alice", endpoint_fp=EP2) == []


# ---------------------------------------------------------------- 观测留痕

def test_corrupted_candidates_pool_is_quarantined_with_reason(tmp_path):
    """候选池原档损坏行的新口径：迁移隔离并带可读原因（migration()['quarantine']），
    不解析进活跃面、不掀翻读取——「原档坏了」与「本来没候选」事后可区分。"""
    path = tmp_path / ".userdata" / AX._EXAMPLE_CANDIDATES_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("这不是json\n{bad json}\n", encoding="utf-8")
    migration = _service(tmp_path).store.migration()
    assert migration["quarantined"] == 2
    reasons = {q["filename"]: q["reason"] for q in migration["quarantine"]}
    assert reasons[AX._EXAMPLE_CANDIDATES_NAME]
    assert AX.list_example_candidates(tmp_path, principal="anonymous", endpoint_fp="") == []


def test_corrupted_ledger_is_quarantined_and_injects_nothing(tmp_path):
    """正式库原档损坏 → 注入侧按无生效示例的口径不变；损坏行经迁移隔离留痕。"""
    path = tmp_path / ".userdata" / AX._EXAMPLES_LEDGER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{{{\n", encoding="utf-8")
    migration = _service(tmp_path).store.migration()
    assert migration["quarantined"] == 1 and migration["quarantine"][0]["reason"]
    assert AX._load_success_examples(tmp_path, "把 upload_a.json 删掉",
                                     principal="anonymous", endpoint_fp="") == []


def test_candidate_write_failure_warns(tmp_path, capsys, monkeypatch):
    """候选提案写库失败不再零痕迹——收录语义不变（不掀翻主流程），
    stderr 留一行可定位的痕迹。"""
    def _boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(KnowledgeService, "propose", _boom)
    AX._WARNED.clear()
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                             [_step("curate.rollback", slots={"target": "upload_a.json"})])
    assert "候选池写盘失败" in capsys.readouterr().err


def test_audit_write_failures_warn(tmp_path, capsys, monkeypatch):
    """联网账本与降级审计账本的写盘失败统一 `_warn_once` 纪律——
    观测设施自身故障必须可观测（同一原因只打一行，不刷屏）。"""
    from dataset_recommender.corpus import net_ledger as NL

    def _boom(path, row):
        raise OSError("disk full")

    monkeypatch.setattr(NL, "append_jsonl", _boom)
    AX._WARNED.clear()
    AX._audit_loop_tool(tmp_path, "curate.check_updates", {}, True, "")
    AX._audit_loop_tool(tmp_path, "curate.check_updates", {}, True, "")
    AX._audit_fallback(tmp_path, "decide", "channel_down", "原话", "model-x")
    err = capsys.readouterr().err
    assert err.count("联网账本写盘失败") == 1, "同一原因只打一行（warn once 纪律）"
    assert "降级审计账本写盘失败" in err
