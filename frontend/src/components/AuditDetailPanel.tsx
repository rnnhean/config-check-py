import { useState } from 'react'
import { Alert, Button, Card, Empty, Space, Spin, Tabs, Tag, Typography } from 'antd'
import { AuditOutlined, ApartmentOutlined, DashboardOutlined, WarningOutlined } from '@ant-design/icons'
import { api } from '../api'
import { AUDIT_STATUS_COLOR, AUDIT_STATUS_LABEL, formatTime } from '../constants'
import type { AuditDetail } from '../types'
import { OverviewTab } from './OverviewTab'
import { FindingsTab } from './FindingsTab'
import { TracesTab } from './TracesTab'
import { MetricsTab } from './MetricsTab'

interface Props {
  detail: AuditDetail | null
  loading: boolean
  error: string | null
  hasSelection: boolean
  onRetry: () => void
}

export function AuditDetailPanel({ detail, loading, error, hasSelection, onRetry }: Props) {
  const [tab, setTab] = useState('overview')

  if (loading && !detail) {
    return (
      <Card>
        <div className="center-box">
          <Spin />
          <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>
            正在加载审计详情…
          </Typography.Paragraph>
        </div>
      </Card>
    )
  }

  if (error) {
    return (
      <Card title="加载失败">
        <Alert
          type="error"
          showIcon
          message="加载失败，请检查服务是否运行"
          description={error}
          action={
            <Button size="small" onClick={onRetry}>
              重试
            </Button>
          }
        />
      </Card>
    )
  }

  if (!detail) {
    return (
      <Card>
        <div className="center-box">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={hasSelection ? '暂无详情' : '从左侧选择一次审计，或点右上角「运行审计」'}
          />
        </div>
      </Card>
    )
  }

  if (detail.status === 'failed' || !detail.report) {
    return (
      <Card
        title={
          <Space>
            <span>审计未完成</span>
            <Tag color={AUDIT_STATUS_COLOR[detail.status] ?? 'default'}>
              {AUDIT_STATUS_LABEL[detail.status] ?? detail.status}
            </Tag>
          </Space>
        }
        extra={<span className="mono muted">{detail.id}</span>}
      >
        <Alert
          type="error"
          showIcon
          message="审计未能完成"
          description={detail.error ?? '后端未返回报告，也没有给出具体原因'}
        />
        <div style={{ marginTop: 16 }}>
          <TracesTab traces={detail.traces} />
        </div>
      </Card>
    )
  }

  const report = detail.report
  const findings = report.findings

  return (
    <Card
      title={
        <Space>
          <span>审计详情</span>
          <Tag color={AUDIT_STATUS_COLOR[detail.status] ?? 'default'}>
            {AUDIT_STATUS_LABEL[detail.status] ?? detail.status}
          </Tag>
          <span className="mono muted" style={{ fontSize: 12 }}>
            {detail.id}
          </span>
        </Space>
      }
      extra={
        <Space size={12}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {formatTime(detail.createdAt)}
          </Typography.Text>
          <a href={api.reportHtmlUrl(detail.id)} target="_blank" rel="noreferrer">
            原始报告 ↗
          </a>
        </Space>
      }
      styles={{ body: { paddingTop: 8 } }}
    >
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          {
            key: 'overview',
            label: (
              <span>
                <AuditOutlined /> 概览
              </span>
            ),
            children: <OverviewTab detail={detail} report={report} />,
          },
          {
            key: 'findings',
            label: (
              <span>
                <WarningOutlined /> 问题清单 ({findings.length})
              </span>
            ),
            children: <FindingsTab findings={findings} />,
          },
          {
            key: 'traces',
            label: (
              <span>
                <ApartmentOutlined /> 节点链路 ({detail.traces.length})
              </span>
            ),
            children: <TracesTab traces={detail.traces} />,
          },
          {
            key: 'metrics',
            label: (
              <span>
                <DashboardOutlined /> 运行指标
              </span>
            ),
            // 指标页只在被打开时轮询，避免在概览页也每 3 秒打一次接口
            children: tab === 'metrics' ? <MetricsTab /> : null,
          },
        ]}
      />
    </Card>
  )
}
