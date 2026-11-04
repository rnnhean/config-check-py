"""对照组：复刻「改造前」的写法——async 端点里直接调用同步 httpx.Client。

这不是线上代码，只给 `scripts/bench_concurrency.py` 当反面对照：
FastAPI 的 `async def` 端点运行在事件循环线程上，里面一旦 `await` 之前做同步阻塞
IO（`httpx.Client.post`、`time.sleep`、CPU 重活），整个进程同一时刻只能处理一个请求，
后到的请求连读请求体都排不上。

为了使三组的工作量可比，这里同样每个请求串行发 5 次模型调用
（与 `draft_fixes` 4 条 + `compile` 1 条一致），指向同一个假模型端点。
"""

from __future__ import annotations

import os
import time

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

LLM_CALLS_PER_AUDIT = 5

app = FastAPI(title="config-check-bench-blocking")


def _call_sync(base_url: str, api_key: str) -> None:
    """同步阻塞调用，与改造前 app/llm.py 的写法一致。"""
    with httpx.Client(timeout=120.0) as client:
        for _ in range(LLM_CALLS_PER_AUDIT):
            client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": os.environ.get("MODEL_NAME", "bench-model"),
                    "messages": [{"role": "user", "content": "bench"}],
                },
            )


@app.post("/api/audits")
async def create_audit() -> JSONResponse:
    # 反面写法：async 端点里同步阻塞，事件循环被独占
    started = time.perf_counter()
    _call_sync(
        os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:1/v1").rstrip("/"),
        os.environ.get("OPENAI_API_KEY", ""),
    )
    return JSONResponse(
        {"status": "done", "durationMs": round((time.perf_counter() - started) * 1000, 2)},
        status_code=201,
    )


@app.get("/api/health")
async def health() -> dict[str, object]:
    return {"ok": True, "llmMode": "bench-blocking", "llmAsync": False}

