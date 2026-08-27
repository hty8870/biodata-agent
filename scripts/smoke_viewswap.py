# -*- coding: utf-8 -*-
"""视图交换（+）Playwright 视觉/操作回归。

覆盖 #swSwapBtn 交换「主区结果网格 ↔ 侧栏对话窗」的状态：
  a) 交换前基准（有结果 + 有对话）；
  b) 交换后：主区 #chatStage 对话列 + 侧栏 grid-mini 紧凑结果卡（整体 + 局部特写）；
  c) 交换动画中间帧（连拍两帧，肉眼判断无跳变）；
  d) 交换态点开 #scopePop 范围弹层（向上弹、不被裁剪）；
  e) 交换态发送一条对话消息（输入条可聚焦可输入、发送键在场）；
  f) 交换态切「细化筛选」tab 再切回；
  g) 760px 宽度窗口（压过 780 全站断点，自动退化为现状布局）；
  h) 交换态收起侧栏（对话按既有 chat-in-main 规则回主区，内容不消失）；
  i) 换回正常态，确认与 a) 一致。
 增补（j 段，接在 b 后）：
  j1) 头部件随结果进侧栏：#resultsHead 在 #sideBoardScroll 顶部（卡片列表之前），
      主区不再有任何结果区头部件；
  j2) 主区纯聊天：.chat-stage-log 无背景/边框/阴影（白卡框已去），列宽 880px；
  j3) 紧凑卡恢复「数据集详情」小钮且可点（新标签 /dataset）；
  j4) 侧栏内「放宽方式 ▸」内联展开不被滚动容器裁剪；
  j5) 交换态点侧栏 #feasibilityBtn / #taskPackBtn：面板在主区对话列上方展开、可见、可关。
 增补（k 段）：
  k1) 交换态「检索结果」页签下 #swHits（常驻查询条件栏）不可见；
      「细化筛选」页签下恢复可见（并入 f 段断言）；正常态可见（并入 i 段断言）；
  k2) 输入条结构性钉底：960 / 1080 两种视口高度、20+ 条长对话（DOM 注入压力）下，
      #chatComposer 底边贴视口底（±6px）、对话列内部滚动、页面自身不滚动（无双滚动条）。
 增补（l 段，头部件紧凑化）：
  l1) 交换态头部区（标题行+摘要卡+放宽提示）总高 ≤150px：标题行并一行（≤32px）、
      两按钮显短文案（.bt-short 可见 / .bt-full 隐藏）；
  l2) 放宽提示折叠单行（cov-txt ≤22px）、「放宽方式」钮折叠态可点——点击展开：
      detail 可见 + cov-txt 解除截断变多行（完整来源分计数回流），再点收回单行；
      （本查询的提示行含 6 来源 189 条，兼作「长来源清单」态截图）；
  l3) 正常态防误伤（并入 i 段）：还原后 .bt-full 可见 / .bt-short 隐藏、rs-text 无截断盒；
  l4) 同内容往返测高：换「10x Visium 数据」（无放宽提示态）——正常态高 h1 → 交换态
      紧凑高 h2 ≤150 且 < h1 → 还原后 h3 与 h1 一致（±2px，像素级不变）。
全程收集 console 报错（pageerror + console error），零新增才 PASS。

用法：先起服务（BIODATA_SKIP_RECALL_WARM=1 PORT=7973 run_web.py），再
    .venv/Scripts/python.exe scripts/smoke_viewswap.py [base_url]
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".fix-shots" / "viewswap4"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7973"

QUERY = "推荐有 FASTQ 的人类乳腺癌数据"


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    fails: list[str] = []
    js_errors: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1680, "height": 960})
        page.on("pageerror", lambda e: js_errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: js_errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        # 新手导览（biodata_onboarding_v1 未标记 done 时首次检索后自动弹出）会遮挡交互、
        # 偷走键盘事件——本脚本验视图交换，开跑前直接标记已导览。
        page.goto(BASE, wait_until="networkidle")
        page.evaluate("() => { localStorage.setItem('biodata_onboarding_v1', 'done');"
                      " localStorage.removeItem('biodata_sidebar_closed_v1'); }")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)   # playHero 时间线落幕
        # 验证环境不等本地语义模型（2GB 惰性加载会拖死首查；BIODATA_SKIP_RECALL_WARM 只跳预热、
        # 不拦请求内加载）。getConfig 现读 DOM checkbox——直接关掉「本地精准重排 / 智能策略」，
        # 本脚本验的是视图交换、不是排序质量。
        page.evaluate(
            "() => { const r = document.getElementById('cfgRecall'); if (r) r.checked = false;"
            " const s = document.getElementById('cfgStrategy'); if (s) s.checked = false; }")

        def wait_idle(timeout=30000) -> None:
            """等上一次检索/路由落地（提交键解除禁用）——在途闸不开时发送会被拒，消息留在框里。"""
            page.wait_for_function(
                "() => { const b = document.getElementById('submitBtn'); return b && !b.disabled; }",
                timeout=timeout)

        # ---- 造态：一次真实检索（有结果） ----
        page.fill("#queryInput", QUERY)
        page.click("#submitBtn")
        page.wait_for_selector("#resultsGrid .card", timeout=30000)
        wait_idle()
        page.wait_for_timeout(800)    # revealCards stagger + swIn 落幕
        check("造态：结果卡出现", page.locator("#resultsGrid .card").count() > 0)

        # ---- 造态：侧栏发一句对话（有对话） ----
        page.click("#chatInput")
        page.fill("#chatInput", "只要 2020 年以后的")
        page.press("#chatInput", "Enter")
        wait_idle()                   # 路由 + 检索落地（fail-open 也有 sys 回音）
        page.wait_for_timeout(600)
        log_len = page.evaluate("() => document.getElementById('cbHistory').children.length")
        check("造态：对话记录有内容", log_len >= 2, f"turns={log_len}")
        page.screenshot(path=str(SHOTS / "a-交换前基准.png"))

        # ---- b/c) 点交换钮：中间帧连拍 + 落位断言 ----
        page.click("#swSwapBtn")
        page.wait_for_timeout(90)    # GSAP from 0.3s 的前段
        page.screenshot(path=str(SHOTS / "c1-交换动画-早帧.png"))
        page.wait_for_timeout(120)
        page.screenshot(path=str(SHOTS / "c2-交换动画-中帧.png"))
        page.wait_for_timeout(600)   # 动画落幕
        state = page.evaluate(
            """() => ({
                swapped: document.body.classList.contains('view-swapped'),
                logInStage: document.getElementById('cbHistory').parentElement.id,
                composerIn: document.getElementById('chatComposer').parentElement.id,
                gridIn: document.getElementById('resultsGrid').parentElement.id,
                gridMini: document.getElementById('resultsGrid').classList.contains('grid-mini'),
                stageHidden: document.getElementById('chatStage').hidden,
                tabText: document.querySelector('#swTabBoard span').textContent,
                pressed: document.getElementById('swSwapBtn').getAttribute('aria-pressed'),
                ariaLabel: document.getElementById('sideBoardScroll').getAttribute('aria-label'),
                focus: document.activeElement && document.activeElement.id,
            })"""
        )
        check("交换：body.view-swapped", state["swapped"])
        check("交换：#cbHistory 进 #chatStageLog", state["logInStage"] == "chatStageLog", state["logInStage"])
        check("交换：#chatComposer 进 #chatStageBar", state["composerIn"] == "chatStageBar", state["composerIn"])
        check("交换：#resultsGrid 进 #sideBoardScroll + grid-mini",
              state["gridIn"] == "sideBoardScroll" and state["gridMini"], state["gridIn"])
        check("交换：#chatStage 可见", state["stageHidden"] is False)
        check("交换：页签文案「检索结果」", state["tabText"] == "检索结果", state["tabText"])
        check("交换：aria-pressed=true / aria-label 同步",
              state["pressed"] == "true" and state["ariaLabel"] == "检索结果（紧凑视图）")
        check("交换：焦点落 #chatInput（或分支键）",
              state["focus"] in ("chatInput", "cbBranchBtn"), state["focus"])
        page.screenshot(path=str(SHOTS / "b1-交换后-整体.png"))
        # 局部特写：主区对话列 / 侧栏紧凑卡（首张卡）
        page.locator("#chatStage").screenshot(path=str(SHOTS / "b2-交换后-主区对话列.png"))
        page.locator("#resultsGrid .card").first.screenshot(path=str(SHOTS / "b3-交换后-侧栏紧凑卡.png"))

        # ---- j1) ：头部件随结果进侧栏（#sideBoardScroll 顶部、卡片列表之前），主区无头部件 ----
        j1 = page.evaluate(
            """() => {
                const scroll = document.getElementById('sideBoardScroll');
                const head = document.getElementById('resultsHead');
                const kids = [...scroll.children];
                return {
                    headIn: head.parentElement.id,
                    headIdx: kids.indexOf(head),
                    gridIdx: kids.indexOf(document.getElementById('resultsGrid')),
                    titleIn: document.querySelector('.results-head').closest('#sideBoardScroll') ? 'side' : 'main',
                    summaryIn: document.getElementById('searchTrace').closest('#sideBoardScroll') ? 'side' : 'main',
                    wrapHasHeadBits: !!document.querySelector('#resultsWrap .results-head, #resultsWrap #searchTrace, #resultsWrap #unusedQueryTerms'),
                };
            }"""
        )
        check("vs2：#resultsHead 进侧栏且在卡片列表之前",
              j1["headIn"] == "sideBoardScroll" and 0 <= j1["headIdx"] < j1["gridIdx"], str(j1))
        check("vs2：标题行/摘要卡都在侧栏、主区无任何头部件",
              j1["titleIn"] == "side" and j1["summaryIn"] == "side" and not j1["wrapHasHeadBits"], str(j1))
        page.locator("#sideBoardScroll").screenshot(path=str(SHOTS / "j1-交换态-侧栏头部件.png"))

        # ---- j2) ：主区纯聊天——白卡框已去（无背景/边框/阴影），列宽 880px ----
        j2 = page.evaluate(
            """() => {
                const cs = getComputedStyle(document.getElementById('chatStageLog'));
                return { bg: cs.backgroundColor, shadow: cs.boxShadow, border: cs.borderTopWidth, maxW: cs.maxWidth };
            }"""
        )
        check("vs2：对话舞台无白卡框（透明背景/零边框/零阴影）",
              j2["bg"] in ("rgba(0, 0, 0, 0)", "transparent") and j2["shadow"] == "none" and j2["border"] == "0px", str(j2))
        check("vs2：对话列宽放宽到 880px", j2["maxW"] == "880px", j2["maxW"])

        # ---- j3) ：紧凑卡恢复「数据集详情」小钮且可点（新标签 /dataset） ----
        detail_btn = page.locator("#resultsGrid .card .btn-detail").first
        check("vs2：紧凑卡有可见的「数据集详情」钮", detail_btn.is_visible())
        with page.context.expect_page() as pop:
            detail_btn.click()
        newtab = pop.value
        newtab.wait_for_load_state("domcontentloaded")
        check("vs2：详情钮点击开新标签 /dataset", "/dataset" in newtab.url, newtab.url)
        newtab.close()

        # ---- j4) ：侧栏内「放宽方式 ▸」内联展开不被滚动容器裁剪 ----
        cov_btn = page.locator("#sideBoardScroll .cov-expand").first
        if cov_btn.count() and cov_btn.is_visible():
            cov_btn.click()
            page.wait_for_timeout(350)
            j4 = page.evaluate(
                """() => {
                    const d = document.querySelector('#sideBoardScroll .cov-detail');
                    if (!d) return { ok: false, why: 'no .cov-detail' };
                    const r = d.getBoundingClientRect();
                    const s = document.getElementById('sideBoardScroll').getBoundingClientRect();
                    return { ok: r.height > 0 && r.left >= s.left - 1 && r.right <= s.right + 1
                              && r.top >= s.top - 1 && r.top <= s.bottom,
                             dr: [r.left, r.right, r.top, r.height], sr: [s.left, s.right] };
                }"""
            )
            check("vs2：放宽方式内联展开、不被侧栏滚动容器裁剪", j4["ok"], str(j4))
            page.screenshot(path=str(SHOTS / "j4-交换态-侧栏放宽方式展开.png"))
            cov_btn.click()   # 收起，回干净态
            page.wait_for_timeout(250)
        else:
            check("vs2：放宽方式内联展开不被裁剪", True, "本结果无覆盖缺口条——跳过（合法）")

        # ---- j5) ：交换态点侧栏两按钮，面板在主区对话列上方展开/可关 ----
        for btn_id, panel_id, shot in (("feasibilityBtn", "feasibilityPanel", "j5a-交换态-可行性面板.png"),
                                       ("taskPackBtn", "taskPackPanel", "j5b-交换态-任务包面板.png")):
            if not page.locator("#" + btn_id).is_visible():
                check(f"vs2：#{btn_id} 可见", False, "按钮在侧栏头部件里应可见")
                continue
            page.click("#" + btn_id)
            try:
                page.wait_for_selector(f"#{panel_id}:not([hidden])", timeout=12000)
                if panel_id == "taskPackPanel":
                    # 预览是异步 fetch，完成后会重绘面板——不等它落地就点关闭会被迟到的响应重开
                    # （首轮回归真踩到：点关 → fetch 落地 → renderTaskPackPlan 又 hidden=false）。
                    page.wait_for_selector("#taskPackPanel .tp-loading", state="detached", timeout=15000)
            except Exception:
                pass
            pj = page.evaluate(
                f"""() => {{
                    const p = document.getElementById('{panel_id}');
                    const stage = document.getElementById('chatStageLog').getBoundingClientRect();
                    if (p.hidden) return {{ ok: false, why: 'panel still hidden' }};
                    const r = p.getBoundingClientRect();
                    return {{ ok: r.width > 400 && r.bottom <= stage.top + 2 && r.height > 40,
                              home: p.parentElement.id, w: r.width, bottom: r.bottom, stageTop: stage.top }};
                }}"""
            )
            check(f"vs2：#{panel_id} 在主区对话列上方展开且为主区宽度", pj["ok"], str(pj))
            page.screenshot(path=str(SHOTS / shot))
            page.click("#" + btn_id)   # 再点关闭，回干净态
            page.wait_for_timeout(400)
            closed = page.evaluate(f"() => document.getElementById('{panel_id}').hidden")
            check(f"vs2：#{panel_id} 可关闭", closed)

        # ---- k1) ：交换态「检索结果」页签下 #swHits（常驻查询条件栏）不可见；节点仍在 ----
        k1 = page.evaluate(
            """() => {
                const h = document.getElementById('swHits');
                return { hidden: h.getBoundingClientRect().height === 0,
                         hasKids: h.children.length > 0,
                         mode: document.getElementById('sideWork').dataset.swMode,
                         inWork: h.parentElement.id === 'sideWork' };
            }"""
        )
        check("vs3：检索结果页签下 #swHits 不可见（节点仍在、有内容）",
              k1["hidden"] and k1["hasKids"] and k1["mode"] == "board" and k1["inWork"], str(k1))
        page.screenshot(path=str(SHOTS / "k1-交换态-结果页签无查询条件栏.png"))

        # ---- l1) ：头部件紧凑化——头部区总高 ≤150px、标题行单行、短文案钮 ----
        def head_span():
            """#resultsHead 是 display:contents 无自身盒，量可见子件的包围跨高。"""
            return page.evaluate(
                """() => {
                    const kids = [...document.getElementById('resultsHead').children]
                        .filter((el) => !el.hidden && el.getBoundingClientRect().height > 0);
                    if (!kids.length) return { height: 0 };
                    const top = Math.min(...kids.map((el) => el.getBoundingClientRect().top));
                    const bottom = Math.max(...kids.map((el) => el.getBoundingClientRect().bottom));
                    return { height: Math.round(bottom - top) };
                }"""
            )

        l1 = page.evaluate(
            """() => {
                const rh = document.querySelector('#sideBoardScroll .results-head').getBoundingClientRect();
                const fb = document.getElementById('feasibilityBtn');
                const shortVis = (b) => getComputedStyle(b.querySelector('.bt-short')).display !== 'none';
                const fullVis = (b) => getComputedStyle(b.querySelector('.bt-full')).display !== 'none';
                return { rowH: Math.round(rh.height),
                         shortOn: shortVis(fb) && shortVis(document.getElementById('taskPackBtn')),
                         fullOff: !fullVis(fb) && !fullVis(document.getElementById('taskPackBtn')) };
            }"""
        )
        l1h = head_span()["height"]
        check("vs4：头部区总高 ≤150px（vs3 约 350px）", l1h <= 150, f"total={l1h}px")
        check("vs4：标题行并一行（≤32px）", l1["rowH"] <= 32, f"rowH={l1['rowH']}px")
        check("vs4：两按钮显短文案（bt-short 可见 / bt-full 隐藏）", l1["shortOn"] and l1["fullOff"], str(l1))
        page.screenshot(path=str(SHOTS / "l1-头部紧凑-折叠态.png"))

        # ---- l2) ：放宽提示折叠单行、「放宽方式」钮折叠态可点、点击展开回流完整来源计数 ----
        cov2 = page.locator("#sideBoardScroll .cov-expand").first
        if cov2.count() and cov2.is_visible():
            c0 = page.evaluate(
                """() => {
                    const t = document.querySelector('#sideBoardScroll .cov-txt').getBoundingClientRect();
                    const b = document.querySelector('#sideBoardScroll .cov-expand').getBoundingClientRect();
                    return { txtH: Math.round(t.height), btnH: Math.round(b.height) };
                }"""
            )
            check("vs4：放宽提示折叠成单行（cov-txt ≤22px），放宽钮留在折叠行上",
                  c0["txtH"] <= 22 and c0["btnH"] > 0, str(c0))
            cov2.click()
            page.wait_for_timeout(450)   # max-height 过渡 .25s
            c1 = page.evaluate(
                """() => {
                    const t = document.querySelector('#sideBoardScroll .cov-txt').getBoundingClientRect();
                    const d = document.querySelector('#sideBoardScroll .cov-detail');
                    return { txtH: Math.round(t.height), detailOpen: d && !d.hidden };
                }"""
            )
            check("vs4：点击展开——detail 可见 + cov-txt 解除截断变多行（完整来源计数回流）",
                  c1["detailOpen"] and c1["txtH"] > c0["txtH"] + 20, f"{c0['txtH']}px → {c1['txtH']}px")
            page.screenshot(path=str(SHOTS / "l2-放宽提示展开-长来源清单.png"))
            cov2.click()   # 收起，回折叠干净态
            page.wait_for_timeout(400)
            c2 = page.evaluate(
                "() => Math.round(document.querySelector('#sideBoardScroll .cov-txt').getBoundingClientRect().height)")
            check("vs4：再点收回单行", c2 <= 22, f"txtH={c2}px")
        else:
            check("vs4：放宽提示折叠/展开", False, "本结果无覆盖缺口行——vs4 段需要带 caveats 的查询")

        # ---- d) 范围弹层：向上弹、不被裁剪 ----
        page.click("#scopeChip")
        page.wait_for_timeout(400)
        pop_ok = page.evaluate(
            """() => {
                const pop = document.getElementById('scopePop');
                if (pop.hidden) return { ok: false, why: 'hidden' };
                const r = pop.getBoundingClientRect();
                const bar = document.getElementById('chatStageBar').getBoundingClientRect();
                return { ok: r.bottom <= bar.top + 2 && r.top >= 0, top: r.top, bottom: r.bottom, barTop: bar.top };
            }"""
        )
        check("交换态：#scopePop 向上弹且不越顶", pop_ok["ok"], str(pop_ok))
        page.screenshot(path=str(SHOTS / "d-交换态-范围弹层.png"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)

        # ---- e) 交换态发一条对话 ----
        wait_idle()
        page.click("#chatInput")
        page.type("#chatInput", "只要人类的")
        page.screenshot(path=str(SHOTS / "e1-交换态-输入中.png"))
        send_visible = page.locator("#chatSendBtn").is_visible()
        page.press("#chatInput", "Enter")
        wait_idle()
        page.wait_for_timeout(500)
        last_turn = page.evaluate(
            "() => { const h = document.getElementById('cbHistory'); return h.children.length ? h.lastElementChild.textContent.slice(0, 60) : ''; }")
        check("交换态：输入条可输入、发送键在场、发送后有回应",
              send_visible and "只要人类的" in page.locator("#cbHistory").inner_text() and bool(last_turn),
              f"last={last_turn!r}")
        page.screenshot(path=str(SHOTS / "e2-交换态-发送后.png"))

        # ---- f) 切「细化筛选」tab 再切回 ----
        facets_on = page.evaluate("() => !document.getElementById('swTabFacets').disabled")
        if facets_on:
            page.click("#swTabFacets")
            page.wait_for_timeout(450)
            f_state = page.evaluate(
                "() => ({ pane: !document.getElementById('sideFacets').hidden, gridGone: !document.getElementById('sideBoardPane').hidden })")
            check("交换态切「细化筛选」：分面板出现、结果面板让位", f_state["pane"] and not f_state["gridGone"], str(f_state))
            fhits = page.evaluate(
                "() => document.getElementById('swHits').getBoundingClientRect().height > 0"
                " && document.getElementById('sideWork').dataset.swMode === 'facets'")
            check("vs3：切「细化筛选」页签 #swHits 恢复可见", fhits)
            page.screenshot(path=str(SHOTS / "f1-交换态-细化筛选tab.png"))
            page.click("#swTabBoard")
            page.wait_for_timeout(450)
            back = page.evaluate(
                "() => !document.getElementById('sideBoardPane').hidden && document.getElementById('resultsGrid').parentElement.id === 'sideBoardScroll'")
            check("切回「检索结果」：紧凑卡仍在侧栏", back)
            page.screenshot(path=str(SHOTS / "f2-交换态-切回结果tab.png"))
        else:
            check("交换态切「细化筛选」tab 再切回", True, "本分面无内容、tab 禁用——跳过（合法）")

        # ---- g) 760px 宽窗（压过 780 全站断点）：自动退化为现状布局 ----
        page.set_viewport_size({"width": 760, "height": 900})
        page.wait_for_timeout(700)
        g_state = page.evaluate(
            """() => ({
                swapped: document.body.classList.contains('view-swapped'),
                gridIn: document.getElementById('resultsGrid').parentElement.id,
                stageHidden: document.getElementById('chatStage').hidden,
                gridVisible: document.getElementById('resultsGrid').getBoundingClientRect().width > 0,
            })"""
        )
        check("760px（过断点）：退出交换、网格回主区可见", (not g_state["swapped"]) and g_state["gridIn"] == "resultsWrap"
              and g_state["stageHidden"] and g_state["gridVisible"], str(g_state))
        page.screenshot(path=str(SHOTS / "g-760px过断点退化.png"))
        page.set_viewport_size({"width": 1680, "height": 960})
        page.wait_for_timeout(700)
        re = page.evaluate(
            "() => ({ swapped: document.body.classList.contains('view-swapped'), gridIn: document.getElementById('resultsGrid').parentElement.id })")
        check("拉回桌面：交换自动恢复", re["swapped"] and re["gridIn"] == "sideBoardScroll", str(re))

        # ---- h) 交换态收起侧栏：按既有规则落位、内容不消失 ----
        page.click("#sideCollapse")
        page.wait_for_timeout(800)
        h_state = page.evaluate(
            """() => ({
                swapped: document.body.classList.contains('view-swapped'),
                logHome: document.getElementById('cbHistory').parentElement.id,
                logVisible: document.getElementById('cbHistory').getBoundingClientRect().height > 0,
                gridIn: document.getElementById('resultsGrid').parentElement.id,
                gridVisible: document.getElementById('resultsGrid').getBoundingClientRect().width > 0,
            })"""
        )
        check("收起侧栏：退出交换、网格回主区可见", (not h_state["swapped"]) and h_state["gridIn"] == "resultsWrap"
              and h_state["gridVisible"], str(h_state))
        check("收起侧栏：对话记录仍可见（chatMain 回退位）", h_state["logVisible"], h_state["logHome"])
        page.screenshot(path=str(SHOTS / "h-交换态收起侧栏.png"))
        page.click("#sideFab")
        page.wait_for_timeout(800)
        h2 = page.evaluate("() => document.body.classList.contains('view-swapped')")
        check("展开侧栏：交换自动恢复", h2)

        # ---- k2) ：输入条结构性钉底——960/1080 视口 + 20+ 条长对话（DOM 注入压力） ----
        def pin_probe(tag: str) -> None:
            st = page.evaluate(
                """() => {
                    const bar = document.getElementById('chatComposer').getBoundingClientRect();
                    const log = document.getElementById('cbHistory');
                    const de = document.documentElement;
                    return { barBottom: bar.bottom, vh: innerHeight,
                             pageScroll: de.scrollHeight - innerHeight,
                             logScrollable: log.scrollHeight > log.clientHeight + 4,
                             logTop: log.getBoundingClientRect().top };
                }"""
            )
            check(f"vs3 钉底（{tag}）：composer 底边贴视口底 ±6px",
                  abs(st["barBottom"] - (st["vh"] - 20)) <= 6, str(st))
            check(f"vs3 钉底（{tag}）：页面自身不滚动（无双滚动条）",
                  st["pageScroll"] <= 1, f"scrollHeight-vh={st['pageScroll']}")
            return st

        pin_probe("960px 视口/短对话")
        page.screenshot(path=str(SHOTS / "k2a-钉底-960短对话.png"))
        # 长对话压力：注入 24 条气泡（纯 DOM 压力，验布局/滚动；不落 _cbLog、不触发重渲）
        page.evaluate(
            """() => {
                const h = document.getElementById('cbHistory');
                for (let i = 0; i < 24; i++) {
                    const row = document.createElement('div');
                    row.className = i % 2 ? 'cbh-row cbh-sys-row' : 'cbh-row';
                    row.innerHTML = `<div class="${i % 2 ? 'cbh-sys-bubble' : 'cbh-bubble'}">压测气泡 ${i + 1}：视图交换长对话钉底回归</div>`;
                    h.appendChild(row);
                }
                h.scrollTop = h.scrollHeight;
            }"""
        )
        page.wait_for_timeout(400)
        st = pin_probe("960px 视口/24+ 条长对话")
        check("vs3 钉底（长对话）：对话列内部可滚动", st["logScrollable"], str(st))
        page.screenshot(path=str(SHOTS / "k2b-钉底-960长对话.png"))
        page.set_viewport_size({"width": 1680, "height": 1080})
        page.wait_for_timeout(500)
        st = pin_probe("1080px 视口/长对话")
        check("vs3 钉底（1080 长对话）：对话列内部可滚动", st["logScrollable"], str(st))
        page.screenshot(path=str(SHOTS / "k2c-钉底-1080长对话.png"))
        page.set_viewport_size({"width": 1680, "height": 960})
        page.wait_for_timeout(500)

        # ---- i) 换回正常态：与基准一致 ----
        page.click("#swSwapBtn")
        page.wait_for_timeout(700)
        n_state = page.evaluate(
            """() => ({
                swapped: document.body.classList.contains('view-swapped'),
                logIn: document.getElementById('cbHistory').parentElement.id,
                composerIn: document.getElementById('chatComposer').parentElement.id,
                gridIn: document.getElementById('resultsGrid').parentElement.id,
                gridMini: document.getElementById('resultsGrid').classList.contains('grid-mini'),
                stageHidden: document.getElementById('chatStage').hidden,
                tabText: document.querySelector('#swTabBoard span').textContent,
                ariaLabel: document.getElementById('sideBoardScroll').getAttribute('aria-label'),
                headIn: document.getElementById('resultsHead').parentElement.id,
                headFirst: document.getElementById('resultsWrap').firstElementChild.id,
                hitsVisible: document.getElementById('swHits').getBoundingClientRect().height > 0,
            })"""
        )
        check("还原：view-swapped 摘除、grid-mini 摘除、舞台 hidden",
              (not n_state["swapped"]) and (not n_state["gridMini"]) and n_state["stageHidden"])
        check("还原：对话回侧栏、输入条回工作卡、网格回结果区",
              n_state["logIn"] == "sideBoardScroll" and n_state["composerIn"] == "sideWork"
              and n_state["gridIn"] == "resultsWrap", str(n_state))
        check("还原：vs2 头部件回 #resultsWrap 首子（静态原位）",
              n_state["headIn"] == "resultsWrap" and n_state["headFirst"] == "resultsHead", str(n_state))
        check("还原：vs3 正常态 #swHits 恢复可见（两页签现状不动）", n_state["hitsVisible"])
        check("还原：页签/aria 文案复原",
              n_state["tabText"] == "继续对话" and n_state["ariaLabel"] == "对话与细化记录（可上下滚动）")
        # l3) 正常态防误伤：还原后按钮显完整文案、摘要无截断盒（紧凑规则只在交换态命中）
        n_vs4 = page.evaluate(
            """() => {
                const fb = document.getElementById('feasibilityBtn');
                const rs = document.getElementById('resultSummaryText');
                return { fullOn: getComputedStyle(fb.querySelector('.bt-full')).display !== 'none',
                         shortOff: getComputedStyle(fb.querySelector('.bt-short')).display === 'none',
                         rsDisplay: getComputedStyle(rs).display };
            }"""
        )
        check("vs4：正常态按钮显完整文案（bt-full 可见 / bt-short 隐藏）",
              n_vs4["fullOn"] and n_vs4["shortOff"], str(n_vs4))
        check("vs4：正常态摘要无截断盒（display 非 -webkit-box）",
              n_vs4["rsDisplay"] != "-webkit-box", n_vs4["rsDisplay"])
        page.screenshot(path=str(SHOTS / "i-换回正常态.png"))

        # ---- l4) 同内容往返测高 + 无放宽提示态：「10x Visium 数据」（API 探测零 caveats）。
        #     另开干净页：结果态主检索框隐藏、对话又带上下文，全新查询只在 hero 态可做。 ----
        page2 = browser.new_page(viewport={"width": 1680, "height": 960})
        page2.on("pageerror", lambda e: js_errors.append(f"pageerror: {e}"))
        page2.on("console", lambda m: js_errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page2.goto(BASE, wait_until="networkidle")
        page2.evaluate("() => { localStorage.setItem('biodata_onboarding_v1', 'done');"
                       " localStorage.removeItem('biodata_sidebar_closed_v1'); }")
        page2.reload(wait_until="networkidle")
        page2.wait_for_timeout(1200)
        page2.evaluate(
            "() => { const r = document.getElementById('cfgRecall'); if (r) r.checked = false;"
            " const s = document.getElementById('cfgStrategy'); if (s) s.checked = false; }")
        page2.fill("#queryInput", "10x Visium 数据")
        page2.click("#submitBtn")
        page2.wait_for_selector("#resultsGrid .card", timeout=30000)
        page2.wait_for_function(
            "() => { const b = document.getElementById('submitBtn'); return b && !b.disabled; }", timeout=30000)
        page2.wait_for_timeout(800)
        no_cov = page2.evaluate(
            "() => document.getElementById('coverageCaveats').hidden"
            " || document.getElementById('coverageCaveats').children.length === 0")
        check("vs4：无放宽提示态造态成功（coverageCaveats 为空）", no_cov)
        h1 = page2.evaluate(
            """() => {
                const kids = [...document.getElementById('resultsHead').children]
                    .filter((el) => !el.hidden && el.getBoundingClientRect().height > 0);
                const top = Math.min(...kids.map((el) => el.getBoundingClientRect().top));
                const bottom = Math.max(...kids.map((el) => el.getBoundingClientRect().bottom));
                return Math.round(bottom - top);
            }""")
        page2.click("#swSwapBtn")
        page2.wait_for_timeout(800)
        h2 = page2.evaluate(
            """() => {
                const kids = [...document.getElementById('resultsHead').children]
                    .filter((el) => !el.hidden && el.getBoundingClientRect().height > 0);
                const top = Math.min(...kids.map((el) => el.getBoundingClientRect().top));
                const bottom = Math.max(...kids.map((el) => el.getBoundingClientRect().bottom));
                return Math.round(bottom - top);
            }""")
        check("vs4：无放宽提示态交换后头部区 ≤150px 且小于正常态",
              h2 <= 150 and h2 < h1, f"正常态 {h1}px → 交换态 {h2}px")
        page2.locator("#sideBoardScroll").screenshot(path=str(SHOTS / "l4-无放宽提示-交换态.png"))
        page2.click("#swSwapBtn")
        page2.wait_for_timeout(800)
        h3 = page2.evaluate(
            """() => {
                const kids = [...document.getElementById('resultsHead').children]
                    .filter((el) => !el.hidden && el.getBoundingClientRect().height > 0);
                const top = Math.min(...kids.map((el) => el.getBoundingClientRect().top));
                const bottom = Math.max(...kids.map((el) => el.getBoundingClientRect().bottom));
                return Math.round(bottom - top);
            }""")
        check("vs4：还原后正常态头部区高度与交换前一致（±2px，像素级不变）",
              abs(h3 - h1) <= 2, f"{h1}px → {h3}px")
        page2.close()

        browser.close()

    if js_errors:
        print("\n浏览器 JS 报错：")
        for e in js_errors:
            print("  " + e)
        fails.append("console 零报错")
    else:
        print("PASS 浏览器 console 零报错")

    print(f"\n{'全部通过' if not fails else '有失败'}：{len(fails)} 项失败" if fails else "\n全部通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
