"""重拍《使用说明书》里的界面图（Playwright 驱**系统 Edge**）。

为什么要有这个脚本：图是交付物的一部分，说明书里每张图都必须是**当前这一版**界面。
以前每次都靠临时脚本手动拍，换个人、隔一轮就复现不出同样的取景 —— 于是图慢慢和界面漂移。
这里把「拍哪几张、每张前置状态是什么」写成代码，任何时候都能一条命令重拍全部。

浏览器用 `channel="msedge"`：Windows 本机已装 Edge，不必再下一份 Chromium 内核；
2026-07-24 实测也比裸 `--headless` 驱内核稳。

前置：先起服务（`python scripts/run_web.py`，默认 7860），再跑本脚本。脚本**不会**自己起服务——
它拍的是「真的跑起来的那个界面」，服务由谁起、起在哪个端口，应当是调用者明确决定的事。

用法：
    python scripts/capture_manual_figures.py                     # 全部
    python scripts/capture_manual_figures.py --only act-receipt  # 只拍某几张
    python scripts/capture_manual_figures.py --base-url http://127.0.0.1:7861
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "使用说明书" / "图"

VIEWPORT = {"width": 1440, "height": 900}
SCALE = 2                      # 说明书是 A4 打印稿，2x 才不糊
SETTLE_MS = 700                # 入场动画 / 结果渲染的收尾时间


def _health(base_url: str) -> str:
    # 服务端被长 LLM 调用（死地址重试预算可达数分钟）卡住事件循环时，health 会排队——
    # 10s 太短会把「忙」误判成「没起来」（2026-08-18 ai-fallback 重拍真踩过）。
    with urllib.request.urlopen(base_url.rstrip("/") + "/api/health", timeout=60) as resp:
        return resp.read().decode("utf-8")


def _new_page(browser, base_url: str):
    """每张图一个干净上下文：不继承上一张的 localStorage / 设置 / 结果。"""
    ctx = browser.new_context(
        viewport=VIEWPORT,
        device_scale_factor=SCALE,
        locale="zh-CN",
        # 动画一律推到终态再拍：入场动画拍到一半会出现半透明 / 位移错位的假图。
        reduced_motion="reduce",
    )
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    # 首次进入的轻量导览会盖住整屏 —— 先标记成看过，再进页面。
    # consent v2（键 `biodata_consent_v2`）同理：未标记时首次检索前弹 #consentModal 阻断提交，
    # 凡涉及检索的图（results/dataset/continue/act-receipt/ai-fallback）都会卡死在等待结果上
    #（2026-08-23 ku1-w4 重拍 results.png 真踩过：/api/interpret 发出后被弹窗拦住，/api/utterance
    #  始终没发，240s 超时——与 visual_regression.py:207-214 同源同修）。
    page.add_init_script("try { localStorage.setItem('biodata_onboarding_v1', '1'); } catch (e) {}"
                         "try { localStorage.setItem('biodata_consent_v2', '1'); } catch (e) {}")
    # 不用 networkidle：首页起来后仍有常驻/延迟请求，networkidle 会一直等到超时。
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector("#queryInput", timeout=30_000)
    page.wait_for_timeout(SETTLE_MS)
    return ctx, page, errors


def _search(page, query: str, timeout_ms: int = 240_000) -> None:
    page.fill("#queryInput", query)
    page.press("#queryInput", "Enter")
    # 结果区显形即可：0 结果时它渲染空态卡，同样是一次完成的检索。
    # 2026-08-18：服务端已配真实 AI key 时，零配置会话会自动切到真实 LLM——重排/润色走
    # 远端接口，单次检索可超过 60s（限流时更久），dataset/act-receipt/ai-fallback 三张
    # 曾集体撞这条 60s 超时。放宽到 240s：拍图要的是「当前真实界面」，等真结果比降级快拍重要。
    page.wait_for_function("() => { const w = document.getElementById('resultsWrap');"
                           " return w && w.style.display !== 'none'; }", timeout=timeout_ms)
    page.wait_for_timeout(SETTLE_MS)


def _open_settings(page) -> None:
    page.click("#settingsBtn")
    page.wait_for_selector('#settings[aria-hidden="false"]', timeout=10_000)
    page.wait_for_timeout(SETTLE_MS)


def _set_checkbox(page, selector: str, want: bool) -> None:
    """开关是自定义样式（真 input 被 .slider 盖住），所以直接改 checked 再派发 change。

    刻意不用 page.check()：那会点到看不见的原生 input 上，在这套 markup 里不稳定。
    派发真事件 → 页面自己的 onchange 逻辑照常跑，与用户手点等价。
    选择器先判存在再读 checked：设置抽屉的开关 ID 改版时会变，el 为 null 直接读
    .checked 抛的是 TypeError，报错里看不出是哪个开关没对上——先点名选择器。
    """
    found = page.evaluate(
        """([sel, want]) => { const el = document.querySelector(sel);
             if (!el) return false;
             if (el.checked !== want) { el.checked = want;
               el.dispatchEvent(new Event('change', {bubbles: true})); }
             return true; }""",
        [selector, want],
    )
    assert found, f"{selector} 不在页面上——开关 ID 可能改了，先核对设置抽屉 DOM"
    assert page.is_checked(selector) == want, f"{selector} 没能切到 {want}"


# ---------------------------------------------------------------- 逐张取景

def shot_home(page, path: Path) -> None:
    page.screenshot(path=str(path))


def shot_results(page, path: Path) -> None:
    _search(page, "推荐有 FASTQ 的人类乳腺癌数据")
    # 结果区显形只是 pre-loop 先行帧（「初步结果」徽标 + 侧栏「正在更深一步思考…」在途泡）；
    # 说明书图要的是终态（旧图即终态：侧栏「检索完成…」）。徽标唯一写口是 search.js _prelimBadge，
    # final 帧落地即摘——等它隐去再拍。规则档秒回时徽标从没亮过，本等待即刻返回。
    #（2026-08-23 ku1-w4 重拍真踩过：只等 resultsWrap 显形，把「- 18s」在途计时泡拍进了图。）
    page.wait_for_function("() => { const b = document.getElementById('prelimBadge');"
                           " return !b || b.hidden; }", timeout=300_000)
    page.wait_for_timeout(SETTLE_MS)
    page.screenshot(path=str(path))


def shot_browse(page, path: Path) -> None:
    page.click('.nav-item[data-view="browse"]')
    page.wait_for_selector("#browseGrid .card", timeout=60_000)
    page.wait_for_timeout(SETTLE_MS)
    page.screenshot(path=str(path))


def shot_settings(page, path: Path) -> None:
    _open_settings(page)
    page.screenshot(path=str(path))


def shot_continue(page, path: Path) -> None:
    # 2026-08-18 取景改道 ×2：
    # ① 不再发追问句——追问进真实 LLM 环（DeepSeek 分流共识 + ReAct），一轮 >10min，
    #    等到超时只会把「规划中…」半成品泡拍进说明书（连踩三次）；
    # ② 检索前关掉「AI 执行」——否则初始检索自己也挂着「正在更深一步思考…」中途态。
    #    关掉后走确定性管线秒回，对话留下完整的一问一答；图要展示的是页签布局
    #   （查询条件 + 对话 + 输入条 + 范围弹层），一轮完成的对话就是合格样板。
    _open_settings(page)
    _set_checkbox(page, "#cfgAgentExec", False)
    page.click("#settingsClose")
    page.wait_for_timeout(300)
    _search(page, "人类肺癌的单细胞数据")
    page.click("#swTabBoard")          # 侧栏切到「对话记录」
    page.wait_for_timeout(400)
    # dock1（2026-08-05）：范围弹层重排为「右坞双圆钮 + 单面板」。拍前点开弹层，
    # 让两枚竖排圆钮（数据来源 / 发表时间）与自动识别态的从简面板入镜。
    page.click("#scopeChip")
    page.wait_for_selector("#scopePop:not([hidden])", timeout=10_000)
    page.wait_for_timeout(SETTLE_MS)
    # 规则档秒回时进度收尾链（finishProgress/resetSubmitButton）没被调到，_pctActive 滞留为真，
    # 回音泡右端的 #cbLivePct「0%」在**每次重渲**时被重新注入（真实前端残留，已上报待修）——
    # 所以摘除必须放在所有会触发重渲的动作之后、按快门之前的最后一步
    #（2026-08-18：放切页签之前摘被重渲重新注入；还曾错摘 #cbProgPct——挂着的这颗是 #cbLivePct）。
    page.evaluate("document.querySelectorAll('#cbHistory .cbh-live-pct, #cbHistory .cbh-prog-num')"
                  ".forEach(e => e.remove())")
    page.wait_for_timeout(120)
    _clip_shot(page, path, "#sideWork")


def shot_dataset(page, path: Path, base_url: str) -> None:
    _search(page, "人类肺癌的单细胞数据")
    with page.context.expect_page() as tab:
        page.click("#resultsGrid .btn-detail >> nth=0")
    detail = tab.value
    detail.wait_for_load_state("domcontentloaded")
    detail.wait_for_timeout(2500)
    detail.screenshot(path=str(path))


def shot_act_receipt(page, path: Path) -> None:
    """一句话执行回执（p10：长在对话流里的执行总结）。

    2026-08-18 取景前提更新：本仓库服务端现已配好真实 AI key（health.llm_server.key_detected），
    零配置会话会自动切到真实 LLM——拍到的就是用户默认看到的 **AI 路径回执**（明细里写明
    「哪项是大模型替你填的、哪项没另外核对」），说明书图 8 的图注已按此口径改写。
    旧前提（无 AI 的规则兜底档、「按关键词猜的」注记）要复现得另起一个不带 key 的服务，
    那是 10.5 边界 ④ 的文字内容，不再用图展示。

    2026-08-24 dl-auto-1 取景前提更新：pack.download 档在模型首次调用即**自动开始下载**、
    不再停确认闸，回执正文由「请在面板点确认」改为「已开始下载 N 个数据集共约 X，写入
    ⟨目录⟩；进度在下载面板看，可增删」。本函数用「人类肺癌数据，打包前5条」取景，`AI 执行`
    默认开 → 会**真的触发一次真实下载**（写入本机 `~/Downloads/BioData数据-*`）：拍完如需
    清理，调 `/api/download/cancel` 取消并删除本次测试产生的目录（别删用户既有下载目录）。
    取景走的是「先有结果、再在对话窗里说动作」档——回执是带「明细（没做到的）/执行过程」
    折叠区的系统总结泡，图 8 要展示的就是它。折叠区拍前展开，读者才看得见「没做到的」那几行。
    """
    _open_settings(page)
    # 「AI 执行」现行开关（旧 #cfgAutoAct 已随 2026-08-03 设置重构退役）。默认开，
    # 这里显式钉一下防默认值漂移；未配 key 时它若真从关被切开会被禁点闸弹回——
    # 那时 assert 报错正说明这张图的拍摄前提（无 AI 的规则兜底档）要重新设计。
    _set_checkbox(page, "#cfgAgentExec", True)
    page.click("#settingsClose")
    page.wait_for_timeout(300)
    _search(page, "人类肺癌数据")
    # 有结果后在侧栏对话窗说动作：回执以系统总结泡呈现（明细/执行过程折进 details）。
    # 整句重述「人类肺癌数据，打包前5条」而非只说「打包前5条」：chat 发送会把原话写进
    # #queryInput（board.js 发送即清空改动），任务包预览读的就是它——只说动作词会让预览
    # 拿动作句当检索句、0 命中假失败（该疑点已上报，此处先把图拍成）。
    page.fill("#chatInput", "人类肺癌数据，打包前5条")
    page.click("#chatSendBtn")
    page.wait_for_selector("#cbHistory .cbh-sys-extra", timeout=30_000)
    # 等总结泡的收尾渲染（执行注记回标那帧会重建 DOM）落定再动手——展开早了会被后到的重画抹掉。
    page.wait_for_timeout(1500)
    # 只展开「明细」折叠区（没做到的注记折在里面，不展开图里看不见）；「执行过程」保持
    # 默认折叠态入镜——那正是用户看到的初始样子，也让图保持一屏之内。
    page.evaluate("const d = document.querySelector('#cbHistory .cbh-sys-extra details'); if (d) d.open = true")
    # 回执整轮高于默认滚动视口：先加高视口、把它滚到滚动区顶再拍——否则元素截图被
    # 滚动容器裁掉下半截（本轮真被裁过一次）。
    page.set_viewport_size({"width": VIEWPORT["width"], "height": 1500})
    page.evaluate("document.querySelector('#cbHistory .cbh-turn:has(.cbh-sys-extra)')?.scrollIntoView({block: 'start'})")
    page.wait_for_timeout(SETTLE_MS)
    # 只取回执那一轮（总结泡正文 + 纠错 chips + 展开的明细）——图 8 展示的是回执本身。
    _clip_shot(page, path, "#cbHistory .cbh-turn:has(.cbh-sys-extra)")


def shot_ai_fallback(page, path: Path) -> None:
    """摘要句里的「没能完成」档。

    真去调一次、真失败：model 填一个 provider 上不存在的名字（接口地址保持与服务端一致，
    服务端密钥照旧适用），provider 秒回 4xx——`rerank` 记的是 `llm_call_failed`
    （试过但没成），而不是 `llm_not_configured`（压根没配）—— 摘要句必须写「没能完成」。
    不需要任何真实密钥值，只需要服务端已配好 key（本仓库交付默认态）。
    """
    _open_settings(page)
    page.evaluate("() => { document.getElementById('apiConfig').open = true; }")   # 展开「AI / API 配置」
    page.wait_for_timeout(300)
    # 2026-08-18 失败手法换代：旧手法 base_url 指死端口（127.0.0.1:9）——但那次调用会
    # 把服务端事件循环卡进同步网络栈的重试预算（实测 >8min，期间 health 都排队），
    # 拍图等不起、同实例其它图也被拖死。新手法：base_url 保持与服务端一致（
    # 服务端 key 按 _build_request_overrides 同约继续适用）、api key 留空、
    # 只把 model 填成一个不存在的名字——provider 秒回 4xx「模型不存在」，
    # 同样是「真去调了、真失败」（llm_call_failed，非 auth_error），但毫秒级落地。
    page.select_option("#cfgProvider", "compatible")
    page.fill("#cfgBaseUrl", "https://api.deepseek.com")   # 与服务端一致 → 服务端 key 适用
    page.fill("#cfgModel", "model-does-not-exist-on-purpose")
    page.fill("#cfgApiKey", "")
    # 2026-08-18（M1 环架构后取景改道）：必须关掉「AI 执行」。开着时查询先撞分流那次 LLM
    # 调用——它失败后整路回退成纯规则检索，rerank 层根本不上场，摘要句既没有「AI 重排」
    # 也没有「没能完成」，这张图就丢了它的论点。
    # 关掉后查询直走确定性管线，llm_rerank 层真去调、真失败，「没能完成」才落进摘要句。
    _set_checkbox(page, "#cfgAgentExec", False)
    _set_checkbox(page, "#cfgStrategy", False)      # 关自动选择，强制用手动勾选的层
    _set_checkbox(page, "#cfgRerank", True)         # AI 重排 on
    page.click("#settingsClose")
    page.wait_for_timeout(300)
    _search(page, "人类肺癌的单细胞数据")
    assert "没能完成" in page.inner_text("#resultSummaryText"), \
        "摘要句没有「没能完成」——AI 重排的失败路径又改道了，先核对再拍"
    page.wait_for_timeout(SETTLE_MS)
    _clip_shot(page, path, "#searchTrace")


def _clip_shot(page, path: Path, selector: str) -> None:
    """只拍某个元素。

    用 Playwright 的**元素截图**，不自己算 clip 矩形。两条都踩过：
    ① 只给 `page.screenshot(clip=…)` 而不整页截 → 超出视口的部分被**静默截断**，
       元素以后长高一点图就悄悄少一截，脚本照样报 OK；
    ② 改成 `full_page=True` + 同一个 clip 又更糟 —— `bounding_box()` 给的是**视口**坐标，
       `full_page` 的 clip 吃的是**文档**坐标，页面一滚动就整块拍偏（本轮真拍偏过一次）。
    元素截图两件事都由 Playwright 负责：它自己滚动、自己处理比视口高的元素。
    """
    loc = page.locator(selector)
    assert loc.count(), f"{selector} 不在页面上，没法出图"
    loc.first.screenshot(path=str(path))


FIGURES = {
    "home": ("home.png", lambda p, path, url: shot_home(p, path)),
    "results": ("results.png", lambda p, path, url: shot_results(p, path)),
    "browse": ("browse.png", lambda p, path, url: shot_browse(p, path)),
    "settings": ("settings.png", lambda p, path, url: shot_settings(p, path)),
    "continue": ("continue.png", lambda p, path, url: shot_continue(p, path)),
    "dataset": ("dataset.png", lambda p, path, url: shot_dataset(p, path, url)),
    "act-receipt": ("act-receipt.png", lambda p, path, url: shot_act_receipt(p, path)),
    "ai-fallback": ("ai-fallback.png", lambda p, path, url: shot_ai_fallback(p, path)),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:7860")
    ap.add_argument("--only", nargs="*", choices=sorted(FIGURES), default=None)
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()

    try:
        print("health:", _health(args.base_url))
    except Exception as exc:
        print(f"服务没起来（{args.base_url}）：{exc}\n先跑 `python scripts/run_web.py`。", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺 playwright：`pip install playwright`（浏览器用系统 Edge，无需 `playwright install`）", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = args.only or list(FIGURES)
    failed: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        for key in wanted:
            filename, fn = FIGURES[key]
            path = out_dir / filename
            ctx, page, errors = _new_page(browser, args.base_url)
            try:
                fn(page, path, args.base_url)
                size = path.stat().st_size
                status = "OK " if not errors else "ERR"
                print(f"[{status}] {filename:18} {size / 1024:7.1f} KiB"
                      + (f"  console/page errors: {errors}" if errors else ""))
                if errors:
                    failed.append(f"{key}: {errors}")
            except Exception as exc:
                print(f"[FAIL] {filename:18} {type(exc).__name__}: {exc}")
                failed.append(f"{key}: {exc}")
            finally:
                ctx.close()
        browser.close()

    if failed:
        print("\n有图没拍成 / 拍摄过程中有前端报错：")
        for line in failed:
            print("  -", line)
        return 1
    print("\nCAPTURE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
