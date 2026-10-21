"use strict";

/* 启动收口（ES Module，全应用唯一入口）：DOMContentLoaded 时平铺串调各模块的
   init/sync。每个名字都从属主模块 import，不再靠共享全局运行时解析——某个模块把函数改名/删了，
   import 在加载期就炸（tests/test_frontend_boot_contract.py 静态双端核对同一个不变量）。
   init() 函数体保持原顺序原样：加载序契约（core 最先、boot 最后）与初始化顺序都不变。 */
import { loadDomainManifest, registerContextChanged } from "#core";
import { loadConfig, initFabTuck, initSidebar, initSidebarResize, initStrategyTooltips, showView,
    initLocalModelControl, syncAiGates, syncAgentAvailability, syncStrategyNode, initLibWin, initHistWinSkeleton,
    initFloatingWin, initRailToggle, closeSettings } from "#shell";
import { bind, playHero, renderHeroGreeting, initHeroRot, initTimeFilter, initTimeSelector, initSourceSelector, initSourceChips,
    setSourcesReady } from "#interactions";
import { initAccounts } from "#accounts";
import { initUserMemory, renderMemoryManager, renderMemorySuggestions } from "#memory";
import { initDream } from "#dream";
import { initUsage } from "#usage_log";
import { initBenchfb } from "#benchfb";
import { initCondBoard, cbClear, cbConvId, cbLogForHistory, ubRecoverPendingRun,
    cbSharedConversation, cbRefreshSharedConversation } from "#board";
import { initReplay } from "#replay";
import { initKnowledge } from "#knowledge";
import { initTaskPack } from "#task_pack";
import { initDownloads } from "#downloads";
import { initAct } from "#act";
import { initHistWin, renderHistory, viewHistorySnapshot, createHistoryRow } from "#browse";
import { initOnboarding } from "#onboarding";
import { initProjects } from "#projects";
import { initWorkspaces, setWorkspaceHooks, renderLearningScopeSelect } from "#workspaces";
import { bumpRecSeq } from "#search";
import { resetSubmitButton } from "#progress";
import { exitResultsLayout } from "#results";
import { initConversationClient } from "#conversation_client";
import { initLiveDots } from "#live_dot";

let _bootReady = false;

async function init() {
    loadConfig(); initSidebar(); initSidebarResize(); initRailToggle(); initStrategyTooltips();
    // telemetry namespace 必须等 whoami 落定；否则启动上传/首轮打点会随机落匿名或登录账户。
    await initAccounts();
    initConversationClient();
    setWorkspaceHooks({ conversationId: cbConvId, hasConversation: () => cbLogForHistory().length > 0,
        sharedConversation: cbSharedConversation, refreshConversation: cbRefreshSharedConversation,
        openConversation: viewHistorySnapshot, historyRow: createHistoryRow });
    registerContextChanged((kind) => {
        if (kind === "conversations") renderHistory({ refresh: false });
        if (kind === "before-project" || kind === "before-account") {
            cbClear(); bumpRecSeq(); resetSubmitButton(); exitResultsLayout();
        }
        if (["project", "membership", "workspace-data", "learning-scope", "account"].includes(kind)) {
            renderLearningScopeSelect(); renderMemoryManager(); renderMemorySuggestions(); renderHistory();
        }
        if (_bootReady && kind === "project") Promise.resolve().then(ubRecoverPendingRun);
    });
    await initWorkspaces();
    bind(); initUserMemory(); initDream(); initUsage(); initBenchfb(); initLocalModelControl(); syncAiGates(); showView("query");
    renderHeroGreeting(); initHeroRot(); playHero(); initTimeFilter(); initTimeSelector(); initSourceSelector();
    setSourcesReady(initSourceChips()); initCondBoard(); initTaskPack(); initDownloads(); initAct(); initLibWin(); initHistWinSkeleton();
    initReplay();   // 复盘页签（2026-10-10 rp1 起迁入侧栏第三页签，浮窗退役）：DOM 自守卫 + 幂等，replay 全家不 import #shell/#board
    initKnowledge({ floating: initFloatingWin, beforeOpen: () => closeSettings({ returnFocus: false }) });
    initOnboarding(); initFabTuck(); syncAgentAvailability();
    initLiveDots();   // 选中/当前项目圆点 WebGL 活点（自守卫：无命中圆点或 WebGL2 不可用即静默回退）
    // 页面标题随活动领域包（biodata 默认与静态 <title> 逐位一致；fire-and-forget 不阻塞启动）。
    loadDomainManifest().then((m) => {
        const t = m && m.branding && m.branding.page_title;
        t && (document.title = t);
    });
    initProjects();   // 追踪浮窗/存为追踪/上下文 chip（全 DOM 自守卫）
    await initHistWin(); _bootReady = true;
    ubRecoverPendingRun();
    const refresh = () => { if (!document.hidden) cbRefreshSharedConversation(); };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    setInterval(refresh, 5000);
}
document.addEventListener("DOMContentLoaded", init);
