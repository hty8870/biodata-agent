"""F1 追踪 UI 集成端到端冒烟（默认本地测试服 7988，可由 BIODATA_E2E_BASE 覆盖；系统 Edge）。
验证：追踪/收藏 × 主输入框/侧栏输入框四种上下文 chip、发送即清、候选徽标与顶部导出区。
任一关键断言失败 → 非零退出并附截图。"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BIODATA_E2E_BASE", "http://127.0.0.1:7988")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

fails = []
def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        fails.append(name)
        print(f"  FAIL {name}{'  —— ' + detail if detail else ''}")

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    console_errors = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(str(e)))

    page.goto(BASE, wait_until="load")
    page.wait_for_timeout(1500)
    # 首次进入的 12 步教程浮层会拦截后续弹窗点击——先跳过
    try:
        if page.locator("#onboarding").is_visible():
            page.click("#onboardingSkip")
            page.wait_for_timeout(400)
            print("  .. onboarding 已跳过")
    except Exception:
        pass
    check("页面标题", "BioData Agent" in page.title())
    check("导航「我的库」在场", page.locator("#libNav").count() == 1)
    check("「存为追踪」按钮在场（默认隐藏）", page.locator("#saveProjectBtn").count() == 1 and page.locator("#saveProjectBtn").is_hidden())

    # 检索 → 落地 → 按钮露出
    page.fill("#queryInput", "人类乳腺癌数据")
    page.click("#submitBtn")
    # 首次发送可能弹 consent（默认采集开）：点「同意并继续」（弹窗出现可能略晚，放宽等待）
    try:
        page.wait_for_selector("#consentModal:not([hidden])", timeout=12000)
        page.click("#consentAgreeBtn")
        print("  .. consent 已同意")
    except Exception:
        pass
    page.wait_for_selector("#resultsGrid .card", timeout=20000)
    page.wait_for_timeout(800)
    check("「存为追踪」按钮露出", page.locator("#saveProjectBtn").is_visible())
    # 先收藏第一条，为后半段「收藏 × 主框/侧栏」两条路径准备同源数据。
    fav_btn = page.locator("#resultsGrid .card .fav").first
    fav_btn.click()
    page.wait_for_selector(".fav-popover", timeout=3000)
    page.click(".fav-popover .fav-pop-default")
    page.wait_for_timeout(300)
    check("收藏入口准备完成", "active" in (fav_btn.get_attribute("class") or ""))
    page.screenshot(path="/tmp/f1_results.png")

    # 存为追踪
    page.click("#saveProjectBtn")
    page.wait_for_selector("#libWin:not([hidden])", timeout=10000)
    page.wait_for_timeout(1200)  # 基线 + 渲染
    check("我的库浮窗打开（追踪页签）", page.locator("#libWin").is_visible())
    check("浮窗进入详情（有返回列表钮）", page.locator("#artifactsWinBody [data-prj-back]").count() == 1)
    check("详情有「在对话中使用」", page.locator("#artifactsWinBody [data-prj-use]").count() == 1)
    check("详情候选行渲染", page.locator("#artifactsWinBody .prj-cand-wrap").count() >= 1)
    badge_inside = page.evaluate("""() => {
        const wrap = document.querySelector('#artifactsWinBody .prj-cand-wrap');
        const badge = wrap && wrap.querySelector('.prj-cand-st');
        const card = wrap && wrap.querySelector('.card');
        if (!badge || !card) return false;
        const b = badge.getBoundingClientRect(), c = card.getBoundingClientRect();
        return b.left >= c.left - 1 && b.right <= c.right + 1 && b.top >= c.top - 1 && b.bottom <= c.bottom + 1;
    }""")
    check("待核验徽标收在卡片边界内", badge_inside)
    export_on_top = page.evaluate("""() => {
        const exp = document.querySelector('#artifactsWinBody [data-p5-mount-section]');
        const checkMount = document.querySelector('#artifactsWinBody [data-p4-mount-check]');
        const checkSec = checkMount && checkMount.closest('.prj-sec');
        return !!exp && !exp.hidden && (!checkSec || exp.getBoundingClientRect().top < checkSec.getBoundingClientRect().top);
    }""")
    check("导出区位于追踪详情顶部", export_on_top)
    page.screenshot(path="/tmp/f1_detail.png")

    # 回到列表视图（点返回）→ 列表卡片
    page.click("#artifactsWinBody [data-prj-back]")
    page.wait_for_timeout(600)
    check("列表视图追踪卡片", page.locator("#artifactsWinBody .prj-card").count() >= 1)
    page.screenshot(path="/tmp/f1_list.png")

    # 再进详情 → 在对话中使用 → 上下文 chip（：侧栏窄框态 = 小圆徽章 + hover/点击 popover）
    page.click("#artifactsWinBody .prj-card")
    page.wait_for_timeout(600)
    page.click("#artifactsWinBody [data-prj-use]")
    page.wait_for_timeout(800)
    check("上下文 chip 出现（侧栏小圆徽章）", page.locator("#artifactCtx .ctx-dot").is_visible())
    side_midline_delta = page.evaluate("""() => {
        const dot = document.querySelector('#artifactCtx .ctx-dot');
        const input = document.getElementById('chatInput');
        const d = dot.getBoundingClientRect(), r = input.getBoundingClientRect(), cs = getComputedStyle(input);
        const firstLineCenter = r.top + parseFloat(cs.paddingTop) + parseFloat(cs.lineHeight) / 2;
        return Math.abs((d.top + d.height / 2) - firstLineCenter);
    }""")
    check("侧栏小圆徽章与第一行文本中线对齐", side_midline_delta <= 2, str(side_midline_delta))
    page.hover("#artifactCtx .ctx-dot")
    page.wait_for_timeout(300)
    pop_visible = page.evaluate("() => getComputedStyle(document.querySelector('#artifactCtx .ctx-pop')).visibility === 'visible'")
    check("侧栏 hover 展开 popover", pop_visible)
    page.click("#artifactCtx .ctx-dot")   # 点击展开 popover（触屏等价通道；hover 展开走 CSS）
    page.wait_for_timeout(400)
    check("上下文 popover 含预览与来源类型标注", page.locator("#artifactCtx .ctx-pop .actx-preview").count() == 1
          and page.locator("#artifactCtx .ctx-pop .ctx-pop-kind").count() == 1)
    check("追踪来源类型标注正确", "追踪" in page.locator("#artifactCtx .ctx-pop .ctx-pop-kind").inner_text())
    check("ku2-w2：「仅本轮」开关已退役（发送即清）", page.locator("#artifactCtx #actxOnce").count() == 0)
    check("隐私口径文案在场", "会发往你配置的 AI 服务商" in page.locator("#artifactCtx").inner_text()
          or "不出本机" in page.locator("#artifactCtx").inner_text())
    page.screenshot(path="/tmp/f1_ctxcard.png")

    # 发送一条消息（mock provider → utterance 路由）：文本与双挂点一起清空。
    page.wait_for_timeout(800)
    try:
        page.fill("#chatInput", "再看下有没有别的")
        page.click("#chatSendBtn")
        page.wait_for_timeout(3000)
        check("发送即清：侧栏文本清空", page.input_value("#chatInput") == "")
        check("发送即清：侧栏 chip 自动移除", page.locator("#artifactCtx").is_hidden())
        check("发送即清：主框 chip 同步移除", page.locator("#artifactCtxMain .ctx-chip").count() == 0)
    except Exception as e:
        check("上下文卡发送路径", False, str(e))

    # 任务 1a：hero 态（无结果）主输入框也要立即出现完整 chip
    page.goto(BASE, wait_until="load")
    page.wait_for_timeout(1500)
    try:
        if page.locator("#onboarding").is_visible():
            page.click("#onboardingSkip")
            page.wait_for_timeout(300)
    except Exception:
        pass
    page.click("#libNav")
    page.wait_for_timeout(1000)
    page.click("#artifactsWinBody [data-prj-use]")
    page.wait_for_timeout(900)
    check("追踪 × 主输入框：完整 chip 出现（hero 态）", page.locator("#artifactCtxMain .ctx-chip").is_visible())
    indent = page.evaluate("() => document.getElementById('queryInput').style.textIndent")
    check("主输入框首行 text-indent 随 chip 让位", bool(indent) and indent != "0px", indent)
    main_midline_delta = page.evaluate("""() => {
        const chip = document.querySelector('#artifactCtxMain .ctx-chip');
        const input = document.getElementById('queryInput');
        const c = chip.getBoundingClientRect(), r = input.getBoundingClientRect(), cs = getComputedStyle(input);
        const firstLineCenter = r.top + parseFloat(cs.paddingTop) + parseFloat(cs.lineHeight) / 2;
        return Math.abs((c.top + c.height / 2) - firstLineCenter);
    }""")
    check("主框完整 chip 与第一行文本中线对齐", main_midline_delta <= 2, str(main_midline_delta))
    check("主框完整 chip 有截断名与取消钮", page.locator("#artifactCtxMain .ctx-chip-name").count() == 1
          and page.locator("#artifactCtxMain .ctx-chip-x").count() == 1)

    # 收藏 × 主输入框：同一挂点改用爱心图标，popover 如实标注「收藏数据集」。
    page.click("#libNav")
    page.wait_for_timeout(700)
    page.click("#libTabFavs")
    page.wait_for_timeout(700)
    page.locator("#libPaneFavs [data-fav-use]").first.click()
    page.wait_for_timeout(700)
    check("收藏 × 主输入框：完整 chip 出现", page.locator("#artifactCtxMain .ctx-ico-dataset").is_visible())
    page.click("#artifactCtxMain .ctx-chip-main")
    check("收藏 × 主输入框：来源类型标注正确", "收藏数据集" in page.locator("#artifactCtxMain .ctx-pop-kind").inner_text())
    page.click("#artifactCtxMain .ctx-chip-x")

    # 再落一次结果，让收藏上下文进入窄侧栏输入框。
    page.fill("#queryInput", "人类乳腺癌数据")
    page.click("#submitBtn")
    page.wait_for_selector("#resultsGrid .card", timeout=20000)
    page.wait_for_timeout(700)
    page.click("#libNav")
    page.wait_for_timeout(700)
    page.click("#libTabFavs")
    page.wait_for_timeout(700)
    page.locator("#libPaneFavs [data-fav-use]").first.click()
    page.wait_for_timeout(700)
    check("收藏 × 侧栏输入框：小圆徽章出现", page.locator("#artifactCtx .ctx-dot-dataset").is_visible())
    page.hover("#artifactCtx .ctx-dot-dataset")
    page.wait_for_timeout(300)
    check("收藏 × 侧栏输入框：popover 来源类型正确", "收藏数据集" in page.locator("#artifactCtx .ctx-pop-kind").inner_text())

    # 若出现旧后端 422，上面的发送即清断言会直接失败；这里仅避免同一根因再被重复记成 JS 错误。
    js_errors = [e for e in console_errors if "422" not in e and "Unprocessable Entity" not in e]
    check("页面无 JS 报错", not js_errors, "; ".join(js_errors[:3]))
    browser.close()

print("\n" + ("FAILED: " + "; ".join(fails) if fails else "OK f1 e2e smoke"))
sys.exit(1 if fails else 0)
