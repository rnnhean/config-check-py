"""规则单测：用最小人造数据固定每条规则的判据，避免被真实数据的变化掩盖。"""

from __future__ import annotations

import pytest

from app import rules


def make_dataset(products: list[dict], client_dict: dict[str, str] | None = None) -> rules.ProductDataset:
    return rules.ProductDataset(
        source="synthetic",
        extracted_at="2026-01-01T00:00:00Z",
        client_dict=client_dict or {"1": "PC", "2": "iOS"},
        products=[
            rules.RawProduct(
                key=p.get("key", f"k{i}"),
                value=p.get("value", f"产品{i}"),
                en_name=p.get("en_name", f"product_{i}"),
                client_list=p.get("client_list", ["1"]),
                ai_product=p.get("ai_product", 0),
            )
            for i, p in enumerate(products)
        ],
    )


def hits_of(dataset: rules.ProductDataset) -> dict[str, list[str]]:
    return {a.rule.id: [h.product.key for h in a.hits] for a in rules.run_rules(dataset)}


def test_r1_missing_en_name() -> None:
    ds = make_dataset([{"key": "a", "en_name": ""}, {"key": "b", "en_name": "   "}, {"key": "c"}])
    assert hits_of(ds)["R1"] == ["a", "b"]


def test_r2_illegal_en_name_chars() -> None:
    ds = make_dataset(
        [
            {"key": "ok1", "en_name": "360fanyi"},
            {"key": "ok2", "en_name": "a_b-c"},
            {"key": "bad1", "en_name": "a b"},
            {"key": "bad2", "en_name": "a.b"},
            {"key": "bad3", "en_name": "中文名"},
        ]
    )
    assert hits_of(ds)["R2"] == ["bad1", "bad2", "bad3"]


def test_r2_ignores_empty_en_name_covered_by_r1() -> None:
    ds = make_dataset([{"key": "a", "en_name": ""}])
    assert hits_of(ds)["R2"] == []


def test_r3_duplicate_en_name() -> None:
    ds = make_dataset(
        [
            {"key": "a", "en_name": "same"},
            {"key": "b", "en_name": "same"},
            {"key": "c", "en_name": "unique"},
        ]
    )
    assert hits_of(ds)["R3"] == ["a", "b"]


def test_r3_blank_en_names_are_not_duplicates() -> None:
    ds = make_dataset([{"key": "a", "en_name": ""}, {"key": "b", "en_name": " "}])
    assert hits_of(ds)["R3"] == []


def test_r4_duplicate_chinese_name() -> None:
    ds = make_dataset(
        [
            {"key": "a", "value": "同名"},
            {"key": "b", "value": "同名"},
            {"key": "c", "value": "唯一"},
        ]
    )
    assert hits_of(ds)["R4"] == ["a", "b"]


def test_r5_unknown_terminal_code() -> None:
    ds = make_dataset(
        [{"key": "a", "client_list": ["1", "9"]}, {"key": "b", "client_list": ["2"]}],
        client_dict={"1": "PC", "2": "iOS"},
    )
    assert hits_of(ds)["R5"] == ["a"]


def test_r6_empty_client_list() -> None:
    ds = make_dataset([{"key": "a", "client_list": []}, {"key": "b", "client_list": ["1"]}])
    assert hits_of(ds)["R6"] == ["a"]


@pytest.mark.parametrize(
    ("name", "en_name", "ai_product", "expected"),
    [
        ("智能助手", "assistant", 0, True),
        ("纳米搜索", "nano_search", 0, True),
        ("智绘设计", "zhihui", 0, True),
        ("AI学习", "ai_study", 0, True),
        ("普通产品", "normal", 0, False),
        ("智能助手", "assistant", 1, False),  # 已标记则不命中
        ("MAIL服务", "mail", 0, False),  # 大写 AI 必须是独立词
        ("AIX工具", "aix", 0, False),
    ],
)
def test_r7_ai_marker(name: str, en_name: str, ai_product: int, expected: bool) -> None:
    ds = make_dataset([{"key": "p", "value": name, "en_name": en_name, "ai_product": ai_product}])
    hit = "p" in hits_of(ds)["R7"]
    assert hit is expected


def test_r8_unused_terminals_use_dictionary_denominator() -> None:
    ds = make_dataset(
        [{"key": "a", "client_list": ["1"]}],
        client_dict={"1": "PC", "2": "iOS", "3": "Harmony", "4": "pad"},
    )
    findings = rules.build_findings(ds, rules.run_rules(ds), rules.find_unused_terminals(ds))
    r8 = next(f for f in findings if f.rule_id == "R8")
    assert r8.scope == "terminal"
    assert r8.affected == 3
    # 分母是终端字典 4 个，不是产品数 1 个
    assert r8.affected_share == 0.75
    # reliability=0.5 < 0.6，按统一公式应为 low
    assert r8.severity == "low"


@pytest.mark.parametrize(
    ("reliability", "share", "expected"),
    [
        (1.0, 0.05, "critical"),
        (1.0, 0.2, "critical"),
        (0.9, 0.01, "high"),
        (1.0, 0.0, "medium"),  # 可靠性>=0.6 兜底为 medium
        (0.6, 0.5, "medium"),
        (0.5, 0.5, "low"),
    ],
)
def test_severity_matrix(reliability: float, share: float, expected: str) -> None:
    assert rules.severity_of(reliability, share) == expected


def test_findings_exclude_rules_without_hits() -> None:
    ds = make_dataset(
        [{"key": "a", "en_name": "ok", "value": "产品", "client_list": ["1", "2"]}],
        client_dict={"1": "PC", "2": "iOS"},
    )
    findings = rules.build_findings(ds, rules.run_rules(ds), rules.find_unused_terminals(ds))
    assert findings == []


def test_findings_sorted_by_severity_then_affected() -> None:
    ds = make_dataset(
        [
            {"key": "a", "en_name": ""},
            {"key": "b", "value": "智能助手"},
            {"key": "c", "value": "智能助手"},
        ]
    )
    findings = rules.build_findings(ds, rules.run_rules(ds), rules.find_unused_terminals(ds))
    order = [rules.SEVERITY_ORDER[f.severity] for f in findings]
    assert order == sorted(order)
