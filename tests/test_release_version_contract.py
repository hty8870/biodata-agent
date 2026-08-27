"""版本 / 工具数的**跨文件单一真源**门。

2026-07-17 全盘审计的一批文档漂移，根因都在这里：这个门原本只钉 `webapp.py` ↔ `launch_web.ps1`
两处，**没钉面向客户的文档**。于是代码一路走到 1.4.0，而 README 与 DEVELOPMENT 还写着「当前 Web API
版本为 1.2.0」；MCP 工具从 5 个长到 9 个，README 却还列着 5 个、并明确重申「MCP 工具数仍为 5」——
**唯一会写盘的工具 `upload_dataset` 对客户完全不可见**。

所以这里补的不是「把数字改对」（改一次、下次接着漂），而是**把文档纳入同一个门**：
数字从代码里读，文档必须与之相符。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _web_api_version() -> str:
    webapp = (ROOT / "src" / "dataset_recommender" / "app" / "webapp.py").read_text(encoding="utf-8")
    m = re.search(r'^WEB_API_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', webapp, re.MULTILINE)
    assert m, "webapp.py 里找不到 WEB_API_VERSION"
    return m.group(1)


def _expected_tools() -> list[str]:
    src = (ROOT / "src" / "dataset_recommender" / "app" / "mcp_server.py").read_text(encoding="utf-8")
    m = re.search(r"^_EXPECTED_TOOLS = \(\n(.*?)^\)$", src, re.MULTILINE | re.DOTALL)
    assert m, "mcp_server.py 里找不到 _EXPECTED_TOOLS"
    return re.findall(r'"([a-z_]+)"', m.group(1))


def test_launcher_expected_version_matches_web_health_version() -> None:
    webapp = (ROOT / "src" / "dataset_recommender" / "app" / "webapp.py").read_text(encoding="utf-8")
    ps_launcher = (ROOT / "scripts" / "launch_web.ps1").read_text(encoding="utf-8-sig")
    sh_launcher = (ROOT / "scripts" / "launch_web.sh").read_text(encoding="utf-8")
    web_match = re.search(r'^WEB_API_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', webapp, re.MULTILINE)
    ps_match = re.search(r"^\$ExpectedVersion = '([0-9]+\.[0-9]+\.[0-9]+)'$", ps_launcher, re.MULTILINE)
    sh_match = re.search(r"^EXPECTED_VERSION_FALLBACK='([0-9]+\.[0-9]+\.[0-9]+)'$", sh_launcher, re.MULTILINE)
    assert web_match and ps_match and sh_match
    # 2.1.0：`results[]` additive 新增 `gene_count`（10x 平台信息补充旁挂表；无补充为 ""），
    #        卡片与介绍页关键事实同步展示「检测基因数」（s1）。
    # 2.0.0：`/api/utterance` 重写为 turn pipeline（breaking：响应 {route,query,plan,echo_zh,retrieval,via}，
    #        route ∈ search/tool/none；规则匹配一切指令都过、零命中≠无效，LLM 分流带原话+命中概览+
    #        当前条件；检索动词携带 effective_query）。入参裁掉条件板上下文字段（refine 改由 LLM 分流）。
    # 1.9.0：新增 `/api/act/summary`（p10：执行结果的 LLM 中文总结；只总结不执行，fail-open，
    #        护栏写进 user prompt——ok=False 绝不说「已」；LLM 配置链与 /api/action/plan 同径）。
    # 1.8.0：新增 `/api/curate/plan` + `/api/curate/apply`（对话式数据库管护两步确认；
    #        与 MCP curate_datasets / CLI curate_datasets.py 共用 corpus_curation 单一真源）。
    # 1.7.0：`search_trace.steps[]` additive 新增 `fallback_note`——某一层回退时「该怎么对用户说」由后端出话，
    #        前端不再自己写死措辞（此前一律说成「本次未启用」，把故障说成了选择）。
    # 1.6.0：新增 `/api/action/plan`（一句话执行层，只出计划不执行）。
    # 2.2.0：新增 `/api/curate/sync-updates`（2026-08-06「工作流即工具」批 `curate.sync_updates`：
    #        检查更新→有新增则自动入库的复合流；无 token——原子调用无信任边界，回收站可撤）。
    # 2.3.0：`/api/utterance` 入参 additive 新增 `req_id`（2026-08-08 idem1 P0：断流重发幂等——
    #        服务端按 req_id 认领，同号在途等 owner 收尾回缓存体，不再二次执行写工具；
    #        无 req_id 行为逐位不变；响应体形状零变化）。
    # 2.4.0：新增 `/api/curate-examples/pending` + `/approve` + `/dismiss`（2026-08-13 操作样例库
    #        改用户挑选入库：机械收录只进候选池，勾选迁入正式库，注入侧只读正式库）。
    # 2.5.0：engagement 落地包新增 4 端点（2026-08-22 Wave 1A-B2 / Wave 2-P5 先落地、Wave 3 统一
    #        收口 bump）：`/api/curate/sync-status`（实例级同步状态）+ `/api/curate/recall`
    #        （按 operation_id 整次撤回一次 sync）+ `/api/watch/check`（课题更新检查的确定性
    #        重跑，≤200 无序 uid+语义指纹+truncated）+ `/api/artifacts/export-pack`（课题研究
    #        材料 ZIP 导出：manifest/纳入排除表/下载任务包/三格式引文/溯源/方法草稿/recipe）。
    # 2.6.0：新增 `/api/download/update`（2026-08-24 dl-auto-1 在途增删：对运行中的下载队列做
    #        add/remove——remove 排队=跳过、remove 正在下载=中止并清理未完成部分后继续下一条、
    #        remove 已完成=如实拒绝、add=追加进队列尾部；additive）。
    # 2.7.0：新增 `/api/admin/corpus-sync` + `/api/admin/corpus-sync/status` + `/api/curate/sync-updates/status`
    #        （2026-08-26 语料定期更新批：进程内单飞语料同步 job，admin 双闸端点 + 用户侧异步
    #        状态查询；`/api/curate/sync-updates` guard on 改 202 异步、guard off 逐字节不变；additive）。
    assert web_match.group(1) == ps_match.group(1) == sh_match.group(1) == "2.7.0"
    assert "src\\dataset_recommender\\app\\webapp.py" in ps_launcher
    assert "src/dataset_recommender/app/webapp.py" in sh_launcher
    assert "src\\dataset_recommender\\webapp.py" not in ps_launcher
    builder = (ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8")
    assert '"product_version": resolved_product_version' in builder
    assert '"--expected-version"' in builder


def test_customer_docs_state_the_real_web_api_version() -> None:
    """README / DEVELOPMENT 里「当前 Web API 版本为 X」必须等于代码里的 WEB_API_VERSION。

    只匹配「**当前**版本」这种断言句；`/api/diagnose` 从 `1.2.0` 起只接受 POST 那类**历史陈述**
    刻意不在匹配范围内（它讲的是某个变更发生在哪个版本，与当前版本无关）。
    """
    real = _web_api_version()
    for rel in ("README.md", "DEVELOPMENT.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        claims = re.findall(r"当前 Web API 版本为 `([0-9]+\.[0-9]+\.[0-9]+)`", text)
        assert claims, f"{rel} 里找不到「当前 Web API 版本为 …」的断言句"
        for c in claims:
            assert c == real, f"{rel} 声称当前版本 {c}，代码是 {real}"


def _mcp_server_version() -> str:
    src = (ROOT / "src" / "dataset_recommender" / "app" / "mcp_server.py").read_text(encoding="utf-8")
    m = re.search(r'^_SERVER_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', src, re.MULTILINE)
    assert m, "mcp_server.py 里找不到 _SERVER_VERSION"
    return m.group(1)


def _route_decorator_count() -> int:
    """`webapp.py` 里的路由装饰器条数（`@app.get/post/...` + `@app.api_route`）。

    刻意不 import app 去数 `app.routes`：那会把 FastAPI 自带的 `/docs`、`/openapi.json`、
    `/redoc`、`/static` 一起数进来，与文档表里「我们自己声明了哪些路由」不是同一件事。
    """
    src = (ROOT / "src" / "dataset_recommender" / "app" / "webapp.py").read_text(encoding="utf-8")
    return len(re.findall(r"^@app\.(?:get|post|put|delete|head|patch|api_route)\(", src, re.MULTILINE))


def test_developer_doc_route_table_counts_every_decorator() -> None:
    """DEVELOPMENT.md §6 自称「与 webapp.py 的路由装饰器一一对应，共 N 个」。

    2026-07-26 抓到：那句话说 24，实际 25 —— 少的是 `/dataset`（详情页独立路由，ux3b 那轮加的），
    表里**整行缺失**。「一一对应」这种自我声明没有门就是一句空话。
    """
    text = (ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")
    m = re.search(r"与 `webapp\.py` 的路由装饰器一一对应，共 (\d+) 个", text)
    assert m, "DEVELOPMENT.md §6 里找不到「与 webapp.py 的路由装饰器一一对应，共 N 个」"
    real = _route_decorator_count()
    assert int(m.group(1)) == real, f"DEVELOPMENT.md 说 {m.group(1)} 条路由，实际装饰器 {real} 条"


@pytest.mark.skipif(
    not (ROOT / "docs" / "agent" / "FRONTEND.md").exists(),
    reason="docs/agent/ 是内部工程文档，公开仓不含；有它时照常校验版本一致性",
)
def test_frontend_doc_states_the_real_web_api_version_and_route_count() -> None:
    """`docs/agent/FRONTEND.md` 是**前端改动的第一读物**，它写错版本会一路带偏。
    2026-07-26 抓到它停在 `1.4.0` / 20 条路由——落后两个版本。"""
    text = (ROOT / "docs" / "agent" / "FRONTEND.md").read_text(encoding="utf-8")
    m = re.search(r"Web API 版本 `([0-9]+\.[0-9]+\.[0-9]+)`，(\d+) 条路由", text)
    assert m, "FRONTEND.md 里找不到「Web API 版本 `X`，N 条路由」"
    assert m.group(1) == _web_api_version(), f"FRONTEND.md 声称 {m.group(1)}，代码是 {_web_api_version()}"
    assert int(m.group(2)) == _route_decorator_count()


def test_acceptance_note_states_the_real_web_api_version() -> None:
    """`测试说明.txt` 是**客户拿到包之后照着做的第一件事**。

    它让客户访问 `/api/health` 确认 `version="X"`——这个数字一旦落后，客户照做会看到不一致，
    合理反应是「这个包是不是发错了」。2026-07-26 抓到它停在 1.5.0 而代码已是 1.6.0。
    """
    real = _web_api_version()
    text = (ROOT / "docs" / "测试说明.txt").read_text(encoding="utf-8")
    claims = re.findall(r'version="([0-9]+\.[0-9]+\.[0-9]+)"', text)
    assert claims, "测试说明.txt 里找不到 `version=\"…\"` 验收行"
    for c in claims:
        assert c == real, f"测试说明.txt 让客户核对 version={c}，代码是 {real}"
    for c in re.findall(r"--expected-version ([0-9]+\.[0-9]+\.[0-9]+)", text):
        assert c == real, f"测试说明.txt 的 --expected-version {c} 与代码 {real} 不符"


def test_customer_manual_states_the_real_versions_and_tool_count() -> None:
    """《使用说明书》是交付给客户的主文档，封面与末尾都自报版本与工具数。

    2026-07-26 抓到：正文已经写进了 1.6.0 才有的「说了就直接做」一节，封面却仍是
    网页 1.5.0 / MCP 1.27.0 / 16 个工具，工具表里也没有 `plan_action` 那一行。
    """
    web, mcp_ver, n = _web_api_version(), _mcp_server_version(), len(_expected_tools())
    text = (ROOT / "docs" / "使用说明书" / "使用说明书.html").read_text(encoding="utf-8")
    assert f"网页服务版本 {web}" in text, f"说明书封面没写「网页服务版本 {web}」"
    assert f"MCP服务版本 {mcp_ver}" in text, f"说明书封面没写「MCP服务版本 {mcp_ver}」"
    assert f"网页服务 {web}、AI 助手接入服务 {mcp_ver}（{n} 个工具）" in text, "说明书末尾的版本声明没对上"
    # 「除 upload_dataset、provision_dataset 与 curate_datasets 外，其余 N 个工具都不写任何东西」讲的是
    # **只读**工具数 = 总数 - 3（v1.30.0 起写盘工具有三个：upload_dataset 写外部库、provision_dataset
    # 写调用方指定目录、curate_datasets 管护 external 的 upload_* 与回收站），与其它「共 N 个工具」
    # 不是同一个数；分开钉，别把写工具声明混进去。
    assert f"其余 {n - 3} 个工具" in text, f"说明书没写「除三个写盘工具外，其余 {n - 3} 个工具…」"
    for m in re.finditer(r"(?:其余 )?(\d+) 个工具", text):
        expected = n - 3 if m.group(0).startswith("其余") else n
        assert int(m.group(1)) == expected, f"说明书里写着「{m.group(0)}」，应为 {expected} 个"
    missing = [t for t in _expected_tools() if f"<code>{t}</code>" not in text]
    assert not missing, f"说明书第 11 章的工具表漏了：{missing}（客户看不见这些能力）"


def test_customer_manual_figures_are_numbered_in_order_and_all_files_exist() -> None:
    """说明书的图是交付物，掉一张图 / 序号乱一位，客户拿到的就是残缺的 PDF。

    2026-07-26 这一轮往正文中间插图（§5.1 的「AI 没能完成」那张），后面四张的序号全要顺移——
    这类顺移是纯手工活，漏一处不会有任何东西报错。所以钉三件事：
    ① 图号从 1 连续递增；② 每个 `<img src="图/…">` 指向的文件真的在盘上；③ 每张图都有 alt。
    """
    manual = ROOT / "docs" / "使用说明书" / "使用说明书.html"
    text = manual.read_text(encoding="utf-8")

    numbers = [int(m.group(1)) for m in re.finditer(r"<figcaption>图 (\d+)　", text)]
    assert numbers, "说明书里一张带编号的插图都没有"
    assert numbers == list(range(1, len(numbers) + 1)), f"图号不连续：{numbers}"

    srcs = re.findall(r'<img src="(图/[^"]+)"', text)
    assert len(srcs) == len(numbers), f"{len(srcs)} 张图 / {len(numbers)} 条图注，对不上"
    missing = [s for s in srcs if not (manual.parent / s.replace("/", "\\")).exists()
               and not (manual.parent / s).exists()]
    assert not missing, f"说明书引用了不存在的图片：{missing}"

    no_alt = re.findall(r'<img src="图/[^"]+"(?![^>]*\balt=)[^>]*>', text)
    assert not no_alt, f"这些插图没有 alt（打印稿之外的可访问性）：{no_alt}"


def test_customer_docs_list_every_mcp_tool() -> None:
    """README 的 MCP 章节必须与 `_EXPECTED_TOOLS` 一致：数量对得上、**每个工具名都出现**。

    钉「每个名字都出现」而不只是数量：数量对但漏了 upload_dataset、多写一个不存在的，都要红。
    """
    tools = _expected_tools()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    n = re.search(r"项目提供 (\d+) 个 stdio MCP 工具", readme)
    assert n, "README 里找不到「项目提供 N 个 stdio MCP 工具」"
    assert int(n.group(1)) == len(tools), f"README 说 {n.group(1)} 个工具，实际 {len(tools)} 个"
    missing = [t for t in tools if f"`{t}`" not in readme]
    assert not missing, f"README 的 MCP 工具表漏了：{missing}（客户看不见这些能力）"


def test_selfcheck_tool_count_in_tutorial_matches_code() -> None:
    """安装教程里每一处 `SELFCHECK_OK tools=N` 都必须等于真实工具数。

    背景：v1.10.0 那次只改了 Windows 正文、漏了附录 A（macOS/Linux）——同一 commit 两处只改一处，
    于是 macOS 用户照教程验收会以为 tools=9 是异常。这条把「每一处」都纳进来。
    """
    n = len(_expected_tools())
    hits = []
    for md in (ROOT / "使用教程").rglob("*.md"):
        for m in re.finditer(r"SELFCHECK_OK tools=(\d+)", md.read_text(encoding="utf-8")):
            hits.append((md.relative_to(ROOT).as_posix(), int(m.group(1))))
    assert hits, "教程里找不到任何 SELFCHECK_OK tools=N 验收行"
    bad = [(p, v) for p, v in hits if v != n]
    assert not bad, f"教程验收行与真实工具数({n})不符：{bad}"


@pytest.mark.skipif(
    not (ROOT / "docs" / "agent" / "QUALITY_GATES.md").exists(),
    reason="docs/agent/ 是内部工程文档，公开仓不含；有它时照常校验工具数一致性",
)
def test_quality_gates_doc_states_the_real_tool_count() -> None:
    n = len(_expected_tools())
    text = (ROOT / "docs" / "agent" / "QUALITY_GATES.md").read_text(encoding="utf-8")
    m = re.search(r"tools/list（(\d+) 个预期工具", text)
    assert m, "QUALITY_GATES.md 里找不到「tools/list（N 个预期工具」"
    assert int(m.group(1)) == n, f"QUALITY_GATES.md 说 {m.group(1)} 个，实际 {n} 个"


# ---------------------------------------------------------------- 文档工具表 ↔ _EXPECTED_TOOLS
#
# 2026-07-18 审查抓到：客户安装教程正文写「九个工具」+ 9 行表，实际 14 个——**穿过 v1.13→v1.22 五个
# 版本无人察觉**，因为唯一的守卫 test_selfcheck_tool_count_in_tutorial_matches_code 只看 `SELFCHECK_OK
# tools=N` 那一行，从不读正文和工具表。这里把「每个工具名都必须在文档里出现」纳入门：新增工具却漏改
# 这些表 → 立刻红。（数量措辞用中文数字，难以稳健正则；改钉「每个名字都在」，等价且更强。）

def _doc_missing_tools(rel_path: str) -> list[str]:
    text = (ROOT / rel_path).read_text(encoding="utf-8")
    return [t for t in _expected_tools() if t not in text]


def test_tutorial_lists_every_mcp_tool() -> None:
    missing = _doc_missing_tools("使用教程/MCP安装/MCP_安装教程.md")
    assert not missing, f"安装教程漏了 MCP 工具：{missing}（客户照它验收会看不到这些能力）"


def test_modules_doc_lists_every_mcp_tool() -> None:
    missing = _doc_missing_tools("MODULES.md")
    assert not missing, f"MODULES.md（权威契约文档）漏了 MCP 工具：{missing}"


def test_developer_doc_lists_every_mcp_tool() -> None:
    missing = _doc_missing_tools("DEVELOPMENT.md")
    assert not missing, f"DEVELOPMENT.md §7 MCP 工具表漏了：{missing}"


def test_all_first_party_static_assets_share_one_cache_generation() -> None:
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    versions = re.findall(r'/static/(?:css|js)/[^"?]+\?v=([a-zA-Z0-9-]+)', html)
    assert len(versions) == 88   # 2026-08-23 ku3w1 实测 88（新增 #flow_trace importmap 条目；构成 = 1 css + script 标签 + importmap 条目，随模块增删以实测为准。历史：ku1w1 87 / eng1 86 / b1 60）
    assert set(versions) == {CACHE_GENERATION}


def test_dataset_page_loads_the_usage_layer_it_actually_calls() -> None:
    """`dataset.html` 必须加载 usage_core.js + usage_log.js。

    2026-07-29 真机抓到：详情页只加载 core.js / cards.js / fav_folders.js / dataset_page.js，
    但 `core.js` 的 `toggleFav` 与 `cards.js` 的 `buildCard` 都**直接**调 `usageLog` / `USAGE_KINDS`
    （本仓库刻意不给跨模块调用加 `typeof` 守卫，见 FRONTEND.md §4.3）——于是在详情页点心形收藏
    抛 ReferenceError：收藏其实已写进 localStorage，但心形不变实心、不弹 toast、popover 不关，
    在用户看来就是「点了没反应」。首页三门全绿，因为它们都只读 index.html。
    """
    html = (ROOT / "web" / "static" / "dataset.html").read_text(encoding="utf-8")
    for js in ("core/usage_core.js", "core/usage_log.js"):
        assert f"/static/js/{js}?v=" in html, f"dataset.html 没加载 {js}，该页调 usageLog 会 ReferenceError"
    # 反向钉：这两个文件真的定义了详情页用到的那两个名字（改名就红，而不是运行时才炸）
    core_js = (ROOT / "web" / "static" / "js" / "core" / "usage_core.js").read_text(encoding="utf-8")
    log_js = (ROOT / "web" / "static" / "js" / "core" / "usage_log.js").read_text(encoding="utf-8")
    assert "USAGE_KINDS" in core_js and "function usageLog(" in log_js


def test_dataset_page_shares_the_same_cache_generation() -> None:
    """独立详情页 `dataset.html` 必须与首页**同一代**令牌。

    2026-07-21 实测漂移：首页已 bump 到 `20260720-ux4`，`dataset.html` 还停在 `20260720-ux3g`——
    上面那条只读 index.html，从不看详情页，所以三门全绿。两页**共用** core.js / cards.js /
    fav_folders.js：令牌不同代 = 详情页用户继续吃旧缓存，且首页新 JS 与详情页旧 JS 混用，
    正是本文件下方注释里那起「混合缓存 ReferenceError」事故的同一形态。
    """
    html = (ROOT / "web" / "static" / "dataset.html").read_text(encoding="utf-8")
    versions = re.findall(r'/static/(?:css|js)/[^"?]+\?v=([a-zA-Z0-9-]+)', html)
    assert versions, "dataset.html 里找不到任何带 `?v=` 的一方静态资源引用"
    assert set(versions) == {CACHE_GENERATION}, (
        f"dataset.html 的缓存令牌 {sorted(set(versions))} 与首页 {CACHE_GENERATION} 不同代"
    )


def test_every_first_party_asset_reference_carries_a_token() -> None:
    """两页里所有一方 css/js 引用都必须带 `?v=`——漏一个，那个文件就永远吃旧缓存。"""
    for rel in ("web/static/index.html", "web/static/dataset.html"):
        html = (ROOT / rel).read_text(encoding="utf-8")
        naked = re.findall(r'(?:src|href)="(/static/(?:css|js)/[^"?]+)"', html)
        assert not naked, f"{rel} 里这些一方资源引用没带缓存令牌：{naked}"


# ---------------------------------------------------------------- 静态资源 ↔ 缓存令牌
#
# **改了 web/static/{js,css} 的内容，就必须 bump index.html 的 `?v=` 令牌。**
#
# 为什么需要一道「内容指纹」门（2026-07-17 对抗评审抓到的真事故）：本项目原有两道令牌守卫 ——
# `test_onboarding_contract.py` 查「每个引用都带令牌」、上面那条查「所有令牌同一代」——
# 但它们都是**一致性**检查，**不是变更检测**：整轮不 bump，两道全绿。
# 于是那一轮改了 4 个 JS、令牌一动不动就差点合并，后果是：
#   1. `webapp.py` 用裸 `StaticFiles` mount、不发 Cache-Control（实测只有 etag/last-modified）
#      → 浏览器走**启发式**新鲜度 → 回访用户 URL 逐字节相同 → 直接用旧 JS，**修复等于没发布**；
#   2. 更糟：浏览器按**文件**独立淘汰缓存。新 `search.js` + 旧 `progress.js` 的混合缓存下，
#      `search.js` 会调到尚不存在的 `resetSubmitButton` → `ReferenceError`，且该调用在 try 之外
#      → unhandled rejection → 按钮照样卡死。**一个专治按钮卡死的修复反而制造按钮卡死。**
# 开发日志记过两次同类事故（一次 app.css、一次 tabindex，均为「真交付 bug」）。
#
# 这道门用与冻结基准 `FROZEN_BASE_SHA256` 相同的范式：把内容指纹钉成常量。改静态资源 → 指纹变 →
# **本条立刻红**，报错信息直接告诉你「bump 令牌 + 同步这两个常量」。指纹按行尾归一（`\r\n`/`\r` → `\n`）
# 后计算，故对 LF/CRLF checkout 差异免疫。

CACHE_GENERATION = "20260827-web1"  # 2026-08-27 web1（令牌号从 web5 升 20260827-web1、指纹重算）：试用通道换型 GLM-5.3-Flash——shell.js 试用预设模型名兜底 deepseek-v4-flash→glm-5.3-flash + 注释同步（key/url 与 embedding 通道对齐，回落 BIODATA_EMBED_API_KEY；trial 默认不发 thinking 参数）。历史：2026-08-26 web52026-08-26 web5（令牌号从 web4 升 web5、指纹重算）：语料定期更新+追踪自动刷新批前端——删全体批量按钮（project_updates.js 面板/observer/F2 钩子 + sync_button.js/core P4 联动 + index.html 挂点 + dataset.html 两个 script 标签）；单追踪检查向上追溯编排（上游同步→轮询 job→合并回执）；登录后按语料代自动刷新（health corpus.gen 比对 + setHealthArrivedHook/setAccountChangedHook + setWatchesRefreshedHook）；sync_button.js _startSync 识别 202 async 响应轮询 job；core.js API map 加 curateSyncJobStatus。历史：2026-08-26 web4（令牌号从 web3 升 web4、指纹重算）：公网护栏硬化批前端——accounts.js 护栏模式不记/清除一键切换 token 且菜单隐藏切换项；shell.js webGuardOn 统一判定 + 护栏下 base_url 不落请求/「记住api key」不持久化/自定义地址入口隐藏；task_pack.js·act.js 护栏下隐藏真实下载入口只走任务包；usage_upload.js 护栏下跳过 MCP 遥测中继。历史：2026-08-26 web3（令牌号从 web2 升 web3、指纹重算）：设置卡按钮与说明文字间距拉开——usage-setting .btn margin-top 2→12px、memory-setting .btn 补 12px、去掉「向开发者发送意见」内联 8px 统一走类。历史：2026-08-26 web2（令牌号从 web1 升 web2、指纹重算）：账户卡「数据按账号隔离」换行堆叠（.account-box label strong/small display:block，与其它设置卡一致）。历史：2026-08-26 web1（令牌号从 t3ux1 升 web1、全量指纹重算）：网页版设置卡四修——① 排序策略卡按 health.recall_api 显示「智能召回已在线」（shell.js _renderLocalModelStatus 在线分支）；② 使用反馈/训练两条介绍文案精简（index.html）；③ 账户卡精简为用户名+「数据按账号隔离」（index.html/accounts.js）。历史：2026-08-25 t3ux1（令牌号从 t3guard1 升 t3ux1、全量指纹重算）：T3 后续 ux 精简批——① 试用预设隐藏地址/密钥行 + 模型框上屏锁定模型 + 「今日剩余」额度卡（/api/account/trial-quota + app.css .trial-quota）；② 记住设置/记住api key 文案精简两项八字 + 联动；③ 训练采集默认开启（usage_log.js usageTrainingConsentGivenForScope 缺失键=同意，显式 "0" opt-out 优先）。历史：2026-08-25 t3guard1（令牌号从 telexp1 升 t3guard1、全量指纹重算）：T3 账号护栏批——登录锁定 CSS（app.css body.auth-locked）、限量试用 provider 预设与登录门（shell.js/accounts.js）；历史：2026-08-25 telexp1（令牌号从 telcontract1 升 telexp1、全量指纹重算）：默认关闭的确定性排序实验分臂、后端实验回显与完整排序快照；历史：2026-08-25 telcontract1（令牌号从 telpol1 升 telcontract1、全量指纹重算）：合同 v2、独立训练授权与丢弃增量账本；历史：2026-08-25 telpol1：policy_id 结构体/紧凑串双轨。历史：2026-08-25 telbody1：遥测 2MiB 兼容与 413 自动缩包。历史：2026-08-25 ahc1（令牌号从 pill1 升 ahc1、全量指纹重算）：审计批 C——API Key 独立持久化授权，shell.js 变更随本代令牌生效。历史：2026-08-24 pill1（令牌号从 wv1 升 pill1、全量指纹重算）：结果批次 pill 窄气泡溢出修复——app.css `.ft-pill` max-width 240px→min(240px,100%) + min-width:0（气泡 max-width:85% 上限 < 240px 时 pill 跟随气泡收窄、关键词段省略号截断，宽气泡 240px 上限不变）；headless 几何断言窄列 pillRight<bubbleRight、宽列 pillW=240。历史：2026-08-24 wv1（令牌号从 dl2 升 wv1、全量指纹重算）：wvfix 批——桌面壳回归修复（构建 venv 缺 pywebview → PyInstaller modulegraph 静默不打包 → frozen 无壳回退浏览器且回退原因日志丢失：本次 _check_venv 强制探 pywebview + frozen `--shell-probe` fail-closed + spec collect_all("webview")）+ 思考框去除（app.css `.cbh-pending .cbh-sys-bubble.cbh-prog` 补剥 border/box-shadow"）；CSS/令牌随本代生效；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-24 dl2（令牌号从 dl1 升 dl2、全量指纹重算）：dl-auto-1 续批——任务A 混合句「检索+下载」单句自动执行（turn 前端直派面 pack.download）、任务B 环内引文导出自动下载 RIS+BibTeX、任务C 投稿材料自动下载 md/ris/bib；JSP 变更（act.js/reuse_pack.js）随本代令牌生效；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-24 dl1（令牌号从 ku3w7a 升 dl1、全量指纹重算）：一句话下载自动化 + 在途下载管理（dl-auto-1）——pack.download 分级后直接自动开始真实下载（act.js actRunPackDownload 调 task_pack 导出的 tpDownloadStart 内核，不再停确认闸）；运行中允许勾选/取消勾选并点「更新下载」增删条目（task_pack.js running 区差量 + /api/download/update 端点）；JSP 变更（core.js/act.js/task_pack.js）随本代令牌生效；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-24 ku3w6a（令牌号从 ku3w4c 升 ku3w6a、全量指纹重算）：ku3 W6 批（本窗口 W5，与并行窗口 ku3-w5@c441f73 区分）——① narrate 回执去八股化（agent_exec.py _STEPS_REPORT_RULES_ZH：铁律 3 删逐字句式改原则式「0 命中照实直说、结果已更新绝不提保持不变」、铁律 9 清单体改引导式+反自相矛盾条；实证工具执行记录本就在喂 narrate，补钉 test_steps_report_feeds_tool_execution_facts）；② pill 跨轮泄漏修复（batch_select.js 两 display 分支 mergeBatches(currentView…)→mergeBatches([], batches)——本轮回执 pill 严格=本轮批；verify 场景 G 两轮连续对话钉死：轮2 回执仅 0 命中 pill、轮1 历史 pill 原样保留）；③ .rs-strip 下沿与输入条加 10px 间距；④ 空态卡放宽 chips 与选择条文案逐字对齐+契约钉防漂移+状态同步（board.js 导出 closeRescueStrip，results.js applyRelaxation 进预览即收选择条）；⑤ 跨平台：webview_shell.py wintypes 收进 os.name==\"nt\" 守卫、新增 scripts/run_web.sh；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-24 ku3w4c（令牌号从 ku3w4b 升 ku3w4c、全量指纹重算）：ku3 W5 零命中救回 UX 改造（用户 2026-08-24 设计稿）——① search.js 自动救回链整体退役（删 maybeSearchRescue/handleSearchRescue 及 /api/agent/search-rescue 自动调用，救回 sys 气泡灭绝，唯一气泡=诚实回执）；② batch_select.js 新纯函数 isZeroHitBatch/deriveRescueOptions（选项源优先级 relaxation_options→degraded_search→query_constraints 派生→换个说法兜底）/latestActiveBatchId；③ board.js 救回选择条 .rs-strip（贴侧栏继续对话输入框 .cb-bar 上沿——结果态 hero #queryInput 隐藏、可见 composer 在此；选项点击只选中不提交、提交键选中后解禁、右上角叉暂关可经 pill 重开、摘要 ellipsis+title 浮窗）；零命中 pill（.ft-pill--zero 琥珀虚线描边）追加「点击处理」徽标（仅最新结果的零命中批渲染；非最新点击无任何反应连换批也不响应）；④ 审美复核四修（explore agent-532 截图复核 4 项 PASS 后打磨）：提示文案去孤儿词、「点击处理」徽标实底改描边弱化（不与提交键争层级）；verify_ku3w3.py 场景 A/E 改断言+新增场景 F（选择条四步交互）六场景全过；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-24 ku3w4b（令牌号从 ku3w4a 升 ku3w4b、全量指纹重算）：ku3 W4 过夜批后端+择优闸段——① 后端 search.rerun 废「改空拒」（agent_exec.py：0 命中照常采纳产上屏批、披露句如实；拒绝只剩同集与条件丢失两档，后者文案改诚实「没有执行」）；narrate prompt 增铁律 9（汇报说清命中关键词+检索排序手段）与铁律 10（重检/择优/采纳/救回/闸/批次黑话禁令）；静态回执族与 webapp.py 检索救回文案去黑话；prompts/loop_search.md 明示换条件重查 0 命中也采纳；② 前端 batch_select.js alternate 档拆分——条件变更批（scope_fingerprint 不同）mode=display 整屏覆盖含 0 命中（recordKeyVector stable=false 不再拦截），排序层择优只留同 scope 重检批，ALTERNATE_SYS_TEXT 退役为「这次重检没有得出更优结果，当前结果保持不变。」只留同 scope 较弱档；act.js 清 rewrite_empty_kept_original 死分支；③ flow_trace.js shouldDiscardOutcome 空批 supersede 判据定稿为 kind 基（query_raw 一轮内全同值不可用——胜者属 re-search 档 search_rerun/rescue/rerank 且败者是 preliminary 或同档 → 0 命中也撤旧批含 pill；多意图独立 rank 批跨 query 保留）；verify_ku3w3.py 场景 A 改条件变更 0 命中换屏+旧 pill 撤下断言、新增场景 E（多意图+0 命中 active 跨意图 pill 保留）；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-24 ku3w4a（令牌号从 ku3w3c 升 ku3w4a、全量指纹重算）：ku3 W4 过夜批——① 缺陷0 工具行重复且 pending 不落定修复（flow_trace.js：stage id 改 label 基「tool:+展示名」让 tool_start 与完成帧天然同 id；route_consensus/understand/decide/validate/repair/narrate 等 LLM 结构节点不入轨迹行；完成帧丢 verb 时沿用 tool_start 捕获的真 verb，压缩计数不再误落「本地处理」）；② 结果 pill 文案从原始 query 改命中关键词（board.js flowHitKeywords 取 batch payload query_constraints 的 include 值顿号连接、>40 截断、拿不到回退原 query）且 pill 从气泡外挪进回执气泡内部（文字下方，app.css 补 .cbh-sys-bubble .ft-pills 内边距适配）；③ 新手教程 13→14 步：新增「我的库」介绍步（target #libNav），MCP/skill 步从第 3 屏后移到第 12 屏并精简（产品动机：MCP 绕开 react 侧数据采集，不主动推）；④ 遥测协议告知窗文案浓缩（index.html consentModal 四段→两段，只留传什么/不传什么/怎么关闭，完整口径仍在设置页采集卡片）；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-24 ku3w3c（令牌号从 ku3w3b 升 ku3w3c、全量指纹重算）：ku3 W3 真机审美复核四修——① 结果头部件 #batchBar 批次 pill 切换器整体退役（用户手写定稿「检索结果页不再展示该 pill」，renderBatchSwitcher 恒隐藏，切换入口唯一化到对话流 .ft-pill）；② ft-pill 点击换批后 is-on 与结果区同步（cbHistoryClick 更新 entry.pills 活跃标并重渲，此前结果区已换批、对话流还亮旧批）；③ _applyBatchDecision display 分支 _a 补 sayText:text（say 恒记用户原话，缺省时 search.js 回落 query=活跃批检索词，把用户说的话换成检索词）；④ 双窗口（导航卡+侧栏工作卡并立）时左上导航恒折叠两列（board.js swSync 按 #sideWork 可见性 toggle body.side-duo，app.css 两列规则改 body.facets-active/body.side-duo 并集——此前只认 facets-active，无活跃分面的继续对话窗口态导航傻大单列）；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-24 ku3w3b（令牌号从 ku3w3 升 ku3w3b、全量指纹重算）：ku3 W3 返工收尾——① board.js flowPushEvent/flowPushStage 补 cbRenderHistory 实时渲染（无 preliminary 的纯工具流中流工具行此前永不上屏，真机场景 C 复现）；② _applyBatchDecision display 分支 _run 返回 promise（此前 _run() 返回 undefined，.then 抛 TypeError 吞掉 dispatchAction）；③ 第三波入代：upsertStage 调用计数 n（同 id 落定后再来=n+1，压缩句按真调用数加和）、cbPushCurrent 落地合成结果 pill（多批 _flowPillsFrom(result_batches,active,false)/legacy 单批 frameId pill）、_applyBatchDecision dedupe/alternate 分支 pill 数据源从恒 null 的 decision.view 改 decision.mergedBatches+activeBatchId、pill 渲染加 data-ft-frame 且 cbHistoryClick 无 bid 时 cbViewFrame 兜底；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-24 ku3w3（令牌号从 ku3w2 升 ku3w3、全量指纹重算）：ku3 W3 信息流结构纠偏（用户 2026-08-24 重申三段结构：上工具行/中唯一气泡/下结果 pill）——① flow_trace.js 整体重写为一工具一行纯核（KIND_TOOL/KIND_RESULT 两类，route_consensus 等分流元事件除名、preliminary=初步检索 rank 行、tool_start/step 同 id 天然去重、行无 detail；compressFlow 改按 FLOW_TOOL_KIND 平铺表计数加和「执行了 1 次检索，1 次联网搜索。」失败补「（N 次失败）」；新增 flowVerbLabel 非流式合成行展示名表）；② board.js——压缩快照与结果 pill 改挂回执 entry（_flowAttach/_flowPills 持件、cbLogPush("sys") 领取），轨迹块渲染在气泡上方、pill（.ft-pill 点击 switchBatch 换批）在气泡下方、全局 ft-trace 只活在流式在途；ubDispatch 删「已完成分流」行；流式 SSE 回调不再喂行动流（非工具节点+detail 上屏是用户点名冗余，过程展示全归信息流工具行，真实执行的 arx 由 actDispatchPlan 自开）；cbPushCurrent 退役 execSummary 改非流式合成工具行+flowFinish；_applyBatchDecision 唯一气泡规则（纯检索计划置 _execReceiptCovered 由批次回执扛唯一气泡、混合计划归 actFinish）；③ act.js——actFinish 纯检索计划+已认领时抑制第二颗气泡（保守例外：撤回/专项卡/取消照出），execSummary 通道与 act_core.actToolSummary/ACT_TOOL_KIND 整体退役（计数口径归 flow_trace 平铺表）；④ app.css 新增 .ft-pills/.ft-pill（.batch-pill 小号同族）+ .cbh-exec-summary/.ft-detail 死样式清除；⑤ 测试——flow_trace_spec.mjs 按新语义重写（35 断言）、test_act_frontend 两处 ux1 pinning 改钉 ku3-w3 语义；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-23 ku3w2（令牌号从 ku3w1 升 ku3w2、全量指纹重算）：ku3 W2 视觉层——思考行重定（用户 2026-08-24：模型运作指示挪到**流式输出最底部**+做漂亮：board.js 渲染序 turns→行动流→轨迹→思考行、去气泡化无框小字、文案微光扫过 cbhShimmer、三点 accent 弹跳、一次性 cbh-prog-in 入场；morph/enter 目标显式排除 .cbh-pending/.ft-trace/.arx-turn 尾部块防错播）、app.css 新增 .ft-* 全套无框轨迹样式（运行中微脉冲点/完成 ✓/失败琥珀 ✗）、压缩落帧摘要原地淡入动画（board.js 一次性 ft-compressed 旗标，渲染即消费，逻辑零改动）、.ft-summary 灰字摘要 details 展开/收起过渡（::details-content + interpolate-size，不支持则瞬时开合）；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-23 ku3w1（令牌号从 ku2w4 升 ku3w1、全量指纹重算）：ku3「信息流重构」W1=逻辑层（纯工程）——① 新增 web/static/js/core/flow_trace.js 纯核（事件→阶段映射 stageFromEvent、阶段状态机去重 upsertStage【同 id 更新不 append，修重复消息】、压缩 compressFlow【逐阶段映射：prelim/route/research 痕迹进灰字摘要、result 保留为 pill，只减不增】、覆盖丢弃 shouldDiscardOutcome【supersede 即丢弃，连存储也丢】）；② board.js 信息流结构改动——流事件（preliminary/tool_start/step）经 flowPushEvent 记轨迹、ubDispatch 分流钩子 mechanical 触发「已完成分流」+ 落地即压缩 flowFinish、cbRenderHistory 尾部新增 .ft-trace 过程轨迹块（展开态无框小字 / 终态灰字摘要，结构与 CSS 钩子留给 W2）、_applyBatchDecision 落地前 _discardSuperseded 剔被覆盖批（weak 批不再作备选 pill，ALTERNATE_SYS_TEXT 改如实不变句）；③ package.json / 两页 HTML importmap 加 #flow_trace；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-23 ku2w4（令牌号从 ku2w3 升 ku2w4、全量指纹重算）：ku2 收口视觉核验修复批——app.css .ctx-pop 补 text-align:left（hero 主框 chip popover 继承 hero 居中排版、多行预览整段居中，与侧栏 popover 左对齐不一致）+ projects.js _renderCtxMain 主框 chip 在场时暂隐 placeholder（首行 text-indent 挤压使长示例文案折行被单行 textarea 裁出半截灰字，移除后恢复）；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-23 ku2w3（令牌号从 ku2w2 升 ku2w3、全量指纹重算）：docs/文案顺位批——帮助页「接入 AI 助手」块前移至「重新打开新手教程」之后、置于「怎么用」之前（index.html 纯 DOM 移动，按钮 id agentPromptCopyBtn 与 interactions.js copyAgentPrompt 接线不变），新手教程「接进你自己的 AI 助手」步前移至第 3 屏（onboarding.js 数组顺序，target/visual/size 与「这一步完全可选，跳过不影响任何功能」措辞内核不变），使用说明书（docs/使用说明书/使用说明书.html）同步 ku2 行为与上述顺位；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-23 ku2w2（令牌号从 ku2w1 升 ku2w2、全量指纹重算）：上下文 chip 双输入框返工（追踪/收藏四组合、发送即清、侧栏小圆徽章 + hover popover）与追踪卡片候选徽标/顶部导出视觉修复；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-23 ku2w1（令牌号从 ku1w4 升 ku2w1、全量指纹重算）：ku2「批量结果展示残留」批——① 检索结果重复/备选 pill 点不动：batch_select.js 参考批或候选批缺 batch_id 时 activeBatchId 塌成 ""（加 _activeBatchIdFor 按归一 id 解析，杜绝「两枚 pill 都标规则排序、无一枚 is-on」的高亮错位与 switchBatch 空转）；results.js renderBatchSwitcher 跳过 payload.ok===false 的死批 + 按归一 id 判 is-on，switchBatch 改比归一 id（批缺 id 不再被误判「已在这批」而空转，pill 恢复可点可切回）；② 追踪「检查更新」后 候选/待核验 计数不动：语义上检查本就不改候选表（绝不自动纳入），问题在回执没说清——project_updates_core 新增 watchReceiptText 如实说「检查了 N 条，X 条有更新，Y 条已是最新」，project_updates.js 回执补「候选表未自动改变，更新需逐条纳入候选」；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-23 ku1w4（令牌号从 ku1w2 升 ku1w4、全量指纹重算）：ku1「我的库」批次 W4 视觉核验修复批——app.css .tag 长标签 nowrap+溢出裁切（长疾病串在 24px 钉高芯片换行溢出，fx2 前旧疾）+ .ra-btn.armed 删 ::after 文案修双行「再点确认」重影、projects.js 行「检查更新」完成后即时重渲（时间戳/徽章不再要重开窗才刷新）、fav_folders.js 收藏「更新」load_error 先拉目录再重查（修「目录未加载」死路）+ 指纹 raw_data_status 走 fastqInfo 语义口径（修 emoji 串误报「已更新」）+ 指纹变时目录新值落回收藏记录、browse.js 注册 setCatalogEnsure；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-23 ku1w1（令牌号从 fx1 升 ku1w1、全量指纹重算）：ku1「我的库」批次 W1=任务 9 结果覆盖策略修复——环内 rank/rerank 吃结构化检索现场并 fail-closed（agent_exec.py）+ turn 批次组卷加 scope_fingerprint（规范化检索范围指纹）+ 前端新 core/batch_select.js 纯核（selectDisplayBatch/rankingLevel，search a 档与 route=tool 档共用）+ results.js 非活动备选批排序层标注；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-22 fx1（令牌号从 eng1 升 fx1、全量指纹重算）：结果区头部重设计（.res-overview 概览卡/批次 segmented 控件化/审核横幅+摘要卡去盒化/覆盖缺口虚线分隔琥珀点/.res-notes 诚实行组 :has 归隐/.res-next 下一步组，ladderNarrow 下移）+ 侧栏非查询视图单列填满修复（body:not(.on-query) 覆盖 facets-active 跨视图残留）+ 阶梯 chips 胶囊化与两颗 chip 文案精简 + 结果头三钮 .bt-full 文案精简（eng1 同代 CSS 修复随之送达）；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-22 eng1（令牌号从 ad1 升 eng1、全量指纹重算）：engagement 落地包全波次合入（F1 课题体系/F2 下一步行动阶梯+任务卡/P5 课题导出中心/P4 更新检查闭环/F2 数据集同步按钮/B3 意见反馈+生产公钥填入 feedback_core.js）+ Wave3 新手教程文案与版本 2.5.0/1.34.0；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-22 ad1（令牌号从 ov1 升 ad1、全量指纹重算）：ov1-adapt1 自适应上传阈值——/v1/ingest 200 响应 additive 注入 server_hint（pressure 三信号取 max→离散档 <0.3→2条/30s、0.3–0.7→5条/120s、≥0.7→20条/300s），前端默认阈值 10→2、hint 采用/钳制[2,50]条·[15s,10min]/持久化 meta、429 临时升档、老接收端 fail-safe 不动；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-22 ov1（令牌号从 om3 升 ov1、全量指纹重算）：ov1 过夜改造批 + 修复批（fix1a-d）+ 集成修复 fix2 合入——遥测 schema v3（ImpressionContext 卡级归因/imp/label 事件/label 载荷带 recId）、激进上传（1 轮/10 条/5min 定时/启动/pagehide，3min 最小间隔、since_ts 过滤墙空页 ack 推进）、consent 文案 v2 如实化（尽力过滤·并非匿名化·明文 HTTP）、混合 query 机械意图闸 v2（子句级+能力账+保底弃权）、帮助页「接入 AI 助手」块；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-21 om3（令牌号从 om2 升 om3、全量指纹重算，批号 ux1）：新手教程第 5 屏服务商按钮接通右侧真实表单（点按钮设 #cfgProvider + dispatch change → applyPreset 填地址/模型、同步 aria-pressed、焦点移 API Key），第 10 屏详情页实拍图加缓存令牌与加载失败文字兜底（六子标签）；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-21 om2（令牌号从 om1 升 om2、全量指纹重算）：本地模型体积文案按真实安装实测校准——统一口径「约下载 3 GB（模型 2.2 GB + 运行组件约 1 GB），安装后约占 5 GB」（实测 venv 849MB + uv 缓存 845MB + 权重 2.2GB ≈ 4.0GB）；同步前端设置页/确认框、Inno task、便携版提示与三处契约测试；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-21 om1（令牌号从 ob3 升 om1、全量指纹重算）：新手教程 13→12 步——第 2 屏「不只找数据：一句话交代整件事」前置复杂任务心智、第 5 屏真实 API 配置表单（默认仅本次会话）、第 6 屏排序说明；设置页新增本地模型在线安装行（状态/安装/取消/进度五态）；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-21 ob3（令牌号从 tc1 升 ob3、全量指纹重算）：新手教程 ReAct 化——在「出结果后可以接着改条件」与「使用反馈」之间插入两步：「复杂任务，一句话交代」（三条可直接抄的示例 query：pack.download 执行类 / curate.sync_updates 联网管护类 / rank→compare.datasets→cite.export 多步链，对照 VERB_SPECS 核实）+「尽管发难的、偏的、多步的」（鼓励挑战能力边界 + 诚实边界「不假装成功」+ 打分呼应 step0，收尾句按 BENCHFB_BUILD 分叉），步骤数 11→13，新增 react 教程视觉；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-21 tc1（令牌号从 a5 升 tc1、全量指纹重算）：遥测并发/多用户修复——per-profile consent、独立事件键与跨标签页 single-flight、captured scope 精确 ACK、packet/event 幂等、HTTPS 配置 fail-closed、receiver async DB/CORS/双层限流；STATIC_ASSETS_SHA256 重算。tc1 合并（kimi-tc1-merge-m1）追加 allow-insecure 显式白名单：usage_upload.js 对非 loopback 明文公网 HTTP 增加 meta `biodata-telemetry-allow-insecure`（逗号分隔主机）fail-closed 判定、两页 meta 增默认空声明、契约/Node 规格补钉（17/17），文档补风险说明；代际令牌保持 tc1（首次发布，无历史缓存），STATIC_ASSETS_SHA256 按合并后工作树重算。2026-08-21 a5（令牌号从 af1 升 a5、全量指纹重算）：设置「使用反馈」采集卡片文案收敛：移除 #usageHint/#benchfbHint 动态状态行及对应写入逻辑，卡片收敛为标题、介绍、开关和导出按钮；同步清理死样式与视觉回归遮罩；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-21 af1（令牌号从 tl2 升 af1、全量指纹重算）：审计修复批 af1（CLM-20260821-0530-zcode-audit-af1）前端两处——act.js site_url「打开官网核对」链接补 isHttp 门禁（全站最后一个无协议校验的 href sink：escapeHtml 只封引号不封协议，拦 javascript: 伪协议，对齐 reuse_pack.js 同类守卫）、interactions.js 浏览页搜索框 250ms trailing debounce（此前每击键两次全库扫描+时间线 DOM 重建；参照本文件识别预览 500ms debounce 先例）；STATIC_ASSETS_SHA256 按当前工作树重算。2026-08-20 tl2（令牌号从 tl1 升 tl2、全量指纹重算）：激活 ping 批（p1）——consent 同意即发一次性 hello 包：usage_upload.js 加 sendActivationPing（双重门控、HTTP 200 后写 biodata_ping_sent_v1=1 一次性、失败静默下次触发点重试；触发点：同意落盘后 fire-and-forget + maybeUploadUsage 启动路径补发覆盖「旧版本同意过、升级到带 ping 版本」的机器；k:"hello" 走 buildTelemetryPackage 构造函数——无 kind 白名单）；core.js LS 键表登记 pingSent；契约测试补 ping 一次性/双重门控/唯一出网通道断言；e2e 快验加 hello 包断言；STATIC_ASSETS_SHA256 重算（32 个一方静态文件）。2026-08-20 tl1（令牌号从 ob3 升 tl1、全量指纹重算）：tl1 批统一收口（T2）——consent 首次弹窗拦截/评分卡缩小并按高收益处收敛/none·error 轮「评价」按钮/输入框随行自动伸展/行为埋点自动采集+阈值自动上传+手动导出兜底/单版本化等全部前端与测试改动落地后，由 T2 统一 bump 缓存代；STATIC_ASSETS_SHA256 按当前工作树重算（32 个一方静态文件：1 css + 31 js）。2026-08-19 ob3（令牌号从 ob2 升 ob3、全量指纹重算）：打包面板旧措辞收尾统一（ob3 批）——bl1 改「下载这批数据」后残留的两处旧名收尾：act.js 回执 panel chip「打开打包面板自己挑」→「打开下载面板自己挑」、actRunPackPreview 降级 extra 同改 + 注释；board.js 降级回音兜底「已打开打包面板」→「已打开下载面板」（两处默认值）+ 注释；指任务包 zip 产物本身的「任务包/打包」措辞一律保留；测试同步 test_act_frontend.py 硬钉断言、test_action_markers.py / test_action_plan.py / test_board_honesty.py 注释、smoke_packchips.py 标签与文档串、_mainline_surgery.py 两处 act.js 锚点、docs/agent/FRONTEND.md 描述；说明书 html 两处第 0 步旧句转述（「绝不自动发送」为旧终稿用语）改为与 onboarding.js 现行终稿一致（默认记录用户消息/执行结果/评分 + 点「导出反馈包」手动发 .json 程序包），PDF 已重印（40 页）；_ob3_check/ 快验截图（已入 .gitignore）。2026-08-19 ob2（令牌号从 bl1 升 ob2、全量指纹重算）：新手教程第0步反馈强化版终稿（ob2 批）——标题「你的使用，在帮它变好」渲染成主题绿色（app.css 新增 .onboarding-copy h3.onboarding-title-accent，color: var(--tour-accent)=全局 --accent，不硬编码色值）、正文逐字替换为「这个版本默认记录用户消息、执行结果和你的评分 … 导出的.json程序包手动发给开发者」并整体加粗（.onboarding-copy > p.onboarding-text-bold { font-weight: 700 }）；onboarding.js 第0步对象加 additive 字段 titleAccent/boldText（= BENCHFB_BUILD，仅 BENCHFB_BUILD=true 分支生效，主线版 false 分支文案视觉一律不动），showOnboardingStep 按 classList.toggle 挂类（其余步骤字段缺省强拆类，视觉不变）；tests/test_onboarding_contract.py 新增第0步 true 分支终稿静态断言；_ob2_check/ 快验脚本与截图（已入 .gitignore）。2026-08-19 bl1（令牌号从 dl2 升 bl1、全量指纹重算）：打包下载入口 chip 文案对齐「真实下载优先」（bl1 批）——结果区右上入口按钮 #taskPackBtn 从「📦 打包这批数据（清单、下载脚本、引文）」改「📦 下载这批数据（真实文件 · 清单脚本 · 引文）」（title/bt-full/bt-short「📦 下载」同改），任务包面板标题「打包这批数据」→「下载这批数据」；附属文案同步：results.js 空态指路句改「可以直接下载真实文件，也可一次生成清单、下载脚本、FAIR 自检与引文」+ 注释、task_pack.js 注释、app.css 注释、download_script.py 00-START-HERE 第 2 步「下载这批数据」面板名、MODULES.md/使用说明书 引用；测试同步 test_frontend_viewswap.py 硬钉（bt-full/bt-short 文案）、test_hidden_attribute_contract.py、smoke_packchips.py 标签；视觉基线 8+1 张重录（结果区 chip 文案/下载分区/评分卡/教程步数/四工具卡等今晚各批改）。2026-08-19 dl2（令牌号从 cd2 升 dl2、全量指纹重算）：真实数据下载前端 UX（dl2 批）——「打包下载」升级为「真实数据下载优先」：task_pack.js 新增下载分区（/api/download/plan 分级「N 个可直接下载 · M 个暂不支持（reason 收 details）」→ 主按钮「直接下载真实数据」→ 确认条 → start → 1s 轮询 → 进度「正在下载 a/b 个文件 · x.x/y.y GB」+ 取消 → 终态 done/cancelled/error 三套诚实摘要，md5_mismatch/skipped/error 逐条列出；次按钮「仍生成任务包」走既有 zip 链兜底；无 supported 时诚实降级；409 提示「有下载任务进行中」+ 查看进度回跳）；act.js actRunPackDownload 真实下载优先分流；core.js API 集中声明 downloadPlan/downloadStart/downloadStatus/downloadCancel；B 文案修正（「约 X 下载量」→「真实数据共约 X」）；search.js 缓存命中分支 try/finally 收尾（渲染抛错也复位按钮，修 loading 卡死 + 在途闸拦输入）；app.css +.tp-dl-*；download_manager.start_job 先预检后建目录（修 507 留空目录）+ 测试回归；任务包内容按数据集分组 + 00-START-HERE 小白三步。2026-08-19 cd2（令牌号从 cd1 升 cd2、全量指纹重算）：组合轮（检索+工具）cite 卡静默丢失修复——act.js 从 #act_core 的 import 漏了 tpBytes（actCiteExportCardHtml 在 files 非空时调用它 → 浏览器 ReferenceError，被 ubDispatchAction 的 .catch 静默吞掉，组合轮与纯工具轮的 cite 卡都不上屏、行动流留在屏上），补 import 一行；tests/test_act_frontend.py 新增 import 结构门（#act_core import 必须含 tpBytes）+ 组合轮分发真行为门（plan.steps=[rank,cite_export] 走 actLoopStepCardHtml 断言 cite 卡非空含下载链），_card_in_node 的 tpBytes 手桩改抽 act_core 真函数（不再掩盖缺失 import）；_cd2_check/ 桩验证（page.route 桩 /api/utterance 回放组合轮 SSE，断言卡上屏+下载链+结果区+hero 评分卡，纯工具轮不回归）10/10 通过。2026-08-19 cd1（令牌号从 fb1 升 cd1、全量指纹重算）：环内四工具前端专项卡 + cite.export 浏览器下载接线——act.js 新增 actCompareCardHtml/actCiteExportCardHtml/actCompatFindCardHtml/actFairCheckCardHtml 四张专项卡（一卡一工具、核心信息 + <details> 明细；degraded=true 只上诚实降级句不渲染空表格），actLoopStepCardHtml 补四种 card_kind 分发，actDispatchPlan 图内通道按执行顺序收集卡经 actFinish 的 html 通道上屏 .cbh-sys-extra（entry.html 是重画真源，历史重画随 html 恢复）；core.js API 集中声明 citationsDownload（/api/citations/download）；后端 webapp.py 新增 GET /api/citations/download（additive：入参 f 只接受裸文件名、路径分隔符/.. 拒绝、resolve 后须在 .userdata/citations/ 内、不存在 404、Content-Disposition attachment）；app.css +.arx-cmp-*/.arx-fair-*/.arx-card-details；DEVELOPMENT.md §6 路由表 38→39、FRONTEND.md/ARCHITECTURE_AND_CONTRACTS.md/MODULES.md 端点登记同步；测试：tests/test_act_frontend.py 四卡真行为门（node 桩跑构造函数）、tests/test_api_citations_download.py 新端点 pytest、web_smoke 加端点 token。2026-08-19 fb1（令牌号从 tu1 升 fb1、全量指纹重算）：benchfb 评分卡从「全局一张卡」改「每次 query 一张独立评分卡」——benchfb.js 删 _promptId 单值，每轮收尾（hero 检索/对话检索/工具执行）生成绑定各自 rec.id 的卡：对话轮经 benchfbOnChatEntry 通知 board 把 id 贴到本轮系统回复 entry（bfRecId），cbRenderHistory 在 entry 下渲染 [data-bf-mount] 挂载点、benchfbAfterRender 填回；hero 轮卡挂结果区顶部槽位（results.js renderResults 尾部调 benchfbAfterSearchRender 重建）；「标出有用条目」只对最新一次检索的卡可见（_lastSearchRecId）；标注名次统一走 _gridCards 跳过槽位防错位；app.css +.bf-mount:empty/.bf-hero-mount；FRONTEND.md §7/§3c 同步。2026-08-19 tu1（令牌号从 cap1 升 tu1、全量指纹重算）：教程新增第0步——onboarding.js ONBOARDING_STEPS 头部插入「使用反馈承诺 + 高质量查询引导」步（target #queryInput / visual "" / size medium，文案按 BENCHFB_BUILD 分叉：强化版「你的使用，在帮它变好」引导高难度 query + 随手打分，主线版「尽管用真需求考验它」能力总览），index.html 进度占位 1/10→1/11、web_smoke/onboarding 契约计数同步 10→11。2026-08-19 cap1（令牌号从 ux1 升 cap1、全量指纹重算）：环内四工具批前端收口——act_core.js ACT_TOOL_KIND 补 compare.datasets→「对比」/ compat.find→「检索」/ fair.check→「自检」（cite.export 沿用「下载打包」；环内 plan.steps 全量 verb 已进 actToolSummary 摘要）；index.html 第三颗样例 chip「Xenium 人类乳腺癌」→「乳腺癌数据，导出 BibTeX 引文」（检索 + cite.export 导出 RIS/BibTeX 双格式引文，真机实测稳定走通）。2026-08-18 ux1：执行披露精简 + 「查看历史回复」误显示修复——act_core.actToolSummary 工具调用摘要统计（verb→类别映射、只报非零、零调用空串）；act.js actSummaryHtml 只留功能钮、actFinish 带 execSummary、actDispatchPlan 图内通道去卡片/策略行；board.js cbLogPush/cbProgressDone/cbPushCurrent 支持 execSummary、cbRenderHistory 渲染 .cbh-exec-summary；ubDispatch search a/c 档留痕与 tool 档派发延后到 runRecommend 落地后（修 sys 挂 preliminary 先行帧导致的误显示）；search.js landRecommendResult 透传 toolVerbs；app.css +.cbh-exec-summary。2026-08-18 ux2（令牌号沿用 ux1、仅指纹重算）：数字百分比进度全面退役改不确定态加载——progress.js 里程表机器（_setPct/_pctVal/_pctRate/rAF）移除、startProgress/finishProgress/resetSubmitButton 纯 loading 态开关；board.js 进度泡去数字列（#cbProgPct 只留流式 label）、#cbLivePct 改三点续滚；app.css 摘 .cbh-live-pct/cbhLivePulse/残留 .sb-od、+.cbh-live-dots。对话区字号统一 12px 基准（.chat-stage .cb-history 13.5→12、.arx-step 12.5→12、.chat-stage .cbh-view-link 12→11.5）；hero 提交按钮「检索」→「发送」+ 放大镜换纸飞机；chat-in-main 输入统一为圆角长条 #chatComposer（placeChatLog 搬进 hero、placeScopeControls 范围控件随迁、app.css 桌面对话态隐藏 hero console）。2026-08-18 hc2：删 sourcesCaption 按钮（hero 库覆盖信息条退役，chips 直贴输入框）+ hero 问候整句统一样式（index.html h1 去 span.grad、整句纯文本兜底，interactions.js renderHeroGreeting 同步不再挂 .grad；app.css 摘除 .sources-caption 与 .hero h1 .grad 规则，visual_regression.py setup_home 改等静态 chips）；2026-08-18 hc1：问候式 hero（用户 2026-08-18）——index.html h1 从「用一句话，找到对的数据集」改为按时段情景问候（interactions.js 新增 renderHeroGreeting，boot.js init 在 playHero 前调用；HTML 留静态兜底），副标题改为能力清单（对话式检索/初步分析/下载/维护数据库 + 推荐理由与编号/DOI 直达保留），sourcesCaption 从 console 下方挪到 chips 下方（chips 紧贴输入框成动作簇，库覆盖信息作页脚级背书）；2026-08-18 mb7：rescue 去重门禁（评估迁移步骤 1）——utterance final 帧 plan.steps 含 search.rerun/rerank 步（不论成败）即跳过 /api/agent/search-rescue 补发，消除同一查询环内+端点双跑两份 LLM 账单；同批 a 档换屏留痕优先读活跃批随行的 disclosure_zh（rescue2 披露句移植进环，无则回退通用句）；board.js ubDispatch search a/c 与 tool 三档把 plan.steps 透传 runRecommend→landRecommendResult→maybeSearchRescue（并列第四闸，防刷屏三件套原语义不变；被机械闸拒掉的步也在 steps 实录）；2026-08-18 mb6：8项对抗修复前端收口——search.js 的 rescue POST 与 /api/recommend 同源透传日期/分面/抑制/宽容且采纳换屏不清状态；act.js rollback 卡片按 rolled_back/note_zh/真实文件数区分拒绝与成功，保留 mb5 空回执措辞；results.js 区分「改写后重检/换词重检」并对剩余撞名稳定补 ·2/·3；2026-08-17 mb5：fp1 前端两处修正——results.js 批次切换器 pill label 撞名修复（初步批 label=本轮原话、rank 批 label=rank query，同句两轮出现两枚同名 pill 无法区分；渲染时先数撞名，撞名 pill 按 batch kind 加来源前缀 初步·/新检索·/重检·/救回·，kind 口径同后端 turn.py/agent_exec.py，不撞名一字不动）；act.js 环内 display=false 探测步兜底回执改自然表述（「这一步已经跑完，没有需要展示的内容。」——仍完全如实，不暗示有结果被藏起来）；2026-08-17 mb4：前端三处注释摘除已删 flag 字样（index.html 批次切换器 HTML 注释、results.js mb1 块注释、board.js _mirrorPrelim 判别括号注）——纯注释无行为变化，令牌照纪律 bump；2026-08-17 mb3：board.js 注释摘除已删 flag 字样（AGENT_MULTI_BATCH 已随 nl1 摘除）——纯注释无行为变化，令牌照纪律 bump；2026-08-17 mb2：M3 波2 跨契约缺陷修复——ubDispatch 的 preliminary_final 分支前置到通用 result_payload 分支之前（后端 M3 批次常驻且仅 preliminary 批时 legacy result_payload 也非 None（镜像活跃批，turn.py 组卷收尾），a 档先判会对屏上同一批初步结果重复上屏并误报「已更新」；preliminary_final 由独立 loop_payload 哨兵推导，不误压真正的环内采纳结果；同批补 tool 档消费（真链路核实缺口）——三 flag ON 时检索由环内 rank/rerank 工具完成、整轮 route=tool（EXEC plan），ubDispatch tool 分支现消费 result_payload（环内上屏批）+result_batches/active_batch：仅 preliminary 批且先行帧已落地只摘徽标不重复上屏，否则 prefetched 落地活跃批视图（批次切换器对真实多批响应渲染），修复「换条件一轮后屏上仍是上一轮结果 + 初步徽标残留」；search 档补镜像判别与多批并入（活跃批 kind=preliminary 且先行帧在屏 → result_payload 只是该批镜像而非环内采纳，a 档不得截胡、落 c 档润色照跑；a 档真采纳落地并入 result_batches/active_batch 供切换器渲染）——search 档 a/b/c 经注入帧 fixture 逐档钉死；2026-08-17 mb1：M3 多批检索结果前端——批次切换器（后端 M3 批次常驻时响应带 result_batches+active_batch，>1 批在 #resultsHead 内 #batchBar 渲染 pill 组、随视图交换两态搬家；切换纯前端经 applyRecommendResult 重渲该批 payload（标题行两钮/摘要卡/诚实回显条/条件板随批走），不发网络、不推历史帧、不动输入框、不过救回门禁；缺失/≤1 批恒 hidden=回退；切换动画 ≈200ms 淡入微移同款）；2026-08-16 pl1b：preliminary_final b 档解锁——searchParamSnapshot 加 polish（/api/recommend 与 ubRouteBody 同源第十参，后端据「AI 润色会不会跑」判 b 档；ubRouteBody 注释同步）；2026-08-16 prelim1：初步结果先行 + 信息流升级前端（ubRouteBody 携带当前检索参数（top_k/rerank/recall/strategy/分面/抑制/宽容/时间窗）与 /api/recommend 同源构造（searchParamSnapshot）；SSE 新增 preliminary/tool_start 事件分派——pre-loop 先行结果经共享入口 landRecommendResult(fromPrelim) 落地 + 「初步结果」徽标（#prelimBadge，viewswap 两态随 #resultsHead 可见）+ 进度泡换句「正在更深一步思考…」+ recSeqNow 代际闸；final 三档：环内采纳 result_payload 直接换屏（prefetched，淡入同款 firstReveal 参数 + sys 留痕）/ preliminary_final 免二次检索摘徽标收尾 / 现状 runRecommend；行动流 tool_start 即时 pending running 行、完成帧按 label 改行不落新行（arxSettlePending）+ 头部 ≥2s 秒表与收尾「· 用时 Ns · N 步」）；2026-08-16 sr1：检索工具化 Phase 2 前端（零命中救回：landRecommendResult 共享落地入口 + /api/agent/search-rescue 门禁触发 + seq 双守卫 + sys 三档留痕；act.js search_rerun 步骤卡只出摘要不换屏；search.rerun 前端 runner 永久豁免）；2026-08-16 sum1：摘要卡方法层关键词行内高光（renderResultSummary 层名外包 <mark class="sum-layer">，荧光笔式下半截底色，交换态 300px 窄栏不破行；润色附注句精简为「推荐说明由 AI 润色。」）；2026-08-16 pack1：打包回执 chip 精简（「按原话重新检索」「以后别自动执行」两颗 chip 退役；previewTaskPack/renderTaskPackPlan 不再自动开面板，actRunPackPreview 按 pack.preview/download 分流）；2026-08-16 vs4：视图交换四轮（交换态侧栏结果页签头部件紧凑化 ≤150px：单行标题栏双 span 短文案钮 + 摘要 2 行截断 + 放宽提示折叠单行 + 两卡合并，作用域 body.view-swapped+#sideWork[data-sw-mode=board]）；2026-08-16 vs3：视图交换三轮（交换态检索结果页签隐藏 #swHits + 输入条结构性钉底 flex 一屏布局）；2026-08-16 vs2：视图交换二轮（#resultsHead 头部件随结果进侧栏 + 主区纯聊天去白卡框 880px + grid-mini 恢复数据集详情钮）；2026-08-16 vs1：视图交换批（#swSwapBtn 主区结果网格 ↔ 侧栏对话窗，#chatStage + grid-mini 紧凑卡）；2026-08-15 ta2 frontend2：前端在册未修 8 条 + fail-open benchfb 留痕（M-01~M-08，见 触发点审计-2026-08-15/frontend.md）；2026-08-15 ta1：触发点审计修复批（act.js 零命中真值陷阱 + board.js fail-open 提示）
STATIC_ASSETS_SHA256 = "6f870ba011bd1ef9e9283bd91a7919d75f31ba1218168b461516d6c9fbabf5df"


def _static_assets_digest() -> str:
    """web/static/{css,js} 全部一方资源的内容指纹（行尾归一后按路径排序哈希）。"""
    import hashlib

    h = hashlib.sha256()
    base = ROOT / "web" / "static"
    files = sorted(
        (p for d in ("css", "js") for p in (base / d).rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(base).as_posix(),
    )
    for p in files:
        h.update(p.relative_to(base).as_posix().encode("utf-8"))
        h.update(b"\0")
        body = p.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        h.update(body)
        h.update(b"\0")
    return h.hexdigest()


def test_static_asset_changes_force_a_cache_token_bump() -> None:
    """静态资源内容一变 → 本条红 → 逼你 bump 令牌。这是「变更检测」，上面那条只是「一致性」。"""
    actual = _static_assets_digest()
    assert actual == STATIC_ASSETS_SHA256, (
        "web/static/{css,js} 的内容变了。这意味着你**必须**同时：\n"
        f"  1. 把 web/static/index.html 里全部 `?v=` 令牌从 {CACHE_GENERATION} 换成新代号；\n"
        "  2. 把本文件的 CACHE_GENERATION 改成同一个新代号；\n"
        f"  3. 把 STATIC_ASSETS_SHA256 改成 {actual}\n"
        "不 bump 令牌的后果：回访用户的浏览器按启发式缓存继续跑旧 JS（webapp 不发 Cache-Control），"
        "你的前端修复到不了他们；且新旧 JS 混合缓存会抛 ReferenceError。"
    )
