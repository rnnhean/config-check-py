"""极简 HTML 报告页：不引入任何前端构建，服务端直接拼字符串。

所有来自数据的文本都经过 html.escape，避免产品名里的字符破坏页面结构。
"""

from __future__ import annotations

from html import escape
from typing import Any

from .rules import SEVERITY_LABEL

SEVERITY_CLASS = {
    "critical": "sev-critical",
    "high": "sev-high",
    "medium": "sev-medium",
    "low": "sev-low",
}

PAGE_CSS = """
:root{--bg:#0f1419;--panel:#161c23;--line:#26303b;--fg:#e6edf3;--muted:#8b98a5;--accent:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:ui-sans-serif,system-ui,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.6}
main{max-width:960px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:24px;margin:0 0 8px}
h2{font-size:17px;margin:32px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.muted{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-top:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}
.card .num{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}
.card .label{font-size:12px;color:var(--muted)}
.finding{background:var(--panel);border:1px solid var(--line);border-left-width:4px;border-radius:8px;padding:16px;margin-bottom:14px}
.sev-critical{border-left-color:#f85149}.sev-high{border-left-color:#d29922}
.sev-medium{border-left-color:#58a6ff}.sev-low{border-left-color:#3fb950}
.tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted);margin-left:6px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500}
code{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#7ee787}
details{margin-top:10px}
summary{cursor:pointer;color:var(--accent);font-size:13px}
.empty{color:var(--muted);padding:32px;text-align:center;border:1px dashed var(--line);border-radius:8px}
form{display:inline}
button{background:var(--accent);color:#04121f;border:0;border-radius:6px;padding:8px 16px;font-size:14px;cursor:pointer;font-weight:600}
ul.audits{list-style:none;padding:0;margin:0}
ul.audits li{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:8px}
ul.audits a{color:var(--accent);text-decoration:none}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{escape(title)}</title><style>{PAGE_CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def render_index(audits: list[dict[str, Any]]) -> str:
    rows = []
    for a in audits:
        rows.append(
            "<li>"
            f"<a href=\"/api/audits/{escape(a['id'])}/report\">{escape(a['id'])}</a>"
            f"<span class=\"tag\">{escape(a.get('status', ''))}</span>"
            f"<div class=\"muted\">{escape(a.get('createdAt', ''))} · "
            f"{a.get('findingCount', 0)} 条问题 · 去重 {a.get('distinctAffected', 0)} 个产品 · "
            f"{a.get('durationMs', 0)} ms</div>"
            f"<div>{escape(a.get('headline', ''))}</div></li>"
        )
    body = [
        "<h1>产品配置检查</h1>",
        "<p class=\"muted\">确定性规则检出 + LangGraph 编排 + 模型只负责解释。</p>",
        "<form method=\"post\" action=\"/api/audits\">"
        "<button type=\"submit\">跑一次检查</button></form>",
        "<h2>历史运行</h2>",
    ]
    if rows:
        body.append(f"<ul class=\"audits\">{''.join(rows)}</ul>")
    else:
        body.append("<p class=\"empty\">还没有运行记录，点上面的按钮跑一次。</p>")
    return _page("产品配置检查", "".join(body))


def render_report(audit: dict[str, Any]) -> str:
    report = audit.get("report")
    if not report:
        return _page(
            "报告不可用",
            f"<h1>报告不可用</h1><p class=\"muted\">审计 {escape(audit.get('id', ''))} "
            f"状态为 {escape(audit.get('status', 'unknown'))}。</p>",
        )

    b = report.get("severityBreakdown", {})
    cards = "".join(
        f"<div class=\"card\"><div class=\"num\">{value}</div>"
        f"<div class=\"label\">{escape(label)}</div></div>"
        for label, value in [
            ("目录产品数", report.get("total", 0)),
            ("问题条目", report.get("findingCount", 0)),
            ("去重受影响产品", report.get("distinctAffected", 0)),
            ("高危", b.get("critical", 0)),
            ("较高", b.get("high", 0)),
            ("中等", b.get("medium", 0)),
        ]
    )

    findings_html: list[str] = []
    for f in report.get("findings", []):
        samples = "".join(
            "<tr>"
            f"<td><code>{escape(str(s.get('key', '')))}</code></td>"
            f"<td>{escape(str(s.get('value', '')))}</td>"
            f"<td>{escape(str(s.get('en_name', '')))}</td>"
            f"<td>{escape(str(s.get('detail', '')))}</td>"
            "</tr>"
            for s in f.get("samples", [])[:30]
        )
        review = (
            f"<p><strong>待业务确认：</strong>{escape(f.get('reviewQuestion', ''))}</p>"
            if f.get("needsHumanReview")
            else ""
        )
        findings_html.append(
            f"<div class=\"finding {SEVERITY_CLASS.get(f.get('severity'), '')}\">"
            f"<strong>{escape(f.get('ruleId', ''))} · {escape(f.get('ruleLabel', ''))}</strong>"
            f"<span class=\"tag\">{escape(SEVERITY_LABEL.get(f.get('severity'), f.get('severity', '')))}</span>"
            f"<div class=\"muted\">命中 {f.get('affected', 0)} 个"
            f"{'终端编码' if f.get('scope') == 'terminal' else '产品'} · "
            f"占比 {(f.get('affectedShare', 0) * 100):.1f}% · 可靠性 {f.get('reliability', 0)}</div>"
            f"<p><strong>判据：</strong>{escape(f.get('criterion', ''))}</p>"
            f"<p><strong>后果：</strong>{escape(f.get('consequence', ''))}</p>"
            f"<p><strong>建议：</strong>{escape(f.get('fixAction', ''))}</p>"
            f"<p><strong>说明：</strong>{escape(f.get('explanation', ''))}</p>"
            f"{review}"
            f"<details><summary>命中明细（{len(f.get('samples', []))} 条）</summary>"
            "<table><thead><tr><th>key</th><th>中文名</th><th>en_name</th><th>说明</th></tr></thead>"
            f"<tbody>{samples}</tbody></table></details></div>"
        )

    traces = "".join(
        f"<tr><td>{escape(t.get('name', ''))}</td><td>{escape(t.get('status', ''))}</td>"
        f"<td>{t.get('durationMs', 0)} ms</td></tr>"
        for t in audit.get("traces", [])
    )

    body = [
        f"<h1>{escape(report.get('headline', '产品配置检查报告'))}</h1>",
        f"<p class=\"muted\">运行 {escape(audit.get('id', ''))} · 追踪 {escape(audit.get('traceId', ''))} · "
        f"{escape(report.get('generatedAt', ''))} · 数据摘要 {escape(report.get('dataset', {}).get('digest', ''))}</p>",
        f"<div class=\"grid\">{cards}</div>",
        "<h2>结论摘要</h2>",
        f"<p>{escape(report.get('executiveSummary', ''))}</p>",
        f"<p class=\"muted\">{escape(report.get('method', ''))}</p>",
        "<h2>问题清单</h2>",
        "".join(findings_html) or "<p class=\"empty\">未检出配置问题。</p>",
        "<h2>节点耗时</h2>",
        "<table><thead><tr><th>节点</th><th>状态</th><th>耗时</th></tr></thead>"
        f"<tbody>{traces}</tbody></table>",
    ]
    return _page("产品配置检查报告", "".join(body))
