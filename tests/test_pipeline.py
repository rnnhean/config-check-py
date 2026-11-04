"""端到端（mock）与韧性测试。

断言的目标数字来自对 data/products.json 的真实复算：
R1=6、R2=4、R7=19、R8=2 → 4 条 findings，去重后 27 个产品。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.llm import LlmProvider, LlmRuntime, MockProvider, build_runtime
from app.main import create_app
from app.pipeline import build_graph, run_audit
from app.store import AuditStore


def mock_runtime(budget: int = 4000) -> LlmRuntime:
    return LlmRuntime(provider=MockProvider(), model="mock-deterministic", budget_tokens=budget)


def test_end_to_end_mock_findings(data_path: Path) -> None:
    result = run_audit(data_path, mock_runtime())
    assert result.status == "done"
    report = result.report
    assert report is not None

    # 4 条 findings，按「严重度 → 命中数」排序
    assert report["findingCount"] == 4
    assert [f["ruleId"] for f in report["findings"]] == ["R1", "R2", "R7", "R8"]
    assert [f["severity"] for f in report["findings"]] == ["high", "high", "medium", "low"]
    by_rule = {f["ruleId"]: f for f in report["findings"]}
    assert by_rule["R1"]["affected"] == 6
    assert by_rule["R2"]["affected"] == 4
    assert by_rule["R7"]["affected"] == 19
    assert by_rule["R8"]["affected"] == 2

    # 去重后 27 个受影响产品（R8 是终端维度，不计入）
    assert report["distinctAffected"] == 27
    assert report["affectedTotal"] == 31
    assert report["total"] == 148

    # 六个节点全部执行成功
    assert [t["name"] for t in result.traces] == [
        "load_catalog",
        "scan_rules",
        "quantify",
        "assess",
        "draft_fixes",
        "compile",
    ]
    assert all(t["status"] == "done" for t in result.traces)
    assert all(t["durationMs"] >= 0 for t in result.traces)
    assert result.trace_id.startswith("tr_")


def test_rerun_is_deterministic(data_path: Path) -> None:
    first = run_audit(data_path, mock_runtime())
    second = run_audit(data_path, mock_runtime())

    # run_id / trace_id 每次都不同，但业务结果必须完全一致
    # （generatedAt 是真实时间戳，隔秒跑会不同，比较时排除）
    assert first.id != second.id
    first_report = {k: v for k, v in first.report.items() if k != "generatedAt"}
    second_report = {k: v for k, v in second.report.items() if k != "generatedAt"}
    assert first_report == second_report
    assert first.report["dataset"]["digest"] == second.report["dataset"]["digest"]


def test_budget_exhaustion_degrades_without_changing_findings(data_path: Path) -> None:
    runtime = mock_runtime(budget=1)
    result = run_audit(data_path, runtime)
    assert result.status == "done"
    # 预算超限后不再调用模型，全部使用确定性兜底文案
    assert result.llm["budgetExhausted"] is True
    assert result.llm["degraded"] is True
    assert result.llm["calls"] == 0
    assert result.llm["tokens"] == 0
    # 检出与定级不依赖模型，因此结果不变
    assert result.report["findingCount"] == 4
    assert result.report["distinctAffected"] == 27
    assert "命中" in result.report["executiveSummary"]


class FlakyProvider(LlmProvider):
    """前 N 次失败，之后成功的假 provider，用于验证重试。"""

    name = "flaky"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0

    def complete(self, system: str, user: str, timeout: float) -> tuple[str, int, int]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise TimeoutError("simulated timeout")
        return "模型恢复后输出", 10, 5


def test_retry_then_success() -> None:
    provider = FlakyProvider(failures=2)
    runtime = LlmRuntime(provider=provider, model="fake", budget_tokens=10_000, max_retries=2)
    text = runtime.complete("product_finding", {"kind": "product_finding", "ruleLabel": "R1"})
    assert text == "模型恢复后输出"
    assert runtime.stats.attempts == 3
    assert runtime.stats.failures == 0


def test_retry_exhausted_falls_back() -> None:
    provider = FlakyProvider(failures=99)
    runtime = LlmRuntime(provider=provider, model="fake", budget_tokens=10_000, max_retries=2)
    facts = {
        "kind": "product_finding",
        "ruleLabel": "R1",
        "consequence": "下游无法关联",
        "affected": 6,
        "affectedShare": 0.041,
        "scope": "product",
    }
    text = runtime.complete("product_finding", facts)
    # 3 次尝试（首次 + 2 次重试）后降级为确定性文案
    assert runtime.stats.attempts == 3
    assert runtime.stats.failures == 1
    assert runtime.stats.degraded is True
    assert "6" in text and "R1" in text


def test_build_runtime_defaults_to_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runtime = build_runtime()
    assert runtime.provider.name == "mock"


def test_build_runtime_forces_mock_when_mode_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
    runtime = build_runtime()
    assert runtime.provider.name == "mock"


def api_client(tmp_path: Path, data_path: Path, runtime_factory) -> TestClient:
    store = AuditStore(tmp_path / "audits")
    app = create_app(data_path=data_path, store=store, runtime_factory=runtime_factory)
    return TestClient(app)


def test_api_endpoints(tmp_path: Path, data_path: Path) -> None:
    client = api_client(tmp_path, data_path, mock_runtime)
    # 指标是进程级累计值（新增异步/并发用例也会写），用增量断言
    baseline = client.get("/api/metrics").json()

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["llmMode"] == "mock"
    assert health.json()["products"] == 148

    created = client.post("/api/audits")
    assert created.status_code == 201
    payload = created.json()
    assert payload["report"]["findingCount"] == 4
    assert payload["report"]["distinctAffected"] == 27
    audit_id = payload["id"]

    listed = client.get("/api/audits")
    assert listed.status_code == 200
    assert len(listed.json()["audits"]) == 1

    detail = client.get(f"/api/audits/{audit_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == audit_id

    report = client.get(f"/api/audits/{audit_id}/report")
    assert report.status_code == 200
    assert "产品配置检查报告" in report.text
    assert "R1" in report.text

    metrics = client.get("/api/metrics")
    body = metrics.json()
    assert metrics.status_code == 200
    assert body["audits"]["total"] - baseline["audits"]["total"] == 1
    assert body["audits"]["failed"] - baseline["audits"]["failed"] == 0
    assert set(body["nodes"]) == {
        "load_catalog",
        "scan_rules",
        "quantify",
        "assess",
        "draft_fixes",
        "compile",
    }
    assert "costUsd" in body["llm"]

    index = client.get("/")
    assert index.status_code == 200
    assert "Config Check" in index.text

    missing = client.get("/api/audits/nope")
    assert missing.status_code == 404


def test_frontend_assets_are_served(tmp_path: Path, data_path: Path) -> None:
    """前端构建产物（Vite）由 FastAPI 托管：index.html + 其引用的 hash 资源。"""
    client = api_client(tmp_path, data_path, mock_runtime)

    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    # 页面标题来自 frontend/index.html，构建后原样落到 web/index.html
    assert "<title>Config Check · 产品配置审计台</title>" in index.text

    # Vite 产物：index.html 引用的 hash 资源必须能取到
    assets = re.findall(r'/static/assets/[^"\']+', index.text)
    assert assets, "index.html 里没有任何 /static/assets 引用"
    for asset in assets:
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert response.content, asset


def test_metrics_accumulate_across_runs(tmp_path: Path, data_path: Path) -> None:
    client = api_client(tmp_path, data_path, mock_runtime)
    baseline = client.get("/api/metrics").json()
    client.post("/api/audits")
    client.post("/api/audits")
    body = client.get("/api/metrics").json()
    # 指标是进程级累计值，用增量断言，避免受其他用例影响
    assert body["audits"]["total"] - baseline["audits"]["total"] == 2
    assert body["nodes"]["load_catalog"]["count"] - baseline["nodes"]["load_catalog"]["count"] == 2
    assert body["nodes"]["compile"]["avgMs"] >= 0


def test_report_persisted_to_disk(tmp_path: Path, data_path: Path) -> None:
    store = AuditStore(tmp_path / "audits")
    result = run_audit(data_path, mock_runtime(), build_graph(data_path, mock_runtime()))
    store.save(result)
    saved = json.loads((tmp_path / "audits" / f"{result.id}.json").read_text(encoding="utf-8"))
    assert saved["report"]["findingCount"] == 4
    # 新实例能从磁盘恢复，模拟进程重启
    reloaded = AuditStore(tmp_path / "audits")
    assert reloaded.get(result.id) is not None
    assert reloaded.list()[0]["distinctAffected"] == 27


def test_failed_node_is_recorded_in_traces(tmp_path: Path, data_path: Path) -> None:
    """数据文件缺失时，失败节点也必须留下 trace，而不是只抛出异常。"""
    missing = tmp_path / "missing.json"
    result = run_audit(missing, mock_runtime())
    assert result.status == "failed"
    assert result.error is not None
    assert len(result.traces) == 1
    assert result.traces[0]["name"] == "load_catalog"
    assert result.traces[0]["status"] == "failed"
