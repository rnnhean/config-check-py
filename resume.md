# 简历 bullet · config-check-py

> 数字来自本机实测（2026-11-03 / 11-04，Python 3.12.10，148 个真实产品配置）。
> 这是 member-pulse 的 Python 重写版，数据同一份、结论同一批。
> mock 与真实模型（qwen-plus）两种模式都跑过，检出结论完全一致。

## 项目表述

**产品配置检查 Agent**（个人项目 · Python / FastAPI / LangGraph / pytest）

- 用 **LangGraph `StateGraph`** 把「载入 → 规则扫描 → 影响面 → 定级 → 修复建议 → 汇总」六个节点编排成固定拓扑的可审查流水线，节点间状态显式传递；对 **148 个真实产品配置、8 个终端编码**做数据治理检查，输出 `findings=4 / distinctAffected=27 / affectedTotal=31`，去重逻辑与 TS 版一致。

- **数值与文本严格分离**：8 条规则、影响面统计、`可靠性 × 影响面` 定级全部由确定性代码完成，模型拿到的已是 `severity` 字段，只能解释、不能改判；模型超时 / 失败 / 超 token 预算时整段降级为确定性文案，审计结论不变——实测 `LLM_TOKEN_BUDGET=50` 时 `calls=0, budgetExhausted=true, degraded=true`，findings 仍为 4 / 27。**接入真实模型（qwen-plus）后实测 `findings=4 / distinctAffected=27 / affectedTotal=31`，与 mock 完全一致**，`factCheckNotes=null` 表示摘要中没有出现数据之外的外部系统名，未触发幻觉拦截。

- **LLM 调用异步化 + 服务端并发保护**：`httpx.AsyncClient` 连接池（应用级复用，连接 5s / 读取 120s 分级超时）、只对连接错误 / 5xx / 429 重试（最多 2 次、指数退避、429 尊重 `Retry-After` 且等待上限 30s）、LangGraph 6 个节点改为 async 并走 `ainvoke`；`MAX_CONCURRENT_LLM` 默认 4 的 FIFO 排队闸门，排队超时返回 429 + `Retry-After`。本地假端点（单次调用 200ms）压测 N=50：**并发 1→8 时 QPS 0.97→6.80（×7.0），P50 1025→1062ms，0 错误**；同场景复刻改造前的同步阻塞写法 QPS 恒为 0.62~0.66、P95 1797→12766ms、并发 8 时 2 个连接失败。审计期间每 100ms 探测 `GET /api/health`，异步实现最大 22ms，阻塞写法被压到 1696ms——事件循环是否被占住有直接证据。`LLM_ASYNC=0` 可一键回退同步实现。

- 工程化交付：每个请求带 `trace_id`、逐节点耗时与结构化 JSON 日志、`GET /api/metrics`（含 `llm_inflight` / `llm_queue_depth` / `llm_timeouts_total` / `llm_retries_total` / P50/P95）；**59 个 pytest 用例通过**（规则单测、mock 端到端、重跑一致性、重试耗尽降级、持久化、失败节点留痕、异步超时/重试/降级、限流排队与 429、并发下 trace_id 不串）；Docker 镜像构建成功（67.7MB），容器内实跑 health + POST 通过；`.venv` 53MB、49 个包，无 torch / transformers / sentence-transformers / onnxruntime。

## 面试会被问到的点

| 问题 | 怎么答 |
| --- | --- |
| 「为什么用 LangGraph 而不是自己写 if-else？」 | 六个节点的拓扑是固定的、可审查的，LangGraph 给的是显式 state 传递和节点级可观测。不是为了用框架而用——如果用不上 checkpoint / 分支，纯函数组合更简单。要主动说出这个取舍。 |
| 「这里 Agent 在哪？不就是个规则引擎？」 | 诚实回答：**Agent 成分很低，主体是确定性规则引擎**。模型只负责把判据和后果写成中文。这个取舍是刻意的——数据治理场景里模型编造的代价比规则漏检更高。如果面试官要的是「模型自主决策」，这个项目不对口。 |
| 「为什么 R8 等级和 TS 版不一样？」 | TS 版硬编码 medium 绕过了公式，Python 版严格走公式得到 low（可靠性 0.5 < 0.6）。我选择保住「严重度 = 公式输出」这条不变量，把差异写进 README 而不是偷偷对齐。 |
| 「同步 POST 能上生产吗？」 | 单看单请求还是请求-响应式，真实模型实测 10.4 秒，其中几乎全是模型等待（8026ms + 2351ms），规则节点总共不到 3ms；要彻底解决得后台任务 + 轮询/SSE。但**阻塞事件循环的问题已经解决**：LLM 调用走 `httpx.AsyncClient`，LangGraph 节点全 async，实测并发 1→8 时 QPS 0.97→6.80；同时用 `MAX_CONCURRENT_LLM=4` 的排队闸门保护下游配额，超时返回 429 + `Retry-After`。 |
| 「异步了就够了吗？模型配额怎么办？」 | 不够。异步只解决「事件循环被占住」，不限制同时在跑的调用数。所以另加了 FIFO 排队闸门：默认 4 个并发的审计，超出的排队，排 10 秒还没进去就 429 让客户端退避。压测里 `blocking` 组并发 8 时 P95 12.8 秒、2 个连接失败，就是没有这层保护的后果。 |
