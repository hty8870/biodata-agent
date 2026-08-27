from __future__ import annotations

import argparse
import os
import socket
import threading
import time
import webbrowser
from pathlib import Path
import sys

import uvicorn

# sys.path 必须锚定**真实源码位置**（本文件上两级 = 仓库根），与运行 cwd 无关；
# PROJECT_ROOT 的运行时语义（install_root）经 runtime_paths 单一真源解析——source/portable
# 下 = 项目根（历史逐字节一致）；frozen 由新启动器接管，本脚本不服务 frozen。
_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from dataset_recommender.app.runtime_paths import get_app_paths  # noqa: E402

PROJECT_ROOT = get_app_paths().install_root

# 公网护栏硬化：护栏模式（BIODATA_REQUIRE_ACCOUNT=1）的启动期 fail-closed 校验。
# 缺任何一项，公网实例都会在降级形态下运行（注册整体关闭、Host 白名单空 = 只剩 loopback、
# 配额默认值未必是部署者本意）。缺省关 → 本机形态零校验零行为变化。
_GUARD_ENV = "BIODATA_REQUIRE_ACCOUNT"
_GUARD_REQUIRED_ENVS = (
    "BIODATA_INVITE_CODE",        # 注册邀请码：护栏模式注册必须有码
    "BIODATA_TRUSTED_HOSTS",      # Host 守卫白名单：公网入口
)
_GUARD_QUOTA_ENVS = (
    "BIODATA_LLM_DAILY_PER_USER",  # 每账号每日 LLM 上限：必须显式给正整数
    "BIODATA_LLM_DAILY_GLOBAL",    # 全局每日 LLM 上限：同上
)


def _validate_guard_config() -> int:
    """护栏模式启动配置校验：缺项打印中文错误到 stderr 并返回 2（不起服务）；配置齐或闸关返回 0。

    独立可调用（不起 uvicorn、不预热），供单元测试直接覆盖 env 组合矩阵。"""
    if os.getenv(_GUARD_ENV, "").strip().lower() not in ("1", "true", "yes", "on"):
        return 0
    problems = []
    for name in _GUARD_REQUIRED_ENVS:
        if not os.getenv(name, "").strip():
            problems.append(f"{name} 未设置或为空")
    for name in _GUARD_QUOTA_ENVS:
        raw = os.getenv(name, "").strip()
        try:
            ok = bool(raw) and int(raw) > 0
        except ValueError:
            ok = False
        if not ok:
            problems.append(f"{name} 必须显式设置为正整数（当前值 {raw!r}）")
    if not problems:
        return 0
    print("[startup] 已开启账号护栏（BIODATA_REQUIRE_ACCOUNT=1），但公网必需配置缺失/非法，拒绝启动：",
          file=sys.stderr)
    for item in problems:
        print(f"[startup]   - {item}", file=sys.stderr)
    print("[startup] 请在部署环境（如 /opt/biodata-web/.env）补齐后重试；本机使用请移除 BIODATA_REQUIRE_ACCOUNT。",
          file=sys.stderr)
    return 2


def warm_web_recall(*, preferred_backend: str = "cross_encoder", log=print) -> str:
    """启动期（主线程、事件循环之外）预热本地语义重排模型。

    动机：`strategy=auto`（前端默认）在候选压力高、语义信息丰富的查询上会选用本地
    cross-encoder 做语义重排。该模型（bge-reranker-v2-m3，~2GB）若不预热，会在**首个
    用户请求内**惰性加载（~12s，冷进程/冷磁盘可达数十秒），让首查看起来「卡死」。这里
    在开服时、请求处理之前把它加载进 `vector_recall` 的进程级缓存，首查即命中。

    只在真正的启动器 `run_web.py` 调用——**不能**放进 FastAPI startup 事件，否则每个
    `TestClient(app)`（web 测试大量使用）实例化都会触发 2GB 加载。加载时机也满足
    `vector_recall.warm_recall_backend` 的硬约束：必须在事件循环/请求处理之外。

    返回状态字符串（供测试/诊断，不抛异常、绝不阻断开服）：
    - "disabled"    ：`BIODATA_SKIP_RECALL_WARM` 置真 → 跳过（快速开发重启用）。
    - "unavailable" ：本地无该模型或依赖未装（交付默认态）→ 不加载、不下载，秒过。
    - "warmed"      ：模型已加载进进程级缓存，后续 auto 首查直接命中。
    - "failed"      ：可用但加载失败（已被 vector_recall 内部吞掉 → 本次运行 auto 回退确定性）。
    """
    if os.getenv("BIODATA_SKIP_RECALL_WARM", "").strip().lower() in ("1", "true", "yes", "on"):
        return "disabled"
    try:
        from dataset_recommender.retrieval.vector_recall import recall_backend_available, warm_recall_backend
    except Exception:
        return "unavailable"
    if not recall_backend_available(preferred_backend):
        return "unavailable"
    log(f"[startup] 预热本地语义重排模型（{preferred_backend}）… 首次加载较慢，请稍候。")
    started = time.perf_counter()
    try:
        ok = warm_recall_backend(preferred_backend)
    except Exception as exc:  # 防御：warm_recall_backend 已自吞异常，这里再兜一层，绝不阻断开服。
        log(f"[startup] 语义模型预热异常（{time.perf_counter() - started:.1f}s）：{type(exc).__name__}: {exc}")
        return "failed"
    elapsed = time.perf_counter() - started
    if ok:
        log(f"[startup] 语义模型预热完成（{elapsed:.1f}s）—— strategy=auto 首查将直接命中缓存。")
        return "warmed"
    log(f"[startup] 语义模型预热未成功（{elapsed:.1f}s）—— 本次运行 auto 将回退确定性排序。")
    return "failed"


def open_browser_when_ready(host: str, port: int, *, timeout_s: float = 90.0,
                            poll_s: float = 0.25, log=print) -> threading.Thread:
    """守护线程：轮询 (host, port) 直到 uvicorn 真正开始监听，再开系统默认浏览器。

    为什么不能在 uvicorn.run 前直接 webbrowser.open：主线程先做语义模型预热（可达 ~12s），
    端口到 uvicorn.run 才绑定——过早开页只会让浏览器先撞「连接被拒」。守护线程连接成功
    即开页；超时如实提示手动地址，绝不抛异常、不阻断开服（与预热同款防御口径）。
    服务 macOS/Linux「源码起服务 + 浏览器全功能」的官方形态（README 平台支持矩阵）。
    """
    url = f"http://{host}:{port}"

    def _wait_and_open() -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=1.0):
                    pass
            except OSError:
                time.sleep(poll_s)
                continue
            if not webbrowser.open(url):
                log(f"[startup] 无法自动打开浏览器，请手动访问 {url}")
            return
        log(f"[startup] {timeout_s:.0f}s 内服务仍未就绪，未自动开页；就绪后请手动访问 {url}")

    thread = threading.Thread(target=_wait_and_open, name="biodata-open-browser", daemon=True)
    thread.start()
    return thread


def main() -> int:
    # parse_known_args：pytest/嵌入式调用方会把自身参数留在 sys.argv 里，未知参数一律
    # 忽略、只认 --open——缺省 False 时行为与历史逐字节一致（test_main_calls_warm_before_serving 钉死）。
    parser = argparse.ArgumentParser(prog="run_web.py", description="BioData Agent Web 服务启动器")
    parser.add_argument("--open", action="store_true",
                        help="服务就绪后自动打开系统默认浏览器（macOS/Linux 推荐用法）")
    args, _unknown = parser.parse_known_args()
    # 缺省仅回环（本机安全形态不变）；Docker/服务器部署经 BIODATA_WEB_HOST 显式改绑
    # （容器内 0.0.0.0，宿主 Host 守卫白名单另见 webapp 的 BIODATA_TRUSTED_HOSTS）。
    host = os.getenv("BIODATA_WEB_HOST", "").strip() or "127.0.0.1"
    # PORT 是无前缀通用名（云平台/开发工具常设），ambient 残留非数字值曾让启动
    # 直接 ValueError 崩溃——改容错解析（非数字回落 7860 并 warning 点名）。
    from dataset_recommender.llm.config import parse_int_env  # noqa: E402
    port = parse_int_env("PORT", 7860)
    # 公网护栏 fail-closed：护栏模式缺必需配置直接 return 2，不起服务；
    # 闸关时零校验、零行为变化（既有 warm/serve 顺序钉死测试不受影响）。
    guard_rc = _validate_guard_config()
    if guard_rc:
        return guard_rc
    if args.open:
        print("[startup] 已启用 --open：服务就绪后自动打开浏览器。")
        open_browser_when_ready(host, port)
    warm_web_recall()
    print(f"BioData Agent Web UI running at http://{host}:{port}")
    # 下载 job、整库响应缓存与桌面壳活动状态目前是进程内单一真源；显式锁定 1 worker，
    # 防止未来复制启动参数时误开多进程，产生“查不到另一个进程任务”的分裂状态。
    uvicorn.run(
        "dataset_recommender.app.webapp:app", host=host, port=port,
        reload=False, workers=1, log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
