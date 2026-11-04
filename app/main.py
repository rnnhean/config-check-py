"""FastAPI 应用：审计 API + 极简 HTML 报告页 + 结构化指标。

关键约定：
- 审计走异步路径（`LLM_ASYNC=1`，默认）：LangGraph 节点是 async 的，
  LLM 调用用 `httpx.AsyncClient`，单次 10 秒级模型调用不再阻塞事件循环。
- `LLM_ASYNC=0` 关闭异步：运行时换回同步实现（节点内用线程池执行），
  行为与改造前一致，用于回退与对照压测。
- 每个请求带 trace_id，异常路径也写结构化日志。
- 不读取 .env；配置全部来自进程环境变量。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import report_html
from .concurrency import AsyncConcurrencyLimiter, QueueWaitTimeout
from .llm import (
    AsyncRuntimeFactory,
    async_enabled,
    build_runtime,
)
from .observability import METRICS, configure_logging, log_event, new_trace_id
from .pipeline import AuditResult, run_audit, run_audit_async
from .store import AuditStore

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_PATH = BASE_DIR / "data" / "products.json"
AUDIT_DIR = BASE_DIR / "data" / "audits"
WEB_DIR = BASE_DIR / "web"


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def create_app(
    data_path: Path | None = None,
    store: AuditStore | None = None,
    runtime_factory: Any = None,
    async_runtime_factory: Any = None,
) -> FastAPI:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    resolved_data = Path(data_path or os.environ.get("PRODUCTS_PATH") or DEFAULT_DATA_PATH)
    audit_store = store or AuditStore(AUDIT_DIR)

    use_async = async_enabled()
    # 两个工厂都存在时按 LLM_ASYNC 选择；测试里可以只传其中一个强制模式
    if runtime_factory is None and async_runtime_factory is None:
        if use_async:
            factory: Any = AsyncRuntimeFactory()
        else:
            factory = build_runtime
    elif async_runtime_factory is not None:
        factory = async_runtime_factory
        use_async = True
    else:
        factory = runtime_factory
        use_async = False

    max_concurrent = _int_env("MAX_CONCURRENT_LLM", 4)
    queue_timeout = _float_env("LLM_QUEUE_TIMEOUT_SECONDS", 10.0)
    limiter = AsyncConcurrencyLimiter(max_concurrent, queue_timeout=queue_timeout)

    # LLM 运行时按次新建（stats / 预算按一次审计统计）；
    # 真实模型下 provider 与连接池由 AsyncRuntimeFactory 复用，生命周期跟应用一致。

    def get_runtime() -> Any:
        return factory()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            aclose = getattr(factory, "aclose", None)
            if aclose is not None:
                await aclose()

    app = FastAPI(title="config-check", version="1.1.0", lifespan=lifespan)

    # antd 的单个 chunk 压缩前约 1 MB，gzip 后 ~330 KB。Starlette 自带中间件，不用手写压缩。
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # 前端静态资源：web/ 是 `pnpm build`（frontend/）的产物，直接由 FastAPI 托管。
    # 无条件挂载 + check_dir=False：首次前端构建之前目录可能不存在，
    # 若按目录存在与否决定是否挂载，会在 import 时求值，导致构建后必须重启进程才生效。
    app.mount("/static", StaticFiles(directory=str(WEB_DIR), check_dir=False), name="static")

    # 编译一次图复用；状态每次调用独立，LangGraph 的 Invoke 不共享 state
    graph_holder: dict[str, Any] = {}
    graph_lock = threading.Lock()

    def get_graph(runtime: Any) -> Any:
        with graph_lock:
            if "graph" not in graph_holder:
                from .pipeline import build_graph

                graph_holder["graph"] = build_graph(resolved_data, runtime)
            return graph_holder["graph"]

    async def execute(trace_id: str | None = None) -> AuditResult:
        METRICS.record_audit_start()
        ok = False
        try:
            runtime = get_runtime()
            if use_async:
                result = await run_audit_async(
                    resolved_data, runtime, get_graph(runtime), trace_id
                )
            else:
                # 同步 fallback：丢线程池，端点本身仍是 async，不阻塞事件循环
                result = await asyncio.to_thread(
                    run_audit, resolved_data, runtime, get_graph(runtime), trace_id
                )
            # 落盘是同步文件 IO，丢线程池，避免大结果写盘时卡事件循环
            await asyncio.to_thread(audit_store.save, result)
            ok = result.status == "done"
            return result
        finally:
            METRICS.record_audit_end(failed=not ok)

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next: Any) -> Any:
        trace_id = request.headers.get("x-trace-id") or new_trace_id()
        request.state.trace_id = trace_id
        started = time.perf_counter()
        # 200ms 的轮询类接口（metrics / health）在压测时无意义地刷日志，静默掉
        quiet = request.url.path in ("/api/metrics", "/api/health")
        try:
            response = await call_next(request)
        except Exception:
            log_event(
                "http_error",
                traceId=trace_id,
                method=request.method,
                path=request.url.path,
                durationMs=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        response.headers["x-trace-id"] = trace_id
        if not quiet:
            log_event(
                "http_request",
                traceId=trace_id,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                durationMs=round((time.perf_counter() - started) * 1000, 2),
            )
        return response

    @app.get("/api/health", response_class=JSONResponse)
    async def health() -> dict[str, Any]:
        try:
            from . import rules

            dataset = rules.load_dataset(resolved_data)
            data_ok = True
            total = dataset.total
        except Exception:  # noqa: BLE001 - 健康检查要返回状态而不是抛错
            data_ok = False
            total = 0
        runtime = get_runtime()
        return {
            "ok": data_ok,
            "version": "1.1.0",
            "llmMode": "mock" if runtime.provider.name == "mock" else "real",
            "llmAsync": use_async,
            "model": runtime.model,
            "maxConcurrentLlm": max_concurrent,
            "dataPath": str(resolved_data),
            "products": total,
            "audits": audit_store.count(),
        }

    @app.get("/api/metrics", response_class=JSONResponse)
    async def metrics() -> dict[str, Any]:
        return METRICS.snapshot()

    @app.post("/api/audits")
    async def create_audit(request: Request) -> JSONResponse:
        # 并发保护：LLM 槽位满时排队，排队超时返回 429 + Retry-After（不直接拒绝）
        trace_id = getattr(request.state, "trace_id", None)
        try:
            async with limiter:
                result = await execute(trace_id)
        except QueueWaitTimeout:
            return JSONResponse(
                {
                    "error": "llm queue timeout",
                    "inflight": limiter.inflight,
                    "queueDepth": limiter.queue_depth,
                },
                status_code=429,
                headers={"Retry-After": str(limiter.retry_after_seconds)},
            )
        status = 201 if result.status == "done" else 500
        return JSONResponse(result.to_dict(), status_code=status)

    @app.get("/api/audits", response_class=JSONResponse)
    async def list_audits(limit: int = 50) -> dict[str, Any]:
        return {"audits": audit_store.list(limit=max(1, min(limit, 500)))}

    @app.get("/api/audits/{audit_id}", response_class=JSONResponse)
    async def get_audit(audit_id: str) -> JSONResponse:
        result = audit_store.get(audit_id)
        if result is None:
            return JSONResponse({"error": "audit not found"}, status_code=404)
        return JSONResponse(result.to_dict())

    @app.get("/api/audits/{audit_id}/report", response_class=HTMLResponse)
    async def get_report(audit_id: str) -> HTMLResponse:
        result = audit_store.get(audit_id)
        if result is None:
            return HTMLResponse(
                report_html._page("未找到", "<h1>未找到该审计</h1>"), status_code=404
            )
        return HTMLResponse(report_html.render_report(result.to_dict()))

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        # 优先返回前端构建产物 web/index.html；未构建时退回服务端渲染的极简列表。
        index_path = WEB_DIR / "index.html"
        if index_path.is_file():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        return HTMLResponse(report_html.render_index(audit_store.list()))

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots() -> str:
        return "User-agent: *\nDisallow: /\n"

    return app


app = create_app()
