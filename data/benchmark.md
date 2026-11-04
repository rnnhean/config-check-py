# 并发压测结果

> 生成时间：2026-11-04T03:19:18Z　本机：Windows 11 / 本机　Python 3.12.10
> 假模型端点固定延迟 **200 ms / 次调用**，单次审计 5 次调用，N=50

## 方法

- 模型侧：本地 `ThreadingHTTPServer` 提供 OpenAI 兼容 `/v1/chat/completions`，固定 sleep 后返回固定 JSON。零费用、延迟可控，能复现「一次模型调用几百毫秒」的时序。
- 服务侧：真实 `uvicorn` 子进程，仅靠环境变量切换实现。
  - `async` = `LLM_ASYNC=1`：LangGraph 异步节点 + `httpx.AsyncClient` 连接池；
  - `sync` = `LLM_ASYNC=0`：同步 `httpx.Client`，由 `asyncio.to_thread` 线程池承载；
  - `blocking` = `scripts/bench_blocking.py`：复刻改造前写法，async 端点里直接调用同步 `httpx.Client`，**会独占事件循环**。
- 客户端：`httpx.AsyncClient` + `Semaphore(concurrency)`，同时发出 N 个 `POST /api/audits`。
- 为单独衡量实现差异，压测进程把 `MAX_CONCURRENT_LLM=64`、排队超时放到 60s，不让限流器参与；限流器本身的行为由 pytest 覆盖。

## 结果

| 实现 | 并发 | N | 总耗时 (s) | QPS | P50 (ms) | P95 (ms) | 错误 | 状态码 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| async | 1 | 50 | 51.29 | 0.97 | 1025 | 1032 | 0 | 201×50 |
| async | 4 | 50 | 13.61 | 3.67 | 1040 | 1111 | 0 | 201×50 |
| async | 8 | 50 | 7.36 | 6.80 | 1062 | 1081 | 0 | 201×50 |
| sync | 1 | 50 | 123.53 | 0.40 | 2344 | 3460 | 0 | 201×50 |
| sync | 4 | 50 | 72.37 | 0.69 | 5492 | 6950 | 0 | 201×50 |
| sync | 8 | 50 | 58.56 | 0.85 | 8821 | 9517 | 0 | 201×50 |
| blocking | 1 | 50 | 81.06 | 0.62 | 1594 | 1797 | 0 | 201×50 |
| blocking | 4 | 50 | 79.56 | 0.63 | 6307 | 6715 | 0 | 201×50 |
| blocking | 8 | 50 | 75.88 | 0.66 | 12661 | 12766 | 2 | 连接失败×2, 201×48 |

### 事件循环占用（health 探测）

审计进行中每 100ms 打一次 `GET /api/health`，记录其最大耗时。它不会被 LLM 调用拖慢——除非事件循环被占住。

| 实现 | 审计期间 health 最大耗时 |
| --- | ---: |
| async | 22 ms |
| sync | 28 ms |
| blocking | 1696 ms |

> 备注：`sync` 组沿用改造前的 provider 实现——**每次 LLM 调用都新建一个 `httpx.Client`**，在本机 Windows 上单次构造约 350ms，5 次调用就多出约 1.8s。这是「同步实现」本身的成本，不是被 async 拉开的差距；纯粹衡量「阻塞事件循环」的对照组是 `blocking`（它复用单个 Client，单请求约 1.4s）。
