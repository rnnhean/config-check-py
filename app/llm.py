"""LLM 适配层：Mock 确定性输出 + 真实 OpenAI 兼容接口。

安全约束：
- 任何情况下都不打印 API key、请求头或完整请求体。
- 默认 LLM_MODE=mock，完全离线；只有显式非 mock 且存在 OPENAI_API_KEY 才走真实调用。
- 异步客户端只记录状态码、耗时与尝试次数，不落任何请求/响应内容。

韧性：
- 单次调用超时（同步路径默认 20s；异步路径连接 5s、读取 120s）
- 失败最多重试 2 次（即最多 3 次尝试）
- 异步客户端只对连接错误 / 5xx / 429 重试，429 尊重 Retry-After（上限 30s）
- 重试用尽 / 超时 / 预算耗尽 → 返回确定性兜底文案，不中断审计
- 单次运行 token 预算上限，超限后不再调用模型，直接降级

异步切换：
- LLM_ASYNC=1（默认）走 AsyncLlmRuntime + httpx.AsyncClient
- LLM_ASYNC=0 保留同步 LlmRuntime 作为 fallback，行为与改造前一致
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any

import httpx

from .observability import METRICS, log_event

logger = logging.getLogger("config_check.llm")

SYSTEM_PROMPTS = {
    "product_finding": (
        "你是负责产品目录数据治理的技术负责人。根据给出的确定性检测事实，用 2-3 句中文说明这个配置缺陷"
        "会造成什么下游后果、为什么值得修。只能使用事实中的数据，不得编造产品或数字，"
        "不要输出 JSON、标题或 Markdown。"
    ),
    "product_report": (
        "你是负责产品目录数据治理的技术负责人。根据给出的事实，用 3-4 句中文写一段给管理层的摘要，"
        "概括产品目录存在哪些系统性问题、哪些会阻塞跨系统对接。只能使用事实中的数据，不得编造，"
        "不要输出 JSON 或 Markdown。"
    ),
}


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：4 个字符 ≈ 1 token。用于预算控制与 mock 计数。"""
    return max(1, (len(text) + 3) // 4)


@dataclass
class LlmStats:
    calls: int = 0
    attempts: int = 0
    failures: int = 0
    total_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    degraded: bool = False
    budget_exhausted: bool = False

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self, cost_usd: float) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "attempts": self.attempts,
            "failures": self.failures,
            "totalMs": round(self.total_ms, 2),
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "tokens": self.tokens,
            "costUsd": round(cost_usd, 6),
            "degraded": self.degraded,
            "budgetExhausted": self.budget_exhausted,
        }


class LlmProvider:
    name = "base"

    def complete(self, system: str, user: str, timeout: float) -> tuple[str, int, int]:
        raise NotImplementedError


class MockProvider(LlmProvider):
    """确定性 mock：同一事实永远得到同一段文案，便于离线测试与演示。"""

    name = "mock"

    def complete(self, system: str, user: str, timeout: float) -> tuple[str, int, int]:
        try:
            facts = json.loads(user)
        except json.JSONDecodeError:
            return "离线模式：已根据确定性规则完成本步骤分析。", 0, 0
        kind = facts.get("kind")
        if kind == "product_finding":
            unit = "终端编码" if facts.get("scope") == "terminal" else "产品"
            base = "终端字典" if facts.get("scope") == "terminal" else "目录"
            share = facts.get("affectedShare", 0) * 100
            text = (
                f"「{facts.get('ruleLabel')}」在目录中命中 {facts.get('affected')} 个{unit}"
                f"（占{base} {share:.1f}%）。{facts.get('consequence', '')}"
            )
        elif kind == "product_report":
            b = facts.get("breakdown", {})
            text = (
                f"本次共扫描 {facts.get('total')} 个产品，命中 {facts.get('findingCount')} 条检查规则，"
                f"去重后 {facts.get('distinctAffected')} 个产品存在至少一项配置问题。"
                f"按严重度分布为高危 {b.get('critical', 0)} 条、较高 {b.get('high', 0)} 条、"
                f"中等 {b.get('medium', 0)} 条、较低 {b.get('low', 0)} 条，"
                f"主要风险集中在{facts.get('topRuleLabel')}。"
                "建议先修复阻塞跨系统对接的字段类问题，再处理口径不一致的标记类问题。"
            )
        else:
            text = "离线模式：已根据确定性规则完成本步骤分析。"
        return text, estimate_tokens(system + user), estimate_tokens(text)


class OpenAICompatibleProvider(LlmProvider):
    """OpenAI 兼容 /chat/completions。只从环境读取 key，绝不外泄。"""

    name = "openai-compatible"

    def __init__(self) -> None:
        self._api_key = os.environ.get("OPENAI_API_KEY", "")
        self._base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self._model = os.environ.get("MODEL_NAME", "gpt-4o-mini")

    def available(self) -> bool:
        return bool(self._api_key)

    @property
    def model(self) -> str:
        return self._model

    def complete(self, system: str, user: str, timeout: float) -> tuple[str, int, int]:
        import httpx

        payload = {
            "model": self._model,
            "temperature": 0.3,
            "max_tokens": 600,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # 只报状态码，不记录请求头与 body，避免 key / 业务数据进日志
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                json=payload,
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"LLM HTTP {resp.status_code}")
        data = resp.json()
        text = str(data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or estimate_tokens(system + user))
        completion_tokens = int(usage.get("completion_tokens") or estimate_tokens(text))
        return text, prompt_tokens, completion_tokens


class LLMError(RuntimeError):
    """LLM 调用错误基类。异常信息里不带 key / 请求头 / 响应体。"""


class LLMHttpStatusError(LLMError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"LLM HTTP {status_code}")
        self.status_code = status_code


class LLMTransportError(LLMError):
    """连接失败、连接超时、读取超时等网络层错误。"""


class LLMTimeoutError(LLMTransportError):
    """明确标记超时，便于上层统计。"""


def _retry_after_seconds(headers: httpx.Headers) -> float | None:
    """解析 Retry-After：支持秒数与 HTTP-date 两种格式。"""
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class AsyncLlmProvider:
    """异步 provider 接口。"""

    name = "base-async"

    async def complete(self, system: str, user: str, timeout: float) -> tuple[str, int, int]:
        raise NotImplementedError

    async def aclose(self) -> None:
        """释放底层连接池；mock provider 无需实现。"""


class AsyncMockProvider(AsyncLlmProvider):
    """异步 mock：复用同步 MockProvider 的确定性文案，保证两种模式输出一致。"""

    name = "mock"

    def __init__(self) -> None:
        self._inner = MockProvider()

    async def complete(self, system: str, user: str, timeout: float) -> tuple[str, int, int]:
        return self._inner.complete(system, user, timeout)


class AsyncOpenAICompatClient(AsyncLlmProvider):
    """OpenAI 兼容 /chat/completions 的异步客户端。

    连接池由单个 httpx.AsyncClient 持有，生命周期与 FastAPI 应用一致；
    不要在每次请求里重新构造，否则会退化成「每请求一次 TCP/TLS 握手」。

    重试策略（最多 2 次重试，指数退避）：
    - 仅连接错误 / 读取超时 / 5xx / 429
    - 429 优先使用 Retry-After，单次等待上限 30s
    - 4xx（除 429）不重试，直接失败交给上层降级
    """

    name = "openai-compatible"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 120.0,
        write_timeout: float = 10.0,
        pool_timeout: float = 5.0,
        max_retries: int = 2,
        backoff_base: float = 0.5,
        backoff_max: float = 8.0,
        retry_after_cap: float = 30.0,
        max_connections: int = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        self._base_url = (
            base_url if base_url is not None else os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self._model = model if model is not None else os.environ.get("MODEL_NAME", "gpt-4o-mini")
        self._max_retries = max(0, max_retries)
        self._backoff_base = max(0.0, backoff_base)
        self._backoff_max = max(self._backoff_base, backoff_max)
        self._retry_after_cap = max(0.0, retry_after_cap)
        self._timeout = httpx.Timeout(
            connect=connect_timeout, read=read_timeout, write=write_timeout, pool=pool_timeout
        )
        # 连接池复用：max_keepalive_connections 控制空闲长连接数量。
        # 上限给到 MAX_CONCURRENT_LLM 的规模即可，避免连接数无限堆积。
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            transport=transport,
            limits=httpx.Limits(
                max_connections=max_connections, max_keepalive_connections=max_connections
            ),
        )

    @property
    def model(self) -> str:
        return self._model

    def available(self) -> bool:
        return bool(self._api_key)

    def _backoff_delay(self, attempt: int) -> float:
        return min(self._backoff_base * (2**attempt), self._backoff_max)

    async def complete(self, system: str, user: str, timeout: float | None = None) -> tuple[str, int, int]:
        payload = {
            "model": self._model,
            "temperature": 0.3,
            "max_tokens": 600,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        url = f"{self._base_url}/chat/completions"
        # 调用方可以覆盖读取超时，但连接超时始终保持 5s 级别
        request_timeout: httpx.Timeout | None = None
        if timeout is not None:
            request_timeout = httpx.Timeout(
                connect=self._timeout.connect,
                read=timeout,
                write=self._timeout.write,
                pool=self._timeout.pool,
            )
        attempt = 0
        while True:
            started = time.perf_counter()
            try:
                response = await self._client.post(
                    url, headers=headers, json=payload, timeout=request_timeout
                )
            except httpx.TimeoutException as exc:
                duration_ms = (time.perf_counter() - started) * 1000
                METRICS.record_llm_timeout()
                log_event(
                    "llm_attempt",
                    outcome="timeout",
                    attempt=attempt + 1,
                    durationMs=round(duration_ms, 2),
                )
                if attempt < self._max_retries:
                    METRICS.record_llm_retry()
                    attempt += 1
                    await asyncio.sleep(self._backoff_delay(attempt - 1))
                    continue
                raise LLMTimeoutError("LLM timeout") from exc
            except httpx.TransportError as exc:
                duration_ms = (time.perf_counter() - started) * 1000
                log_event(
                    "llm_attempt",
                    outcome="transport_error",
                    attempt=attempt + 1,
                    durationMs=round(duration_ms, 2),
                )
                if attempt < self._max_retries:
                    METRICS.record_llm_retry()
                    attempt += 1
                    await asyncio.sleep(self._backoff_delay(attempt - 1))
                    continue
                raise LLMTransportError("LLM connection failed") from exc

            duration_ms = (time.perf_counter() - started) * 1000
            status = response.status_code
            # 只记录状态码 / 耗时 / 尝试次数，绝不落 headers 或 body
            log_event(
                "llm_attempt",
                outcome="http",
                status=status,
                attempt=attempt + 1,
                durationMs=round(duration_ms, 2),
            )
            if status == 429 or status >= 500:
                if attempt < self._max_retries:
                    retry_after = _retry_after_seconds(response.headers) if status == 429 else None
                    if retry_after is None:
                        delay = self._backoff_delay(attempt)
                    else:
                        delay = min(retry_after, self._retry_after_cap)
                    METRICS.record_llm_retry()
                    log_event(
                        "llm_retry",
                        status=status,
                        attempt=attempt + 1,
                        delayMs=round(delay * 1000, 2),
                    )
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
                raise LLMHttpStatusError(status)
            if status >= 400:
                raise LLMHttpStatusError(status)

            data = response.json()
            text = str(data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
            usage = data.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or estimate_tokens(system + user))
            completion_tokens = int(usage.get("completion_tokens") or estimate_tokens(text))
            return text, prompt_tokens, completion_tokens

    async def aclose(self) -> None:
        await self._client.aclose()


def _fallback(kind: str, facts: dict[str, Any]) -> str:
    """确定性的兜底文案：不依赖模型，数字全部来自事实。"""
    if kind == "product_finding":
        unit = "终端编码" if facts.get("scope") == "terminal" else "产品"
        base = "终端字典" if facts.get("scope") == "terminal" else "目录"
        share = facts.get("affectedShare", 0) * 100
        return (
            f"「{facts.get('ruleLabel')}」命中 {facts.get('affected')} {unit}"
            f"（占{base} {share:.1f}%）。{facts.get('consequence', '')}"
        )
    if kind == "product_report":
        b = facts.get("breakdown", {})
        return (
            f"本次扫描 {facts.get('total')} 个产品，命中 {facts.get('findingCount')} 条检查规则。"
            f"去重后 {facts.get('distinctAffected')} 个产品存在至少一项配置问题，"
            f"其中最严重的是「{facts.get('topRuleLabel')}」。按严重度分布为高危 {b.get('critical', 0)} 条、"
            f"较高 {b.get('high', 0)} 条、中等 {b.get('medium', 0)} 条、较低 {b.get('low', 0)} 条。"
        )
    return "已根据确定性规则完成本步骤分析。"


@dataclass
class LlmRuntime:
    provider: LlmProvider
    model: str
    budget_tokens: int = 4000
    max_retries: int = 2
    timeout_s: float = 20.0
    # 价格仅用于成本估算，默认取 gpt-4o-mini 量级，可用环境变量覆盖
    price_in_per_m: float = 0.15
    price_out_per_m: float = 0.60
    stats: LlmStats = field(default_factory=LlmStats)
    # 节点失败时由 pipeline 写入，保证异常路径也有可观测痕迹
    failed_traces: list[Any] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def cost_usd(self) -> float:
        return (
            self.stats.prompt_tokens / 1_000_000 * self.price_in_per_m
            + self.stats.completion_tokens / 1_000_000 * self.price_out_per_m
        )

    def complete(self, kind: str, facts: dict[str, Any]) -> str:
        fallback = _fallback(kind, facts)
        system = SYSTEM_PROMPTS.get(kind, SYSTEM_PROMPTS["product_report"])
        user = json.dumps(facts, ensure_ascii=False)
        # 预算按「已用 token + 本次预估 prompt token」判断，超限后连一次都不发
        estimated = estimate_tokens(system + user)

        with self._lock:
            if self.stats.tokens >= self.budget_tokens or (
                self.stats.tokens + estimated > self.budget_tokens
            ):
                if not self.stats.budget_exhausted:
                    logger.warning("llm budget exhausted, degrading to deterministic text")
                self.stats.budget_exhausted = True
                self.stats.degraded = True
                return fallback

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            with self._lock:
                self.stats.attempts += 1
            started = time.perf_counter()
            METRICS.llm_call_started()
            try:
                text, prompt_tokens, completion_tokens = self.provider.complete(
                    system, user, self.timeout_s
                )
                if not text.strip():
                    raise RuntimeError("empty completion")
                with self._lock:
                    self.stats.calls += 1
                    self.stats.prompt_tokens += prompt_tokens
                    self.stats.completion_tokens += completion_tokens
                return text.strip()
            except Exception as exc:  # noqa: BLE001 - 统一降级，不把异常抛给流程
                last_error = exc
                if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
                    METRICS.record_llm_timeout()
                logger.warning(
                    "llm call failed (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries + 1,
                    type(exc).__name__,
                )
            finally:
                METRICS.llm_call_finished((time.perf_counter() - started) * 1000)
        with self._lock:
            self.stats.failures += 1
            self.stats.degraded = True
        logger.warning("llm degraded after retries: %s", type(last_error).__name__)
        return fallback


def build_runtime() -> LlmRuntime:
    mode = os.environ.get("LLM_MODE", "mock").strip().lower()
    budget = int(os.environ.get("LLM_TOKEN_BUDGET", "4000"))
    timeout_s = float(os.environ.get("LLM_TIMEOUT_SECONDS", "20"))
    real = OpenAICompatibleProvider()
    if mode == "mock" or not real.available():
        provider: LlmProvider = MockProvider()
        model = "mock-deterministic"
    else:
        provider = real
        model = real.model
    return LlmRuntime(
        provider=provider,
        model=model,
        budget_tokens=budget,
        timeout_s=timeout_s,
        price_in_per_m=float(os.environ.get("LLM_PRICE_IN_PER_M", "0.15")),
        price_out_per_m=float(os.environ.get("LLM_PRICE_OUT_PER_M", "0.60")),
    )


@dataclass
class AsyncLlmRuntime:
    """异步版运行时。计费/预算/降级逻辑与同步版一致，只是把 IO 换成 await。

    注意：预算检查和统计计数仍走 threading.Lock。这不阻塞，锁内只有几次整数运算；
    真实耗时全在 `await provider.complete()` 里，不会持锁等待网络。
    """

    provider: AsyncLlmProvider
    model: str
    budget_tokens: int = 4000
    max_retries: int = 2
    # None 表示沿用 provider 自身的分级超时（真实客户端读 env 的 connect/read）；
    # 只有需要临时覆盖的调用方才会传具体秒数。
    timeout_s: float | None = None
    price_in_per_m: float = 0.15
    price_out_per_m: float = 0.60
    stats: LlmStats = field(default_factory=LlmStats)
    failed_traces: list[Any] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def cost_usd(self) -> float:
        return (
            self.stats.prompt_tokens / 1_000_000 * self.price_in_per_m
            + self.stats.completion_tokens / 1_000_000 * self.price_out_per_m
        )

    def _budget_blocked(self, estimated: int) -> bool:
        with self._lock:
            if not (
                self.stats.tokens >= self.budget_tokens
                or self.stats.tokens + estimated > self.budget_tokens
            ):
                return False
            if not self.stats.budget_exhausted:
                logger.warning("llm budget exhausted, degrading to deterministic text")
            self.stats.budget_exhausted = True
            self.stats.degraded = True
            return True

    async def complete(self, kind: str, facts: dict[str, Any]) -> str:
        fallback = _fallback(kind, facts)
        system = SYSTEM_PROMPTS.get(kind, SYSTEM_PROMPTS["product_report"])
        user = json.dumps(facts, ensure_ascii=False)
        estimated = estimate_tokens(system + user)

        if self._budget_blocked(estimated):
            return fallback

        last_error: Exception | None = None
        # 客户端内部已对连接错误 / 5xx / 429 做了重试；这里再包一层是兜底，
        # 保证 provider 是 mock 或自定义实现时也有统一的尝试上限与降级。
        for attempt in range(self.max_retries + 1):
            with self._lock:
                self.stats.attempts += 1
            started = time.perf_counter()
            METRICS.llm_call_started()
            timed_out = False
            try:
                text, prompt_tokens, completion_tokens = await self.provider.complete(
                    system, user, self.timeout_s
                )
                if not text.strip():
                    raise RuntimeError("empty completion")
                with self._lock:
                    self.stats.calls += 1
                    self.stats.prompt_tokens += prompt_tokens
                    self.stats.completion_tokens += completion_tokens
                return text.strip()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一降级，不把异常抛给流程
                last_error = exc
                timed_out = isinstance(exc, (LLMTimeoutError, httpx.TimeoutException, TimeoutError))
                logger.warning(
                    "llm async call failed (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries + 1,
                    type(exc).__name__,
                )
            finally:
                METRICS.llm_call_finished((time.perf_counter() - started) * 1000, timed_out=timed_out)
        with self._lock:
            self.stats.failures += 1
            self.stats.degraded = True
        logger.warning("llm async degraded after retries: %s", type(last_error).__name__)
        return fallback

    async def aclose(self) -> None:
        await self.provider.aclose()


def build_async_runtime() -> AsyncLlmRuntime:
    """一次性构造异步运行时（自带 provider）。

    服务进程请用 `AsyncRuntimeFactory`：它复用同一个 provider 连接池；
    这个函数每次调用都会新建客户端，只适合脚本 / 单次调用场景。
    """
    mode = os.environ.get("LLM_MODE", "mock").strip().lower()
    budget = int(os.environ.get("LLM_TOKEN_BUDGET", "4000"))
    # mock 模式不要先构造真实客户端：AsyncOpenAICompatClient 会创建连接池，丢弃即泄漏
    if mode == "mock" or not os.environ.get("OPENAI_API_KEY", ""):
        provider: AsyncLlmProvider = AsyncMockProvider()
        model = "mock-deterministic"
    else:
        real = AsyncOpenAICompatClient(
            connect_timeout=float(os.environ.get("LLM_CONNECT_TIMEOUT_SECONDS", "5")),
            read_timeout=float(os.environ.get("LLM_READ_TIMEOUT_SECONDS", "120")),
        )
        provider = real
        model = real.model
    return AsyncLlmRuntime(
        provider=provider,
        model=model,
        budget_tokens=budget,
        price_in_per_m=float(os.environ.get("LLM_PRICE_IN_PER_M", "0.15")),
        price_out_per_m=float(os.environ.get("LLM_PRICE_OUT_PER_M", "0.60")),
    )


def async_enabled() -> bool:
    """LLM_ASYNC 默认 1；只有显式为 0/false/no 时走同步实现。"""
    return os.environ.get("LLM_ASYNC", "1").strip().lower() not in ("0", "false", "no", "off")


class AsyncRuntimeFactory:
    """按次返回新的 AsyncLlmRuntime，但复用同一个 provider（连接池）。

    这样拆开是有意的：
    - provider（httpx.AsyncClient + 连接池）必须与应用同生命周期，否则每次请求
      都会重新做 TCP/TLS 握手，异步连接池就白建了；
    - runtime 每次审计新建，stats / 预算按「一次运行」统计，不会跨请求累加。
    """

    def __init__(self) -> None:
        self._provider: AsyncLlmProvider | None = None

    def _build_provider(self) -> AsyncLlmProvider:
        mode = os.environ.get("LLM_MODE", "mock").strip().lower()
        if mode == "mock" or not os.environ.get("OPENAI_API_KEY", ""):
            return AsyncMockProvider()
        return AsyncOpenAICompatClient(
            connect_timeout=float(os.environ.get("LLM_CONNECT_TIMEOUT_SECONDS", "5")),
            read_timeout=float(os.environ.get("LLM_READ_TIMEOUT_SECONDS", "120")),
        )

    def __call__(self) -> AsyncLlmRuntime:
        if self._provider is None:
            self._provider = self._build_provider()
        budget = int(os.environ.get("LLM_TOKEN_BUDGET", "4000"))
        model = "mock-deterministic" if self._provider.name == "mock" else getattr(
            self._provider, "model", "unknown"
        )
        return AsyncLlmRuntime(
            provider=self._provider,
            model=model,
            budget_tokens=budget,
            price_in_per_m=float(os.environ.get("LLM_PRICE_IN_PER_M", "0.15")),
            price_out_per_m=float(os.environ.get("LLM_PRICE_OUT_PER_M", "0.60")),
        )

    async def aclose(self) -> None:
        if self._provider is not None:
            await self._provider.aclose()
            self._provider = None
