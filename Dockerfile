# syntax=docker/dockerfile:1
# 三段构建：前端产物 → Python 依赖 → 运行镜像。
# 改造前为单阶段镜像（296MB，root 运行）。改用 --target 而不是 venv，
# 避免在 runtime 里重复带一份 pip/setuptools。
FROM node:22-slim AS frontend

ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    CI=1

# corepack 自带 pnpm；显式激活与本地一致的版本，避免不同构建机上拉到不同 major 版本
RUN corepack enable && corepack prepare pnpm@8.7.1 --activate

WORKDIR /build/frontend
# 先只拷依赖清单，命中 Docker 层缓存；源码改动不会触发重装依赖
COPY frontend/package.json frontend/pnpm-lock.yaml ./
# pnpm store 用 cache mount 跨构建复用：这台机器到 npm registry 只有 ~20 KiB/s，
# 不复用的话每构建一次都要重新下 ~200 MB 依赖。
RUN --mount=type=cache,id=pnpm-store,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile

COPY frontend/ ./
# 构建产物直接写到 /build/web（Vite 的 base 是 /static/，由后端 StaticFiles 托管）
RUN pnpm exec vite build --outDir /build/web --emptyOutDir


FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --target /opt/deps -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/deps \
    LLM_MODE=mock \
    LLM_ASYNC=1 \
    MAX_CONCURRENT_LLM=4 \
    PORT=5203

WORKDIR /app

# 固定 uid/gid，便于挂卷时对齐权限；非 root 运行。
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/deps /opt/deps

# 只拷运行所需：应用代码、前端构建产物与产品数据。审计结果写到 /app/data/audits。
COPY --chown=app:app app/ ./app/
# 前端资源来自 frontend 阶段，不再依赖仓库里已构建的 web/ 目录
COPY --from=frontend --chown=app:app /build/web/ ./web/
COPY --chown=app:app data/products.json ./data/products.json

RUN mkdir -p /app/data/audits && chown -R app:app /app/data
VOLUME ["/app/data/audits"]

USER app

EXPOSE 5203

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5203/api/health', timeout=3).status == 200 else 1)"]

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5203"]
