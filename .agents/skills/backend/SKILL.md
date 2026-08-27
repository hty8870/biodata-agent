---
name: backend
description: BioData Agent 后端（src/dataset_recommender + mcp_server.py + scripts/）编码约束。凡修改本仓库 Python 后端代码时必须遵守。
---

# 后端编码约束（BioData Agent）

> 本文件是给编码 agent 的硬性约束。完整模块职责见仓库根 `MODULES.md`，开发史见
> `开发日志归档/开发日志.md`（>800KB，只读顶 80 行，查历史用 grep）。根 `AGENTS.md` 的
> 协作纪律同样生效。

## 运行与测试

- 解释器：仓库内 venv——`./.venv/Scripts/python.exe`（Windows / Git Bash）。
- 导入包路径：脚本/测试需 `PYTHONPATH=src`（个别测试文件有内联导入，缺它会出现假失败）。
- 改完必须跑相关测试；发布级改动跑全量：`./.venv/Scripts/python.exe -m pytest tests/ -q`。
- 冻结门：`./.venv/Scripts/python.exe scripts/evaluate_recommendation.py` 必须保持
  Top1=Top5=97.7、硬违规=0、NoResult 满分；held-out 门（`eval/eval_queries_holdout.json`）
  只作泛化看门狗，**不得据它回改解析器/词表**（held-out 只用一次，修复走 dev 集）。

## 架构红线（碰了就毁保证，禁止）

- `corpus_curation.py` 不得 import `retriever` / `workflow` / `query_parser`；检索/编排/评测
  模块不得 import `corpus_curation`（`tests/test_curation_isolation.py` AST 机械门）。
- 检索召回唯一通道是结构化维度硬过滤——**不给自由文本加召回通道**（0% 违规保证系于此）。
- 官方评测 `evaluate_recommendation.py` 直调 `retriever.retrieve`，不经过 workflow/strategy/
  LLM 层；保持这个结构性隔离。
- `database/base/` 冻结基准结构性不可达（计数 + SHA-256 门）；`database/external/` 官方快照
  改动走 `research/` 契约（WORK_RULES §6/§7：staging→报告→提升门→manifest+SHA+
  回滚说明），对话式管护只覆盖 `upload_*` 命名空间。

## 诚实口径（本项目的核心风格）

- 端点/数据源不供的字段保持 null + 如实 warnings，**永不猜值**；缺失哨兵判定唯一真源
  `normalizer.is_missing_value`。
- 抽样/反标得来的 tissue/disease 值集不穷尽 → 记录 `metadata_provenance.complete=False`
  （retriever「值集不完整」第三态据此生效）。
- fail-closed：认不出的来源/动词/参数一律如实报错或弃权，不静默猜；网络/工具失败优雅降级
  并写明降级内容。
- LLM 汇报层必须有机械后检：既遂声称（「已下载/已入库」）只允许对应步骤真实存在且 ok=true
  时出现（agent_exec `_report_contradicts_steps` 先例）。

## 联网纪律（仅 curate.search_online / check_updates / corpus_net）

- 唯一网络出口 `corpus_curation._fetch`（限速/退避/超时），一切联网走 `_fetch_logged` 记
  `.userdata/curate_net_ledger.jsonl`；测试在 `_fetch` 接缝注入假响应，**测试禁网**。
- 源适配器注册表 `SOURCE_ADAPTERS`（arrayexpress/cellxgene/hubmap/single_cell_portal）：
  三键 `label/search/description`，species 本地子串过滤、联网只发原始 query；未注册源
  fail-closed `source_not_registered`。
- plan 零写盘、apply 才写盘/联网；confirm_token 重算比对不一致 → 零写入。

## 编码风格

- 中文注释与 docstring，解释「为什么」多于「是什么」；新模块带模块级 docstring 说明用途、
  红线与消费点。
- 单一真源：口径只定义一次，其余模块 import 复用；复制逻辑必须在注释里注明同源关系与
  双同步义务（先例：`corpus_enrich._alias_occurrences` ↔ `query_parser`）。
- 错误契约：业务错误用带 `code/hint` 的异常类型（UploadError/CurateError 同构），
  三端翻译（Web→HTTPException、MCP→ToolError、agent→step.error_code）写在 docstring。
- 最小改动：不顺手重构、不扩大爆炸半径；additive 优先于修改既有契约形状。
