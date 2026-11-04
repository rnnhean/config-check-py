# config-check-py — 产品配置检查 Agent

一个把「产品目录数据治理」做成生产级 Agent 服务的 Python 实现：确定性规则检出问题，
LangGraph 编排六个节点，模型只负责把判据和后果讲清楚，不参与检出、定级和改数据。

数据是真实的：`data/products.json` 来自 `member-pulse` 项目的实际产品清单（148 个产品、
8 个终端编码），不是造出来的样例。

---

## 这是什么

会员/权益系统里有一张产品目录，每个产品有 `key`、中文名、`en_name`、支持的终端列表、
`ai_product` 标记。这张表同时被权益发放、订单归属、运营后台、AI 化率报表引用。字段一旦
不一致，下游不会报错，只会静默漏数。

本项目回答一个具体问题：**这张目录里现在有哪些配置缺陷、影响多少产品、严重程度如何、
该谁去修**。

它的核心不是「调模型写一段话」，而是：

- 8 条**确定性**规则，同一份数据永远得到同一批结论；
- 严重程度由 `可靠性 × 影响面` 公式算出，模型碰不到定级；
- 模型失败、超时、超预算时，全部降级为规则生成的确定性文案，审计结果不变；
- 每个请求有 `trace_id`，每个节点有耗时和状态，输出结构化 JSON 日志。

## 对应 JD 哪几条

| JD 条目 | 在本项目里的落点 |
|---|---|
| **第 2 条 · Agent 编排** | `app/pipeline.py` 用 LangGraph `StateGraph` 组装 6 个节点（载入 → 规则扫描 → 影响面 → 定级 → 修复建议 → 汇总），节点间状态显式传递，拓扑固定可审查 |
| **第 4 条 · 可观测与韧性** | `app/observability.py`：`trace_id`、逐节点耗时、结构化 JSON 日志、`GET /api/metrics`；`app/llm.py`：超时、最多 2 次重试、失败降级、token 预算上限 |
| **第 5 条 · 异步与并发** | `app/llm.py` 的 `AsyncOpenAICompatClient`（`httpx.AsyncClient` 连接池、分级超时、分类重试）、LangGraph 全异步节点、`app/concurrency.py` 限流排队、`scripts/bench_concurrency.py` 压测 |
| **第 6 条 · 工程化交付** | FastAPI 服务 + React/antd 审计台（`frontend/`，Vite 构建到 `web/`）+ 服务端 HTML 报告页、文件持久化、三段式 Dockerfile、pytest（规则单测 + mock 端到端 + 韧性 + 异步/并发 + 前端资源断言） |

（JD 第 7 条「真实业务数据」也顺带覆盖：数据不是手搓的，规则结论可以直接拿去对业务。）

## 架构

```
                    POST /api/audits
                          │
              ┌───────────▼───────────┐
              │  trace_id + 日志中间件  │
              └───────────┬───────────┘
                          │
   ┌──────────────────────▼───────────────────────┐
   │  并发闸门（令牌式 FIFO 队列，超时 429）        │
   └──────────────────────┬───────────────────────┘
                          │
   ┌──────────────────────▼───────────────────────┐
   │   LangGraph StateGraph（6 个 async 节点）     │
   │                                              │
   │  load_catalog  载入 148 个产品 + 终端字典      │
   │       ↓                                      │
   │  scan_rules    执行 R1–R8 确定性规则           │
   │       ↓                                      │
   │  quantify      命中数 / 占比 / 去重受影响产品   │
   │       ↓                                      │
   │  assess        可靠性 × 影响面 → 严重度        │
   │       ↓                                      │
   │  draft_fixes   LLM 解释 + 修复建议（可降级）    │
   │       ↓                                      │
   │  compile       汇总成报告 JSON                 │
   └──────────────────────┬───────────────────────┘
                          │
         ┌────────────────┼─────────────────┐
         ▼                ▼                 ▼
   data/audits/*.json  GET /api/metrics  GET /…/report (HTML)
```

### 模块

| 文件 | 职责 |
|---|---|
| `app/rules.py` | 8 条规则、影响面统计、严重度公式、数据载入。**纯函数，无 IO 依赖** |
| `app/pipeline.py` | LangGraph 图定义（全 async 节点）、节点计时、`run_audit()` / `run_audit_async()` |
| `app/llm.py` | 同步 + 异步 LLM provider、分级超时、分类重试、预算、降级 |
| `app/concurrency.py` | LLM 并发闸门：FIFO 排队、排队超时、队列深度指标 |
| `scripts/bench_concurrency.py` | 并发压测：async / sync / blocking 三组 × 并发 1/4/8 |
| `app/observability.py` | JSON 日志 formatter、`trace_id`、进程级指标聚合 |
| `app/store.py` | 审计结果落盘 + 内存索引，进程重启可恢复 |
| `app/report_html.py` | 服务端拼 HTML 报告页（`/api/audits/{id}/report`），前端只做外链 |
| `frontend/` | React 19 + TypeScript + Vite + antd 6 审计台：历史列表、概览、问题清单、节点链路、运行指标 |
| `app/main.py` | FastAPI 路由、中间件、健康检查 |

## 8 条规则

| 规则 | 判据 | 可靠性 | 本次命中 |
|---|---|---|---|
| R1 | `en_name` 为空或纯空白 | 1.0 | 6 |
| R2 | `en_name` 不符合 `^[A-Za-z0-9_-]+$` | 1.0 | 4 |
| R3 | 多个产品 `en_name` 相同 | 1.0 | 0 |
| R4 | 多个产品中文名相同 | 1.0 | 0 |
| R5 | `client_list` 含字典外编码 | 1.0 | 0 |
| R6 | `client_list` 为空 | 1.0 | 0 |
| R7 | 名称含 AI 语义标记但 `ai_product=0` | 0.6 | 19 |
| R8 | 字典里的终端零覆盖 | 0.5 | 2 |

严重度公式（与需求一致）：

```
reliability >= 0.9 且 影响面 >= 5%  → critical
reliability >= 0.9 且 影响面 >  0   → high
reliability >= 0.6                  → medium
否则                                 → low
```

影响面分母：R1–R7 是产品总数（148）；R8 是终端字典大小（8），因为它影响的是「终端」而非「产品」。

### 与 TS 实现的差异

TS 版 `catalog-rules.ts` 把 R8 的严重度**硬编码为 `medium`**，绕过了公式。本实现严格
套用公式，`reliability=0.5 < 0.6` 所以 R8 判为 **low**。这样「严重度 = 公式输出」这条
不变量成立，不会出现「明明可靠性最低却是中等」的口径矛盾。如果业务确实认为零覆盖终端
更严重，应该调 R8 的可靠性系数，而不是在代码里写死等级。

除此之外规则判据、字符集、AI 关键词、分母选择均与 TS 版一致。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/audits` | 跑一次检查，同步返回完整结果（201）；并发满载排队超时返回 429 + `Retry-After` |
| `GET` | `/api/audits?limit=50` | 历史运行摘要列表 |
| `GET` | `/api/audits/{id}` | 单次运行完整结果（含节点 traces） |
| `GET` | `/api/audits/{id}/report` | HTML 报告页 |
| `GET` | `/api/health` | 健康检查：数据可读、模式、产品数 |
| `GET` | `/api/metrics` | 调用次数、失败数、token、成本、各节点耗时，以及 `llm_inflight` / `llm_queue_depth` / `llm_timeouts_total` / `llm_retries_total` / `p50/p95` |
| `GET` | `/` | 审计台首页：返回 `web/index.html`（前端未构建时回退服务端渲染列表） |
| `GET` | `/static/*` | 前端构建产物（`web/`，Vite base 为 `/static/`） |

`POST /api/audits` 响应关键字段：

```json
{
  "id": "run_...", "traceId": "tr_...", "status": "done",
  "traces": [{"name": "load_catalog", "status": "done", "durationMs": 2.26}],
  "report": {
    "findingCount": 4, "distinctAffected": 27, "affectedTotal": 31,
    "severityBreakdown": {"critical": 0, "high": 2, "medium": 1, "low": 1},
    "findings": [{"ruleId": "R1", "severity": "high", "affected": 6, ...}]
  },
  "llm": {"calls": 5, "tokens": 639, "costUsd": 0.000154, "degraded": false}
}
```

## 怎么跑

```bash
cd D:/20260916/tmp/resume-projects/config-check-py

# 1. 环境
uv venv --python "C:/Users/v-renhan/AppData/Local/Programs/Python/Python312/python.exe" .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt

# 2. 测试
.venv/Scripts/python.exe -m pytest -q

# 3. 启动（默认 mock，完全离线）
LLM_MODE=mock .venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 5203

# 4. 跑一次
curl -X POST http://127.0.0.1:5203/api/audits
```

浏览器打开 `http://127.0.0.1:5203/` 可以点按钮跑检查并看 HTML 报告。

### 前端开发

前端是 React 19 + TypeScript + Vite + antd 6，源码在 `frontend/`，唯一产物落到 `web/`。

```bash
cd frontend
pnpm install
pnpm dev      # http://127.0.0.1:5603，/api 代理到 127.0.0.1:5203
pnpm build    # 构建产物输出到 ../web（Vite base 为 /static/）
```

- 开发时先起后端（`uvicorn` 监听 5203），前端 5603 只做页面与代理。
- `pnpm build` 会先跑 `tsc -b` 做类型检查，再输出到 `web/`。
- **改完前端要让后端重启**：`web/` 在应用 import 时决定挂载内容，不重启会继续读到旧白名单或旧 index.html。
- `web/` 是纯构建产物，不要手写文件进去；旧的手写 `theme.css` / `app.css` / `app.js` 已删除。

### Docker

```bash
docker build -t config-check-py:local .
docker run --rm -p 5203:5203 config-check-py:local
```

镜像分三段：`node:22-slim` 构建前端 → `python:3.12-slim` 装依赖到 `/opt/deps` →
runtime 只带应用代码、`frontend` 阶段的 `web/` 产物和产品数据。

## 环境变量

全部从进程环境读取，**不读取任何 `.env` 文件**。

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_MODE` | `mock` | `mock` 强制离线；其他值且存在 `OPENAI_API_KEY` 才走真实模型 |
| `OPENAI_API_KEY` | 空 | 存在即启用真实模式（配合 `LLM_MODE` 非 mock）。**永不打印** |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容端点 |
| `MODEL_NAME` | `gpt-4o-mini` | 模型名 |
| `LLM_TOKEN_BUDGET` | `4000` | 单次运行 token 预算，超限停止调用并降级 |
| `LLM_TIMEOUT_SECONDS` | `20` | 同步实现单次调用超时 |
| `LLM_ASYNC` | `1` | `1` 走异步客户端；`0` 回退同步实现（行为与改造前一致） |
| `LLM_CONNECT_TIMEOUT_SECONDS` | `5` | 异步客户端连接超时 |
| `LLM_READ_TIMEOUT_SECONDS` | `120` | 异步客户端读取超时 |
| `MAX_CONCURRENT_LLM` | `4` | 同时在跑的审计数上限，超限排队 |
| `LLM_QUEUE_TIMEOUT_SECONDS` | `10` | 排队上限，超时返回 429 |
| `LLM_PRICE_IN_PER_M` / `LLM_PRICE_OUT_PER_M` | `0.15` / `0.60` | 成本估算单价（USD / 百万 token） |
| `PRODUCTS_PATH` | `data/products.json` | 数据文件路径 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 测试与实测结果

`pytest`：**60 passed**（规则单测 + 端到端 mock + 韧性 + 异步/并发 + 前端资源）。

覆盖的关键断言：

- 4 条 findings：R1=6、R2=4、R7=19、R8=2
- 去重后 27 个受影响产品（R7 与 R1/R2 有交集，直接相加会虚高到 31）
- 重跑结果完全一致（`report` 逐字段相等，只有 `id` / `traceId` 变化）
- `LLM_TOKEN_BUDGET=1` → `calls=0`、`budgetExhausted=true`、findings 不变
- 重试：前 2 次抛 `TimeoutError`、第 3 次成功 → `attempts=3`、`failures=0`
- 重试耗尽 → 3 次尝试后降级为确定性文案
- `LLM_MODE=mock` 即使有 `OPENAI_API_KEY` 也强制 mock
- API 全链路：health / POST / list / detail / report / metrics / 404
- 持久化：落盘后可被新实例重新载入
- 数据文件缺失 → `status=failed`，且失败的 `load_catalog` 节点仍出现在 traces 中
- 异步客户端：5xx/连接错误重试后成功、429 遵守 `Retry-After`、4xx 不重试、超时计入 `timeoutsTotal`
- 异步运行时：重试耗尽降级为确定性文案且不抛异常；预算耗尽 `calls=0`
- 限流：`MAX_CONCURRENT_LLM=1` 时并发 3 个请求 → 1 个 201 + 2 个 429（带 `Retry-After`）；排队深度指标同步变化
- 并发 8 个请求的 `x-trace-id` 与 body `traceId` 一一对应、互不串号
- `LLM_ASYNC=0` 同步回退路径结果与异步一致

真实 mock 运行（`POST /api/audits` 实测输出）：

```
status: done | findings: 4 | distinctAffected: 27 | affectedTotal: 31
severity: {"critical":0,"high":2,"medium":1,"low":1}
  R1 high   affected=6  share=0.041 reliability=1   缺少 en_name
  R2 high   affected=4  share=0.027 reliability=1   en_name 含非法字符
  R7 medium affected=19 share=0.128 reliability=0.6 ai_product 标记与名称不一致
  R8 low    affected=2  share=0.25  reliability=0.5 字典中存在但没有产品使用的终端
llm: calls=5, tokens=639, costUsd=0.000154, degraded=false
```

重跑对比：`ids differ: true | report identical: true`。

### 真实模型运行（qwen-plus）

`LLM_MODE=real` + 真实 key，同一份 148 产品数据：

```
status=done | durationMs=10391
findings=4 | distinctAffected=27 | affectedTotal=31
severity={"critical":0,"high":2,"medium":1,"low":1}
llm={"calls":5,"attempts":5,"failures":0,"promptTokens":993,"completionTokens":324,
     "tokens":1317,"costUsd":0.000343,"degraded":false,"budgetExhausted":false}
factCheckNotes=null
```

摘要（模型写的）：

> 产品目录共148个条目，其中27个条目存在4类质量问题，累计影响31处数据实例。最突出的问题是"缺少en_name"，为高频触发规则。在4个问题中，2个属高风险级别，将直接阻塞跨系统对接…

**检出数、去重数、严重度分布与 mock 完全一致**——这是这套设计的核心主张：
数值由确定性规则算，换不换模型、模型有没有幻觉，结论都不变。模型只改变了措辞。
`factCheckNotes=null` 说明这次没触发幻觉拦截（摘要里没有出现数据中不存在的专有名词）。

耗时分布：规则类节点 0.01–0.91ms，两次模型调用分别 8026ms / 2351ms——**10.4 秒里 10.4 秒几乎全花在等模型**，
也印证了「同步 POST 接真实模型必须改异步」这条限制。

## 异步与并发

> 这一节解决的是改造前最硬的一条限制：**async 端点里跑同步 `httpx.Client`，一个请求等模型 10 秒，
> 后 19 个请求连读请求体都排不上**，QPS 恒等于 1/单请求耗时。

### 改造前到底卡在哪

FastAPI 的 `async def` 端点跑在事件循环线程上。改造前 `app/llm.py` 的 `complete()` 用的是同步
`httpx.Client`，一次审计串行发 5 次调用（`draft_fixes` 4 条 + `compile` 1 条），全部在事件循环里
阻塞等待。以一个请求 10 秒为例：

| 时刻 | 事件循环在做什么 | 其它 19 个请求 |
|---|---|---|
| 0–10s | 请求 1 阻塞在第 1~5 次模型调用上 | 全部排队，**连 TCP 都已接但请求没被处理** |
| 10–20s | 请求 2 才开始跑 | 剩下 18 个继续排 |

结果：并发数加多少都没用，吞吐恒为 `1 / 单请求耗时`；客户端开始超时，但服务端「看起来还很闲」
（CPU 空转、没有报错）。这不是慢，是把并发能力整个丢掉了。

### 改造后的三层

1. **异步客户端** `AsyncOpenAICompatClient`（`app/llm.py`）
   - 一个 `httpx.AsyncClient` 连接池，生命周期跟应用一致（`AsyncRuntimeFactory` 持有，
     FastAPI `lifespan` 关闭时 `aclose()`），**不是每次请求新建**；
   - 超时分级：连接 5s、读取 120s、写入 10s、取池 5s；
   - 重试只对**连接错误 / 读取超时 / 5xx / 429**，最多 2 次、指数退避（0.5s 起、上限 8s）；
     429 优先读 `Retry-After`，单次等待硬上限 30s；4xx（除 429）不重试；
   - 日志只记 `status / durationMs / attempt`，**不记 key、请求头、响应体**；
   - 重试耗尽后仍走原有降级：返回确定性文案，审计结论与 `status=done` 都不变。
2. **LangGraph 全异步节点**（`app/pipeline.py`）
   - 6 个节点都改成 `async def`，含 LLM 调用的两个节点 `await`；
   - `run_audit_async()` 走 `graph.ainvoke()`——同步节点的图 `ainvoke` 会报
     "Either initialize with a synchronous function or invoke via the async API"，
     所以节点必须真异步，否则只是把阻塞藏起来；
   - 保留 `run_audit()` 同步入口（内部 `asyncio.run`），pytest 与脚本不用改。
3. **服务端并发保护**（`app/concurrency.py` + `app/main.py`）
   - `MAX_CONCURRENT_LLM`（默认 4）限制同时在跑的审计，超限**排队而不是直接拒绝**；
   - 排队超过 `LLM_QUEUE_TIMEOUT_SECONDS`（默认 10s）返回 **429 + `Retry-After`**；
   - 用显式 FIFO 队列而不是 `Semaphore + wait_for`，避免超时取消时「令牌已发出但调用方已放弃」
     造成槽位泄漏（pytest 里有对应用例）。

### 怎么切回同步

`LLM_ASYNC=0` 走同步 `LlmRuntime`，节点里用 `asyncio.to_thread` 执行，行为与改造前一致
（同样的 provider、同样的重试/降级、同样的结果），只是并发能力退回线程数上限。
两边共用 `run_audit_async` 与同一套按钮/指标，方便 A/B 对照。

### 指标

`GET /api/metrics` 的 `llm` 下新增（同时提供 snake_case 顶层字段）：

| 字段 | 含义 |
|---|---|
| `inflight` / `llm_inflight` | 正在跑的模型调用数 |
| `queueDepth` / `llm_queue_depth` | 等待并发槽的审计数 |
| `timeoutsTotal` / `llm_timeouts_total` | 累计超时次数 |
| `retriesTotal` / `llm_retries_total` | 累计重试次数 |
| `p50Ms` / `p95Ms` | 最近 2000 次模型调用的延迟分位（nearest-rank） |

### 压测

`scripts/bench_concurrency.py`：本地 `ThreadingHTTPServer` 起一个 OpenAI 兼容假端点
（固定延迟、零费用），真实 uvicorn 子进程分别以三种实现启动，N=50 个 `POST /api/audits`，
并发 1/4/8。三组：

- `async`：`LLM_ASYNC=1`
- `sync`：`LLM_ASYNC=0`（同步 `httpx.Client` + 线程池）
- `blocking`：`scripts/bench_blocking.py`，复刻改造前写法（async 端点里直接同步调用）

```bash
.venv/Scripts/python.exe scripts/bench_concurrency.py --n 50 --latency-ms 200
```

本机实测（2026-11-04，单次模型调用 200ms，单次审计 5 次调用）：

| 实现 | 并发 | 总耗时 (s) | QPS | P50 (ms) | P95 (ms) | 错误 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| async | 1 | 51.29 | 0.97 | 1025 | 1032 | 0 |
| async | 4 | 13.61 | 3.67 | 1040 | 1111 | 0 |
| async | 8 | 7.36 | 6.80 | 1062 | 1081 | 0 |
| sync | 1 | 123.53 | 0.40 | 2344 | 3460 | 0 |
| sync | 4 | 72.37 | 0.69 | 5492 | 6950 | 0 |
| sync | 8 | 58.56 | 0.85 | 8821 | 9517 | 0 |
| blocking | 1 | 81.06 | 0.62 | 1594 | 1797 | 0 |
| blocking | 4 | 79.56 | 0.63 | 6307 | 6715 | 0 |
| blocking | 8 | 75.88 | 0.66 | 12661 | 12766 | 2 |

事件循环占用（审计进行中每 100ms 探测一次 `GET /api/health`，记录最大耗时）：

| 实现 | 审计期间 health 最大耗时 |
| --- | ---: |
| async | 22 ms |
| sync | 28 ms |
| blocking | 1696 ms |

读法：

- **async 并发 1→8，QPS 0.97→6.80（×7.0），总耗时 51.3s→7.4s**；P50 从 1025ms 只涨到 1062ms，
  说明排队的是请求、不是事件循环。
- **blocking 并发 1→8，QPS 始终在 0.62~0.66**（≈ 1 / 单请求 1.6s，加并发完全无效），
  P95 从 1797ms 涨到 12766ms，并发 8 时 2 个连接失败；探测的 health 也被压到 1696ms，
  与「一个请求独占事件循环」完全吻合。
- `sync` 组并发加线程后 QPS 能从 0.40 升到 0.85，但 P95 从 3460ms 涨到 9517ms：线程池确实
  绕开了事件循环阻塞，代价是排队与上下文切换。它比 `blocking` 还慢，原因是**沿用改造前的
  provider 写法**：每次 LLM 调用都新建 `httpx.Client`，本机单次构造约 350~420ms，5 次调用
  多出约 1.8s。这是同步实现自身的开销（也正是「复用连接池」这条改动的价值），不是 async
  带来的差距；它的 health 探测仍是 28ms，因为 `asyncio.to_thread` 把阻塞移出了事件循环。

> 压测用的是假端点，数字只衡量服务端调度能力，不代表真实模型耗时。

## 已知限制

1. **`POST /api/audits` 仍是请求-响应式**。LLM 调用已经异步化、事件循环不再被阻塞，
   但单次审计还是要等模型跑完才返回（真实模型秒级）。要彻底解决长请求，还需要
   后台任务 + 轮询/SSE（TS 版用的就是 SSE）；本项目的并发保护只解决「同时来很多请求」，
   不解决「单个请求很久」。
2. **审计结果存 JSON 文件**，没有数据库。进程重启能恢复历史，但不支持多实例共享，
   并发写同一 id 的情况不会发生（每次 run_id 唯一），但规模上去后要换存储。
3. **token 估算是 4 字符 ≈ 1 token 的近似值**，mock 模式下没有真实 usage，
   成本数字只用于演示口径，不是账单。
4. **R7 的 AI 关键词是启发式**（`AI` / 纳米 / 智绘 / 智能），可靠性 0.6 且标记为
   `needsHumanReview`，必须业务确认，不能自动改标。
5. **R8 等级与 TS 版不同**（low vs medium），原因见上文「与 TS 实现的差异」。
6. **Docker 镜像**已是多阶段构建（builder 装依赖到 `/opt/deps`，runtime 只带运行产物），
   构建在慢网络下曾超时一次，复用层缓存后成功。
7. **没有鉴权**。这是演示服务，部署到公网前必须加认证；限流目前只做了 LLM 并发闸门
   （保护下游模型配额），不是按 IP/租户的入口限流。
8. **限流是单进程内存态**。多 worker / 多实例部署时每个进程各有一份计数，
   真实上限是 `MAX_CONCURRENT_LLM × worker 数`；要全局限额需要把队列挪到 Redis 之类的共享存储。

## 数据来源

`data/products.json` 复制自 `member-pulse/data/products.json`（原文件未修改）。
字段：`source`、`extractedAt`、`total`、`clientDict`（8 个终端编码）、`products`（148 条）。
