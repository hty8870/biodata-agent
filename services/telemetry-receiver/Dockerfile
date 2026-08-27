# BioData Agent 遥测接收端（设计文档 §6：FastAPI + uvicorn）
# SUP-M01：基础镜像钉 amd64 digest（Docker Hub 2026-08-21 查询）；pip 安装走
# --require-hashes（requirements.lock 每行 == 钉版 + SHA-256，构建期校验哈希）。
FROM python:3.12-slim@sha256:876416ecde9aca2bcc90e1fb0c7a9500bbf749f5788b70f82d4c5a5c2357f8b4

WORKDIR /app

COPY requirements.lock ./
# 生产机位于中国大陆；默认使用腾讯镜像避免境外 PyPI 长时间挂起。所有 wheel 仍由
# requirements.lock 的 --require-hashes 强校验，镜像无法替换成未登记内容。
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
RUN pip install --index-url "$PIP_INDEX_URL" --require-hashes --no-cache-dir -r requirements.lock

COPY app.py ./
COPY telemetry_idempotency.py ./
# 保留期脚本：服务 lifespan 已自动执行；该 CLI 保留给宿主机 cron 双保险与 dry-run。
COPY scripts/ ./scripts/

# 非 root 运行
RUN useradd --create-home telemetry
USER telemetry

EXPOSE 8471
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8471"]
