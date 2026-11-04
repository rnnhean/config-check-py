import { Descriptions, Divider, Progress, Space, Tag, Typography } from 'antd'
import { SEVERITY_META, SEVERITY_ORDER, formatNumber, formatTime, sharePercent } from '../constants'
import type { AuditDetail, AuditReport, SeverityKey } from '../types'

interface Props {
  detail: AuditDetail
  report: AuditReport
}

export function OverviewTab({ detail, report }: Props) {
  const breakdown = report.severityBreakdown
  // 严重度块的分母用「问题类型数」，占比反映各类问题在整份报告里的权重
  const totalFindings = Math.max(report.findingCount, 1)

  return (
    <Space direction="vertical" size={18} style={{ width: '100%' }}>
      <div>
        <h2 className="finding-headline">{report.headline}</h2>
        <Typography.Paragraph style={{ marginTop: 8, marginBottom: 0, maxWidth: '80ch' }}>
          {report.executiveSummary || '（报告未包含摘要）'}
        </Typography.Paragraph>
      </div>

      <div className="severity-grid">
        {SEVERITY_ORDER.map((key) => (
          <SeverityBlock
            key={key}
            severity={key}
            count={breakdown?.[key] ?? 0}
            total={totalFindings}
          />
        ))}
      </div>

      <Descriptions
        size="small"
        column={{ xs: 1, sm: 2, md: 4 }}
        items={[
          { key: 'affectedTotal', label: '命中总次数', children: formatNumber(report.affectedTotal) },
          {
            key: 'distinctAffected',
            label: '去重受影响产品',
            children: formatNumber(report.distinctAffected),
          },
          { key: 'duration', label: '本次耗时', children: `${detail.durationMs.toFixed(2)} ms` },
          { key: 'generatedAt', label: '报告生成时间', children: formatTime(report.generatedAt) },
        ]}
      />

      <div>
        <Typography.Text strong>方法说明</Typography.Text>
        <Typography.Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0, fontSize: 12.5 }}>
          {report.method}
        </Typography.Paragraph>
      </div>

      <div>
        <Typography.Text strong>数据集</Typography.Text>
        <div style={{ marginTop: 6 }}>
          <Space size={8} wrap>
            <Tag>source</Tag>
            <span className="mono" style={{ fontSize: 12 }}>
              {report.dataset.source || '—'}
            </span>
          </Space>
          <div style={{ marginTop: 6 }}>
            <Space size={8} wrap>
              <Tag>digest</Tag>
              <span className="mono" style={{ fontSize: 12 }}>
                {report.dataset.digest || '—'}
              </span>
              <span className="muted" style={{ fontSize: 12 }}>
                共 {formatNumber(detail.dataset.total)} 个产品 · 抽取于{' '}
                {formatTime(report.dataset.extractedAt)}
              </span>
            </Space>
          </div>
        </div>
      </div>

      <Divider style={{ margin: 0 }} />

      <Space size={16} wrap style={{ fontSize: 12 }}>
        <span className="muted">
          LLM：{detail.llm.calls} 次调用 · {formatNumber(detail.llm.tokens)} tokens · $
          {detail.llm.costUsd}
        </span>
        {detail.llm.degraded && <Tag color="orange">已降级为确定性文案</Tag>}
        {detail.llm.budgetExhausted && <Tag color="red">token 预算耗尽</Tag>}
      </Space>
    </Space>
  )
}

function SeverityBlock({ severity, count, total }: { severity: SeverityKey; count: number; total: number }) {
  const meta = SEVERITY_META[severity]
  const percent = total > 0 ? Math.round((count / total) * 100) : 0
  return (
    <div className="severity-block" data-severity={severity}>
      <div className="severity-block-head">
        <span className="severity-block-label">{meta.label}</span>
        <Tag color={meta.tagColor} style={{ margin: 0 }}>
          {severity}
        </Tag>
      </div>
      <div className="severity-block-value">{count}</div>
      <Progress
        percent={percent}
        size="small"
        showInfo={false}
        strokeColor={meta.color}
        aria-label={`${meta.label}占问题类型比例`}
      />
      <span className="muted" style={{ fontSize: 11.5 }}>
        占问题类型 {sharePercent(count / total)}%
      </span>
    </div>
  )
}
