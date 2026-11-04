"""产品配置检查规则（与 TS 版 catalog-rules.ts 保持一致的确定性实现）。

设计原则：
1. 缺陷由确定性规则检出，同一份数据永远得到同一批缺陷。
2. 每条规则说明判据与后果，判据可被业务推翻，后果可被核对。
3. 严重程度由「可靠性 × 影响面」计算，模型只负责解释，不参与定级。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

EN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
AI_PATTERN = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])|纳米|智绘|智能")

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_LABEL = {"critical": "高危", "high": "较高", "medium": "中等", "low": "较低"}


@dataclass
class RawProduct:
    key: str
    value: str
    en_name: str
    client_list: list[str]
    ai_product: int


@dataclass
class ProductDataset:
    source: str
    extracted_at: str
    client_dict: dict[str, str]
    products: list[RawProduct]

    @property
    def total(self) -> int:
        return len(self.products)

    def digest(self) -> str:
        payload = json.dumps(
            [
                {
                    "key": p.key,
                    "value": p.value,
                    "ai_product": p.ai_product,
                    "en_name": p.en_name,
                    "client_list": p.client_list,
                }
                for p in self.products
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class Rule:
    id: str
    label: str
    criterion: str
    consequence: str
    reliability: float
    needs_human_review: bool
    review_question: str
    # 命中后给出可执行的修复动作；规则本身无法替业务拍板的部分单独标注
    fix_action: str


RULES: list[Rule] = [
    Rule(
        id="R1",
        label="缺少 en_name",
        criterion="en_name 为空字符串或纯空白。en_name 是跨系统关联的英文主键。",
        consequence=(
            "下游按 en_name 关联时无法匹配到该产品，权益发放、订单归属、报表聚合都会漏掉它，且不会报错。"
        ),
        reliability=1.0,
        needs_human_review=False,
        review_question="",
        fix_action="按「产品线_形态」补全 en_name，允许数字开头，补完用 R2 的字符集复检。",
    ),
    Rule(
        id="R2",
        label="en_name 含非法字符",
        criterion="en_name 不符合 ^[A-Za-z0-9_-]+$（只允许字母、数字、下划线、连字符；允许数字开头）。",
        consequence=(
            "空格、点号、大小写混用在拼 URL 参数、作为数据库键或环境变量名时容易截断或转义出错，"
            "是跨系统对接的典型事故来源。"
        ),
        reliability=1.0,
        needs_human_review=False,
        review_question="",
        fix_action="替换空格与点号为下划线，统一小写；改动前先确认没有下游按旧值硬编码。",
    ),
    Rule(
        id="R3",
        label="en_name 重复",
        criterion="两个及以上产品的 en_name 完全相同。",
        consequence="en_name 若被当作唯一键使用，重复会导致更新覆盖或查询返回多条，属于静默的数据污染。",
        reliability=1.0,
        needs_human_review=False,
        review_question="",
        fix_action="保留一个主产品，其余加业务后缀区分；确认下游唯一键约束后再改。",
    ),
    Rule(
        id="R4",
        label="中文名重复",
        criterion="两个及以上产品使用完全相同的中文名 value。",
        consequence=(
            "运营在后台按名称搜索时会选错产品，导致配置到错误商品上；同一名称对应多个 key 时，权限与权益也可能挂错。"
        ),
        reliability=1.0,
        needs_human_review=False,
        review_question="",
        fix_action="给重复名称补充端或形态后缀（如「会员-鸿蒙端」），并在运营后台加 key 提示。",
    ),
    Rule(
        id="R5",
        label="声明了字典外的终端编码",
        criterion="client_list 中出现 option_type=18 字典里没有的编码。",
        consequence="下游按终端分流时该产品无法被任何端识别，等于上架了但用户看不到。",
        reliability=1.0,
        needs_human_review=False,
        review_question="",
        fix_action="把编码改成字典内的合法值；若确实是新终端，先补字典再挂产品。",
    ),
    Rule(
        id="R6",
        label="未声明任何终端",
        criterion="client_list 为空数组。",
        consequence="产品存在但没有任何端可见，通常是接入流程中断留下的半成品。",
        reliability=1.0,
        needs_human_review=False,
        review_question="",
        fix_action="确认该产品是否已上线；已上线则补 client_list，未上线则标记为草稿并移出生产目录。",
    ),
    Rule(
        id="R7",
        label="ai_product 标记与名称不一致",
        criterion="产品名或 en_name 含明确的 AI 语义标记（大写独立 AI、纳米、智绘、智能），但 ai_product=0。",
        consequence=(
            "按 ai_product 字段统计 AI 化率会偏低；若下游按该字段决定是否展示 AI 入口，这些产品会被漏掉。"
        ),
        reliability=0.6,
        needs_human_review=True,
        review_question=(
            "这些产品名称带 AI 字样但未打标，究竟是漏标，还是名称沿用历史品牌（如「纳米」作为产品线名）"
            "而实际并非 AI 能力？需要产品侧逐个确认，不能一概改标。"
        ),
        fix_action="逐个确认是否具备 AI 能力；确认后由产品负责人改 ai_product，不批量改标。",
    ),
    Rule(
        id="R8",
        label="字典中存在但没有产品使用的终端",
        criterion="option_type=18 字典里的终端编码，在全部产品的 client_list 中一次都没出现。",
        consequence=(
            "要么该端确实没有产品接入，要么接入时漏填了编码。这个差异会直接影响「我们覆盖了几个端」的判断。"
        ),
        reliability=0.5,
        needs_human_review=True,
        review_question=(
            "这些终端在字典里存在却零覆盖，是业务上确实没做，还是产品接入时漏填了 client_list？"
            "需要端侧负责人确认，不能直接判定为缺陷。"
        ),
        fix_action="找端侧负责人确认是业务未接入还是漏填；确认后再决定补产品还是下线字典项。",
    ),
]

RULES_BY_ID = {r.id: r for r in RULES}


@dataclass
class Hit:
    product: RawProduct
    detail: str


@dataclass
class RuleAggregate:
    rule: Rule
    hits: list[Hit] = field(default_factory=list)


@dataclass
class Finding:
    rule_id: str
    rule_label: str
    criterion: str
    consequence: str
    severity: str
    affected: int
    affected_share: float
    scope: str
    reliability: float
    samples: list[dict[str, Any]]
    needs_human_review: bool
    review_question: str
    explanation: str = ""
    fix_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "ruleLabel": self.rule_label,
            "criterion": self.criterion,
            "consequence": self.consequence,
            "severity": self.severity,
            "severityLabel": SEVERITY_LABEL[self.severity],
            "affected": self.affected,
            "affectedShare": self.affected_share,
            "scope": self.scope,
            "reliability": self.reliability,
            "samples": self.samples,
            "needsHumanReview": self.needs_human_review,
            "reviewQuestion": self.review_question,
            "explanation": self.explanation,
            "fixAction": self.fix_action,
        }


def load_dataset(path: Path) -> ProductDataset:
    if not path.exists():
        raise FileNotFoundError(f"data file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    products = raw.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError("products.json 必须包含非空产品数组")
    dict_raw = raw.get("clientDict") or {}
    if not isinstance(dict_raw, dict):
        raise ValueError("clientDict 必须是对象")
    parsed = [
        RawProduct(
            key=str(p.get("key", "")),
            value=str(p.get("value", "")),
            en_name=str(p.get("en_name", "") or ""),
            client_list=[str(c) for c in (p.get("client_list") or [])],
            ai_product=int(p.get("ai_product") or 0),
        )
        for p in products
    ]
    return ProductDataset(
        source=str(raw.get("source", "")),
        extracted_at=str(raw.get("extractedAt", "")),
        client_dict={str(k): str(v) for k, v in dict_raw.items()},
        products=parsed,
    )


def _pct(num: int, den: int) -> float:
    if den == 0:
        return 0.0
    return round(num / den, 3)


def _terminals(product: RawProduct, client_dict: dict[str, str]) -> list[str]:
    seen: list[str] = []
    for code in product.client_list:
        label = client_dict.get(code, f"未知({code})")
        if label not in seen:
            seen.append(label)
    return seen


def _en_name(products: Iterable[RawProduct], client_dict: dict[str, str]) -> dict[str, list[RawProduct]]:
    index: dict[str, list[RawProduct]] = {}
    for p in products:
        if p.en_name and p.en_name.strip():
            index.setdefault(p.en_name, []).append(p)
    return index


def run_rules(dataset: ProductDataset) -> list[RuleAggregate]:
    products = dataset.products
    dict_ = dataset.client_dict
    en_index = _en_name(products, dict_)
    name_index: dict[str, list[RawProduct]] = {}
    for p in products:
        name_index.setdefault(p.value, []).append(p)

    aggregates: list[RuleAggregate] = []
    for rule in RULES:
        agg = RuleAggregate(rule=rule)
        if rule.id == "R8":
            # R8 是终端维度的聚合检查，逐产品无意义
            aggregates.append(agg)
            continue
        for p in products:
            detail: str | None = None
            if rule.id == "R1":
                if not p.en_name.strip():
                    detail = "en_name 为空"
            elif rule.id == "R2":
                if p.en_name.strip() and not EN_NAME_PATTERN.match(p.en_name):
                    detail = f'en_name="{p.en_name}" 不符合命名规范'
            elif rule.id == "R3":
                if p.en_name.strip() and len(en_index.get(p.en_name, [])) > 1:
                    detail = f"en_name 被 {len(en_index[p.en_name])} 个产品共用"
            elif rule.id == "R4":
                if len(name_index.get(p.value, [])) > 1:
                    detail = f"中文名被 {len(name_index[p.value])} 个产品共用"
            elif rule.id == "R5":
                unknown = [c for c in p.client_list if c not in dict_]
                if unknown:
                    detail = f"未知终端编码 {','.join(unknown)}"
            elif rule.id == "R6":
                if len(p.client_list) == 0:
                    detail = "client_list 为空"
            elif rule.id == "R7":
                if p.ai_product != 1:
                    m = AI_PATTERN.search(f"{p.value} {p.en_name}")
                    if m:
                        detail = f"名称出现「{m.group(0)}」但 ai_product=0"
            else:  # pragma: no cover - 规则表与分支必须一一对应
                raise RuntimeError(f"unhandled rule {rule.id}")
            if detail:
                agg.hits.append(Hit(product=p, detail=detail))
        aggregates.append(agg)
    return aggregates


def find_unused_terminals(dataset: ProductDataset) -> list[str]:
    used = {c for p in dataset.products for c in p.client_list}
    return [code for code in dataset.client_dict if code not in used]


def severity_of(reliability: float, affected_share: float) -> str:
    if reliability >= 0.9 and affected_share >= 0.05:
        return "critical"
    if reliability >= 0.9 and affected_share > 0:
        return "high"
    if reliability >= 0.6:
        return "medium"
    return "low"


def build_findings(
    dataset: ProductDataset,
    aggregates: list[RuleAggregate],
    unused_terminals: list[str] | None = None,
) -> list[Finding]:
    total = dataset.total
    findings: list[Finding] = []

    for agg in aggregates:
        rule = agg.rule
        if rule.id == "R8" or not agg.hits:
            continue
        share = _pct(len(agg.hits), total)
        findings.append(
            Finding(
                rule_id=rule.id,
                rule_label=rule.label,
                criterion=rule.criterion,
                consequence=rule.consequence,
                severity=severity_of(rule.reliability, share),
                affected=len(agg.hits),
                affected_share=share,
                scope="product",
                reliability=rule.reliability,
                samples=[
                    {
                        "key": h.product.key,
                        "value": h.product.value,
                        "en_name": h.product.en_name,
                        "terminals": _terminals(h.product, dataset.client_dict),
                        "detail": h.detail,
                    }
                    for h in agg.hits
                ],
                needs_human_review=rule.needs_human_review,
                review_question=rule.review_question,
                fix_action=rule.fix_action,
            )
        )

    if unused_terminals is None:
        unused_terminals = find_unused_terminals(dataset)
    if unused_terminals:
        rule = RULES_BY_ID["R8"]
        terminal_total = len(dataset.client_dict)
        terminal_share = _pct(len(unused_terminals), terminal_total)
        findings.append(
            Finding(
                rule_id="R8",
                rule_label=rule.label,
                criterion=rule.criterion,
                consequence=rule.consequence,
                # 影响面分母是终端字典（不是产品目录），但定级仍走同一条公式：
                # reliability=0.5 < 0.6 → low。这与 TS 版把 R8 硬编码为 medium 不同，
                # 差异和理由写在 README「与 TS 实现的差异」。
                severity=severity_of(rule.reliability, terminal_share),
                affected=len(unused_terminals),
                affected_share=terminal_share,
                scope="terminal",
                reliability=rule.reliability,
                samples=[
                    {
                        "key": f"terminal:{code}",
                        "value": dataset.client_dict[code],
                        "en_name": "",
                        "terminals": [dataset.client_dict[code]],
                        "detail": f"字典编码 {code} 在全部产品中零覆盖",
                    }
                    for code in unused_terminals
                ],
                needs_human_review=rule.needs_human_review,
                review_question=rule.review_question,
                fix_action=rule.fix_action,
            )
        )

    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], -f.affected, f.rule_id))
    return findings


def distinct_affected(findings: list[Finding]) -> int:
    keys = {
        s["key"]
        for f in findings
        if f.scope == "product"
        for s in f.samples
    }
    return len(keys)


def severity_breakdown(findings: list[Finding]) -> dict[str, int]:
    breakdown = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        breakdown[f.severity] += 1
    return breakdown
