# BioData Agent 网页版部署手册（通用模板）

> 范围：把本仓库以**版本化 Docker 镜像**部署到一台 Linux 服务器（下文用 `<server-ip>` 指代），
> 对外提供 HTTP 服务；回退 = 换 tag 重新部署，分钟级。域名 + HTTPS + CI/CD 自动化不在本模板范围。
> 本文为模板：`<server-ip>`、`<deploy-user>`、`<deploy-dir>`、`<data-dir>`、`<ssh-key>` 等占位符
> 按实际环境替换（例如 `ubuntu@<server-ip>`、`~/.ssh/<your-key>.pem`）。

## 1. 形态一览

| 项 | 值 |
|---|---|
| 公网入口 | `http://<server-ip>/`（防火墙放行所选端口；生产建议置于 TLS 反向代理之后） |
| 容器 | `biodata-web`（单容器、单进程单 worker、非 root uid 1000） |
| 端口 | 宿主 `80` → 容器 `8510`（run_web.py 读 `PORT=8510`、`BIODATA_WEB_HOST=0.0.0.0`，按需覆盖） |
| 镜像 | `biodata-web:<WEB_API_VERSION>-g<短sha>`，如 `biodata-web:2.7.0-g<短sha>`；建议每机保留最近 5 个 |
| 语料/前端 | `src/ web/ database/`（tracked 快照）**烤进镜像**——与代码同版本，回退天然一致 |
| 运行态数据 | 宿主卷 `<data-dir>` → 容器 `/data`（`BIODATA_DATA_ROOT=/data`，portable 双根分离）：账号 `.userdata/`、上传 `database/external/`、trace、日志、run；回退不动数据 |
| 密钥 | `<deploy-dir>/.env`（`chmod 600`，内容为 `.env.zhipu` 型密钥 + `IMAGE_TAG` + `BIODATA_TRUSTED_HOSTS` 两行部署参数） |
| 资源 | `mem_limit` 按服务器预算设置（示例见 `docker-compose.web.yml`） |

服务器路径边界（部署相关改动以此为止）：

- `<deploy-dir>/`——部署物：`build/<tag>/`（构建上下文）、`.env`、`docker-compose.web.yml`、`deploy.sh`、`RELEASES.log`
- `<data-dir>/`——应用运行态数据（备份策略由部署方自定，建议对 `<data-dir>` 做定期备份）

## 2. 代码同步到服务器

原则：**一个 tag 一份完整构建上下文，禁止手工散拷单文件**。上下文 = 工作树的 git tracked
快照（`.dockerignore` 在构建时进一步瘦身）。

```bash
TAG=2.7.0-g<短sha>          # 短sha = 当前 commit 的 git rev-parse --short HEAD
ssh -i ~/.ssh/<ssh-key> <deploy-user>@<server-ip> "sudo mkdir -p <deploy-dir>/build && sudo chown <deploy-user> <deploy-dir> <deploy-dir>/build"
git archive --format=tar.gz -o /tmp/biodata-ctx-$TAG.tar.gz HEAD
scp -i ~/.ssh/<ssh-key> /tmp/biodata-ctx-$TAG.tar.gz <deploy-user>@<server-ip>:/tmp/
ssh -i ~/.ssh/<ssh-key> <deploy-user>@<server-ip> "mkdir -p <deploy-dir>/build/$TAG && tar -xzf /tmp/biodata-ctx-$TAG.tar.gz -C <deploy-dir>/build/$TAG && rm /tmp/biodata-ctx-$TAG.tar.gz"
```

注意：git archive 只含 tracked 内容——`database/trace/`、`database/external/upload_*.json`
等 gitignored 运行产物天然不进上下文（镜像也不需要它们）。

## 3. 生成依赖锁定文件（改依赖时才需要重跑）

`requirements/requirements.txt` 只有下界约束；镜像必须按**精确锁定**安装。网页版镜像额外装
「AI 执行」扩展链 `langgraph` + `langchain-openai`（版本与 requirements/requirements-ci.lock 对齐），
本机形态不受影响（扩展在本地仍可选装）。在线 MCP 形态再装 `mcp`（webapp 顶层挂载
`/mcp`，版本与 requirements/requirements-ci.lock 的 `mcp==1.28.1` 对齐）。锁定文件在目标服务器上用与镜像完全相同的基础环境生成：

```bash
# $CTX = <deploy-dir>/build/$TAG（构建上下文已按 §2 同步）
ssh -i ~/.ssh/<ssh-key> <deploy-user>@<server-ip> "
  sudo docker run --rm -v $CTX:/ctx -w /ctx python:3.12-slim bash -lc \
    'pip install --no-cache-dir -q -r requirements/requirements.txt \
       langgraph==<ci锁版本> langchain-openai==<ci锁版本> mcp==<ci锁版本> && pip freeze' \
    | sudo tee $CTX/deploy/web/requirements-web.lock >/dev/null
"
scp -i ~/.ssh/<ssh-key> <deploy-user>@<server-ip>:<deploy-dir>/build/$TAG/deploy/web/requirements-web.lock deploy/web/requirements-web.lock
```

回填后 `deploy/web/requirements-web.lock` 随分支 commit（Dockerfile COPY 它安装）。
若曾生成过锁定文件且依赖未变，直接复用 build 目录里的上一份即可。

## 4. 首次部署（one-time）

```bash
# 1) 服务器目录与数据卷（部署目录属主 = <deploy-user>；数据卷属主 = 容器内 uid 1000）
ssh -i ~/.ssh/<ssh-key> <deploy-user>@<server-ip> "
  sudo mkdir -p <deploy-dir> <data-dir>
  sudo chown <deploy-user>:<deploy-user> <deploy-dir>
  sudo chown 1000:1000 <data-dir>
  sudo chmod 700 <data-dir>
"

# 2) .env：密钥从本机上传（绝不打印内容），再加两行部署参数
scp -i ~/.ssh/<ssh-key> "<本机仓库根>/.env.zhipu" <deploy-user>@<server-ip>:/tmp/biodata-web.env.upload
ssh -i ~/.ssh/<ssh-key> <deploy-user>@<server-ip> "
  sudo install -m 600 -o root -g root /tmp/biodata-web.env.upload <deploy-dir>/.env
  rm /tmp/biodata-web.env.upload
  printf 'BIODATA_TRUSTED_HOSTS=<server-ip>\nIMAGE_TAG=\n' | sudo tee -a <deploy-dir>/.env >/dev/null
"

# 3) compose 与部署脚本就位（来自构建上下文）
ssh -i ~/.ssh/<ssh-key> <deploy-user>@<server-ip> "
  cp <deploy-dir>/build/$TAG/deploy/web/docker-compose.web.yml <deploy-dir>/
  cp <deploy-dir>/build/$TAG/deploy/web/deploy.sh <deploy-dir>/deploy.sh
  chmod +x <deploy-dir>/deploy.sh
"

# 4) 构建镜像（上下文 = build/<tag> 目录；tag 规则 <WEB_API_VERSION>-g<短sha>）
ssh -i ~/.ssh/<ssh-key> <deploy-user>@<server-ip> "
  sudo docker build -f <deploy-dir>/build/$TAG/deploy/web/Dockerfile \
    -t biodata-web:$TAG <deploy-dir>/build/$TAG
"

# 5) 部署
ssh -i ~/.ssh/<ssh-key> <deploy-user>@<server-ip> "sudo <deploy-dir>/deploy.sh $TAG"
```

> compose 与 deploy.sh 需与实际部署目录一致：`deploy.sh` 顶部的 `BASE` 默认 `/opt/biodata-web`，
> 可用环境变量 `BIODATA_DEPLOY_DIR` 覆盖；compose 的数据卷宿主目录默认 `/data/biodata-web`，
> 可用 `BIODATA_DATA_DIR` 覆盖；compose 的 `env_file` 使用相对路径 `.env`，要求 compose
> 文件与 `.env` 位于同一目录（即 `<deploy-dir>`）下启动。

`BIODATA_TRUSTED_HOSTS` 是 webapp Host 守卫的显式白名单（本机形态默认仅 loopback）；
上域名时把该行改成逗号分隔追加域名即可（重启生效）。

## 5. 日常发布与回退

发布新版 = §2 同步 → §3（依赖变了才跑）→ §4-4 构建 → `sudo <deploy-dir>/deploy.sh <new-tag>`。
deploy.sh 自动：写 `IMAGE_TAG` → `up -d` → 等 healthy（≤120s）→ 失败自动切回上一 tag →
成功记 `<deploy-dir>/RELEASES.log` 并清理镜像只留最近 5 个。

**手工回退**（不依赖 deploy.sh 的自动路径时）：

```bash
ssh -i ~/.ssh/<ssh-key> <deploy-user>@<server-ip> "sudo <deploy-dir>/deploy.sh <old-tag>"
# 或最原始形态：改 <deploy-dir>/.env 的 IMAGE_TAG=<old-tag> 后
#   sudo docker compose --env-file <deploy-dir>/.env -f <deploy-dir>/docker-compose.web.yml -p biodata-web up -d
```

## 6. 故障排查入口

```bash
sudo docker ps                                   # 容器状态（应 Up ... (healthy)）
sudo docker inspect --format '{{.State.Health.Status}} {{.State.Health.FailingStreak}}' biodata-web
sudo docker logs --tail 100 biodata-web          # 应用日志
sudo docker exec -it biodata-web bash            # 进容器（slim，无 curl；用 python urllib）
curl -s http://localhost/api/health              # 服务器本机验证（Host: localhost→127.0.0.1 守卫放行）
curl -s -H 'Host: <server-ip>' http://<server-ip>/api/health   # 模拟公网 Host（白名单放行路径）
tail <deploy-dir>/RELEASES.log                   # 部署/回退流水
```

常见症状：

- **403 仅接受本机 loopback Host**：`.env` 的 `BIODATA_TRUSTED_HOSTS` 缺失或没包含访问用的
  Host；改后 `sudo docker compose ... up -d` 重建容器（env_file 变化需重建）。
- **healthy 起不来**：`docker logs` 看是否依赖缺失（锁定文件与镜像不符）或 8510 未监听
  （`BIODATA_WEB_HOST`/`PORT` 被覆盖）。
- **注册/上传报错且日志含 Permission denied**：宿主 `<data-dir>` 属主不是 1000:1000。

## 7. 公网护栏（可选，全部 additive）

公网形态的三道闸：**登录强制 + 注册邀请 + LLM 日配额**，全部 additive——缺省（不设任何
下列变量）时与本机单机形态逐字节一致，本机安装包用户零感知。只拦 `/api/` 前缀，静态前端
与登录页天然可达；未登录调非白名单 `/api/*` 返回 401 `{"ok":false,"error":"auth_required"}`，
前端据此自动锁定整页只留登录框。

### 7.1 环境变量（改 .env 后需 `up -d` 重建容器生效）

| 变量 | 缺省 | 语义 |
|---|---|---|
| `BIODATA_REQUIRE_ACCOUNT` | 关 | `1` 开启登录强制门（白名单：health/register/login/logout/whoami） |
| `BIODATA_INVITE_CODE` | 空 | 护栏模式下注册必填；**未配置 = 注册整体关闭**（宁可关死不留缝） |
| `BIODATA_LLM_DAILY_PER_USER` | 100 | 正式通道每账号每日 LLM 轮数（0=不限） |
| `BIODATA_LLM_DAILY_GLOBAL` | 1000 | 正式通道全站每日熔断（0=不限） |
| `BIODATA_LLM_QUOTA_EXEMPT` | 空 | 豁免用户名（逗号/空白分隔） |
| `BIODATA_TRIAL_API_KEY` | 空 | 「限量试用」通道专用 key；**未设时回落 `BIODATA_EMBED_API_KEY`**（试用与 embedding 召回共用同一把 key，部署侧只维护一份；两变量都只在进程环境，请求级覆盖链从不注入） |
| `BIODATA_TRIAL_DAILY_PER_USER` | 30 | 试用通道每账号每日轮数（独立桶，更紧） |
| `BIODATA_TRIAL_DAILY_GLOBAL` | 500 | 试用通道全站每日熔断 |
| `BIODATA_TRIAL_BASE_URL` / `BIODATA_TRIAL_MODEL` | 官方默认 | 试用通道端点/模型覆盖 |
| `BIODATA_TRIAL_THINKING` | 空 | 试用通道思考档逃逸口（`enabled`/`disabled`）。**缺省不发 thinking 参数**（部分模型始终思考、拒收 disabled） |
| `BIODATA_LLM_QUOTA_FILE` | userdata 层 `llm_quota.json` | 配额账本路径（一般不动） |

### 7.2 计数口径（哪一发请求会烧服务端 LLM）

埋闸端点全集：`/api/recommend`（润色/AI 重排/动作审核意图为真时）、`/api/utterance`、
`/api/action·plan`、`/api/agent·search-rescue`、`/api/act·summary`、`/api/dream`。
**不计**：BYOK（请求自带 key）、mock、LLM 未启用、服务端无 key——这些烧不到服务端配额。
超限返回 429 中文文案；按 UTC 日重置。账本只留当日、故障放行（可用性优先，provider 侧
消费上限是最后防线）。

试用通道（前端「限量试用」预设）：端点/模型锁定服务端托管值，请求级 key/地址/模型
**一律忽略**（防注入防偷换）；凭据只认 `BIODATA_TRIAL_API_KEY`（未设回落
`BIODATA_EMBED_API_KEY`），绝不回落 `LLM_API_KEY`。

### 7.3 日常运营动作

```bash
# 生成邀请码（私下发给用户；泄漏了换一个新的重启即可）
python -c "import secrets; print(secrets.token_urlsafe(8))"

# 改限额/豁免/邀请码：编辑 .env 对应行后重建
sudo docker compose --env-file <deploy-dir>/.env -f <deploy-dir>/docker-compose.web.yml -p biodata-web up -d

# 查看当日用量账本（容器内 /data/.userdata/llm_quota.json，只留当日）
sudo docker exec biodata-web cat /data/.userdata/llm_quota.json
```
