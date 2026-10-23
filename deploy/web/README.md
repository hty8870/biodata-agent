# BioData Agent 网页版部署手册（2026-08-25）

> 范围：把仓库以**版本化 Docker 镜像**部署到 Internet-facing 主机。应用端口只发布到
> 宿主 loopback，公网入口必须先经过 TLS 反向代理；无域名/证书时不得承载真实账户、查询、
> 会话或 BYOK 凭据。回退 = 换 tag 重新部署，分钟级。
>
> 日常发布不依赖 GitHub Actions、TCR 或其计费状态：本地验收后生成最小构建包，服务器
> 构建、冒烟，再调用现有 `deploy.sh` 切换版本。托管工作流仅为可选客户端。
> 公网使用 HTTPS；HTTP 仅用于宿主 loopback 诊断。

## 1. 形态一览

| 项 | 值 |
|---|---|
| 公网入口 | `https://<your-domain>/`（80 只做 HTTPS 重定向） |
| 容器 | `biodata-web`（单容器、单进程单 worker、非 root uid 1000） |
| 端口 | 宿主 `127.0.0.1:8510` → 容器 `8510`；Nginx/Caddy 在 443 终止 TLS |
| 镜像 | `biodata-web:<WEB_API_VERSION>-g<短sha>`，如 `biodata-web:2.6.0-gae5156f`；本机保留最近 5 个 |
| 语料/前端 | `src/ web/ database/`（tracked 快照）**烤进镜像**——与代码同版本，回退天然一致 |
| 运行态数据 | 宿主卷 `/data/biodata-web` → 容器 `/data`（`BIODATA_DATA_ROOT=/data`，portable 双根分离）：账号 `.userdata/`、上传 `database/external/`、trace、日志、run；回退不动数据 |
| 密钥 | `/opt/biodata-web/.env`（root:600；以 `deploy/web/.env.example` 为结构合同） |
| 内存 | `mem_limit: 1536m`（服务器预算 ≤1.5G；2C4G 同机还有遥测栈） |
| 遥测 | 独立端点；新部署必须使用 HTTPS，client ingest token 仅作滥用过滤、不作身份认证 |

服务器路径边界（本任务的一切改动以此为止）：

- `/opt/biodata-web/`——部署物：`.env`、compose、root-owned `deploy.sh`、`RELEASES.log`
- `/data/biodata-web/`——应用运行态数据（宿主每日 03:00 自动备份 `/data`，见 §6）
- 受控运维系统——保存批准、digest、备份、回退和健康证据；不把真实记录回填 Git

## 2. 从干净提交生成构建上下文

一个 tag 对应一份不可变构建包，不再上传整个私仓或手工散拷源码。工具从 Git blob 读取
Dockerfile 的真实 COPY 源，额外带部署配置与共享冒烟脚本；拒绝未提交改动、上传、秘密、
研究模型和已有输出路径。运行报告放在已忽略的 `artifacts/`，不改变源码快照。

在本地干净提交上执行（`$Python` 是已解析的项目解释器，输出名每次换新）：

```powershell
& $Python scripts/quality_gate.py --profile full --report-json artifacts/quality-web-release.json
if ($LASTEXITCODE -ne 0) { throw 'full quality gate failed' }
& $Python scripts/build_web_context.py --out artifacts/web-context.tar.gz
if ($LASTEXITCODE -ne 0) { throw 'context build failed' }
$m = Get-Content -LiteralPath artifacts/web-context.tar.gz.manifest.json -Raw | ConvertFrom-Json
$m | Select-Object commit, version, image_tag
Get-FileHash -Algorithm SHA256 -LiteralPath artifacts/web-context.tar.gz
scp -i ~/.ssh/<ssh-key> artifacts/web-context.tar.gz artifacts/web-context.tar.gz.manifest.json <admin-user>@<server-host>:/tmp/
if ($LASTEXITCODE -ne 0) { throw 'context transfer failed' }
```

服务器上把 `TAG` 和 `EXPECTED_SHA` 设为本地 manifest 中的 `image_tag` 和 `archive.sha256`，
先比对传输哈希，再解到**新目录**。管理员需先为构建用户准备 `/opt/biodata-web/build`：

```bash
set -euo pipefail
TAG='<manifest-image-tag>'
EXPECTED_SHA='<manifest-archive-sha256>'
[[ "$TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]
printf '%s  %s\n' "$EXPECTED_SHA" /tmp/web-context.tar.gz | sha256sum --check -
CTX="/opt/biodata-web/build/$TAG"
mkdir "$CTX"
tar -xzf /tmp/web-context.tar.gz -C "$CTX"
sudo docker build --label "org.opencontainers.image.revision=<manifest-commit>" \
  -f "$CTX/deploy/web/Dockerfile" -t "biodata-web:$TAG" "$CTX"
```

manifest 含每个成员的路径、长度和 SHA-256；归档 SHA 通过后还可逐成员核对。构建目录不含
`.env` 或运行态数据，不能代替服务器独立管理的密钥与数据卷。

## 3. 依赖锁定（改依赖时才更新）

日常发布直接消费已提交的 `deploy/web/requirements-web.lock`，不现场改锁。
需要更新时，在与 Dockerfile 相同的 Linux/Python 基础环境、独立工作目录里解析并验证依赖，
回填源码锁后重新提交、跑完整质量门、重新生成构建包。不要改已经过 SHA 验证的上下文。

## 4. 首次部署（one-time）

```bash
# 1) 服务器目录与数据卷（部署目录由 root 管；数据卷属主 = 容器 uid 1000）
ssh -i ~/.ssh/<ssh-key> <admin-user>@<server-host> "
  sudo mkdir -p /opt/biodata-web /data/biodata-web
  sudo chown root:root /opt/biodata-web
  sudo chown 1000:1000 /data/biodata-web
  sudo chmod 700 /data/biodata-web
"

# 2) 按 deploy/web/.env.example 另建 server-only .env；必须包含账户门、Secure cookie、
#    域名、邀请码和正整数配额。真实文件绝不从 Git 生成、绝不打印。
scp -i ~/.ssh/<ssh-key> /secure/local/biodata-web.env <admin-user>@<server-host>:/tmp/biodata-web.env.upload
ssh -i ~/.ssh/<ssh-key> <admin-user>@<server-host> "
  sudo install -m 600 -o root -g root /tmp/biodata-web.env.upload /opt/biodata-web/.env
  rm -f /tmp/biodata-web.env.upload
"

# 3) root-owned compose / deploy.sh 就位
ssh -i ~/.ssh/<ssh-key> <admin-user>@<server-host> "
  sudo install -m 644 -o root -g root /opt/biodata-web/build/$TAG/deploy/web/docker-compose.web.yml /opt/biodata-web/docker-compose.web.yml
  sudo install -m 755 -o root -g root /opt/biodata-web/build/$TAG/deploy/web/deploy.sh /opt/biodata-web/deploy.sh
"

# 4) 安装 TLS 反向代理模板并验证 nginx 配置/证书，再构建镜像
ssh -i ~/.ssh/<ssh-key> <admin-user>@<server-host> "
  sudo docker build -f /opt/biodata-web/build/$TAG/deploy/web/Dockerfile \
    -t biodata-web:$TAG /opt/biodata-web/build/$TAG
  sudo nginx -t
"

# 5) 部署：日常远程账号经 sudoers 白名单直接调 deploy.sh（镜像已在 step 4 本机构建）
ssh -i ~/.ssh/<deploy-key> deploy@<server-host> "sudo -n /opt/biodata-web/deploy.sh '$TAG'"
```

`BIODATA_TRUSTED_HOSTS` 填浏览器实际访问域名；compose 固定开启账户门与 Secure cookie，
并只发布宿主 loopback。`PUBLIC_HEALTH_URL` 用同一 HTTPS 入口的 `/api/health`。

## 5. 日常发布与回退

主路径：干净提交的本地 `full` → §2 构建包与传输哈希 → 服务器 Docker 构建 → 共享冒烟 →
确认无在线用户和运行中任务 → 调用现有 `deploy.sh`。不需要 TCR、GitHub Actions 或托管额度。

服务器构建后先做隔离验证（沿用 §2 的 `CTX`、`TAG`）：

```bash
sudo python3 "$CTX/scripts/smoke_web_image.py" --image "biodata-web:$TAG" --require-account
```

脚本使用唯一临时容器、loopback 动态端口、不挂宿主数据卷、不读 `.env`；同时验证健康、
`account.required=true` 和未登录访问受保护 API 返回 401，只清理本次带匹配 label 的容器。
若服务器已有 `deploy.sh` 与本次提交不同，先备份原脚本，再安装构建包中的同源版本：

```bash
sudo cp -p /opt/biodata-web/deploy.sh "/opt/biodata-web/deploy.sh.before-$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -m 755 -o root -g root "$CTX/deploy/web/deploy.sh" /opt/biodata-web/deploy.sh
```

实际切换仍须事先得到用户部署授权，且刚刚核实没有在线访问或运行中任务；有活动则延期。
部署账号的 sudoers 白名单保持不变；管理员也调用同一脚本，不另造部署逻辑：

```bash
sudo /opt/biodata-web/deploy.sh "$TAG"
curl --fail --proto '=https' --tlsv1.2 https://<your-domain>/api/health
```

`deploy.sh` 校验 tag、更新 `IMAGE_TAG`、`up -d`、等待 healthy（≤120s）、断言账户护栏；
失败自动切回上一 tag，成功记 `RELEASES.log` 并保留最近 5 个镜像。上线后还需核验公网
HTTPS、未登录 401、实际 image ID 与源提交，不能只凭进程启动判定成功。数据卷不随换 tag 回退。

**手工回退**（同一脚本，无需 GitHub）：

```bash
sudo /opt/biodata-web/deploy.sh '<old-tag>'
```

`.github/workflows/deploy-web.yml` 保留为可选 TCR/SSH 客户端；仓库级 `AUTO_DEPLOY=false`。
它不会替代用户授权、活动检查或本地完整验收。公仓合并的本地 `local-quality` 回执规则见
[自动化质量门与候选发布](../../docs/AUTOMATION_AND_RELEASE.md)。

## 6. 备份核对

将 `/data/biodata-web` 纳入加密备份并定期做恢复演练。备份脚本、目标位置、保留期和
最后一次恢复证据属于运维记录，不写入公开仓库；仓库只保留这一条可验证要求。

## 7. 故障排查入口

```bash
sudo docker ps                                   # 容器状态（应 Up ... (healthy)）
sudo docker inspect --format '{{.State.Health.Status}} {{.State.Health.FailingStreak}}' biodata-web
sudo docker logs --tail 100 biodata-web          # 应用日志（json-file 10m×3 轮转）
sudo docker exec -it biodata-web bash            # 进容器（slim，无 curl；用 python urllib）
curl -s http://127.0.0.1:8510/api/health          # 宿主 loopback，验证容器映射
curl --fail --proto '=https' https://<your-domain>/api/health     # 公网只能走 TLS
tail /opt/biodata-web/RELEASES.log               # 部署/回退流水
```

常见症状：

- **403 仅接受本机 loopback Host**：`.env` 的 `BIODATA_TRUSTED_HOSTS` 缺失或没包含访问用的
  Host；改后 `sudo docker compose ... up -d` 重建容器（env_file 变化需重建）。
- **healthy 起不来**：`docker logs` 看是否依赖缺失（锁定文件与镜像不符）或 8510 未监听
  （`BIODATA_WEB_HOST`/`PORT` 被覆盖）。
- **注册/上传报错且日志含 Permission denied**：宿主 `/data/biodata-web` 属主不是 1000:1000。

## 8. 验收证据

每次部署至少保存：镜像 digest、批准人、部署时间、TLS 检查、`account.required=true`、
备份/回退可用性和外部 HTTPS health 结果。真实主机、账号、指纹和输出放受控运维系统，
不写入仓库。

## 9. 账号护栏（T3，2026-08-25）

公网形态的三道闸：**登录强制 + 注册邀请 + LLM 日配额**。源码/本地启动器缺省仍保持
单机兼容；`docker-compose.web.yml` 则是专用 production profile，固定开启账户门与 Secure cookie，
缺少域名、邀请码或正整数配额时启动器 fail-closed 拒绝起服。只拦 `/api/` 前缀，静态前端
与登录页天然可达；未登录调非白名单 `/api/*` 返回 401 `{"ok":false,"error":"auth_required"}`，
前端据此自动锁定整页只留登录框。

### 9.1 环境变量（改 .env 后需 `up -d` 重建容器生效）

| 变量 | production profile | 语义 |
|---|---|---|
| `BIODATA_REQUIRE_ACCOUNT` | compose 固定 `1` | 登录强制门（白名单：health/register/login/logout/whoami） |
| `BIODATA_COOKIE_SECURE` | compose 固定 `1` | session cookie 只经 HTTPS 发送 |
| `BIODATA_INVITE_CODE` | 必填 | 注册邀请码；缺失时启动失败 |
| `BIODATA_LLM_DAILY_PER_USER` | 正整数必填 | 正式通道每账号每日 LLM 轮数 |
| `BIODATA_LLM_DAILY_GLOBAL` | 正整数必填 | 正式通道全站每日熔断 |
| `BIODATA_TRUSTED_HOSTS` | 域名必填 | Host/Origin 同源守卫；不要填任意通配 |
| `FORWARDED_ALLOW_IPS` | compose 固定 `*` | 仅因宿主端口绑定 loopback，才允许 TLS 代理转发 scheme/host |
| `BIODATA_LLM_QUOTA_EXEMPT` | 空 | 豁免用户名（逗号/空白分隔，产品所有者用） |
| `BIODATA_TRIAL_API_KEY` | 空 | 「限量试用」通道专用 key（DeepSeek，低成本；与正式 key 完全隔离；只在进程环境，请求级覆盖链从不注入） |
| `BIODATA_TRIAL_DAILY_PER_USER` | 30 | 试用通道每账号每日轮数（独立桶，更紧） |
| `BIODATA_TRIAL_DAILY_GLOBAL` | 500 | 试用通道全站每日熔断 |
| `BIODATA_TRIAL_BASE_URL` / `BIODATA_TRIAL_MODEL` | 官方默认 | 试用通道端点/模型覆盖（默认 `https://api.deepseek.com/v1` / `deepseek-v4-flash`，2026-09-01 起） |
| `BIODATA_TRIAL_THINKING` | 空 | 试用通道思考档逃逸口（`enabled`/`disabled`/`none`）。**缺省 = disabled**：deepseek-v4-flash 默认思考档拒收 tool_choice="required" 且更慢（2026-08-25 实测）；`none` = 不发 thinking 参数（换始终思考的模型时用——glm-5.3-flash 拒收 disabled，2026-08-27 实测 400） |
| `BIODATA_LLM_QUOTA_FILE` | userdata 层 `llm_quota.json` | 配额账本路径（一般不动） |

### 9.2 计数口径（哪一发请求会烧服务端 LLM）

埋闸端点全集：`/api/recommend`（润色/AI 重排/动作审核意图为真时）、`/api/utterance`、
`/api/action·plan`、`/api/agent·search-rescue`、`/api/act·summary`、`/api/dream`。
**不计**：BYOK（请求自带 key）、mock、LLM 未启用、服务端无 key——这些烧不到服务端配额。
超限返回 429 中文文案；按 UTC 日重置（北京时间每日 08:00 归零）。账本只留当日、故障
放行（可用性优先，provider 侧消费上限是最后防线）。

试用通道（前端「限量试用」预设）：端点/模型锁定服务端托管值，请求级 key/地址/模型
**一律忽略**（防注入防偷换）；凭据只认 `BIODATA_TRIAL_API_KEY`，绝不回落 `LLM_API_KEY`
（也不回落 `BIODATA_EMBED_API_KEY`——智谱 key 对 DeepSeek 端点无效，2026-08-27 换型
GLM 时曾短暂共用，2026-09-01 改回时移除）；默认 `deepseek-v4-flash`（2026-09-01 起）
恒关思考档——其默认思考档拒收 tool_choice="required" 且更慢（2026-08-25 实测）；
换始终思考的模型时用 `BIODATA_TRIAL_THINKING=none`（不发该参数）。

### 9.3 日常运营动作

```bash
# 生成邀请码（私下发给用户；泄漏了换一个新的重启即可）
python -c "import secrets; print(secrets.token_urlsafe(8))"

# 改限额/豁免/邀请码：编辑 .env 对应行后重建
sudo docker compose --env-file /opt/biodata-web/.env -f /opt/biodata-web/docker-compose.web.yml -p biodata-web up -d

# 查看当日用量账本（容器内 /data/.userdata/llm_quota.json，只留当日）
sudo docker exec biodata-web cat /data/.userdata/llm_quota.json
```

## 10. 可选演示：Redis 会话 + RQ 消息队列（2026-09-08）

默认部署形态**不需要**本节——未设开关变量时，会话走 JSON 文件后端、corpus-sync 走进程内
线程 job，行为与历史逐字节一致。本节仅是学习/演示用的可选增强，**未接入 deploy.sh、
不进入生产发布流程**；投产（多实例、Redis 备份与公网口径）须另行评审决策。

两个独立开关（语义真源在代码注释：`redis_sessions.py` / `rq_jobs.py` / `redis_store.py`）：

- `BIODATA_SESSION_STORE=redis`：登录会话改存 Redis，多进程/多实例共享登录态；
- `BIODATA_JOB_BACKEND=rq`：corpus-sync 长任务改投 RQ 队列，由独立 worker 进程执行。
  worker 保存稳定代际；各 Web 进程在普通 API 入口按各自游标调用既有语料/向量失效，
  无需发起页面继续轮询任务状态。

前置条件：运行环境装有可选依赖（`pip install -r requirements/requirements-mq.txt`；
默认发布镜像不含，演示需自行扩展镜像或本机源码形态）。compose 叠加示例见
`deploy/web/docker-compose.mq.example.yml`（含 redis 服务与 worker 服务，文件头有完整
用法与回退说明）。

### MQ 格式与恢复边界

- Queue、Worker 与恢复查询的 Job.fetch 均使用 `rq.serializers.JSONSerializer`。
  不接受旧 pickle 作业，也没有 pickle 回退。JSON 不等于执行沙箱：Redis 与队列写权限
  仍必须限定于可信服务，不能让不可信客户端修改任务函数或参数。
- 从旧格式切换前，停止 Web 入队与旧 worker，核对在途任务、已完成写入及实际回执并备份。
  再选择新的 Redis 数据库或全新演示数据卷启用新版本；旧队列不自动迁移、重新入队或执行。
  不得把可能已完成的写任务直接重发。旧数据的清理须在确认没有在途任务后单独决定。
  若会话也换到新数据库，既有登录态需要重新登录。
- 入队连接错误可能发生在事务已提交之后。接口会如实返回确认失败，单飞占位仍保留；
  只有确认 job 缺失且宽限期结束才按中断回收，不因回包丢失直接允许另一轮写入。
- RQ 补偿结果初始设置 `result_ttl=-1`，不按默认500秒过期。worker 已确认业务终态写入时，
  会在 RQ 正常结束时释放不再需要的补偿记录。终态写失败则持久保留，后续核对成功且
  RQ 已 finished 后才安全回收；不能确认时继续保留并告警。
  应监控演示 Redis 的容量与持久化，不可用时不会自动切换到线程后端。
- MQ Web 通知只覆盖该同步任务。其他进程的上传/回滚/管护及 CLI/MCP 缓存的统一版本检测
  仍须在语料生产者/读取点复用既有代际机制，不应把此通知当成全站缓存一致性保证。
