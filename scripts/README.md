# scripts/ 目录索引

`scripts/` 是**开发、评测与发布辅助脚本**的集合，不是应用运行时依赖：启动、使用与部署都不看这里，正式入口见根 [README](../README.md)。
脚本一律用仓库虚拟环境的解释器运行（Windows 为 `.venv/Scripts/python.exe`，其它平台为对应的 `python3`）；质量门清单、profile 与冻结基线口径见
[质量门与验证矩阵](../docs/QUALITY_GATES.md)，交付与发布的整体流程见 [自动化与发布](../docs/AUTOMATION_AND_RELEASE.md)。
新增脚本必须同时完成分类登记——对外进 `packaging/public-mirror/` 的 profile 公开清单（默认 `files.txt`，另有 `files.skeleton.txt` / `files.biodata-pack.txt`），内部或本机产物进 `.deliveryignore` / `.gitignore`；
本目录的文案与产物按「陌生公众可读」写，不含内部编号、真名与真实运维信息（主机、IP、账号用 `<server-ip>` 这类占位符）。标「薄壳」的脚本只解析参数，实现真源在被指向的模块里。

## 1. 启动与运行入口
- `run_web.py` —— Web 服务启动器（源码模式主入口，含公网护栏的启动期 fail-closed 校验）；`run_web.sh` 是它的 POSIX 薄壳。
- `launch_web.ps1` / `launch_web.sh` —— Windows 与 macOS/Linux 一键启动器：解析 Python → 建或复用 `.venv` → 按需装依赖 → 首启向导 → 端口漂移 → 开浏览器。
- `run_app.py` —— 桌面窗口（pywebview）开发入口，与打包版可执行文件共用同一条入口链；`setup_mcp.ps1` —— 把 MCP 服务器写进 codex / claude 客户端配置。

## 2. 数据管护、入库与质量审计
- `curate_datasets.py` —— 对话式数据库管护的命令行薄壳（清点 / 本地导入 / 联网搜索 / 回收站 / 恢复），与 MCP、Web 共用同一分发真源。
- `assemble_biodata.py` —— 领域包装配器：骨架仓库的只读快照 + 本仓「肉区」→ 产品仓。
- `promote_uploads.py` —— 把 sync 自动入库的 `upload_*.json` 从运行时目录晋升进受版本控制的基础快照。
  受控查询重基线后，用 `--refresh-frozen-queries --rebaseline-reason "公开可读的授权依据"` 单独同步 `automation/frozen-queries.json`；此模式只更新判据，不执行晋升或修改查询。普通晋升不会刷新查询指纹。
- `ingest_cellxgene.py` / `ingest_hca.py` / `ingest_ebi_scea.py` / `ingest_arrayexpress.py` / `ingest_refinebio.py` / `ingest_zenodo.py` —— 外部平台库离线快照族：各拉取一个公共源、归一到本项目记录 schema，只写 `database/external/`（opt-in，默认不并入冻结基准）。
- `build_sample_supplement.py` —— 由平台补充信息表生成 by-uid 旁挂账本（冻结基准逐字不动）；`sanitize_metadata_contacts.py` —— 清除可再分发元数据快照中的联系邮箱字面量。
- `audit_metadata_coverage.py` —— 多源元数据字段覆盖率审计（纯本地、不联网）：哪些约束维度在某些来源根本没有值。
- `data_quality_audit.py` —— 内容一致性审计（标注与描述文本矛盾、`n_files=0` 等）；`measure_entity_gap.py` —— 把细粒度实体类查询的漏召缺口分四类定量。

## 3. 下载供应链与活台账
- `provision_dataset.py` —— 下载执行器的命令行薄壳：真实下载数据集并逐文件核对 md5 与大小。
- `patrol_links.py` —— 链接存活巡检（Range-GET），产出新的一帧内容寻址快照；`diff_snapshots.py` —— 两帧对比出「谁死了 / 谁恢复 / 谁大小变了」。
- `record_provision_results.py` —— 把下载与加载的实测报告回写成台账新帧。
- `report_provisioning.py` —— 聚合台账，生成文件级与数据集级的 provisioning success rate 报告。
- `load_smoke.py` —— 抽样真下载并用 scanpy 真加载，把「能否装进 Scanpy」从未知变实测；`scan_lab_assets.py` —— 扫描本地实验室目录并与清单比对，产出只读资产台账。（本组另有少量内部巡检 / 运维脚本不随公开仓分发。）

## 4. 检索评测与冻结门
- `evaluate_recommendation.py` —— 官方冻结推荐评测门（base 语料、二元 must_match），基线与命令见质量门文档。
- `evaluate_curate_agent.py` —— 管护动作的 execution-based 冻结门：断言动作执行后沙盒库的最终文件状态，而不是 agent 的措辞。
- `evaluate_multisource.py` —— 全来源多源检索质量评测（开发态，非冻结门）。
- `evaluate_recall.py` / `evaluate_rerank.py` —— 附加分级评测：可选向量召回 / LLM 重排相对规则排序的 nDCG@5 与 MRR，不改也不替官方二元口径。
- `ranking_interleave.py` —— 确定性、离线的 team-draft interleaving：把成对排序表变成可复现的实验分配。（本组另有少量内部评测脚本不随公开仓分发。）

## 5. LTR 离线数据与训练
- `synth_ltr_data.py` —— 第 1 阶段：从冻结语料确定性合成带分级的 query–document 训练对（离线、可复现）。
- `llm_rewrite_queries.py` —— 第 2 阶段：把模板查询改写成自然口语且不破坏约束；`llm_label_pairs.py` —— 第 3 阶段：LLM 语义相关性打标（0–3）。
- `llm_cross_score.py` —— 第 3b 阶段：多模型交叉打分取共识，压低单模型偏好；`augment_vec_scores.py` —— 第 4 阶段：给合成对附上生产同款的稠密向量分。
- `train_ltr.py` —— 训练并对比 pointwise GBDT 与 pairwise RankNet，报 nDCG@5/10、MRR 与三个生产规则基线；`ltr_promote.py` —— 攒量监控 → 候选重训 → 四门准入 → 版本化转正与回滚。
- `ood_duel.py` —— 离线 OOD 排序对比的唯一实现：五折混 OOD 重训决斗与 `--compare-frozen` 零重训制品对照；`ltr_promote.py` 的 OOD 门直接复用该模块。
- `synth_ood_queries.py` —— 生成分布外（真实语域、边角情形）查询集；`harvest_ood_candidates.py` —— 用真管线采集该集合的候选（含自动降级轨）。
- `split_ood_evalonly.py` —— 按 query 哈希把交叉打分集切成可训练层与只评测层；`check_leakage.py` —— 新查询波次的确定性泄漏守卫，防训练数据与冻结评测集重叠。
- `build_corpus_vectors.py` —— 离线生成随部署分发的语料向量文件；`fetch_embedding_model.py` —— 一次性把向量召回用的本地模型下到 `models/`，此后运行时不联网。

## 6. 执行侧 Agent 实验与探针
- `evaluate_agent_live.py` —— 执行侧 agent 的真 LLM 集成验证（剧本化工具结果 + 沙箱项目根，含失败聚类与「汇报数字出处」不变量）。
- `evaluate_search_live.py` —— search 档的真 LLM 端到端验证：不替换工具，rank/rerank 真跑确定性管线。
- `sim_agent_scenarios.py` —— 路由模拟剧本：真实用户句过图后打印路由表供人工核对。
- `probe_decide_format.py` —— 原样捕获 decide/understand 的每次真实调用，并分类「不可读」的成因。
- `probe_multicall_fidelity.py` / `probe_multicall_legality.py` —— 批量 tool_call 的保真度与第 2..N 个调用合法率微验证；`aggregate_multicall_arms.py` —— 把多组保真度记录聚合成对照表。
- `probe_glm53flash_trial.py` —— 探测模型供应商在工具调用关键参数组合下的真实行为。
- `augment_agent_cases.py` —— 把人工用例机械扩增成对抗变体：变体的期望由生成器继承，LLM 只写新问法。（本组另有少量内部实验验证脚本不随公开仓分发。）

## 7. 遥测、反馈与调用留痕
- `telemetry_export.py` —— 遥测落库数据 → 推荐评测 benchmark 原料（join 回执账本去重）；`build_telemetry_benchmark.py` —— 把候选冻结成可复现、不可变的 benchmark 数据集目录。
- `telemetry_parquet.py` —— 导出 JSONL 无损转 Parquet（离线、不连数据库）；`telemetry_pg_load.py` —— HTTP/PostgreSQL 有界负载探针（10 / 50 / 100 路并发）。
- `telemetry_delete.py` —— 按 install_id 级联删除遥测主包与回执；`benchfb_ingest.py` —— 接收用户导出的 benchmark 反馈包并转成可用原料；`summarize_mcp_calls.py` —— 汇总 MCP 调用留痕做需求分析证据。

## 8. 质量门与本地守卫
- `process_tree.py` —— 共享子进程辅助模块：进程树级超时与回收，供质量门和安装测试复用。
- `quality_gate.py` —— 按清单执行质量门（本地与 CI 共用同一真源），缺工具、超时或命令失败一律 fail-closed。
- `precommit_check.py` —— opt-in 本地 pre-commit 守卫，只做快而确定的三件事。
- `install-hooks.ps1` —— 安装本地 git 钩子；`secret_patterns.py` —— 兼容壳，值级脱敏真源已在 `src/` 内。（本组另有少量内部日志 / 治理脚本不随公开仓分发。）

## 9. 浏览器冒烟与 UI 回归
- `smoke_test.py` —— 无浏览器的命令行/工作流端到端冒烟（mock LLM 链、健康检查、回退行为、表格完整性）；`web_smoke_test.py` —— Web 冒烟：对前端 HTML/JS 做静态契约与乱码检查。
- `smoke_ui_playwright.py` / `smoke_ui_acceptance.py` —— 前端契约与「真 LLM、不 stub」验收走查族（设置开关、工具路由、结果卡与状态卡，截图存证）。
- `smoke_dlq_pills_playwright.py` / `smoke_packchips.py` / `smoke_suggestion_chips.py` —— 对话流回执元素的浏览器级回归：下载队列 pill、打包 chip、婉拒候选 chip。
- `smoke_prelim_backend.py` / `smoke_multi_step_playwright.py` / `smoke_sidebar_states.py` / `smoke_srtool.py` / `smoke_sumhl.py` —— 单特性前端回归族：初步结果先行、多步执行步骤卡、侧边栏状态机、零命中救回、摘要高亮。
- `visual_regression.py` —— 关键页面与状态的截图基线（录制 / 带容差比对）。（本组另有少量内部回归 / 取证脚本不随公开仓分发。）

## 10. 发布、打包与公开镜像
- `build_public_mirror.py` / `verify_public_mirror.py` —— 公开镜像的生成与校验：按清单从源仓库装配公开树，并逐文件核对公开树与源一致。
- `make_delivery.py` —— 回答「哪些文件可以外发」：git 对账 + 敏感词与秘密复核；`build_release.py` —— 交付候选包的组装与发布门（复用同一份受限词表，避免两处漂移）。
- `make_submission_package.py` —— 生成形状可复现的对外提交包；`print_pdf.py` —— 用 Playwright 把 HTML 打成 PDF。
- `build_windows_runtime.py` / `build_windows_installer.py` / `build_supply_chain.py` —— Windows 冻结运行时（PyInstaller onedir）、图形化安装器与供应链产物的构建链。
- `installer_e2e.py` —— 安装 / 升级 / 卸载的 E2E harness；`smoke_release_first_launch.py` —— 源码包首次启动冒烟（建全新虚拟环境、装依赖、验证可启动）。
