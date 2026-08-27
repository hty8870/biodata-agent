---
name: frontend
description: BioData Agent 前端（web/static 下 html/css/js）编码约束。凡修改本仓库前端代码时必须遵守。
---

# 前端编码约束（BioData Agent）

> 后端约束见 `../backend/SKILL.md`。本文件管 `web/static/` 的一切改动。

## 缓存令牌契约（最容易踩的雷，先读这条）

- `web/static/{css,js}` 任何文件内容一变，**必须同步三件事**，缺一不可：
  1. `web/static/index.html` 与 `dataset.html` 里全部 `?v=` 令牌换成新代号（两页同一代）；
  2. `tests/test_release_version_contract.py` 的 `CACHE_GENERATION` 改成同一代号；
  3. 同文件的 `STATIC_ASSETS_SHA256` 改成新指纹（测试报错信息里会给出算法和值）。
- 不 bump 的后果：浏览器启发式缓存继续跑旧 JS，修复到不了用户；新旧 JS 混合缓存会抛
  ReferenceError（开发日志记过两次真交付事故）。
- 守门测试：`./.venv/Scripts/python.exe -m pytest tests/test_release_version_contract.py -q`。

## 交互与视觉

- 对话式交互：除首页起始对话外，一切对话界面是**微信式**（气泡上下排列、输入框在对话记录
  下方、默认空），集中在左下侧边栏；不做第二个对话入口。
- 检索/执行中的等待反馈：按钮处滚动进度数字；完成后结果展示 + 侧边栏变形要有过渡动画。
- 状态变化用动画过渡（出现/移动/变形），不跳变；输入中不改变 tag/框宽（识别完成后一次性
  更新；停止输入一段后再识别，防框体跳动）。
- 视觉走既有设计体系（青绿主色、圆角卡片、留白），新组件先对齐现有卡片样式再谈创新。

## 文案纪律

- 用户可见文字：简明、口语、无 AI 味——不堆术语、不写「我们很高兴」、不长篇解释；
  报错说人话并给下一步（「把这些关键词去掉，可能会有结果」好于三段式解释）。
- 不撒谎：功能做不到就明说做不到 + 指路（快照源如实说「请去官网核对」）；降级如实标注
  降了什么。
- hint/说明文字提及能力清单时与后端真源一致（例：联网源四源化后「都是英文源」不再单提
  ArrayExpress）。

## 结构与协作

- 页面：index.html（智能查询主页）/ dataset.html（数据集浏览）；逻辑按文件分工
  （search.js / act.js / cards.js / progress.js …），新功能优先在既有文件内 additive，
  新文件须在两页 importmap 与 script 标签同时登记。
- 前端不自己造口径：来源名、动作名、错误提示等从后端响应取（后端出话原则），前端不硬编码
  业务措辞；必须硬编码的（如 act.js 的 `ACT_SOURCE_TOKEN_RE`）注释里注明与后端哪个常量同源。
- 行动流（act.js runner）：链式直推 plan→apply 已预先授权，不再开问卷；保留审计展示
  （步骤、账本、回执），写操作结果回执放对话气泡内。
- 改完跑前端相关测试（test_act_frontend / test_unified_box / test_action_plan 等）+
  契约门；真机视觉验证用测试服：`BIODATA_SKIP_RECALL_WARM=1 PORT=79xx ./.venv/Scripts/python.exe scripts/run_web.py`（7860 端口永不碰）。
