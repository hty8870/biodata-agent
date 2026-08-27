# BioData Agent

BioData Agent 是一个面向公开单细胞与空间组学数据集目录的**对话式数据发现 Agent**。架构上以 **ReAct 为主环、RAG 为检索核**（ReAct+RAG hybrid）：用户用自然语言对话，系统调用大模型进行意图观察后对应加载tool与系统提示词，每次tool call都会附着完整的执行信息与执行时的背景信息，使其拥有常规agent不具备的**安全性**与deepseek harness同款的**强可溯源性**（审计/回退等等都方便）

项目可在本地离线靠规则检索与可选的向量召回+重排执行检索。LLM 重排、推荐说明润色、AI 执行均为可选能力，不影响基础检索的可用性。形态上既可本机安装包/源码运行，也有带账号体系的网页版镜像部署。

## 架构速览

```text
用户一句话
  └─ 统一路由（规则分类 → 直达 / 条件板规划 / LLM 护栏解析）
       ├─ 检索面（RAG）：关键词检索 → LLM 判断（重排 / 润色 / 识别工具调用）
       │    → 硬过滤 + 多层排序（规则 / 本地语义 / AI 重排）→ 可解释结果卡片
       └─ 执行面（ReAct 主环，langgraph）：封闭动词表工具调用
            → 两步确认写盘 → 审计账本 → 回执（未装扩展自动回退内置规划器）
```

两条面共用同一套语料（冻结基准 + 外部库）与同一套确定性生成器；任一 AI 环节不可用都诚实回退，绝不静默改变行为。

## 快速开始

### Windows：双击启动

1. 确认电脑已安装 Python 3.10 或更高版本。
2. 双击项目根目录的 `打开前端.bat`，也可以双击 `start-web.bat`。
3. 首次运行会自动准备项目专用 `.venv`、按需安装运行依赖，并打开浏览器；不会静默借用开发或 MCP 环境。
4. 页面地址默认为 <http://127.0.0.1:7860>。

> 仓库克隆说明：交付包/便携包内启动脚本与依赖清单仍在包根目录（包内布局不变）；**仓库克隆**里启动脚本在 `launchers/`、依赖清单在 `requirements/`、MCP 入口在 `src/dataset_recommender/app/mcp_server.py`。快速开始一节面向交付包用户按包内路径书写；后文开发与排障章节的命令按仓库克隆路径书写。

首次安装依赖需要网络。以后启动会复用本项目的 `.venv`。如确需共享解释器，须用 `BIODATA_PYTHON` 显式指定；关闭启动窗口即可停止服务。

如果 Python 没有加入 PATH，可在启动前设置解释器路径：

```powershell
$env:BIODATA_PYTHON = 'C:\你的Python3.10+安装路径\python.exe'
.\打开前端.bat
```

临时改用其他端口：

```powershell
$env:PORT = '7861'
.\打开前端.bat
```

### macOS / Linux：一键启动

macOS 或 Linux 的「双击即用」对齐 Windows 的 `打开前端.bat`——解压后点一个入口就全流程搞定：

```bash
sh 打开前端.sh        # Linux / 通用；macOS 也可直接双击「打开前端.command」
```

首次运行会自动准备并复用项目内 `.venv`（不会静默借用开发/MCP 环境）、按需安装依赖、
用英文问三个可选问题（全可回车跳过），然后自动打开浏览器；页面地址默认为
<http://127.0.0.1:7860>（端口被占会自动漂移到 7861-7869，同版本已在运行则复用并提示）。
首装依赖需联网，之后复用已有环境。若 macOS 弹出“来自身份不明的开发者”，右键 → 打开，
或先在终端执行 `chmod +x 打开前端.command`。

### 桌面窗口模式（可选，2026-08-21 起）

想要「原生应用」手感（独立窗口、无浏览器界面）时，可以装壳层可选依赖后用窗口模式启动：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements\requirements-webview.txt   # 一次即可（pywebview 5.4）
.\.venv\Scripts\python.exe scripts\run_app.py                          # 原生窗口，关窗即退出
```

- 窗口底色/尺寸/图标已与应用一致：Windows 11 原生标题栏的 caption/text/border 分别对齐
  页面 `--bg`/`--text`/`--border`，不再出现白色顶栏色差；图标取自
  `packaging/assets/BioDataAgent.ico`，完整保留官方 favicon 的青底、白环与轨道弧；
  不支持 DWM caption color 的旧 Windows/macOS/Linux 保留操作系统原生标题栏配色；
  数据集官网等外部链接在**系统浏览器**中打开；结果文本可选中复制、Ctrl+滚轮可缩放。
- 下载（复用清单/任务包等）走系统「另存为」对话框；真实数据仍在下载时关闭桌面窗口会先
  明确提醒。登录态/收藏/普通设置（localStorage）落在数据根目录 `webview/` 下，随数据一起
  迁移、重装不丢；API Key 默认只留在当前会话，只有单独勾选“也记住 API Key”才会落入本地存储。
- 壳不可用时**优雅回退**：没装 `pywebview`、没装 WebView2 运行时（干净 Windows 系统）或
  建窗失败 → 自动改为打开系统浏览器 + 恢复托盘，服务照常运行，绝不白屏。
  **浏览器开发通道永远保留**（调试、截图、Playwright 工作流照旧走 `run_web.py`）。
- 调试开关：`BIODATA_SHELL_DEBUG=1` 可让窗口内右键打开 DevTools（壳内排障）。
- 安装版（Inno 打包的 `BioDataAgent.exe`）开始菜单/桌面快捷方式与安装完成立即运行默认
  已是窗口模式（内置 `--window` 参数）；直接双击裸 exe 仍走浏览器 + 托盘，作为
  恢复/诊断通道。

### 手动启动

Windows PowerShell：

```powershell
Set-Location -LiteralPath 'C:\你的路径\biodata-agent'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements\requirements.txt
.\.venv\Scripts\python.exe scripts\run_web.py
```

macOS 或 Linux：

```bash
cd /path/to/biodata-agent
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements/requirements.txt
./.venv/bin/python scripts/run_web.py
```

如果环境已经存在，只需执行最后一条启动命令。若 `python` 或 `python3` 命令不可用，请改用实际 Python 3.10+ 解释器的绝对路径。

启动命令可加 `--open` 参数：服务就绪后自动用系统默认浏览器打开界面（macOS/Linux 推荐用法，Windows 同样适用）：

```bash
./.venv/bin/python scripts/run_web.py --open
```

### 平台支持

| 平台 | 支持形态 | 说明 |
|---|---|---|
| Windows | 桌面窗口 App（WebView2）+ 安装器；也可浏览器访问 | 双击 `start-web.bat` 或安装版启动；桌面形态含托盘、单实例、本地通知 |
| macOS / Linux | 源码起服务 + 任意现代浏览器 | 界面是同一套网页，检索、对话、上传、管护等全部功能与 Windows 完全一致；双击 `打开前端.command`（macOS）或运行 `sh 打开前端.sh`（Linux/通用）即可一键启动（自动找 Python、建/复用 venv、装依赖、首启向导、起服务、开浏览器），与 Windows 的 `打开前端.bat` 对齐 |

macOS/Linux 端不提供原生桌面窗口、安装器与托盘（WebView 壳与启动器为 Windows 专属）；账户与本地数据
（收藏、历史、上传）保存在各自机器上，不同设备之间互不同步——换一台机器即全新用户态，需重新注册与配置密钥。

#### 企业代理环境

在必须经代理出网的企业内网中，各部分对代理的处理不同（属安全设计，不是缺陷）：

- **LLM 调用、数据集管护（curate 联网搜）与遥测上传**：走本机标准 `HTTP_PROXY` / `HTTPS_PROXY`
  等代理环境变量（或各客户端工具各自的代理配置）。
- **下载执行器（`provision_dataset`、任务包下载等）**：为防 SSRF，钉目标 IP 直连、**绕过代理**。
  因此在「只有代理能出网、机器本身无法直连外网」的内网里，「服务端代下」会失败（无路由或超时），
  属预期行为——此时请把下载脚本拿到能直连外网的机器执行，或用浏览器/系统下载工具手动下载数据集。

### 可选：执行侧 Agent 扩展（langchain/langgraph）

对话里的「工具调用/数据库管护」类指令（检查更新、联网搜、删除、导入等）默认由内置规划器处理，
**不装任何扩展也能正常使用**。安装 langchain 扩展后，这部分规划改由 langgraph 编排的 Agent 接管
（工具调用协议 + 自我修正 + 有界多步执行，规划更稳）。开启「AI 执行」（设置里的开关，**默认开**）后，
操作类指令会**直接替你办掉**：联网搜会访问官方数据源并把新数据导入你的外部库，删除是移入回收站
（可随时恢复），每一步都写入本机审计账本（`.userdata/` 下的 jsonl），出错可核对可回退：

```bash
pip install -r requirements/requirements-langchain.txt
```

未安装 langchain 扩展、或未配置大模型时，执行层自动回退到内置规划器（功能一致，AI 接口
不可用时只打开打包清单并如实标注；`BIODATA_AGENT_EXEC=off` 可强制只用内置规划器）。
在设置里关掉「AI 执行」则一切输入按规则检索处理，操作类指令只回一句说明和指路、不执行任何动作。

## 基本使用流程

1. 在“智能查询”中输入实验需求，例如：
   - `推荐近三年 CELLxGENE 中的人类肝脏单细胞数据`
   - `找有 FASTQ 的小鼠脑 scRNA-seq 数据`
   - `2015 年以来的人类乳腺癌数据，不要空间转录组`
2. 保持“数据来源”和“发表时间”为“自动识别”，系统会从查询中提取对应条件；也可以展开后手动选择。
3. 看结果上方的检索摘要：它写明系统识别出了哪些条件、这次用了哪几层排序、库中共匹配多少条，以及有没有哪一层因为不可用而改用了基础方式。
4. 打开结果卡片查看数据集来源、匹配理由、页面链接和文件清单；点击“数据集详情”会在新标签页打开该数据集的详情页。

首次进入页面会显示 12 步新手教程，可拖动、可跳过，之后能在“帮助”里重新打开。教程第 5 步会展开真实的 API 配置表单：先选服务商，再填自己的 API Key（默认只在本次会话使用，除非主动勾选记住设置）；不想现在配置可直接跳过，基础检索仍然可用。桌面端左侧栏可拖动调整宽度，也支持键盘方向键微调。

“数据集浏览”提供年份时间线与范围筛选。当用户明确点击保存时，当前查询可作为一条“用户记忆”保存在当前浏览器；记忆可检查、复用、逐条删除或清空，不会自动拼入查询，也不会上传到服务端。

检索结果页底部还有一组“下一步行动”建议：按风险分层明示（只读查看、改条件重检、写盘/联网各档分开标注），结果过宽时会直接给出收窄方向。满意的检索现场可以一键“存为追踪”——追踪只存在本机浏览器里，从左侧导航“我的库”进入：切换进度状态必须填理由；“检查更新”用当初的条件确定性重跑，新出现在的数据集先进“待核验”，绝不自动纳入；导出中心可产出研究材料包（纳入排除表、RIS/BibTeX/GB/T 7714 三格式引文、“数据发现与筛选方法”草稿——草稿如实标注）。数据集详情页的“同步数据集”可检查官方源更新并导入本地外部库，整次同步可一键撤销。设置抽屉里的“向开发者发送意见”支持把建议加密发回：单次明示授权、诊断信息默认勾选可去勾、内容在本地先用开发者公钥加密。

“使用反馈”默认开启本地采集：软件会记录你发送的消息、系统展示的结果、点开或下载的条目、耗时与报错、评分和评语。不采集 API Key、密码和账户名；**每个本机账户首次发送前独立确认**。只有部署方配置了安全 HTTPS 遥测通道才会脱敏自动上传；默认空配置只存在本地，可手动导出。可随时关闭，或清空当前账户尚未上传的 usage/benchmark 记录；本地清空不会远程删除已经上传的数据。

## 排序策略

设置页会完整显示当前可用的排序层：

- **规则排序**：始终启用。先执行硬条件过滤，再按确定性规则排序。
- **本地精准重排**：使用本地语义模型调整候选顺序，不需要 API Key。模型未安装时自动回退到规则排序；可在安装器里勾选在线下载（默认不勾），也可在设置页稍后在线安装。
- **AI 重排**：调用已配置的 LLM 调整通过硬过滤的候选顺序，需要网络和 API Key。
- **AI 润色推荐说明**：只改善推荐理由的表达，不改变数据和排序。

鼠标悬停在排序策略上，或用键盘将焦点移到对应项，页面会说明该策略的用途、依赖和回退行为。

默认开启“自动选择排序策略”。系统会根据查询复杂度决定是否叠加本地精准重排或 AI 重排；规则排序始终保留。若希望完全手动控制，可关闭自动选择，再单独启用本地或 AI 重排。

自动模式不会隐藏 AI 重排候选数和关键词审核设置。即使暂时没有允许自动使用 AI，也可以预先调整这些参数。

## 数据范围

当前内置语料包含 8,022 条公开数据集元数据。用户上传后总数会相应增加。

| 来源 | 当前记录数 |
|---|---:|
| 10x Genomics | 784 |
| CELLxGENE Discover | 2,198 |
| ArrayExpress | 1,784 |
| HuBMAP | 1,016 |
| Broad Single Cell Portal | 830 |
| Human Cell Atlas | 532 |
| EBI Single Cell Expression Atlas | 384 |
| refine.bio | 300 |
| Zenodo | 94 |
| NCBI GEO | 60 |
| ENCODE | 40 |

数据分为两层：

- `database/base/`：随项目提供的 10x Genomics 基础库，用于稳定检索与评测。
- `database/external/`：外部库——其他公开来源的快照与用户上传的数据。只有明确选择相应来源时才参与推荐。

项目处理的是数据集元数据，例如标题、物种、组织、疾病、技术、发布日期、公开 URL 和文件清单。项目不包含生物序列、私有临床原始数据或湿实验操作方案。

## 上传自己的数据集

网页端进入“数据集浏览”，在页面顶部上传 UTF-8 JSON 文件。上传内容只写入 `database/external/`，不会覆盖基础库；成功后无需重启即可浏览和检索。

最小示例：

```json
[
  {
    "dataset_name": "Human lung single-cell atlas",
    "species": "Human",
    "tissue": "lung"
  }
]
```

完整字段、来源优先级、批量格式和删除方法见[数据集上传规范](使用教程/数据集上传/数据集上传规范.md)。

## API 与 LLM 配置

不配置 API 也能使用规则检索和已安装的本地语义模型。基础安装与规则检索不依赖任何模型。

网页端配置方法：

1. 打开“设置”。
2. 展开“AI / API 配置”。
3. 可直接选择 DeepSeek、Kimi、Qwen、GLM、OpenRouter 或 OpenAI；页面会自动填入官方兼容地址和推荐模型。未单列的服务仍可使用“兼容接口”，本地部署可选“本地模型”。
4. 保存后再开启 AI 重排、自动使用 AI 或推荐说明润色。

预设地址和模型都可以修改，以适配账号区域、模型权限或后续版本变化。API Key 默认只在当前页面会话中使用；普通设置保存与密钥保存相互独立，只有同时开启“记住非敏感设置”和“也记住 API Key”后，Key 才会写入当前浏览器的本地存储。旧版本遗留但没有这项独立授权的 Key 会在载入设置时自动清除。不要在共享电脑上保存个人 Key。

服务端也可以使用项目根目录的本地 `.env`。先复制 `.env.example`，再填写实际值：

```powershell
Copy-Item .env.example .env
```

`.env` 已被 Git 忽略，不应上传、提交或发送给他人。配置优先级为：进程环境变量、`BIODATA_LLM_ENV_FILE` 指向的外部配置文件、项目 `.env` 或 `.env.zhipu`、程序默认值。

MCP 场景建议使用项目外密钥文件，详见[MCP 安装、API 配置与验收教程](使用教程/MCP安装/MCP_安装教程.md)。

## 命令行使用

Windows：

```powershell
$Python = '.\.venv\Scripts\python.exe'
& $Python src\dataset_recommender\app\cli.py --query '推荐有 FASTQ 的人类乳腺癌数据' --strategy auto --no-llm --show-pipeline
```

macOS 或 Linux 把 `$Python` 换成 `./.venv/bin/python`。

常用参数：

| 参数 | 作用 |
|---|---|
| `--top-k N` | 设置返回数量 |
| `--strategy fixed\|auto` | 使用手动策略或自动策略 |
| `--recall off\|cross_encoder\|dense` | 选择本地语义重排方式 |
| `--rerank off\|llm` | 是否使用 AI 重排 |
| `--rerank-top-n N` | 设置送入 AI 重排的候选数量 |
| `--rerank-audit` | 让 AI 审核关键词并在必要时改写后重搜 |
| `--use-llm` / `--no-llm` | 开启或关闭推荐说明润色 |
| `--show-pipeline` | 输出实际执行策略与回退信息 |
| `--output-file PATH` | 把结果写入文件 |

查看完整参数：

```powershell
& $Python src\dataset_recommender\app\cli.py --help
```

## 本地语义模型

本地精准重排依赖额外模型，模型权重不会随提交包分发。安装版（Inno Setup 打包的 `.exe`）在安装器中提供可选「本地模型」任务，默认不勾选；安装后也可在设置页的「本地模型」入口在线安装，失败或取消会诚实回退、不影响规则检索。源码包可按下面方式手工安装依赖与模型：

```powershell
$Python = '.\.venv\Scripts\python.exe'
& $Python -m pip install -r requirements\requirements-embeddings.txt
& $Python scripts\fetch_embedding_model.py
```

如需同时下载所有支持的本地模型，可使用 `--all`。未安装模型不会阻止项目启动，系统会回退到规则排序。

## MCP 接入

项目提供 19 个 stdio MCP 工具，可供 Codex、Claude Code 或其他兼容客户端调用：

| 工具 | 作用 |
|---|---|
| `recommend_datasets` | 按中文自然语言查询推荐数据集（可选分面细化、放宽约束、时间范围） |
| `browse_datasets` | 按来源/物种/平台/年份浏览目录，不需要查询语句 |
| `get_file_manifest` | 取某数据集的文件清单（文件名、大小、md5、下载直链） |
| `get_dataset_introduction` | 取某数据集的结构化介绍（与网页“数据集详情”页的介绍标签同源） |
| `assess_dataset_fair` | 对某数据集做复用就绪度检查 + 生成「复用公开数据」英文声明 |
| `build_reuse_pack` | 把 N 个复用的公开数据集整理成投稿材料（英文段落 + 数据集清单 + 待核实项 + RIS/BibTeX 导出） |
| `lookup_identifier` | 按标识符精确反查（UUID / E-XXXX-N / DOI 直达记录；GEO/SRA 号如实告知不在本目录、指向原库） |
| `find_compatible_datasets` | 给一个数据集找同物种 + 兼容 chemistry/platform 的其它数据集（只回「元数据兼容」，非「可整合」断言） |
| `assess_feasibility` | 研究问题 → 可行性概览（候选数 + 总细胞量下限 + 物种/平台/年份/来源分布 + 可下载率 + 缺口） |
| `plan_query_edit` | 接着改条件：把「换成小鼠 / 再加一条：要有 FASTQ / 去掉组织限制」规划成一次具体改动（只规划、不检索） |
| `plan_action` | 一句话要做什么：把「把前 5 条打包」「存成压缩包给我」归一化成封闭动词表里的一个动作（只出计划、不执行；判定依据保证是原话字面子串） |
| `build_task_pack` | 一句话任务包：一次检索 → 结果清单 + 下载脚本 + FAIR 自检 + 引文（先预览、确认后再产包） |
| `verify_local_assets` | 扫描本地目录，与 10x 文件清单（md5/大小/文件名）比对成资产台账（只算 md5、不读数据内容；外部库无单文件清单则无法 md5 核验） |
| `provision_dataset` | 把数据集文件按需真正下载到调用方指定目录（https+白名单、md5/大小核对、.corrupt 留证据；默认只下主文件，dry_run 可先预演；只在给定 dest_dir 时写盘，绝不写 `database/`） |
| `curate_datasets` | 对话式数据库管护：清点 / 本地导入（内容去重）/ 联网搜官方源 / 回收站式删除 / 恢复 / 检查官方源更新 / 同步更新（管护对象限外部库 `upload_*` 文件；先预览拿 confirm_token、确认后才写盘/联网，token 不符零写入；同步落盘返回 operation receipt，可整次撤回） |
| `parse_constraints` | 只解析查询语句、不检索（看系统把话理解成了什么） |
| `upload_dataset` | 把自己的数据集元数据摄取进外部库，即时可检索（写 `database/external/`，与 `provision_dataset`、`curate_datasets` 并列为仅有的三个写盘工具） |
| `biodata_status` | 服务与语料状态 |
| `biodata_llm_status` | LLM 配置状态（只读配置、不联网） |

`recommend_datasets` 的每个候选都兼容性追加 `introduction`，内容与网页“数据集详情”页的介绍标签共用同一确定性生成器。

### 对话式数据库管护（curate_datasets）

`curate_datasets`（写工具之一）让你用一句话管护**自己上传的数据**：`action=list` 清点外部库与回收站、
`import` 导入本地数据集 JSON（内容 hash 去重，撞重默认拒绝、`force=true` 覆盖）、`search_online` 联网搜索
官方源（首发 ArrayExpress，候选先预览、确认后才入库）、`remove` 把上传文件移入回收站（可逆，不是真删除）、
`restore` 从回收站移回、`check_updates` 检查官方源有无更新、`sync_updates` 把更新同步进外部库（落盘返回
operation receipt，可整次撤回）。所有写动作都是**两步确认**：默认只返回预览和 `confirm_token`（不落盘），
回传 token 才真执行；token 与内容指纹不符时一个字节都不写。管护对象仅限 `database/external/` 的
`upload_*` 文件，官方快照与冻结基准 `database/base/` 结构性不可达。网页端对应 `POST /api/curate/plan`、
`/api/curate/apply`、`/api/curate/sync-updates`（另有只读 `/api/curate/sync-status` 与整次撤回
`/api/curate/recall`），命令行对应 `scripts/curate_datasets.py`，三处共用同一套逻辑。

网页端另提供统一对话路由 `POST /api/utterance`：一句话先经规则分类——明确检索直达（不调大模型）、
改条件走条件板规划、动作或歧义句走大模型护栏解析（仅这一路可能调用大模型；只路由、不执行）。

每次 MCP 工具调用都会在本机追加一行脱敏 JSON 日志到 `.userdata/mcp_calls.jsonl`（不联网、不入库），用于需求分析；在 MCP 客户端的 env 里设 `BIODATA_MCP_CALL_LOG=off` 可关闭，统计用 `scripts/summarize_mcp_calls.py`。

Windows 可使用 `scripts\setup_mcp.ps1` 完成独立环境、协议自检、API 配置、客户端注册和回读验证。完整步骤见[MCP 安装教程](使用教程/MCP安装/MCP_安装教程.md)。

## 开发与测试

面向开发者的环境、架构、HTTP 端点、扩展方法和验证矩阵见[开发指南](DEVELOPMENT.md)。

当前 Web API 版本为 `2.7.0`。开发和交付优先使用清单驱动的统一质量门：

```powershell
$Python = '.\.venv\Scripts\python.exe'
& $Python scripts\quality_gate.py --list
& $Python scripts\quality_gate.py --profile fast
& $Python scripts\quality_gate.py --profile full --report-json artifacts\quality-full-local.json
```

`fast` 用于快速语法与自动化契约检查；`full` 还会执行依赖一致性、治理校验、全量 pytest、项目/Web/MCP smoke 和冻结推荐评测。runner 会清除密钥/进程注入变量、禁用模型下载，并用离线环境与网络 tripwire 约束受审查的门；这不是操作系统级网络沙箱。运行前仍需在当前环境安装所需依赖，并准备 Node.js 与 PowerShell。也可以单独运行常用检查定位失败：

```powershell
& $Python -m pytest tests\ -q
& $Python scripts\smoke_test.py
& $Python scripts\web_smoke_test.py
```

MCP 自检必须使用安装教程创建的 MCP 专用 Python，而不是只安装了基础依赖的项目 `.venv`：

```powershell
$McpPython = Join-Path $env:LOCALAPPDATA 'BioDataAgent\mcp-venv\Scripts\python.exe'
& $McpPython src\dataset_recommender\app\mcp_server.py --selfcheck
```

哈希锁定的 CI 环境、GitHub 工作流与候选 ZIP 构建以仓库内工件为准：工作流见 `.github/workflows/`，质量门清单见 `automation/quality-gates.json`，构建与校验脚本为 `scripts/build_release.py`。仓库中的工作流是可执行配置，不代表已在 GitHub 实跑；当前流程只生成候选包，不创建 GitHub Release，也不部署生产环境。

## 目录概览

```text
biodata-agent/
├─ launchers/                     图形入口：打开前端.bat/.command/.sh、start-web.bat、创建桌面快捷方式.bat
│                                 （交付包/便携包内仍按历史布局落在包根目录）
├─ README.md                      本文件：安装与使用
├─ DEVELOPMENT.md                 面向二次开发：架构、接口、验证方法
├─ MODULES.md                     模块与接口契约的详细事实
├─ SECURITY.md                    密钥与数据处理约定
├─ requirements/                  依赖清单：requirements.txt（运行最小依赖）及 CI/可选组件清单与锁
├─ 使用教程/                      MCP 接入、Skill 安装、数据集上传规范
├─ src/dataset_recommender/       后端：查询解析、检索、排序与接口实现
│  └─ app/mcp_server.py           接入 AI 助手（MCP）的入口
├─ web/static/                    前端：页面、样式和脚本
├─ database/base/                 随产品自带的基础数据集目录（只读）
├─ database/external/             其它公开来源与你上传的数据
├─ scripts/                       启动、质量门、评测、打包与冒烟测试脚本
├─ tests/                         自动化测试
├─ automation/                    质量门清单（quality-gates.json），本地与 CI 共用
├─ docs/                          使用说明书与验收用的 测试说明.txt
├─ .agents/skills/                可装进 AI 助手的行为规范
└─ .github/workflows/             CI 与候选发布工作流定义
```

## 使用边界

- 推荐结果依赖当前元数据快照。来源页面或下载链接可能在快照后发生变化。
- `md5sum` 可用于检查下载传输是否完整，不能作为抗恶意篡改的安全证明。
- 外部来源的字段完整度不同；未知字段不会自动视为满足或不满足条件。
- AI 与本地语义重排只调整候选顺序，明确的物种、组织、疾病、技术、时间和原始数据条件仍由规则过滤负责。
- 发现无匹配、歧义或无法可靠解析的条件时，系统可能要求澄清或返回无结果，不会用不满足硬条件的数据填充结果。
