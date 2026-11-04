"""可观测性：trace_id、结构化 JSON 日志、进程内指标聚合。"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象，方便被采集器直接解析。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["excType"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger("config_check")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.propagate = False


def log_event(event: str, **fields: Any) -> None:
    logging.getLogger("config_check").info("", extra={"extra_fields": {"event": event, **fields}})


def new_trace_id() -> str:
    return f"tr_{uuid.uuid4().hex[:16]}"


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


def _percentile(values: list[float], pct: float) -> float:
    """nearest-rank 百分位，输入必须已排序。样本为空时返回 0。"""
    if not values:
        return 0.0
    index = int(round((pct / 100.0) * (len(values) - 1)))
    return values[min(max(index, 0), len(values) - 1)]


@dataclass
class NodeTrace:
    name: str
    status: str = "pending"  # pending | done | failed
    duration_ms: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "durationMs": round(self.duration_ms, 2),
            "detail": self.detail,
        }
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class MetricsRegistry:
    """全进程累计指标。线程安全，FastAPI 多线程 worker 下也安全。"""

    audits_total: int = 0
    audits_failed: int = 0
    audits_running: int = 0
    llm_calls: int = 0
    llm_failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    node_durations: dict[str, list[float]] = field(default_factory=dict)
    # ---- LLM 并发与延迟指标 ----
    # inflight：真正在跑的模型调用数；queue_depth：排队等待并发槽的请求数
    llm_inflight: int = 0
    llm_queue_depth: int = 0
    llm_retries_total: int = 0
    llm_timeouts_total: int = 0
    # 只保留最近 2000 次调用的延迟，避免进程长期运行内存无限增长
    llm_latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_audit_start(self) -> None:
        with self._lock:
            self.audits_total += 1
            self.audits_running += 1

    def record_audit_end(self, failed: bool) -> None:
        with self._lock:
            self.audits_running = max(0, self.audits_running - 1)
            if failed:
                self.audits_failed += 1

    def record_llm(self, runtime: Any) -> None:
        stats = runtime.stats
        with self._lock:
            self.llm_calls += stats.calls
            self.llm_failures += stats.failures
            self.prompt_tokens += stats.prompt_tokens
            self.completion_tokens += stats.completion_tokens
            self.cost_usd += runtime.cost_usd

    def record_node(self, name: str, duration_ms: float) -> None:
        with self._lock:
            self.node_durations.setdefault(name, []).append(duration_ms)

    # ---- LLM 并发 / 重试 / 超时 / 延迟 ----
    def llm_call_started(self) -> None:
        with self._lock:
            self.llm_inflight += 1

    def llm_call_finished(self, duration_ms: float, timed_out: bool = False) -> None:
        with self._lock:
            self.llm_inflight = max(0, self.llm_inflight - 1)
            self.llm_latencies_ms.append(duration_ms)
            if timed_out:
                self.llm_timeouts_total += 1

    def record_llm_retry(self) -> None:
        with self._lock:
            self.llm_retries_total += 1

    def record_llm_timeout(self) -> None:
        with self._lock:
            self.llm_timeouts_total += 1

    def set_llm_queue_depth(self, depth: int) -> None:
        with self._lock:
            self.llm_queue_depth = max(0, depth)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            nodes = {
                name: {
                    "count": len(values),
                    "avgMs": round(sum(values) / len(values), 2) if values else 0.0,
                    "maxMs": round(max(values), 2) if values else 0.0,
                    "totalMs": round(sum(values), 2),
                }
                for name, values in self.node_durations.items()
            }
            latencies = sorted(self.llm_latencies_ms)
            p50 = round(_percentile(latencies, 50), 2)
            p95 = round(_percentile(latencies, 95), 2)
            llm = {
                "calls": self.llm_calls,
                "failures": self.llm_failures,
                "promptTokens": self.prompt_tokens,
                "completionTokens": self.completion_tokens,
                "tokens": self.prompt_tokens + self.completion_tokens,
                "costUsd": round(self.cost_usd, 6),
                "inflight": self.llm_inflight,
                "queueDepth": self.llm_queue_depth,
                "timeoutsTotal": self.llm_timeouts_total,
                "retriesTotal": self.llm_retries_total,
                "latencyCount": len(latencies),
                "p50Ms": p50,
                "p95Ms": p95,
            }
            return {
                "audits": {
                    "total": self.audits_total,
                    "failed": self.audits_failed,
                    "running": self.audits_running,
                },
                "llm": llm,
                "nodes": nodes,
                # 兼容任务/监控约定的 snake_case 字段，值和 llm 内一致
                "llm_inflight": self.llm_inflight,
                "llm_queue_depth": self.llm_queue_depth,
                "llm_timeouts_total": self.llm_timeouts_total,
                "llm_retries_total": self.llm_retries_total,
                "llm_p50_ms": p50,
                "llm_p95_ms": p95,
            }


METRICS = MetricsRegistry()
