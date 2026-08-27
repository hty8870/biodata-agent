#!/usr/bin/env python3
"""外部模拟模型回路 harness（跨模型实验批）——用**外部模型**（k3 子 agent、
实现 CLI 等无法走仓库 LLM client 的驱动方）驱动 `agent_exec.plan_with_agent` 的同一个
六节点循环，产出与 `probe_multicall_fidelity.py` **逐字段同构**的 capture 记录。

原理：monkeypatch `ax._invoke_tool_channel`（understand/repair/decide 全部调用点共用的
三级通道），使其不调任何 API，而是把每次请求序列化到 workdir、轮询外部驱动写回的
decision 文件，再把 decision 构造成与真实 AIMessage 同形状的应答对象返回——下游
（`_raw_from_message` / `_decide_answer_kind` / 全部机械闸 / 事件留痕）吃到的与真实
响应**逐位同构**。`_task_checklist_call`（understand 内 complex 车道的清单轻量调用，
直接 `chat_model.invoke`）经 `plan_with_agent` 的 chat_model 注入缝接管到同一机制——
注入 dummy 模型后 `should_use_llm` 闸与 ChatOpenAI 构建都被跳过，**全程零真实 API
调用**（config 是本文件自造的 dummy LLMConfig，不读 .env、不需要任何 key，也无需
MOCK_LLM=1）。

被 stub 的 LLM 调用点（循环后装饰，不影响 events/executions/plan_steps）：
  - `ax._report_with_llm`（单步 db_status 的 LLM 汇报）→ 恒返回 None；
  - `ax._steps_report_with_llm`（多步全程 LLM 汇报）→ 恒返回 None；
  两者返回 None 即走生产既有确定性兜底拼接（同一批事实），plan.report_source=
  "deterministic"。capture 记录不含汇报文本，验证分析器也不消费它。

=========================== 用法 ===========================

  # 外部模型驱动（每用例一个长驻进程）：
  python scripts/sim_model_loop.py run --case b08a --round 1 \
      --workdir <W> --out-dir sim-selfcheck --model-name k3

  # 查看进度：
  python scripts/sim_model_loop.py status --workdir <W>
      → DONE / ERROR:<msg> / WAITING:<i>（正在等待 decision_<i>.json）

  # 无外部模型的自验（从验证 DeepSeek 臂 capture.jsonl 重放同 case+round 的应答）：
  python scripts/sim_model_loop.py run --case b08a --round 1 --replay \
      --workdir <W> --out-dir sim-selfcheck --model-name replay-deepseek

--out-dir 相对路径按验证同口径解析到
`research/reports/multicall-fidelity-probe/<out-dir>/`，产物
capture.jsonl + trajectories/<case>_r<N>.json 可被
`python scripts/probe_multicall_fidelity.py --analyze [--judge] --out-dir <out-dir>`
原样分析。

=========================== 外部驱动契约 ===========================

循环每需要一次模型应答，harness 写 `W/request_<i>.json`（i 从 1 起单调递增，原子写），
然后每 2 秒轮询 `W/decision_<i>.json`（45 分钟未出现 → 记录 exc="decision_timeout"、
进程非零退出）。驱动方（k3 子 agent / 实现 CLI / 人工程序）：

  1. 读 `W/request_<i>.json`：
     - i / node（understand / decide / repair / checklist / json_fallback）/
       case / round / model；
     - messages：完整消息列，逐条 {"role": system|user|assistant, "content": str}，
       顺序与真实调用一致；
     - tools：本次提供的工具表 [{"name","description","parameters"}]（parameters 为
       JSON schema，与 bind 时一致；decide 节点的表里含循环控制工具 finish 与
       unsupported_next_step）；checklist/json_fallback 轮 tools 为空表；
     - choice（required/auto，checklist 轮为 null）、json_prompt（若设置）、
       refallback_on_empty。
  2. 写 `W/decision_<i>.json`（建议先写临时文件再 rename，harness 对半截文件有
     5×1s 重读容忍，之后按「畸形输出」处理——与真实模型回了垃圾同路径）：
     - 工具调用应答：{"calls": [{"name": <工具表里的名字>, "args": {...}}, ...]}；
       一次给多个调用合法（真实模型的批量调用语义：循环只吃第一个，其余留档进
       events——这正是保真度验证要量的东西，请如实给出模型的全部输出）；
     - 散文/JSON 文本应答（checklist 轮、json_fallback 轮，或模拟「模型没调工具
       只回了文本」）：{"text": "<原文>"}——checklist 轮的原文应是 JSON 数组字符串；
     - 模拟调用本身抛异常：{"error": "<异常类型名>"}（等价真实通道的 fb 路径）；
     - 结束循环：在 decide 轮调用工具表里的 finish（必填 completion_report——
       逐件核销报告，机械核销闸会真校验，与真实通道同口径）；
     - 工具名必须来自该请求 request 的 tools 表；未知工具名 / args 非对象 /
       decision JSON 畸形 → 走真实通道对不可读模型输出的同一路径
       （invalid_tool_calls / invalid 分诊 → decide 重问一次后停环、understand
       跌 json_fallback 再问一轮），harness **不会**因此重新轮询同一 i。
  3. 用 status 子命令查看进度，或等 run 进程退出（0=完成，3=decision 超时，
     4=replay 与原记录有分歧，其余非零=harness 自身错误）。

畸形/未知名的忠实化口径（与真实 langchain 消息形状的映射）：
  - decision 不是 JSON 对象、calls 非数组、数组元素非对象、args 非 dict → 进
    answer.invalid_tool_calls（真实侧「工具调用参数 JSON 解析失败」的对应物）；
  - 工具名不在本请求工具表 → 保留在 answer.tool_calls 原位（真实侧「幻觉工具名」
    的对应物——`_decide_answer_kind`/`_raw_from_message` 判 invalid、多调用取
    第一个的语义与真实逐位一致），事件的 invalid 列同时留痕。

replay 模式的两个**已知不对称**（capture.jsonl 不记录这两类调用，只能重建）：
  - checklist 轮：验证 spy 只留 understand/decide 事件，清单应答原文未留档。replay
    从该记录全部事件的 loop 调用按 (verb, quoted) 去重**综合重建**清单
    （expect_verb=调用动词、anchor=quoted），再过生产 `_parse_checklist` 同一套
    机械校验——对「清单条目是否触发 finish 否决」的行为等效，措辞不与原清单相同；
  - repair 轮：验证不留 repair 事件。replay 重放上一次 understand 的调用（等价
    「模型没修」）——若原记录的 repair 真修好了，replay 会在此分歧（自验三用例
    均未触发 repair，分歧未实际发生）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dataset_recommender.agent import agent_exec as ax  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402
import probe_multicall_fidelity as probe  # noqa: E402
from evaluate_agent_live import build_tools  # noqa: E402

#: decision 轮询节奏与超时（任务书钉死：2 秒轮询、45 分钟超时）。
POLL_S = 2.0
DECISION_TIMEOUT_S = 45 * 60

#: decision 文件存在但 JSON 读不出时的重读容忍（非原子写法的半截文件）——
#: 重读耗尽后按「模型回了垃圾」处理，与真实通道对不可读输出同路径，**不重问同一 i**。
_REREAD_TRIES = 5
_REREAD_S = 1.0


class _DecisionTimeout(Exception):
    """decision_<i>.json 45 分钟未出现。"""


class _ReplayExhausted(Exception):
    """replay 队列耗尽：本次运行比原记录多要了模型应答（流程已分歧）。"""


class _SimAnswer:
    """与 langchain AIMessage 同形状的最小应答对象：下游只读这五个属性
    （`_raw_from_message`/`_decide_answer_kind`/`_classify` 读 tool_calls，
    `_message_text` 读 content，验证序列化器读 tool_calls/invalid_tool_calls，
    `_usage_record` 读 usage_metadata——逐一核对过全部访问点）。"""

    def __init__(self, content="", tool_calls=None, invalid_tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.invalid_tool_calls = invalid_tool_calls or []
        self.response_metadata = {}
        self.usage_metadata = None


# --------------------------------------------------------------------------- 序列化

_ROLE_MAP = {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}


def _render_messages(messages) -> list[dict]:
    """langchain 消息列 → 逐条 {"role","content"}（保留 role 与顺序；content blocks
    形态经 `_message_text` 拼出文本段，与下游阅读口径一致）。"""
    out = []
    for m in messages or []:
        mtype = str(getattr(m, "type", "") or "")
        out.append({"role": _ROLE_MAP.get(mtype, mtype or "unknown"),
                    "content": ax._message_text(m)})
    return out


def _render_tools(tools) -> list[dict]:
    """OpenAI 函数规格 → {"name","description","parameters"}（与 bind 时一致的 JSON schema）。"""
    out = []
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        out.append({"name": fn.get("name"), "description": fn.get("description"),
                    "parameters": fn.get("parameters")})
    return out


def _build_answer(decision, valid_names: set) -> _SimAnswer:
    """decision JSON → 与真实 AIMessage 同形状的应答（映射口径见模块 docstring）。

    畸形 → tool_calls 空 + invalid_tool_calls 留痕 + content 空：下游落到「不可读」
    分诊（decide 重问一次后停环；understand 由 refallback_on_empty 跌 json_fallback
    再问一轮）——与真实模型回了垃圾的路径逐位一致。"""
    if not isinstance(decision, dict):
        return _SimAnswer(invalid_tool_calls=[{
            "name": None, "args": str(decision)[:500],
            "error": "malformed_decision: 不是 JSON 对象"}])
    calls = decision.get("calls")
    text = decision.get("text")
    if calls is None and isinstance(text, str):
        return _SimAnswer(content=text)
    if not isinstance(calls, list):
        return _SimAnswer(invalid_tool_calls=[{
            "name": None, "args": str(decision)[:500],
            "error": "malformed_decision: calls 不是数组"}])
    tool_calls, invalid = [], []
    for c in calls:
        if not isinstance(c, dict):
            invalid.append({"name": None, "args": str(c)[:200],
                            "error": "malformed_call: 不是对象"})
            continue
        name, args = c.get("name"), c.get("args")
        if isinstance(args, dict) is False and args is not None:
            invalid.append({"name": name if isinstance(name, str) else None,
                            "args": args, "error": "args_not_object"})
            continue
        entry = {"name": name, "args": args if isinstance(args, dict) else {}}
        if not isinstance(name, str) or not name or name not in valid_names:
            # 幻觉工具名：真实侧会原样躺在 tool_calls 里由下游判 invalid（多调用取
            # 第一个的语义不变），同时在 invalid 列留痕供验证事件对照。
            tool_calls.append(entry)
            invalid.append({"name": name if isinstance(name, str) else None,
                            "args": entry["args"],
                            "error": f"unknown_tool: {name!r} 不在本请求工具表"})
            continue
        tool_calls.append(entry)
    # replay 透传的原记录 invalid 列（DeepSeek 臂实测恒空，为完整性保留）。
    for c in decision.get("invalid") or []:
        if isinstance(c, dict):
            invalid.append({"name": c.get("name"), "args": c.get("args"),
                            "error": str(c.get("error") or "")})
    return _SimAnswer(tool_calls=tool_calls, invalid_tool_calls=invalid)


# --------------------------------------------------------------------------- 驱动

def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_state(workdir: Path, phase: str, **extra) -> None:
    payload = {"phase": phase,
               "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    payload.update(extra)
    _write_atomic(workdir / "_state.json",
                  json.dumps(payload, ensure_ascii=False, indent=2))


class _FileDriver:
    """run 模式驱动：写 request_<i>.json，轮询 decision_<i>.json（2s / 45min）。"""

    def __init__(self, workdir: Path, timeout_s: float = DECISION_TIMEOUT_S,
                 meta: dict | None = None):
        self.workdir = workdir
        self.timeout_s = float(timeout_s)
        self.meta = meta or {}
        self.i = 0

    def request(self, payload: dict) -> dict:
        self.i += 1
        i = self.i
        body = {"i": i, **self.meta, **payload}
        _write_atomic(self.workdir / f"request_{i}.json",
                      json.dumps(body, ensure_ascii=False, indent=2))
        _write_state(self.workdir, "waiting", i=i)
        decision_path = self.workdir / f"decision_{i}.json"
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if decision_path.is_file():
                for _try in range(_REREAD_TRIES):
                    try:
                        return json.loads(decision_path.read_text(encoding="utf-8"))
                    except (ValueError, OSError):
                        time.sleep(_REREAD_S)
                # 重读耗尽：按「模型回了垃圾」处理（真实通道对不可读输出同路径）。
                return {"_malformed": decision_path.read_text(
                    encoding="utf-8", errors="replace")[:500]}
            time.sleep(POLL_S)
        raise _DecisionTimeout(f"decision_{i}.json {int(self.timeout_s)}s 未出现")


class _ReplayDriver:
    """replay 模式驱动：从验证 DeepSeek 臂记录的 events 依次取应答（零外部模型）。

    understand/decide 轮按序弹出记录事件，calls/invalid/fb 原样回放；checklist 轮
    综合重建（见模块 docstring「已知不对称」）；repair 轮重放最近一次 understand
    的调用。request 文件照常落盘（便于对照检查），但不轮询 decision。"""

    def __init__(self, events: list[dict], workdir: Path | None = None,
                 meta: dict | None = None):
        self._queue = [e for e in events if e.get("node") in ("understand", "decide")]
        self._all_events = list(events)
        self._last_understand: dict | None = None
        self.workdir = workdir
        self.meta = meta or {}
        self.i = 0
        self.repair_rounds = 0   # run 结束后供汇报：replay 命中的 repair 轮数

    def _emit(self, payload: dict) -> int:
        self.i += 1
        if self.workdir is not None:
            body = {"i": self.i, **self.meta, **payload}
            _write_atomic(self.workdir / f"request_{self.i}.json",
                          json.dumps(body, ensure_ascii=False, indent=2))
            _write_state(self.workdir, "waiting", i=self.i)
        return self.i

    def request(self, payload: dict) -> dict:
        node = str(payload.get("node") or "")
        self._emit(payload)
        if node == "checklist":
            return {"text": json.dumps(self._synth_checklist(), ensure_ascii=False)}
        if node == "repair":
            self.repair_rounds += 1
            calls = (self._last_understand or {}).get("calls") or []
            return {"calls": calls}
        if not self._queue:
            raise _ReplayExhausted(
                f"replay 队列已空仍被请求（node={node}）——流程与原记录已分歧")
        ev = self._queue.pop(0)
        if ev.get("node") == "understand":
            self._last_understand = ev
        decision = {"calls": ev.get("calls") or [], "invalid": ev.get("invalid") or []}
        if ev.get("fb"):
            decision["fb"] = ev["fb"]   # 原记录的兜底档标注原样透传
        return decision

    def _synth_checklist(self) -> list[dict]:
        """从记录事件的 loop 调用重建清单（capture 未留清单应答原文——replay 专用）。"""
        n2v = probe._name_to_verb_union()
        items, seen = [], set()
        for ev in self._all_events:
            for c in ev.get("calls") or []:
                verb = n2v.get(str((c or {}).get("name") or ""))
                if verb not in probe._LOOP_VERBS:
                    continue
                anchor = str(((c or {}).get("args") or {}).get("quoted") or "")
                key = (verb, ax._norm_source(anchor))
                if key in seen:
                    continue
                seen.add(key)
                items.append({"text": anchor[:40] or verb, "anchor": anchor,
                              "expect_verb": verb})
        return items[:8]


class _SimChatModel:
    """注入 `plan_with_agent(chat_model=...)` 的假模型：唯一的活口是 `invoke`
    （`_task_checklist_call` 的清单轻量调用走这里进 request/decision 机制）。
    `bind_tools` 是绊线——工具通道必须经 monkeypatch 后的 `_invoke_tool_channel`，
    谁走到这里说明补丁没盖住，立即炸出来。"""

    def __init__(self, driver):
        self._driver = driver

    def invoke(self, messages):
        decision = self._driver.request({
            "node": "checklist", "kind": "chat", "choice": None,
            "messages": _render_messages(messages), "tools": [],
            "json_prompt": None, "refallback_on_empty": False})
        return _build_answer(decision, set())

    def bind_tools(self, *_a, **_k):
        raise RuntimeError("sim harness 漏缝：工具通道未走 _invoke_tool_channel 补丁")


# --------------------------------------------------------------------------- 通道补丁

def _make_channel(driver, cur: dict):
    """`_invoke_tool_channel` 的替代实现：签名逐位一致，返回 (answer, note, fb, je)。

    忠实点：refallback_on_empty + json_prompt 且首答不可用（用生产
    `_raw_from_message` 同一真源判定）→ fb="no_tool_calls" 并发起 json_fallback 轮
    （真实通道的散文 JSON 兜底在 harness 里的对应物）；{"error"} decision 等价调用
    本身抛异常。note 恒空（required→auto 降档是 provider 400 触发的，外部回路无此
    面——外部模型不产生服务端 400）；事件留痕与验证 spy 同构。"""

    def sim_channel(chat_model, *, tools, messages, choice, json_prompt=None,
                    refallback_on_empty=False, name_to_verb=None, usage_sink=None,
                    usage_node=""):
        del chat_model, usage_sink  # 零 API；用量台账保持缺席（与 FakeModel 路径同形）
        valid_names = {str((t or {}).get("function", {}).get("name") or "")
                       for t in tools or []}

        def _round_text(prompt_text):
            return driver.request({
                "node": usage_node, "kind": "json_fallback", "choice": None,
                "messages": [{"role": "user", "content": prompt_text}],
                "tools": [], "json_prompt": json_prompt,
                "refallback_on_empty": refallback_on_empty})

        decision = driver.request({
            "node": usage_node, "kind": "tool_channel", "choice": choice,
            "messages": _render_messages(messages),
            "tools": _render_tools(tools),
            "json_prompt": json_prompt, "refallback_on_empty": refallback_on_empty})
        fb = str(decision.pop("fb", "") or "") if isinstance(decision, dict) else ""
        note, je = "", ""
        answer = None
        if isinstance(decision, dict) and decision.get("error"):
            # 调用本身抛异常的等价路径：fb=异常类型名；给了 json_prompt 就走兜底轮。
            fb = str(decision["error"])
            if json_prompt is not None:
                d2 = _round_text(json_prompt)
                if isinstance(d2, dict) and d2.get("error"):
                    je = str(d2["error"])
                else:
                    answer = _build_answer(d2, valid_names)
        else:
            answer = _build_answer(decision, valid_names)
            if (not fb and refallback_on_empty and json_prompt is not None
                    and not ax._raw_from_message(answer, name_to_verb or {})):
                fb = "no_tool_calls"
                d2 = _round_text(json_prompt)
                if isinstance(d2, dict) and d2.get("error"):
                    answer, je = None, str(d2["error"])
                else:
                    answer = _build_answer(d2, valid_names)

        if usage_node in ("understand", "decide") and answer is not None:
            cur["events"].append({
                "seq": len(cur["events"]) + len(cur["execs"]),
                "node": usage_node, "m": len(cur["execs"]), "choice": choice, "fb": fb,
                "calls": probe._ser_calls(answer), "invalid": probe._ser_invalid(answer)})
        return answer, note, fb, je

    return sim_channel


def _logging_run(cur: dict, verb: str, run):
    """与验证 `_logging_run` 逐行同构：executions 记录实际执行的调用（含失败步）。"""

    def wrapped(slots, root):
        try:
            result = run(slots, root)
        except Exception:
            cur["execs"].append({"seq": len(cur["events"]) + len(cur["execs"]),
                                 "verb": verb, "slots": dict(slots or {}), "ok": False})
            raise
        cur["execs"].append({"seq": len(cur["events"]) + len(cur["execs"]),
                             "verb": verb, "slots": dict(slots or {}), "ok": True})
        return result
    return wrapped


# --------------------------------------------------------------------------- 记录与比对

def _load_replay_record(case_id: str, rd: int) -> dict:
    """验证主目录 capture.jsonl（DeepSeek 臂）里同 case+round 的**最后一条**记录。"""
    cap = probe.OUT_DIR / "capture.jsonl"
    found = None
    for line in cap.open(encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("case") == case_id and int(r.get("round") or 0) == rd:
            found = r
    if found is None:
        print(f"replay 源记录不存在：{cap} 里没有 case={case_id} round={rd}",
              file=sys.stderr)
        raise SystemExit(2)
    return found


def _diff_against(rec: dict, orig: dict) -> list[str]:
    """replay 记录 vs 原记录：executions（verb+slots+ok 逐步）、events 全字段、
    plan_steps 三路比对，返回人读分歧行（空 = 完全一致）。"""
    lines: list[str] = []
    oe, re_ = orig.get("executions") or [], rec.get("executions") or []
    for i in range(max(len(oe), len(re_))):
        a = oe[i] if i < len(oe) else None
        b = re_[i] if i < len(re_) else None
        ka = None if a is None else (a.get("verb"), json.dumps(a.get("slots") or {},
                                     ensure_ascii=False, sort_keys=True), a.get("ok"))
        kb = None if b is None else (b.get("verb"), json.dumps(b.get("slots") or {},
                                     ensure_ascii=False, sort_keys=True), b.get("ok"))
        if ka != kb:
            lines.append(f"  executions[{i}]：原={ka} replay={kb}")
    if (orig.get("events") or []) != (rec.get("events") or []):
        oev, rev = orig.get("events") or [], rec.get("events") or []
        lines.append(f"  events 不一致（原 {len(oev)} 条 / replay {len(rev)} 条）：")
        for i in range(max(len(oev), len(rev))):
            a = oev[i] if i < len(oev) else None
            b = rev[i] if i < len(rev) else None
            if a != b:
                lines.append(f"    events[{i}] 原={json.dumps(a, ensure_ascii=False)[:200]}")
                lines.append(f"    events[{i}] 新={json.dumps(b, ensure_ascii=False)[:200]}")
    if (orig.get("plan_steps") or []) != (rec.get("plan_steps") or []):
        lines.append(f"  plan_steps 不一致：原={orig.get('plan_steps')} replay={rec.get('plan_steps')}")
    if str(orig.get("exc") or "") != str(rec.get("exc") or ""):
        lines.append(f"  exc 不一致：原={orig.get('exc')!r} replay={rec.get('exc')!r}")
    return lines


# --------------------------------------------------------------------------- run

def run_case(case_id: str, rd: int, workdir: Path, out_dir: Path,
             model_name: str, replay: bool, timeout_s: float) -> int:
    cases = probe._load_probe_cases([case_id])
    case = cases[0]
    workdir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trajectories").mkdir(exist_ok=True)

    orig_rec = _load_replay_record(case_id, rd) if replay else None
    meta = {"case": case_id, "round": rd, "model": model_name}
    driver = (_ReplayDriver(orig_rec["events"], workdir=workdir, meta=meta) if replay
              else _FileDriver(workdir, timeout_s=timeout_s, meta=meta))
    cur: dict = {"events": [], "execs": []}

    orig_channel = ax._invoke_tool_channel
    orig_report, orig_steps_report = ax._report_with_llm, ax._steps_report_with_llm
    old_tools, old_root = ax.LOOP_TOOLS, ax._agent_project_root
    # dummy config：chat_model 注入后 should_use_llm 闸与 ChatOpenAI 构建都被跳过，
    # base_url/api_key 永不触网（127.0.0.1:9 是刻意的黑洞地址，双保险）。
    cfg = LLMConfig(enable_llm=True, mock_llm=False, api_key="sim-harness-no-key",
                    base_url="http://127.0.0.1:9/sim-harness", model=model_name)
    plan: dict = {}
    exc = ""
    started = time.monotonic()
    ax._invoke_tool_channel = _make_channel(driver, cur)
    ax._report_with_llm = lambda *a, **k: None        # stub：循环后装饰（docstring）
    ax._steps_report_with_llm = lambda *a, **k: None  # stub：同上
    try:
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_tools(case.get("tools") or {})
            ax.LOOP_TOOLS = {v: {**s, "run": _logging_run(cur, v, s["run"])}
                             for v, s in spec.items()}
            ax._agent_project_root = lambda: Path(tmp)
            case_ctx = case.get("context") or {}
            try:
                plan, _trace = ax.plan_with_agent(
                    str(case["utterance"]),
                    has_results=bool(case_ctx.get("has_results", False)),
                    result_total=int(case_ctx.get("result_total") or 0),
                    config=cfg, retrieval=None,
                    current_query=str(case_ctx.get("current_query") or ""),
                    current_filters=case_ctx.get("current_filters"),
                    chat_model=_SimChatModel(driver))
            except _DecisionTimeout:
                exc = "decision_timeout"
            except Exception as err:  # noqa: BLE001
                plan, exc = {}, f"{type(err).__name__}: {str(err)[:160]}"
    finally:
        ax._invoke_tool_channel = orig_channel
        ax._report_with_llm, ax._steps_report_with_llm = orig_report, orig_steps_report
        ax.LOOP_TOOLS, ax._agent_project_root = old_tools, old_root

    rec = {"type": "run", "case": case["id"], "round": rd, "model": model_name,
           "cat": case.get("cat"), "utterance": case["utterance"],
           "events": cur["events"], "executions": cur["execs"],
           "plan_steps": [{"verb": s.get("verb"), "slots": s.get("slots"),
                           "ok": s.get("ok")}
                          for s in (plan.get("steps") or [])],
           "exc": exc,
           "ms": int((time.monotonic() - started) * 1000)}
    with (out_dir / "capture.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    (out_dir / "trajectories" / f"{case['id']}_r{rd}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")

    n_multi = sum(1 for e in rec["events"] if len(e["calls"]) >= 2)
    print(f"[{case_id} r{rd}] 执行 {len(rec['executions'])} 步，通道调用 "
          f"{len(rec['events'])} 次（多调用 {n_multi} 次），请求文件 {driver.i} 个，"
          f"{rec['ms'] / 1000:.1f}s {exc[:60]}")

    if exc == "decision_timeout":
        _write_state(workdir, "error", msg="decision_timeout")
        return 3
    if replay:
        if getattr(driver, "repair_rounds", 0):
            print(f"  ⚠ replay 命中 {driver.repair_rounds} 个 repair 轮——原记录未留 "
                  "repair 应答，本轮回放的是 understand 原调用（见 docstring 已知不对称）")
        diffs = _diff_against(rec, orig_rec)
        if diffs:
            print(f"  replay 与原记录存在 {len(diffs)} 处分歧：")
            for ln in diffs:
                print(ln)
            _write_state(workdir, "error", msg="replay_diverged")
            return 4
        print("  replay 与原记录：executions / events / plan_steps / exc 全部一致 ✓")
    _write_state(workdir, "done", case=case_id, round=rd,
                 capture=str(out_dir / "capture.jsonl"))
    return 0


def status(workdir: Path) -> int:
    state_path = workdir / "_state.json"
    if state_path.is_file():
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
        except ValueError:
            st = {}
        phase = st.get("phase")
        if phase == "done":
            print("DONE")
            return 0
        if phase == "error":
            print(f"ERROR:{st.get('msg') or ''}")
            return 0
        if phase == "waiting":
            print(f"WAITING:{st.get('i')}")
            return 0
    # 状态文件缺席（进程还没写第一个请求，或已异常退出）：从文件面推断。
    requests = sorted(workdir.glob("request_*.json"),
                      key=lambda p: int(p.stem.split("_")[1])) if workdir.is_dir() else []
    if not requests:
        print("WAITING:1")
        return 0
    last_i = int(requests[-1].stem.split("_")[1])
    for p in requests:
        i = int(p.stem.split("_")[1])
        if not (workdir / f"decision_{i}.json").is_file():
            print(f"WAITING:{i}")
            return 0
    print(f"WAITING:{last_i}")
    return 0


def main() -> int:
    ap_ = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap_.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("run", help="跑一个用例一轮（长驻进程，等外部驱动写 decision）")
    pr.add_argument("--case", required=True)
    pr.add_argument("--round", type=int, default=1)
    pr.add_argument("--workdir", required=True)
    pr.add_argument("--out-dir", required=True,
                    help="相对路径解析到探针主目录下（如 sim-selfcheck）")
    pr.add_argument("--model-name", required=True)
    pr.add_argument("--replay", action="store_true",
                    help="无外部模型自验：重放探针 DeepSeek 臂同 case+round 记录")
    pr.add_argument("--timeout-s", type=float, default=DECISION_TIMEOUT_S)
    ps = sub.add_parser("status", help="查看 workdir 进度：DONE / ERROR:<msg> / WAITING:<i>")
    ps.add_argument("--workdir", required=True)
    args = ap_.parse_args()
    if args.cmd == "status":
        return status(Path(args.workdir))
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = probe.OUT_DIR / out_dir
    return run_case(args.case, args.round, Path(args.workdir), out_dir,
                    args.model_name, args.replay, args.timeout_s)


if __name__ == "__main__":
    sys.exit(main())
