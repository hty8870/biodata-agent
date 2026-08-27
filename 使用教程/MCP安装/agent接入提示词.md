# BioData Agent MCP 接入提示词（复制给任何支持 MCP 的 agent 用）

> 用法：把下面整段复制给你的 agent（Claude Code / Kimi Code / Codex 等），只按注释替换少数占位，
> 让它在你的机器上**代你完成** BioData MCP 的接入：定位服务器 → 真协议自检 → 注册到客户端 →
> 安装 skill → 配 LLM env → 重启验收。它与 [MCP_安装教程.md](MCP_安装教程.md) 是同一套事实；
> 本提示词给的是「让 agent 照着做」的执行清单，教程给的是「人读」的完整说明。
>
> 路径里写死的默认值只作提示，**一律以【请自行探测确认】的实测结果为准**；机器上装的是安装版
> 还是源码版，以探测到的真实文件为准，不要两个都装。

```text
请在我这台机器上代我完成 BioData Agent 的本地 stdio MCP 接入与验收。你全程只做我明确授权的最小动作；
涉及写用户配置、下载/解压文件、重启客户端时，先说明你要做什么再执行，不要使用任何关闭安全审批的旁路参数。

【背景】BioData Agent 是公开单细胞数据集目录的中文检索/推荐软件，本地跑，MCP 服务器暴露 19 个工具
（16 只读 + 3 写盘）。要接入的是它的 **MCP 服务器**，不是网页端。

【硬约束】
1. 项目/安装目录只读：不改源码、不删改数据、不在项目里建虚拟环境或缓存。写盘只允许发生在
   agent 的客户端配置与 skills 目录。
2. 绝不读取、打印、复述或记录任何 API Key / token / 密码。涉及密钥的输入只让用户本人在终端里完成。
3. 不知道就探测：所有路径以【请自行探测确认】为准，不要靠猜；探测不到就停下来问我，不要硬编一个路径。

【步骤 1 · 定位 MCP 服务器（二选一，先探测再决定）】
- 安装版（本机装有 BioData Agent 桌面版）：默认 MCP 可执行文件在
  `%LOCALAPPDATA%\Programs\BioData Agent\BioDataAgentMCP.exe`【请自行探测确认：开始菜单快捷方式
  「BioData Agent」的目标路径即可查到安装目录；exe 就在安装目录根下】。
- 源码版（从源码包跑）：定位包含 `mcp_server.py` 的目录【请自行探测确认】，用该项目的虚拟环境
  Python【请自行探测确认】按 `python mcp_server.py` 方式运行（详见 MCP_安装教程.md 路线 B）。
先跑一次 `--version` 确认产物：安装版 exe 或源码版 python 都应打印 `biodata-mcp 1.34.0 | MCP SDK …`。

【步骤 2 · 真协议自检（--selfcheck）】
运行 `BioDataAgentMCP.exe --selfcheck`（安装版）或 `<venv-python> mcp_server.py --selfcheck`（源码版）。
通过标准：退出码 0 且末行含 `SELFCHECK_OK tools=19 ... download_index_ready=true`。
不通过就把输出原样给我，按 MCP_安装教程.md「7. 排错」定位，不要绕过。

【步骤 3 · 注册到客户端（stdio，command/args/env 三要素）】
把服务器登记为 stdio MCP，配置形状与 使用教程/MCP安装/mcp.example.json 一致：
- `command`：安装版 = BioDataAgentMCP.exe 的绝对路径（args 为空，exe 本身就是服务器）；
  源码版 = venv Python 绝对路径（args = [mcp_server.py 绝对路径]）。
- `env`：至少 `PYTHONUTF8=1`（源码版建议加 `PYTHONDONTWRITEBYTECODE=1`；安装版 exe 不需要）。
- 服务器名统一用 `biodata`。
按你的客户端对应方式登记，并先查重（同名已存在且路径一致就跳过）：
- Codex：`codex mcp get biodata` → `codex mcp add biodata --env ... -- <command> [args...]`。
- Claude Code：`claude mcp get biodata` → `claude mcp add --transport stdio --scope user biodata -- <command> [args...]`。
- Kimi Code / 其它：按该客户端自己的 MCP 配置文档登记；只填 command/args/env 三要素，
  不要臆造专属字段名，不确定就查 `--help` 或官方文档【请自行探测确认】。
不要编辑客户端配置里与 biodata 无关的任何内容。

【步骤 4 · 安装 skill（可选但推荐，二选一）】
biodata-dataset-discovery 技能约束「使用 BioData 的 agent」如实转述检索结果（目录未命中 ≠ 研究
不存在、未标注 ≠ 不匹配等 6 条不变量），装进我的 agent 的 skills 目录。
- 安装版：从安装目录的资源副本复制 `_internal\.agents\skills\biodata-dataset-discovery`
  【请自行探测确认：安装目录下 `_internal\.agents\skills\`】整个目录；
- 或让本地 BioData Web 服务在线给包：GET `http://127.0.0.1:7860/api/guide/skill.zip`（服务未启动时
  可用桌面版「BioData Agent」启动；端口以实际为准【请自行探测确认】），下载后解压出
  `biodata-dataset-discovery` 目录。
目标位置 = 我这个 agent 的 skills 目录（Claude Code 用户级是 `%USERPROFILE%\.claude\skills\`；
Kimi Code / Codex 按各自文档【请自行探测确认】）。放好后告诉我重启即可生效。
不要安装项目里其它 skill（那些是给 BioData 开发用的，不随本包交付）。

【步骤 5 · LLM 环境（可选；默认离线可用）】
默认全程确定性、离线、无需 Key；只有我想用 LLM 增强时才配：
- 安装版：Web 与 MCP 共用 `%LOCALAPPDATA%\BioDataAgent\config\.env`【请自行探测确认】，编辑
  ENABLE_LLM / LLM_PROVIDER / LLM_API_KEY / LLM_BASE_URL / LLM_MODEL；Key 的输入只让我本人在编辑器里完成。
- 源码版：按 MCP_安装教程.md「3.3 可选：给已安装的 MCP 补配 LLM API」用项目脚本生成项目外密钥文件，
  客户端配置只放 `BIODATA_LLM_ENV_FILE` 路径，不放 Key。
不配也不影响检索——这是可选步骤，我没提就跳过。

【步骤 6 · 重启与验收】
1. 让我重启客户端（或你说明重启方法由我执行）。
2. 重启后调一次 `biodata_status`：要求 ok=true、corpus_total>0、download_index_ready=true，
   并列出全部 19 个工具名。不要用 shell 或网页代替 MCP 调用。
3. 若配了 LLM，再调 `biodata_llm_status`（check_connection=false），只汇报 configured/provider/model，
   不回显路径或秘密。

【收尾报告】
告诉我：装的是安装版还是源码版（对应命令与绝对路径）、--selfcheck 结果原文、
客户端登记的命令/参数/env（不含任何秘密）、skill 是否装上及目标目录、LLM 是否配置、
验收时 biodata_status 的 ok/corpus_total/download_index_ready 与工具数。任何一步没做或失败，
如实说明卡在哪、需要我做什么，不要假装完成。
```

---

## 配套说明（人读；agent 执行时不必转述）

- **它和教程的关系**：本提示词让 agent「照着做」；注册命令的完整形态、排错表、卸载与手工配置见
  [MCP_安装教程.md](MCP_安装教程.md)，skill 的安装/验证/卸载见 [Skill 安装教程](../Skill安装/Skill_安装教程.md)。
- **安装版资源副本**：安装包随带 `_internal\.agents\skills\biodata-dataset-discovery`、
  `_internal\使用教程\MCP安装\` 与 `_internal\使用教程\Skill安装\`；上述 `/api/guide/skill.zip`
  是同一份内容的在线下载口（两者任选其一，内容一致）。
- **埋点说明**：MCP 每次工具调用会追加一行脱敏 JSON 到本地 `%LOCALAPPDATA%\BioDataAgent\.userdata\mcp_calls.jsonl`
  （源码版为项目根 `.userdata\`），零网络；Web 提供 `/api/telemetry/mcp-calls` 供中继增量读取。
- **上限口径**：`browse_datasets` 的 `limit` 上限为 100，与网页 `/api/datasets` 同一常量源。
