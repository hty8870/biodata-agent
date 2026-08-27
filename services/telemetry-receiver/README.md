# BioData Agent 遥测接收端（services/telemetry-receiver）

接收前端脱敏后上传的 usage / benchfb 使用数据（契约真源：
`docs/工作记录/设计_遥测上传与单版本化_2026-08-20.md` §2 上传协议、§6 接收端）。
栈：FastAPI + uvicorn + PostgreSQL 16，docker compose 两容器（receiver + db）。

安全修复批 S2/tc1（2026-08-21）起：schema 收紧（`extra="forbid"` / app 白名单 / 明细元素
形状与条数上限）、日配额（全局字节 + 每 profile 包数）、90 天保留清理、有界双层限流器、
packet/event 幂等、同步 DB 线程池卸载、HTTPS fail-closed 配置，
生产依赖 hash 锁（`requirements.lock` + `--require-hashes`）与基础镜像 digest 钉版。

## 端点

| 端点 | 行为 |
|---|---|
| `POST /v1/ingest` | Header token 校验；JSON body ≤2MiB（兼容已发布 1.9MB 客户端）；严格 schema；IP 300/min 粗防洪 + profile 30/min；全局/IP/profile 日配额；packet/event 幂等；**落库前净化**（ov1-fix1b，见下节）；**feedback 解密**（eng-b3，见下节）；DB 工作在线程池执行；200 的 `server_hint.max_body_bytes` 与 413 的 `detail.max_body_bytes` 供新客户端自动缩包；`accepted_feedback` 为 eng-b3 additive 计数 |
| `GET /v1/stats` | 使用独立的服务端 `X-Stats-Token`；轻量统计：`packets_total` / `events_total`（usage+benchfb+mcp+feedback 事件去重总数）/ `last_24h_packets` / `db_size_bytes`（PG 用 `pg_database_size`，SQLite 文件用文件大小）/ `oldest_packet_at` / `retention`；不扫 payload 大字段（ov1-bench1 批新增，eng-b3 扩 feedback） |
| `GET /healthz` | `{"ok": true}`；含 DB 连通检查（连不上 → 503） |

CORS：默认只允许 loopback Web UI；公网 HTTPS UI 通过 `ALLOWED_ORIGINS`（逗号分隔）或
`ALLOWED_ORIGIN_REGEX` 显式配置，禁止 `*`。

### Schema 收紧（SEC-C02 修 4，S2 起）

- 顶层 `extra="forbid"`：收到未知字段 → 422；兼容旧客户端 6 字段和 tc1 新增
  `packet_id/client_id/profile_id` 的 9 字段包。
- `app` 子对象白名单：只收 `cache_generation` / `ua` / `lang`，其余 → 422。
- `usage_events` ≤1500 条、`benchfb_records` ≤60 条、`mcp_records` ≤200 条（ov1-bench1 批新增，
  元素须为 dict 且带字符串 `call_id`，与 usage/benchfb 同风格的条数/形状约束）、
  `feedback_records` ≤20 条（eng-b3 新增，**严格 pydantic 模型** `FeedbackRecord`，见下节），
  超出 →422。
- 明细元素必须为对象；键数 ≤ 100、键名 ≤ 64 字符、嵌套深度 ≤ 20、单字符串 ≤ 2MiB（与 body 上限同值，body 层先拦，此处为显式单字段守卫）。

### 意见反馈（eng-b3 起）

`/v1/ingest` 顶层新增可选键 `feedback_records[]`，每条为严格模型
`{feedback_id, identity, ephemeral_pubkey, nonce, ciphertext, with_diag}`
（`extra="forbid"`，未知字段/超限 → 422）。自由文本**不明文传输**：客户端用内置开发者公钥
做 WebCrypto ECDH(P-256)+HKDF-SHA256+AES-256-GCM（协议参数与 `feedback_core.js` 两端同源），
服务端用环境变量 `FEEDBACK_DECRYPT_KEY` 里的 P-256 私钥解密（支持 PEM 或 base64 DER；
结果进程内缓存）：

- **未配置私钥**而请求带 `feedback_records` → 422 `"feedback decrypt key not configured"`；
  无 `feedback_records` 的 usage/benchfb/mcp 路径行为完全不变（fail-open 于旧行为）。
- **解密失败/明文损坏** → 422 明确错误，包整体不落库、明文不落日志、不回显。
- 解密后明文（`{feedback_id, authorized_at, text, diag?}`）**落库前过值级遮蔽**（含 eng-b3
  追加的 API Key 形态，见下节）再入 `payload.feedback_records`（遮蔽后形态，`with_diag` 透传）。
- **幂等**：`event_key = sha256(identity|"feedback"|feedback_id)`，identity 取记录内 `identity`
  字段（客户端按 profile/install 标识语义填充，空值兜底包级 `identity_of`）——同一
  feedback_id 重传只入库首次。
- 响应 additive 计数 `accepted_feedback`；埋点 `feedback_sent{with_diag}` 由入库侧计数。

### 落库前净化（ov1-fix1b 起，防御纵深）

客户端上传前已做结构性脱敏（`usage_core.js telemetryStrip`）；本层在服务端**落库前**
（schema 校验后、幂等 claim 前）对 `usage_events` / `benchfb_records` / `mcp_records`
递归重放同一套规则，兜旧版本客户端、打点漂移与直接调 API：

- **键级剔除**：`api_key/password/passwd/username/accountusername/account_name/account_id/
  token/secret/authorization/cookie/email`（大小写不敏感）整键删除；
- **`base_url` 值**只留 host（保留端口，非法/空 → 空串；路径/查询串整段不采）；
- **值级遮蔽**：自由文本里的手机号/证件号/邮箱按带边界正则替换为 `[手机号]`/`[证件号]`/`[邮箱]`；
  eng-b3 起**追加 API Key 形态遮蔽**（`sk-…`/`AKIA…`/Bearer token/`ghp_…` → `[API Key]`，
  追加在既有规则**之后**，usage 既有遮蔽语义不动）——feedback 明文（服务端第二层）与
  usage/benchfb/mcp 自由文本兜底（客户端第一层）共用。

纪律：只数处数——响应 additive 新键 `sanitized`（重复包路径恒 0）与服务端日志都只带
计数，**绝不回带被处理的原始值**。净化发生在 `legacy_packet_id` 摘要之前，重试同包净化
结果确定，幂等不受影响。

### 自适应上传阈值（ov1-adapt1 起）

200 响应带 additive 字段 `server_hint: {pressure, batch_threshold, min_interval_ms}`，
前端据此动态调整上传批量与节奏（服务器空闲时几乎实时、压力大时攒批保护接收端）：

- **压力** = max(在途请求/16, 限流窗口内请求/RATE_LIMIT_MAX, 今日已收字节/DAILY_BYTES_BUDGET)，
  钳到 [0,1]；分母取不到时该信号按 0。
- **离散档映射**：pressure < 0.3 → `{2, 30s}`；0.3–0.7 → `{5, 120s}`；≥ 0.7 → `{20, 300s}`。
- 老客户端不读该字段无影响（additive）；429 时前端临时升到高档（20 条/5 分钟）直到下一次 200 hint 覆盖。

### 事件幂等键口径（ov1-fix1b 修订）

`event_key = sha256(identity|kind|event_id)`：usage/benchfb 的 identity 为匿名账户
（`identity_of`：profile → client → install 兜底）；**mcp 的 identity 为 `install_id`**
（整机口径——同一安装切换匿名账户重传同一 `call_id` 不再重复入库）；**feedback 的
identity 为记录内 `identity` 字段**（eng-b3，客户端按 profile/install 标识语义填充）。

### 配额与保留（SEC-C02 修 5，S2 起）

- 全局每日入库字节上限 100MB；每来源 IP 每日 20MB（库中只保存按日 HMAC 桶，不保存原始 IP）；每随机 `profile_id` 每日包数上限 500。任一超限 → 429，整个事务不落库。
- 字节配额通过 `ingest_daily_usage` 原子累加，热路径不再扫描/转换历史 JSON；`ingest_packets.raw_bytes` 记录每个新包的真实 HTTP body 字节数。
- DB 和导出目录统一保留 90 天：服务每次启动立即清理一次，随后默认每 24 小时执行；最近一次结果持久化并由 `/v1/stats.retention` 显示。宿主 cron 仅作双保险：
  ```bash
  # 手动（receiver 容器内）：
  sudo docker compose -f /opt/biodata-telemetry/docker-compose.yml exec -T receiver \
      python scripts/telemetry_retention.py --days 90 --dry-run   # 先 dry-run 看数量
  # 服务器双保险 cron（每日 03:30，避开 03:00 数据备份窗口）：
  sudo crontab -e   # 追加：
  # 30 3 * * * cd /opt/biodata-telemetry && sudo docker compose exec -T receiver python scripts/telemetry_retention.py --days 90 --quiet >> /var/log/biodata-telemetry-retention.log 2>&1
  ```
- 以上配额/保留参数可用 env 覆盖：`DAILY_BYTES_BUDGET`、`PER_IP_DAILY_BYTES`、`PER_INSTALL_DAILY_PACKETS`、`RETENTION_DAYS`、`RETENTION_INTERVAL_SECONDS`、`EXPORT_DIR`（部署冒烟时可用小预算验证 429 路径）。

## 表结构（设计文档 §6）

```sql
ingest_packets(
  id bigserial PRIMARY KEY,
  received_at timestamptz NOT NULL DEFAULT now(),
  install_id text NOT NULL,
  schema text NOT NULL,
  ua text,
  cache_generation text,
  n_usage int NOT NULL DEFAULT 0,
  n_benchfb int NOT NULL DEFAULT 0,
  raw_bytes bigint NOT NULL DEFAULT 0,
  payload jsonb NOT NULL            -- 原样存，查询/物化留给后续分析
);
CREATE INDEX ix_ingest_packets_install_id ON ingest_packets (install_id);
CREATE INDEX ix_ingest_packets_received_at ON ingest_packets (received_at);

ingest_packet_receipts(packet_id PRIMARY KEY, received_at, identity, row_id);
-- indexes: (identity, received_at), (row_id)
ingest_event_receipts(event_key PRIMARY KEY, received_at, packet_id, kind);
-- indexes: (kind, received_at), (received_at), (packet_id)
ingest_daily_usage(day_utc, scope, bucket, raw_bytes, packet_count, PRIMARY KEY(day_utc, scope, bucket));
telemetry_service_state(key PRIMARY KEY, value, updated_at);
```

receipt 与主包同事务：重复 packet 直接返回原 ACK；重叠 batch 只把首次 event 写入主包。

## 配置（环境变量，无默认值、不硬编码秘密）

- `INGEST_TOKEN`：客户端会携带的滥用过滤凭据，不是秘密，不能证明发送者身份；由 HTML meta 注入。
- `STATS_TOKEN`：只存服务端的独立管理 token，专用于 `/v1/stats`；绝不得注入或打包到客户端。
- `DATABASE_URL`：生产 `postgresql+psycopg2://telemetry:<pw>@db:5432/biodata_telemetry`；测试用 `sqlite:///:memory:`。
- `FEEDBACK_DECRYPT_KEY`（eng-b3）：意见反馈解密 P-256 私钥（PEM 或 base64 DER），与客户端内置公钥配对。不配置时 `feedback_records` 返回 422 明确错误，usage/benchfb/mcp 路径不受影响。
- 可选覆盖：`BODY_READ_TIMEOUT`、`DAILY_BYTES_BUDGET`、`PER_IP_DAILY_BYTES`、`PER_INSTALL_DAILY_PACKETS`、
  `RETENTION_DAYS`、`RETENTION_INTERVAL_SECONDS`、`EXPORT_DIR`、`RATE_LIMIT_MAX`、`PROFILE_RATE_LIMIT_MAX`、`RATE_LIMIT_WINDOW`、
  `ALLOWED_ORIGINS`、`ALLOWED_ORIGIN_REGEX`、`DB_POOL_SIZE`、`DB_MAX_OVERFLOW`、
  `DB_POOL_TIMEOUT`、`UVICORN_WORKERS`。默认 1 worker、5+5 个连接；只有外置共享限流并核算
  `workers*(pool+overflow) <= PostgreSQL 可用连接数` 后才可提高 worker 数。

## 本地开发与测试（Windows）

```powershell
$Python = .venv\Scripts\python.exe
& $Python -m pip install fastapi uvicorn httpx pytest sqlalchemy psycopg2-binary
$env:INGEST_TOKEN = 'dev-token'
$env:STATS_TOKEN = 'dev-stats-token-different-from-ingest'
$env:DATABASE_URL = 'sqlite:///:memory:'
& $Python -m pytest tests\test_telemetry_receiver.py -q
```

## 生产依赖锁定（SUP-M01，S2 起）

- `requirements.txt`：顶层依赖钉版（可读约束）；不带 `uvicorn[standard]` extras（生产容器无需 reload/watchfiles/uvloop，缩小依赖面）。
- `requirements.lock`：顶层依赖全量解析树 + `==` 钉版 + SHA-256（Linux manylinux2014_x86_64 / CPython 3.12）。Dockerfile 以 `pip install --require-hashes -r requirements.lock` 安装，构建期校验哈希。
- 更新流程：改顶层 `requirements.txt` → 按需求重新解析生成锁（pip 跨平台下载 + wheel SHA-256，参照仓库 `requirements-ci.lock` 生成模式）→ 全量替换 `requirements.lock` → 重新部署验证。
- 基础镜像钉 digest：`python:3.12-slim@sha256:876416ec…f8b4`（Dockerfile）、`postgres:16@sha256:56f243d2…a30bc`（compose），均为 Docker Hub 2026-08-21 查询的 amd64 digest。

## 机上部署（<server-ip>，协调者执行）

1. 准备目录与数据卷（postgres 镜像以 uid 999 运行，宿主机目录须归它）：
   ```bash
   sudo mkdir -p /opt/biodata-telemetry /data/biodata-telemetry/pgdata
   sudo chown 999:999 /data/biodata-telemetry/pgdata
   ```
2. 拷入 `app.py`、`telemetry_idempotency.py`、依赖锁、Docker/Compose 与 `scripts/` 到部署目录，并创建 `.env`：
   ```bash
   sudo cp .env.example /opt/biodata-telemetry/.env
   sudo chmod 600 /opt/biodata-telemetry/.env && sudo chown root:root /opt/biodata-telemetry/.env
   # 编辑 .env 填入 INGEST_TOKEN / STATS_TOKEN / DB_PASSWORD（两个 token 必须不同）
   ```
3. 端口暴露：
   - **风险已知的明文直连（本项目生产现状，tc1 合并裁决、用户知情授权）**：接收端历史即公网
     `http://<server-ip>:8471`（无域名无备案，明文 HTTP 是用户知情接受的设计），因此当前
     Compose 明确使用 `8471:8471`，并保持 `sudo ufw allow 8471/tcp`。**同时**前端两页 meta
     必须显式登记该主机：`biodata-telemetry-endpoint=http://<server-ip>:8471/v1/ingest`、
     `biodata-telemetry-allow-insecure=<server-ip>`（白名单仅该主机，空值即拒绝明文公网 HTTP）。
   - **未来迁移目标**：取得域名/TLS 后，将 Compose 收窄到 `127.0.0.1:8471:8471`，由
     Nginx/Caddy 等反向代理只公开 443；移除前端明文例外并关闭 ufw 的公网 8471。届时代理必须
     生成可信客户端 IP，receiver 仍不得信任公网客户端自报的转发头。
4. 启动并验证：
   ```bash
   cd /opt/biodata-telemetry && sudo docker compose up -d --build
   curl -s http://127.0.0.1:8471/healthz        # 期望 {"ok":true}
   curl -s -X POST http://127.0.0.1:8471/v1/ingest -H 'X-Ingest-Token: <token>' \
     -d '{"schema":"biodata-telemetry/1","install_id":"smoke","usage_events":[]}'  # 期望 {"ok":true,"id":1}
   ```
5. 改动服务端代码/配置后更新机上文档 `/opt/OPS_NOTES.md`（根 AGENTS.md 纪律）。

## 导出与分析（ov1-bench1 批）

落库数据由仓库 `scripts/` 下的只读消费管线变成推荐评测、训练和分析原料；数据库入口
（均接受 `--dsn` = PG 连接串或 SQLite 文件路径，缺省读 env `BIODATA_TELEMETRY_DSN`）：

- **`scripts/telemetry_export.py`**：主导出管线。join receipts 去重 → schema/版本校验
  （`biodata-telemetry/1`，缺字段/旧 pending 数据容错）→ 隐私扫描（手机号/身份证/邮箱
  正则遮蔽；证件号或整体即手机号 → `quarantine.jsonl` 不进正常产物）→ 产物：
  `impressions.jsonl` / `interactions.jsonl` / `turns.jsonl` / `explicit_labels.jsonl` /
  `mcp_calls.jsonl` / `benchmark_candidates.jsonl`（字段兼容 `scripts/benchfb_ingest.py`
  的 candidates 并新增 `tid/iid/policy`）/ `quality_report.md`（关联完整率、可标注率、
  重复率、上传延迟分布、schema 漂移、事件分布、mcp 统计）/ `review.html`（人工审阅页，
  可按 kind/rating 过滤）。`--accepted ids.txt` 只把名单内 id 写进
  `benchmark_candidates.final.jsonl`；另产出有界 `agent_trajectories.jsonl`（已有脱敏
  route/action/search trace 的逐步轨迹，含 prompt/实验/model/outcome/training consent，绝不采
  chain-of-thought）；`--incremental` 幂等由产物行键 merge 保证
  （ov1-fix1b 起全量扫描 + 按行键归并；watermark 状态文件仅信息性）。
- **`scripts/telemetry_delete.py`**：按 `--install-id` 删除（默认 dry-run 打印将删计数，
  `--yes` 才真删；事务级联删主包 + 两张 receipts，防幂等账本留孤儿）。**删除范围仅限
  数据库三张表**（ov1-fix1b 显式化）：每日导出产物（`--out` 目录，如
  `/data/biodata-telemetry/export`）、`quarantine.jsonl`、`review.html` 与 `/backup`
  备份**不在其列**——导出产物重跑导出覆盖或按需删除，备份随 7 天保留期自动过期。
- **`GET /v1/stats`**：只接受独立的服务端 `STATS_TOKEN`；轻量统计与最近保留清理结果（见上表）。

用法与 cron 示例（服务器上经 receiver 容器执行，`--dsn` 用 compose 内网地址；注意
`docker compose exec` 不经过 shell，`$DATABASE_URL` 必须包一层 `sh -c` 才能在容器内展开）：

```bash
# 手动全量导出到 /data/biodata-telemetry/export（协调者/分析者执行）
cd /opt/biodata-telemetry && sudo docker compose exec -T receiver \
  sh -c 'python /opt/biodata-telemetry/scripts/telemetry_export.py \
  --dsn "$DATABASE_URL" --out /data/biodata-telemetry/export'

# 每日增量导出（03:45，避开 03:00 备份与 03:30 保留清理窗口；已装机，见 sudo crontab -l）：
# 45 3 * * * cd /opt/biodata-telemetry && sudo docker compose exec -T receiver \
#   sh -c 'python /opt/biodata-telemetry/scripts/telemetry_export.py \
#   --dsn "$DATABASE_URL" --out /data/biodata-telemetry/export --incremental' \
#   >> /var/log/biodata-telemetry-export.log 2>&1

# 按安装码删除（误传/投诉处置；先 dry-run 看计数再 --yes）
sudo docker compose exec -T receiver sh -c 'python /opt/biodata-telemetry/scripts/telemetry_delete.py \
  --dsn "$DATABASE_URL" --install-id xxxx'        # dry-run
sudo docker compose exec -T receiver sh -c 'python /opt/biodata-telemetry/scripts/telemetry_delete.py \
  --dsn "$DATABASE_URL" --install-id xxxx --yes'  # 真删

# 统计（独立管理 token，不得使用客户端 ingest token）
curl -s http://127.0.0.1:8471/v1/stats -H "X-Stats-Token: <server-only-token>"
```

产物字段字典（前端打点与协调者对齐用）见 `scripts/telemetry_export.py` 模块 docstring 与
`tests/test_telemetry_export.py` 的逐字段断言。

### 冻结 benchmark、Parquet 与交错评估

训练/评测数据必须先冻结，不能直接把每日滚动导出当测试集：

```bash
python scripts/build_telemetry_benchmark.py \
  --input /data/biodata-telemetry/export/benchmark_candidates.jsonl \
  --out-root /data/biodata-telemetry/frozen --purpose training

# 分析依赖与应用/receiver 隔离；锁文件覆盖 Windows CPython 3.12 和 Linux x86_64 wheel。
python -m pip install --require-hashes -r requirements-analytics.lock
python scripts/telemetry_parquet.py \
  --input /data/biodata-telemetry/export/interactions.jsonl \
  --input /data/biodata-telemetry/export/benchmark_candidates.jsonl \
  --out /data/biodata-telemetry/parquet/run-20260825

# 输入每行：query_id、control_uids、candidate_uids。
python scripts/ranking_interleave.py interleave --input paired-rankings.jsonl \
  --output assignments.jsonl --seed rank-e1
# 在 assignment 行追加 clicked_uids 后离线归因；重复/未知点击不会虚增任一臂。
python scripts/ranking_interleave.py credit --input assignments-with-clicks.jsonl \
  --output click-credit.jsonl
```

三条管线都拒绝覆盖已有输出，manifest 都记录输入/输出哈希。冻结器还按用户、语义簇和
时间桶分组切分，避免同一用户或同义查询泄漏到 train/test 两侧；只有与用途匹配的显式
授权记录才进入冻结数据。

### PostgreSQL 并发验收

`scripts/telemetry_pg_load.py` 用真实 HTTP 入口对 PostgreSQL 接收链运行有界 10/50/100 并发波次，
默认每档 100 个独立、非重复的 v2 包，并输出吞吐、HTTP 状态以及 p50/p95/p99 延迟；凭据只从参数或
`BIODATA_INGEST_TOKEN` 读取，永不写入报告。压测身份使用独立 profile，避免 profile 配额掩盖
数据库能力；IP/全局限流仍保持真实生效。生产运行前先确认当前一分钟没有其他压测：

```bash
BIODATA_INGEST_TOKEN='<ingest token>' python scripts/telemetry_pg_load.py \
  --endpoint http://127.0.0.1:8471/v1/ingest --levels 10,50,100 \
  --requests-per-level 100 --out /tmp/telemetry-load.json
```

## 备份

数据落在 `/data/biodata-telemetry/pgdata`，随服务器既有每日 03:00 备份机制自动进 `/backup`（留 7 天）。
每次涉及 schema 的部署前，必须先用 `pg_dump -Fc` 生成一份可恢复的逻辑备份并校验文件非空。

## 运维

- 查看日志：`sudo docker compose -f /opt/biodata-telemetry/docker-compose.yml logs -f --tail=100 receiver`
- 重启：`sudo docker compose -f /opt/biodata-telemetry/docker-compose.yml restart receiver`
- 查看数据（SQL 示例，进入 db 容器）：
  ```bash
  sudo docker exec -it biodata-telemetry-db psql -U telemetry -d biodata_telemetry
  ```
  ```sql
  -- 最近 100 条上传（不含明细）
  SELECT id, received_at, install_id, schema, ua, cache_generation, n_usage, n_benchfb
  FROM ingest_packets ORDER BY id DESC LIMIT 100;

  -- 某 install 的全部上传明细
  SELECT id, received_at, n_usage, n_benchfb, payload
  FROM ingest_packets WHERE install_id = 'xxxx' ORDER BY id DESC;

  -- 按天汇总
  SELECT received_at::date AS day, count(*) AS packets,
         sum(n_usage) AS usage_events, sum(n_benchfb) AS benchfb
  FROM ingest_packets GROUP BY 1 ORDER BY 1 DESC;

  -- 按 schema 版本计数
  SELECT schema, count(*) FROM ingest_packets GROUP BY 1;
  ```

## 已知边界

- 限流仍是进程内有界滑动窗口；横向扩容需在反向代理/Redis 外置共享限流。
- 配额是滥用缓解，不是强身份认证；攻击面由 2MiB 单包、IP/profile 配额和全局配额共同兜底。若未来在信任的反向代理后横向扩容，必须由代理生成可信客户端 IP，并将限流/配额状态外置；本版不信任公网客户端自报的转发头。
- `install_id` 仅保留旧报表兼容；幂等/归因使用 128-bit `client_id`、每账户随机
  `profile_id`、`packet_id` 与 event/record id，均不含账户名或账户 id。
- 上传内容已由前端结构性脱敏（api_key 整键删除、端点只留主机名、不记密码/账户名）；
  ov1-fix1b 起服务端落库前再递归重放同套规则（见上节「落库前净化」）。
- 导出管线 ov1-fix1b 起做**跨包精确键 join**（impressions/interactions/labels 均以
  install+事件 id 归并，`--incremental` 幂等由产物行键 merge 保证）；口径与产物字段见
  `scripts/telemetry_export.py` 模块 docstring 与 `tests/test_telemetry_export.py`。
