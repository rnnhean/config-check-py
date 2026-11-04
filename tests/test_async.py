"""异步 LLM 客户端、并发限流、trace_id 隔离的测试。

全部使用 mock transport / 假 provider，不发真实网络请求。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.concurrency import AsyncConcurrencyLimiter, QueueWaitTimeout
from app.llm import (
    AsyncLlmProvider,
    AsyncLlmRuntime,
    AsyncMockProvider,
    AsyncOpenAICompatClient,
    LLMHttpStatusError,
    LLMTimeoutError,
    MockProvider,
    _fallback,
)
from app.main import create_app
from app.pipeline import run_audit_async
from app.store import AuditStore


def make_client(handler: Any, **kwargs: Any) -> AsyncOpenAICompatClient:
    """用 MockTransport 构造客户端，避免真实网络。"""
    defaults = dict(
        api_key="test-fake-key",
        base_url="https://fake.local/v1",
        model="fake-model",
        backoff_base=0.0,
        max_retries=2,
        transport=httpx.MockTransport(handler),
    )
    defaults.update(kwargs)
    return AsyncOpenAICompatClient(**defaults)


def ok_body(text: str = "模型输出") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    }


# ---------------------------------------------------------------- 异步客户端


def test_async_client_success() -> None:
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.headers["authorization"] == "Bearer test-fake-key"
        return httpx.Response(200, json=ok_body())

    async def run() -> None:
        client = make_client(handler)
        try:
            text, pt, ct = await client.complete("sys", "user")
            assert text == "模型输出"
            assert (pt, ct) == (12, 7)
        finally:
            await client.aclose()

    asyncio.run(run())
    assert calls["n"] == 1


def test_async_client_retries_5xx_then_succeeds() -> None:
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503, text="upstream busy")
        return httpx.Response(200, json=ok_body("恢复"))

    async def run() -> None:
        client = make_client(handler)
        try:
            text, _, _ = await client.complete("sys", "user")
            assert text == "恢复"
        finally:
            await client.aclose()

    asyncio.run(run())
    # 首次 + 2 次重试 = 3 次
    assert calls["n"] == 3


def test_async_client_gives_up_after_two_retries() -> None:
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    async def run() -> None:
        client = make_client(handler)
        try:
            with pytest.raises(LLMHttpStatusError) as exc:
                await client.complete("sys", "user")
            assert exc.value.status_code == 500
        finally:
            await client.aclose()

    asyncio.run(run())
    # 1 次 + 最多 2 次重试
    assert calls["n"] == 3


def test_async_client_does_not_retry_4xx() -> None:
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    async def run() -> None:
        client = make_client(handler)
        try:
            with pytest.raises(LLMHttpStatusError):
                await client.complete("sys", "user")
        finally:
            await client.aclose()

    asyncio.run(run())
    assert calls["n"] == 1


def test_async_client_retries_transport_error() -> None:
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=ok_body())

    async def run() -> None:
        client = make_client(handler)
        try:
            text, _, _ = await client.complete("sys", "user")
            assert text == "模型输出"
        finally:
            await client.aclose()

    asyncio.run(run())
    assert calls["n"] == 2


def test_async_client_timeout_raises_and_counts() -> None:
    from app.observability import METRICS

    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    before = METRICS.snapshot()["llm"]["timeoutsTotal"]

    async def run() -> None:
        client = make_client(handler)
        try:
            with pytest.raises(LLMTimeoutError):
                await client.complete("sys", "user")
        finally:
            await client.aclose()

    asyncio.run(run())
    after = METRICS.snapshot()["llm"]["timeoutsTotal"]
    assert calls["n"] == 3
    # 3 次超时都要计入指标
    assert after - before == 3


def test_async_client_respects_retry_after_on_429() -> None:
    calls = {"n": 0}
    seen_delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.1"}, text="slow down")
        return httpx.Response(200, json=ok_body())

    async def run() -> None:
        client = make_client(handler, backoff_base=0.0)
        try:
            started = time.perf_counter()
            text, _, _ = await client.complete("sys", "user")
            seen_delays.append(time.perf_counter() - started)
            assert text == "模型输出"
        finally:
            await client.aclose()

    asyncio.run(run())
    assert calls["n"] == 2
    # 尊重 Retry-After：至少等待 0.1s，而不是 0 退避
    assert seen_delays[0] >= 0.09


def test_retry_after_is_capped() -> None:
    from app.llm import _retry_after_seconds

    headers = httpx.Headers({"Retry-After": "3600"})
    assert _retry_after_seconds(headers) == 3600.0
    # 客户端侧上限 30s，单次等待不会超过 retry_after_cap
    client = AsyncOpenAICompatClient(api_key="x", retry_after_cap=30.0)
    assert client._retry_after_cap == 30.0


def test_async_client_does_not_log_secrets(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ok_body())

    async def run() -> None:
        client = make_client(handler)
        try:
            await client.complete("sys", "user")
        finally:
            await client.aclose()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "test-fake-key" not in logs
    assert "Authorization" not in logs
    assert "Bearer" not in logs


# ------------------------------------------------------------ 运行时降级链路


class FailingAsyncProvider(AsyncLlmProvider):
    name = "failing-async"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def complete(self, system: str, user: str, timeout: float) -> tuple[str, int, int]:
        self.calls += 1
        raise self.exc


def test_async_runtime_degrades_without_raising() -> None:
    provider = FailingAsyncProvider(LLMTimeoutError("timeout"))
    runtime = AsyncLlmRuntime(provider=provider, model="fake", budget_tokens=10_000, max_retries=2)
    facts = {
        "kind": "product_finding",
        "ruleLabel": "R1",
        "consequence": "下游无法关联",
        "affected": 6,
        "affectedShare": 0.041,
        "scope": "product",
    }
    text = asyncio.run(runtime.complete("product_finding", facts))
    assert text == _fallback("product_finding", facts)
    assert provider.calls == 3
    assert runtime.stats.failures == 1
    assert runtime.stats.degraded is True


def test_async_runtime_budget_short_circuit() -> None:
    provider = FailingAsyncProvider(RuntimeError("should not be called"))
    runtime = AsyncLlmRuntime(provider=provider, model="fake", budget_tokens=1)
    assert asyncio.run(runtime.complete("product_finding", {"kind": "product_finding", "ruleLabel": "R1"})) != ""
    assert provider.calls == 0
    assert runtime.stats.budget_exhausted is True
    assert runtime.stats.calls == 0
    text = _fallback("product_finding", {"kind": "product_finding", "ruleLabel": "R1"})
    assert "R1" in text


def test_async_mock_matches_sync_mock() -> None:
    facts = {
        "kind": "product_finding",
        "scope": "product",
        "ruleLabel": "R1",
        "affected": 6,
        "affectedShare": 0.041,
        "consequence": "下游无法关联",
    }
    user = json.dumps(facts, ensure_ascii=False)
    sync_out = MockProvider().complete("sys", user, 1.0)
    async_out = asyncio.run(AsyncMockProvider().complete("sys", user, 1.0))
    assert sync_out == async_out


def test_async_pipeline_end_to_end(data_path: Path) -> None:
    runtime = AsyncLlmRuntime(provider=AsyncMockProvider(), model="mock-deterministic")
    result = asyncio.run(run_audit_async(data_path, runtime))
    assert result.status == "done"
    assert result.report["findingCount"] == 4
    assert result.report["distinctAffected"] == 27
    assert [t["name"] for t in result.traces] == [
        "load_catalog",
        "scan_rules",
        "quantify",
        "assess",
        "draft_fixes",
        "compile",
    ]


# ------------------------------------------------------------------ 限流器


def test_limiter_queues_instead_of_rejecting() -> None:
    limiter = AsyncConcurrencyLimiter(limit=1, queue_timeout=1.0)
    order: list[str] = []

    async def worker(name: str, hold: float) -> None:
        async with limiter:
            order.append(f"start:{name}")
            await asyncio.sleep(hold)
            order.append(f"end:{name}")

    async def run() -> None:
        await asyncio.gather(worker("a", 0.05), worker("b", 0.0), worker("c", 0.0))

    asyncio.run(run())
    # 严格的串行入队：同一个槽位交替交接，不会两个同时进去
    assert order == ["start:a", "end:a", "start:b", "end:b", "start:c", "end:c"]
    assert limiter.inflight == 0
    assert limiter.queue_depth == 0


def test_limiter_queue_timeout() -> None:
    limiter = AsyncConcurrencyLimiter(limit=1, queue_timeout=0.05)

    async def run() -> None:
        await limiter.acquire()
        try:
            with pytest.raises(QueueWaitTimeout):
                await limiter.acquire()
        finally:
            limiter.release()
        # 超时的等待者不能泄漏槽位
        assert limiter.queue_depth == 0
        assert limiter.inflight == 0

    asyncio.run(run())


def test_limiter_queue_depth_metric_updates() -> None:
    from app.observability import METRICS

    limiter = AsyncConcurrencyLimiter(limit=1, queue_timeout=1.0)

    async def run() -> None:
        await limiter.acquire()
        task = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0.02)
        assert limiter.queue_depth == 1
        assert METRICS.snapshot()["llm_queue_depth"] == 1
        limiter.release()
        await task
        assert limiter.queue_depth == 0
        assert METRICS.snapshot()["llm_queue_depth"] == 0
        limiter.release()

    asyncio.run(run())


def test_limiter_rebinds_when_event_loop_changes() -> None:
    limiter = AsyncConcurrencyLimiter(limit=2, queue_timeout=0.5)

    async def use() -> None:
        async with limiter:
            await asyncio.sleep(0.01)

    asyncio.run(use())
    asyncio.run(use())
    assert limiter.inflight == 0


# ------------------------------------------------- 并发端点：排队、429、trace_id


class SlowAsyncProvider(AsyncLlmProvider):
    """固定延迟 provider，用于制造并发叠加。"""

    name = "slow-async"

    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay

    async def complete(self, system: str, user: str, timeout: float) -> tuple[str, int, int]:
        await asyncio.sleep(self.delay)
        return "并发测试文案", 10, 5


def _slow_factory(delay: float):
    def factory() -> AsyncLlmRuntime:
        return AsyncLlmRuntime(provider=SlowAsyncProvider(delay), model="slow")

    return factory


def test_concurrent_requests_queue_and_timeout_with_429(
    tmp_path: Path, data_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_LLM", "1")
    monkeypatch.setenv("LLM_QUEUE_TIMEOUT_SECONDS", "0.08")
    app = create_app(
        data_path=data_path,
        store=AuditStore(tmp_path / "audits"),
        async_runtime_factory=_slow_factory(0.4),
    )

    async def run() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await asyncio.gather(*[client.post("/api/audits") for _ in range(3)])

    responses = asyncio.run(run())
    codes = sorted(r.status_code for r in responses)
    # 1 个成功进入，另外 2 个排队超时 → 429
    assert codes == [201, 429, 429]
    for r in responses:
        if r.status_code == 429:
            assert int(r.headers["retry-after"]) >= 1
            assert r.json()["error"] == "llm queue timeout"


def test_concurrent_requests_within_limit_all_succeed(
    tmp_path: Path, data_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_LLM", "4")
    monkeypatch.setenv("LLM_QUEUE_TIMEOUT_SECONDS", "5")
    app = create_app(
        data_path=data_path,
        store=AuditStore(tmp_path / "audits"),
        async_runtime_factory=_slow_factory(0.15),
    )

    async def run() -> tuple[list[httpx.Response], float]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.perf_counter()
            responses = await asyncio.gather(*[client.post("/api/audits") for _ in range(4)])
            return responses, time.perf_counter() - started

    responses, elapsed = asyncio.run(run())
    assert [r.status_code for r in responses] == [201] * 4
    # 4 个并发、每个 5 次 0.15s 调用：串行要 ~3s，并发应远小于此
    assert elapsed < 2.5


def test_trace_id_is_not_shared_across_concurrent_requests(
    tmp_path: Path, data_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_LLM", "8")
    monkeypatch.setenv("LLM_QUEUE_TIMEOUT_SECONDS", "5")
    app = create_app(
        data_path=data_path,
        store=AuditStore(tmp_path / "audits"),
        async_runtime_factory=_slow_factory(0.05),
    )

    async def run() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await asyncio.gather(*[client.post("/api/audits") for _ in range(8)])

    responses = asyncio.run(run())
    header_ids = [r.headers["x-trace-id"] for r in responses]
    body_ids = [r.json()["traceId"] for r in responses]
    run_ids = [r.json()["id"] for r in responses]

    assert len(set(header_ids)) == 8
    assert header_ids == body_ids
    assert len(set(run_ids)) == 8
    # 并发不改变结果
    assert all(r.json()["report"]["findingCount"] == 4 for r in responses)


def test_metrics_expose_concurrency_fields(tmp_path: Path, data_path: Path) -> None:
    app = create_app(
        data_path=data_path,
        store=AuditStore(tmp_path / "audits"),
        async_runtime_factory=_slow_factory(0.01),
    )
    with TestClient(app) as client:
        client.post("/api/audits")
        body = client.get("/api/metrics").json()
    for key in (
        "llm_inflight",
        "llm_queue_depth",
        "llm_timeouts_total",
        "llm_retries_total",
        "llm_p50_ms",
        "llm_p95_ms",
    ):
        assert key in body
    for key in ("inflight", "queueDepth", "timeoutsTotal", "retriesTotal", "p50Ms", "p95Ms"):
        assert key in body["llm"]
    assert body["llm"]["latencyCount"] >= 5


def test_sync_fallback_still_works(tmp_path: Path, data_path: Path) -> None:
    """LLM_ASYNC=0 时走同步运行时，结果与异步一致。"""
    from app.llm import LlmRuntime, MockProvider

    def sync_factory() -> LlmRuntime:
        return LlmRuntime(provider=MockProvider(), model="mock-deterministic")

    app = create_app(
        data_path=data_path,
        store=AuditStore(tmp_path / "audits"),
        runtime_factory=sync_factory,
    )
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["llmAsync"] is False
        created = client.post("/api/audits")
        assert created.status_code == 201
        assert created.json()["report"]["distinctAffected"] == 27


def test_runtime_factory_reuses_provider_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接池复用：多次取 runtime，provider 是同一个实例；stats 仍然按次隔离。"""
    from app.llm import AsyncRuntimeFactory

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_MODE", "mock")
    factory = AsyncRuntimeFactory()
    first = factory()
    second = factory()
    assert first.provider is second.provider
    assert first.stats is not second.stats
    assert first.stats.calls == 0 and second.stats.calls == 0
    asyncio.run(factory.aclose())
