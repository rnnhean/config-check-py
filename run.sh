#!/usr/bin/env bash
# 本地启动脚本：默认 mock 模式，端口 5203。
# 用法: ./run.sh            (mock)
#       LLM_MODE=real OPENAI_API_KEY=... ./run.sh
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PORT:-5203}"
HOST="${HOST:-127.0.0.1}"

if [ ! -x ".venv/Scripts/python.exe" ] && [ ! -x ".venv/bin/python" ]; then
  echo "未找到 .venv，请先执行:"
  echo "  uv venv --python C:/Users/v-renhan/AppData/Local/Programs/Python/Python312/python.exe .venv"
  echo "  uv pip install --python .venv/Scripts/python.exe -r requirements.txt"
  exit 1
fi

PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY=".venv/bin/python"

export LLM_MODE="${LLM_MODE:-mock}"
exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT"
