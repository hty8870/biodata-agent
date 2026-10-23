# -*- coding: utf-8 -*-
"""复盘前端的静态门。

三门都不执行 JS：`web_smoke_test.py` 只做字符串检查，`node --check` 只验语法。所以
「取了但从没画」「顺序反了」这类问题在浏览器里**没有任何红灯**，只有肉眼能看见——
而复盘窗口是后台产物，肉眼检查恰恰最容易漏。这里每条都对应一次真实失效：

① `ctx.freshPids`：图谱靠它拿「本次新进展」的 pid 集。名字一改，`ctx.freshPids && …()` 会
   静默短路成空集（FRONTEND.md §4.3 那个坑），图上一个环都没有，而角标照旧正常——
   看起来就像「功能没做」，没有任何报错。
② `_onOpen` 里的顺序：先 `_markSeen()` 再取数，会把本次该打标的 unseen 提前核销掉，
   打标集恒空（本批顺手修掉的既有竞态，改回去要红）。
③ `#shell`：复盘四模块不许被拉进既有 SCC（import 环门只许缩不许扩）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "static" / "js"


def _read(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


REPLAY = _read("panel/replay.js")
REPLAY_GRAPH = _read("panel/replay_graph.js")


def test_graph_reads_the_fresh_set_from_its_host():
    """宿主必须注入 `freshPids`，图谱必须真的用它——任何一端改名都红。

    双端断言是刻意的：只断一端的话，上面①描述的静默短路能一路穿过三门。
    """
    assert "freshPids:" in REPLAY, "replay.js 没给图谱注入 freshPids（打标集取不到）"
    body = re.search(r"function render\(\)\s*\{(.*?)\n    \}", REPLAY_GRAPH, re.S)
    assert body, "找不到 render 的函数体"
    assert "ctx.freshPids" in body.group(1), "fresh 快照必须在 render 里取（否则切标签页/重画都不更新）"


def test_open_marks_seen_only_after_the_points_payload_is_in_hand():
    """开窗必须**先取数再核销**：payload 里的 `unseen` 就是本次要打标的 pid 集，
    先发 POST /api/replay/seen 会把它清掉（与角标同一真源，清了就再也标不上）。
    """
    body = re.search(r"function _onOpen\(\)\s*\{(.*?)\n\}", REPLAY, re.S)
    assert body, "找不到 _onOpen 的函数体"
    code = body.group(1)
    assert "loadPoints" in code and "_markSeen" in code
    assert code.index("loadPoints") < code.index("_markSeen"), "_onOpen 里核销早于取数——打标集会被清空"


def test_replay_modules_stay_out_of_the_shell_import_cycle():
    """复盘四模块都不许 import `#shell`：被拉进既有 SCC 会让 import 环门红，
    且环上的绑定只许在函数体内触碰（模块求值期触碰 = TDZ 崩）。"""
    for name in ("core/replay_core.js", "core/replay_layout.js", "panel/replay.js", "panel/replay_graph.js"):
        assert 'from "#shell"' not in _read(name), f"{name} 不许 import #shell"
