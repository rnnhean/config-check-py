"""服务端并发保护：限制同时在跑的 LLM 调用数。

行为约定：
- 未超过 `MAX_CONCURRENT_LLM` 时立即放行，不引入额外延迟；
- 超过时排队等待（而不是直接拒绝），队列深度实时写入 `/api/metrics`；
- 排队超过 `LLM_QUEUE_TIMEOUT_SECONDS` 仍未拿到槽位 → 抛 `QueueWaitTimeout`，
  由中间件翻译成 `429 + Retry-After`，让客户端自己退避。

粒度说明：令牌按「一次审计请求」发放，而不是按「单次模型调用」。因为同一次审计内
（draft_fixes 的 4 条 + compile 的 1 条）模型调用是串行 await 的，所以「同时在跑的审计数
≤ MAX_CONCURRENT_LLM」等价于「同时在跑的模型调用数 ≤ MAX_CONCURRENT_LLM」；
按审计发令牌的好处是排队中的请求整体不占用模型资源，队列语义也更直观。

实现没有用 `asyncio.Semaphore` + `wait_for`，因为 wait_for 在取消瞬间可能
「令牌已发出但调用方已放弃」，造成槽位泄漏。这里用显式的等待队列：释放时直接把
槽位交给队首的 Future，队列长度天然就是 queue depth。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from typing import Any

from .observability import METRICS, log_event


class QueueWaitTimeout(Exception):
    """排队超时：当前没有可用槽位，且等待时间已超过上限。"""

    def __init__(self, waited_s: float) -> None:
        super().__init__(f"llm queue wait timeout after {waited_s:.3f}s")
        self.waited_s = waited_s


class AsyncConcurrencyLimiter:
    """计数信号量 + FIFO 等待队列，带排队超时。

    必须在事件循环内使用；每个 FastAPI 应用持有一个实例。
    跨事件循环复用（例如多次 TestClient）时自动重建等待队列状态，避免 future 绑定到旧循环。
    """

    def __init__(self, limit: int, queue_timeout: float = 10.0) -> None:
        self.limit = max(1, int(limit))
        self.queue_timeout = max(0.0, float(queue_timeout))
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # 换了事件循环：旧循环里的 future 已经不可能被唤醒，直接丢弃
            self._loop = loop
            self._active = 0
            self._waiters.clear()
            METRICS.set_llm_queue_depth(0)
        return loop

    @property
    def queue_depth(self) -> int:
        return len(self._waiters)

    @property
    def inflight(self) -> int:
        return self._active

    async def acquire(self) -> None:
        loop = self._bind_loop()
        if self._active < self.limit:
            self._active += 1
            return

        waiter: asyncio.Future[None] = loop.create_future()
        self._waiters.append(waiter)
        METRICS.set_llm_queue_depth(len(self._waiters))
        started = time.perf_counter()
        try:
            # shield：超时取消不能把「已交付的槽位」一并丢掉
            await asyncio.wait_for(asyncio.shield(waiter), timeout=self.queue_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            if not waiter.done():
                # 仍在排队：从队列摘除后归还给调用方处理
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                waited = time.perf_counter() - started
                log_event("llm_queue_timeout", waitedMs=round(waited * 1000, 2))
                raise QueueWaitTimeout(waited) from None
            # done 且未取消 → 超时瞬间正好拿到槽位，视为成功
        finally:
            METRICS.set_llm_queue_depth(len(self._waiters))

    def release(self) -> None:
        """归还槽位：有排队者就把它直接交给队首，否则减少 active。"""
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                METRICS.set_llm_queue_depth(len(self._waiters))
                return
        self._active = max(0, self._active - 1)

    @property
    def retry_after_seconds(self) -> int:
        """429 响应里建议客户端等待的秒数，至少 1 秒。"""
        return max(1, int(math.ceil(self.queue_timeout)))

    async def __aenter__(self) -> "AsyncConcurrencyLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()
