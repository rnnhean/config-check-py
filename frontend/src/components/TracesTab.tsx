import { Collapse, Empty, Tag, Timeline, Typography } from 'antd'
import { JsonView, darkStyles, defaultStyles } from 'react-json-view-lite'
import 'react-json-view-lite/dist/index.css'
import {
  NODE_LABELS,
  TRACE_STATUS_COLOR,
  TRACE_STATUS_LABEL,
  formatMs,
} from '../constants'
import type { NodeTrace } from '../types'

interface Props {
  traces: NodeTrace[]
}

/**
 * 节点链路：按 traces 的原始顺序（即图的执行顺序）渲染时间线，
 * detail 是各节点自己产出的结构化证据，用 react-json-view-lite 折叠展示，
 * 不把 JSON 直接铺成一大段文本。
 */
export function TracesTab({ traces }: Props) {
  if (!traces.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本次审计没有节点记录" />
  }

  // 深色模式下 <html data-theme> 由 main.tsx 维护，这里读同一个信号
  const dark = document.documentElement.dataset.theme === 'dark'

  return (
    <Timeline
      items={traces.map((trace) => ({
        color: trace.status === 'failed' ? 'red' : 'green',
        children: <TraceItem trace={trace} dark={dark} />,
      }))}
    />
  )
}

function TraceItem({ trace, dark }: { trace: NodeTrace; dark: boolean }) {
  const label = NODE_LABELS[trace.name] ?? trace.name
  const hasDetail = trace.detail && Object.keys(trace.detail).length > 0

  return (
    <div className="trace-item">
      <div className="trace-head">
        <span style={{ fontWeight: 600 }}>{label}</span>
        <span className="mono muted" style={{ fontSize: 11 }}>
          {trace.name}
        </span>
        <Tag color={TRACE_STATUS_COLOR[trace.status] ?? 'default'} style={{ margin: 0 }}>
          {TRACE_STATUS_LABEL[trace.status] ?? trace.status}
        </Tag>
        <span className="mono muted" style={{ fontSize: 11.5 }}>
          {formatMs(trace.durationMs)}
        </span>
      </div>
      {trace.error && (
        <Typography.Text type="danger" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
          {trace.error}
        </Typography.Text>
      )}
      {hasDetail && (
        <Collapse
          size="small"
          ghost
          style={{ marginTop: 6 }}
          items={[
            {
              key: 'detail',
              label: <span style={{ fontSize: 12 }}>节点明细</span>,
              children: (
                <JsonView
                  data={trace.detail}
                  shouldExpandNode={() => false}
                  style={dark ? darkStyles : defaultStyles}
                />
              ),
            },
          ]}
        />
      )}
    </div>
  )
}
