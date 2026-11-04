import { Card, Empty, Spin, Tag, Tooltip, Button } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { AUDIT_STATUS_COLOR, AUDIT_STATUS_LABEL, formatMs, formatTime } from '../constants'
import type { AuditSummary } from '../types'

interface Props {
  audits: AuditSummary[]
  selectedId: string | null
  loading: boolean
  refreshing: boolean
  onSelect: (id: string) => void
  onRefresh: () => void
}

/**
 * 审计历史：整行可点，选中态用左侧色条（.run-row[data-active]），
 * 与 hover 的背景色区分开，避免用户误以为鼠标悬停就是选中。
 */
export function AuditList({ audits, selectedId, loading, refreshing, onSelect, onRefresh }: Props) {
  return (
    <Card
      title="审计历史"
      size="small"
      styles={{ body: { padding: 0 } }}
      extra={
        <Tooltip title="刷新列表">
          <Button
            type="text"
            size="small"
            icon={<ReloadOutlined />}
            loading={refreshing}
            onClick={onRefresh}
            aria-label="刷新列表"
          />
        </Tooltip>
      }
    >
      {loading ? (
        <div className="center-box">
          <Spin />
        </div>
      ) : audits.length === 0 ? (
        <div className="center-box">
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有审计记录，点右上角「运行审计」" />
        </div>
      ) : (
        <div className="audit-list">
          {audits.map((audit) => {
            const status = audit.status || 'done'
            return (
              <button
                key={audit.id}
                type="button"
                className="run-row"
                data-active={audit.id === selectedId}
                onClick={() => onSelect(audit.id)}
              >
                <span className="run-row-main">
                  <span className="run-row-headline" title={audit.headline || audit.id}>
                    {audit.headline || audit.id}
                  </span>
                  <span className="run-row-sub">
                    <Tag color={AUDIT_STATUS_COLOR[status] ?? 'default'} style={{ marginRight: 6 }}>
                      {AUDIT_STATUS_LABEL[status] ?? status}
                    </Tag>
                    {audit.findingCount} 类问题 · {formatMs(audit.durationMs)}
                  </span>
                  <span className="run-row-id">{formatTime(audit.createdAt)}</span>
                </span>
              </button>
            )
          })}
        </div>
      )}
    </Card>
  )
}
