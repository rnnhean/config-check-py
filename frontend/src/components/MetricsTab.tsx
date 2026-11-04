import { useEffect } from 'react'
import { useRequest } from 'ahooks'
import { Alert, App as AntdApp, Card, Col, Row, Space, Spin, Statistic, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { api } from '../api'
import { NODE_LABELS, NODE_ORDER, formatMs, formatNumber, formatUsd } from '../constants'
import type { NodeMetric } from '../types'

const POLL_MS = 3_000

interface NodeRow extends NodeMetric {
  name: string
}

/**
 * 运行指标：挂载即拉一次，之后每 3 秒轮询。
 * 轮询交给 ahooks 的 useRequest（pollingInterval），失败自动保留上一次成功数据；
 * 组件卸载时 ahooks 负责停轮询并丢弃在途结果，不需要自己管定时器。
 */
export function MetricsTab() {
  const { message } = AntdApp.useApp()
  const { data: metrics, error, loading } = useRequest(api.metrics, {
    pollingInterval: POLL_MS,
    pollingErrorRetryCount: -1,
  })

  // 首次失败提示一次；后续轮询失败只在页面内展示，避免每 3 秒弹一次
  useEffect(() => {
    if (error && !metrics) message.error('加载失败，请检查服务是否运行')
  }, [error, metrics, message])

  if (loading && !metrics) {
    return (
      <div className="center-box">
        <Spin />
      </div>
    )
  }

  if (!metrics) {
    return (
      <Alert
        type="error"
        showIcon
        message="加载失败，请检查服务是否运行"
        description={error?.message}
      />
    )
  }

  const { audits, llm, nodes } = metrics
  // 已知节点按图的执行顺序在前，接口里新增的未知节点追加在后面
  const nodeRows: NodeRow[] = [
    ...NODE_ORDER.filter((name) => nodes[name]).map((name) => ({ name, ...nodes[name] })),
    ...Object.keys(nodes)
      .filter((name) => !NODE_ORDER.includes(name))
      .map((name) => ({ name, ...nodes[name] })),
  ]

  const columns: ColumnsType<NodeRow> = [
    {
      title: '节点',
      key: 'name',
      render: (_, row) => (
        <Space size={6}>
          <span>{NODE_LABELS[row.name] ?? row.name}</span>
          <span className="mono muted" style={{ fontSize: 11 }}>
            {row.name}
          </span>
        </Space>
      ),
    },
    {
      title: '次数',
      dataIndex: 'count',
      width: 90,
      align: 'right',
      render: (value: number) => <span className="mono">{formatNumber(value)}</span>,
    },
    {
      title: '平均耗时',
      dataIndex: 'avgMs',
      width: 120,
      align: 'right',
      render: (value: number) => <span className="mono">{formatMs(value)}</span>,
    },
    {
      title: '最大耗时',
      dataIndex: 'maxMs',
      width: 120,
      align: 'right',
      render: (value: number) => <span className="mono">{formatMs(value)}</span>,
    },
    {
      title: '总耗时',
      dataIndex: 'totalMs',
      width: 120,
      align: 'right',
      render: (value: number) => <span className="mono">{formatMs(value)}</span>,
    },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {error && (
        <Alert
          type="warning"
          showIcon
          message="指标刷新失败，显示的是上一次成功的数据"
          description={error.message}
        />
      )}

      <div>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          审计
        </Typography.Text>
        <Row gutter={[12, 12]} style={{ marginTop: 8 }}>
          <Col xs={12} sm={8}>
            <Card size="small">
              <Statistic title="累计审计" value={audits.total} />
            </Card>
          </Col>
          <Col xs={12} sm={8}>
            <Card size="small">
              <Statistic
                title="失败"
                value={audits.failed}
                valueStyle={audits.failed > 0 ? { color: '#dc2626' } : undefined}
              />
            </Card>
          </Col>
          <Col xs={12} sm={8}>
            <Card size="small">
              <Statistic title="进行中" value={audits.running} />
            </Card>
          </Col>
        </Row>
      </div>

      <div>
        <Space size={8} wrap>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            LLM
          </Typography.Text>
          <Tag color={metrics.llm_inflight > 0 ? 'blue' : 'default'} style={{ margin: 0 }}>
            在飞 {metrics.llm_inflight}
          </Tag>
          <Tag color={metrics.llm_queue_depth > 0 ? 'orange' : 'default'} style={{ margin: 0 }}>
            排队 {metrics.llm_queue_depth}
          </Tag>
          <Tag style={{ margin: 0 }}>调用 {formatNumber(llm.calls)}</Tag>
          <Tag style={{ margin: 0 }}>失败 {formatNumber(llm.failures)}</Tag>
        </Space>
        <Row gutter={[12, 12]} style={{ marginTop: 8 }}>
          <Col xs={12} sm={8} md={6}>
            <Card size="small">
              <Statistic title="P50" value={llm.p50Ms} suffix="ms" />
            </Card>
          </Col>
          <Col xs={12} sm={8} md={6}>
            <Card size="small">
              <Statistic title="P95" value={llm.p95Ms} suffix="ms" />
            </Card>
          </Col>
          <Col xs={12} sm={8} md={6}>
            <Card size="small">
              <Statistic title="Token" value={llm.tokens} />
            </Card>
          </Col>
          <Col xs={12} sm={8} md={6}>
            <Card size="small">
              <Statistic title="成本" value={formatUsd(llm.costUsd)} />
            </Card>
          </Col>
          <Col xs={12} sm={8} md={6}>
            <Card size="small">
              <Statistic
                title="超时"
                value={llm.timeoutsTotal}
                valueStyle={llm.timeoutsTotal > 0 ? { color: '#dc2626' } : undefined}
              />
            </Card>
          </Col>
          <Col xs={12} sm={8} md={6}>
            <Card size="small">
              <Statistic
                title="重试"
                value={llm.retriesTotal}
                valueStyle={llm.retriesTotal > 0 ? { color: '#d97706' } : undefined}
              />
            </Card>
          </Col>
          <Col xs={12} sm={8} md={6}>
            <Card size="small">
              <Statistic title="延迟样本" value={llm.latencyCount} suffix="次" />
            </Card>
          </Col>
          <Col xs={12} sm={8} md={6}>
            <Card size="small">
              <Statistic title="注入 Token" value={llm.promptTokens} />
            </Card>
          </Col>
        </Row>
      </div>

      <div>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          节点耗时
        </Typography.Text>
        <Table<NodeRow>
          style={{ marginTop: 8 }}
          rowKey="name"
          size="small"
          columns={columns}
          dataSource={nodeRows}
          pagination={false}
          locale={{ emptyText: '还没有节点记录（先跑一次审计）' }}
          scroll={{ x: 620 }}
        />
      </div>

      <Typography.Text type="secondary" style={{ fontSize: 11.5 }}>
        每 3 秒自动刷新 · 指标为进程级累计值
      </Typography.Text>
    </Space>
  )
}
