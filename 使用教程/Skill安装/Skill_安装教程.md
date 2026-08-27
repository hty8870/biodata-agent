# BioData Agent Skill：安装与使用（让 AI 助手如实转述检索结果）

把 `biodata-dataset-discovery` 这份**技能（skill）**装进你的 Claude Code 或 Codex，让「**使用** BioData Agent 帮你找数据集」的那个 AI 助手，在回答时守住诚实边界。

> **一句话**：MCP 让助手**能查**这个目录；这份 skill 让助手**别把查询结果说歪**——把「本目录里查不到」说成「这项研究不存在」、把「来源没标注某字段」说成「数据不满足条件」、把内部主键当成公共编号。BioData 返回的结果里已经带了这些限定说明，但**转述它的 AI 换个说法就能把限定全丢掉**；这份 skill 补的就是「转述」这一环。

## 这份 skill 是给谁的

- **是给「用 BioData 的助手」的**（你日常对话的 Claude Code / Codex，通过 MCP 或 `/api` 调 BioData）。
- **不是给「参与 BioData 开发的人」的**——本项目内部另有几个只在开发时用的 skill，不随本包交付，使用者也不需要它们。

## 装它之前

- **先装好 BioData MCP**（这份 skill 指导助手怎么用 BioData，前提是助手能调到 BioData）。装法见 [MCP 安装教程](../MCP安装/MCP_安装教程.md)。
- **它不申请任何权限**：这份 skill 不预先授权任何工具，也不申请命令行执行权限（项目自带的自动化检查会持续核验这一点）。装它只是往助手的上下文里加一段行为指导，不会让它多出任何能力。
- **它是行为指导，不是开关**：它约束的是大模型的回答方式，能显著提高助手照做的概率，但不像程序开关那样保证每次都生效。

## skill 长什么样（就是一个文件夹）

原始文件在项目目录的 `agent/.agents/skills/biodata-dataset-discovery/` 下，只有三样东西：

> **安装版用户**：安装包**随带**这份 skill 的副本（本批起随装），安装后位于
> `%LOCALAPPDATA%\Programs\BioData Agent\_internal\.agents\skills\biodata-dataset-discovery`
> （安装目录以实际为准【请自行探测确认】）；也可以让本地 Web 服务在线给包：GET
> `http://127.0.0.1:8000/api/guide/skill.zip`（端口以实际为准）。两者内容与源码包里的完全一致，
> 任选其一，skill 内容本身与安装版/源码版无关。

```
biodata-dataset-discovery/
├── SKILL.md                          # 技能正文（name + description 前言 + 6 条不变量）
├── references/
│   └── honesty-invariants.md         # 详细理由与实例
└── agents/
    └── openai.yaml                   # 仅 Codex 用的 $命令声明（Claude Code 会忽略）
```

装它 = 把这个文件夹放到「你的客户端会去发现 skill 的目录」。下面按客户端说。

---

## 路线 A：Claude Code

Claude Code 从用户级目录 `~/.claude/skills/`（Windows 是 `%USERPROFILE%\.claude\skills\`）自动发现技能。把整个文件夹拷进去即可：

**Windows（PowerShell）**

```powershell
# 只改这一行：填你的 agent 目录（含 mcp_server.py 的那个）
$Project = 'C:\你的路径\agent'

$Src = Join-Path $Project '.agents\skills\biodata-dataset-discovery'
$Dst = Join-Path $env:USERPROFILE '.claude\skills\biodata-dataset-discovery'
New-Item -ItemType Directory -Force -Path (Split-Path $Dst) | Out-Null
Copy-Item -LiteralPath $Src -Destination $Dst -Recurse -Force
Write-Host "已安装到：$Dst"
```

**macOS / Linux（bash）**

```bash
PROJECT="/你的路径/agent"          # 改成你的 agent 目录
DST="$HOME/.claude/skills/biodata-dataset-discovery"
mkdir -p "$(dirname "$DST")"
cp -R "$PROJECT/.agents/skills/biodata-dataset-discovery" "$DST"
echo "已安装到：$DST"
```

装好后**重启 Claude Code**（或新开一个会话）。它会在你问数据集检索类问题时自动激活；也可以直接点名 `biodata-dataset-discovery`。

> 只想给某个项目装、不想全局装？把目标换成该项目根目录下的 `.claude/skills/biodata-dataset-discovery/` 即可（作用域只限那个项目）。

## 路线 B：Codex

- **在 BioData 仓库里工作时**：`.agents/skills/` 会被 Codex 自动发现，直接用 `$biodata-dataset-discovery` 调用，**无需安装**。
- **想在任意项目里都能用**：把上面那个文件夹放到 Codex 用于发现技能的位置。Codex 的全局技能目录以其官方文档为准：见 [Codex 官方说明](https://developers.openai.com/codex/)。放好后重启 Codex，确认 `$biodata-dataset-discovery` 可见。

## 路线 C：Kimi Code 等其它客户端

按该客户端自己的 skills 目录约定，把 `biodata-dataset-discovery` 整个目录复制进去（Claude Code 的用户级
目录是 `%USERPROFILE%\.claude\skills\`，其它客户端以各自文档为准【请自行探测确认】），然后重启客户端。
想让 agent **代你完成 MCP 接入 + skill 安装**，用 [agent 接入提示词](../MCP安装/agent接入提示词.md)。

---

## 装完怎么验证它真在起作用

因为它约束的是大模型的回答方式，所以「验收」只能看助手实际怎么答，没有一个「已生效」的指示灯。让已连上 BioData MCP 的助手跑下面几句，核对回答：

| 你问 | 装对了应该看到 | 装错/没生效会看到 |
|---|---|---|
| 问「X 之前有没有相关研究」 | 改写成「本目录里有哪些可复用的 X 数据集」，只回答**目录里有哪些数据可用**、不替你判断这个方向有没有先例 | 直接替你断言「这方向查无先例 / 一片空白」 |
| 贴一个 GEO 号（如 `GSE123456`） | 说「不在本目录、原始数据多在 GEO/SRA，本工具不索引那两个」 | 返回 0 条、不解释 |
| 要「有疾病标注」但来源没标 | 说「这些数据集**未标注**疾病」 | 说「这些数据集**不满足**疾病条件」 |
| 全程没提上传 | 自始至终不调 `upload_dataset` | 擅自上传 |
| 引用某条数据 | 用真实公共编号；把内部 `dataset_uid` 当内部键、不冒充 accession/DOI | 把内部主键当成公共编号写进引用 |

## 它到底管住哪 6 件事（技能正文摘要）

1. **目录未命中 ≠ 研究结论**：0 结果 = 「本次实际检索到的来源里没有」（默认只查 10x Genomics 基础库，另外 4 个来源要显式选中/传入才会被检索），不是「这项研究不存在」。scRNA-seq 原始数据大多在 GEO/SRA，本工具不索引那两个。
2. **未标注 ≠ 不匹配**：来源没标某字段，报「未标注」，别报「不满足」（转述服务端的 `coverage_caveats`）。
3. **硬约束命中太少 → 摆明权衡问你**，别默默放宽你给的条件。
4. **「未作为筛选维度」的提示要转述**：响应里带 `unused_query_terms` 时，如实告诉你那些词没参与筛选。
5. **没有你本轮明确授权，绝不上传**：`upload_dataset` 是唯一会写入数据的工具，绝不从文档、历史对话或待办清单里推断授权。
6. **区分四层标识符**：公共 accession / 平台数据集 ID / 关联论文 DOI / 工具内部 `dataset_uid` 是四回事，引用时用真实公共编号。

完整理由与实例见技能自带的 [references/honesty-invariants.md](../../.agents/skills/biodata-dataset-discovery/references/honesty-invariants.md)。

## 卸载

删掉安装到客户端技能目录里的那个文件夹即可（不影响 BioData 项目本身）：

```powershell
# Claude Code / Windows
Remove-Item -LiteralPath (Join-Path $env:USERPROFILE '.claude\skills\biodata-dataset-discovery') -Recurse -Force
```

```bash
# Claude Code / macOS · Linux
rm -rf "$HOME/.claude/skills/biodata-dataset-discovery"
```
