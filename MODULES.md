# MODULES.md · 模块边界与公共契约

> 这里是**逐模块的职责 + 公共接口 + 并行安全**明细。
> 目的：多个贡献者并行改动时，一眼看清「这块归谁、动它会牵到谁」。

> **阅读导航（渐进披露）**：本文件约 136KB，**不要整读**。先 `grep -n '^#' MODULES.md` 取最新小节行号（行号随编辑漂移，以 grep 输出为准），再按行区间只读所需小节：
>
> - §「后端 `src/dataset_recommender/`」：改后端、检索、查询理解、数据装载时读；
> - §「前端 `web/static/`」：改前端文件时读；
> - §「自动化与候选发布」：改 `automation/`、质量门或候选发布时读；
> - §「并行矩阵」：多 Agent 分工前读；
> - §「`/api/recommend` 响应字段 → 前端消费点映射」：改 HTTP 字段名/形状前**必须整节读**。

> **目录结构速查（深度整理批 · 模块化文件夹重组——「改什么去哪个文件夹」；2026-09-23 骨肉分离批起 8 个子包）**：
> 后端 `src/dataset_recommender/` 不再是平铺，模块按职责入 8 个子包：
>
> | 子包 | 管什么 | 里的模块 |
> |---|---|---|
> | `llm/` | 大模型接入与 AI 文案 | llm_client · config · prompts · intro_llm · act_summary_llm · dream · scope_gate · search_reply_llm |
> | `domain/`（2026-09-23 骨肉分离批新增） | 领域包机制（骨架侧） | descriptors（维度描述符） · pack（DomainPack/DomainBranding 契约） · registry（活动领域解析：BIODATA_DOMAIN / BIODATA_DOMAIN_DIR 外部装载） · vocab_tools（目录驱动的 suggestable_terms / pinyin_alias_index） |
> | `domains/`（同批新增） | 领域包（「肉」） | biodata/（默认领域·骨架能力测试集：vocabulary 词表+派生规则 · dimensions 六维描述符 · pack 装配 · prompts 提示词经 `prompts/domains/biodata/`） |
> | `corpus/` | 语料/数据管护（装载、联网源、下载、上传、回收站） | corpus · corpus_curation · corpus_enrich · corpus_net · **net_ledger**（联网账本路径/时间戳/加锁追加单源） · corpus_status · data_loader · data_quality · fs_utils · uploads · downloads · download_plan · download_executor · **download_manager** · download_script · inspection · provenance · reachability · sample_supplement · patch_package |
> | `retrieval/` | 检索排序与查询理解 | retriever · rerank · vector_recall · vector_index · **ltr_features**（LTR 9 维特征构造单源） · model_runtime · model_worker · query_parser · **query_lexicon**（查询词法；领域词表 vocabulary 已迁 `domains/biodata/`） · search_request · strategy · normalizer · synonyms · units · fair · feasibility · recall_api · **warn_once**（家族共享 warn-once 告警单源） · **candidate_text**（候选打分文本序列化单源） |
> | `content/` | 条目内容生成与呈现（介绍/标识符/任务包/复用包/成果导出） | introduction · summary_genre · item_view · identifiers · identifier_patterns · reuse_pack · task_pack · export_pack · lab_ledger · compatibility · labels |
> | `agent/` | agent 执行侧（动词表/形状闸/图循环/回合/微型分流器） | action_plan · agent_schemas · agent_exec · turn · compare · hybrid_policy · l0_router · decider（Jev 决策器：Qwen3-1.7B 真·RLCD 校准蒸馏） · **collab_rules**（规则/候选/审核兼容面） · **knowledge**（统一知识类型/防线/提示投影） · learning_scope · learning_loop · memory_relations · trace/（嵌套子包：recorder · events · snapshot · rollback · export） |
> | `app/` | 编排与接口（主管线/网页/CLI/面板/账户/可选 Redis·MQ 后端） | workflow · webapp · cli · board · accounts · model_installer · redis_store · redis_sessions · durable_jobs · rq_jobs · rq_worker · conversation_runs · conversation_runs_api · conversation_contracts · conversation_store · conversation_results · conversation_service · conversation_api · learning_store · learning_service · learning_api · knowledge_store · knowledge_service · knowledge_api · llm_admission · mcp_agent_tools · workspace_store · workspace_api · session_log · replay_digest · replay_store · replay_service · mcp_server · mcp_tokens · desktop_launcher · desktop_launcher_win32 · webview_shell · request_validation · runtime_paths · limits · llm_quota · recommend_rows |
> | 包根（不属任何子包） | 横切共享叶子与随包台账 | secret_patterns（脱敏/扫描单一真源） · data/（随包只读台账 JSON/JSONL，非 Python 包） |
>
> 仓库根另有独立子包 `devcontext/`（项目上下文检索系统，agent 侧记忆；**仅私仓存在，不派生公开镜像**——公开仓库无此目录，本行只对私仓读者有效；详见 `devcontext/README.md`，测试 `devcontext/tests/` 已入 pytest-suite 质量门）：
> source_catalog（真源分类+local_only 隐私闸）· index_store（索引生命周期+向量）· resolver（检索编排+rerank 闸）· retriever · chunker · reranker · semantic_backend（本地/API 向量与重排）· cost_budget（零网络估算；估算与执行共用同一缓存身份——`dimensions` 只取当前选中 embedder，未知/非正一律保守判缓存 miss；build/resolve/eval 三估算同一口径） · cost_policy · doc_audit · pack_builder · projection · query_lexicon · util · cli。
>
> 后文明细表的「文件」列仍写裸模块名，按上表定位子包（如 `retriever.py` → `retrieval/retriever.py`）。
> 前端 `web/static/js/` 56 个文件入 4 个子文件夹：`core/`（core · shell · boot · progress ·
> interactions · onboarding · usage_core · usage_log · usage_upload · benchfb_core · benchfb ·
> artifacts · projects_core · projects · project_updates_core · project_updates · project_exports_core ·
> feedback_core · feedback_dialog_core · feedback · batch_select · flow_trace · replay_core · replay_layout ·
> copy · storage_queue · **downloads**（全应用唯一浏览器下载引擎，单通道原则锚点））、`search/`（search · results · facets ·
> cards · browse · dataset_page · sync_button_core · sync_button）、`act/`（act · act_core ·
> act_run · task_pack · reuse_pack）、`panel/`（board · board_core · memory · memory_rank ·
> dream · dream_core · **rules**（协作规则卡：记忆弹窗里的**行为档**块——候选逐条确认 / 生效中可撤回 /
> 记忆条目「转为协作规则」的过桥动作） · accounts · fav_folders · examples · project_exports ·
> replay · replay_graph）。importmap 的
> `#xxx` 键不变，JS 内部 import 零改动。

---

## 后端 `src/dataset_recommender/`（分层 DAG：**顶层 import 无环**；惰性环在案见下注）

> **惰性环在案（2026-10-01 AST 实测 6 处，刻意不拆）**：`llm.config ↔ llm.llm_client`、
> `content.introduction ↔ content.summary_genre`、retrieval 8 模块族（`candidate_text · ltr_features · model_runtime · recall_api · rerank · retriever · vector_index · vector_recall`，共享 warn-once/候选文本已抽 `warn_once`/`candidate_text` 叶子剥离模块级粘合）、
> `corpus.corpus ↔ corpus.patch_package ↔ corpus.uploads`、`agent.agent_exec ↔ agent.collab_rules`、
> `app.rq_jobs ↔ app.webapp`——均为**一方顶层 import、另一方函数体内惰性 import** 的软环
> （模块级 import 图实测无环，import 期安全）。改动这些模块时维持「惰性边只在函数体内」的现势；
> 新增顶层反向边之前先想能不能抽中立叶子模块。

| 层 | 文件 | 一句话职责 | 对外公共接口（别的模块靠这些用它） |
|---|---|---|---|
| 数据/IO | `data_loader.py` | 扫目录读 JSON → 原始记录 | `scan_json_files` · `load_raw_records(dir, lenient=)` · `extract_records` |
| 数据/IO | `normalizer.py` | 原始 dict → `DatasetRecord` 归一；**缺失哨兵判定单一真源** | `DatasetRecord` · `normalize_records` · `normalize_dataset_record` · `MISSING_VALUE_TOKENS` · `is_missing_value` |
| 数据/IO | `corpus.py` | 装配语料：base + 可选 external | `load_normalized_corpus(data_dir, root, sources=)` · `load_full_corpus` · `available_sources` · `known_source_values`（接口层校验 sources 用） · `source_of` · `invalidate_external_cache` · `BASE_SOURCE` · **`corpus_snapshot(records, *, with_content=False)`**（确定性语料快照 `snapshot_id`=全部 uid 的 SHA-256 前 12 位 + 条数 + 来源分布，可复现锚点；`with_content=True` 追加 `content_digest`——覆盖字段取值，用于发现「编号没变但内容被更新过」） |
| 数据/IO | `download_plan.py` | 下载方式四档判定 + 下载计划（判据是 `download_url != url`，**不是**「有没有 download_url」；md5 覆盖用 all() 不是 any()） | `classify_download_tier` · `assert_item_shape` · `build_plan` · `safe_name` / `safe_uid` · `TIER_*` · `DownloadPlanError` |
| 数据/IO | `download_script.py` | 把下载计划渲染成清单与两个 runner（**只生成文本、不写盘**；默认 dry-run、只放行 https + 白名单主机；`.ps1` 在 stdout 被重定向时把输出编码钉死为无 BOM UTF-8，支持 Windows PowerShell 5.1 与 7） | `artifact_files` · `render_manifest_tsv` / `_json` · `render_sh` / `render_ps1` · `primary_only_sentence` |
| 数据/IO | `download_executor.py` | 下载执行器：本机真执行 `build_plan` 计划（stdlib 流式 + 边下边算 md5；https+计划白名单；`.part`→原子改名；核对不符改名 `.corrupt` 留证据；旗标文件默认跳过）；保护区扩为**整个 `database/`**（此前只护 base，显式传 `database/external` 会把二进制下载物落进只许 `upload_*.json` 的元数据库——MCP 文档承诺「绝不写 database/」自此口径对齐）+ `src/.../data` 照旧。additive：`download_one` 新增 `subdir`（落盘目录覆盖）/`cancel_event`（每块前检查，置位抛 `DownloadCancelled` 并**保留 .part** 可续传）/`progress_cb`（每块回调字节数）三个可选参数。**加固**：生产 opener 默认改为带计划白名单的 `_policy_opener`——`_open_stream_safe` 手动逐跳重校验（scheme 仅 https + 主机精确白名单 + 端口仅 443 + IP 解析闸 `resolve_and_validate`：全部 A/AAAA 任一命中回环/私网/链路本地/保留/组播/未指定/非全球单播即拒，含云元数据 169.254.169.254），限 3 跳、每跳固定到已校验 IP 建连（`_PinnedHTTPSConnection`，SNI/证书/Host 用原主机名）防 DNS rebinding；流式硬上限 `hard_max_bytes`（声明×1.05 与 `GLOBAL_FILE_CAP`=1 TiB 取小，无声明按全局；Content-Length 超限早退），超限中止并清理 `.part`，不重试（`_DownloadTooLarge` → `STATUS_UNREACHABLE` + 中文 error）；策略拒绝（`DownloadPolicyError` → `STATUS_REJECTED`）不重试；`url_policy_error` 增加端口(443)/userinfo/非法端口闸；`_open_stream` 保留为无白名单安全形态（load_smoke 用，https+端口+IP+重定向闸仍生效） | `provision` · `download_one` · `build_rows` · `resolve_out_dir` · `url_policy_error` · `resolve_and_validate` · `forbidden_ip_reason` · `ProvisionReport` / `FileResult` · `ProvisionError` · `DownloadPolicyError` · `DownloadCancelled` · `MAX_REDIRECTS` · `HARD_SIZE_FACTOR` · `GLOBAL_FILE_CAP`；CLI `scripts/provision_dataset.py`，回写 `scripts/record_provision_results.py`（兼收 load_smoke 报告）；加载冒烟 `scripts/load_smoke.py`（按 platform 分层抽样 primary `.h5` 真下载真加载，可选依赖 scanpy 见 `requirements/requirements-loadsmoke.txt`） |
| 数据/IO | `download_manager.py` | 服务端真下载管理器（Web `/api/download/*` 四端点共用）：内存 job 注册表（上限 20 个、同一时刻只允许一个 running）+ 每 job 后台线程；**逐数据集子文件夹** `<dir>/<safe_uid>__<标题前40字符>/`（直接解决「哪个文件属于哪个数据集」）+ 根目录随进度写 `README.txt` / 随完成追加 `manifest.tsv`；磁盘预检 `disk_usage.free < total×1.05` 拒绝（507）；取消 `threading.Event` chunk/文件间检查、.part 保留可续传；下载执行复用 `download_executor.download_one`，来源判定复用 `download_plan.build_plan` 四档（10x 台账 774 checksum_verifiable + CELLxGENE 2198 size_only + SCP/AE 有真直链条目，GEO 等 page_only/direct_unsized 如实进 unsupported 并给中文 reason；**不新增网络抓取代码**）。**加固**：不注入 opener 时下载走 executor 生产 `_policy_opener`（每跳+IP+硬上限防线无条件生效）；新增 env 开关 `BIODATA_DOWNLOAD_BLOCK_USER_UPLOADED`（默认关=允许；置 1/true/yes/on 时 source 为「用户上传」的记录进 unsupported 带中文 reason——策略便利非安全边界）；`_finalize_file` 对超硬上限给专门文案 | `build_download_plan` · `start_job` · `get_status` · `cancel_job` · `default_download_dir` · `DownloadManagerError` · `STATE_*` / `FILE_*`；测试接缝 `records=`/`out_dir=`/`opener=`/`sleep=`（禁真网）；`start_job` 先磁盘预检、通过后才建任务目录（此前 507 路径会在 `~/Downloads/BioData数据-*` 留空目录），`tests/test_download_manager.py` 有「测试不污染真实下载目录」回归断言；传输层测试见 `tests/test_download_ssrf_guards.py`。**在途增删**：新增 `update_job`（对运行中 job 做 add/remove——remove 排队=跳过、remove 正在下载=中止并清理未完成部分后继续、remove 已完成=如实拒绝、add=追加进队列尾部；无运行中 job 抛 `job_not_running`）与 `_MultiCancel`（逐行取消信号合成：单行移除只中止该文件、不整 job 取消），Web 端点新增 `POST /api/download/update`（additive） |
| 编排 | `task_pack.py` | 一句话任务包**唯一编排点**：一次算、四处扇出，出口逐一比对四份产物的数据集集合 | `build_task_pack` · `render_files` · `files_to_zip_bytes` · `plan_spec` / `plan_token` · `PACK_FILES` · `TaskPackError` |
| 数据/IO | `uploads.py` | 数据集上传摄取**单一真源**（Web `/api/upload` + MCP `upload_dataset` 共用）：落 `database/external/`、绝不碰 base；计数→落盘→记账同住 `ingest_critical_section`（线程锁 + 跨进程 OS 文件锁，同线程可重入，超时如实 lock_busy） | `ingest_dataset` · `new_upload_name`（upload_ 前缀 + .json 校验） · `decode_json_bytes` · `ingest_critical_section` · `UploadError`(code/hint) · `UploadResult` |
| 数据/IO | `corpus_curation.py` | 对话式数据库管护**单一真源**（MCP `curate_datasets` + Web `/api/curate/*` + CLI `scripts/curate_datasets.py` 三入口共用）：list 清点 / import 本地导入（内容 hash 去重）/ search_online 联网搜官方源（适配器九源：arrayexpress/cellxgene/hubmap/single_cell_portal+ 10x（官网私有 API、双层形状闸 fail-closed）/hca（Azul，facet 过滤+分页拉取本地匹配）+ geo/zenodo（后接入）+ refinebio（GEO/SRA/AE 统一加工镜像，ES 模糊匹配 + accession 直达）；唯一网络出口 `_fetch`（GET/POST）+ 全量端点 300s TTL 缓存 + 请求账本 `.userdata/curate_net_ledger.jsonl`）/ remove 回收站式可逆删除（`.userdata/recycle/` + manifest）/ restore 恢复；**plan 零写盘、apply 才写盘/联网、confirm_token 重算比对不一致零写入**；管护对象限 external 的 `upload_*` 命名空间，base 结构性不可达；检索/编排/评测不 import 本模块（AST 机械门）；Web `/api/curate/*` 的前端消费点 = `act.js`（**全自动化**：runner 链式直推 plan→apply，不再开问卷——预先授权推翻「写操作必须人批准」，只保留审计账本 + 回收站可回退；问卷弹窗 `survey.js` 退役；两步端点保留给 MCP/CLI） | `run_curate_action`（三入口统一分发） · `list_curations` · `plan_import`/`apply_import` · `plan_search_online`/`apply_search_online` · `plan_remove`/`apply_remove` · `plan_restore`/`apply_restore` · **`check_updates`**（只读来源更新检查：在线比对 ArrayExpress/ENCODE/10x/HCA/GEO/Zenodo/refine.bio 七源 / 快照源如实报告，端点 `/api/curate/check-updates`，无 confirm_token） · **`sync_updates`**（检查更新→有新增则自动入库的复合流，「工作流即工具」：步骤顺序写死在代码里——先只读比对、再把能闭环来源〔能在线比对 ∩ 有入库适配器 = ArrayExpress / 10x / HCA / GEO / Zenodo / refine.bio 六源〕的疑似新增逐编号搜回、合并成一个 `curate_sync_*` 批次文件入库；外部库 dataset_uid 集合拦截重复入库；闭不了环的来源逐条如实写明哪段做不到；无 token 两步——原子调用无信任边界，端点 `/api/curate/sync-updates`） · `write_boundary_zh` · `require_action` · `ACTIONS` · `SOURCE_ADAPTERS` · 来源名归一经 `corpus_net.SOURCE_ALIASES`/`resolve_source_key` · `CurateError`(code/hint) · `make_confirm_token` |
| 数据/IO | `corpus_enrich.py` | 元数据反标富化：用词表别名（词边界与 `query_parser._alias_occurrences` 逐行同源复制——隔离门禁止 import query_parser，改动须双同步）从 title+description 回填 species/tissue/disease/chemistry 的**缺失**字段；只填缺失值不覆盖真值、tissue/disease 落笔置 `metadata_provenance.complete=False`（值集不穷尽诚实口径）、命中明细留痕 `provenance.backfill`；双消费点 = 离线快照富化与 `corpus_curation` 联网适配器入库前 | `detect_terms` · `backfill_record` · `BACKFILL_METHOD` · `DIM_TO_FIELD` |
| 数据/IO | `data_quality.py` | 只读内容一致性检查（字段 vs 描述文本 + n_files=0） | `record_caveats(record, catalog)`（内联 caveat，仅物种） · `field_description_conflict` · `DIMS_CHECKED` |
| 数据/IO | `corpus_net.py` | 联网工具组：免 key 通用搜索（DuckDuckGo HTML，主力，限速 ≥1s/超时 ≤12s）+ 官方源轻量适配器（ArrayExpress/ENCODE/10x/HCA + GEO/Zenodo（后接入）+ refine.bio（后接入），对照；10x 为官网私有 API、形状漂移即如实降级，HCA=Azul）统一出口 `{ok, items, note_zh?, error?}`，**绝不抛异常炸链**（网络失败/反爬墙/页面结构变化都如实降级）；每次联网记 `curate_net_ledger.jsonl`；被 `corpus_curation` 单向 import（check_updates 的 ENCODE/10x/HCA/GEO/Zenodo 在线比对走这里） | `search_hca_hits`（HCA 展示/管护共用分页与匹配，页数上限仅在本模块维护；预算耗尽显式说明覆盖不完整） · `search_online_source`（统一出口） · `search_duckduckgo` · `search_encode`/`search_10x`/`search_hca`/`search_arrayexpress_items` · `encode_recent_items`/`tenx_dataset_items` · `parse_ddg_html` · `fetch_text_logged`/`fetch_json_logged`（账本包装） |
| 数据/IO | `net_ledger.py`（2026-10-01 整理批新增） | 联网/审计 JSONL 账本的路径、时间戳、加锁追加**单一真源**（写原语自 `corpus_net` 逐字节抽离；原经 corpus_curation 转发 seam 被 agent/app 三包按私有名跨包取用 14 处，现统一走公共名，corpus_net/corpus_curation 仅留私有别名兼容既有内部引用）；并发账本写必须互斥（Windows 追加写跨并发句柄非原子，实测丢行） | `USERDATA_DIR_NAME` · `NET_LEDGER_NAME` · `net_ledger_path` · `now_iso` · `append_jsonl` |
| 数据/IO | `corpus_status.py` | 数据库状态汇报（`curate.db_status` 的能力真源）：**只读离线不抛**——各源条数/快照日期（复用 `corpus_curation.CHECK_UPDATE_SOURCES` + `_snapshot_local_info`，不复制口径）+ 外部库与回收站清单（复用 `list_curations`）+ 近期审计摘要（读 `curate_net_ledger.jsonl` 尾部窗口）；单点失败降级为该部分如实空缺，绝不掀翻整份汇报。双消费点：agent 图内 execute 节点（`LOOP_TOOLS` 首项）与 `/api/curate/status` 端点（未装扩展时前端 runner 直取） | `db_status` |
| 数据/IO | `introduction.py` | 由已有元数据稳定生成数据集介绍（含 `sample_size_caveats`） | `build_dataset_introduction`（返回 dict 含 `summary`/`facts`/`caveat`/**`sample_size_caveats`**） · `plain_metadata_text` · `meaningful_metadata_text` |
| 数据/IO | `identifiers.py` | 标识符类型识别 + 精确反查 + 诚实 fail-closed（Web `/api/recommend`.identifier_lookup + MCP `lookup_identifier` 共用） | `classify(text)` · `lookup(text, load_records)`（惰性装载：GEO/SRA 直接 fail-closed 不装载） |
| 数据/IO | `identifier_patterns.py` | 标识符**形态识别**叶子模块（正则词表 + `classify` 单一真源，零本包依赖）：`identifiers` 从这里再导出；检索解析层（query_parser，冻结路径上）引它做裸标识符 fail-closed，而不会把反查装配链拖进冻结闭包 | `classify(text)` · `INDEXED_SOURCES_ZH` |
| 数据/IO | `compatibility.py` | 元数据兼容分组（同物种+兼容 chemistry/platform；**只回兼容、非「可整合」**，Web `/api/compatible` + MCP `find_compatible_datasets` 共用） | `find_compatible(seed_uid, records, limit=)` · `CAVEAT_ZH`（必要非充分，常显） |
| 数据/IO | `feasibility.py` | 可行性概览（研究问题→硬过滤全命中集聚合：候选数/Cells 数量合计（不同单位不混加，区间取下界）/物种·平台·年份·来源分布/可下载率/缺口；Web `/api/feasibility` + MCP `assess_feasibility` 共用） | `build_report(survivors, result_total, truncated=)`（`total_cells_lower_bound` 保留兼容字段名，仅合计 Cells；Spots/Nuclei/未知单位不换算，未纳入数单列；`results.js` 消费该统计） |
| 数据/IO | `inspection.py` | 活台账只读层：逐文件存活/体积核验状态 + provision 回写的 integrity 实测档（`i`=verified/mismatch，additive）+ load_smoke 回写的 load 实测档（`l`=loaded/failed，additive；failed 计入 problem 并派生 reason）；无 `i`/`l` 的旧向量仍 unknown | `status_for` · `dataset_summary` · `snapshot_info` · `is_available` · `norm` |
| 数据/IO | `units.py` | 样本量数值+单位的显示归一 + 单位释义 + **样本量语义提醒**（Web/MCP 共用） | `format_sample_size` · `explain_unit` · `UNIT_EXPLANATIONS` · **`sample_size_note(count,unit,n_files=)`**（细胞数≠生物学重复 / 文件数≠样本数 / 单位不明不横向比较 / 仅元数据不判统计功效，按状态条件生成，确定性单一真源） |
| 评估 | `fair.py` | 数据集 FAIR 元数据自检 + 投稿数据可用性说明（纯函数，按已有字段判定，不联网、不猜） | `build_fair_report` · `assess_fair` · `build_data_availability_statement` · `PASS`/`PARTIAL`/`UNKNOWN` |
| 数据/IO | `downloads.py` | 按 uid/url 查真实文件级直链 | `get` · `primary_url` · `fastq_url` · `has_fastq` · `files_for` · `file_count` · `all_real_urls` · `is_available` |
| 数据/IO | `sample_supplement.py` | 10x 平台信息补充旁挂账本（2026-08 手工整理 Excel → `scripts/build_sample_supplement.py` 生成）：样本量**只补缺不覆盖**、检测基因数、次要指标事实行；照 downloads 先例优雅降级 | `get` · `count_fill` · `gene_count` · `count_note` · `extra_facts` · `is_available` |
| 数据/IO | `lab_ledger.py` | 实验室资产台账（**只读·纯校验和·不联网**）：扫本地目录树 → 与 10x 文件清单（15119 md5/大小/文件名）比对成台账。只算 md5 不读内容；巨文件跳 md5 只按名+大小；外部库无单文件清单→无法 md5 核验（见 report.caveat）。CLI `scripts/scan_lab_assets.py` + MCP `verify_local_assets` 共用 | `build_manifest_index` · `scan_directory` · `match_local_file` · `build_ledger_report` · `file_md5` |
| 视图/投影 | `item_view.py` | 记录 → 展示层 item 的**单一真源**投影（Web `/api` 与 MCP 共用；含年份解析真源）；`metadata_provenance` 与 `ena_study_accessions` 随结论复制传递，供 introduction/provenance/reuse_pack 保留逐记录核验语义，前端既有展示字段不变 | `build_item(record, include_introduction=)` · `published_year` |
| 数据/IO | `reachability.py` | 按下载 host 推断国内可达性**启发**（非实测速度；卡片徽章，item_view/workflow 共用） | `classify(url)` |
| 数据/IO | `summary_genre.py` | 数据集叙述**体裁路由**单一真源（prose/title/scaffold；截断判定 + 剥模板；LLM 导读按此路由） | `classify(item)` · `is_truncated` · `strip_scaffolding` |
| 数据/IO | `provenance.py` | 复用出处事实抽取（公开编号 / 平台号 / collection DOI / 原始数据出处 / 快照日期 / 英文源名） | `public_accession` · `platform_id` · **`dataset_doi`**（数据记录自己的 DOI，如 Zenodo concept/record；与 `collection_doi` **粒度不同不可混用**） · `collection_doi` · `raw_data_provenance` · `english_source_name` · `snapshot_as_of` |
| 数据/IO | `reuse_pack.py` | 复用出处清单（选中数据集 → 投稿材料：英文段 + 清单 + 待核实 + RIS/BibTeX；Web `/api/reuse-pack` 与 MCP `build_reuse_pack` 共用） | `build_pack_for_uids` · `sanitize_uids` · `to_markdown` · `to_ris` · `to_bibtex` · `ReusePackError`（RIS/BibTeX 同时携带两类 DOI 且措辞分离："Dataset DOI"=数据记录本体、"Associated publication DOI"=产出该数据的**论文**，不把数据集 DOI 冒充论文 DOI） |
| 查询理解 | `query_parser.py` | 中文 query → 结构化约束（fail-closed）；维度集与词表改由活动领域包经 `domain.registry` 供给（`DIMENSIONS` 留 PEP 562 兼容 shim） | `parse_query(q, mapping, today=)`（today=相对日期基准，默认 date.today()） · `QueryIntent` · `_leftover_negation` 扫描前屏蔽 `NEGATION_EXEMPT_COMPOUNDS`；`_has_conditional` 用负向 lookbehind 防「排除+非」拼出假『除非』（见下「非X 复合词」注） |
| 查询理解 | `query_lexicon.py`（2026-09-23 骨肉分离批自 vocabulary.py 拆出） | 查询词法（语言级：否定/或/填充词/raw 说法/动作词，对任意领域包生效） | `ACTION_MARKERS` · `FILLER_*` · `NEG_*` · `RAW_*` · `OR_MARKERS` 等（与旧 vocabulary.py 词法块逐字一致） |
| 查询理解 | `domains/biodata/vocabulary.py`（同批自 `retrieval/vocabulary.py` 迁入领域包） | biodata 受控词表 + 平台/assay/modality 派生规则（领域数据单一真源） | `CATALOG`（含 species「Non-human Primate」分组、lymphoma 增补非霍奇金别名；SCP/GEO 方法学专名 perturb-seq/snuc-seq/div-seq/dronc-seq/pick-seq + disease aneuploidy + modality 无连字符别名 scrnaseq/snrnaseq） · `derive_assay` · `derive_platform_family` · **`derive_modality(platform_family, chem_text)`**（single-cell/spatial/""，单一真源，normalizer 存字段 + retriever 读 + 评测裁判从 raw 独立重算三方共用） · `DIM_LABELS_CN`（经 `DomainPack.dim_labels_zh` 下发） |
| 领域机制 | `domain/descriptors.py` | 维度描述符：维度名/中文标签/字段访问器/匹配语义（substring|exact）/partial_capable/casefold | `DimensionDescriptor` · `MATCH_SUBSTRING` · `MATCH_EXACT` |
| 领域机制 | `domain/pack.py` | DomainPack 契约（骨肉分离「肉」的声明形状）+ DomainBranding | `DomainPack`（dimensions/catalog/dim_labels_zh/branding/prompts_dir/base_source/facet_order/explain_order/raw_data_labels/vocab） · `DomainBranding` |
| 领域机制 | `domain/registry.py` | 活动领域解析与缓存（骨架访问领域数据的唯一入口；防火墙由 `tests/test_domain_firewall.py` 钉死） | `get_domain` · `set_domain` · `reset_domain_cache` · `domain_dimensions` · `domain_catalog` · `domain_labels_zh` · `domain_base_source` · `DEFAULT_DOMAIN` · `ENV_DOMAIN`（BIODATA_DOMAIN） · `ENV_DOMAIN_DIR`（BIODATA_DOMAIN_DIR，外部包装载） |
| 领域机制 | `domain/vocab_tools.py` | 目录（catalog）驱动的通用词表机制（对任意领域包生效；函数体与旧实现逐字一致，仅数据源经注册表） | `suggestable_terms` · `pinyin_alias_index`（缓存按 catalog 身份键控，换域自动重建） · `reset_vocab_caches` |
| 查询理解 | `search_request.py` | Web/MCP 共用的请求入口解释（来源专名必须在装载语料前处理） | `resolve_search_request` · `SearchRequestResolution` · `SOURCE_ALIASES` · `SOURCE_NEGATION_PREFIX_RE` · `SOURCE_NEGATION_SUFFIX_RE` |
| 检索/排序 | `retriever.py` | 硬过滤 + 打分排序 | `DatasetRetriever`（+ 只读 `coverage_caveats`：诚实降级缺口统计；`relaxation_options(…, include_dead=)`：零命中引导式放宽试算——2026-09-13 夜批起 `include_dead=True` 保留放松后仍零存活的死维，供环内 rerank 探测报告；缺省 False 逐位不变） · `RetrievedCandidate` · `constraint_satisfied(…, lenient=)` · `passes_hard_filter`（按 `intent.lenient_dims` 宽容空字段） |
| 检索/排序 | `rerank.py` | 可选 LLM/listwise 重排 + 可选关键词审核（仅 ride-along 单路；空池独立审核档 随检索工具化删除——零命中救回搬进 ReAct 环 `search.rerun` + `/api/agent/search-rescue`） | `rerank_candidates`（经 workflow 调；`audit_ctx` in/out dict 开审核） · `build_rerank_prompt`（`audit_keywords` 非 None → 审核版 prompt） · `parse_audit_response` · `parse_order` · `_validated_rewrite`（改写校验，ride-along 与环内 search.rerun 共用） |
| 检索/排序 | `ltr_features.py`（2026-09-30 队列② R1 新增） | LTR 融合 **9 维推理期特征构造唯一真源**（自 `vector_recall._ltr_fused_order` 抽离，行为逐位不变，golden 逐位门 `tests/test_ltr_features.py`）：lex 按池内 max 归一 · vec 原始余弦（`dense[i] is None` → has_vec=0/vec_norm=0，生产 dense 路径恒有分恒 1）· 位置 1/(i+1) · 逐维约束命中（生产裁判 `retriever.constraint_satisfied` 对硬约束+软偏好并集）· raw 双槽；intent None → None；判官函数体内局部导入、零重依赖；双消费点 = 生产推理 `_ltr_fused_order` + 离线回放 `research/ltr_data/build_f3_features.py` | `ltr_feature_rows` |
| 检索/排序 | `vector_recall.py` | 可选本地稠密召回/重排（缺模型→降级）；cross_encoder 主进程无 sentence-transformers 时可转常驻 JSONL 外部 worker（`model_runtime.external_cross_scorer`）打分，依赖冲突/worker 崩溃只回退规则顺序；dense 融合三档 `fusion`：`linear`=min-max+α（历史默认、字节等价保留）/ `rrf`=名次融合 k=60（尺度无关）/ **`ltr`=学习排序融合**（**2026-09-14 起 dense/vector 缺省**：本地 RankNet MLP 按 `models/ltr/ltr_spec.json` 特征契约打 9 维推理期特征分（构造单源 `ltr_features.ltr_feature_rows`），制品随仓库发布于 `models/ltr/`（版本血缘与四门指标见 `models/ltr/REGISTRY.json`；重训/转正/回滚走 `scripts/ltr_promote.py`）；模型缺失/intent 缺席/打分畸形一律 fail-soft 回退原默认融合 dense→linear、vector→rrf 并留痕 `ltr_status=fallback`；运行异常返回规则序时 `fusion=rule-order`，不宣称执行备用融合。无工件环境与旧默认逐位一致；产出恒为存活集排列） | `valid_vector`（索引/打分共用形状与有限性守卫） · **`VectorSnapshot`**（不可变向量快照：构造即过 `valid_vector`、缓存范数、写属性即拒——batch 打分复用的只读视图） · **`batch_cosine`**（余弦批打分唯一实现；`VectorSnapshot` 走缓存范数快路径，`failsoft=True` 时坏向量→None 不炸整批） · `recall_rerank`（+`fusion`） · `RECALL_FUSIONS` · `RRF_K` · `default_ltr_dir` · `load_ltr_scorer`（正缓存、失败每次重试） · `recall_backend_ready`（已预热？piped-stdio 安全） · **`recall_backend_available`**（可执行？本地可加载或 API 数据源就绪） · `recall_backend_local_available`（本地可加载？暖机闸用） · `resolve_auto_recall_preference`（auto 偏好序 vector→cross_encoder） · `warm_recall_backend`（经 retriever/workflow 惰性调；**Web 由 `run_web.warm_web_recall` 启动期主线程预热**，避免 auto 首查请求内加载 cross-encoder 而阻塞） · **`semantic_fallback_recall`**（2026-09-12 起：解析弃权且降级救不回时的全库语义兜底——不经条件硬过滤、按查询余弦直排语料索引产出 top-N，嵌入器/索引不可用→None 回退诚实弃权；workflow 标注 resolution_status="vector_fallback"） |
| 检索/排序 | `warn_once.py`（2026-10-01 整理批新增） | retrieval 家族「同一原因只提示一次」stderr 告警**单一真源**（零本包依赖叶子，自 vector_recall 抽离）：共享 `WARNED` 集合 + 带前缀去重判定；vector_recall / rerank / recall_api / vector_index 保留私有别名——共享同一集合对象，测试 `_WARNED.clear()` 复位语义不变 | `WARNED` · `warn_once_prefixed(prefix, key, message)` |
| 检索/排序 | `candidate_text.py`（2026-10-01 整理批新增） | 候选 → 打分文本序列化**单一真源**（自 vector_recall 抽离，行为逐位不变）：带标签结构化字段 + 描述截断 ≤400（消融确认的胜出形态）；稠密索引（vector_index）、API 批处理（recall_api）、本地打分（vector_recall）三处必须吃同一形态；仅依赖 retriever 候选类型，不构成新环 | `candidate_text(cand)` |
| 检索/排序 | `vector_index.py` | 语料级向量索引（vector 召回后端的本地数据源）：全语料候选打分文本用本地 MiniLM 嵌成向量持久化到 `userdata_dir/vector_index/`，按「uid+文本指纹」增量重建（坏档/版本不符整档重建）；构建在短读锁外完成，发布走**两阶段条件提交**——OS 文件锁下先核起点签名/文件代际/模型/语料代（`commit_check` + `build_check`），任一不符即放弃提交（返回 None 回退规则序，绝不发布不一致索引），全部通过才原子替换；查询侧与 `recall_api.api_dense_vectors` 同契约返回 [查询,*文档] 向量，未覆盖条目现场补嵌**仅内存不持久化**；任何失败→None 回退规则序，导入期零重依赖 | `ensure_index` · `index_dense_vectors` · `load_index` · `index_path` · `get_embedder`（本地嵌入器唯一取用口，失败 None 回退规则序） · `EMBED_DIMENSIONS` · `MAX_TEXT_CHARS` · `reset_caches_for_test`；只读快照以 `vector_recall.VectorSnapshot` 表达（不可变视图，供 batch 打分复用） |
| 检索/排序 | `model_runtime.py` | 隔离本地模型运行时的路径与 JSONL 客户端：在线安装的 torch/transformers 全住 `data_root/model-runtime/venv`，主 FastAPI 进程不修改 `sys.path`；打分经该 venv 的常驻 worker，依赖冲突或 worker 崩溃只让 vector_recall 回退规则顺序；READY 就绪判定 = 运行时要件 + 逐文件 **size+sha256** 证据（`model_worker.verify_model_manifest`）：无哈希的旧清单不认证 READY、双模型前的旧 manifest 缺 `embed_files` 则嵌入侧不就绪（fail-closed，显式重装后自动恢复） | `runtime_root` · `runtime_python` · `ready_manifest_path` · `worker_script` · `model_dir` · `read_ready_manifest` · `external_runtime_ready` · `embed_model_dir` · `external_embed_ready` · `_WorkerProcess`（常驻 worker 共享基座） · `ExternalCrossScorer` · `ExternalEmbedder` · `external_cross_scorer` · `external_embedder` · `READY_SCHEMA` · `STARTUP_TIMEOUT_S` · `SCORE_TIMEOUT_S` |
| 检索/排序 | `model_worker.py` | 本地语义模型隔离 worker：被 PyInstaller 作 data 复制到 `_internal/tools/model_worker.py`，由 `data_root/model-runtime/venv` 的独立 Python 执行，刻意不 import 主项目；`--download` 从固定 ModelScope id 下载、失败回退固定 HuggingFace id；每个镜像独立 staging，发布仅认证本次完整文件集（旧模型管理文件可恢复退役；所有同名被覆盖文件先备份，失败逐字节恢复，非同名用户文件保留）；`--serve` 加载本地 CrossEncoder（`--embed` 切换 SentenceTransformer 嵌入器）以 JSONL stdin/stdout 提供有界打分/嵌入；`model_file_manifest(target, integrity=True, include_unmanaged=False)` 产出 `{size, sha256}`（integrity=False 仅尺寸，include_unmanaged 仅完整 staging 取证），`verify_model_manifest` 只认 size+sha256 及完整分片证据；`completed_model_manifest` 返回有效下载证据供安装消费者复用，不重新认证目标残留；`model_generation` 的共享文件签名由编码器句柄、正缓存与索引共用；Windows 以完整文件引用+USN V2/V3 变更 token 配合 stat 失效，可信 token 暖路径不重哈希；无可信 token 时每次签名读取完整 SHA256（O文件字节，可在一次校验中重复读取），不退回 stat-only 成功缓存；非 Windows 保留 ctime 代际 | `MODEL_ID` · `EMBED_MODEL_ID` · `IGNORE_PATTERNS` · `MAX_REQUEST_BYTES` · `MAX_PAIRS` · `MAX_QUERY_CHARS` · `MAX_DOCUMENT_CHARS` · `MAX_TEXTS` · `MAX_EMBED_TEXT_CHARS` · `model_file_manifest` · `verify_model_manifest` · `completed_model_manifest` · `model_generation` · `model_files_ready` · `download_model` · `serve` · `main` |
| 检索/排序 | `strategy.py` | 排序难度分类器（opt-in，纯函数，按候选压力、自由语义量与后端授权选 recall/rerank） | `classify_strategy(intent, records, *, recall_available, llm_available, preferred_recall, top_k, n_survivors, prefer_hybrid)` · `StrategyDecision` · `count_survivors` · `STRATEGY_MODES` |
| LLM | `llm_client.py` | 通用 OpenAI 兼容/智谱客户端；推荐文案与介绍/回执共享完成原因判据，截断、过滤或未知终态保留确定性内容 | `load_llm_config` · `call_llm` · `healthcheck` · `diagnose_network` · `is_complete_finish_reason`；配置加载统一走 `config.load_env_candidates` |
| LLM | `action_plan.py` | 执行侧 NLU **护栏单一真源**：封闭动词表（`VERB_SPECS`/`VERB_BY_NAME`，28 动词〔23 EXEC + 5 ROUTE；rank/rerank/search.rerun 常驻 EXEC、route.request 常驻 ROUTE 元动词；compare.datasets / compat.find / fair.check / results.show / ask.relax 常驻 EXEC（环内专属，无前端 runner——results.show 是屏态唯一出口（2026-09-13 rerank 处置批），ask.relax 是「问用户放松哪个条件」选择卡（2026-09-14 YOLO 批，由 utterance 响应 ask_relax 键驱动）），cite.export 由 plan-only 补上环内执行（双通道）〕）+ `plan_action`（LLM 主力 + 规则兜底 fail-open）+ 极性门（否定取消 → 动词照留 `cancelled=true`，征询/疑问「没」等长掩码；孪生规则解析器由 `tests/test_curate_nlu_twin_parity.py` 机械差分，两处同步改）+ 机械降级闸（词表外 verb 降 none、quoted 非逐字清空、EXEC 无 quoted 整计划降 none）。**`raw_shape_violations`**：公共 raw 形状校验单一真源——返回人读 violations 供 agent 路径在降级发生前拦下自修；`agent_exec._validate_raw` 消费它并只叠加多步专属闸（`tests/test_action_plan.py` 前缀钉） | `plan_action` · **`raw_shape_violations`** · `VERB_BY_NAME` · `VERB_SPECS` · `polarity_blocked` · `normalize_utterance` · `should_use_llm` · `rule_operation_marker` · `ROUTE_QUERY_VERBS` |
| LLM | `agent_exec.py` | 执行侧 Agent 规划与**有界多步执行**（langgraph 编排； **langgraph 地道化迁移**）：**图模块级编译一次**（六节点模块级函数非闭包；`_get_graph()` 懒单例 + 锁护首轮 + `_GRAPH_BUILDS` 计数钉；每请求依赖走 `context_schema` + `Runtime` 注入 `_AgentContext{chat_model, model_name, decide_model, decide_model_name, decide_lane}`〔frozen〕，state 保持纯数据）+ **reducer**（trace/steps/observations/dead_ends/reask_writes/usage_ledger 六个 key `Annotated[list, operator.add]`，节点只返**增量**；`plan.steps` 仍写**全量快照**——前端把非空 steps 当「后端已执行、runner 绝不再跑」的所有权令牌）+ **values 流式**（`stream_mode="values"` 每帧全量态、末帧即终态，零手工合并；`on_event` 按 trace 长度 diff 发增量事件，协议不变；`recursion_limit=50` 框架保险）。节点：route_consensus（常驻**环首**：分流共识——并行 2 票一致即定、分歧加投第 3 票、平票/废票机械兜底 general；按共识装套件工具面与系统提示，全部原始投票落 trace；**2026-09-19 分流三改**：模式化 `BIODATA_ROUTE_MODE`（binary 二元写闸=默认/three_class 文档化兜底，read 套件=general−写工具）+ L0 级联第一级 `_l0_route_verdict`（`BIODATA_ROUTE_L0` 默认开，τ 取工件内嵌工作点，低置信升级 LLM 共识））→ understand（`bind_tools` 挂 VERB_SPECS + agent_schemas **程序生成**的全动词表；三级通道 required→auto→JSON 兜底，收进共享助手 `_invoke_tool_channel`〔`parallel_tool_calls=False` 恒上〕；跌兜底记 agent_fallbacks.jsonl）→ validate（复用 `build_plan_from_raw`/`_finalize` **同一套**机械护栏——公共形状三条（缺 verb/词表外 verb、quoted 非逐字、EXEC 无 quoted）走 `action_plan.raw_shape_violations` 单一真源，本文件只叠加多步专属闸（点名源/幻觉取消/sync 主题/keywords 接地），语义分流是刻意策略非漂移；循环续步产出挂 `loop_plan`，plan.verb 恒首步动词）→ repair≤1（只服务首步；**同走共享助手结构化通道**，不再是无 bind 的散文调用）→ **execute**（`LOOP_TOOLS` 注册表图内真跑：`curate.db_status` 只读 / `curate.check_updates` 只读在线比对 / `curate.search_online` 联网搜+入库 / `curate.sync_updates` 检查→能闭环来源自动入库复合流 / `search.rerun` 换词重检（机械择优闸）/ `rank` 裸新检索（**试跑化**，2026-09-13 rerank 处置批：如实回报 + 机械健康度三态 `_health_of_meta`，**healthy 即 auto-show**——不经 LLM 机械直调 results.show 路径出批） / `rerank` 坏 query **三术式**重检（2026-09-13 夜批：`relax` 槽非空 → 结构化定向放松（探测报告 key 经 `_relax_to_suppressed` 译为 suppressed_constraints，零 LLM 重检原句，applied="relax"；非法 key 抛 `_RerankRelaxParamError` bad_param、对管线零触发）/ **repair 路由**（2026-09-18 专家化批 WP3，**比整句改写更窄的干预优先**：原句未收录词弃权时先试零 LLM 局部纠错 `_repair_route`——受控词表唯一最近邻 + `_repair_diff_ok` **降级基线白名单**（与原句同口径 `strip_terms` 剥未收录词后的降级解析对比：新增限被纠维度、丢失须粘连吸收、排除/角色/极性/raw/date/free_text 变动一律拒、要求解析改善）才自动打补丁，applied="repair"；等距歧义/无候选/白名单不过 → 不采纳，候选菜单经 `_rewrite_repair_zh` 注入改写调用作 advisory；采纳但试跑零存活时 `_repair_note_zh(empty=True)` 降级「仅供参考」措辞——WP3 评测实证可见误纠全部伴随零存活，覆盖不过的纠正不配断言式呈现；数据真源 `research/agent_eval/wp3_repair_eval_0918.py`；**2026-09-18 WP5 拼音扩容**：候选三通道——整串编辑距离（via=edit）/ 近似包含（via=substring，残渣嵌影证据弱，**一律 advisory 不自动采纳**——『黏液纤毛→血液』可见误纠红线实证）/ 拼音（via=pinyin，`pinyin_alias_index` 整串连写 + `_repair_pinyin_spans` 把被空格切碎的相邻纯拼音词拼回长串——fei xian wei hua→肺纤维化；只自动采纳 dist=0 唯一命中，同音碰撞一票否决）；数据真源 `research/agent_eval/wp5_pinyin_eval_0918.py`）/ 缺省 → 改写通道（`_relaxation_probe_zh` 对当前条件做确定性松弛试算报告（含 include_dead 死维标注）注入 `_rewrite_query`，仅建议非命令；probe_zh 随结果回传 decide 供下一轮 relax 决策）；试跑化三态出口：屏面健康+试跑 degenerate → 机械驳回 rejected；屏面 degenerate+试跑 healthy → 0→N auto_shown；其余仅 advisory 对照 baseline/comparison_zh 只报事实不判「更好」，上屏归模型经 results.show；**双闸**（2026-09-18 触发子集批，数据真源 `research/agent_eval/trigger_subset_gate_0918.py`）：触发闸=屏面 healthy/unknown 且 total>3 时如实拒绝执行（refused=true/refuse_reason=screen_healthy，零 LLM 零试跑，换措辞走 search.rerun）；语义闸=改写定稿经 `_constraint_diff` 机械对比原句，未授权语义变化（硬约束增删/排除变化/软硬互转/极性翻转/原始数据·时间硬要求变化/解析异常）即 semantic_blocked——健康屏面驳回（rejected + reject_reason=semantic_change，生死线扩档）、空屏面放行但 disclosure_zh 如实标注、其余档 advisory 附警示） / `results.show` **屏态唯一出口**（handle 句柄指认环内试跑实录 → 重放同一条确定性管线 use_llm=False 重建载荷 + 指纹复核 fail-closed；display 槽已退役，其它工具没有上屏权限；独立 `MAX_SHOW=2` 预算闸防 show↔rerank ping-pong；decide 上下文注入「结果不健康」纯情报员告警段 `_screen_alert_block_zh`（2026-09-14 二批：degenerate 试跑结果内嵌 `relax_live` 零 LLM 预演 digest〔`_relax_live_digest`，与弹卡同一 `relaxation_options` 真源〕并随 `_step_projection` 进 decide 投影，告警据此点名活选项〔ask.relax 对因〕或如实标注不适用〔从对策清单摘掉〕——修复 probe_zh 不进投影导致的盲选）） / `ask.relax` **问用户放松哪个条件**（2026-09-14 YOLO 批：屏面 degenerate 机械闸放行 → `retriever.relaxation_options` 确定性预演（零 LLM，死维不入选）生成「放松哪个条件→各救回多少条」选择卡，suppress 清单经 `_relax_to_suppressed` 翻译并与既有 suppressed 合并去重；载荷经 turn 组卷 `ask_relax` 键透传前端渲染，用户点选后原查询+所选 suppress 零 LLM 重搜；不上屏（屏态唯一出口仍是 results.show）、独立 `MAX_ASK_RELAX=1` 预算闸、真发起经 `_is_search_settle_step` 核销检索半任务；闸内情拒绝/解析弃权/无可放松项均为 asked=False 如实拒绝档）（覆盖策略修复不变：rank/rerank/results.show 都吃 `_loop_structured_kwargs(ctx)` 的结构化检索现场（2026-09-17 三径合一：同函数再透传排序杠杆 top_k/recall/strategy，与 pre-loop 摘要、/api/recommend 同口径，缺键回默认逐位不变），回填 applied_* 并机械复核，缺失即 fail-closed——`structured_context_lost` + `batch=None`，绝不放宽重跑顶掉屏上更优批；`_verified_recommend_payload` 是载荷+回填+复核的单一通道） / `route.request` 换线元动词（常驻，均只读）/ `compare.datasets` 对比两数据集（确定性 diff + 独立 LLM 措辞，数字交叉核验）/ `cite.export` 导出 RIS+BibTeX 引文落盘 `.userdata/citations/`（写工具，回执带路径）/ `compat.find` 元数据兼容查找（caveat 恒带）/ `fair.check` 13 项 FAIR 复用就绪度自检（非官方认证，边界句恒带）（后四者均 needs_context——缺省对象取当前结果；search/action 两套件都装）——全自动化预先授权，confirm_token 图内闭环 fail-closed；**run() 出口形状闸**（`LOOP_RESULT_MODELS` model_validate：残缺/错型 = ValidationError = step.ok=False + `error_code=bad_result_shape`；step 经 `Step` 模型 `model_dump(exclude_none=True)`，JSON 契约逐位不变）；每步落 `curate_net_ledger.jsonl` 审计行；异常 ok=False 不炸图；终态码失败记死路账；注册表外动词空过）→ **decide**（仅真跑后进入；**tool-calling 主通道**：绑 15 loop 工具（含 search.rerun/rank/rerank/results.show/ask.relax/route.request 及四个上下文工具）+ `finish` + `unsupported_next_step`〔非 loop 动词真枚举〕——刻意**不是** 28 动词全表，不把不可在循环内执行的动作伪装成可调用工具；回 loop 工具=续步、finish=done、unsupported=婉拒〔declined_zh 语义与旧版逐位一致〕、幻觉工具名/散文=非法=停环、**多 tool_call 取第一个**〔受测 provider 不遵守 parallel_tool_calls=False，多调用是常态且第一个调用经实测合法、循环带新状态再判断后续不吞事；`_classify` 收口分诊并留痕 trace〕+ **同批只读消费**〔第 2..N 个调用里只读白名单（check_updates/db_status）且互相独立的续步经 `_batch_readonly_extras` 机械过滤（参数键严格/逐个裁决闸/去重/步数预算）后随首步同批执行——raw_batch → validate 同口径复检 → execute 逐个真跑留痕；写动词/幻觉名出现即截断、整尾回炉再判；独立只读批量同批执行有实测依据、写动词预发保真不足——故事实边界〕；**仅调用本身抛异常**才跌散文 JSON 兜底（`_DECIDE_RULES_ZH` 全文再问一次 + 记 node="decide" 兜底账），主通道拿到非法应答重问一次（重问后的写动词经强制核销通道放行——落 `reask_writes` 强制核销账，finish 报告须引用其步骤号单独交代，否则核销硬闸拒收）；机械校验=白名单/去重/quoted 逐字/点名源一致性（受控规范名逐字出现于原话则豁免）/搜索覆盖闸（同主题换措辞重搜机械拦截）/死路拦截（`_TERMINAL_STEP_CODES`）+ 连续失败熔断 2（二分：联网二连败=联网暂停整族禁提、非网络二连败=禁提失败动作，均不再硬停）+ `MAX_STEPS=8`（加到顶结算闸——跑满上限时 pending+清单对账双结清则不谎报没做完；写步另有独立预算 `MAX_WRITE_STEPS=2`，search_online 的 network_error 失败可证零副作用不占写步预算）；**finish 硬闸**：必填 `completion_report` 逐项核销，机械检出未完成项 → 拒收收尾并回灌重问一次，第二次仍命中 fail-safe 接受留痕；decide prompt 机械注入「已搜主题清单」）→ narrate（确定性收口；LLM 全程汇报 `plan.report_zh` + 机械后检 `_report_contradiction_reason` 五路弃用回退（含 denied_read：否认真发生过的搜索/检查） + `discard_reason` 留痕；单步 db_status observation/report 旧路径逐位保留；MAX_STEPS 截断如实标注）。plan 契约与 action_plan 逐位同形（`source="agent"` + `trace`；observation/report_zh/steps additive）。**可选依赖**：langchain 只在函数内惰性 import；未装扩展 / `BIODATA_AGENT_EXEC=off` / 大模型未武装 → `turn.route_turn` 捕获 `AgentError` 原样回退 `action_plan.plan_action` 保底，绝不成为新单点。**成功经验 few-shot 库**（Vanna auto_train 式）：成功收尾且**一遍过**（有工具步、全 ok、含 curate.*、非取消；收录质量闸——跌 JSON 兜底/被 repair 修过/finish 被打回/截断/清单剔除任一即不录，「跑通」≠「干得漂亮」）机械产生统一知识 example 草稿提案（分区键=principal+endpoint_fp，request_id 含项目作用域幂等，**不注入**）；用户在记忆审查面评估并真人确认后才成为生效条目。注入侧：understand/decide 统一走 `agent.knowledge.knowledge_context` 完整投影（空库 → prompt 与历史逐位一致；超预算经 `_knowledge_context_safe` 降级空段并留痕，不抛穿主轮）；原 `.userdata/curate_*.jsonl` 为永久保留的迁移原档。**协作规则卡/示例/记忆**（2026-10-09 统一知识空间重构）：三类知识唯一真源为共享会话 SQLite（`app/knowledge_*.py`，见「知识」行）；`collab_rules.py` 是兼容面——读走统一投影、写只提交草稿提案（旧 approve/revoke 入口不再直接生效），旧 top-k 检索注入通道已退役（decide/understand 均走统一完整投影）。
| LLM | `collab_rules.py` | **协作规则/学习候选兼容面**（2026-10-09 统一知识空间重构）：全部读写经 `KnowledgeService` 统一道闸（共享会话 SQLite），原 `.userdata/collab_rules.jsonl` 为永久保留的迁移原档、不再活跃读写。`approve_collab_rules`/`revoke_collab_rules` 只产生草稿提案（approved 恒 0 的诚实计数 + proposed_count/change_id；幂等命中已定案提案时沿旧提案 id 派生 follow 请求编号重开草稿——已定案不复活、不改写），生效须服务端评估 + 真人确认；`list_learning_records`/`get_learning_candidate`/`save_learning_candidate`/`review_learning_candidate`/`revoke_learning_rule` 面向学习与旧审阅面，候选分页与审计同源；撤回先过独立依赖检查、仍须真人提交。旧 top-k 检索注入段已退役（prompt 注入唯一通道是 `agent/knowledge.py` 的 `knowledge_context` 完整投影） | `list_collab_rules` · `approve_collab_rules` · `revoke_collab_rules` · `list_learning_records` · `get_learning_candidate` · `save_learning_candidate` · `review_learning_candidate` · `revoke_learning_rule` · `learning_rules_snapshot` · `ledger_path` |
| LLM | `agent_schemas.py` | 执行侧 agent 的 **pydantic 契约层**：返回契约 `DbStatusResult`/`CheckUpdatesResult`/`SearchOnlineResult`/`SyncUpdatesResult`/`SearchRerunResult`/`RankResult`/`RerankResult`/`RouteRequestResult`/`RollbackResult`/`CompareResult`/`CiteExportResult`/`CompatFindResult`/`FairCheckResult`/`AskRelaxResult`（2026-09-14 YOLO 批新增——ask.relax 选择卡出口；`extra="allow"` 防误杀 additive；测试替身与真表共享同一闸）+ `Step` 实录模型 + LLM-facing 入参 `verb_parameters_schema`（VERB_SPECS 逐动词建模：`confidence` `Literal["high","low"]`、`source` 真枚举（程序取自 `search_request.SOURCE_ALIASES` 规范名）、`limit` `Field(ge=1,le=MAX_LIMIT)`；**required 恒空**——提示层收紧，裁决仍归 `build_plan_from_raw`/`_finalize`，两层不合并）。pydantic 是 fastapi 传递必装依赖，顶层 import 安全（与 langchain 可选扩展地位不同，注释写明） | `Confidence` · `SourceName` · `DbStatusResult`/`CheckUpdatesResult`/`SearchOnlineResult` · `Step` · `LOOP_RESULT_MODELS` · `verb_parameters_schema` |
| LLM | `compare.py` | 数据集对比的**确定性 diff + 措辞层**（`compare.datasets` 能力真源）：`diff_items` 纯函数逐字段产出结构化差异（same/different/only_a/only_b/both_missing，复用 `item_view.build_item` + `normalizer.is_missing_value`）——**事实层**，数字与事实只此一处真源；LLM 措辞只允许翻译 diff JSON，过机械健全性检查（非空/≤800 字/**不引入 diff 事实外的新数字**——`introduces_foreign_numbers` 交叉核验）任一不过退确定性拼接 `render_deterministic`，`wording_source` 如实标注。自身不 import langchain 系（措辞调用在 agent_exec，与 rerank 同纪律）；只被 `agent_exec` 消费 | `diff_items` · `field_value` · `render_deterministic` · `build_prompt` · `introduces_foreign_numbers` · `DIFF_FIELDS` |
| LLM | `turn.py` | 统一对话管线（`/api/utterance` 单一真源；**本模块只路由不执行**——「AI 执行」开时 LLM 分流进入 `agent_exec` 的 langgraph 图，图内 LOOP_TOOLS 才真执行写工具）：编号快速道（零 LLM）→「AI 执行」闸（关=规则直达，操作意图给降级气泡）→ 开=规则匹配概览（`rule_match_summary`，一切指令都过、零命中≠无效）+ 原话 100% 进 LLM 分流（`agent_exec` 优先、任何失败回退 `action_plan.plan_action`）→ search/tool/none；LLM 真判 none 带机械候选 chips；批次常驻：轮内有批时响应 additive 加 `result_batches`/`active_batch`，legacy `result_payload` 镜像 active 批； 覆盖策略修复：每批再加 `scope_fingerprint`（规范化 query+sources+facet/suppressed/lenient+date 的稳定哈希，契约级身份键——前端据它判同/去重/换词批备选）； 初步检索执行闸（2026-09-19 任务3：`_prelim_gate_skip`——L0 判 retrieve/skip 决定 _RagFlight 是否起跑，`BIODATA_PRELIM_GATE` 默认开、混合句不经闸、闸误放由 verdict hook 的 search 判决 lazy 补起兜底）；AgentError 照静默降级、**预期外异常留 node="turn" 脱敏审计行**（agent_fallbacks.jsonl——代码 bug 不再与模型故障吞成同一种静默） | `route_turn` · `rule_match_summary` · `_prelim_gate_skip` |
| LLM | `l0_router.py` | L0 微型分流器**纯 Python 推理**（零新增依赖、微秒级、CPU 本地）：训练侧在 research/route_eval/（sklearn TF-IDF(char 2-4gram)+LogReg+温度缩放校准），生产侧经 `export_l0_production.py` 导出 JSON 工件（`router_models/`，sklearn 数值 parity 证据随 export 报告；缺席/损坏一律 fail-open 回 None 不成为新单点）。消费点：agent_exec 分流级联 `_l0_route_verdict`（三分类/二元写闸两工件）与 turn 初步检索执行闸 `_prelim_gate_skip`（retrieve/skip 工件，τ 内嵌召回优先工作点） | `score` · `available` · `operating_tau` · `reset_cache` |
| LLM | `decider.py` | Jev 决策器（2026-09-20 真·小 Jev 批转正；2026-09-19 复刻批初版）：**Qwen3-1.7B+LoRA 真·RLCD 校准蒸馏**（教师 20 票频率 KL 蒸馏，校准内生），**单前向并行选项打分**（答案位 logits 仅对选项 token 集 softmax → 类型化决策+校准概率，与 `train_qwen_decider.py` 同形状）。定位为 L0 的语义增强上级：同一打分契约覆盖 binary/prelim 两任务（route3 本批退役=死代码清理，自动落 L0），`score_with_fallback` 是唯一调用链（decider→l0→None），τ 按实际判决实现取各自工件工作点（`tau_with_fallback`）。可选依赖（torch/transformers/peft 与 embeddings 同 envelope），缺依赖/缺工件/无 GPU（`BIODATA_DECIDER_DEVICE=cpu` 强制除外）一律 fail-open 回 L0；`BIODATA_DECIDER`=auto（默认）/off/only（评测用） | `score` · `score_with_fallback` · `tau_with_fallback` · `available` · `reset_cache` |
| LLM | 句内子意图清单 | 含多个执行动作的句子先经 `plan_action_intents` 枚举，再由 `turn._partition_intents` 分为环内动作和前端动作；环内动作写入 `intent_checklist`，由状态栏与 finish 闸逐项核销，前端动作只经 `plan.pending_frontend` 接力。前端 `actDispatchPlanChain` 将顶层 plan（兼容读取旧 `plan.intents`）与尾巴按 `verb + canonical slots` 首见去重后串行派发。枚举失败会回落既有单次探测，不阻断旧路径 | `plan_action_intents` · `_partition_intents` · `_intent_checklist_unsettled` |
| LLM | `intro_llm.py` | LLM 中文导读层（**默认随 key：ENABLE_LLM 未设置时有真实 key 默认开/无 key 默认关，显式设置优先·fail-open·只在单数据集介绍端点**；additive 叠 `llm_summary`，绝不替换确定性证据；**短路 mock**、只译 prose/title 体裁、prompt 内嵌防编造铁律与受控词表术语落表） | `enrich_introduction_with_llm(item, intro=, config=)` · `should_use_llm(config, genre)` · `build_intro_prompt(intro, item, genre)` |
| LLM | `prompts.py` | 提示词 | `build_curator_prompt` · `PROMPT_NAME` |
| 编排 | `workflow.py` | 串起全链一次推荐（含共享请求解释、rerank 审核、实际执行追踪）；**未收录词自动降级**（2026-09-10 起：unresolved_term 不再弃权——编排层忽略未收录词、经同一 `_prepare_context` 拿剩余条件重搜，`resolution_status="degraded"` + `degraded_search{ignored_terms,query,active_filters}` 如实标注；硬闸=忽略后一个条件都不剩时不降级；检索层原语不变，冻结评测结构性隔离）；`intent_projection` 含 `unresolved_terms`（additive：OOV 词表闭环的日志钩子与前端回显共用）；`run_with_meta` 主入口收 `RecommendParams` 参数对象（24 检索杠杆的字段/默认值单一真源；`**kwargs` 为存量调用的兼容通道，新调用一律走参数对象；`run_with_meta` 另有可选 keyword-only `before_llm(config)` 回调（additive，**真实 LLM 请求的准入接缝**）——在确定将发出服务端真实请求的前一刻触发（rerank/polish 先到者；mock/未启用/无 key/零候选提前返回一律不触发），同一轮至多一次（rerank+polish 两 stage 合算一轮），状态是 run 级局部闭包（同 workflow 实例并发不串扰）、刻意不进 `RecommendParams`（参数对象须可日志/JSON 序列化）；缺省 None 与旧签名逐位等价） | `DatasetRecommendationWorkflow` · `.run_with_meta(RecommendParams | **kwargs, before_llm=)` · `RecommendParams` · `WorkflowResult`（含 `.interpretation/.search_trace/.strategy/.audit`） |
| 编排 | `model_installer.py` | 安装版可选本地语义模型的可恢复在线安装：独立 venv（固定 Python 3.12.13）+ 固定模型（`BAAI/bge-reranker-v2-m3`），依赖来自随包 SHA-256 lock、uv 只用 wheels；重依赖住 `data_root/model-runtime/venv`，不进 FastAPI 主进程；单飞跨进程锁，失败/取消不写 READY、不影响规则排序；对前端只回状态/阶段/体积，不回本机路径/uv 输出/下载源原始错误 | `model_install_status` · `start_model_install` · `cancel_model_install` · `install_local_model` · `cli_install_local_model` · `status_path` · `install_log_path` · `model_lock_path` · `uv_path` · `ModelInstallError` · `STATUS_SCHEMA` · `PYTHON_VERSION` |
| 配置 | `config.py` | Settings + 环境/路径锚定 | `get_settings()` · `Settings` · `load_env_candidates`（通用 `LLM_*` > provider 别名；进程 env 逐项最高；设了 `BIODATA_LLM_ENV_FILE`（须绝对路径且存在）即以它为 LLM 配置来源、此时**不再读**项目 `.env/.env.zhipu`，未设时才回落项目 `.env/.env.zhipu`，再到默认；`.env.example` 不参与运行）· `external_llm_env_status`（脱敏） |
| 配置 | `runtime_paths.py` | source/portable/frozen 三模式的 install/resource/data 双根单一真源；frozen 用户数据落平台数据根，程序资源只读 | `AppPaths` · `get_app_paths` · `default_data_root_frozen` · `resolve_data_path` · `resolve_resource_path` · `atomic_write_json` · `atomic_write_bytes` |
| 接口 | `request_validation.py` | 检索类请求入参校验**跨端单一真源**：query 四道闸（空/控制字符 Cc·Cf/纯符号/超长 `MAX_QUERY_CHARS=2000`）·ISO 日期（严格档）·倒挂窗口·来源（形状/空白/未知，`known` 由调用方传入）；Web（`_validate_or_400`→400+hint+`X-Error-Code` 头）与 MCP（→`ToolError(code, hint)`）各自翻译、闸体同源——替代此前两份手写并已漂移的内联校验（feasibility 缺来源/倒挂、task-pack 缺倒挂、Web query 缺两道闸） | `ParamValidationError` · `validate_query` · `validate_iso_date` · `validate_date_window` · `validate_sources` · `MAX_QUERY_CHARS` |
| 桌面壳 | `desktop_launcher.py` | 回环 Uvicorn 生命周期、固定端口/单实例/托盘、迁移与浏览器/窗口双启动通道；显式 `workers=1` 并断言（下载 job/壳状态/缓存仍为进程内真源）；`--window` 时由专用 runner 阻塞到关窗，壳失败恢复浏览器+托盘 | `Launcher` · `TrayHandlers` · `RuntimeStore` · `InstanceStore` · `main` · `tray_selfcheck` |
| 桌面壳 | `desktop_launcher_win32.py` | 纯 ctypes Windows 托盘、消息泵、剪贴板与通知实现（无 pystray/Tk/.NET 依赖） | `Win32Tray` · `is_interactive` · `show_message` |
| 桌面壳 | `webview_shell.py` | pywebview 5.4 可选原生窗口：Python sidecar/回环 API 保持不变；建窗时把下载管理器的真实 running 状态绑定到关窗守卫，下载中关窗先给原生提醒；localStorage 持久化、WebView2 预检、外链系统浏览器、进程单 GUI-loop；Windows 标题栏用完整 favicon 品牌图标并以 DWM caption/text/border 对齐页面浅灰色板，不支持时保留系统配色 | `shell_requested` · `make_desktop_opener` · `WINDOW_CLOSED` · `FALLBACK_BROWSER` |
| 接口 | `webapp.py` | FastAPI HTTP（Web API `3.7.0`（路由集合以 `app.routes` 为准，勿在此硬编码会漂移的数量；含 `/`GET+HEAD、`/dataset`GET+HEAD、`/favicon.ico`、可选本地模型 `/api/local-model/status`+`/install`+`/cancel`（独立 venv 在线安装/取消，单飞，失败不影响规则排序）、条件板 `/api/board/plan`、执行层 `/api/action/plan`、统一路由 `/api/utterance`（可选 `req_id` 幂等占用：断流重发同号返回缓存结果，写工具不会跨请求重复执行）、任务包 `/api/task-pack/preview`+`/build`、管护 `/api/curate/plan`+`/apply`+`/check-updates`+`/sync-updates`+`/sync-status`+`/recall`、追踪更新检查 `/api/watch/check`、追踪导出 `/api/artifacts/export-pack`、样例库 `/api/curate-examples/pending`+`/approve`+`/dismiss`、协作规则卡 `/api/rules/list`+`/approve`+`/revoke`（`.userdata/collab_rules.jsonl` 的逐条确认/撤回；撤回 tombstone）、引文下载 `/api/citations/download`（把环内 `cite.export` 落盘在 `.userdata/citations/` 的文件发回浏览器，入参 `f` 只接受裸文件名、目录穿越/`..`/绝对路径拒绝、resolve 后须仍在 `.userdata/citations/` 内、不存在 404、attachment 响应）、真下载 `start`/`status`/`cancel`/`update`（在途增删，additive）、共享会话与复盘 `/api/conversations`+`/api/conversations/{id}`+`/api/conversations/actions`+`/api/conversations/{id}/actions`+`/api/conversations/{id}/replay`+`/api/conversations/{id}/replay/{action}`（与高层 MCP 五工具读写**同一持久现场**））；默认整库 `/api/datasets` 缓存已序列化 bytes，以语料代际自动失效，提供弱 ETag/304 并由 GZipMiddleware 压缩大响应；`/api/diagnose` 为 POST JSON；`/api/fair` = FAIR 自检+DAS；`/api/account/*` = 本地账户（注册/登录/登出/whoami））；服务端 LLM 配置读取一律持 `ENV_LOCK`（`_temporary_env` 请求级注入的串读防线—— dream 模块后补的锁，此前是唯一漏网读取点，钉 `tests/test_dream.py::test_dream_endpoint_config_load_holds_env_lock`）；CSP 不写死脚本清单——`_content_security_policy(page_html)` 对页面内**实际内联** `<script>` 逐段算内容 sha256 授权（带 `src=` 的跳过；HTML 路由按真实渲染页算，非 HTML 响应 setdefault 无哈希版）；请求身份快照 `_request_user` 缓存于 `request.state.account_user`（同请求零重复反查），账户语料隔离经 `bind_patch_scope(user.id)` 绑到请求作用域；LLM 配额闸 `_gate_llm_quota` 按请求有效配置 + 登录身份判定（diagnose 不受 AI 开关影响传 `require_enabled=False`；replay backfill 经 `quota_gate` 在确认会真整理后过闸——被拒 429 零写入、零 key 绑定；`/api/recommend` 经 workflow `before_llm` 在**真实调用前**过闸——auto 分类器实际派生的 off/非 LLM 路径与零候选提前返回零真实请求、不计量不阻断（**此「免费」只限主 workflow 的 rerank/polish stage**：同一请求的后台复盘在 worthy 且确有会话痕迹、将调度 draw 时仍走同一共享回调独立过闸——有额度时耗掉本请求这一轮，额度已尽时不绘图、主流程仍 200），mock/BYOK/trial 按请求落点物化 config 判，rerank+polish 两 stage 合算一轮；同一请求级回调原样共享给复盘落痕 `record_turn(*, before_llm=)`——主重排/润色与后台整理共享一轮额度，复盘侧被拒只阻止绘图（key/session log 已在过闸前留存、fail-soft 不回滚） | `app` |
| 接口 | `conversation_contracts.py` / `conversation_store.py` | shared 会话的容量、分页、有界历史及持久真源；消息/现场/帧引用与任务终态同 SQLite 事务，owner 隔离、revision CAS、显式幂等导入、归档/删除/分支；删除提交不可复活的tombstone并清除内容/任务记录、使学习来源失效，迟回不可写回；文件待清理项持久保存，`has_pending_cleanup()`只读必要元数据、数据库不存在不建库。标准Web ASGI启动发现pending才启动既有ConversationRuntime维护，无MCP/无业务请求亦恢复；无pending保惰性，IO在线程池，最后宿主退出关闭本生命周期创建的runtime，不误关借用实例。启动及heartbeat每周期最多重试16项，项间0.5秒软预算不是单次I/O硬时间保证，不授权LLM或重放业务；帧 `message_boundary` 明确消息上界，物化分支不依赖空 run_id | `ConversationStore` · `ConversationStore.has_pending_cleanup` · `history_block` |
| 接口 | `conversation_service.py` / `conversation_api.py` | Web/MCP 共用回合执行和会话生命周期；复用 turn/Agent/Workflow，完整结果直接交付，持久现场为权威；整个 execute_turn 显式绑定主体补丁与学习项目范围（含最终 Workflow/policy）；HTTP shared utterance、会话 list/get/actions 与 owned replay；owned replay HTTP strict `{data, LLM覆盖}` 解包后调用扁平 service，owner/复盘鉴权先于配置和额度 callbacks；删除响应顶层`ok`及`conversation.deleted/cleanup_pending/detail`如实区分tombstone已提交、会话不可访问与文件待后台清理，统一清理复盘/session log与项目归属；列表刷新失败不撤销已确认删除，不宣称SQLite与文件系统跨介质原子性或备份/已批准规则抹除；浏览器动作如实标 requires_client，不虚报已完成 | `ConversationService`（`list_conversations` / `get_conversation` 为 HTTP/MCP 项目归属公共投影） · `execute_turn` · `build_conversation_router` · `OwnedReplayRequest` · `OwnedReplayData` |
| 投影 | `conversation_results.py` | 完整推荐 envelope、实际状态/最终批次组卷、客户端动作和真实产物投影；policy 结构与紧凑串共用生产锚点，shared 内核版本和旧 Web 适配版本明确区分 | `complete_payload` · `settle_search_result` · `state_from_payload` · `policy_from_snapshot` · `policy_id_string` |
| 准入 | `llm_admission.py` | Web/MCP 共用环境锁与不可变配置物化、身份额度规则和按轮 Admission；慢调用在锁外、凭据不持久化；BYOK/mock/无真实 LLM 不计，trial 独立桶 | `materialize` · `gate_quota` · `Admission` |
| MCP | `mcp_agent_tools.py` | 自持有消息/现场的高层知识库 Agent；在线身份取 Bearer scope，stdio 默认 local、显式启动凭据经同实例令牌库验证；不接受 owner 参数，旧底层工具安全档保留；同步service调用经显式有界调度，不直接阻塞异步协议事件循环，准入总8=慢调用6+agent_task短控制2，满载快速`mcp_transport_busy`拒绝、不无限排队；trusted上下文随调度传递，未启动操作可撤回，取消等待不提前释放仍在执行的容量，由实际worker结束回收；学习批准/替代只能网页真人确认 | `agent_chat` · `agent_conversations` · `agent_projects` · `agent_task` · `agent_replay` · `agent_learning` |
| 知识 | `knowledge.py` / `knowledge_store.py` / `knowledge_service.py` / `knowledge_api.py` | 三类知识的唯一 SQLite 真源（共享会话数据库），系统内置只读、用户与项目作用域；原 JSONL 仅保留作迁移原档。提案明确 create/update/retract/move、目标与持久性（缺省 unknown）→ final 幸存完整影响集逐项评估 → 真人 CAS 提交；超容量完整分批，不 top-k 或严重度折叠。评估版本 `biodata-memory-relations/2` 由 status.capabilities.assessment_version 声明，保存 semantic_pairs/coverage/edit_checks/difference_hash；complete 表示覆盖完整，不等于 committable。update 及 create/update 承接撤回条目时独立核编辑依据、未变义务、before/after 引用及版本摘要，纯撤回保留原确定性合同。旧 pending/draft 评估须重评，已确认历史不重写。来源/权限/配置/目录代与系统摘要 CAS、逐调用计量、只读/依赖和真人确认边界不变；草稿不注入 prompt，完整知识投影仍是唯一消费通道；不接受 actor 赋权 | `KnowledgeService.status` / `entries` / `entry` / `changes` / `change` / `propose` / `assess` / `commit` / `reject` / `snapshot` · `knowledge_context` · `build_knowledge_router` |
| 学习 | `learning_loop.py` / `memory_relations.py` | 单一提示词和工具表的有界 ReAct；关系唯一入口复用纯 SEMANTIC_RELATIONS，old=P/new=H，逐旧项返回五叶或叶外 null，并保全来源、旧版本及全覆盖。比较原文预览由服务端绑定，模型不重复抄录；编辑依据引文另行严格核验，引用存在不证明语义正确。无旧项仍核出处，但不伪造叶、不调用模型猜持久性；身份/操作/scope/permanence 由提案上游提供，兼容保存面显式传 permanence。API-first；显式 relation_predictor 共用同一投影/输出闸，不混传 llm_call 或自动切换；未默认接入研究学生，候选不会自行生效 | `run_learning_loop` · `TOOLS` · `assess_relations` · `ASSESSMENT_VERSION` · `RELATIONS` |
| 语义 | `agent/semantic_relations.py` | 训练/推理共享 stdlib 五叶标签、交换与同域输入；old=P/new=H，按名字 E/E/N/C/N 聚合有限 simplex。`semantic_pair_text` 唯一完整事实序列器；共享入口 keyword-only `limits=None` 默认 `SEMANTIC_LIMITS_PRODUCT`（字符串8192/整对65536），可信研究调用显式 `SEMANTIC_LIMITS_RESEARCH`（65536/262144），pair 不携带 limits。JSONDecoder区间/类型/children/key与安全string边界驱动唯一递归JSON树renderer，recipe为`semantic-json-tree-witness-v3`；`render_semantic_fragments(source, selection)`的v3 `nodes`合同原位替代整数prefix，无旧fallback。两向完整canonical真实含special均fit时逐字保留；否则typed holes、原位members/items计数与多段quoted string外显缺口。按完整entry排序/同entry同步，label-blind差异边界witness最多先占剩余预算一半，再A/B/frame均衡与回流；所有候选按两向实际token max接受，不二分预算/假设BPE单调。逐字段source→rendered segments/omitted ranges/type/结构/选择原因与真实正文token indices审计；多段`rendered_range=null`，visible hash仅含真正可见frame及交换无关entry文本。ML/lean exporter复用同接缝；partial保原gold与分母，`requires_llm_fallback`仅兼容信息不全标记，不自动调LLM或弃权。交换前提是调用者声明的角色无关共享frame，不猜指称、不改gold；无模型依赖、不改旧七类裁决 | `SEMANTIC_RELATIONS` · `NLI_CLASSES` · `SEMANTIC_TO_NLI` · `SWAPPED_RELATIONS` · `SemanticRelationError` · `validate_probabilities` · `project_nli_probabilities` · `swap_relation` · `swap_probabilities` · `validate_semantic_pair` · `swap_pair` · `semantic_pair_text` · `SEMANTIC_INPUT_RECIPE` · `semantic_pair_pieces` · `render_semantic_fragments` · `prepare_semantic_input` |
| 学习 | `learning_store.py` / `learning_service.py` / `learning_api.py` | 回合终态与来源事件同SQLite短事务，原run去重；独立executor/执行锁/epoch/暂停删除迟回闸/每调用额度，配置仅内存，短期授权捕获持久endpoint/scope epoch；MCP与shared chat在物化配置前复用`LearningStore.authorization_snapshot(owner)`，单个只读事务捕获该可信账户的全部endpoint/config与项目scope代际，按实际配置指纹选择旧代际再CAS；捕获失败或冲突只跳过后台授权，主回合继续，run请求如实记录`learning_authorization`，不以取得旧key后的新状态复活授权；status提供`config_epoch`，Web control/trigger以`expected_identity`（可信账户条件、非owner选择）+非负整数`expected_config_epoch`在配置物化/写入前CAS，分别报`learning_identity_conflict`/`learning_config_conflict`；service control/trigger/authorize的keyword参数透传并重验，可信内部旧调用省为None时也必须捕获并比对，旧worker不能借新缓存复活。`control(action=release)`与MCP `release`按trusted身份释放旧endpoint全部项目配置，保留长期consent与候选；MCP内部捕获代际，不接受caller owner/key/confirmed。前端有效AI配置编辑经shell通用callback即时释放旧授权，不等查询；`learning_rules_snapshot`与decide共用scope选择，项目读取本项目+继承全局并保留真实project_id，完整快照CAS覆盖全局变化；replace仅能改同项目，旧list默认不扩返回。候选/审核与统一知识 SQLite 同源，原规则 JSONL 仅为迁移原档；分页、同作用域原子提交、默认关闭、非 trial 和网页真人逐条确认边界不变 | `LearningStore` · `LearningService.status`（`config_epoch`） · `LearningService.control` / `trigger` / `authorize`（`*, expected_config_epoch=None`，仅可信内部兼容） · `build_learning_router` · `learning_rules_snapshot` |
| 接口 | `conversation_runs.py` / `conversation_runs_api.py` | 有界对话执行、账户所属持久回执、事件游标重放、合作取消与重启中断对账；凭据只保留在内存执行闭包。终态回执回传**真实字段**：`completed_result`（完成步快照，来自 `agent.run_control.completed_snapshot`）、`may_have_side_effects`（取消/超时/异常如实标注可能已产生的副作用）、`cancelled`（取消）、`code=run_timeout`（超 `max_seconds` 默认 900 秒时间预算）；排队任务 `future.cancel` 直接取消（`may_have_side_effects=False`），执行中取消在安全边界停手 | `RunStore` · `ConversationRuntime` · `build_runs_router` |
| 接口 | `workspace_store.py` / `workspace_api.py` | 账户归属的对话项目、名称/颜色编辑与持久排序（旧库兼容升级）、归档恢复、对话成员关系与 HTTP 适配；SQLite 连接复用 durable_jobs 的运行态事务机制 | `WorkspaceStore` · `build_workspace_router` |
| 账户 | `accounts.py` | 本地单机用户账户（注册/登录/登出 + 落盘会话 `.userdata/sessions.json`：30 天 TTL、原子写、过期即销毁；**会话快照 schema_version=2——只持久化 token 的 SHA-256 摘要（cookie 仍是不透明随机 token，摘要不能当凭据），旧格式明文 token 只读兼容、下次写入统一升级为 v2**）：scrypt 哈希密码、gitignore 的 `.userdata/` 库、防枚举/节流/fail-closed 存储；`BIODATA_ACCOUNTS_FILE`/`BIODATA_SESSIONS_FILE` 覆盖路径经 `_assert_runtime_path` fail-closed——运行时状态绝不许落仓库 `database/`；会话可选 **Redis 后端**（`BIODATA_SESSION_STORE=redis` → `RedisSessionStore`，协议不变、多进程共享登录态；开关笔误 fail-closed） | `register` · `authenticate` · `create_session` · `resolve_session` · `destroy_session` · `session_token_digest`（摘要口径同时供 Redis 后端复用） · `AccountError` · `SESSION_COOKIE` · `default_store_path` |
| 接口 | `redis_store.py` | Redis 连接唯一真源（`BIODATA_REDIS_URL` 解析/超时/建连，缺包或建连失败翻 `RedisUnavailable`）：惰性单例 decode 客户端 + RQ 专用 bytes 客户端（RQ job 数据是 zlib 二进制）双通道，共用同一 URL/超时配置 | `get_client` · `get_rq_client` · `redis_url` · `redis_error_types` · `RedisUnavailable` · `_reset_for_tests` |
| 账户 | `redis_sessions.py` | 会话 opt-in Redis 后端：与默认 JSON 文件后端同一 SessionStore 协议（create/resolve/destroy），TTL 由 Redis 过期承载，多进程/多实例共享登录态；撤销失败翻译为 AccountError/HTTP 503，成功响应才清 cookie，accounts.js 既有 res.ok 闸负责保留失败时的本地登录态。**旧明文 key 迁移**（`biodata:sess:<token>` → 摘要键）走 WATCH 事务：并发迁移冲突限次重读、迁移同时监听新旧键防撤销竞态、`PEXPIREAT` 取 Redis TTL 与 `expires_at` 中较早到期时间（不把另一请求完成的迁移误判为登出） | `RedisSessionStore`（`create` · `resolve` · `destroy`） |
| 接口 | `durable_jobs.py` | 通用 SQLite 持久 job 镜像（一类任务一行、`kind` 主键）：默认线程路径的状态跃迁写穿透 + 进程重启对账（running 孤儿如实翻 failed）+ 全新进程 idle 回落呈现上轮终态；`BIODATA_JOBS_DB` 覆盖路径同样过 `_assert_runtime_path` | `record_running` · `record_terminal` · `load` · `reconcile_interrupted` · `default_db_path` · `JobStoreError` |
| 接口 | `rq_jobs.py` | corpus-sync 的 opt-in RQ 消息队列后端（`BIODATA_JOB_BACKEND=rq`，笔误 fail-closed）：状态存 Redis hash（五键形状与内存快照逐键一致），web 入队 / worker 进程执行，终态失效经**稳定代际**跨进程传递：worker 把 `corpus_generation` 写进状态 hash，每个 web consumer（`consume_web_invalidate_flag` 的模块级默认实例，可注入独立 `InvalidationConsumer` 实例）各记自己的 last-generation、普通 API 请求即消费，不依赖状态轮询；无代际的历史行按 `legacy:<job_id>:<finished_at>` 合成一次性代（代际读不到时不信任本地缓存）；队列走 bytes 连接、状态走 decode 连接；WATCH 原子抢占状态、每轮 UUID job ID，抢占失败仅附着现有任务；running 按 RQ 状态/执行预算内心跳对账，worker 返回终态副本以补偿 hash 写失败，过期 worker 不能覆盖新任务；整任务显式 60 分钟超时，limits.py 统一预算（向量重建 20 分钟保持默认路径不变） | `backend_active` · `start` · `snapshot` · `corpus_sync_entry` · `consume_web_invalidate_flag` · `JobBackendError` · `RQ_SERIALIZER`（队列/Worker/Job.fetch 共用 `rq.serializers.JSONSerializer`；历史 pickle 作业不提供反序列化兜底，按未完成对账） |
| 接口 | `rq_worker.py` | RQ worker 进程入口（`python -m dataset_recommender.app.rq_worker`）：POSIX 走 `rq.Worker`（fork），无 fork 平台自动回落 `rq.SimpleWorker` | `main` |
| 接口 | `session_log.py` | 对话级 session log（2026-09-28 复盘批，通用机制）：按（principal, 对话）分区的 append-only JSONL 痕迹（用户原话/路由/检索/回复事实流），落盘前整行过 `secret_patterns.redact`（脱敏第一道工序）、按路径锁、fail-soft、`BIODATA_SESSION_LOG=off` 关停；复盘（replay_*）的第一个消费者，也是后续用户模型/评测的数据面。项目痕迹谱系第三类（agent turn 级 trace 与 MCP 接口级留痕之外的 conversation 级） | `append_turn` · `read_records` · `log_path` · `enabled` |
| 接口 | `replay_digest.py` | 复盘整理纯逻辑（零 IO 零 LLM 调用，移植自内部工具「复盘器」digest/condense）：session log 记录 → 浓缩文本（cap 截断保头尾）→ 制图 prompt（动机/成果/知识点/决策点四类 + 因果边、反编造、严格 JSON）；LLM 输出 → 容错解析（围栏/散文容忍、坏项过滤、局部 id 校验）；lint 内容体检（观察模式：越界引用/重名/时间逆向/孤岛/零边） | `condense_records` · `build_prompt` · `retry_prompt` · `parse_digest` · `lint_digest` · `seq_range` |
| 接口 | `replay_store.py` | 复盘产物持久化（`.userdata/replay/<principal>/<conv>.json`，atomic_write_json + 按路径 RLock）：digest 的 LLM 局部 id → 持久 pid 翻译落库（lint 随 digest）；PID/EID 按内容身份复用、永不按新数组位置重解释，内部 point_history 保留已离图条目的原文供旧标注与快照定位，旧格式首次更新自动接续身份；前端 replay_layout/replay_graph/replay_core 按不透明 ID 消费、字段形状不变、标注 threads（含 sidechat 的 "session" 线程）、「添加到对话」快照（打包+清图+快照内追加；快照**自含** `items[].{pid,head,kind,point,notes}` 与关系闭包 `edges`＝两端被标注的边 ∪ 被标注的边本身，且只标边时为其两端补 notes 空的 context 条目——不计 count、不进旧 note 条数，续标可正常追加；`take_snapshot` 与 `compose_annotations` 同一构造源，前端不再自行反查 point/edge）、角标 unseen/已读（worth=成果+决策点、知识点单列）；首写落 replay_key、之后读写必须匹配（匿名分区防串号） | `load` · `save` · `transaction`（同路径读改写锁） · `ensure_key` · `save_digest` · `find_point` · `resolve_edge` · `compose_annotations` · `take_snapshot` · `annotate_snapshot` · `append_thread` · `unseen` · `mark_seen` · **`digest_version`**（单调递增；seen 绑定「用户实际看到的版本」） · `mark_seen(observed_version=)`（等于当前版本才全量核销；落后/超前不猜不核销并回 `stale=true` 与当前版本，旧前端不带该参时保持 legacy 全量核销） |
| 接口 | `replay_service.py` | 复盘编排（fail-soft 总闸，绝不掀翻主路径）：记 turn 经 kw-only 可选 `before_llm` 接入请求级准入（缺省 None 兼容旧调用）——写序为先 key 绑定 + session log 落库，再于确认 worthy 时过闸并调度整理；被拒/异常只阻止后台整理（fail-soft），已落痕迹与 key 不回滚 → 后台静默整理（进程内 daemon 线程单飞、dirty 补跑封顶 4 轮；mock/无 key/trial 不自动整理——按请求落点物化 config 判，试用配额不烧在后台，RQ 后端留交接）→ status/points/annotate/snapshot/seen（observed_version/observed_pids 原样透传 store 的版本绑定核销）/chat（全上下文 sidechat：digest+浓缩痕迹+标注+复盘史；整段 prompt 有 `_CHAT_INPUT_BUDGET=32000` 字符预算，标注按剩余预算截断并如实注明省略字数——磁盘上的完整标注一字不动）/backfill（仅服务端无记录时生效；确认本次确实会整理后、任何写入之前调 `quota_gate`——被拒即 429 **零写入**；过闸后同事务持久化 key 再 append 日志，末次状态保存失败仍保留原 key 授权；手动补建计入 LLM 配额，与「后台自动整理不烧 trial」是两件事）；LLM 走 `llm_client.call_llm`，配置用请求落点物化好的不可变 LLMConfig（后台线程绝不读 env） | `record_turn` · `schedule_draw` · `manual_digest` · `status` · `points` · `annotate` · `take_snapshot` · `annotate_snapshot` · `mark_seen` · `chat` · `backfill` · `drawing` |
| 接口 | `cli.py` | 命令行入口 | `main` |
| 接口 | `mcp_server.py`（`src/dataset_recommender/app/`） | MCP stdio 25 工具、在线23工具（19 基础：16 只读 + 3 写盘——`upload_dataset` 写外部库、`provision_dataset` 写调用方指定 dest_dir、`curate_datasets` 管护外部库 upload_* 与回收站；另 6 个高层 Agent 工具 `agent_chat`/`agent_conversations`/`agent_projects`/`agent_task`/`agent_replay`/`agent_learning`（持久会话/项目/复盘与经授权的独立学习，会消耗模型额度，实现在 `mcp_agent_tools.py`）；学习批准/替代仅网页真人确认，不接受MCP代批；与网页能力对齐；版本以 `_SERVER_VERSION` 为准，当前 v1.38.0（v1.37.0：高层 Agent 五工具落地——身份由传输层注入、不接受裸 account_id；stdio 无 `BIODATA_MCP_ACCESS_TOKEN` = 本机 `local`，与同一实例 Web「本机」同 scope，显式设了就必须是同实例令牌库里已有的令牌（否则启动 fail-closed）；在线 Bearer 高层走服务端配置 + 同一账户额度，旧 19 工具 `force_llm_off` 安全档不放松；工具 19→24、在线 22）；v1.36.0：Wave 3 退役 recommend 的三个环外 LLM 参数，救回统一进 Agent search.rerun 套件；v1.35.0：在线形态落地——同一 FastMCP 实例两用，webapp 挂载 `/mcp`（streamable-HTTP + Bearer 令牌，落盘只存 sha256 摘要）；在线工具（provision_dataset/verify_local_assets 本机文件系统语义不开放、tools/list 直接过滤）；upload_dataset 在线只接受 records，非空 path 返回 online_path_disabled，显式 LLM 参数推离安全档 ToolError 显式拒绝 + 隐式路径 contextvar 降级（llm/scope_gate），账户语料走 bind_patch_scope；stdio 本地形态逐字节不变）；v1.34.0：`curate_datasets` 补齐 `check_updates`/`sync_updates` 执行动作，sync 落盘返回 operation receipt 可整次撤回（对齐 Web `/api/curate/sync-updates`+`/recall`）；v1.33.0：`browse_datasets` limit 硬上限 200→100，与 Web `/api/datasets` 同源 `app/limits.MAX_DATASETS_LIMIT`，超限错误语义一致；调用留痕 schema v0→v1，每行新增 `call_id`，Web `/api/telemetry/mcp-calls` 按行号增量中继；v1.32.0：recommend/task_pack 日期参数拉齐 Web 严格档——非法格式/日历不存在/from>to 倒挂一律 bad_param 点名）；全部工具入口 append-only 调用留痕到 `.userdata/mcp_calls.jsonl`（参数名+字符串值双层脱敏，值级真源 `dataset_recommender.secret_patterns`），`BIODATA_MCP_CALL_LOG=off` 关闭，汇总走 `scripts/summarize_mcp_calls.py`） | `agent_chat`（直接说一句话走完整轮、自己保存现场；同 `request_id` 幂等、`pending` 用 `agent_task` 续读）· `agent_conversations`（list/get/create/archive/restore/rename/delete/select_frame/branch/import；`expected_revision` 防覆盖，import 仅逐条确认的旧史）· `agent_projects`（list/create/update/archive/restore/reorder/assign）· `agent_task`（status/events/cancel/resolve）· `agent_replay`（status/points/annotate/snapshot/annotate_snapshot/seen/chat/digest）· `recommend_datasets`（+`facet_filters`/`suppressed_constraints`/`lenient_dims`/`date_from`/`date_to` opt-in；回显 `coverage_caveats`）· `get_file_manifest` · `parse_constraints` · `browse_datasets`（对齐 `/api/datasets`）· `get_dataset_introduction`（对齐 `/api/introduction`）· `assess_dataset_fair`（FAIR 自检 + DAS，对齐 `/api/fair`）· `lookup_identifier`（对齐 `/api/recommend`.identifier_lookup）· `find_compatible_datasets`（对齐 `/api/compatible`）· `assess_feasibility`（对齐 `/api/feasibility`）· `build_reuse_pack`（对齐 `/api/reuse-pack`）· `plan_query_edit`（接着改条件：一句话 → 一次具体改动，只规划不检索；对齐 `/api/board/plan`）· `plan_action`（一句话要做什么 → 封闭动词表里的一个动作，只出计划不执行；`quoted` 保证是用户原话字面子串、否定取消时动词照留并标 `cancelled=true`（执行层据此不执行只回音）；对齐 `/api/action/plan`）· `build_task_pack`（一句话任务包：四件套口径一致；对齐 `/api/task-pack/*`）· `verify_local_assets`（对齐 CLI `scan_lab_assets.py`）· **`provision_dataset`**（按需真下载到调用方 dest_dir：download_executor 单一真源；默认 scope=primary 只下主文件、max_files 默认 50 硬上限 500 超限报错、dry_run 预演不写；fail-closed 绝不写 database/；对齐 CLI `provision_dataset.py`）· **`curate_datasets`**（对话式数据库管护，写工具 ③：action=list/import/search_online/remove/restore/check_updates/sync_updates 共用 corpus_curation 单一真源；plan 默认 dry_run 零写盘、apply 回传 confirm_token 才写盘/联网、token 不符零写入；sync_updates 显式 dry_run=False 才执行（预演拒绝）；search_online 联网限官方源注册表 + 请求账本 + 实体级去重（同来源编号/同页面链接已在库中则跳过并回显 skipped_existing；apply 落盘前重检防 TOCTOU）；对齐 `/api/curate/*` 与 CLI `curate_datasets.py`）· `upload_dataset`（写 `database/external/`，对齐 `/api/upload`）· `biodata_status` · `biodata_llm_status`；CLI `--selfcheck` / `--llm-check` |
| 数据/IO | `patch_package.py` | 每账户语料补丁包（网页版「基线版本 + 用户补丁包」）：基线共享只读，绑定作用域的写入落本人补丁包；schema_version=1、单包记录/字节上限；所有写入口与回滚 CAS 共用账户级可重入跨进程事务锁（`fs_utils.file_critical_section(lock_path, timeout=60)`：线程 RLock → OS 文件锁，外层摄取锁 → 补丁锁、不反向）；真实 RMW 在锁内捕获前像/写后指纹，UID 读写判等统一 uid_key；`restore_adds` 同 uid 只恢复**最后进入 trash 的版本**（其余版本原样留在回收区），adds 已有重复 uid 即 `patch_corrupt` 拒绝（不静默删记录），只有**选中版本**决定 adds 条数增量（vs `MAX_PATCH_ADD_RECORDS`），字节闸是 `_save_patch` 对**完整序列化补丁**（adds/trash/blocks 全体）在落盘前核 `MAX_PATCH_FILE_BYTES`——任一超限均零落盘，`already_present` 只反映恢复前状态；返回三键 `restored/not_found/already_present` 不变 | `current_patch_scope` · `bind_patch_scope` · `unbound_patch_scope` · `patch_path_for` · `patch_critical_section` · `collect_patch_mutations`（context 收据列表，不持长锁） · `restore_patch_file` · `restore_adds` · `uid_key` · `PatchError` · `MAX_PATCH_ADD_RECORDS` |
| 数据/IO | `labels.py` | 用户可见文案标签**单一真源**（FASTQ 状态/未命名占位/介绍事实标签/课题条件标签；后端呈现层共享，浏览器侧伴侣 `web/static/js/core/copy.js`，跨语言取值有契约测试逐位钉死） | `RAW_FASTQ_YES/NO/GUESS/UNKNOWN` · `raw_fastq_status` · `UNNAMED_DATASET` · `INTRO_FACT_LABELS` · `PROJECT_CONDITION_LABELS` |
| LLM | `search_reply_llm.py` | LLM 检索回执层（fail-open、只在 `/api/search/reply`；「系统回复 = LLM final answer」单通道原则锚点）：确定性事实句 → LLM 原位改写成自然中文，失败留事实句 | `build_search_reply_prompt` · `search_reply_with_llm` |
| LLM | `scope_gate.py` | 在线 MCP 形态的 LLM 成本闸：contextvar 级「本调用禁用真 LLM」——隐式 LLM 使用（`should_use_llm` 只问服务端配没配 key、不问调用来源）在在线形态下强制降级，防令牌持有者烧服务端账单 | `llm_forced_off` · `force_llm_off` |
| LLM | `agent/trace/`（recorder · events · snapshot · rollback · export 嵌套子包） | agent 执行侧**仅追加事件记录器**（可追溯性侧车）：事件信封 {seq, ts, kind, payload}、六类受控 kind（route_decision/llm_call/tool_call/batch_emission/state_snapshot/finish_reason）、每 session+turn 一份 JSONL、追加处校验可 JSON 序列化；mutating curate 的文件级快照（capture/finalize 产出 created/modified/deleted 三清单；`finalize(mutation=…)`：目录模式按工具自报的三类名字过滤；补丁模式消费 `mutation.patch` 的真实 RMW 前像/写后指纹（空列表零变化，失败后真实提交仍如实留锚）；多次 RMW 不连续时拒绝整步回退，未归因变化仅进 `unattributed_*`。补丁快照携 `patch_receipt`；缺可信收据或 CAS 指纹的旧快照明确 conflict，不隐式覆盖；补丁 CAS/归档/恢复共持账户跨进程事务锁）与按快照回退（created 移回收站、modified/deleted 有 preimage 写回原字节、无 preimage fail-closed；**全量 preflight CAS**：每个目标都要满足「当前状态 = 快照写后状态」，任一不满足（含缺写后指纹）进入 `conflicts`/`skipped[].code=state_conflict`，apply 在临界区内**整体零修改**并回 `conflict=true`；CLI 非零退出；`rolled_back_at` 只在真有实际恢复且无 unrestorable/errors/冲突时才写、且只记**首次**完成时间（单调，后续尝试不覆盖））；`apply_rollback` 先锁外只读 routing 定锁（目录模式摄取锁 / 补丁模式账户补丁锁），锁内重读 meta 核 mode/scope 未变且已 finalize，再把全量 CAS 计划、恢复、完成标记与 save 收在同一临界区；不声称抵御硬杀/断电的数据库级原子性；trace 自身故障 fail-soft 绝不掀翻主流程 | `TraceRecorder` · `emit_event` · `active_recorder` · `bind_recorder` · `emit_*` 载荷纯函数 · `SnapshotStore`/`snapshot_store` · `plan_rollback`/`apply_rollback`；CLI `export --turn/--list` |
| 接口 | `limits.py` | Web `/api/datasets` 与 MCP `browse_datasets` 分页上限**单一真源**（=100；改值=两入口对外上限同改，属公开契约变化） | `MAX_DATASETS_LIMIT` |
| 接口 | `llm_quota.py` | 账号级 LLM 调用日配额（只在部署护栏形态 `BIODATA_REQUIRE_ACCOUNT=1` 下经 webapp `_gate_llm_quota` 调用，本机单机形态完全不经过）：按 UTC 日重置、原子写持久化、只计数不记内容；**计数口径**只含「真会消耗服务端 LLM」的请求——BYOK（请求自带 key）/mock/未启用/服务端无 key 一律不计；`trial` 走**独立更紧桶**（`u:<name>:trial`/`global:trial`，默认 per-user 30 / global 500；正式桶默认 100/1000；限额 ≤0 = 该维不限）；账本可再生 fail-open（损坏重建、存储故障放行 + error 日志），路径误配抛 `QuotaPathError`（接口层 503）；调用侧：`/api/diagnose` 不受 AI 开关影响仍计（`require_enabled=False`），replay backfill 在确认会真整理后过 `quota_gate`（被拒 429 零写入），后台自动整理按设计不烧 trial | `check_and_increment(project_root, username, *, trial, per_user_limit, global_limit)` · `usage_snapshot(project_root, username, *, trial)` · `QuotaExceeded` · `QuotaPathError` · `default_quota_path` |
| 接口 | `mcp_tokens.py` | 在线 MCP 接入令牌库（网页版「接入 AI 助手」在线形态凭证）：长期 API 令牌（无 TTL、手动吊销才失效）、落盘只存 sha256 摘要、每账户上限 5、与 accounts 会话库同构不同语义 | `mint_token` · `resolve_token` · `list_tokens` · `revoke_token` · `McpTokenError` · `default_tokens_path` |
| 视图/投影 | `recommend_rows.py` | 卡片行投影与 `/api/recommend` 响应载荷真源（自 workflow 拆出以满足冻结路径隔离闸：卡片行必须经 `content.introduction`，而 workflow 闭包不许触达呈现层） | `rows_from_retrieved` · `recommend_payload` |
| 横切 | `secret_patterns.py`（包根，不属任何子包） | 锚定 secret 值模式 + 扫描/脱敏助手**单一真源**（交付复核、质量门 report 脱敏、MCP 调用留痕值级脱敏、desktop_launcher 日志脱敏四处共用；`scripts/secret_patterns.py` 为兼容壳重导出） | `find_secret_patterns` · `redact` |

> 以下补记块为**历史设计决策记录**（其中版本号与数量为当时口径）；当前状态以模块表正文与 `docs/CHANGELOG.md` 为准。

> **对抗修复契约补记**：`/api/agent/search-rescue` additive 接受与 `/api/utterance`
> 同义的五个结构化筛选字段；`turn.py → agent_exec.py → workflow` 基准/改写/采纳三段同参，
> payload 丢任一显式条件即拒绝换屏。`curate.rollback` 不占正向写预算，独立每轮最多 2 次；
> 候选只认正向写步、跳过 rollback 自身，最新快照缺失/损坏/未 finalize 均 fail-closed。
> 分工定性（落地）：rescue 通道迁移步骤 1/2 已落地（前端去重门禁、披露资产入环），步骤 3 端点收窄进入观察期，暂定「非环路径专用兜底」。

> **环内结果处理四工具补记**：ReAct 环补齐四个「结果处理」工具（LOOP_TOOLS 登记 +
> 动词表常驻，search/action 两套件都装）——`compare.datasets`（确定性字段 diff +
> 独立 LLM 措辞，`agent/compare.py` 事实层单一真源，数字交叉核验）、`cite.export`
> （RIS + BibTeX 双格式落盘 `.userdata/citations/` + 路径回执，补前端旧路径只下 .ris
> 的缺口）、`compat.find`（包 `compatibility.find_compatible`，CAVEAT_ZH 恒带）、
> `fair.check`（包 `fair.build_fair_report`，复用者视角就绪度、非官方认证，边界句恒带）。
> 缺省对象取「最近一批检索结果」（重跑标准管线取 uid，记录本体走全量语料）；各配独立
> 预算（MAX_COMPARE/MAX_CITE_EXPORT/MAX_COMPAT/MAX_FAIR），提示词 `prompts/domains/biodata/compare.md`
> 即真源。验证剧本面（`scripts/evaluate_agent_live.py`）同步登记四工具与 `eval/agent_live_cases_four_tools.jsonl`。

> **MCP 错误契约（v1.10.0）**：**非法请求**（空 query / query 含控制·不可见字符或纯符号无内容或超长 / 枚举越界(recall/rerank/**strategy**) /
> 非正或超上限(100) top_k/rerank_top_n / 非严格整数(拒 "3"·true) / 未知参数名(extra=forbid) / 未知或空白 sources / 坏 uid /
> `browse_datasets` limit·offset 非法(非正/负/超上限 100，与网页 `/api/datasets` 同源) / `get_dataset_introduction` / `assess_dataset_fair` 未给 uid·url·name 或查不到 / name 命中多条同名且 source 消歧失败(`ambiguous_name`，消息附各候选 uid，改用 uid 重调) /
> `upload_dataset` 未给 records·path(`empty_input`)·二者同给(`bad_param`)·path 不存在(`not_found`)·超 64MB(`too_large`)·非 .json(`bad_file`)·非 UTF-8/非 JSON/无记录(`bad_encoding`/`invalid_json`/`no_records`)；
> `provision_dataset` 空 uid(`empty_uid`)·清单外 uid(`unknown_uid`)·dest_dir 空/非绝对路径(`bad_out_dir`)·落受保护区(`protected_out_dir`)·scope 越界或计划超 max_files(`bad_param`)·无白名单(`no_allowed_hosts`)；
> `curate_datasets` 未知动作(`bad_action`)·apply 缺 confirm_token 或缺必传参(`bad_param`)·external/回收站无此文件(`unknown_file`)·非 upload_* 命名空间(`not_curatable`)·token 不符(`token_mismatch`，零写入)·内容整集撞重非 force(`duplicate_content`)·源未注册(`source_not_registered`)·联网失败(`network_error`)·零候选(`no_candidates`)·payload 超 64MB(`too_large`)）
> `raise ToolError`（或 pydantic 校验错误）→ 客户端 `isError=true`
> （机器码 `empty_query`/`bad_query`/`bad_param`/`bad_source`/`bad_uid`/`empty_key`/`not_found`/`ambiguous_name`/`empty_input`/`too_large`/`bad_file`/`bad_encoding`/`invalid_json`/`no_records`/`empty_uid`/`unknown_uid`/`bad_out_dir`/`protected_out_dir`/`no_allowed_hosts`/`bad_action`/`not_curatable`/`token_mismatch`/`duplicate_content`/`source_not_registered`/`network_error`/`no_candidates`，或 pydantic「Extra input」/「Input should be…」）；
> 非法时间表达（近0/负年、非法日历日、并列年份歧义）走**业务弃权** `abstain=true`（非报错）；`get_file_manifest` 自动 strip uid；
> **合法业务结果**（无匹配 / 弃权 / 澄清）仍 `ok=true`、`isError=false`。候选/清单加 `caveats`（数据一致性建议信号）。
> `biodata_llm_status(check_connection=false)` 只读脱敏配置、不联网；显式 `true` 才做最小 Chat Completions 探测，
> 缺 Key/鉴权/限流/端点模型/网络/响应格式问题作为正常诊断结果返回稳定 `error_code`，不返回 Key、密钥路径或原始服务商错误正文。
> 改这些工具的**入参校验或返回形状**前，同步更新 `使用教程/MCP安装/MCP_安装教程.md` 的错误契约段与 `tests/test_mcp_validation.py`。
> v1.8.0 起 `recommend_datasets` 每个 candidate 兼容性追加 `introduction`，与 Web `/api/introduction` 共用 `introduction.py`。
> **v1.15.0**：共享 `build_dataset_introduction` 的返回 additive 加 **`sample_size_caveats`**（`list[str]`，`units.sample_size_note` 单一真源）——样本量语义提醒（细胞数≠生物学重复 / 文件数≠样本数 / 单位不明不横向比较 / 仅元数据不判统计功效），按记录状态条件生成。经此工具的 `recommend_datasets` 每条候选 `introduction`、`get_dataset_introduction`、Web `/api/introduction`、卡片介绍弹窗（`cards.js renderIntroduction` 的 `.intro-ss-caveats`）四处同源。纯只读 additive、无新参/新工具/新错误码；冻结评测不经介绍层，结构性不变。
> **v1.9.0 前端能力同步**：`recommend_datasets` additive 加 `facet_filters`（分面细化）/ `suppressed_constraints`（忽略已命中→放宽）/ `date_from` / `date_to`（发表时间范围）四个 opt-in 参数，与 Web `/api/recommend` 共用 `workflow.sanitize_facet_filters` / `sanitize_suppressed`（单一真源；webapp 私有 `_sanitize_*` 已改薄委托），响应 additive 回显 `query_constraints` / `applied_facets` / `applied_suppressed`；新增只读工具 `browse_datasets`（全库浏览+分面+分页，对齐 `/api/datasets`）与 `get_dataset_introduction`（uid/url/name→确定性介绍，对齐 `/api/introduction`）。工具 5→7。旧工具名、旧字段、默认离线/确定性路径与官方评测逐位不变。
> **v1.10.0 上传能力**：新增第 8 个工具 `upload_dataset`（`records` 结构化数组/对象 **或** `path` 本地 .json 二选一 → 摄取进 `database/external/`，对齐 Web `/api/upload`）。摄取核心抽成 `src/dataset_recommender/uploads.py` 的 `ingest_dataset`，Web 与 MCP **共用单一真源**（webapp `api_upload` 已改薄委托，响应逐位不变）。这是 MCP **唯一有磁盘写副作用**的工具：写入**只落** `database/external/`、**绝不**碰冻结基准 `database/base/`（官方 767 评测走 base-only、永不读外部库 → 基准恒定）；打破「只读」但保「离线 / 永不崩 / 非法→isError / base 不动」不变量；写盘=**非确定性**（时间戳文件名）。工具 7→8。
> **v1.30.0 对话式数据库管护**（管护动作内允许显式联网调官方公开 API；删除类以回收站式可逆删除为前提）：新增第 19 个工具 `curate_datasets`（写工具 ③）+ Web `/api/curate/plan`、`/api/curate/apply` + CLI `scripts/curate_datasets.py`，三入口共用 `corpus_curation.run_curate_action` 单一真源分发。动作：list 清点（纯只读）/ import 本地导入（内容 hash 去重，撞重默认拒绝、`force` 覆盖）/ search_online 联网搜官方源（源适配器注册表首发 arrayexpress，请求账本 `.userdata/curate_net_ledger.jsonl`，候选先审后入不落盘）/ remove 回收站式可逆删除（`.userdata/recycle/` + manifest + 缓存即时失效）/ restore 恢复。两步确认：plan（默认）零写盘返回 preview + `confirm_token`，apply 回传 token 重算比对、不一致 `token_mismatch` 零写入。管护对象限 `database/external/` 的 `upload_*` 命名空间（官方快照 `not_curatable`）；`action_plan` 新增 `curate.list`/`curate.import`/`curate.search_online`/`curate.remove` 四个 EXEC 动词（只出 plan，执行走 `/api/curate/*`）。检索/编排/冻结评测不 import `corpus_curation`（`tests/test_curation_isolation.py` AST 机械门）→ 767 结构性不变。工具 18→19。
> **NLU 护栏化（LLM 主力 + 机械护栏）**：① `action_plan` 生产 prompt 对齐 `prompt_v3`——否定取消**不再整计划降 none**，改为动词照判 + plan additive 字段 **`cancelled: true`**（执行层据此不执行、只回音「好，不做了」；机械极性门与 LLM 自报取或，门优先）；极性门补**征询掩码**（「能不能/要不要/要不…吧」是征询不是否定，`action_plan._QUESTION_HEDGES`）；词表补 **`curate.restore`**（EXEC，暂列 `FRONTEND_UNWIRED_EXEC_VERBS` 豁免——执行入口已有 `/api/curate/*`，前端 runner 随统一窗口接线；**已接线、移出豁免**）；规则兜底 caveat 如实标注「管护操作这一档够不到」。② 新增统一路由端点 **`POST /api/utterance`**（additive）：`board.classify_utterance` 规则分类 → 明确 search（判据单一真源 `board.is_clear_search`：无动作信号/无改动操作/无否定〔存在性问句「有没有」不算〕/**有检索信号**——零检索信号的歧义句不再短路、落入护栏路径）短路零 LLM；refine 候选走 `board.plan_edit`；action 候选/歧义（含带否定的）走 `action_plan.plan_action` 护栏路径（fail-open 兜底同 `/api/action/plan`）；identifier/clarify 如实回音。响应 `{route, board_plan?, action_plan?, echo_zh}`；**仅 action/歧义分支可触发 LLM**。MCP `plan_action` 文档同步 cancelled 语义；既有端点行为逐位不变。

> **执行侧 Agent 化（langgraph 编排；项目主体仍是 RAG，只重做执行侧编排）**：
> ① 新增 `agent_exec.py`（见上表 LLM 行）——`turn.route_turn` 的 LLM 分流**优先**走它，
> 不可用/失败原样回退 `action_plan.plan_action`；`/api/utterance` 响应 additive 加
> `agent:{available,used}`（请求体 additive 加 `agent:bool` 开关），`/api/health` additive 加
> `extensions.agent`。② 动词表新增 **`curate.check_updates`**（EXEC，slots 可带 source）——
> 「检查更新」语义从 `curate.search_online` 的 when_zh **剥出**（此前并入它是问题：
> 「检查10x是否有更新」被判成联网搜数据集）；配套只读能力 `corpus_curation.check_updates`
> （ArrayExpress 真在线比对，复用 `_fetch_logged` 限速出口；离线快照源如实报告 + 官网核对入口，
> 不伪造能力）与新端点 `POST /api/curate/check-updates`（无 confirm_token）。③ 极性门真 bug 修复：
> 词表剔除动作词「删掉/移除/过滤掉/拒收」（EN 剔 "remove"）——它们此前把「删掉我上传的 X」
> 误判 cancelled（检索守卫表语义≠执行极性语义）。④ 前端：runner `actRunCurateCheckUpdates`
> （只读无问卷）；设置大模型子开关组加 `cfgAgent`（health 探测不可用时置灰）；行动流接
> `plan.trace` 后端真实步骤；删 `ACT_PREFILL_STRIP` 补丁与死函数 `actChatNote`。
> 可选依赖 `requirements/requirements-langchain.txt`（不装则行为与未装扩展前逐位一致）。
> 模拟剧本 `scripts/sim_agent_scenarios.py`（离线替身断言 + `--live` 人工核表）。

> **联网源四源化 + held-out 评测 + SCP/GEO NL 检索化**：
> ① `SOURCE_ADAPTERS` 扩为四源——新增 `cellxgene`（全量拉取+本地过滤，300s TTL 缓存）、
> `hubmap`（POST search.api v3：聚合动态推导 dataset_type allowlist + match(operator=and) 全文子句，
> 验证探测证明写死基名会整源漏光）、`single_cell_portal`（全量列表+逐条详情富化）；
> `_fetch`/`_fetch_logged` 加 keyword-only `method/body/headers`（默认 GET 逐位不变，账本形状不变）；
> 来源名归一 `_resolve_search_source_key`（key/label/别名三段，fail-closed；现锚点已收口为 `corpus_net.SOURCE_ALIASES`/`resolve_source_key`）；
> HuBMAP/SCP 端点不供 species/disease → `corpus_enrich.backfill_record` 反标留痕。
> ② 评测体系引入 held-out：盲建 50 条（作者未见解析器与主集；该集后不随公开仓发布）；
> `evaluate_recommendation.py` 阈值参数化（`--expect-*`，默认=冻结常量，主集 97.7/0 逐位不变）；
> 基线阈值钉死首次评测结果（主要 gap 为弃权型措辞盲区，修复留给 dev 集，
> **不得据 holdout 回改解析器**）；质量门 full profile 新增
> `holdout-recommendation-evaluation`；主集仍是唯一发布门。
> ③ SCP(830)/GEO(60) NL 检索补盲：结构化维度官方端点不供全 null → 约束式查询
> 硬过滤整源灭掉（此前只能编号直达的已知缺口）；新共享模块 `corpus_enrich.py`
> 用词表别名反标 title+description 回填缺失四维（只填缺失值、多物种共现保守留空、
> tissue/disease 落笔记录 `complete=False` 值集不穷尽诚实口径）；词表 +6 条目
> （perturb-seq/snuc-seq/div-seq/dronc-seq/pick-seq/aneuploidy）+ modality 别名 scrnaseq/snrnaseq。
> 前端 act.js 来源 token RE 与「英文源」hint 同步四源。
> 验证：全量 pytest 2330 绿；冻结门 97.7/97.7/0 逐位不变；multisource 全 PASS。

> **「工作流即工具」复合流**：
> ① 新复合工具 **`curate.sync_updates`**（EXEC，slots 可带 source）——「检查更新，若有则
> 下载/入库」固定两步流折叠成一个工具：`corpus_curation.sync_updates` 内部写死步骤顺序
> （先 `check_updates` 只读比对 → 能闭环来源〔能在线比对 ∩ 有入库适配器，当前 = ArrayExpress〕
> 的疑似新增逐编号搜回、合并成一个 `curate_sync_*` 批次文件经 uploads 管线入库）；
> 外部库 dataset_uid 集合拦截重复入库（check 只比对官方快照，不查这里会反复再入库）；
> 闭不了环的来源逐条如实写明哪段做不到；无 confirm_token——原子调用无信任边界
> （与 `_loop_search_online` 的 plan→apply 原子化同口径）。配套：端点
> `POST /api/curate/sync-updates`、`SyncUpdatesResult` 形状闸模型、前端 act.js
> 卡片+runner+撤回钮（actUndoSpec 同 search_online 口径）。
> ② **失败语义二分 + 连续失败熔断**（agent_exec）：`_TERMINAL_STEP_CODES`
> （source_not_registered）失败的 (verb,目标源) 记死路账，decide 机械拦截同目标重试
> （可纠正码 bad_param/no_candidates/network_error 不拦截，留给 LLM 换参重试）；
> 最近两步全败不再硬停：联网二连败 → 联网暂停（整族禁提）、
> 非网络二连败 → 禁提失败动作（指纹到 verb），链上剩余独立事项不被连坐。
> ③ 注册表一致性构建期闸 `tests/test_loop_tool_registry.py`：LOOP_TOOLS ↔
> LOOP_RESULT_MODELS 键集锁步 / 注册项字段齐整且 EXEC / _DECIDE_VERB_ORDER 同集合 /
> 槽位专职描述全覆盖（泛模板回退是问题温床，转构建期错误）。
> ④ 盘点结论：pack.download（屏上结果态 + 浏览器下载，UI 交互流）
> 与 dream（素材主权在前端 localStorage，服务端零存储是既定架构）**不**工具化——
> 包装无稳定收益且破坏边界，维持前端通道。

> **复杂度路由 + 视觉回归基线 + none 边界收口**：
> ① **复杂度路由**：`decide_lane(utterance)` 确定性评分（连接词+条件词+max(0,点名来源−1)，阈值 2；**克制语素一票留 simple**，「分别」豁免）→ complex 车道的 **decide 与 repair** 走 `LLM_MODEL_COMPLEX` 档（未配置=路由关闭、行为与单模型逐位一致；understand/validate/narrate 恒 chat——首答分类 chat 够快够准且克制类对 reasoner 零暴露）；45 例定标：complex 27 例、其余全 simple；`_AgentContext` +decide_model/decide_model_name/decide_lane（frozen 不变式不变），`plan_with_agent*` 加显式 decide_model 注入缝，decide/repair trace 留「长链档」档标；tests/test_agent_decide_routing.py 25 钉；
> ② **前端视觉回归基线**：`scripts/visual_regression.py`（--record/--check，容差比对+动态区 mask，咬人证明过）+ `tests/web/visual_baseline/` 11 状态基线；
> ③ understand none 边界收口：判据**就近**进 `build_action_prompt` 现场块（无结果且无当前查询时「改条件无从谈起；动作类照样选」）——边界用例全过；经验：few-shot 示例对 deepseek-chat 几乎无效，判据贴近现场事实才有效。
> Web API 2.3.0；MCP v1.32.1。

> **三闸修复 + thinking 旋钮 + 缓存埋点**：
> ① **点名源确定性补位双站点**（`_autofill_named_source`：validate 节点 + `_adjudicate_decide_obj`
> 裁决——quoted 逐字点名唯一受控来源 + source 缺槽位 → 直接补位、trace 明示，绕过
> 「violation→repair 让 reasoner 借题换动词」失败链；多点名/无点名仍走 violation）；
> ② **点名源闸升格**（`_named_source_violation` 改用 `_named_sources_in` 共享真源：别名 +
> 受控规范名逐字两趟，裸「encode」仍不认、全大写 ENCODE 算点名；旧豁免块成死代码已删；
> 缺槽位消息补「只补 source 槽位，动词本身不用换」——实证的 repair 过纠对抗）；
> ③ **sync 主题闸分句作用域 + 别支豁免**（`_clause_spans`/`_sync_gate_scope_zh`：闸的作用域
> 收到「sync 所引分句 + 条件回指前扩一句」；主题词只在别支且四条件齐备 → 放行；
> 消息 A/B 分流，B 引向 check 填 source）；
> ④ **幻觉取消镜像闸**（`_DENIAL_MORPH_RE`：EXEC 动词 cancelled=true 而原话无否定语素 →
> violation 走 repair；`(?<!分)别` 豁免「分别」、征询句式排除）；
> ⑤ **V4 thinking 旋钮**（LLMConfig +thinking/reasoning_effort、`_build_chat_model`
> 经 extra_body 注入；complex 车道新 env `LLM_COMPLEX_THINKING`/`LLM_COMPLEX_EFFORT`——
> 不配模型名只开 thinking 也建第二 client，官方旋钮替代双模型路由；实测契约：别名
> =v4-flash、effort 七档、thinking 拒 required、strict /v1 直收（裁决不进代码）、
> 缓存 64-token 块命中）；
> ⑥ **缓存埋点**（`_usage_record` 抠 usage_metadata.input_token_details.cache_read，
> `_invoke_tool_channel` +usage_sink，understand/repair/decide/narrate 全节点接线，
> state +usage_ledger（operator.add 第六个累积键），终态汇总 plan.llm_usage
> {calls, input_total, cache_read_total, cache_hit_rate}——空则缺席，离线钉键集逐位不变）。
> 验证：live 评测严格率达标、三闸零误伤；
> 「10x 认不出」定位为 harness check_10x_unknown 桩（evaluate_agent_live.py:218）的问题，
> 非工具问题（10x 在线比对正常）。

> **decide violation 重问对称化**：
  `_adjudicate_decide_obj` 升四元组（第四件=violation 反馈，只在 `_validate_raw` 违规停法下
  非空）；decide 节点据此带检查意见重问一次，与非法应答重问**共享同一份预算**（每次
  decide 至多一次）；去重/覆盖闸/死路/联网暂停等刻意机械停不重问；重问后放行写步照常落
  强制核销账。该不对称就此根治。search_online bad_param 补修正指引。


> **refine.bio 第 11 源端到端**：早期调研判「无全文搜索」
  是打在 `/v1/experiments/` 上（`?search=` 400），实测 `/v1/search/`（ElasticSearch）
  支持全文 + technology/organism/platform/num_downloadable_samples 过滤。适配器双模块同 zenodo 型：
  ES 模糊 OR 匹配如实标注（"spatial transcriptomics" 实测命中 1.9 万弱相关）、双形状闸
  （search 列表项 vs experiment 详情的 platform_names/platforms 变体）、accession 直达
  （`/v1/experiments/{主 accession}/`，副号 404 如实）、`ordering=-source_first_published`
  水位线 check + sync 闭环（闭环集 AE/10x/HCA/GEO/Zenodo/refine.bio）；限速 ≤60/min
  （无官方红线文档）。首批 curated 切片 300 条（单细胞/单核/空间 + 人/小鼠 + 有可下载
  处理数据，多源召回 → 内容闸 → 跨源 accession 去重）：
  四槽位原生字段充分利用（organism 入库即填、specimen_part/disease 经 samples 端点单页
  富化、count=num_downloadable_samples）；`identifiers._find_records` additive 补
  `alternate_accession` 等值匹配（副号 GSE 直达镜像条目）；检索侧 SOURCE_ALIASES 登记
  refine.bio；act.js 来源 token 剔除正则补 zenodo/refinebio（zenodo 第 10 源时漏同步）。
  前端面板数据驱动自动出现（默认勾选与既有 10 源同口径——zenodo 先例无默认关机制）。


> **DeepSeek V4 迁移 + GEO 降级 + Zenodo 第 10 源 + 评测层升级 + 对抗扩增**：
> ① **DeepSeek V4 迁移**：别名实测=v4-flash（reasoner 别名=thinking 开）；`LLMConfig`
> +thinking/reasoning_effort、complex 车道新 env `LLM_COMPLEX_THINKING`/`LLM_COMPLEX_EFFORT`
> （不配模型名只开 thinking 也建第二 client——官方旋钮替代双模型路由）；实测后
> **推荐 THINKING=on 不配 effort**（effort=low 的质量损失不值其时延收益）；thinking 拒 required 的
> 400 由既有降档链免疫；strict schema /v1 直收但暂未采用（小 schema 边际收益不明）。
> ② **任务清单 JSON 化 + finish 逐条核销**：understand 追加独立轻量调用产清单（simple 零变化，
> 多事项证据闸），条目 anchor 必须原话子串（全半角归一）+ expect_verb 受控枚举；
**零信任对账** `_checklist_unsettled`（按来源覆盖核销、条件豁免双严格且、一婉拒只
  豁免一条 unsupported）；`_finish_veto_all` 聚合否决（四路汇聚一次回灌、教学后缀）；
  pending 提示升硬闸（升闸剔「搜」）；narrate 未决如实标注（不写件数避 number_grounded）。
> ③ **GEO 三通道降级**：NCBI 主断/形状漂移 → E-GEOD 镜像（≤2016，GSE 编号换算）→
  Europe PMC 文献弱兜底；逐跳 note 如实写实际通道与局限；AE 版本监控钉。
> ④ **Zenodo 第 10 源**（NGDC 无 API 暂缓）：Lucene 字段限定检索 +
  mostrecent 水位线 check + sync 闭环（闭环集 AE/10x/HCA/GEO/Zenodo）；限速 20/min、
  legacy/RDM 双形状闸；species 抠不到留空不编；检索主链路 SOURCE_ALIASES 未动（另行评估）。
> ⑤ **评测层升级**：失败聚类段（首败维度×节点×verb 三元组聚簇）+ number_grounded
  数字出处不变量（词边界、步数/百分比/日期豁免）。
> ⑥ **对抗用例扩增器** `scripts/augment_agent_cases.py`：四型变体，expect 机械继承/
  合并写死（LLM 只写 utterance）；首轮放量 156 条入观察集（自检丢弃 5、撞车 11）。
> ⑦ 截断续写（finish_reason=length 且非 tool_calls 截断 → 续写一次，有界）；
  缓存埋点（实测命中率 98.2%+，前缀重排不采用）；checkpointer spike
  验证通过但不产品化（恢复收益≈0、interrupt 闸违背图内闭环哲学）。
> conftest 的全局 stub 需同时挂双名模块对象（测试基建内已处理）。

> **幂等性 + decide 完成闸 + 评测体系 v4 + 在线源收尾**：
> ① `/api/utterance` 幂等（可选 `req_id`：占用/等待/缓存——SSE 断流重发同号返缓存结果，写工具不再跨请求重复执行）；
> ② decide 三件：**finish 硬闸**（`completion_report` 必填逐项核销、未完成项机械否决回灌重问一次、第二次 fail-safe 接受留痕）+ **搜索覆盖闸**（同主题换措辞/加过滤重搜机械拦截，零结果只许一次重试）+ **已搜主题清单注入**双通道 prompt；点名源闸补「受控规范名逐字出现」豁免（裸 ENCODE 恒杀合法续步的结构性陷阱）；narrate 后检四路→**五路**（+`denied_read`：否认真发生过的搜索/检查，结果否认「没搜到」刻意豁免）；
> ③ MCP 日期口径拉齐 Web 严格档（v1.32.0）；
> ④ NLU dev 集 55 条 + 词表修复（否定后缀「的不要/别带」/或句式 filler/VDJ 条目/maize 物种/霍奇金·急髓·急淋亚型拆分/filler「检测」），主集冻结七项逐位不变，holdout 复测通过，质量门 +`dev-recommendation-evaluation`；
> ⑤ 在线适配器 +HCA（Azul：facet 过滤+分页本地匹配）/+10x（官网私有 API，双层形状闸 fail-closed——验证检出官网 786 vs 本地 774 的 12 条疑似新增）；HuBMAP species 过滤改分页补齐；既有五源端点逐一 curl 复核**零 churn**（CELLxGENE curation/v1、HuBMAP /v3/search、SCP site/studies 均已在现行配方，加回归钉）；sync 闭环集 = ArrayExpress/10x/HCA；
> ⑥ 评测 v4：用例 90→**135**（+I 典型场景/J 劣质指令/K 长程任务/L 复杂约束），收紧维度 `report_covers`（汇报须含各 ok 步关键事实）/`chain_complete`（多步链 all-or-nothing）/`ideal_steps`（最优路径步数）+ `--repeat K` pass^k 与不稳定清单 + `load_cases` 启动自检。
> Web API 2.3.0；MCP v1.32.0。

> **GEO 在线化 + narrate 治理 + 复杂度路由**：
> ① decide 重问后写动词改为**放行 + 强制核销**（取代只读闸）——`pending_reask_write` 旗标 → execute 落 `reask_writes` 台账（精确步骤号），`_finish_veto` 新形态 `reask_write_unaccounted`（finish 报告不引用该步即拒收回灌）；投影打 `reasked_write` 标；
> ② GEO 在线适配器（esearch+esummary 双段、`min_interval` 限速 ≤3 req/s、形状闸 fail-closed）：SOURCE_ADAPTERS 7 源、sync 闭环集 +GEO（fixture 复刻真实形状）；
> ③ narrate 治理：真计数基准 +online_recent/local_count/total_records、「同步入库」步骤名引用豁免 + `_WRITE_ZERO_ZH` 零数量豁免、wrote 收窄 record_count>0、汇报规则 3 动词汇既遂含义键 + 规则 7 来源名首现小句带数字（含 0）；
> ④ understand：铁律 9（多件事先做原话最前面那件）；refine.conditions 表内补「现场已有检索」前提；两检索动词 when_zh 划界；跨维「的」弃权裁决维持（NOT(A∧B) 不可表达），提示语补手动筛选出口；
> ⑤ harness：`zero_covered` 遍历 label 全部出现处（假阴性修复）；
> ⑥ 复杂度路由：复杂查询的 decide 走 reasoner 档（chat 档在长链 decide 处易断链）。
> Web API 2.3.0；MCP v1.32.1。

> **v1.11.0 诚实降级层**（缺元数据不再静默判负，与 Web 同步）：`recommend_datasets` additive 加 **`lenient_dims`** opt-in 参——对这些维度上**字段为空**的记录视作通过（无法核验≠不匹配），已知不同值仍排除；与 Web 共用 `workflow.sanitize_lenient_dims` 单一真源。响应 additive 回显 **`coverage_caveats`**（满足其它条件、但某维未标注的记录计数按源分组）/ `applied_lenient`。检索器新增只读 `coverage_caveats(records, intent, facet_filters=None)`（**不被 retrieve/评测调用**，同 relaxation_options / facets 结构性隔离冻结门；`facet_filters` 由同轮 COV-1 修复补入——不传则激活分面时「另有 N 条」会高估，务必与当前存活集同口径）；`constraint_satisfied` 加 `lenient=` 参、`passes_hard_filter` 按 `intent.lenient_dims` 宽容——**默认空集 → 逐位 no-op**，官方 767 评测/CLI/旧调用不变。与 `suppressed_constraints` 的区别：suppress 整维放宽（含已知不同值）、lenient 只纳未标注的。工具仍 8、无新错误码。

> **缺失哨兵的单一真源（收敛，改动诚实层判定前必读）**：数据源用 `unknown` / `n/a` /
> `not specified` / `null` / `-` / `未说明` / `未知` 表达「本字段没标注」——**它们不是取值**。判定走
> `normalizer.is_missing_value` / `MISSING_VALUE_TOKENS`，`retriever._dim_field_present`、
> `retriever.facet_value`（经 `_facet_raw`）、`introduction.meaningful_metadata_text` 三处共用。
> 教训：此前同一概念有**三套**定义（introduction 只认 8 个英文词、facets 另有一套含中文、
> `_dim_field_present` **一套都不用、只判空串**），代价是冻结 base 767 条里 **298 条（38.9%）**
> `disease="unknown"` 被诚实层当成「已核验的真不匹配」→ 该层在旗舰语料 × disease 这一格上 **100% 失效**
> （caveat 恒空、lenient 恒无效），而同一界面的卡片对同一条记录显示「疾病：未说明」。
> **加新哨兵拼法只改 `MISSING_VALUE_TOKENS` 一处，不要在别处再造第四套。**
> 注意边界：哨兵语义**只**用于判定「该维是否已标注」，**不**在归一阶段抹写 `record.disease` ——
> 那是展示字段、也进 `_field_contains` 的评分路径，改它会动到冻结 767。

> **modality 维（单细胞语义受控重基线，v2 授权）**：`单细胞/scRNA-seq/单核…` 不再硬映射 `platform=chromium`，
> 改独立 `modality` 维（single-cell / spatial / ""）。派生单一真源 `derive_modality`：**base 全走 platform_family**
> （chromium→single-cell；visium/xenium/atera→spatial）→ 冻结门**逐位不变**；chemistry 扫描仅对外部（platform_family 空）
> 生效，顺序载荷 spatial→单细胞正信号→bulk 排除→fail-closed 为空字符串。**门级**：`retriever.constraint_satisfied` 必须有 modality
> 分支（否则硬过滤恒真=静默 no-op，spatial 漏入→冻结门 FAIL）。评测裁判 `_record_field_text("modality")` 从 raw **独立重算**
> （不回读 record.modality，保看门狗）。加维=改 `DIMENSIONS` → 检索/facet/chip/workflow 泛型点自动流转；改此维前同步
> `eval/eval_queries.json`(te03/co04/co06/co09/co16 must_match=modality:single-cell) 与 `tests/test_modality.py`。

> **排序难度分类器（strategy.py，v1.7.0）**：`run_with_meta(..., strategy="auto")` 同时看候选数相对 `top_k` 的压力、
> `free_text_terms` 语义信息量和后端授权。precise（≤max(8,top_k)）保持规则序；普通 medium/broad 优先**确定性**
> 本地 cross_encoder；高压力且至少两个自由语义词的 complex 在本地与 LLM 都获准时走 cross_encoder→LLM，缺一则用可用单层。
> 只有 key 不等于授权：MCP 还需 `auto_allow_llm=true`，Web 还需勾选「复杂查询允许自动使用 AI」。默认 `strategy="fixed"` = 用调用方显式后端
> （**逐位不变**）。**冻结门隔离**：官方评测（`scripts/evaluate_recommendation.py`）直调 `retriever.retrieve`、
> 根本不经过 workflow → 分类器**结构性够不到评测**，冻结门无条件安全。**能力旗标**：`recall_available`（MCP 传
> `recall_backend_ready`=已预热、piped-stdio 安全；Web/CLI 传 `recall_backend_available`=可执行（本地可加载或 API 数据源就绪））、`llm_available`
> （MCP 默认 False，显式授权且检测到配置才 True）。决策经 `WorkflowResult.strategy` / `/api/recommend` 的
> `strategy` / MCP `meta.strategy` 只读回显，不参与检索、不影响确定性。

> **非X / non-X 正向复合词（误否定修复）**：`非`（`NEGATION_GUARDS_CN` 单字兜底）与 `non`
> （`non-`/`non ` 分隔时命中 `_EN_NEG_RE`）是"只检测绝不执行"的否定 guard；词首带它们、又未被正向 alias 先消费的
> **正向复合词**（非人灵长类 / 非霍奇金淋巴瘤 / 非编码RNA）此前被误报 `unsupported_negation`。两路修复、均结构性隔离
> 冻结门（`eval_queries.json` 无任何 非/non 查询；改动对不含这些复合词的查询逐位 no-op）：**(A) 可映射维度**走
> `CATALOG` alias 正向消费（非人灵长类→species NHP 并集；非霍奇金淋巴瘤→disease lymphoma；与既有 非小细胞肺癌 同机制）；
> **(B) 无维度可映射**（非编码RNA）入 `NEGATION_EXEMPT_COMPOUNDS`，`_leftover_negation` 扫描否定形素前屏蔽整词 →
> 落 `unresolved_term` 诚实弃权。真负向不受影响（`_residual_salient` 不套用屏蔽是硬护栏；`不要非人灵长类` 仍正确排除 NHP）。
> **已知粗粒度**：非霍奇金/霍奇金淋巴瘤共享 target=lymphoma（不区分亚型，精确剔除 hodgkin 待后续）；短别名 `nhp` 保留、
> `nhl` 因子串撞域内机构名 NHLBI 已剔除。**扩词**：正向复合词长尾（非典型/非经典/NAFLD…）按同法增补，避免碰真歧义否定
> （非糖尿病/非肿瘤等保持弃权）。改此逻辑前同步 `tests/test_negation_exempt.py`、`tests/test_negation_contract.py`。

> **Agent 救回通道**：零命中或未收录词的改写只能进 `search.rerun` 正式套件；检索管线不再自带 LLM 审核或动作判断旁路。

**关键数据边界**
- `database/base/`（原 `shujuku/`）：冻结 784 条 10x 基准（10x-Visium.json 774 + 10x-synced.json 10），**严格**装载。别改内容。
- `database/external/`（原 `shujuku_ext/`）：opt-in 外部库（cellxgene/hca/ebi_scea/arrayexpress/encode），**宽容**装载；上传落这里。
- `src/dataset_recommender/data/download_links.by_uid.json`：阶段二文件级直链索引（`downloads.py` 读）。**不在 `database/` 下**（否则会被语料 loader 误当记录）。
- `src/dataset_recommender/data/sample_supplement.by_uid.json`：10x 平台信息补充（样本量/基因数/次要指标，`sample_supplement.py` 读，`scripts/build_sample_supplement.py` 由手工整理 Excel 重新生成）。同在 `database/` 之外；normalizer 只在 base `total_records` 缺失时回填（不覆盖已有值），`gene_count` 纯展示、不进检索评分。

**外部库字段口径（跨源不一致，改适配器前必读）**
- **多值约定**：`tissue`/`disease` 可为逗号分隔多值，超 8 项按 `ingest_cellxgene._clean_join` 截断成「等N项」——**四个适配器共用这一个函数，别另立格式**（硬过滤 `_field_contains` 是子串包含 → 多值串仍能被单词命中；但分面是**精确等值**，整串会成为一个分面项）。
- **`ebi_scea`（修复）**：`tissue`/`disease` 取自各实验的 **experiment-design 端点**（`/gxa/sc/experiment/{acc}/download?fileType=experiment-design`），是 SCEA 策展**声明值**（带 UBERON/PATO/EFO 本体）。汇总接口 `/json/experiments` 只有**因子名**、无取值——曾据此把字段留空 384/384，导致「人类肺组织」搜不到 Human Lung Cell Atlas。design 文件**逐细胞行**（最大 453MB）→ 小文件整读、大文件按 **Range 跨全文多点取样**（Range 是建议性的，收到 200 必须短路成整读，否则会把全文逐点重拉 N 遍）。每条带 `metadata_provenance{origin,endpoint,complete,total_bytes,sampled_bytes,rows_parsed[,determinate,note,error]}`：`complete`＝**是否整读**；`determinate`＝结论由表头决定（源库无该列，与文件大小无关）。`tissue` 只取 `organism part`，**不并入 `sampling site`**（语义不同）。

**诚实层的第三态（改 `_dim_field_present` 前必读）**
- `retriever._dim_field_present(record, dim)` ＝ 字段非空 **且** `_dim_value_set_complete`（值集穷尽）。**不是只看非空**。
- 为什么：SCEA 抽样得来的 tissue 一旦填进字段，只看非空的旧判据会把记录从「不知道」**静默升格**成「已知」——实测 `tissue=alveolus` 下人类肺图谱 `E-ANND-1` 命中 0、caveat 也不报它、lenient 都捞不回；而字段全空时它至少还被算进「另有 160 条无法核验」。**搜「肺」修好、搜「肺泡」修坏**。
- 语义：抽样值**命中**＝可信证据（走 `_field_contains`，不经本函数）；抽样值**不命中**＝**不构成否证** → 仍报 caveat、仍可被 lenient 纳入。
- 隔离：仅 `constraint_satisfied` 的 `lenient` 分支（opt-in，默认 False 时不调用）与 `coverage_caveats`（评测/CLI 不调用）消费它；无 `metadata_provenance` 的记录（冻结 base/10x 与其余三库）一律视作完整 → 默认路径与冻结 767 逐位不变。契约测试：`tests/test_partial_value_set_honesty.py`。
- **健康态口径（四库统一）**：四库统一把源库显式声明的健康态写成 `disease="normal"`——`arrayexpress`/`hca` 自重抓起与 `cellxgene`/`ebi_scea` 一致，不再按 `_HEALTHY` 抹成空；disease 为空只表示源库真未标注。已知健康不再被 `coverage_caveats` 误报「无法核验」、`lenient` 不再误纳已知健康真负、健康查询 AE/HCA 不再恒 0（不变量见 `tests/test_cross_source_disease_convention.py`）。已知残留：`_field_contains` 子串匹配使 "abnormal" ⊃ "normal"，HCA 2 条既有记录会被「健康」查询误命中（改前即存在；修复触及冻结热路径，暂未动）。

---

## 前端 `web/static/`（原生 ES Module + importmap，无构建）

对话项目由 `panel/workspaces.js` 接线颜色圆点、悬停对话卡片、项目编辑与拖拽排序；`core/workspaces_core.js` 提供纯分组、颜色校验与学习范围函数。卡片历史行由 boot hooks 注入既有 browse 行组件，共享回看/新标签页/重搜/删除，保持模块依赖边界；与研究追踪 `projects.js` 分开。项目/全局学习语义见 [Conversation projects](docs/CONVERSATION_PROJECTS.md)。统一知识窗（2026-10-09 合入）由 `panel/knowledge_core.js`（纯核：常量/校验/请求成形）+ `panel/knowledge.js`（文件夹式浮窗界面层，骨架由 shell 经 boot 注入）承担，一切写入走 `/api/knowledge/*` 道闸。

跨容器布局过渡由 `core.js` 的 `beginLayoutTransition` / `cancelLayoutTransition` 统一处理；`board.js` 的 `cbBeginLayoutTransition` 维护查询界面的节点配置。同步更新在绘制前合并，连续切换从当前视觉位置续接，真实输入控件保留焦点；页面与账户切换主动清理副本。

账户入口 `accounts.initAccounts({identityOnly: true})` 供详情页复用身份解析与跨页身份通知；
等待其 Promise 后才允许收藏读写。首页使用默认初始化模式；身份模式不触碰首页工作区 DOM。

异步界面统一复用 `core.beginRequest(owner, readIdentity?)` / `invalidateRequest(owner)`：
令牌的 `isCurrent()` 同时检查属主的最新请求、账户代号和可选身份快照；成功、错误和收尾
写入前均应复核。关闭/重置显式失效，账户变化由 `setCurrentUser` 统一推进代号。
调用入口均在函数体内，模块顶层只创建本地属主对象，不读取环上的绑定。

浏览控件经 `browse.setBrowseState(patch)` 写入 `bs`，分面消费者经 `results.setFacetState`
提交新数组；导入对象/数组不得原地修改。查询 LRU 缓存仅由 search 私有维护，progress 只管加载状态。

> 前端为原生 ES Module + importmap、无构建。逐模块职责见下文与各文件头注释；**五大红线**
> （缓存令牌 / 隐私 / 诚实层 / GSAP 降级 / 无障碍）改动前必读。

历史上这里曾手抄过一份逐文件职责表，因手抄漂移（缺新模块、留退役模块）而移除——
**这类表只留一份、随改动同步维护**。前端本地存储键的清单在 `core.js` 的 `LS` 对象；遥测：usage/benchfb 每事件/记录使用
`nsKeyFor(base, capturedScope) + ::event/record::<id>` 独立键，per-profile consent，避免多标签页整数组覆盖与切账户误清；
其它用户数据除设置外仍走 `nsKey()` 按登录账户命名空间隔离。

> **统一对话窗口**：`#queryInput` 是唯一输入框（侧栏 `#cbInput` 退役，页签保留为
> 历史/帧列表）。提交链路：`board.js ubSubmit` → `POST /api/utterance`（body 自包含：utterance +
> has_results + 条件板上下文自 `_cbStack` 帧）→ 五档 route 分发——search/identifier→`runRecommend`；
> refine→`cbApplyPlan`/`cbCommit`；action→`act.js actDispatchPlanChain` 规范化顶层 plan 与
> `pending_frontend` 后，逐项复用 `actDispatchPlan` 派发返回的 action_plan
> （不再二次调 `/api/action/plan`；`cancelled=true` → 只回 `reason_zh`、不开面板）；
> clarify→对话记录（`#cbHistory` 的 sys 气泡；「AI 执行」关 + 规则检出操作指令时渲染成
> 降级气泡：accent 浅底 + 「去开启 AI 执行」指路按钮，`data-cbh-settings` → `openSettings()`）。
> 系统回复保留完整正文，结果 pill 在结果区展示对应帧；旧记录无 pill 时提供「查看这轮结果」入口，
> 不生成替代回复的占位链接。`cbViewFrame` 不截断对话；游标不在栈顶时输入条变形成
> `#cbForkBar` 三键：「回到最新」（`cbToLatest` 回栈顶）/「从这里建立分支」（`cbBranchFromHere`——
> **新开浏览器标签页**，`?fork=<convId>:<N>` 落点由 browse.js 等 `ACCOUNTS_READY` 后重建前 N 轮 +
> `cbAdoptAsBranch` 换新 convId，本标签页原样保留）/「回退至此」（二段确认后 `cbRevertToFrame` 剪掉
> 之后全部帧与消息，不可撤销）。分支恢复严格使用调用方提供的历史前缀，停在前缀最后一轮，不沿用原会话的游标或草稿；
> 新身份经 `syncHistConversation({createFrames: true})` 独立保存继承的结果帧，原会话不改写。
> 当前帧**没有任何特殊标识**（is-here 描环/状态气泡、hover 回退按钮、
> 撤销/重做按钮全退役）。发送零反应治理：chat 气泡先上屏、进度路由期起跑（`startProgress` 幂等）、
> 发送键百分比镜像主键里程表。
> ①scope 弹层可改性根修——`openSrcPanel/openTimePanel` 对面板物理位于 `#scopePop`
> 内的关闭请求拒执（document 级「点外收起」/Esc 曾把弹层面板误合成两行标签）；`scopePopOpen` 防御性
> 展开兜底；chip/年份改动改走 `applySourceMode/applyTimeMode(rerunDesc)` 一处同步（面板类/激活态/摘要/
> 重检），不再只写 LS 让「自动识别」假激活。②进度泡（`cbProgressBegin/Done/Drop`）：发送即出的系统
> 回复气泡是 `_cbProg` **状态**而非日志条目（不落盘、不参与帧剪枝，`cbLogClear` 清不掉）——滚动 %
> （`_setPct` 镜像 `#cbProgPct`，重画初值取 `progressPct()`）+ 呼吸点；`cbPushCurrent` 完成摘要
> （冠 `plan.message` 前缀）/ sys 回音天然吸收 / 执行注记与选项预览静默撤下；`cbh-morph` 原位渐变。
> ①telegram 分组——`.cb-history` 轮距从容器 gap 改为逐轮 margin：同一方（用户侧
> say/refine/action 与系统侧 sys）连续消息挂 `.cbh-grp` 收紧 6px，跨方才 18px；进度泡纳入。②每帧
> 「查看历史回复」并入该帧系统泡 footer（`.cbh-view-link` 内联链接）——同一帧系统回应只有一颗泡；
> 无系统泡帧（分面点击细化等）给同一颗链接的独立行形态，入口不丢；当前帧照旧无自指入口。
> 两种「查看历史回复」统一为同一颗 `.cbh-view-link` 低调文本链接（muted 小字、无箭头），
> accent 粗字+箭头与 `.cbh-hist` 气泡按钮两副面孔退役。
> `curate.restore` 前端 runner（`actRunCurateRestore`）已接线、移出 `FRONTEND_UNWIRED_EXEC_VERBS`。
> 侧栏页签更名「对话记录」（输入框已退役、页签内容是历史/帧列表）；action 档
> 路由类 `none` 不再退回 `runRecommend`——如实回音「没听懂」（该按钮已退役，现行 none 档只回音/降级气泡；`search.new`/`lookup.identifier` 仍按新检索跑）。
>
> **统一路由管线 + 微信式布局**：① `/api/utterance` 的 **search 短路设计清除**——
> 旧 `board.is_clear_search`（明确检索句绕过 LLM）与统一管线相悖：管线唯一，**关键词匹配 → LLM 判断
> （检索/工具调用）→ 触发行为**，工具调用句关键词往往零命中、短路把它钉死在检索上（典型例：
> 「查找一个新的、与10x同定位的数据库」会被毙在关键词阶段）。规则保留两件事：编号直查 + 具体条件改动
> （refine 零 LLM；**屏上有结果时「只要…」量词约束档**按 add 收窄——同值幂等如实「没有真的改到任何条件」，
> 异值三选一）；LLM 缺席/失败时 `board.search_shaped` 规则兜底 → search，否则规则判定原样返回。
> ② 前端 `ubDispatchAction`：**curate.* 直派**（requires_results=false，不先按原话检索——当时的
> 「打开确认面板等亲手确认」已改为全自动化：runner 链式 plan→apply 直推）。
> ③ 布局：结果态主区**没有输入框也没有对话界面**——唯一输入入口是
> 侧栏工作卡最下方的微信式输入行 `#chatInput`（发送即清空、默认为空）；`#cbHistory` 静态家改
> `#sideBoardScroll`（仅侧栏收起/移动端回退 hero）；范围控件 `#swScope` 两页签共用。
> ④ 弃权文案精简（「把这些词去掉、或换用规范名再搜，可能有结果」）。⑤ 历史记录由独立页面重做为
> **弹出式浮窗**（非模态、可拖动、右下角缩放；点行本体＝本标签页找回，行尾：新标签页打开 ?conv= /
> 重新检索 / 删除·二段确认）。⑥ 零结果帧也能改条件（refine 闸在「有没有帧」）；
> 查询页再点「智能查询」＝新开对话；账号 chip 挪进设置·账户块；范围触发器改为与发送键同形的圆钮
> （两项皆智能识别时 is-auto 特效），弹层内摘要恒可见、chips/年份下拉当场可改。
>
> **turn pipeline（重写动因：工具句在关键词阶段被毙 + 对话记录丢失的顽固故障）**：
> `/api/utterance` 整段重写为 `turn.route_turn` 单一真源（**WEB_API_VERSION 2.0.0**，breaking）——
> **规则匹配（为检索服务但一切指令都过；产出连同原始查询喂给 LLM，零命中/弃权 ≠ 无效）
> → LLM 分流（`action_plan` prompt 新增命中概览 + 当前查询/当前条件两节；route 动词携带
> `effective_query` 完整检索句）→ search / tool / none**。编号快速道直达 search（零 LLM，编号优先于
> 执行词）；LLM 缺席兜底：动作词→tool 规则档、`board.search_shaped`→search、其余→none。
> **规则 refine 档（chat 路径的 classify_utterance/parse_command/plan_edit 串联）退役**：改条件由
> LLM 据当前查询+条件改写成 effective_query（`board.plan_edit` 本体保留，仅供 /api/board/plan 的
> chip 编辑与 MCP）；`actMaybeAutoAct` 与 `userSubmit` 旧档随之死码退役（前端不再调 /api/action/plan，
> 端点仅为外部/MCP 兼容保留）。前端 `ubDispatch` 三档化：search（改写如实回显；chat 来源
> **keepConv 保对话 + sayPushed 不重复推泡**——「先检索后执行」在 chat 里清掉整段对话曾表现为
> 「聊天记录丢了」）/ tool（EXEC plan 直派）/ none（如实回音——该按钮同步退役）。
> LLM 真判 none 的「没听懂」回音 additive 带 `suggestions`
> （机械生成的 2~3 颗候选动作 `[{label, utterance}]`，前端渲染成可点 chip、点击重新入环——
> 分流代替硬拒；LLM 缺席的规则兜底 none 恒空，管护动词没大模型到场给候选也是死路）。
> `curate.search_online` 的 when_zh 覆盖「检查…有没有新数据/更新」（例句「检查10x数据库是否有更新」
> 实测路由 tool/curate.search_online）。
> 两个易误判为模型侧的问题实为机械 bug——
> ①多 tool_call 由「机械拒绝」改「**取第一个**」（部分 provider 不遵守 parallel_tool_calls=False，
> 多调用时取第一个合法调用）；②极性门补**疑问「没」等长掩码**（有没有/有没/了没/句末「没(有)」——
> 「更新没，有新增就搜来入库」这类句式不得误判 cancelled=true；真否定「别/不要」照抓）。

**前端公共出口与范围逻辑（以真实导出为准）**：`core/shell.js` 的 `llmProviderWire()` 是当前接入方式 wire 值的唯一真源（从 `LLM_PRESETS` 经 `getConfig()` 读出，不复制映射表），由 board 在 init 时注册给 `panel/examples.js` 的 `setEndpointProviderWire`——rules.js 复用 examples 的 `endpointCfg()`（账户 × 学习目标 × base_url/model/provider 四分区的唯一读法）——examples/rules 都不 import `#shell`（环只缩不涨，故走注册注入）；`search/search.js` 的 `searchParamSnapshot(query)` 是发 `/api/recommend`、ubRouteBody 预检索、可行性概览与追踪复用的**同一段参数提取**（改字段两处同变），`invalidateRecommendMemo()` 是语料集合 mutation（上传/删除/恢复/联网入库/同步）后作废查询 memo 的**唯一失效入口**（语料代已进缓存键）；`search/sync_button.js` 的 `startSyncUpdates`/`awaitSyncJob` 是异步同步 job 协议（POST 202 → 有限轮询到终态）的唯一实现，dataset 页一键同步与 act.js 的 `curate.sync_updates` 动词共用，禁止第二份轮询；`core/artifacts.js` 的 `artifactsUpdateProject` 把读-改-写收在**同一把 IndexedDB readwrite 事务**内（多标签页并发不静默丢更新），scope 经 `artifactsScope`/`by_scope` 索引隔离；`panel/replay.js` 的迟回包按（账户 scope × conv）身份闸整段丢弃、已读核销带实际渲染过的版本号（页签激活且面板未隐藏、右图未折叠、current 版本真正渲染且成果/决策节点可见才核销；快照/折叠/读取失败不核销）；`panel/replay_graph.js` 返回面 `{reload, openSnapshot}` additive `viewState(){tab, digestVersion|null}` 与可选 `ctx.onViewChanged(state)`（只在显示身份/tab/版本变化后通知，不新增网络通道），`reload(points, opts)` 的 `opts.redraw=false` 只刷新元数据/角标、不重建编辑面板（同身份新 tab 草稿保留）；`core/project_updates.js` 的自动批次用发起时固定的账户 scope + 请求 token 贯穿列表读取/逐条检查/事务回填/语料代写回/列表通知，失败不写回新代（不虚标 done、下次启动可重试）；`core/shell.js` 的 `webGuardKnown()` 区分「护栏政策未知」与「非护栏」——未知期密钥既不恢复也不删，快照到达后按真实政策一次性落位。

**共享会话/复盘的浏览器公共出口（2026-10-06，以真实导出为准）**：`act/conversation_client.js` 是「与网页同一现场」的唯一浏览器客户端——`listSharedConversations()` / `getSharedConversation(id, {metadataOnly})` / `sharedConversationAction(id, action, revision, fields)` 对齐 HTTP `/api/conversations*`；`sharedHistoryRows()` 把共享清单投影成历史浮窗行；`startConversationRun` / `awaitConversationRun` / `cancelConversationRun` / `pendingConversationRuns` / `forgetConversationRun` / `forgetSharedConversationRuns` 是任务续读与清理的唯一实现；`claimConversationClientAction(row, index, stillCurrent)` 领取**客户端动作**（后端如实标 `requires_client`，不在服务端虚报已完成）；可选的 `stillCurrent()` 在跨标签页锁内、写动作回执前复核当前请求与视图，失效时不领取未派发动作。`core/shell.js` 的 `setAiConfigurationChangedHook(fn)` 注册配置释放回调，按 `fn(previous, current, stillCurrent)` 调用；第三参用于校验原账户与待释放意图仍有效。`panel/rules.js` 的 `releaseLearningConfiguration(previous, current, stillCurrent)` 在管理态可返回 `{ok:true, obsolete:true}`，表示本网页旧释放意图已失效；`obsolete` 是本地回执，不是 HTTP 响应字段。`panel/board.js` 的 `cbOpenSharedConversation` / `cbRefreshSharedConversation` / `cbSharedConversation` / `cbRollConvId` 与 `panel/board_core.js` 的 `cbSharedRestore` / `cbLegacyImport` 负责现场恢复与「旧浏览器历史逐条预览后导入」（旧记录不改写）；`search/browse.js` 的 `openSharedHistory` / `renderHistory` / `initHistWin` 是历史浮窗入口；`panel/workspaces.js` 的 `workspaceEnsureSelect(id, options)` 是项目选择态的统一入口；`core/replay_core.js` 的 `setReplaySharedFn(fn)` 注册共享复盘实现（环只缩不涨，走注册注入而非新 import 边）。

**拆分铁律**（改前端前必读）：全部一方 JS 都是 ES Module；
跨模块引用一律 `import { … } from "#xxx"`——specifier 必须在两页 importmap 与根 `package.json` 的 `imports` 表里**同键**存在（parity 门钉死）；
**任何模块顶层（求值期）代码不触碰别模块的绑定**（import 环上的绑定只许在函数体内使用，防 TDZ）；环成员**只许缩不许涨**——`tests/test_frontend_import_graph.py` 机械看守（全模块具名 import 双端断言 + SCC allowlist + core→board / fav_folders→browse 两条反向边防回潮， 先后切断这两边，18 模块 SCC 降到 11+2+2）；
可变共享状态只许属主模块写、他人一律经 `setXxx` setter（live binding 对外只读）；
`boot.js` 是全应用唯一入口（`DOMContentLoaded` 起 `init()`，各模块 init 全经 import 取）。加模块：新建 `.js` + 两页 importmap 与 `package.json` 同键登记 + 由 boot 或既有模块 import。

---

## 自动化与候选发布

| 文件 | 职责 | 公共契约 |
|---|---|---|
| `automation/quality-gates.json` | 本地与 CI 共用的质量门单一真源 | `schema_version=1`；`fast`/`full` profiles；每个门显式工具、超时、离线/禁密钥/禁模型下载和 fail-closed 回退；full 含 `dev-recommendation-evaluation`（dev 集 55 条回归门——盲区修复的合法迭代集）；held-out 泛化看门狗（盲建 50 条、只用一次）**在私有 manifest 的 full profile 内**（`holdout-recommendation-evaluation`）；其查询集与 graded 答案不随公开仓发布，**公开变体不发布/不含该门**（方法论见 docs/QUALITY_GATES.md）；`installer-contracts` 门把声明契约文件按名逐个收集，缺名明确打印 **NOT covered by this run**（该文件本轮未被覆盖，**不是** pytest skip），**全部缺失时 fail-closed 退出 2**、绝不裸跑空/无界选择；私有与公开两变体的执行命令逐字节同源（仅描述措辞不同） |
| `eval/evaluation-manifest.json` | 公开评测消费者输入真源 | 显式列主集/dev/public-validation 与唯一查询下限；缺文件、坏路径、坏 JSON、查询数不足均 fail-closed；holdout 仅列入 withheld_sets，不被公开消费者读取 |
| `public-mirror.json`（public 工作树生成物，私仓不 tracked）/ `scripts/verify_public_mirror.py` | private→public 可执行镜像合同 | 钉 private 源提交、运行时版本、Python AST 等价、元数据/公开评测/依赖锁一致性、经审阅的 JS 哈希对与 private-only 路径排除 |
| `scripts/evaluate_curate_agent.py` | curate **execution-based** 冻结门（BIRD EX 思想：断言沙盒终态文件而非输出文本） | 金标 `eval/eval_curate_agent.json` 22 例（含 unknown_file/not_curatable/token_mismatch/duplicate 等负例零副作用断言）；`--case` 单例调试 · `--out` 报告；pytest 折叠入口 `tests/test_curate_execution_gate.py` |
| `scripts/probe_decide_format.py` | decide/understand 工具通道**格式问题微验证**——探针捕获 `_invoke_tool_channel` 真实出入（含 tool_calls/invalid_tool_calls），`--replay` 对去重现场逐臂回放 | `--ids` · `--rounds`（捕获，沙箱真 API）；`--replay` · `--replay-rounds`（臂对照）；产物 `eval/agent_decide_capture.jsonl` 不入库 |
| `scripts/benchfb_ingest.py` | benchmark 采集反馈包**接收侧**：用户导出的反馈包 JSON（schema `biodata-benchfb/1`）→ 校验/去重/合并 → 审阅 HTML（标注高亮）+ benchmark 候选 `candidates.jsonl`（query + 系统 top-k + 用户标注 uid + 完成度/原因（旧包为星级）/评语/路由/耗时/环境）。纯标准库、只读输入、坏包记错不炸批 | `benchfb_ingest.py 包.json… [--out 目录]`；产物 `merged.json` / `review.html` / `candidates.jsonl`；pytest `tests/test_benchfb_ingest.py` |
| `services/telemetry-receiver/` | 自动遥测公网接收端：严格 schema/body、packet/event receipt 幂等、IP+profile 双层限流、全局/每日配额、同步 DB 线程池卸载、90 天主包+receipt 清理 | transport `biodata-telemetry/1` 兼容旧包，additive `contract_version=2` 带 prompt/实验/propensity、独立 `training_consent` 与仅计数 `drop_report`；缺版本按 v1、缺训练授权按 false；浏览器从两页 meta 读 endpoint/client credential，明文生产主机须显式白名单；`POST /v1/ingest` 响应含 `packet_id/duplicate/accepted_*` |
| `scripts/telemetry_export.py` | PostgreSQL/SQLite 主包流式导出与跨包精确 join；产出训练/评测原料、人工审阅页和质量报告 | DB 游标每批 500 行；导出 `impressions/interactions/turns/explicit_labels/mcp_calls/benchmark_candidates/agent_trajectories`；污染 policy 降级 `policy_unknown`，`tid/iid/policy/route` 精确归因；`--incremental` 行键幂等；评分字段按**独立 rev** 经唯一 `_accumulate_label_fields` 合并：更高 rev 优先（显式 None/[]/'' 同样覆盖），同 rev 用有限接收序来源 `rating_field_srcs`（每字段 `[packet 行 id, 包内数组位置]`）与行级 `rev_src` 决胜——双方来源已知取更早 DB 接收位、任一侧未知则不反转；**次序无关/一致只限来源可考的新格式行**：同 rev 字段折叠与并入次序无关，正常分窗、重叠旧窗、反向窗口与全库扫描终态一致；**legacy 行来源未知时一律保守保存已知值、绝不从行元信息猜来源**，确定性重建只有对原始事件做全库重扫（不泛说 legacy 窗口结果一致）；窗口种子先规范化为初始 acc；`--accepted` 三态：未指定（None）不动 final、空集显式撤销生成空 final、名单只物化人工接受候选 |
| `scripts/llm_cross_score.py` | LTR 查询-文档对的**跨模型语义打标**（第 3b 阶段）：多个异构评委经 OpenAI 兼容网关打分 → 非空评委四舍五入中位数作训练用共识标签（prompt/0-3 锚点/元数据截断与单模型打标逐字复用，标签可比）；网关 key 只从 env 读、不落盘 | 行发布语义唯一（rowwise / pipelined 两引擎共用 `_publish_row` 规范化机制）：**所有请求评委对每个候选都返回分数才发布**（`_row_complete`/`_finalise_row`/`_compact_rows` 同一路径；`None` 永不当作完成）；磁盘已有分只补缺、评委失败的行保持未发布待重跑；`load_progress` 合并同一 query 旧行的互补 partial——补全即零调用物化（同走发布路径，仅当磁盘已是唯一且相同的规范行才跳过）；每个 `query_id` 只保留一行最新完整行（旧行压缩移除），重跑字节稳定；不把「代码就绪/旧正式 data·models 未重生」说成训练收益 |
| `scripts/build_telemetry_benchmark.py` | 把同意训练/评测且有人类标签的候选冻结为不可变数据集 | query/候选去重；用户、语义簇、时间桶防泄漏分组；graded relevance；train/validation/test JSONL + SHA-256 manifest；输出目录已存在即拒绝覆盖 |
| `scripts/telemetry_parquet.py` | JSONL → lossless Parquet 离线分析层 | PyArrow 依赖独立 hash lock；固定常用标量列 + canonical `row_json` 完整保真；10k 默认流式批；Zstd；输入/输出 SHA-256 manifest；临时兄弟目录完成后原子发布、拒绝覆盖与 `database/base` 输出 |
| `scripts/ranking_interleave.py` | 排序策略的确定性离线 team-draft 交错与点击归因 | `interleave` 按 query+seed 近 50/50 选先手、保持两臂内部顺序、去重并记录 ownership；`credit` 对已知去重点击归因并单列未知点击；JSONL/manifest 哈希、原子写、拒绝覆盖 |
| `scripts/telemetry_pg_load.py` | 真实 HTTP→PostgreSQL 的有界并发验收 | 默认 10/50/100 并发、每档 100 独立 v2 包；输出状态码、吞吐、p50/p95/p99；token 只从参数/env 读且不写报告；默认任一非 200 非零退出 |
| `scripts/ltr_promote.py` | LTR 融合制品的**自动重训→转正→回滚**管线（审计真源 + 四门准入） | 子命令 `status`（攒量：当前训练基准 − 现役 `train_counts`，距 `NEW_QUERIES_THRESHOLD` 还差多少；`--require-ready` 供 cron 判退出码；基准含 `MIX_IN_PATTERNS` 命中的**波次层与可训练层**）· `candidate`（包装 `train_ltr.py`，固定带 `--deterministic`；候落入未跟踪 `research/ltr_data/_candidates/<ts>/`）· `gate`（四门：训练器导出自检 / heldout nDCG@10 不退化 / OOD 冻结对照不输（**只评测层** `research/ood_eval/synth_ood_evalonly_xscore.jsonl`，判据取**未舍入** margin）/ **召回开启的 784**；无论过不过都落 `_gates.json`）· `promote`（写前核验候选指纹 → 归档现役 → 原子替换制品（失败回滚）→ 更新 tracked `models/ltr/REGISTRY.json`）· `rollback <version>`（demote 前先认证 REGISTRY 与 `_archive/` 的**完整历史制品指纹**：单边 legacy 可用；两处都无证据、彼此冲突或指纹形状坏一律拒绝，绝不用现存归档字节重新认证；install 与 promote 同 expected 指纹闸；journal 追加与制品 capture→restore 共持同一 journal sidecar 锁（模型 → journal 锁序），评测/门槛不持长锁）· `history`。真源分工：REGISTRY + `_archive/`（内部材料，`.deliveryignore`）是 git 审计链，`.userdata/ltr_promotions.jsonl` 记全部事件含拒绝；`promote` 只认 `_gates.json` 完整四门全绿，且候选版本/制品与现役对照指纹一致（fail-closed）。阈值/容差是文件顶部常量；OOD 门复用 `scripts/ood_duel.compare_frozen`（公开工具） |
| `scripts/ood_duel.py` | 离线 OOD 排序对比的**唯一实现**：五折混 OOD 重训决斗 + 冻结制品对照 | 簇级零泄漏分层折；确定性对手臂（pos / vec / linear / rrf / 门控）；`--compare-frozen DIR` 跳过重训，直接在同一批 OOD 行上比候选与现役，判据用未舍入 `margin_raw`；旧研究路径只做同一模块对象的导入/CLI 转发 |
| `scripts/split_ood_evalonly.py` | 把 OOD 交叉打分集拆成**可训练层**与**只评测层**，让转正门 3 的评测集不再进训练 | 按 query_id 归一化哈希（空白折叠 + casefold）+ 固定 seed 20260929 分桶，同一 query_id 的多行整组落同一侧、两侧 query_id 集合不相交；逐行搬运不重新序列化（重跑逐字节一致）；`--check` 只核对既有派生文件、不写盘 |
| `scripts/check_leakage.py` | 新生成查询波的**泄漏护栏**：把待用查询波与 17 个参考集（11 个评测集 + 6 个现役训练层）做归一化文本判交，在任何标注预算花掉之前拦下"训练集里混进评测查询" | 归一化 = NFKC + casefold + 仅保留字母数字（标点/全角/空格差异也算同一条）；撞评测集致命（退出码 1，fail-closed）、撞现役训练语料或波内重复只剔除该行；`--report` 落判交报告、`--filtered` 落过滤后波、`--selftest` 跑已知答案自检（正反例各含）。参考集写成逐字路径而非 glob（否则待检的波会与自身判重），缺失的参考集按 `absent` 如实报告。新数据波层按 `research/ltr_data/synth_train_wave<epoch>_<date>_{vec,xscore}.jsonl` 命名，训练时经 `ltr_promote.py candidate --mix-in` 并入（缺省 `MIX_IN_PATTERNS` 已含 `research/ltr_data/synth_train_wave*_xscore.jsonl`，无需再显式给出） |
| `research/memory_relations/source_control.py` | 研究数据来源对照与无训练双向融合编排；不进入生产 | D5复用既有C5双向概率及统一交换映射，校验原SHA并复现基线，区分概率等变与硬标签平局；C3逐类匹配独立根，单原向监督、同初始tensor和固定共同更新终点，训练与工件只复用nli_reference/finetune，指标只复用metrics。保留实际覆盖、语言、簇与截断限制；曝光终评仅探索，不认证教师质量或自动晋级 |
| `research/memory_relations/semantic_model.py` | 研究训练共享输入、轻量驻留与原缓存字段审计引用；不进入生产 | 默认 `oriented_rows` 保完整API；cache-backed train/budget共用 `training_only=True` 薄投影，保原text/encoding/pair及科学字段，两向共享新摘要，不挂raw_text/fields、不修改原prepared。原gzip/schema存完整审计，最终发布后绑定manifest/payload SHA、身份、源、成员声明顺序与窗口；`truncation_profile` 显式inline/cache_ref，`replay_input_audit(reference, groups, binding=...)` 逐canonical重放。原NR保存加载调用同一SM ledger公式，保原数据/资格/S7/采样/hash-mask-body/预算/选点/Adam检查；缺坏引用不inline掩盖。仅未请求新通道时原inline作唯一兜底；路径/冷热状态不进科学binding。新源码须fresh prepare/cache，不伪旧版本，不保证全池或NN峰值 |
| `research/memory_relations/semantic_artifacts.py` | 训练审计与推理派生的共享工件策略；不改科学源，不自建模型IO引擎 | `export_inference` 以调用者审定brief SHA/run与compact/source字节链为本机信任根，有界顶层header在train前停，mmap原PT selected state；只复用 `NR.save_joint_artifact/load_joint_artifact`，原SM spec仅替换schema与证书validator。正常legacy proof及四键approval保留；run可显式引用`post-save-selected-snapshot-v1`规范记录，绑定原失败/null/成本与独立复验scope/device/差值/counters/core/S7/底层证据，不能冒旧live重演。恢复状态以`named-tensor-data-sha256-v1`逐tensor清单核已加载selected/选中snapshot与Adam，外部aggregate不混作原state_sha256，nativeCPU只标全tensor equal、不冒独立digest；_proof仍四元/原无子命令CLI兼容。新增`publish_runtime_artifact/runtime_spec/load_runtime_artifact(expected_meta_sha256=...)`及`publish-runtime/verify-runtime`，仅V3研究runtime；冻结producer根/commit/策略SHA/原meta pin经独立离线CPU原loader验真，再用原schema闭包桥和同NR引擎比state/编码/双向概率/argmax。`storage_mode=owned`仅保存实际config/tokenizer，FT新增from-config联合构造并strict full-state重载；默认audited_source仍保留旧来源检查，真旧无config才骨干工厂兜底。runtime无continuation/收据正文/绝对来源路径，小origin只留历史digest，包外可信meta pin必填；V2拒绝、不启用产品、不声称质量通过。`semantic-text-control-inference-export-v1` 无continuation、不可续训；记录原updates/selected step/train SHA与完整tensor字节摘要，如实声明复用历史审计、此次未跑原全池guard。`identity/read_metadata/load_inference_artifact` 为fine/public semantic唯一通道；旧v6不fallback，native3及原SM审计入口不变；receipt非ACL，mmap非峰值保证 |
| `research/memory_relations/metrics.py` / `semantic_evaluate.py` | 离线研究共享数学锚点与五叶薄编排；不进入生产 | F1/概率/簇CI只消费原metrics；C5 forward主、dual非法dyad不强选，失败保resolved分母，T=1不变。消费者只经共享semantic_artifacts策略接收推理导出，meta/PT/certificate/policy SHA进入fine锁和public run/resume/score/status。cohort保全部source绑定/lineage/成员约束，允许内部来源所选根为0，仅全集空拒绝；直接单来源仍拒空。根均权、跨source ID/root及跨split簇检查、一次全集summarize，不平均分报告；CPU默认/CUDA显式，不认证操作或风险 |
| `scripts/process_tree.py` | 脚本子进程树的启动与回收单一通道 | `run`；Windows Job 启动闸 / POSIX 进程组；超时、取消和退出回收本次子树 |
| `scripts/quality_gate.py` | 校验清单、解析工具、清空密钥并顺序执行门 | `--list` · `--profile fast\|full` · `--dry-run` · `--report-json`；不负责安装依赖 |
| `scripts/validate-agent-governance.ps1` / `tests/test_agent_governance.py` | 跨客户端指令与治理文档检查，真实 PowerShell CLI 边界回归 | `-Root` · `-MaxProjectInstructionBytes`（默认 32768）；唯一安全目录栈复用于指令文件与 `.claude/rules`，同一 scope 锚点先判强制治理边界、再决定剪枝：root `.claude` 与 `.claude/rules` 祖先/整树、已保留活动源码目录中的指令文件叶不因 Git ignore 或依赖目录名漏检；规范路径去重、枚举与内容读取前拒绝链接/junction，活动源码不可读时 fail-closed；仅本仓 Git 忽略产物可剪枝，tracked 路径及祖先不被忽略或依赖目录名跳过，已忽略且无 tracked 后代的 QA 整目录仍不进入；独立非 Git 根保守遍历、不继承父仓忽略规则；保持模板/编码/LF/链接/语法判据与 exit 0/1；静态边界检查不保证检查后目录被替换的竞态 |
| `automation/frozen-queries.json` | 默认冻结评测的输入身份判据 | 主集归一化 SHA-256、ID 集合、总题/评分/NoResult 计数；`evaluate_recommendation.py` 验证，`promote_uploads.py --refresh-frozen-queries --rebaseline-reason` 在受控重基线后显式同步，均不改查询内容 |
| `scripts/build_release.py` | 从 allowlist 构建并复验候选 ZIP | `build --output-dir` · `verify --archive`；归档内 `release-manifest.json` 逐文件 SHA-256，归档旁生成 `.zip.sha256` |
| `scripts/make_delivery.py` / `scripts/make_submission_package.py` | 交付包构建与交付安全复核（提交版含额外 launcher）；`assert_delivery_source` 是交付源 containment 的**唯一实现**（词法 containment → root 到叶子的每一级祖先与叶子均非链接/junction → resolve 后仍在仓库根内；判定异常同样 fail-closed），`collect_delivery_files` / `run_delivery_gate` / `build_zip` / `make_submission_package.build` 四处共用；交付集枚举从 `tracked_paths`（`git ls-files`，git 不可用即 GitUnavailable）出发，**不用 rglob**（父级 junction 会把仓库外字节带进包、叶级检查看不见） | `build_zip` 在创建任何输出前逐源过闸；`make_submission_package --manual` 是用户显式给出的**外部输入**，不套仓库 containment 闸（它不声称来自仓库）；不承诺抵御「检查之后把目录换成链接」的 TOCTOU 竞态——这是归档窗口内的静态核验 |
| `scripts/build_web_context.py` | 从干净 Git 提交生成最小 Web 构建上下文 | `--out <new.tar.gz>`；真实 Dockerfile COPY 源与部署/冒烟脚本，逐成员及归档 SHA-256；复用 build_release 校验；拒绝 dirty 源、秘密、上传和覆盖输出；不执行部署 |
| `scripts/smoke_web_image.py` | 手工发布与托管工作流共用的镜像冒烟 | `--image <local-image> --require-account`；唯一临时容器、loopback 动态端口、无宿主卷/真实凭据；health 与未登录 401；只清理本次 label 所有的容器 |
| `.github/workflows/ci.yml` | 按需手动的 Windows full + Ubuntu fast 配置 | 完整 SHA 固定 action、最小权限、稳定汇总 job `gate`；普通 push/PR 不触发，托管运行不是本地验收或发布的前提 |
| `.github/workflows/release-candidate.yml` | 按需手动执行 full、候选打包、仓库外解包 smoke | 只产出候选 artifact；不创建 GitHub Release、不读取部署凭据、不部署主机 |
| `.github/workflows/web-image.yml` | 按需手动的镜像构建、共享容器冒烟与非阻断漏洞扫描 | 仅 workflow_dispatch；普通 push/PR 不触发 |
| `.github/workflows/deploy-web.yml` | 可选的 TCR/SSH 发布客户端，不是唯一部署入口 | 受限 SSH 账号 pull → tag → 现有 deploy.sh；workflow_run 仅接力按需的 Web image 构建，仓库级 AUTO_DEPLOY 默认关闭；用户授权与无在线用户检查仍须先行 |
| `.github/workflows/installer-build.yml` | 按需手动构建冻结运行时与 unsigned-dev 安装器 | 数字签名未接通，产物一律带 unsigned-dev 标并如实标注；签名合同见 `packaging/signing/README.md` |
| `.github/workflows/public-mirror.yml` | 按需手动构建并校验 private→public 镜像 | 仅 workflow_dispatch；contents 只读权限 + 并发组防重 |

修改清单、runner 或工作流必须同步 `tests/test_quality_gate.py`、`tests/test_ci_contract.py` 与发布相关 contract tests。候选解包目录不得放在仓库内，否则校验器会把它误当成当前仓库的一部分。操作细节见 [自动化质量门与候选发布](docs/AUTOMATION_AND_RELEASE.md)。

---

## 并行矩阵（谁和谁能同时改）

| A \ B | 另一个 `js/` 模块 | 某个 `src/` 后端文件 | 同一个文件 | 端点/契约形状 |
|---|---|---|---|---|
| **能否并行** | ✅ 安全 | ✅ 安全（不改端点契约） | ❌ 串行 | ⚠️ 需握手 |
| **要点** | 各改各的模块 | 前后端解耦，契约是 API | 串行排队 | 提前声明 `API-CHANGE`，两侧同步 + rebase |

> 经验：绝大多数「前端 ∥ 后端」并行都落在前两列——安全。真正要小心的只有最后两列：
> **别两个人同抓一个文件**、**别偷偷改了 API 形状不告诉另一侧**。改前分工打招呼就是防这两件事的。

---

## `/api/recommend` 响应字段 → 前端消费点映射（改字段名/形状前**必须逐个查全**）

> **为什么单列一张表**：改后端响应字段名却漏改前端 JS 消费点，是一类**三门全绿也检测不到**的静默契约打断——
> `web_smoke_test.py` 只对前端 JS 做静态字符串检查、从不执行任何一行 JS。所以端点/响应契约变更时，**光靠跑门不够**，
> 必须照下表检索到每一个消费点、手动改齐（生产者+消费者+测试原子落地）。
> 改完用 `git grep -n "<字段名>" -- web/static/js/`（无 git 时 PowerShell `Select-String -Path web/static/js/*.js -Pattern "<字段名>"`）复核没有漏网。
>
> **机械护栏**：`tests/test_api_contract.py` 用真实响应（FastAPI TestClient）断言每个
> 端点的**必需键存在**（`required ⊆ actual`：删/改字段名立刻红、加字段不误伤），并钉死 Web `/api/introduction`
> 与 MCP `get_dataset_introduction` 的 introduction 键集**逐一相等**（「mcp 和前端同步」的机械证据）。
> 本表是人读版、该测试是机器可核版——**加/改任一响应字段，两处一起改**。它不替代照表手改消费点（测试只
> 保证字段在、不保证前端读对），但把「字段被悄悄删/改名」这类三门测不出的静默打断变成红灯。

| 响应字段 | 消费的前端文件 | 用途 |
|---|---|---|
| `results[]` | `core.js`(历史计数)·`search.js`(renderResults)·`results.js`(renderResults 渲染)·`facets.js`(展示数) | 结果卡片主数据 |
| `results[].{dataset_name,species,tissue,disease,chemistry,platform,assay,sample_size,gene_count,raw_data_status,published_date,source,url,download_url,dataset_uid,n_files,reason,reachability,collection_doi,dataset_doi}` | `cards.js`(buildCard)·`results.js`(`seriesCountsOf` 统计本响应条数后经 `opts.seriesCounts` 传回) | 单卡各字段（改任一字段名 → 卡片对应位静默空白）；`gene_count`（2.1.0）= 检测基因数（10x 平台信息补充旁挂表 `sample_supplement.by_uid.json`，无补充为 ""，有才在卡脚显示「基因数」）；`reachability`= 按下载 host 推断的国内可达性启发 `{host,tier,tier_label,hosting,advice,heuristic}`（非实测速度），渲染卡片徽章，与 item_view/workflow 共用 `reachability.classify` 单一真源；`collection_doi`（3.1.1，值为 "" 表示无）= collection 级编号（同一论文下 core/extended 等条目共用它，**指向论文、不是数据集自己的 DOI**，见 `provenance.collection_doi` 的粒度警告）——同响应里同 doi ≥2 条时那些卡各加一枚「同系列 ×N」小标（`cards.seriesCountsOf` + `cards.seriesBadgeHtml` 是两个唯一构造点），解释「标题相近但为不同数据集」而**不隐藏任何合法条目**；`dataset_doi`（additive）= 数据记录自己的 DOI（Zenodo 等），与 `collection_doi` 粒度不同不可混用，前端当前不直接消费（复用清单/RIS 在服务端），但 item 投影改名同样要查全 |
| `query_constraints[]`（每项含 `filter_id`/`polarity`/`dim`/`label`/`values`） | `facets.js`(renderFacets「已命中」chip；按 `filter_id` 忽略/恢复，include/exclude 同维不连坐)·`board_core.js`/`board.js`(条件板分区) | 命中硬约束投影（含负向 exclude:<dim> / raw:forbidden）。**极性 `prefer` 不是筛选条件**：`prefer:<dim>` / `prefer:raw` / `prefer:source` / `prefer:date` 来自「优先 X」软偏好，只在 `retriever._rank_score` 加权（`PREFERENCE_BOOST`），一条数据都不筛掉。消费端必须与 include/exclude 视觉与文案分开，且**不得计入「正在按 N 个条件筛选」**；四个 id 均已进 `workflow.SUPPRESSIBLE_FILTER_IDS`（可单独停用，且不误删同名硬条件 `raw:*` / `date:*`） |
| `resolution_status` | `results.js`(renderResults 空态分流)·`facets.js` | results / no_match / abstained / clarification_required |
| `clarification`（`{reason,detail,options:[{id,label,rewrite}]}`） | `results.js`(renderResults clarification 空态 + 两选项按钮) | "不需要fastq"歧义澄清（非"没有匹配"） |
| `facets[]` | `facets.js`(renderFacets 可细化维度) | 分面细化 |
| `result_total` | `results.js`(renderResultSummary 方法句里「库中共 N 条匹配」) | 计数提示（并入结果摘要卡的一段自然语言，不再单独渲染 #resultsTotal） |
| `relaxation_options[]`（每项含 `key`/`label`/`kind`/`count`/`suppress`/`results` 预览） | `batch_select.js`(`deriveRelaxCard` 零命中批组卡)·`board.js`(`askRelaxShow` 渲染选择卡 / cbChoose askrelax 作答 → cbCommit `suppressed_constraints` 零 LLM 重搜 / `maybeAutoRelaxCard` 零命中自动弹卡) | 引导式放宽（2026-09-14 收编为单一通道：唯一用户触点 = ask.relax 选择卡；旧救回选择条 rs-strip 与空态卡 chips 已退役）。`kind` = `"drop"`（去掉一个条件，其余都在）/ `"only"`（只按一个条件搜，其余全放开——第一档全军覆没时唯一还救得回来的一档）。**措辞不可混用**：「去掉 X」与「只按 X 搜」是相反的两件事。`suppress` = 整组替换语义的 filter_id 清单（单锚点 `query_parser.relax_key_to_suppressed`，webapp 已合并请求级 suppressed），前端点选后原样下发即可 |
| `degraded_search`（`{ignored_terms,query,count,results,active_filters}` 或 `null`） | `results.js`(`resolution_status="degraded"` 时结果区顶部的「这批结果忽略了 X」横幅) | 确定性降级**建议**，永不在 workflow 内自动应用。需要 LLM 换词救回时只走 Agent `rescue/search.rerun` 套件，由工具内机械择优闸决定是否换屏 |
| `action_markers[]` | `results.js`(renderActionHint → `#actionHint`) | 用户在查询里说出的执行类诉求（打包/下载脚本/导出引文）。这些词此前会让**整句检索弃权**；现在不再阻断检索，但也不静默吞掉——只指路到「下载这批数据」，**不代劳**。该动作真执行成后由 actFinish 调 `clearActionHint` 摘除并置时间线级核销态（换批/分面重跑/历史回看不再复活；新查询时间线经 `resetActionHint` 复位） |
| `markdown` | `results.js`(诊断/原始输出) | 无结果/原始文案 |
| `llm_response_used` · `provider` | `results.js`(来源标签)·`search.js`(缓存决策) | LLM 状态 |
| `interpretation` | `/api/recommend`：`search.js`→`interactions.js`；`/api/interpret`：`interactions.js` 输入防抖预览（来源/时间摘要均采用后端真源） | 原句、送入规则解析的清洗句、实际来源与完整 intent 投影；轻量预览不装载语料、不执行检索排序 |
| `search_trace`（`{version,automatic,summary,strategy_reason,total_duration_ms,steps[]}`；执行型 step 可含 `duration_ms`；**`status="fallback"` 的 step additive 带 `fallback_note`**） | `results.js`（renderResultSummary： 摘要卡的方法句据真实 `steps[].status=used/fallback` 生成；回退附注由 `fallbackLayerNotes` 直接转述 `fallback_note`，覆盖 `local_semantic`/`llm_rerank`/`llm_polish` 三层） | 后端保留完整执行轨迹；客户界面默认只用一句话说明实际启用的排序层与任何回退，硬过滤/规则基础排序/终检等内部例行步骤不展示；无 trace 时如实说「执行明细不可用（请重启后端）」，不含 Key、内部路径或服务商原始错误。**`fallback_note`（1.7.0 起）是「这次回退该怎么对用户说」的单一真源**（生产者 `workflow._fallback_note`）：`未启用：…` = 这一层压根没开（没装本地模型 / 没配 AI 接口），`没能完成：…` = 试过但失败（provider 拒、超时、返回坏格式）。**前端不许自己写死这句措辞**——此前前端把两者一律写成「本次未启用」，provider 真返 400 时，一次故障在界面上读起来像一个选择。老后端没这个字段时前端退到「没能完成」（宁重不轻）；门在 `tests/test_fallback_wording_honesty.py` |
| `ok` · `fallback_reason` | `search.js`(错误/回退判定)·`results.js` | 请求成败 |
| `coverage_caveats`（`[{dim,label,count,by_source:[{source,count}]}]`） | `results.js`(renderCoverageCaveats：渲染进结果摘要卡内的 `#coverageCaveats`——「另有 N 条因缺 <维度> 未核验」+「放宽方式 ▸」展开两档策略 `.cov-strat`：①纳入未标注「X」的 N 条（toggleLenient）②不按「X」筛选（relaxDimFully）；误读版 `examples` 字段已回退) | 诚实降级：满足其它条件但某维未标注（无法核验）的记录计数，本可能相关却被静默判负；改字段名/形状 → 覆盖提示静默失效 |
| `applied_lenient`（被宽容的维度列表） | `results.js`(renderCoverageCaveats「已纳入未标注「X」✕」撤销 chip)·`search.js`(reqBody 回传 `lenient_dims`) | 诚实降级已生效维度回显（前端据此渲染撤销 + 持续宽容） |
| `unused_query_terms`（`[str]`：无对应筛选维度的实义描述词） | `results.js`(renderUnusedQueryTerms：结果上方「以下词未作为筛选维度」只读提示) | 静默丢词诚实层：性别/年龄/受试者/功能类等系统结构上无维度可落的词，原本静默丢弃零信号，现回显；来自 `QueryIntent.unused_query_terms`（`FILLER_DOMAIN`），改字段名/形状 → 提示静默失效 |
| `or_handling`（`{marker,or_dims,or_excluded_dims,and_dims,fit,exact,note_zh}`，句子里没有「或」时为 `{}`） | `results.js`(renderOrHandling → `#orHandling`：结果上方只读提示) | 「或」不再整句弃权，而是按引擎的真实能力执行——**同一维度多值就是「或」**（`passes_hard_filter` 正向「须含任一 target」/ 负向「命中任一 forbidden 即淘汰」）。`fit` 三档必须如实播报：`exact`（就是你说的或）/ `superset`（多个维度各多值＝交叉组合，比说的更宽）/ `narrower`（「或」跨了维度，只能按同时满足，比说的更窄）。来自 `QueryIntent.or_handling`，改字段名/形状 → 语义偏离的播报静默失效 |
| `identifier_lookup`（query 是标识符时 `{is_identifier,kind,value,indexed,match,external_url,message}`，否则 `null`） | `results.js`(renderIdentifierLookup：直达记录卡片 / GEO·SRA fail-closed 指路) | 标识符精确反查：贴 UUID/E-XXXX-N/DOI 直达本目录记录；贴 GEO/SRA 号如实告知不在本目录 11 个来源收录范围、指向原库（不静默返回 0）。来自 `identifiers.lookup`（Web/MCP `lookup_identifier` 共用），改字段名/形状 → 反查提示静默失效 |
| `applied_facets` · `applied_suppressed` · `warnings` · `pipeline` | 前端当前不直接读（回显/调试用） | 加/改不影响现有前端，但仍属契约 |
| `strategy` | `shell.js`（开发者信息）·`results.js`（通过 search_trace 的普通语言理由间接展示） | auto 决策 `{mode,tier,recall_backend,rerank_backend,reason,signals}` |
| `policy_id`（结构体）+ `policy_id_str`（`bpol1:` 稳定紧凑串；组装失败均为 `null`） | `search.js` / `results.js` / `usage_log.js` / `projects.js` / `artifacts.js` 经 `usagePolicyRef()` 统一消费 | 本次检索配置指纹：结构体供诊断，紧凑串供遥测/索引；旧后端对象按 sorted-key JSON 确定性降级；禁止 `String(policy_id)`；`/api/utterance` 的 search 路由同形透传 |
| `search_trace.ranking_snapshot`（`biodata-ranking-snapshot/1`） | `benchfb.js` 在用户授权后随完整响应保存；UI 不展示 | 在 recall/rerank 后记录完整候选 UID 顺序+SHA-256、前 500 条规则特征、真实排序参数/权重与截断标志；门在 `tests/test_search_trace.py` / `tests/test_retriever.py` |
| `experiment`（`null` 或 `{id,arm,propensity}`） | `search.js` 发请求三件套；`usage_log.js` 只记录后端完整回显 | 默认 `null`；两页 `biodata-experiment-arms` 为空即关闭。启用时按匿名 profile 确定分臂并实际覆盖 strategy/rerank/recall；请求的 id/arm/propensity 必须三件齐全、概率 `(0,1]`、标识只许 `[A-Za-z0-9._-]`，否则 422/fail-closed；普通流量绝不冒充 control |

> 其余端点（`/api/files`·`/api/introduction`·`/api/fair` → `cards.js` 文件/介绍/FAIR·DAS 弹窗（`/api/fair` 走 `loadFairInto`→`renderFairInto`，只读元数据、确定性）；`/api/datasets`·`/api/sources` → `browse.js`；`/api/upload` → `browse.js`；POST `/api/diagnose` → `browse.js`）同理：改响应形状前先检索（`git grep` / `Select-String`）消费文件。
>
> 复盘面（`/api/replay/status`·`/points`·`/snapshots`·`/seen`）的响应字段唯一消费者是 `panel/replay.js`：`digest_version` 必须原样带回 `/api/replay/seen`（版本不符时服务端 `stale=true` 零核销，前端刷新后重试一次）；`snapshots` 自含 `items[]`（`kind`/`point`）与 `edges`，前端不得自行反查；改形状前照上表方法检索消费点。
>
> **共享会话与复盘面（2026-10-06，`/api/conversations*`）**：字段唯一消费者是 `act/conversation_client.js`（`listSharedConversations` / `getSharedConversation` / `sharedConversationAction` 与任务续读三件套）与 `panel/board.js`（`cbOpenSharedConversation` / `cbRefreshSharedConversation` / `cbSharedConversation`）、`panel/board_core.js`（`cbSharedRestore` / `cbLegacyImport`）、`search/browse.js`（`openSharedHistory` / `renderHistory` / `initHistWin`）。响应里的 `scope`（`local` / `account`）、`revision`、`project_id`、`pending`、`result`、`requires_client` 是前端状态机的判定输入——改这些名字前必须照上表方法检索 `web/static/js/` 全量消费点；`requires_client=true` 的动作**只在浏览器里执行**，服务端不虚报完成，消费端不得当成已办。
>
> **MCP 高层工具与 Web 的字段同源**：`agent_chat` 返回的 `run` / `result` / `pending` / `conversation_id` / `web_path` 与 `/api/conversations*` 的持久现场是同一份记录（同 `data_root` + 同身份即同 scope）；`agent_replay` 的 strict `{data, provider, use_llm, …}` 信封与 `POST /api/conversations/{id}/replay/{action}` 的 `OwnedReplayRequest` 同一形状（旧 anonymous key 保留）。改任一侧字段名都要两侧同改。

> **环内四工具卡**：`/api/utterance` 响应 `plan.steps[].{card_kind,result}`
> 是四工具结果到前端的**唯一通道**（后端 `agent_exec.py` 成功步把 run() 原始 dict 随 plan.steps
> 下发，零后端改动）。四种 card_kind 的消费点是 `act.js`：`compare` → `actCompareCardHtml`（结论 +
> n同/n异/n未知 计数条 + <details> 字段明细 + caveat_zh）、`cite_export` → `actCiteExportCardHtml`
> （note_zh + 逐文件行（filename/format/tpBytes 字节）+ 「下载」链 `API.citationsDownload` +
> <details> uids）、`compat_find` → `actCompatFindCardHtml`（note_zh + 种子摘要 + 兼容判据 +
> compatible 列表（名称/uid/_compat_basis）+ total + caveat）、`fair_check` → `actFairCheckCardHtml`
> （readiness_pct 大字 + pass/partial/unknown 计数 + <details> 逐项明细 + note_zh 边界句）；
> `degraded=true` 只渲染诚实降级句。HTML 经 `actDispatchPlan` 图内通道（按执行顺序拼接）→
> `actFinish` 的 html 通道上屏 `.cbh-sys-extra`（entry.html 是重画真源）。改这些 result 字段名
> 必须同步 `act.js` 对应构造函数（`tests/test_act_frontend.py` 的 node 真行为门钉结构）。
