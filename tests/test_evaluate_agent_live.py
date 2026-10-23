# -*- coding: utf-8 -*-
"""集成验证 harness（scripts/evaluate_agent_live.py）的离线单测。

覆盖三个增量（全部离线，绝不发 LLM 请求）：
1. 失败聚类 `_cluster_failures`：按 (首败维度, 首个 ok=false 的 agent 节点, verb)
   三元组聚簇 + 概括模板；
2. 新维 `number_grounded`：汇报整数必须能在 steps 工具返回 JSON 里数值相等命中，
   含词边界、千分位、步序号/百分比/年份/日期豁免与不适用口径；
3. 沙箱提示词保真度 `_stage_prompts`（2026-09-30 P1）：沙箱根内必须能读到真源
   `prompts/`——运行期经 `_prompt_md` 加载的提示词读到线上真源文件，而不是内置降级常量；
   真源缺席时降级常量仍可达（保命档语义不变）。
score_case 其余维度的行为钉在 harness 自带 `--selftest` 里，这里不重复。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

import evaluate_agent_live as eal  # noqa: E402

_DB_RESULT = {"total_records": 4756, "sources": [
    {"source": "10x", "label": "10x Genomics", "local_count": 774,
     "snapshot_date": "2026-08-01"}]}


def _step(verb="curate.db_status", ok=True, result=None):
    s = {"verb": verb, "ok": ok, "slots": {}, "readonly": True}
    if result is not None:
        s["result"] = result
    return s


def _dims(plan, expect=None):
    case = {"id": "t01", "cat": "A单步路由", "utterance": "u",
            "tools": {}, "expect": expect if expect is not None else {"first": "none"}}
    return {c["dim"]: c for c in eal.score_case(case, plan, [])}


# ------------------------------------------------------------------ number_grounded

def test_number_grounded_misses_grounded_and_thousands():
    steps = [_step(result=_DB_RESULT)]
    assert eal._number_grounded_misses(steps, "库内共 4756 条。") == []
    # 千分位逗号先剥离，4,756 ≡ 4756
    assert eal._number_grounded_misses(steps, "库内共 4,756 条。") == []
    # 负载深处的数字同样有出处（774 / 的 08、01）
    assert eal._number_grounded_misses(steps, "10x 有 774 条，快照 2026-08-01。") == []


def test_number_grounded_misses_ungrounded_and_word_boundary():
    steps = [_step(result=_DB_RESULT)]
    # 无出处 → 记缺
    assert eal._number_grounded_misses(steps, "库内共 9999 条。") == ["9999"]
    # 词边界：47560 与 47 都不许命中 4756
    assert eal._number_grounded_misses(steps, "库内共 47560 条。") == ["47560"]
    assert eal._number_grounded_misses(steps, "库内共 47 条。") == ["47"]
    # 多个无出处数字按序去重列出
    assert eal._number_grounded_misses(steps, "甲 111 乙 222 丙 111。") == ["111", "222"]


def test_number_grounded_exemptions():
    steps = [_step(result=_DB_RESULT)]
    # ① ≤9 且紧邻「步」的步骤序号（「3 步」「3步」）
    assert eal._number_grounded_misses(steps, "共跑 3 步，库内 4756 条。") == []
    assert eal._number_grounded_misses(steps, "共跑3步，库内4756条。") == []
    # ② 百分比 / 年份 / 日期头
    assert eal._number_grounded_misses(steps, "覆盖率 100%，库内 4756 条。") == []
    assert eal._number_grounded_misses(steps, "截至 2026 年，库内 4756 条。") == []
    assert eal._number_grounded_misses(steps, "快照 2026-08，库内 4756 条。") == []
    # 豁免只管豁免项：同句里别的无出处数字照旧记缺
    assert eal._number_grounded_misses(steps, "共跑 3 步，库内 8888 条。") == ["8888"]


def test_number_grounded_score_case_gating():
    # steps 非空 + report 非空 → 参评
    d = _dims({"verb": "curate.db_status", "steps": [_step(result=_DB_RESULT)],
               "report_zh": "库内共 4756 条。"})
    assert d["number_grounded"]["ok"] is True
    d = _dims({"verb": "curate.db_status", "steps": [_step(result=_DB_RESULT)],
               "report_zh": "库内共 9999 条。"})
    assert d["number_grounded"]["ok"] is False
    assert "9999" in d["number_grounded"]["detail"]
    # 空 steps / 空 report → 不适用不参评（不进分母）
    assert "number_grounded" not in _dims(
        {"verb": "none", "steps": [], "report_zh": "共 123 条。"})
    assert "number_grounded" not in _dims(
        {"verb": "curate.db_status", "steps": [_step(result=_DB_RESULT)],
         "report_zh": ""})
    assert "number_grounded" not in _dims(
        {"verb": "curate.db_status", "steps": [_step(result=_DB_RESULT)]})


def test_number_grounded_in_new_strict_dims():
    # 计入严格分、剔除出旧维口径（照 v4/v5 新维做法进 _NEW_STRICT_DIMS）
    assert "number_grounded" in eal._NEW_STRICT_DIMS
    assert "number_grounded" not in eal._V3_DIMS


def test_number_grounded_non_dict_results_ignored():
    # result 非 dict/list（异常形状）不参与出处池，也不许炸
    steps = [_step(result="not-a-dict"), _step(result=_DB_RESULT)]
    assert eal._number_grounded_misses(steps, "库内 4756 条，其他 42 个。") == ["42"]


# ------------------------------------------------------------------ 失败聚类

def _rec(cid, dim, node, verb, extra_ok_check=False):
    checks = ([{"dim": "first", "ok": True, "detail": ""}] if extra_ok_check else [])
    checks.append({"dim": dim, "ok": False, "detail": ""})
    trace = ([{"node": node, "ok": False}] if node != "—" else [])
    return {"id": cid, "verb": verb, "checks": checks, "trace": trace}


def test_cluster_failures_groups_by_triple():
    fails = [
        _rec("x01", "must_steps", "execute", "curate.db_status"),
        _rec("x02", "must_steps", "execute", "curate.db_status",
             extra_ok_check=True),  # 首个 ok=false 才是首败维（ok=true 的不算）
        _rec("x03", "must_steps", "narrate", "curate.db_status"),  # 节点不同 → 另一簇
    ]
    cl = eal._cluster_failures(fails)
    assert len(cl) == 2
    big, small = cl  # 按簇大小降序
    assert big[0] == ("must_steps", "execute", "curate.db_status")
    assert big[1] == ["x01", "x02"]
    assert big[2] == "少跑了步"
    assert small[1] == ["x03"]


def test_cluster_failures_summary_templates_and_unknown_dim():
    templates = {"chain_complete": "链没走完", "must_steps": "少跑了步",
                 "report_contains": "汇报缺内容", "faithful": "汇报与实录矛盾",
                 "first": "首步判错"}
    fails = [_rec(f"c{i:02d}", dim, "execute", "v")
             for i, dim in enumerate(templates)]
    fails.append(_rec("c99", "weird_dim", "execute", "v"))
    cl = eal._cluster_failures(fails)
    summaries = {key[0]: summary for key, _ids, summary in cl}
    for dim, text in templates.items():
        assert summaries[dim] == text
    assert summaries["weird_dim"] == "weird_dim"  # 未知名维度原样显示


def test_cluster_failures_fallbacks():
    # trace 没有 ok=false 节点 → "—"；verb 缺失 → "—"；`_` 前缀信号维不算首败维
    rec = {"id": "x01", "verb": None,
           "checks": [{"dim": "_guard_events", "ok": True, "detail": ""},
                      {"dim": "faithful", "ok": False, "detail": ""}],
           "trace": [{"node": "understand", "ok": True}]}
    (key, ids, summary), = eal._cluster_failures([rec])
    assert key == ("faithful", "—", "—")
    assert ids == ["x01"]
    assert summary == "汇报与实录矛盾"
    # 空失败列表 → 空聚类（全过运行报告里该节为空）
    assert eal._cluster_failures([]) == []


# ---------------------------------------------------------- 沙箱提示词保真度（P1）

#: 运行期（非导入期装配）经 `_prompt_md` 取文本的四份：沙箱根里没有真源 prompts/ 时
#: 全部静默落内置降级常量 —— 探针就会测错对象。
_LAZY_PROMPTS = ("query_rewrite.md", "route_consensus.md", "route_write_gate.md", "compare.md")


def _truth_of(name: str) -> str:
    """真源文本（领域包目录优先，与 `_prompt_resource_text` 同口径）。"""
    for cand in (eal._PROMPTS_SRC / "domains" / "biodata" / name, eal._PROMPTS_SRC / name):
        if cand.is_file():
            return cand.read_text(encoding="utf-8").strip()
    raise AssertionError(f"真源里没有 {name}")


def _read_in_sandbox(tmp_path, name: str, fallback: str, src=None) -> str:
    """复刻 `_execute_case` 的沙箱前提读一份提示词：搬真源 → 根猴补丁 → 冷缓存读。

    缓存必须冷（`_prompt_md` 按 (domain_id, name) 缓存）：热缓存会盖掉沙箱解析结果，
    断言就退化成「上一次读到什么」。读完把根与缓存都还原，不污染同进程其他测试。
    """
    eal._stage_prompts(tmp_path, src=src)
    key = (eal.ax._get_domain().domain_id, name)
    old_cache = eal.ax._PROMPT_MD_CACHE.pop(key, None)
    old_root = eal.ax._agent_project_root
    eal.ax._agent_project_root = lambda: Path(tmp_path)
    try:
        return eal.ax._prompt_md(name, fallback)
    finally:
        eal.ax._agent_project_root = old_root
        eal.ax._PROMPT_MD_CACHE.pop(key, None)
        if old_cache is not None:
            eal.ax._PROMPT_MD_CACHE[key] = old_cache


def test_sandbox_prompt_fidelity_runtime_loaders_read_true_source(tmp_path):
    """沙箱内四份运行期提示词必须读真源文件，不是 215/164/116/137 字的内置降级常量。"""
    fallbacks = {
        "query_rewrite.md": eal.ax._QUERY_REWRITE_PROMPT_FALLBACK_ZH,
        "route_consensus.md": eal.ax._ROUTE_CONSENSUS_FALLBACK_ZH,
        "route_write_gate.md": eal.ax._ROUTE_WRITE_GATE_FALLBACK_ZH,
        "compare.md": eal.ax._COMPARE_PROMPT_FALLBACK_ZH,
    }
    assert set(fallbacks) == set(_LAZY_PROMPTS)
    for name in _LAZY_PROMPTS:
        truth, fallback = _truth_of(name), fallbacks[name]
        got = _read_in_sandbox(tmp_path, name, fallback)
        assert got != fallback, f"{name} 落降级常量——沙箱没搬真源"
        assert got == truth, f"{name} 沙箱内文本 ≠ 真源文件"
        assert len(got) > len(fallback)


def test_sandbox_route_write_gate_matches_true_source():
    """回归钉（P1 事故本体）：route_write_gate 真源 1150 字节（479 字）≠ 215 字常量。"""
    fallback = eal.ax._ROUTE_WRITE_GATE_FALLBACK_ZH
    truth = _truth_of("route_write_gate.md")
    assert len(truth) == 479 and len(fallback) == 215  # 两侧都留长度钉，改动可见
    assert "写盘工具闸" in truth and '"gate"' in truth  # 文件特征子串（非降级常量）
    assert "写盘工具闸" not in fallback


def test_sandbox_prompt_fidelity_covers_every_prompt_file(tmp_path):
    """整树搬运的结构保证：真源 `prompts/` 下每一份 md 在沙箱内解析结果逐字相等。

    不是白名单断言——领域包目录与骨架目录都按 `_prompt_resource_text` 的真实查表顺序
    逐个核（新增/改名提示词自动进覆盖，探针里不会悄悄漏掉一份）。
    """
    files = sorted(eal._PROMPTS_SRC.rglob("*.md"))
    assert files, f"{eal._PROMPTS_SRC} 下没有提示词文件"
    for path in files:
        name = path.name
        domain_copy = eal._PROMPTS_SRC / "domains" / "biodata" / name  # 领域包目录优先
        truth = domain_copy if domain_copy.is_file() else path
        assert _read_in_sandbox(tmp_path, name, "") == truth.read_text(
            encoding="utf-8").strip(), name


def test_sandbox_prompt_fallback_reachable_when_source_absent(tmp_path):
    """真源缺席（打包/异常）时降级路径仍可达——内置最小版是生产保命档，语义不动。"""
    empty_src = tmp_path / "empty_src"
    empty_src.mkdir()
    box = tmp_path / "box_empty"
    assert eal._stage_prompts(box, src=empty_src) is True  # 搬运成功但沙箱里没有那份文件
    assert _read_in_sandbox(box, "route_write_gate.md",
                            eal.ax._ROUTE_WRITE_GATE_FALLBACK_ZH,
                            src=empty_src) == eal.ax._ROUTE_WRITE_GATE_FALLBACK_ZH

    box2 = tmp_path / "box_missing"
    assert eal._stage_prompts(box2, src=tmp_path / "no_such_dir") is False
    assert not (box2 / "prompts").exists()
    assert _read_in_sandbox(box2, "route_write_gate.md",
                            eal.ax._ROUTE_WRITE_GATE_FALLBACK_ZH,
                            src=tmp_path / "no_such_dir") == eal.ax._ROUTE_WRITE_GATE_FALLBACK_ZH


def test_execute_case_stages_prompts_into_sandbox():
    """接线钉：`_execute_case` 必须真的引用 `_stage_prompts`（否则上面全绿也白搭）。

    读字节码而非源码文本：`_execute_case` 是 `main()` 的嵌套函数（不能直接调用），
    而源码字符串断言会被改名/重排误伤。`_stage_prompts` 是模块级全局 → 出现在 co_names。
    """
    codes = [c for c in eal.main.__code__.co_consts
             if isinstance(c, type(eal.main.__code__))]
    case_code = next(c for c in codes if c.co_name == "_execute_case")
    assert "_stage_prompts" in case_code.co_names
