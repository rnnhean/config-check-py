"""LangGraph StateGraph 编排：6 个节点完成一次配置审计。

节点：
1. load_catalog  载入产品目录并建立索引
2. scan_rules    逐条执行 8 条确定性规则
3. quantify      统计影响面（命中数、占比、去重产品）
4. assess        按「可靠性 × 影响面」定级
5. draft_fixes   调用模型生成解释与修复建议（失败/超预算降级）
6. compile       汇总成可交付报告

模型只在 draft_fixes / compile 参与，且只负责表达；检出、统计、定级全部由确定性代码完成。
每个节点自己做计时，产出 NodeTrace，run_audit 再统一写结构化日志。
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from . import rules
from .llm import AsyncLlmRuntime, LlmRuntime
from .observability import METRICS, NodeTrace, log_event, new_run_id, new_trace_id

NODE_TITLES = {
    "load_catalog": "载入产品目录",
    "scan_rules": "逐条执行检查规则",
    "quantify": "量化影响面",
    "assess": "判定严重程度",
    "draft_fixes": "生成解释与修复建议",
    "compile": "汇总报告",
}


class AuditState(TypedDict, total=False):
    trace_id: str
    run_id: str
    data_path: str
    dataset: rules.ProductDataset
    aggregates: list[rules.RuleAggregate]
    unused_terminals: list[str]
    findings: list[rules.Finding]
    distinct_affected: int
    affected_total: int
    severity_breakdown: dict[str, int]
    report: dict[str, Any]
    traces: list[NodeTrace]
    runtime: LlmRuntime
    error: str


@dataclass
class AuditResult:
    id: str
    trace_id: str
    status: str
    created_at: str
    duration_ms: float
    dataset: dict[str, Any]
    traces: list[dict[str, Any]]
    report: dict[str, Any] | None
    llm: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "traceId": self.trace_id,
            "status": self.status,
            "createdAt": self.created_at,
            "durationMs": round(self.duration_ms, 2),
            "dataset": self.dataset,
            "traces": self.traces,
            "report": self.report,
            "llm": self.llm,
            "error": self.error,
        }

    def summary(self) -> dict[str, Any]:
        report = self.report or {}
        return {
            "id": self.id,
            "traceId": self.trace_id,
            "status": self.status,
            "createdAt": self.created_at,
            "durationMs": round(self.duration_ms, 2),
            "total": self.dataset.get("total", 0),
            "digest": self.dataset.get("digest", ""),
            "findingCount": report.get("findingCount", 0),
            "distinctAffected": report.get("distinctAffected", 0),
            "headline": report.get("headline", ""),
        }


def _iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _record_failure(state: AuditState, name: str, duration_ms: float, exc: Exception) -> None:
    runtime = state.get("runtime")
    if runtime is not None:
        runtime.failed_traces.append(
            NodeTrace(
                name=name,
                status="failed",
                duration_ms=duration_ms,
                error=type(exc).__name__,
            )
        )
    METRICS.record_node(name, duration_ms)
    log_event(
        "node_failed",
        traceId=state.get("trace_id"),
        node=name,
        durationMs=round(duration_ms, 2),
        error=type(exc).__name__,
    )


def _finish(state: AuditState, name: str, duration_ms: float, trace: NodeTrace, updates: dict[str, Any]) -> dict[str, Any]:
    trace.duration_ms = duration_ms
    trace.status = "done"
    METRICS.record_node(name, duration_ms)
    log_event(
        "node_done",
        traceId=state.get("trace_id"),
        node=name,
        durationMs=round(duration_ms, 2),
        status="done",
    )
    updates["traces"] = state.get("traces", []) + [trace]
    return updates


async def _timed_async(
    state: AuditState,
    name: str,
    fn: Callable[[], Any],
) -> dict[str, Any]:
    """异步节点计时包装。

    `fn` 可以是普通函数（纯 CPU 节点，毫秒级）或协程函数（含 LLM 调用的节点）。
    失败语义：异常写进 runtime.failed_traces 后继续抛，由 LangGraph 中止整张图。
    """
    t0 = time.perf_counter()
    try:
        result = fn()
        if inspect.isawaitable(result):
            result = await result
        updates, trace = result
    except Exception as exc:
        duration_ms = (time.perf_counter() - t0) * 1000
        _record_failure(state, name, duration_ms, exc)
        raise
    _finish(state, name, (time.perf_counter() - t0) * 1000, trace, updates)
    return updates


async def _complete(runtime: Any, kind: str, facts: dict[str, Any]) -> str:
    """统一取模型文案的入口。

    - AsyncLlmRuntime（LLM_ASYNC=1）：直接 await，事件循环不阻塞
    - LlmRuntime（LLM_ASYNC=0，同步 fallback）：丢到线程池执行，避免卡住事件循环
    """
    if isinstance(runtime, AsyncLlmRuntime):
        return await runtime.complete(kind, facts)
    return await asyncio.to_thread(runtime.complete, kind, facts)


def build_graph(data_path: Path, runtime: Any) -> Any:
    """构造并编译 LangGraph StateGraph（全异步节点）。

    线程中执行同步 runtime 时用 `asyncio.to_thread`，因此同步 fallback 也不会
    在 FastAPI 的事件循环里阻塞。
    """

    async def load_catalog(state: AuditState) -> dict[str, Any]:
        def work() -> tuple[dict[str, Any], NodeTrace]:
            dataset = rules.load_dataset(Path(state["data_path"]))
            keys = {p.key for p in dataset.products}
            return (
                {"dataset": dataset},
                NodeTrace(
                    name="load_catalog",
                    detail={
                        "source": dataset.source,
                        "total": dataset.total,
                        "uniqueKeys": len(keys),
                        "duplicateKeys": dataset.total - len(keys),
                        "terminals": len(dataset.client_dict),
                    },
                ),
            )

        return await _timed_async(state, "load_catalog", work)

    async def scan_rules(state: AuditState) -> dict[str, Any]:
        def work() -> tuple[dict[str, Any], NodeTrace]:
            dataset = state["dataset"]
            aggregates = rules.run_rules(dataset)
            unused = rules.find_unused_terminals(dataset)
            return (
                {"aggregates": aggregates, "unused_terminals": unused},
                NodeTrace(
                    name="scan_rules",
                    detail={
                        "ruleCount": len(aggregates),
                        "hits": {a.rule.id: len(a.hits) for a in aggregates},
                        "unusedTerminals": unused,
                    },
                ),
            )

        return await _timed_async(state, "scan_rules", work)

    async def quantify(state: AuditState) -> dict[str, Any]:
        def work() -> tuple[dict[str, Any], NodeTrace]:
            dataset = state["dataset"]
            findings = rules.build_findings(
                dataset, state["aggregates"], state.get("unused_terminals")
            )
            distinct = rules.distinct_affected(findings)
            affected_total = sum(f.affected for f in findings)
            return (
                {
                    "findings": findings,
                    "distinct_affected": distinct,
                    "affected_total": affected_total,
                },
                NodeTrace(
                    name="quantify",
                    detail={
                        "total": dataset.total,
                        "affectedTotal": affected_total,
                        "distinctAffected": distinct,
                        "findings": [
                            {
                                "ruleId": f.rule_id,
                                "affected": f.affected,
                                "affectedShare": f.affected_share,
                            }
                            for f in findings
                        ],
                    },
                ),
            )

        return await _timed_async(state, "quantify", work)

    async def assess(state: AuditState) -> dict[str, Any]:
        def work() -> tuple[dict[str, Any], NodeTrace]:
            breakdown = rules.severity_breakdown(state["findings"])
            # 定级在 build_findings 内由公式完成，本节点只固化为可核对的结果
            return (
                {"severity_breakdown": breakdown},
                NodeTrace(
                    name="assess",
                    detail={
                        "formula": (
                            "reliability>=0.9 and share>=5% -> critical; "
                            ">=0.9 and share>0 -> high; >=0.6 -> medium; else low"
                        ),
                        "breakdown": breakdown,
                    },
                ),
            )

        return await _timed_async(state, "assess", work)

    async def draft_fixes(state: AuditState) -> dict[str, Any]:
        async def work() -> tuple[dict[str, Any], NodeTrace]:
            findings = state["findings"]
            rt = state["runtime"]
            for f in findings:
                f.explanation = await _complete(
                    rt,
                    "product_finding",
                    {
                        "kind": "product_finding",
                        "ruleLabel": f.rule_label,
                        "criterion": f.criterion,
                        "consequence": f.consequence,
                        "affected": f.affected,
                        "affectedShare": f.affected_share,
                        "scope": f.scope,
                        "reliability": f.reliability,
                        "severity": f.severity,
                        "severityLabel": rules.SEVERITY_LABEL[f.severity],
                    },
                )
                if f.needs_human_review:
                    f.explanation = f"{f.explanation}（待业务确认：{f.review_question}）"
            review = [f for f in findings if f.needs_human_review]
            return (
                {},
                NodeTrace(
                    name="draft_fixes",
                    detail={
                        "pendingReview": len(review),
                        "reviewRules": [f.rule_id for f in review],
                        "llmCalls": rt.stats.calls,
                        "degraded": rt.stats.degraded,
                        "budgetExhausted": rt.stats.budget_exhausted,
                    },
                ),
            )

        return await _timed_async(state, "draft_fixes", work)

    async def compile_report(state: AuditState) -> dict[str, Any]:
        async def work() -> tuple[dict[str, Any], NodeTrace]:
            dataset = state["dataset"]
            findings = state["findings"]
            rt = state["runtime"]
            breakdown = state["severity_breakdown"]
            distinct = state["distinct_affected"]
            affected_total = state["affected_total"]
            top = findings[0] if findings else None

            executive_summary = await _complete(
                rt,
                "product_report",
                {
                    "kind": "product_report",
                    "total": dataset.total,
                    "findingCount": len(findings),
                    "distinctAffected": distinct,
                    "affectedTotal": affected_total,
                    "topRuleLabel": top.rule_label if top else "-",
                    "breakdown": breakdown,
                },
            )

            if top:
                headline = (
                    f"{dataset.total} 个产品中检出 {len(findings)} 类配置问题，"
                    f"最严重的是「{top.rule_label}」（{top.affected} 个"
                    f"{'终端编码受影响' if top.scope == 'terminal' else '产品受影响'}）"
                )
            else:
                headline = f"{dataset.total} 个产品未检出配置问题"

            report = {
                "headline": headline,
                "executiveSummary": executive_summary,
                "method": (
                    "问题由确定性规则检出：逐产品执行 8 条判据 → 统计影响面 → "
                    "按「可靠性 × 影响面」定级；模型只负责把判据与后果讲清楚，"
                    "不参与检出、不做定级、不修改数据。"
                ),
                "generatedAt": _iso(),
                "total": dataset.total,
                "findingCount": len(findings),
                "affectedTotal": affected_total,
                "distinctAffected": distinct,
                "severityBreakdown": breakdown,
                "findings": [f.to_dict() for f in findings],
                "dataset": {
                    "source": dataset.source,
                    "digest": dataset.digest(),
                    "extractedAt": dataset.extracted_at,
                },
            }
            return (
                {"report": report},
                NodeTrace(
                    name="compile",
                    detail={
                        "findingCount": len(findings),
                        "distinctAffected": distinct,
                        "llmTokens": rt.stats.tokens,
                        "budgetExhausted": rt.stats.budget_exhausted,
                    },
                ),
            )

        return await _timed_async(state, "compile", work)

    graph = StateGraph(AuditState)
    graph.add_node("load_catalog", load_catalog)
    graph.add_node("scan_rules", scan_rules)
    graph.add_node("quantify", quantify)
    graph.add_node("assess", assess)
    graph.add_node("draft_fixes", draft_fixes)
    graph.add_node("compile", compile_report)
    graph.add_edge(START, "load_catalog")
    graph.add_edge("load_catalog", "scan_rules")
    graph.add_edge("scan_rules", "quantify")
    graph.add_edge("quantify", "assess")
    graph.add_edge("assess", "draft_fixes")
    graph.add_edge("draft_fixes", "compile")
    graph.add_edge("compile", END)
    return graph.compile()


def _run_sync(coro: Awaitable[AuditResult]) -> AuditResult:
    """在同步上下文里安全地跑协程。

    图节点是 async 的，LangGraph 要求走 `ainvoke`；但 `run_audit` 是同步 API
    （pytest、脚本、以及 LLM_ASYNC=0 的调用方都直接用）。如果当前线程已有
    事件循环（例如有人在 async 端点里误调同步函数），不能 `asyncio.run`，
    改在独立线程里跑一个自己的循环，避免 "loop already running"。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]

    outcome: dict[str, Any] = {}

    def runner() -> None:
        try:
            outcome["result"] = asyncio.run(coro)  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - 原样转交给调用线程
            outcome["error"] = exc

    thread = threading.Thread(target=runner, name="run-audit", daemon=True)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def run_audit(
    data_path: Path, runtime: LlmRuntime, graph: Any | None = None, trace_id: str | None = None
) -> AuditResult:
    """执行一次审计（同步入口）。"""
    return _run_sync(run_audit_async(data_path, runtime, graph, trace_id))


def _build_result(
    *,
    trace_id: str,
    run_id: str,
    created_at: str,
    started: float,
    runtime: Any,
    status: str,
    error: str | None,
    final: AuditState,
) -> AuditResult:
    """把图执行的最终 state 归一化成可持久化结果（同步/异步共用）。"""
    duration_ms = (time.perf_counter() - started) * 1000
    traces = list(final.get("traces", [])) + list(getattr(runtime, "failed_traces", []))
    METRICS.record_llm(runtime)

    dataset = final.get("dataset")
    report = final.get("report")
    result = AuditResult(
        id=run_id,
        trace_id=trace_id,
        status=status,
        created_at=created_at,
        duration_ms=duration_ms,
        dataset=(
            {
                "source": dataset.source,
                "digest": dataset.digest(),
                "extractedAt": dataset.extracted_at,
                "total": dataset.total,
            }
            if dataset
            else {"total": 0}
        ),
        traces=[t.to_dict() for t in traces],
        report=report,
        llm=runtime.stats.to_dict(runtime.cost_usd),
        error=error,
    )
    log_event(
        "audit_finished",
        traceId=trace_id,
        runId=run_id,
        status=status,
        durationMs=round(duration_ms, 2),
        findingCount=(report or {}).get("findingCount", 0),
        distinctAffected=(report or {}).get("distinctAffected", 0),
        llmCalls=runtime.stats.calls,
        llmFailures=runtime.stats.failures,
        tokens=runtime.stats.tokens,
        degraded=runtime.stats.degraded,
    )
    return result


async def run_audit_async(
    data_path: Path, runtime: Any, graph: Any | None = None, trace_id: str | None = None
) -> AuditResult:
    """异步执行一次审计。图节点全部是 async，走 `ainvoke` 才真正释放事件循环。

    `trace_id` 可由调用方传入（HTTP 层用中间件生成的 id），保证响应头与
    body.traceId 是同一个值；不传则自行生成。
    """
    trace_id = trace_id or new_trace_id()
    run_id = new_run_id()
    started = time.perf_counter()
    created_at = _iso()
    compiled = graph or build_graph(data_path, runtime)

    initial: AuditState = {
        "trace_id": trace_id,
        "run_id": run_id,
        "data_path": str(data_path),
        "runtime": runtime,
        "traces": [],
    }

    status = "done"
    error: str | None = None
    final: AuditState = initial
    try:
        final = await compiled.ainvoke(initial)
    except Exception as exc:  # noqa: BLE001 - 审计失败应落库为 failed 而非 500
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        log_event("audit_failed", traceId=trace_id, runId=run_id, error=type(exc).__name__)

    return _build_result(
        trace_id=trace_id,
        run_id=run_id,
        created_at=created_at,
        started=started,
        runtime=runtime,
        status=status,
        error=error,
        final=final,
    )
