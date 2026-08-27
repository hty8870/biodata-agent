# BioData Agent MCP：安装、API 配置与验收闭环

把 BioData Agent 的本地检索能力接入 Codex 或 Claude Code。可以使用项目脚本自行安装，也可以让当前客户端协助完成环境检查和注册。

项目启动与普通使用见 [README](../../README.md)，接口和开发说明见[开发指南](../../DEVELOPMENT.md)。

> **最快选择**
>
> - Windows 想一次完成：直接看[路线 0：一键闭环](#路线-0windows-一键闭环推荐)。
> - 想省事：直接看[路线 A：让 Codex / Claude Code 帮你安装](#路线-a让-codex--claude-code-帮你安装推荐)。
> - 想亲自动手：Windows 看[路线 B](#路线-bwindows-powershell-手动安装)，macOS / Linux 看[附录 A](#附录-amacos--linux)。
> - 想把整段提示词复制给 Claude Code / Kimi Code / Codex 让它**代你完成全部接入**：看
>   [agent 接入提示词](agent接入提示词.md)。
> - 已装好但不能用：直接看[排错](#7-排错)。

## 安装前先知道

- **默认运行期离线**：不启用 LLM 参数时，BioData MCP 只读取本机项目数据，不需要 API Key，也不会主动调用外部服务。
- **LLM 是可选增强**：`use_llm=true`、`rerank="llm"` 和关键词审核需要 API Key 与网络；一键脚本会把密钥保存到项目外的专用文件，Codex/Claude 配置里只有文件路径。
- **安装阶段通常联网**：首次下载 Python 或 MCP SDK 需要网络；Codex / Claude Code 仍按各自要求安装、登录和联网。
- **项目应保持完整**：`mcp_server.py` 必须和同项目的 `src/`、数据与下载索引放在一起，不能只复制单个脚本。
- **不会污染系统 Python**：下文把依赖装进项目外的专用虚拟环境。
- **运行要求**：Python 3.10+。项目脚本安装固定版本的 MCP Python SDK；Codex 或 Claude Code 请使用各自当前可用的版本，具体命令以 `codex mcp --help` / `claude mcp --help` 的输出为准。
- **版本口径（别混）**：本文里的 `mcp==1.28.1` 指 MCP **SDK**（Python `mcp` 包）的钉版；BioData MCP **服务**自身的版本是 **1.34.0**（`--version` 会打印 `biodata-mcp 1.34.0 | MCP SDK …`）。两者是两个不同的数——安装/升级 SDK 用 1.28.1，看服务版本用 1.34.0。

安装只需要记住两个绝对路径：

1. **服务器目录**：包含 `mcp_server.py` 的 `agent` 目录。
2. **专用 Python**：安装流程创建的虚拟环境里的 `python.exe` 或 `bin/python`。

若启用 LLM，还会多一个项目外路径：

3. **LLM 密钥文件**：Windows 默认是 `%LOCALAPPDATA%\BioDataAgent\secrets\llm.env`。它不在项目中，也不应提交到 Git。

---

## 安装版用户（BioDataAgentMCP.exe，无需 venv / 无需本项目源码）

**如果你装的是安装版**（Setup 安装包，不是 zip 源码包），MCP 服务器已经随包冻结成独立可执行文件，**不需要**再建虚拟环境，也不需要源码目录里的 `mcp_server.py` 和 `src/`。下面路线 0 / A / B 与附录 A 都是**源码版（zip 源码包）**路线；安装版按本节注册即可。

### 1. 记住两个路径

1. **MCP 可执行文件**（默认每用户安装目录）：
   `%LOCALAPPDATA%\Programs\BioData Agent\BioDataAgentMCP.exe`
   若安装时改过目录，以实际为准（开始菜单快捷方式「BioData Agent」的目标路径即可查到）。
2. **本地数据根**：`%LOCALAPPDATA%\BioDataAgent`（账户、上传、日志、本地模型、`config\.env` 都在这里，与安装目录分离，卸载默认保留）。

### 2. 真实协议验收（--selfcheck）

```powershell
& "$env:LOCALAPPDATA\Programs\BioData Agent\BioDataAgentMCP.exe" --selfcheck
```

末行出现 `SELFCHECK_OK tools=19 corpus_total=… download_index_ready=true llm_configured=…` 且退出码为 0 即通过；`--version` 可打印 `biodata-mcp 1.34.0 | MCP SDK …`。

### 3. 注册到客户端（命令直接指向 exe）

```powershell
# Codex
codex mcp add biodata --env PYTHONUTF8=1 -- "$env:LOCALAPPDATA\Programs\BioData Agent\BioDataAgentMCP.exe"
codex mcp get biodata

# Claude Code（默认 user 作用域）
claude mcp add --env PYTHONUTF8=1 --transport stdio --scope user biodata -- "$env:LOCALAPPDATA\Programs\BioData Agent\BioDataAgentMCP.exe"
claude mcp get biodata
```

`command` 直接指向 exe、`args` 为空（exe 本身就是 stdio MCP 服务器）。Codex 桌面端 `Settings → MCP servers → Add server → STDIO` 或 Claude 桌面的 Add server 里，命令填该 exe 的绝对路径即可。

#### 3.1 Kimi Code 等其它支持 MCP 的客户端

Kimi Code（以及任何未在上文列出的 MCP 客户端）按该客户端自己的 MCP 配置文档登记，配置形状与
本目录 [mcp.example.json](mcp.example.json) 一致，只填 **stdio 三要素**：

| 要素 | 安装版填法 | 源码版填法（见路线 B） |
|---|---|---|
| `command` | `%LOCALAPPDATA%\Programs\BioData Agent\BioDataAgentMCP.exe` | 专用虚拟环境的 `python.exe` / `bin/python` 绝对路径 |
| `args` | 空（exe 本身就是服务器） | `["<agent 目录>/mcp_server.py"]` |
| `env` | `PYTHONUTF8=1` | `PYTHONDONTWRITEBYTECODE=1`、`PYTHONUTF8=1`；配 LLM 时加 `BIODATA_LLM_ENV_FILE=<项目外密钥文件>` |

不要臆造客户端专属的 MCP 字段名——以你所用客户端的 `--help` 或官方 MCP 配置文档为准（例如 Codex 用
`codex mcp add`、Claude Code 用 `claude mcp add`，Kimi Code 的登记方式请按其自身文档【请自行探测确认】）。
想让 agent **代你完成全部接入**（定位 exe → selfcheck → 注册 → 装 skill → 配 LLM env → 重启验收），
直接把 [agent 接入提示词](agent接入提示词.md) 复制给它即可。

#### 3.2 安装随包的 skill（推荐）

安装包**随带** `biodata-dataset-discovery` 技能副本（本批起随装），安装后位于
`%LOCALAPPDATA%\Programs\BioData Agent\_internal\.agents\skills\biodata-dataset-discovery`（安装目录以
实际为准【请自行探测确认】）；也可以让本地 Web 服务在线给包：GET
`http://127.0.0.1:8000/api/guide/skill.zip`（端口以实际为准）。把该目录复制到你的 agent 的 skills 目录
即可（Claude Code 用户级为 `%USERPROFILE%\.claude\skills\`），详见 [Skill 安装教程](../Skill安装/Skill_安装教程.md)。

### 4. LLM 配置指向安装版 config\.env

安装版 MCP 与 Web 应用共用同一份配置：**`%LOCALAPPDATA%\BioDataAgent\config\.env`**（不是源码版的 `secrets\llm.env`）。编辑该文件里的 `ENABLE_LLM` / `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` 后重启客户端即可；也可在 Web 应用的设置里配置（写的是同一文件）。默认离线可用，不配 LLM 也能做确定性检索。

---

## 路线 0：Windows 一键闭环（推荐）

项目自带 `scripts\setup_mcp.ps1`。它会依次完成：创建项目外虚拟环境 → 安装固定版本 MCP SDK → 真协议自检 → 隐藏输入 API Key → 写项目外密钥文件并收紧 ACL → 真实 API 最小探测 → 备份客户端配置 → 注册 MCP → 回读验证。

在 PowerShell 中任选一条；脚本路径可从任意目录调用：

```powershell
$Project = (Resolve-Path 'C:\你的路径\agent').Path

# Codex + OpenAI
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$Project\scripts\setup_mcp.ps1" -Client codex -Provider openai

# Claude Code + 智谱 BigModel
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$Project\scripts\setup_mcp.ps1" -Client claude -Provider zhipuai

# 任意 OpenAI-compatible 服务（DeepSeek / Kimi / Qwen / OpenRouter / 本地网关等）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$Project\scripts\setup_mcp.ps1" `
  -Client codex -Provider compatible -BaseUrl 'https://你的接口/v1' -Model '你的模型名'

# 只装离线 MCP，暂不配置 API
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$Project\scripts\setup_mcp.ps1" -Client codex -Provider none
```

脚本只会在终端中隐藏读取 API Key；**不要把真实 Key 写进命令、聊天、截图或工单**。已有 `biodata` 配置时，脚本先备份，再要求确认；在自动化流程里，可以在人工核对过冲突之后再加 `-Force`。API 实测失败时不会注册客户端，并会恢复本轮前的密钥文件；客户端注册中途失败也会恢复配置备份。

只想先看会改哪里，不做任何写入：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$Project\scripts\setup_mcp.ps1" `
  -PlanOnly -Client codex -Provider openai
```

配置成功后重启客户端，按[第 4 节](#4-重启并做客户端验收)验证。默认会跑一次真实 `--llm-check` 探测；**探测失败不会导致整个注册失败**——会降级为警告并仍注册**离线可用**的服务器（确定性检索不需要 LLM），你可稍后修好网络/密钥再重试。要让探测失败变致命（不通就什么都不注册），加 `-RequireLlm`；要完全跳过探测，加 `-SkipLlmCheck`。

---

## 路线 A：让 Codex / Claude Code 帮你安装（推荐）

把下面整段提示词复制给 Codex 或 Claude Code，只替换项目目录与 API 类型。安装流程会先检查环境和已有配置，再调用项目自带的一键脚本；不会修改项目源码。

> 想用**任何**支持 MCP 的 agent（含 Kimi Code）代你完成安装版/源码版的定位 → selfcheck → 注册 →
> 装 skill → 配 LLM → 验收全流程，用更通用的[agent 接入提示词](agent接入提示词.md)（不依赖项目一键脚本）。

```text
请为我安装并验证 BioData Agent 的本地 stdio MCP。

包含 mcp_server.py 的项目目录：
<替换为 agent 目录的绝对路径>

目标客户端：
只安装到当前正在使用的 Codex 或 Claude Code；若无法判断，先说明检测结果。

LLM API 类型：
<none / openai / zhipuai / compatible；compatible 还需 Base URL 与模型名>

请严格遵守以下约束：

1. 项目目录全程只读。不得修改源码、数据、测试、requirements、Git 配置，不得在项目内创建虚拟环境、缓存、密钥或临时文件。
2. 先核对当前客户端的 `mcp --help`，并运行 `scripts/setup_mcp.ps1 -PlanOnly` 展示将写入的项目外路径。不得读取、打印或复述任何现有 Key。
3. 检查是否已有 `biodata`：不存在则继续；存在则只说明是否冲突并询问我，不要展示其中的 env 值，不得擅自加 `-Force`。
4. 无冲突后运行项目自带 `scripts/setup_mcp.ps1`。它必须使用项目外虚拟环境、固定 `mcp==1.28.1`、先跑真协议 `--selfcheck`、再备份客户端配置后注册。
5. 若选择了 LLM API，不要让我把 Key 发到聊天。让我在受信任的本机 PowerShell 中亲自完成脚本的隐藏输入；Agent 只提供已经填好其他参数的一条命令。Key 只能落在项目外 `%LOCALAPPDATA%\BioDataAgent\secrets\llm.env`，客户端配置只保存 `BIODATA_LLM_ENV_FILE` 路径。
6. API 模式默认会跑 `mcp_server.py --llm-check` 真实最小请求；探测失败时脚本降级为警告、仍注册离线可用的服务器（并明确提示 LLM 未通过、稍后重试）。只有我要求"不通就别注册"时才加 `-RequireLlm`（此时失败会恢复旧密钥文件和客户端配置）；只有我明确同意时才用 `-SkipLlmCheck` 完全跳过探测。密钥文件先建限权 ACL 再写入，旧密钥备份只保留最近数份。
7. 不得关闭沙箱、安全审批或使用任何 dangerously-skip/bypass 参数。需要联网安装依赖或写入用户配置时，只申请完成该步骤所需的最小权限。
8. 重启客户端后，确认十九个工具可见：recommend_datasets、get_file_manifest、parse_constraints、browse_datasets、get_dataset_introduction、assess_dataset_fair、lookup_identifier、find_compatible_datasets、assess_feasibility、plan_query_edit、plan_action、build_task_pack、build_reuse_pack、verify_local_assets、provision_dataset、curate_datasets、upload_dataset、biodata_status、biodata_llm_status；确认 biodata_status.ok=true、corpus_total>0、download_index_ready=true。
9. LLM 模式再调用 biodata_llm_status(check_connection=false) 与 biodata_llm_status(check_connection=true)，只汇报 configured/provider/model/connection/error_code，不得回显路径或秘密。
10. 完成后报告：客户端及版本、Python 与 MCP 版本、虚拟环境路径、服务器路径、配置作用域、备份路径、协议与 API 验收结果、重启要求、卸载命令，以及项目 Git 状态是否保持不变。

现在先做只读检查，然后直接推进；只有遇到同名配置冲突、缺少运行环境或需要扩大授权范围时再停下来询问。
```

安装过程中仍可能需要你完成四件事：批准联网安装、批准写入用户级客户端配置、在本机终端隐藏输入 API Key、重启客户端后确认首次工具调用。**Key 输入保留给本人，是为了避免凭据进入聊天或日志。**

---

## 路线 B：Windows PowerShell 手动安装

### 1. 创建专用环境

只修改第一行项目路径，然后把整段复制到 **PowerShell**。无需激活虚拟环境；路径含中文或空格也可以。

```powershell
# 只修改这一行：填写包含 mcp_server.py 的 agent 目录
$Project = (Resolve-Path 'C:\你的路径\agent').Path

$ErrorActionPreference = 'Stop'
$Server = (Resolve-Path (Join-Path $Project 'mcp_server.py')).Path
$Venv = Join-Path $env:LOCALAPPDATA 'BioDataAgent\mcp-venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
$Uv = Get-Command uv -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Force -Path (Split-Path $Venv) | Out-Null

if (-not (Test-Path -LiteralPath $Python)) {
    if ($Uv) {
        # uv 在缺少 Python 3.12 时会自动下载；虚拟环境仍位于项目外
        & $Uv.Source venv --no-project --python 3.12 $Venv
    }
    else {
        $BasePython = $null
        $Py = Get-Command py -ErrorAction SilentlyContinue

        if ($Py) {
            $BasePython = & $Py.Source -3 -c "import sys; print(sys.executable)"
        }
        else {
            foreach ($Name in @('python3', 'python')) {
                $Candidate = Get-Command $Name -ErrorAction SilentlyContinue
                if (-not $Candidate) { continue }

                & $Candidate.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
                if ($LASTEXITCODE -eq 0) {
                    $BasePython = $Candidate.Source
                    break
                }
            }
        }

        if (-not $BasePython) {
            throw '没有找到 uv 或 Python 3.10+。请先按下方说明安装 uv，再重新运行本段。'
        }

        & $BasePython -c "import sys; assert sys.version_info >= (3,10), '需要 Python 3.10+'"
        & $BasePython -m venv $Venv
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "虚拟环境创建失败：$Venv"
}

if ($Uv) {
    & $Uv.Source pip install --python $Python 'mcp==1.28.1'
}
else {
    & $Python -m pip install 'mcp==1.28.1'
}

& $Python -c "import sys, importlib.metadata as md; assert sys.version_info >= (3,10); from mcp.server.fastmcp import FastMCP; print('环境就绪 | Python', sys.version.split()[0], '| MCP', md.version('mcp'))"
if ($Uv) {
    & $Uv.Source pip check --python $Python
}
else {
    & $Python -m pip check
}

Write-Host "Python：$Python"
Write-Host "服务器：$Server"
```

正常结果应包含 `环境就绪` 和 `No broken requirements found`。请继续使用**同一个 PowerShell 窗口**，后面还要用 `$Python` 与 `$Server`。

如果既没有 `uv`，也没有 Python 3.10+，Windows 10/11 可先安装 uv：

```powershell
winget install --id=astral-sh.uv -e
```

安装后重新打开 PowerShell，再运行上面的主安装块。也可按照 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)选择其他安装方式。

### 2. 做真实协议验收

服务器**自带**真协议自检：`--selfcheck` 会用同一个 `$Python` 另起一个子进程，实际完成 `initialize → tools/list → biodata_status → parse_constraints`，**不写项目、90s 兜底绝不无限等**。它比“直接运行脚本看光标是否停住”可靠得多。

```powershell
& $Python $Server --selfcheck
```

通过时最后一行是：

```text
SELFCHECK_OK tools=19 corpus_total=5705 download_index_ready=true llm_configured=<true|false>
```

- 退出码 `0` = 通过、`1` = 失败：协议握手成功后逐项 `[PASS] / [FAIL]` 指出卡在哪一步；握手前就失败（Python 用错 / mcp 版本不兼容 / 超时）则打印单行 `SELFCHECK_FAIL <原因>`。
- 因为自检用 `$Python` 自己 spawn `$Server`，它同时验证了“**这个 Python 能不能起这个服务器**”——路径或依赖错都会当场暴露。
- `corpus_total` 会随数据更新或用户上传变化，只要 `>0` 即可。

> 排错时随时可重跑这一条；也可用 `& $Python $Server --version` 打印服务器版本 / MCP SDK 版本，便于反馈问题。

### 3. 注册到客户端（二选一）

#### 3.1 Codex

先查是否已有同名配置：

```powershell
codex mcp get biodata
```

- 若提示不存在，执行添加命令。
- 若已存在且 Python、脚本路径完全相同，直接跳到验证，不要重复添加。
- 若已存在但路径不同，先确认旧配置可以替换，再按“更新或项目移动”处理。

```powershell
codex mcp add biodata --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONUTF8=1 -- "$Python" "$Server"
codex mcp get biodata
codex mcp list
```

Codex 桌面端也可在 `Settings → MCP servers → Add server → STDIO` 中填写相同命令和参数。保存后按界面提示 **Restart**。Codex 桌面端、CLI 与 IDE 扩展会共享同一 Codex 主机上的 MCP 配置。

#### 3.2 Claude Code

默认推荐 `user` 作用域：个人配置、所有项目可用，不会在 BioData 项目里创建配置文件。

```powershell
claude mcp get biodata
claude mcp add --env PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 --transport stdio --scope user biodata -- "$Python" "$Server"
claude mcp get biodata
claude mcp list
```

Claude Code 作用域区别：

| 作用域 | 适合场景 | 说明 |
|---|---|---|
| `local` | 只在当前项目使用 | 配置保存在用户目录中的当前项目条目；不适合跨项目调用 |
| `user` | **个人使用，推荐** | 所有项目可用，不写入 BioData 项目 |
| `project` | 团队共享 | 在项目根目录创建或更新 `.mcp.json`，会修改项目并触发 workspace trust / MCP 批准 |

如果选择 `project`，把命令中的 `--scope user` 改为 `--scope project`。首次看到 `Pending approval` 时，在可信项目中启动 Claude Code，接受 workspace trust，然后通过 `/mcp` 核对并批准服务器。

#### 3.3 可选：给已安装的 MCP 补配 LLM API

无需重装虚拟环境。下面命令会隐藏读取 Key、写项目外密钥文件、真实探测 API、备份并刷新现有 `biodata` 注册：

```powershell
# 按实际客户端/API 选择；也可用 zhipuai 或 compatible
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$Project\scripts\setup_mcp.ps1" `
  -Client codex -Provider openai -SkipInstall
```

密钥文件只使用一组通用变量，避免不同服务商维护多套配置：

| 变量 | 说明 |
|---|---|
| `ENABLE_LLM=true` | 允许 LLM 功能；每次工具调用仍需显式传 `use_llm=true` 或 `rerank="llm"` |
| `LLM_PROVIDER` | `openai-compatible` 或 `zhipuai` |
| `LLM_API_KEY` | 真实 Key，仅存在项目外密钥文件中 |
| `LLM_BASE_URL` | 服务商的 API 根地址；服务器会补 `/chat/completions` |
| `LLM_MODEL` | 服务商实际可用模型名 |
| `LLM_TIMEOUT` | 请求超时秒数，默认 60 |

推荐统一使用 `LLM_*` 变量；它们优先于 `OPENAI_*` / `ZAI_*` 等兼容别名，避免宿主机遗留的全局 provider 变量误覆盖 MCP 专用配置。对同一个变量，来源优先级为：**MCP 进程显式值 > `BIODATA_LLM_ENV_FILE` 指向的项目外文件 > 程序默认值**；一旦设置了 `BIODATA_LLM_ENV_FILE`（须为存在的绝对路径），它就是**独占**的配置来源，此时**不再读取**项目本机 `.env` / `.env.zhipu`（未设置该变量时，才回落到项目 `.env` / `.env.zhipu`）。`.env.example` 只是模板，不再作为运行时配置读取。

注册前或排错时，可在同一 PowerShell 临时指定文件并实测；输出是脱敏 JSON：

```powershell
$env:BIODATA_LLM_ENV_FILE = Join-Path $env:LOCALAPPDATA 'BioDataAgent\secrets\llm.env'
& $Python $Server --llm-check
Remove-Item Env:BIODATA_LLM_ENV_FILE
```

`connection="success"` 且退出码为 0 才表示真实 API 可用。`missing_key`、`authentication_failed`、`rate_limited`、`endpoint_or_model_error`、`network_error`、`invalid_response` 是稳定的脱敏错误码；不会打印服务商原始错误正文或 Key。

#### 3.4 Kimi Code 等其它客户端（stdio 三要素）

Kimi Code（以及任何未在上文列出的 MCP 客户端）按该客户端自己的 MCP 配置文档登记，配置形状与
本目录 [mcp.example.json](mcp.example.json) 一致，只填 **stdio 三要素**：`command` = venv Python
绝对路径（源码版）或 `BioDataAgentMCP.exe`（安装版，args 为空）；`args` = `["<agent 目录>/mcp_server.py"]`
（源码版）；`env` = `PYTHONDONTWRITEBYTECODE=1`、`PYTHONUTF8=1`（配 LLM 时加
`BIODATA_LLM_ENV_FILE=<项目外密钥文件>`）。不要臆造客户端专属的 MCP 字段名——以该客户端的 `--help`
或官方 MCP 配置文档为准（Kimi Code 的登记方式请按其自身文档【请自行探测确认】）。登记后重启客户端，
按第 4 节做客户端验收。想让 agent **代你完成全部接入**，直接把
[agent 接入提示词](agent接入提示词.md) 复制给它即可。

### 4. 重启并做客户端验收

1. 真正重启 Codex 桌面端 / IDE 扩展，或退出并重新进入 Codex CLI / Claude Code。
2. 在交互界面输入 `/mcp`，确认 `biodata` 已连接。
3. 若出现工具批准提示，核对 Python 和 `mcp_server.py` 的绝对路径后再批准；不要关闭全局安全审批。
4. 发送下面这句话：

```text
必须调用 biodata MCP 的 biodata_status，不要使用 shell 或 web。只告诉我 ok、corpus_total、download_index_ready、llm.configured 和已发现的工具名；不要输出任何路径或环境变量值。
```

安装成功的最低标准：

- `biodata` 在客户端中显示启用或已连接；
- 十九个工具均可发现；
- `ok: true`；
- `corpus_total > 0`；
- `download_index_ready: true`。

可选做一次完整功能验收：

```text
只使用 biodata MCP：
1. 用 parse_constraints 解析“2015 年以来的人类肺癌单细胞数据，要有 FASTQ”；
2. 用相同查询调用 recommend_datasets，top_k=1；
3. 汇报第一条结果的 dataset_uid。不要下载文件。
```

若已配置 API，再做一次明确的真实检查：

```text
只使用 biodata MCP 调用 biodata_llm_status，check_connection=true。只告诉我 configured、provider、model、connection、error_code；不得输出路径或秘密。
```

---

## 5. 安装后怎么用

十九个工具（与网页端能力对齐；其中 16 个只读 + 3 个写盘：`upload_dataset` 写外部库、`provision_dataset` 写调用方指定目录、`curate_datasets` 管护外部库 upload_* 与回收站）：

| 工具 | 作用 | 关键入参 |
|---|---|---|
| `biodata_status` | 健康自检、数据来源计数、下载索引状态与链接快照 | 无 |
| `parse_constraints` | 只解析查询，展示来源、时间、硬约束和弃权状态 | `query`，可选 `sources`、`auto_parse` |
| `recommend_datasets` | 自然语言查询 → 硬过滤、排序后的数据集候选 | `query`，可选 `sources`、`auto_parse`、`top_k`、`recall`、`use_llm`、`rerank`、`rerank_top_n`、`rerank_audit`、`strategy`、`auto_allow_llm`，以及**分面细化 / 忽略已识别的查询条件 / 把未标注的也纳入 / 发表时间范围**：`facet_filters`、`suppressed_constraints`、`lenient_dims`、`date_from`、`date_to`（见 5.1、5.2） |
| `browse_datasets` | **全库浏览**（对齐网页目录页）：所有来源并列的数据集 + 物种/平台/来源/年份分面，按维度轻过滤 + 分页。无需 query | 可选 `species`、`platform`、`source`、`year`、`limit`、`offset` |
| `get_dataset_introduction` | **单数据集确定性介绍**（只整理现有元数据、不调用 LLM） | `uid`（首选）/ `url` / `name`（可配 `source` 消歧） |
| `assess_dataset_fair` | **单数据集 FAIR 元数据自检 + 投稿数据可用性说明（DAS）**（确定性、离线、不调用 LLM） | `uid`（首选）/ `url` / `name`（可配 `source` 消歧） |
| `lookup_identifier` | **标识符精确反查**：UUID / E-XXXX-N / DOI 直达本目录记录；GEO/SRA 号如实告知不在本目录、指向原库（不静默返回 0） | `identifier`（标识符字符串） |
| `find_compatible_datasets` | 给一数据集找**元数据兼容**（同物种 + 兼容 chemistry/platform）的其它数据集，**始终附「兼容≠可整合」caveat** | `uid`，可选 `limit`（默认 20，范围 1-100） |
| `assess_feasibility` | **可行性概览**：研究问题 → 候选数 / 总细胞量下限 / 物种·平台·年份·来源分布 / 可下载率 / 缺口 | `query`，可选 `sources`（拼错来源报 `bad_source`，不静默判空） |
| `build_task_pack` | **一句话任务包**：一次检索 → 结果清单 + 下载脚本 + FAIR 自检 + 引文，四件套口径一致。两步：`mode='preview'` 先看清单，确认后 `mode='build'` 才产文件 | `mode`、`query`，build 时另需 `selected_uids` 与预览返回的指纹 |
| `plan_query_edit` | **接着改条件**：一句「换成小鼠 / 再加一条：要有 FASTQ / 去掉组织限制」→ 一次具体改动（只规划、不检索） | `query`、`utterance`，以及**原样回传**上一次返回里的 `current_filters`、`resolution` |
| `plan_action` | **一句话要做什么**：把「把前 5 条打包 / 存成压缩包给我 / 这几个导出成引文」归一化成封闭动词表里的一个动作（**只出计划、不执行**；判定依据 `quoted` 保证是原话字面子串；用户说「不要打包」会判成 `none`） | `utterance`，可选 `has_results`、`result_total` |
| `build_reuse_pack` | **复用出处清单**：选中数据集 → 投稿材料（英文出处段 + 数据集清单 + 待核实项 + RIS/BibTeX 导出） | `uids`（数据集 UID 列表） |
| `verify_local_assets` | **本地资产台账**：扫本地目录树，按 md5 与本目录文件清单比对（**只读、纯校验和、不联网、不改文件**） | `directory`（本地目录），可选 `max_files` |
| `provision_dataset` | **按需真下载**（写工具 ②）：把数据集文件下载到**你指定的目录**（https+白名单、md5/大小核对、`.corrupt` 留证据；**只在你给定 `dest_dir` 时写盘，绝不写 `database/`**；默认 `scope=primary` 只下主文件，可先 `dry_run=true` 预演；台账回写走 CLI `scripts/record_provision_results.py`） | `dataset_uid`、`dest_dir`（**绝对路径**），可选 `scope`、`max_files`（默认 50、硬上限 500）、`dry_run`、`include_flagged` |
| `curate_datasets` | **对话式数据库管护**（写工具 ③）：清点 / 本地导入（内容去重）/ 联网搜官方源 / 回收站式删除 / 恢复；管护对象限外部库 `upload_*` 文件（见 5.5） | `action`（list/import/search_online/remove/restore），可选 `query`、`source`、`species`、`limit`、`filename`、`payload_json`、`confirm_token`、`force`、`dry_run`（默认 true=预览） |
| `upload_dataset` | **上传数据集 JSON 进外部库**（对齐网页上传；**写工具 ①**，写入只落 `database/external/`、绝不碰冻结基准，即时可检索） | `records`（结构化数组/对象）**或** `path`（本地 .json 文件）二选一；可选 `filename`、`source`（见 5.3） |
| `get_file_manifest` | 根据推荐/浏览结果的 UID 获取文件级下载清单、大小、MD5、FASTQ 等 | `dataset_uid` |
| `biodata_llm_status` | 脱敏检查 LLM 配置；默认离线，显式要求时做真实 API 最小探测 | `check_connection=false/true` |

一次完整使用链路可以这样要求：

```text
使用 biodata MCP 查找“人类肺癌单细胞数据，要有 FASTQ”，先解释解析出的约束，再返回前 3 个候选。等我选择后，再用 dataset_uid 获取文件清单；不要提前下载文件。
```

`auto_parse=true` 默认开启：查询直接写「CELLxGENE 近三年的人类肝脏数据」即可同时解析来源、相对时间和生物实体；来源专名会在进入规则解析前安全移除，Web 与 MCP 使用同一套规则。未点名来源时仍使用文件级信息最完整的基础语料。需要手工固定范围时传 `sources`；外部来源字段中的 `unknown` 表示信息缺失，不等于不满足条件。`sources` 写错会以 `bad_source` 报错。

查询支持**相对时间**：如「近 3 年」「今年 / 去年」「2010 年代」会自动换算成绝对起止区间；「近几年」这类**年数不明确**的会安全弃权并提示改成明确年数。每条 `candidates` 还附 `introduction`（与网页同口径、只整理现有元数据、不调用 LLM）和 `caveats`（数据一致性建议信号，如物种标注与描述文本不一致；空列表＝无问题）。

文件清单可能较长。若只想了解规模，先让助手只汇报 `counts`、`primary` 和 FASTQ 情况，不要在回复中展开全部 `files`。

### 5.1 检索质量杠杆（可选，默认全关＝确定性）

`recommend_datasets` 还接受下列可选参数。**不传时仍保持离线、无需 API key；自动来源解析只在原句明确点名来源时收窄。** 返回的 `interpretation` 和 `meta.search_trace` 会说明解析结论、实际执行步骤与回退；兼容字段 `recall_used/rerank_used` 表示请求形态，`recall_applied/rerank_applied/llm_applied` 表示实际采用。

| 参数 | 取值 | 作用 | 代价 |
|---|---|---|---|
| `recall` | `off`(默认) / `cross_encoder` / `dense` | 对已命中的候选集做**本地向量重排**，对长句、语义化的查询通常更贴合 | **离线、无需 key**。⚠️ 在 **MCP** 里启用需两步（见下「MCP 里怎么开 recall」）：装本地模型 + 服务器 env 设 `BIODATA_MCP_RECALL`。未满足 → **自动回退到默认的规则排序**（`meta.notes` 会说明原因）、绝不卡住 |
| `use_llm` | `false`(默认) / `true` | 让服务器端 LLM 把结果润色成自然语言 | **需 API key + 联网、结果非确定** |
| `rerank` | `off`(默认) / `llm` | 用 LLM 对候选池重排 | **需 API key + 联网、结果非确定** |
| `rerank_top_n` | 整数 / 省略 | 重排池大小 | — |
| `rerank_audit` | `false`(默认) / `true` | **仅 `rerank="llm"` 时生效**：审核规则抽取的关键词是否正确完整，不完整则把原句改写成规则更易解析的句式、**重走一次检索**并择优（改写更差则退回原句）。存活集**非空**时在那次重排 LLM 调用里顺带审核；存活集**为空**（无匹配/规则弃权）时脱离重排、独立审核一次尝试改写救回 | **需 API key + 联网、结果非确定**；缺 key 时重排本身回退原序 → 审核不触发。决策 + 改写回显见 `meta.audit`（`mode` = `rerank` 顺带 / `empty` 独立） |
| `strategy` | `fixed`(默认) / `auto` | `auto` 同时看候选压力与自由语义量：紧查询走规则；普通宽查询本地优先；复杂查询在授权后可走本地语义→LLM 精排 | MCP 本地后端仍须启动预热；默认不自动用 LLM |
| `auto_allow_llm` | `false`(默认) / `true` | 只为 `strategy=auto` 授权复杂查询自动使用已配置 LLM | `true` 仍需有效 API 配置与网络；与只控制文字润色的 `use_llm` 不同 |
| `auto_parse` | `true`(默认) / `false` | 自动识别查询里的高辨识度数据来源专名；时间与实体始终走共享规则解析 | 来源排除语义会安全跳过自动收窄，避免意图反转 |

> **给通过 Agent 调用的你**：如果你是 Claude Code / Codex 这类**本身就是 LLM** 的助手，通常**不需要** `use_llm`——直接读结构化 `candidates`（含 `raw_data_status`、`sample_size`、`reason`、`matched_fields` 等字段）自己组织叙述，更快、更省、且不引入非确定性。`recall="cross_encoder"` 是唯一**既提升相关性又保持离线、无需 key** 的杠杆，值得优先考虑。

**MCP 里怎么开 `recall`（stdio 稳定性所需，务必按此做）**：MCP 服务器由客户端以管道 stdio（无终端）启动，torch/CUDA 在这种进程里**首次加载会死锁**。因此服务器只在**启动时**（且仅当配置了 `BIODATA_MCP_RECALL`）用 CPU 预热本地重排模型；之后每次调用只打分、秒回。启用两步：

1. **装依赖 + 本地模型**（一次性、需联网）：recall 后端需要 `torch` + `sentence-transformers`，它们**不在** mcp-venv 的基础依赖里。必须往 §1 建好的 **同一个 mcp-venv** 里补装这两个库、再下载模型；只下模型不装依赖，服务器启动预热会失败并**静默回退规则序**（recall 永不生效）。用 §1 的 `$Python`（新开终端就按下面重建这两个变量，`$Project` 改成你的 agent 目录）：

   ```powershell
   $Project = (Resolve-Path 'C:\你的路径\agent').Path
   $Python  = Join-Path $env:LOCALAPPDATA 'BioDataAgent\mcp-venv\Scripts\python.exe'
   & $Python -m pip install -r (Join-Path $Project 'requirements-embeddings.txt')
   & $Python (Join-Path $Project 'scripts\fetch_embedding_model.py') --cross-encoder
   ```
2. **在 MCP 服务器配置的 env 里加** `BIODATA_MCP_RECALL=cross_encoder`（与 `PYTHONUTF8` 放一起：Codex 手工配置见 §9 的 `[mcp_servers.biodata.env]`；Claude `.mcp.json` 见 §9 的 `"env"`；CLI 注册可再加一个 `--env BIODATA_MCP_RECALL=cross_encoder`），然后**重启客户端**。

**代价**：开启后服务器启动多花约 10-20s 预热 → 把 Codex 的 `startup_timeout_sec` 调到 ≥30（§9）。**不开也完全能用**：`recall` 请求会如实**回退规则序**并在 `meta.notes` 说明，服务器**绝不卡死**。**CLI / Web 有真终端、无此限制**，可直接传 `recall="cross_encoder"`。

示例（服务器已按上面设好 `BIODATA_MCP_RECALL=cross_encoder` 并重启后；未开则自动回退规则序、不报错）：

```text
用 biodata MCP 的 recommend_datasets 查“人类肺癌单细胞，要有 FASTQ”，top_k=5；返回后顺便告诉我 meta.recall_used 与 meta.deterministic（应显示 cross_encoder / false）。
```

### 5.2 浏览、单条介绍、分面细化与时间范围（可选，对齐网页「数据细化」）

除按一句查询推荐外，MCP 还提供两个只读工具与四类精细化入参，覆盖网页端的浏览与细化能力：

- **`browse_datasets`（全库浏览）**：不需要 query，直接看目录里有什么。返回所有来源并列的记录 + 物种/平台/来源/年份分面，可按维度轻过滤并分页。全库 5000+ 条，故一次只回一页（`limit` 默认 50、上限 100——与网页 `/api/datasets` 同一常量源；`offset` 分页），分面按过滤后的集合计数；**某个维度上没有标注的数据集不计入该维度的任何分面**（年份缺失单独计在 `unknown_year_count`），所以分面计数之和可能小于 `total`——差额是「该维度未标注」，不代表不满足。记录精简、不含大段介绍。

  ```text
  用 biodata MCP 的 browse_datasets 看看 CELLxGENE Discover 里 2023 年的人类数据有多少，先只给我 total 和前 5 条名称。
  ```

- **`get_dataset_introduction`（单条介绍）**：拿到 `dataset_uid`（来自 recommend / browse）后取该数据集的确定性介绍（只整理现有元数据、不调用 LLM，与网页同口径）。传 `uid`（首选）/ `url` / `name`（可配 `source` 消歧）。

- **`recommend_datasets` 的四类精细化入参**（默认一个都不传时，返回结果与完全不带这些参数的调用一致）：

  | 入参 | 作用 | 用法 |
  |---|---|---|
  | `facet_filters` | 在**当前命中集**上按维度精确收窄（对齐网页「数据细化」侧栏） | 传 `[{"dim": 维度, "value": 分面键}]`；分面键取返回的 `facets[].values[].value`（物种/组织/疾病为小写归一键，如 `homo sapiens`）。跨维度 AND、同维度多值 OR |
  | `suppressed_constraints` | 忽略查询里已识别出的某个筛选条件 → 检索前放宽该维度（对应网页「查询条件」标签上的「忽略」按钮） | 传极性 `filter_id`（`include:<dim>` / `exclude:<dim>` / `raw:required` / `raw:forbidden` / `date:range`），取自返回的 `query_constraints[].filter_id` |
  | `date_from` / `date_to` | 发表时间范围（ISO `YYYY-MM-DD`，含端点） | 显式传入优先，覆盖从 query 解析出的相对时间；给了就必须是真实存在的日期，非法或 from>to 倒挂会报错点名（与网页端同口径） |
  | `lenient_dims` | 把「未标注」的也纳入（对应网页结果区的「也纳入」按钮）：对这些维度上**字段为空**的记录视作通过（无法核验≠不匹配），已知不同值仍排除 | 传维度名列表（`species`/`tissue`/`disease`/`platform`/`assay`/`modality`），取自返回的 `coverage_caveats[].dim`。响应回显 `coverage_caveats`（满足其它条件但某维未标注的记录计数，按来源分组）与 `applied_lenient`。与 `suppressed_constraints` 区别：suppress 整维放宽、lenient 只纳未标注的 |

  响应新增回显：`query_constraints`（本次从查询里识别出的筛选条件 + 各自 `filter_id`）、`applied_facets` / `applied_suppressed`（本次真正生效的细化 / 放宽）。典型「先推荐再收窄」链路：

  ```text
  用 biodata MCP 查"人类肺癌单细胞，要有 FASTQ"，top_k=20；从返回的 facets 里挑"组织=lung"回传 facet_filters 再收窄，只报收窄后的 result_total 和前 3 条。
  ```

  > 这些入参与网页端 `/api/recommend` 使用同一套校验与检索逻辑；MCP 与网页对同一句查询、同一细化操作给出一致结果。**不传这些参数时，返回结果与不带它们的调用完全一致。**

### 5.3 上传数据集（`upload_dataset`，对齐网页上传）

把一份数据集 JSON 摄取进**外部平台库** `database/external/`，摄取后即时可被 `browse_datasets` /
`recommend_datasets(sources=[…])` 检索到，成为一个**可勾选的来源**。它与 Web `/api/upload` 共用同一套
摄取逻辑，逐条打上来源标签、给出可读的校验提示。

- ⚠️ **这是本服务器三个写盘工具之一**（另两个是 `provision_dataset`，写你指定的下载目录；`curate_datasets`，管护外部库，见 5.5）。
  写入**只落** `database/external/`、**绝不**碰冻结基准
  `database/base/`（`database/base/` 随产品一起发布、保持只读；你的上传不会改动它）。与 16 个只读工具不同，
  本工具写盘且文件名带时间戳，故**非确定性**（同一输入两次上传落成两个文件）。
- 输入 `records` 与 `path` **二选一**：
  - `records`：**直接传结构化 JSON**——记录数组 `[ {…}, … ]`（首选）或包裹对象 `{ "records": [ {…} ] }`。
    **不要传 JSON 字符串**（会被 MCP 框架预解析、与结构类型冲突）；直接传数组/对象即可。
  - `path`：本地 `.json` 文件路径（便捷，直接指一个已有文件）。
  - 可选 `filename`（落盘名，自动加 `upload_<时间戳>_` 前缀，须以 `.json` 结尾）、`source`（归属来源名）。
- 返回：`filename` / `saved_to`（`database/external/upload_…json`）/ `record_count` / `sources`（逐条打标计数）/
  `warnings`（缺 dataset_name、物种非通用名等）/ `next`（怎么查到刚上传的数据）。

```text
用 biodata MCP 的 upload_dataset 把这几条数据集元数据上传，source 填"我的实验室"，然后用 browse_datasets(source="我的实验室") 确认它们已入库。
```

### 5.4 按需下载数据集文件（`provision_dataset`）与调用日志

`provision_dataset` 把数据集文件**真正下载到你指定的目录**（对齐 CLI `scripts/provision_dataset.py`）：

- ⚠️ **联网 + 写盘**工具（写工具 ②）。**只在你给定 `dest_dir`（必须是绝对路径）时写盘，绝不写
  `database/`**；只下 https 且主机在白名单内的文件；md5/大小核对不符的文件改名 `.corrupt` 留证据。
- 默认 `scope=primary` **只下 1 个代表性主文件**（不含 FASTQ）；`scope="all"` 才下全部文件。
  单次文件数默认上限 50（硬上限 500），超限**报错**而不是静默截断。
- 建议先 `dry_run=true` 拿到计划（下哪些文件、多大）念给用户确认，再 `dry_run=false` 真下。
- 下载结果想回写活台账：用 CLI `scripts/record_provision_results.py`（本工具自己不写台账）。

```text
用 biodata MCP 的 provision_dataset 把这个数据集先 dry_run 看看要下什么；确认后下载到 D:\data\proj1（dest_dir 用绝对路径）。
```

**调用日志在哪、怎么关**：每次 MCP 工具调用都会在本机追加一行脱敏 JSON 到
`<项目目录>/.userdata/mcp_calls.jsonl`（该目录已被 git 忽略、不会入库；日志**不联网、不外传**）。
每行记录：时间戳（UTC）、工具名、参数摘要、耗时、是否报错与错误码。**你的查询原话会被记录**
（这是判断工具该往哪改的核心证据）；api key / token / 密码类字段**一律不落盘**。
关闭方法：在客户端 MCP 配置的 `env` 里加 `BIODATA_MCP_CALL_LOG=off` 并重启客户端；
日志写失败只会静默跳过、绝不影响工具调用本身。想直接删除该文件也可以，不影响任何功能。
汇总统计（调用总数、工具分布、含 FASTQ/文件类型等文件级约束的 query 占比）：

```powershell
& $Python scripts\summarize_mcp_calls.py        # 加 --json 输出机器可读结果
```

### 5.5 对话式数据库管护（`curate_datasets`）

`curate_datasets` 用一句话管护**自己上传的数据**（写工具 ③；对齐网页 `/api/curate/plan` + `/api/curate/apply`
与 CLI `scripts/curate_datasets.py`，三处共用同一套逻辑）：

- ⚠️ **写盘/联网边界**：默认 `dry_run=true` 只返回**预览 + confirm_token**（不落盘；`search_online` 的预览会
  真实联网查官方源并记一行请求账本 `.userdata/curate_net_ledger.jsonl`，不记秘密）。`dry_run=false` 必须
  回传 `confirm_token` 才真执行；token 与内容指纹不符 → `token_mismatch`，**一个字节都不写**。
- 动作：`action="list"` 清点外部库与回收站（纯只读）；`import` 导入本地数据集 JSON（`payload_json` 传
  JSON 文本；内容 hash 去重，撞重默认拒绝、`force=true` 覆盖）；`search_online` 联网搜官方源（`query` 必填，
  `source` 默认 arrayexpress；apply 时把 plan 返回的整个 JSON 原样作为 `payload_json` 回传）；`remove`
  把上传文件**移入回收站**（`.userdata/recycle/`，可逆、不是真删除）；`restore` 移回。
- 管护对象**仅限** `database/external/` 的 `upload_*` 文件（你自己上传或联网入库的）；官方五源快照
  与冻结基准 `database/base/` 不可经此删改（报 `not_curatable` / 结构性不可达）。

```text
用 biodata MCP 的 curate_datasets 先 list 看看我上传过什么；把 upload_20260801_…_my.json 删掉（先预览，我确认后再执行）。
```

---

## 6. 更新、项目移动与重复安装

重复运行环境安装块会复用同一个虚拟环境。升级 MCP **SDK**（Python `mcp` 包）前应先查看项目的已验证版本；本教程当前固定 SDK 为 `mcp==1.28.1`（BioData MCP **服务**版本是 1.34.0，见「安装前先知道」的版本口径）。

如果项目移动了位置，客户端中保存的绝对路径会失效。先重新运行第 1 节的变量与环境安装块，使 `$Python` 和 `$Server` 指向当前真实路径；再查看旧配置，确认后移除并重新添加：

```powershell
# Codex
codex mcp get biodata
codex mcp remove biodata
codex mcp add biodata --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONUTF8=1 -- "$Python" "$Server"

# Claude Code（本教程默认 user 作用域）
claude mcp get biodata
claude mcp remove --scope user biodata
claude mcp add --env PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 --transport stdio --scope user biodata -- "$Python" "$Server"
```

若手工编辑配置，务必先在原目录创建时间戳备份，并只修改 `biodata` 对应段；不要覆盖整个配置文件。

---

## 7. 排错

| 现象 | 原因或快速检查 | 处理 |
|---|---|---|
| 不确定从哪查起 | 需要一条命令定位全链路 | 先跑 `& $Python $Server --selfcheck`：一步串起 Python → 启动 → 协议 → 数据；逐项 `[FAIL]` 或单行 `SELFCHECK_FAIL <原因>` 指出断在哪一环 |
| 找不到 `py` / `python` | 机器没有 Python launcher，属于常见情况 | 优先安装并使用 `uv`；不要把不存在的别名写进 MCP 配置 |
| Python 版本过低 | `<python> --version` 低于 3.10 | 用 uv 创建 Python 3.12 环境，或换用 Python 3.10+ 后重建虚拟环境 |
| `No module named mcp` | MCP 装到了另一个 Python | 用 `codex mcp get biodata` / `claude mcp get biodata` 显示的同一个 Python 重新安装并做 import 检查 |
| `No module named dataset_recommender` | `mcp_server.py` 被单独搬走或项目不完整 | 恢复完整目录结构；服务器脚本应与项目的 `src/` 和数据放在一起 |
| `biodata` 已存在 | 同名配置已经登记 | 用 `codex mcp get biodata` / `claude mcp get biodata` 比较 command/args；相同则跳过，不同才移除重加 |
| `Connection closed` / `failed` | Python 或脚本绝对路径错误，或服务启动异常 | 检查两个路径是否存在；先重跑 `--selfcheck` 定位，再重启客户端 |
| Claude 显示 `Pending approval` | 项目级 `.mcp.json` 尚未获得信任 | 在该项目中交互启动 Claude，接受 workspace trust，并在 `/mcp` 中批准 |
| 非交互调用提示 `user cancelled` | 工具调用需要用户批准，但非交互进程无法弹出审批 | 在交互客户端完成首次批准；不要用危险的全局绕过参数 |
| Codex 列表显示 `Auth: Unsupported` | 本地 stdio 服务器不使用 OAuth | 若状态为 enabled 且工具可调用，这不是故障 |
| 修改后看不到服务器 | 客户端尚未重新加载配置 | 真正 Restart 桌面端或扩展；CLI 退出后重新进入，再用 `/mcp` 检查 |
| Codex 启动超时 | MCP 初始化超过启动时限 | 先跑 `--selfcheck` 定位；手工 TOML 中可按实测增加 `startup_timeout_sec` |
| 首次工具调用超时 | 工具执行超时，不是启动超时 | Codex 手工配置增加 `tool_timeout_sec`；Claude 项目 JSON 可设置 `timeout` |
| `corpus_total=0` | 项目数据不完整或路径指错 | 检查完整项目和数据目录，不要反复重装 MCP SDK |
| `download_index_ready=false` | 下载索引缺失或未就绪 | 补齐项目下载索引；这不是 Python 依赖问题 |
| `llm.configured=false` / `missing_key` | 客户端未传 `BIODATA_LLM_ENV_FILE`，或文件中没有有效 Key | 重跑一键脚本；确认客户端配置只有密钥文件路径，重启后先调 `biodata_llm_status(false)` |
| `external_env.exists/readable=false` | 路径写错、文件移动或 ACL 不允许当前用户读取 | 重跑一键脚本生成；不要把 Key 改写进 TOML/JSON 绕过检查 |
| `authentication_failed` | Key 无效、过期或服务商不匹配 | 在服务商控制台核对 Key；不要把 Key 发到聊天或日志 |
| `endpoint_or_model_error` | Base URL 不是 Chat Completions 兼容根地址，或模型名不可用 | 核对服务商文档后用 `-Provider compatible -BaseUrl ... -Model ...` 重配 |
| `network_error` | 代理、证书、防火墙、DNS 或网络不可达 | 先在同一 Python 下重跑 `--llm-check`；检查标准 HTTP(S)_PROXY 与证书配置 |
| `rate_limited` | 额度不足或请求被限流 | 查看服务商额度/限速，稍后重试 |
| JSON / TOML 解析失败 | 手工配置引号错误或重复同名段 | 恢复备份，优先改用客户端 CLI 注册 |
| pip / uv 下载失败 | 安装阶段的网络、代理、证书或镜像问题 | 检查网络；只使用自己信任的镜像。服务器运行期本身不需要联网 |
| 下载链接后来不可用 | 外部数据源在快照后发生变化 | 查看 `links_snapshot`；URL 快照不是当前可下载性的永久保证 |

**错误返回约定**：**非法请求**——空 `query`、`query` 含控制/不可见字符（NUL、零宽空格、双向控制符）或纯符号/emoji 无检索内容或过长（>2000 字）、枚举越界（`recall` / `rerank` / `strategy`）、非正或超上限（>100）`top_k` / `rerank_top_n`、非严格整数（拒绝 `"3"` / `true` 强转）、未知参数名（如把 `top_k` 写成 `topk`）、未知或空/空白 `sources` 来源名、错误 `dataset_uid`、`browse_datasets` 的 `limit` / `offset` 非法（非正 / 负 / 超上限 100，与网页 `/api/datasets` 同源常量）、`get_dataset_introduction` 未给 uid/url/name 或查不到、name 命中多条同名且 source 消歧失败（`ambiguous_name`，消息附各候选 uid，改用 uid 重调）、`upload_dataset` 未给 `records`/`path`（`empty_input`）或二者同给（`bad_param`）或 `path` 不存在（`not_found`）或超 64 MB（`too_large`）或文件名非 `.json`（`bad_file`）或 `path` 文件非 UTF-8 / 非 JSON / 无记录（`bad_encoding` / `invalid_json` / `no_records`）、`provision_dataset` 空 uid（`empty_uid`）/ 清单外 uid（`unknown_uid`）/ `dest_dir` 为空或非绝对路径（`bad_out_dir`）/ `dest_dir` 落 `database/base` 等受保护区（`protected_out_dir`）/ `scope` 越界或计划文件数超 `max_files`（`bad_param`）/ 计划无主机白名单（`no_allowed_hosts`）`curate_datasets` 未知动作（`bad_action`）/ apply 缺 `confirm_token` 或缺必传参（`bad_param`）/ 文件不存在（`unknown_file`）/ 非 upload_* 命名空间（`not_curatable`）/ token 不符（`token_mismatch`，零写入）/ 内容整集重复且未 force（`duplicate_content`）/ 源未注册（`source_not_registered`）/ 联网失败（`network_error`）/ 零候选（`no_candidates`）——以 MCP **协议错误位** `isError=true` 返回，消息形如「Error executing tool …: bad_param: …」（含机器码 `empty_query` / `bad_query` / `bad_param` / `bad_source` / `bad_uid` / `empty_key` / `not_found` / `ambiguous_name` / `empty_input` / `too_large` / `bad_file` / `bad_encoding` / `invalid_json` / `no_records` / `empty_uid` / `unknown_uid` / `bad_out_dir` / `protected_out_dir` / `no_allowed_hosts` / `bad_action` / `not_curatable` / `token_mismatch` / `duplicate_content` / `source_not_registered` / `network_error` / `no_candidates`；未知参数/类型错误则为 pydantic 的「Extra input」「Input should be a valid integer」），调用方可直接依赖错误位判断调用是否合法。`sources=[]`（空列表）与省略等价，表示不按来源过滤、用默认基础语料，属正常返回而非报错。**非法时间表达**（`近0年`/`近-1年`、`2020年13月`、`2020年2月30日`、`2020年和2022年`）走**业务弃权**（`understood.abstain=true` + 原因），属正常返回而非报错。`dataset_uid` 前后空格会自动清理。`top_k` 硬上限 100（防止单次返回过大、占满对话上下文）。**合法请求的业务结果**（本目录内无匹配 / 条件不足时的安全弃权 / 需要你补充澄清）仍是正常返回、`ok=true`、`isError=false`——那不是错误。`biodata_llm_status` 把缺 Key、鉴权、限流、端点/模型和网络问题作为**诊断结果**返回稳定 `error_code`，不把可诊断的配置问题升级为协议错误。导入失败、协议异常、进程退出等**进程级**故障仍可能断开连接，因此不承诺“服务器永不崩”。

---

## 8. 卸载

先注销客户端配置：

```powershell
# 按实际客户端选择一条
codex mcp remove biodata
claude mcp remove --scope user biodata
```

这不会删除 BioData 项目，也不会删除虚拟环境。如果确认以后完全不用，再自行删除：

```powershell
$Venv = Join-Path $env:LOCALAPPDATA 'BioDataAgent\mcp-venv'
Remove-Item -LiteralPath $Venv -Recurse -Force
```

若也要删除 LLM 配置，先核对路径，再单独删除密钥文件；不要删除整个 `%LOCALAPPDATA%`：

```powershell
$Secret = Join-Path $env:LOCALAPPDATA 'BioDataAgent\secrets\llm.env'
Write-Host $Secret
Remove-Item -LiteralPath $Secret -Force
```

删除前先核对 `$Venv` / `$Secret` 输出，确保目标确实位于 `BioDataAgent` 专用目录。若曾使用 `project` 或 `local` 作用域安装 Claude MCP，卸载时把 `--scope user` 改成对应作用域。时间戳 `.bak.*` 备份在你确认新配置稳定前应保留；回滚时先退出客户端，再把对应备份复制回原配置路径。

---

## 9. 高级备用：手工配置

仅当客户端 CLI 不可用时使用。若配置中已有 `biodata`，应修改原段，不能重复添加同名 TOML 表或 JSON 键。

同目录提供两个可复制 JSON（均为 Claude Code `.mcp.json` 格式；Codex 用户请用下面的 TOML）：`mcp.example.json` 是纯离线版，`mcp.llm.example.json` 是外置密钥文件版；二者都只含占位路径，不含真实 Key。

### Codex `config.toml`

用户配置默认位于 `~/.codex/config.toml`；Windows 可用 `%USERPROFILE%\.codex\config.toml` 定位。

```toml
[mcp_servers.biodata]
command = 'C:\Path\To\BioDataAgent\mcp-venv\Scripts\python.exe'
args = ['C:\你的路径\agent\mcp_server.py']
# 只有实测超时时再取消下面两行的注释并调整数值：
# startup_timeout_sec = 30
# tool_timeout_sec = 120

[mcp_servers.biodata.env]
PYTHONDONTWRITEBYTECODE = "1"
PYTHONUTF8 = "1"
# 启用 LLM 时添加；离线模式删除此行：
BIODATA_LLM_ENV_FILE = 'C:\Path\To\BioDataAgent\secrets\llm.env'
```

Codex 默认启动超时为 10 秒、工具超时为 60 秒。启动超时应写在 `[mcp_servers.biodata]` 下，并与首次工具执行超时区分。

### Claude Code `.mcp.json`

项目级 `.mcp.json` 应放在项目根目录，适合团队共享，但需要 workspace trust 与首次批准。Windows JSON 路径中的反斜杠必须写成 `\\`。

```json
{
  "mcpServers": {
    "biodata": {
      "type": "stdio",
      "command": "C:\\Users\\你\\AppData\\Local\\BioDataAgent\\mcp-venv\\Scripts\\python.exe",
      "args": ["C:\\你的路径\\agent\\mcp_server.py"],
      "env": {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "BIODATA_LLM_ENV_FILE": "C:\\Users\\你\\AppData\\Local\\BioDataAgent\\secrets\\llm.env"
      }
    }
  }
}
```

离线模式删除 `BIODATA_LLM_ENV_FILE` 一行。若已有 `.mcp.json`，只把 `biodata` 合并进现有 `mcpServers`，不要整体覆盖。团队共享时不要提交带个人用户名的绝对路径或任何 Key；可使用 Claude Code 支持的 `${VAR}` 环境变量展开，例如先在本机设置 `BIODATA_LLM_ENV_FILE`，JSON 中写 `"BIODATA_LLM_ENV_FILE": "${BIODATA_LLM_ENV_FILE}"`。

---

## 10. 行为与校验边界

- **确定性范围**：在代码版本、数据快照、配置和输入参数相同的条件下，**默认参数**的工具层输出确定；显式开启 `use_llm` 或 `rerank="llm"`（见 5.1）会引入模型的非确定性；此外只要 `recall` 不是 `off`（即使是本地、离线的 `cross_encoder`），`meta.deterministic` 也会转为 `false`——这个字段标记的是「本次请求启用了默认之外的排序路径」，不只是「有没有用 LLM」。宿主 Agent 是否调用工具以及如何组织回答不属于该保证。
- **运行期离线（默认）**：默认参数下推荐与解析工具关闭 LLM、不主动联网；仅当显式传 `use_llm=true` 或 `rerank="llm"` 时才联网调用 LLM。`recall="cross_encoder"` 是**本地**计算、不联网。安装依赖与宿主客户端不属于这个离线范围。
- **文件完整性**：清单中提供 `md5sum` 时，可用于下载后的传输完整性检查；MD5 不是抗恶意篡改的安全证明。
- **链接快照**：`links_snapshot` 表示批量校验发生的日期。本工具不实时探活，因此“快照时可访问”不等于“现在一定可下载”。
- **错误降级**：已捕获的业务异常会转换为结构化错误；导入、协议、序列化、资源耗尽等进程级故障仍需按排错章节处理。

## 官方参考

- [Codex MCP 官方说明](https://developers.openai.com/codex/mcp)
- [Claude Code MCP 官方说明](https://code.claude.com/docs/en/mcp)
- [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)

---

## 附录 A：macOS / Linux

在包含 `mcp_server.py` 的 `agent` 目录执行：

```bash
cd "/你的路径/agent"
SERVER="$(pwd -P)/mcp_server.py"
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/biodata-agent/mcp-venv"
PYTHON="$VENV/bin/python"

mkdir -p "$(dirname "$VENV")"

if [ ! -x "$PYTHON" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --no-project --python 3.12 "$VENV"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sys; assert sys.version_info >= (3,10), "需要 Python 3.10+"'
    python3 -m venv "$VENV"
  else
    echo "没有找到 uv 或 Python 3.10+" >&2
    exit 1
  fi
fi

if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PYTHON" 'mcp==1.28.1'
else
  "$PYTHON" -m pip install 'mcp==1.28.1'
fi

"$PYTHON" -c 'import sys, importlib.metadata as md; from mcp.server.fastmcp import FastMCP; print("环境就绪 | Python", sys.version.split()[0], "| MCP", md.version("mcp"))'
if command -v uv >/dev/null 2>&1; then
  uv pip check --python "$PYTHON"
else
  "$PYTHON" -m pip check
fi
```

注册前先做真实协议自检（同 Windows §2，服务器自带、不写项目、90s 兜底）：

```bash
"$PYTHON" "$SERVER" --selfcheck
```

末行出现 `SELFCHECK_OK tools=19 corpus_total=… download_index_ready=true llm_configured=…` 且退出码为 0 即通过；该自检只读配置、不联网。

可选配置 LLM（以 OpenAI 为例；`read -s` 不回显 Key，文件权限设为仅当前用户）：

```bash
SECRET="${XDG_DATA_HOME:-$HOME/.local/share}/biodata-agent/secrets/llm.env"
mkdir -p "$(dirname "$SECRET")"
read -rsp 'API Key: ' BIODATA_SETUP_KEY; printf '\n'
printf '%s\n' \
  'ENABLE_LLM=true' \
  'MOCK_LLM=false' \
  'LLM_PROVIDER=openai-compatible' \
  "LLM_API_KEY=$BIODATA_SETUP_KEY" \
  'LLM_BASE_URL=https://api.openai.com/v1' \
  'LLM_MODEL=gpt-4o-mini' \
  'LLM_TIMEOUT=60' > "$SECRET"
unset BIODATA_SETUP_KEY
chmod 600 "$SECRET"
BIODATA_LLM_ENV_FILE="$SECRET" "$PYTHON" "$SERVER" --llm-check
```

真实检查退出码为 0 后，在下面注册命令中额外增加 `--env BIODATA_LLM_ENV_FILE="$SECRET"`；离线模式不加。

注册到 Codex：

```bash
codex mcp add biodata --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONUTF8=1 -- "$PYTHON" "$SERVER"
codex mcp get biodata
```

注册到 Claude Code：

```bash
claude mcp add --env PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 --transport stdio --scope user biodata -- "$PYTHON" "$SERVER"
claude mcp get biodata
```

随后重启客户端并按[第 4 节](#4-重启并做客户端验收)完成真实调用。
