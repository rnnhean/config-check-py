import type { ReactNode } from 'react'
import { Empty, Progress, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { CheckCircleOutlined, ExclamationCircleOutlined } from '@ant-design/icons'
import { SEVERITY_META, formatNumber, sharePercent } from '../constants'
import type { Finding, FindingSample } from '../types'

interface Props {
  findings: Finding[]
}

export function FindingsTab({ findings }: Props) {
  if (!findings.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未检出配置问题" />
  }

  const columns: ColumnsType<Finding> = [
    {
      title: '严重度',
      dataIndex: 'severity',
      width: 96,
      render: (severity: Finding['severity'], finding) => {
        const meta = SEVERITY_META[severity]
        return (
          <Tag color={meta?.tagColor ?? 'default'} style={{ margin: 0 }}>
            {meta?.label ?? finding.severityLabel ?? severity}
          </Tag>
        )
      },
    },
    {
      title: '规则',
      key: 'rule',
      width: 240,
      render: (_, finding) => (
        <Space direction="vertical" size={0}>
          <span style={{ fontWeight: 600 }}>{finding.ruleLabel}</span>
          <span className="mono muted" style={{ fontSize: 11 }}>
            {finding.ruleId}
          </span>
        </Space>
      ),
    },
    {
      title: '影响',
      key: 'affected',
      width: 170,
      render: (_, finding) => {
        const percent = sharePercent(finding.affectedShare)
        return (
          <div className="impact-cell">
            <div className="impact-cell-head">
              <span className="impact-count">{formatNumber(finding.affected)}</span>
              <span className="muted" style={{ fontSize: 11.5 }}>
                {percent.toFixed(1)}%
              </span>
            </div>
            <Progress
              percent={percent}
              size="small"
              showInfo={false}
              strokeColor={SEVERITY_META[finding.severity]?.color}
            />
          </div>
        )
      },
    },
    {
      title: '范围',
      dataIndex: 'scope',
      width: 92,
      render: (scope: Finding['scope']) =>
        scope === 'terminal' ? <Tag style={{ margin: 0 }}>终端编码</Tag> : <Tag style={{ margin: 0 }}>产品</Tag>,
    },
    {
      title: '可靠性',
      dataIndex: 'reliability',
      width: 88,
      align: 'right',
      render: (value: number) => <span className="mono">{value.toFixed(2)}</span>,
    },
    {
      title: '需人工确认',
      dataIndex: 'needsHumanReview',
      width: 112,
      align: 'center',
      render: (needs: boolean) =>
        needs ? (
          <Tooltip title="规则可靠性不足，结论需要业务确认">
            <ExclamationCircleOutlined style={{ color: '#d97706' }} aria-label="需人工确认" />
          </Tooltip>
        ) : (
          <CheckCircleOutlined style={{ color: '#0f9f6e' }} aria-label="无需人工确认" />
        ),
    },
  ]

  return (
    <Table<Finding>
      rowKey="ruleId"
      size="small"
      columns={columns}
      dataSource={findings}
      pagination={false}
      scroll={{ x: 900 }}
      expandable={{
        expandedRowRender: (finding) => <FindingDetail finding={finding} />,
        rowExpandable: () => true,
      }}
    />
  )
}

function FindingDetail({ finding }: { finding: Finding }) {
  return (
    <Space direction="vertical" size={10} style={{ width: '100%', padding: '4px 0' }}>
      <Field label="判据">{finding.criterion}</Field>
      <Field label="后果">{finding.consequence}</Field>
      <Field label="说明">{finding.explanation || '（无额外说明）'}</Field>
      <Field label="修复建议">{finding.fixAction}</Field>
      {finding.needsHumanReview && (
        <Field label="待业务确认">
          <span style={{ color: '#d97706' }}>{finding.reviewQuestion}</span>
        </Field>
      )}
      <div>
        <Typography.Text strong style={{ fontSize: 12.5 }}>
          命中样例（前 5 条 / 共 {finding.samples.length} 条）
        </Typography.Text>
        <SampleTable samples={finding.samples.slice(0, 5)} />
      </div>
    </Space>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <Typography.Text strong style={{ fontSize: 12.5 }}>
        {label}
      </Typography.Text>
      <div style={{ fontSize: 12.5, marginTop: 2, opacity: 0.85 }}>{children}</div>
    </div>
  )
}

function SampleTable({ samples }: { samples: FindingSample[] }) {
  const columns: ColumnsType<FindingSample> = [
    {
      title: 'key',
      dataIndex: 'key',
      width: 170,
      render: (value: string) => <span className="mono" style={{ fontSize: 11.5 }}>{value || '—'}</span>,
    },
    {
      title: '中文名',
      dataIndex: 'value',
      width: 180,
      render: (value: string) => value || '—',
    },
    {
      title: 'en_name',
      dataIndex: 'en_name',
      width: 170,
      render: (value: string) => <span className="mono" style={{ fontSize: 11.5 }}>{value || '—'}</span>,
    },
    {
      title: '终端',
      dataIndex: 'terminals',
      width: 180,
      render: (terminals: string[]) =>
        terminals.length ? (
          <Space size={4} wrap>
            {terminals.map((item) => (
              <Tag key={item} style={{ margin: 0, fontSize: 11 }}>
                {item}
              </Tag>
            ))}
          </Space>
        ) : (
          '—'
        ),
    },
    {
      title: '说明',
      dataIndex: 'detail',
      render: (value: string) => value || '—',
    },
  ]

  return (
    <Table<FindingSample>
      style={{ marginTop: 6 }}
      rowKey={(record) => `${record.key}|${record.detail}`}
      size="small"
      columns={columns}
      dataSource={samples}
      pagination={false}
      scroll={{ x: 780 }}
    />
  )
}
