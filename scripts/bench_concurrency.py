"""并发压测：同步实现 vs 异步实现 vs 阻塞事件循环（改造前写法）。

测试方法
--------
- **模型侧用本地假端点**：`ThreadingHTTPServer` 起一个 OpenAI 兼容接口，
  `POST /v1/chat/completions` 固定 sleep `--latency-ms` 后返回固定 JSON。
  费用为 0，且延迟完全可控，能复现「一次模型调用要几百毫秒到数秒」的时序。
- **被测服务起真实 uvicorn 子进程**，通过环境变量切换实现：
  - `async`：`LLM_ASYNC=1`，`httpx.AsyncClient` + 连接池，端点 async
  - `sync`：`LLM_ASYNC=0`，同步 `httpx.Client`，由线程池（`asyncio.to_thread`）承载
  - `blocking`：`scripts/bench_blocking.py`，复刻改造前的写法——
    async 端点里直接调同步 `httpx.Client`，会独占事件循环
- 客户端用 `httpx.AsyncClient` + `asyncio.Semaphore(concurrency)` 发起 N 个
  `POST /api/audits`，记录总耗时、QPS、P50/P95、错误数。

用法
----
    .venv/Scripts/python.exe scripts/bench_concurrency.py
    .venv/Scripts/python.exe scripts/bench_concurrency.py --n 100 --latency-ms 300
    .venv/Scripts/python.exe scripts/bench_concurrency.py --groups async,sync
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = ROOT / "data" / "benchmark.md"
# 单次审计固定发 5 次模型调用（4 条 finding + 1 段摘要），与线上路径一致
LLM_CALLS_PER_AUDIT = 5


# --------------------------------------------------------------- 假模型端点


class _FakeLlmHandler(BaseHTTPRequestHandler):
    latency_s: float = 0.2
    # 支持 keep-alive：否则每次调用都新建 TCP 连接，Windows loopback 上
    # 关闭连接的回收延迟会盖过被测实现本身的差异
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        time.sleep(self.latency_s)
        body = json.dumps(
            {
                "choices": [{"message": {"content": "压测固定返回文案。"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默，避免刷屏
        return


def start_fake_llm(latency_ms: int) -> tuple[ThreadingHTTPServer, int]:
    _FakeLlmHandler.latency_s = latency_ms / 1000.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLlmHandler)
    server.daemon_threads = True

    def handle_error(*_: Any) -> None:
        """客户端主动断开 keep-alive 连接属正常现象，不要刷 traceback。"""
        return None

    server.handle_error = handle_error  # type: ignore[method-assign]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


# ---------------------------------------------------------------- 应用进程


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class AppProcess:
    proc: subprocess.Popen[bytes]
    port: int
    label: str

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def wait_healthy(base_url: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{base_url}/api/health", timeout=2.0)
            if resp.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001 - 启动期连接失败属正常
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"app did not become healthy: {last_error}")


def launch_app(module: str, label: str, env_extra: dict[str, str]) -> AppProcess:
    port = free_port()
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            # 压测实现本身，先把并发保护放宽，避免被限流器挡住
            "MAX_CONCURRENT_LLM": "64",
            "LLM_QUEUE_TIMEOUT_SECONDS": "60",
            "LOG_LEVEL": "WARNING",
        }
    )
    env.update(env_extra)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            module,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    app = AppProcess(proc=proc, port=port, label=label)
    try:
        wait_healthy(app.base_url)
    except Exception:
        proc.terminate()
        proc.wait(timeout=10)
        raise
    return app


def stop_app(app: AppProcess) -> None:
    app.proc.terminate()
    try:
        app.proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        app.proc.kill()
        app.proc.wait(timeout=10)


# ------------------------------------------------------------------- 压测


@dataclass
class Sample:
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    statuses: dict[int, int] = field(default_factory=dict)

    def add(self, status: int, latency_ms: float) -> None:
        self.latencies_ms.append(latency_ms)
        self.statuses[status] = self.statuses.get(status, 0) + 1
        # status 0 = 客户端连接失败 / 超时，同样算错误
        if status == 0 or status >= 400:
            self.errors += 1


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[min(max(index, 0), len(ordered) - 1)]


async def run_load(base_url: str, concurrency: int, total: int) -> tuple[Sample, float]:
    semaphore = asyncio.Semaphore(concurrency)
    sample = Sample()

    async with httpx.AsyncClient(timeout=120.0, limits=httpx.Limits(max_connections=total)) as client:

        async def one() -> None:
            async with semaphore:
                started = time.perf_counter()
                try:
                    resp = await client.post(f"{base_url}/api/audits")
                    status = resp.status_code
                except Exception:  # noqa: BLE001 - 连接失败计入错误
                    status = 0
                sample.add(status, (time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        await asyncio.gather(*[one() for _ in range(total)])
        wall = time.perf_counter() - started
    return sample, wall


@dataclass
class Row:
    group: str
    concurrency: int
    total: int
    wall_s: float
    qps: float
    p50_ms: float
    p95_ms: float
    errors: int
    statuses: dict[int, int]
    max_probe_ms: float = 0.0
    probe_count: int = 0


def probe_health_during_audit(base_url: str) -> float:
    """发一个审计请求的同时，每 100ms 打一次 `/api/health`，返回其最大耗时。

    这是「事件循环有没有被阻塞」最直接的证据：
    - async：health 一直秒回，说明审计在等网络时让出了循环；
    - 同步阻塞写在 async 端点里：health 会被压到和审计同样长。
    """
    latencies: list[float] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            try:
                resp = httpx.get(f"{base_url}/api/health", timeout=120.0)
                latencies.append(resp.elapsed.total_seconds() * 1000)
            except Exception:  # noqa: BLE001
                latencies.append(120_000.0)
            time.sleep(0.1)

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    try:
        httpx.post(f"{base_url}/api/audits", timeout=300.0)
    finally:
        stop.set()
        thread.join(timeout=130)
    return max(latencies) if latencies else 0.0


def make_row(group: str, concurrency: int, total: int, sample: Sample, wall: float) -> Row:
    return Row(
        group=group,
        concurrency=concurrency,
        total=total,
        wall_s=wall,
        qps=total / wall if wall > 0 else 0.0,
        p50_ms=percentile(sample.latencies_ms, 50),
        p95_ms=percentile(sample.latencies_ms, 95),
        errors=sample.errors,
        statuses=sample.statuses,
    )


# ------------------------------------------------------------------ 报告


def render_markdown(rows: list[Row], meta: dict[str, Any]) -> str:
    lines = [
        "# 并发压测结果",
        "",
        f"> 生成时间：{meta['generated_at']}　本机：{meta['platform']}　Python {meta['python']}",
        f"> 假模型端点固定延迟 **{meta['latency_ms']} ms / 次调用**，"
        f"单次审计 {LLM_CALLS_PER_AUDIT} 次调用，N={meta['n']}",
        "",
        "## 方法",
        "",
        "- 模型侧：本地 `ThreadingHTTPServer` 提供 OpenAI 兼容 `/v1/chat/completions`，"
        "固定 sleep 后返回固定 JSON。零费用、延迟可控，能复现「一次模型调用几百毫秒」的时序。",
        "- 服务侧：真实 `uvicorn` 子进程，仅靠环境变量切换实现。",
        "  - `async` = `LLM_ASYNC=1`：LangGraph 异步节点 + `httpx.AsyncClient` 连接池；",
        "  - `sync` = `LLM_ASYNC=0`：同步 `httpx.Client`，由 `asyncio.to_thread` 线程池承载；",
        "  - `blocking` = `scripts/bench_blocking.py`：复刻改造前写法，"
        "async 端点里直接调用同步 `httpx.Client`，**会独占事件循环**。",
        "- 客户端：`httpx.AsyncClient` + `Semaphore(concurrency)`，同时发出 N 个 `POST /api/audits`。",
        "- 为单独衡量实现差异，压测进程把 `MAX_CONCURRENT_LLM=64`、排队超时放到 60s，"
        "不让限流器参与；限流器本身的行为由 pytest 覆盖。",
        "",
        "## 结果",
        "",
        "| 实现 | 并发 | N | 总耗时 (s) | QPS | P50 (ms) | P95 (ms) | 错误 | 状态码 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        statuses = ", ".join(
            (f"连接失败×{v}" if k == 0 else f"{k}×{v}") for k, v in sorted(row.statuses.items())
        )
        lines.append(
            f"| {row.group} | {row.concurrency} | {row.total} | {row.wall_s:.2f} | "
            f"{row.qps:.2f} | {row.p50_ms:.0f} | {row.p95_ms:.0f} | {row.errors} | {statuses} |"
        )
    lines.append("")
    lines.append("### 事件循环占用（health 探测）")
    lines.append("")
    lines.append("审计进行中每 100ms 打一次 `GET /api/health`，记录其最大耗时。"
                 "它不会被 LLM 调用拖慢——除非事件循环被占住。")
    lines.append("")
    lines.append("| 实现 | 审计期间 health 最大耗时 |")
    lines.append("| --- | ---: |")
    seen: set[str] = set()
    for row in rows:
        if row.group in seen or row.probe_count == 0:
            continue
        seen.add(row.group)
        lines.append(f"| {row.group} | {row.max_probe_ms:.0f} ms |")
    lines.append("")
    lines.append(
        "> 备注：`sync` 组沿用改造前的 provider 实现——**每次 LLM 调用都新建一个 `httpx.Client`**，"
        "在本机 Windows 上单次构造约 350ms，5 次调用就多出约 1.8s。"
        "这是「同步实现」本身的成本，不是被 async 拉开的差距；"
        "纯粹衡量「阻塞事件循环」的对照组是 `blocking`（它复用单个 Client，单请求约 1.4s）。"
    )
    lines.append("")
    return "\n".join(lines)


def render_summary(rows: list[Row]) -> str:
    """终端里打印一张精简表 + 关键结论。"""
    out = [
        f"{'实现':<10}{'并发':>5}{'总耗时(s)':>12}{'QPS':>9}{'P50(ms)':>10}{'P95(ms)':>10}{'错误':>6}",
    ]
    for row in rows:
        out.append(
            f"{row.group:<10}{row.concurrency:>5}{row.wall_s:>12.2f}{row.qps:>9.2f}"
            f"{row.p50_ms:>10.0f}{row.p95_ms:>10.0f}{row.errors:>6}"
        )
    by_group: dict[str, list[Row]] = {}
    for row in rows:
        by_group.setdefault(row.group, []).append(row)
    out.append("")
    for group, group_rows in by_group.items():
        ordered = sorted(group_rows, key=lambda r: r.concurrency)
        first, last = ordered[0], ordered[-1]
        probe = ordered[0]
        out.append(
            f"- {group}: 并发 {first.concurrency} → {last.concurrency}，"
            f"QPS {first.qps:.2f} → {last.qps:.2f}（×{last.qps / first.qps if first.qps else 0:.1f}），"
            f"P95 {first.p95_ms:.0f}ms → {last.p95_ms:.0f}ms，"
            f"审计期间 health 最大 {probe.max_probe_ms:.0f}ms"
        )
    return "\n".join(out)


# ------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="config-check-py 并发压测")
    parser.add_argument("--n", type=int, default=50, help="每个并发档位的请求数，默认 50")
    parser.add_argument("--concurrency", default="1,4,8", help="并发档位，逗号分隔，默认 1,4,8")
    parser.add_argument("--latency-ms", type=int, default=200, help="假模型单次调用延迟，默认 200ms")
    parser.add_argument(
        "--groups",
        default="async,sync,blocking",
        help="要跑的组：async,sync,blocking（逗号分隔）",
    )
    parser.add_argument("--out", default=str(RESULT_PATH), help="结果文件路径")
    args = parser.parse_args()

    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]

    fake_server, fake_port = start_fake_llm(args.latency_ms)
    fake_base = f"http://127.0.0.1:{fake_port}/v1"
    rows: list[Row] = []

    common_env = {
        "LLM_MODE": "real",
        # 假 key：只用于让客户端认为凭据存在，绝不打印
        "OPENAI_API_KEY": "bench-fake-key",
        "OPENAI_BASE_URL": fake_base,
        "MODEL_NAME": "bench-model",
        "LLM_TOKEN_BUDGET": "100000",
        "LLM_READ_TIMEOUT_SECONDS": "120",
    }

    specs = {
        "async": ("app.main:app", {**common_env, "LLM_ASYNC": "1"}),
        "sync": ("app.main:app", {**common_env, "LLM_ASYNC": "0"}),
        "blocking": ("scripts.bench_blocking:app", {**common_env, "BENCH_LATENCY_MS": str(args.latency_ms)}),
    }

    try:
        for group in groups:
            if group not in specs:
                raise SystemExit(f"未知分组: {group}")
            module, env_extra = specs[group]
            print(f"\n== 启动 {group} 应用 ({module}) ==", flush=True)
            app = launch_app(module, group, env_extra)
            try:
                # 预热一次，排除首次导入 / 建表 / 建连接的影响
                httpx.post(f"{app.base_url}/api/audits", timeout=300.0)
                probe_max = probe_health_during_audit(app.base_url)
                print(f"  health 探测：审计期间 health 最大耗时 = {probe_max:.0f}ms", flush=True)
                for concurrency in levels:
                    sample, wall = asyncio.run(run_load(app.base_url, concurrency, args.n))
                    row = make_row(group, concurrency, args.n, sample, wall)
                    row.max_probe_ms = probe_max
                    row.probe_count = 1
                    rows.append(row)
                    print(
                        f"  c={concurrency:<3} wall={row.wall_s:6.2f}s qps={row.qps:6.2f} "
                        f"p50={row.p50_ms:6.0f}ms p95={row.p95_ms:6.0f}ms errors={row.errors}",
                        flush=True,
                    )
            finally:
                stop_app(app)
    finally:
        fake_server.shutdown()

    print("\n" + render_summary(rows))
    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform": "Windows 11 / 本机",
        "python": sys.version.split()[0],
        "latency_ms": args.latency_ms,
        "n": args.n,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(rows, meta), encoding="utf-8")
    # 机器可读结果，方便以后回归对比
    out_path.with_suffix(".json").write_text(
        json.dumps({"meta": meta, "rows": [row.__dict__ for row in rows]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n结果写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
