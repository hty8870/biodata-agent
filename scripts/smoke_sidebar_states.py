# -*- coding: utf-8 -*-
"""侧边栏状态机 Playwright 回归（顽疾根治的守门员）。

钉两件事：

1. **内联样式零残留**（根因）：gsap autoAlpha 的 tween 只许 clearProps:"all"——
   `.sidebar` 在 boot 动画落幕后**绝不允许**带内联 opacity/visibility（内联优先级压过
   body.side-closed 的淡出规则，侧栏收起时以全不透明姿态滑出=「没有按常规折叠」）。
2. **合法状态枚举**：任意操作序列后，body 布局类与侧栏可见性必须落在合法集合里——
   桌面宽轨 / 桌面窄轨（side-duo）/ 桌面胶囊（rail-collapsed，±rail-peek）/ 移动抽屉开 / 移动抽屉关；
   `side-closed` ⇔ 侧栏不可见恒成立（side-closed 只作移动抽屉语义；桌面折叠走 rail-collapsed，
   胶囊离屏另由 _rail_state 专项断言，#sideFab 在 rail-collapsed 时亮起为品牌锚点、peek 唤出时淡出避让、展开态退役）。

操作序列（每一步后都断言）：boot → 收起 → 展开 → 设置开/关 → 切视图（数据集浏览→回）
→ 缩窗过 780px 断点（移动抽屉规则）→ 拉回桌面 → 再收起/展开。

用法：先起服务（run_web.py 或 uvicorn，端口 7973），再
    .venv/Scripts/python.exe scripts/smoke_sidebar_states.py [base_url]
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".sidebar-shots"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7973"

#: 允许出现在 body 上的布局类全集（状态机字母表；出现表外的类 = 类名残留，立刻 FAIL）。
LEGAL_BODY_CLASSES = {
    "side-closed", "side-resizing", "on-query", "scope-in-side",
    "chat-main-on", "has-results",
    "view-swapped",   # 视图交换（#swSwapBtn）：对话窗 ↔ 结果网格换位
    "side-duo",       # 中栏工作卡可见（board.js swSync 维护）→ 导航轨收窄为纯图标
    "rail-collapsed", # 桌面导航轨折叠为悬浮胶囊（默认滑出左边界）
    "rail-peek",      # 胶囊态指针到左上角 → 胶囊滑入唤出
}   # rp2（2026-10-11）：facets-active 随「筛选」页签一并退役（面板改住右侧抽屉 #facetDrawer，不需要 body 类门）


def _body_classes(page) -> set[str]:
    return set(page.evaluate("() => Array.from(document.body.classList)"))


def _sidebar_state(page) -> dict:
    return page.evaluate(
        """() => {
            const el = document.querySelector('.sidebar');
            const cs = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return {
                inlineOpacity: el.style.opacity || "",
                inlineVisibility: el.style.visibility || "",
                closed: document.body.classList.contains('side-closed'),
                opacity: parseFloat(cs.opacity),
                right: r.right,          // 滑出屏外时 right <= 0
                mobile: window.innerWidth <= 780,
            };
        }"""
    )


def _rail_state(page) -> dict:
    """桌面导航轨三态探测：胶囊离屏/唤入、fab 品牌锚点显隐。"""
    return page.evaluate(
        """() => {
            const card = document.getElementById('sideNavCard');
            const cs = getComputedStyle(card);
            const r = card.getBoundingClientRect();
            const fabCs = getComputedStyle(document.getElementById('sideFab'));
            return {
                collapsed: document.body.classList.contains('rail-collapsed'),
                peek: document.body.classList.contains('rail-peek'),
                cardRight: r.right,
                cardVisible: cs.visibility === 'visible' && parseFloat(cs.opacity) > 0.95,
                fabDisplay: fabCs.display,
                fabOpacity: fabCs.opacity,
            };
        }"""
    )


def _collapse_via_button(page, check, assert_legal, step: str) -> None:
    """桌面 #sideCollapse → rail-collapsed：胶囊离屏 + #sideFab 亮起为品牌锚点。"""
    page.click("#sideCollapse")
    page.wait_for_timeout(700)    # 滑出过渡
    st = _rail_state(page)
    check(f"{step}：rail-collapsed 置位", st["collapsed"], str(st))
    check(f"{step}：胶囊离屏隐藏", st["cardRight"] <= 0 or not st["cardVisible"],
          f"cardRight={st['cardRight']} cardVisible={st['cardVisible']}")
    check(f"{step}：#sideFab 品牌锚点可见", st["fabDisplay"] != "none" and st["fabOpacity"] == "1",
          f"display={st['fabDisplay']} opacity={st['fabOpacity']}")
    assert_legal(page, step)


def _expand_via_edge(page, check, assert_legal, step: str) -> None:
    """指针到左上角 → rail-peek 胶囊滑入（fab 淡出避让）；指针挪开 → 自动滑出屏外（fab 复亮）；再唤出 → 点 wordmark（#railToggle）还原宽轨（fab 退役）。"""
    page.mouse.move(4, 80, steps=3)
    page.wait_for_timeout(600)
    st = _rail_state(page)
    check(f"{step}·唤出：rail-peek + 胶囊滑入可见", st["peek"] and st["cardVisible"], str(st))
    check(f"{step}·唤出：#sideFab 淡出避让", st["fabOpacity"] == "0", st["fabOpacity"])
    # 挪开即收：撤离检查时坐标哨兵不得命中唤出区（曾有 -1≤28∧-1≤320 恒真使 rail-peek 永不撤掉的 bug）
    page.mouse.move(900, 500, steps=6)
    page.wait_for_timeout(900)
    st = _rail_state(page)
    check(f"{step}·挪开：rail-peek 撤掉 + 胶囊滑出屏外",
          not st["peek"] and (st["cardRight"] <= 0 or not st["cardVisible"]), str(st))
    check(f"{step}·挪开：#sideFab 恢复可见", st["fabOpacity"] == "1", st["fabOpacity"])
    page.mouse.move(4, 80, steps=3)
    page.wait_for_timeout(600)
    st = _rail_state(page)
    check(f"{step}·再唤出：rail-peek 恢复", st["peek"] and st["cardVisible"], str(st))
    page.click("#railToggle")
    page.wait_for_timeout(700)
    st = _rail_state(page)
    check(f"{step}：rail-collapsed 解除", not st["collapsed"], str(st))
    check(f"{step}：#sideFab 展开态退役", st["fabDisplay"] == "none", st["fabDisplay"])
    assert_legal(page, step)


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    def assert_legal(page, step: str) -> None:
        classes = _body_classes(page)
        extra = classes - LEGAL_BODY_CLASSES
        check(f"{step}：body 类全部合法", not extra, f"残留类 {sorted(extra)}" if extra else "")
        st = _sidebar_state(page)
        check(f"{step}：.sidebar 无内联 opacity/visibility 残留",
              st["inlineOpacity"] == "" and st["inlineVisibility"] == "",
              f"opacity='{st['inlineOpacity']}' visibility='{st['inlineVisibility']}'")
        # side-closed ⇔ 侧栏不可见（淡出 or 滑出屏外）；展开 ⇔ 可见。恒真式，两种形态都算合法。
        if st["closed"]:
            check(f"{step}：收起态侧栏不可见", st["opacity"] < 0.05 or st["right"] <= 0,
                  f"opacity={st['opacity']} right={st['right']}")
        else:
            check(f"{step}：展开态侧栏可见", st["opacity"] > 0.95 and st["right"] > 0,
                  f"opacity={st['opacity']} right={st['right']}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1680, "height": 960})
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1200)   # playHero 时间线落幕（.sidebar tween 0.5s）
        assert_legal(page, "boot")
        page.screenshot(path=str(SHOTS / "01-boot.png"))

        # localStorage 的持久化偏好不污染状态机断言（上一轮折叠态会被 initSidebar 读回）
        page.evaluate("() => { localStorage.removeItem('biodata_sidebar_closed_v1');"
                      " localStorage.removeItem('biodata_rail_collapsed_v1'); }")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)
        assert_legal(page, "boot-clean")

        _collapse_via_button(page, check, assert_legal, "收起")
        page.screenshot(path=str(SHOTS / "02-collapsed.png"))

        _expand_via_edge(page, check, assert_legal, "展开")
        page.screenshot(path=str(SHOTS / "03-expanded.png"))

        page.click("#settingsBtn")
        page.wait_for_timeout(500)
        check("设置开：drawer 打开", page.evaluate(
            "() => document.getElementById('settings').classList.contains('open')"))
        page.screenshot(path=str(SHOTS / "04-settings.png"))
        page.click("#settingsClose")
        page.wait_for_timeout(400)
        assert_legal(page, "设置关")

        page.click(".side-nav .nav-item[data-view='browse']")
        page.wait_for_timeout(800)
        assert_legal(page, "数据集浏览")
        page.screenshot(path=str(SHOTS / "05-browse.png"))
        page.click(".side-nav .nav-item[data-view='query']")
        page.wait_for_timeout(800)
        assert_legal(page, "回智能查询")

        # 过断点：桌面展开 → 移动端。**收起不是 resize 的责任**（设计如此：resize 不动 side-closed，
        # 抽屉开着过断点＝移动抽屉开，是合法态，遮罩可点关）；「移动端默认收起」是 boot 契约
        # （initSidebar），所以缩窗后重新加载一次再断言。
        page.set_viewport_size({"width": 640, "height": 900})
        page.wait_for_timeout(600)
        assert_legal(page, "缩窗过断点（抽屉保持开＝合法移动抽屉开）")
        page.screenshot(path=str(SHOTS / "06-mobile-open.png"))
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(900)
        st = _sidebar_state(page)
        check("移动端 boot：抽屉默认收起", st["closed"] is True, f"closed={st['closed']}")
        assert_legal(page, "移动端 boot")
        page.screenshot(path=str(SHOTS / "07-mobile-boot.png"))
        page.set_viewport_size({"width": 1680, "height": 960})
        page.wait_for_timeout(800)
        # 移动抽屉的 side-closed 由 shell.js 的断点监听自动摘除（桌面已无 fab，留着就是死状态），
        # 并按桌面胶囊偏好恢复（本路径未折叠过 → 宽轨展开）。
        st = _sidebar_state(page)
        check("拉回桌面：side-closed 自动摘除", st["closed"] is False, f"closed={st['closed']}")
        assert_legal(page, "拉回桌面")

        _collapse_via_button(page, check, assert_legal, "再收起")
        page.screenshot(path=str(SHOTS / "08-collapsed.png"))
        _expand_via_edge(page, check, assert_legal, "再展开")
        page.screenshot(path=str(SHOTS / "09-final.png"))

        browser.close()

    print(f"\n{'全部通过' if not fails else '有失败'}：{len(fails)} 项失败" if fails else "\n全部通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
