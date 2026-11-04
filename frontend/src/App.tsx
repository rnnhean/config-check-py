import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { App as AntdApp, Badge, Button, Card, Space, Statistic, Tag, Tooltip } from 'antd'
import { MoonOutlined, PlayCircleFilled, ReloadOutlined, SunOutlined } from '@ant-design/icons'
import { api, ApiError } from './api'
import { AuditList } from './components/AuditList'
import { AuditDetailPanel } from './components/AuditDetailPanel'
import { formatMs } from './constants'
import type { AuditDetail, AuditSummary, Health, Metrics } from './types'

type ThemeName = 'light' | 'dark'

interface AppProps {
  themeName: ThemeName
  onToggleTheme: () => void
}

export function App({ themeName, onToggleTheme }: AppProps) {
  const { message } = AntdApp.useApp()

  const [health, setHealth] = useState<Health | null>(null)
  // 统计卡用的指标快照：首屏与每次审计结束后刷新一次。
  // 3 秒轮询放在「运行指标」页签内，避免两处同时打同一个接口。
  const [metrics, setMetrics] = useState<Metrics | null>(null)
  const [audits, setAudits] = useState<AuditSummary[]>([])
  const [listLoading, setListLoading] = useState(true)
  const [detail, setDetail] = useState<AuditDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [refreshing, setRefreshing] = useState(false)

  // 详情请求的竞态保护：连续点选时只认最后一次请求的结果
  const detailRequestRef = useRef(0)

  const loadMeta = useCallback(async () => {
    const [healthRes, metricsRes] = await Promise.allSettled([api.health(), api.metrics()])
    if (healthRes.status === 'fulfilled') setHealth(healthRes.value)
    else setHealth(null)
    if (metricsRes.status === 'fulfilled') setMetrics(metricsRes.value)
  }, [])

  const loadAudits = useCallback(
    async (options: { selectFirst?: boolean; silent?: boolean } = {}) => {
      const { selectFirst = false, silent = false } = options
      if (!silent) setListLoading(true)
      try {
        const res = await api.audits(50)
        setAudits(res.audits)
        if (selectFirst || !res.audits.length) {
          setSelectedId((current) => {
            // 已有选中项且仍在列表里就保持不动，避免刷新把用户看到的详情换掉
            if (!selectFirst && current && res.audits.some((item) => item.id === current)) {
              return current
            }
            return res.audits[0]?.id ?? null
          })
        }
        return res.audits
      } catch (error) {
        if (!silent) message.error('加载失败，请检查服务是否运行')
        throw error
      } finally {
        if (!silent) setListLoading(false)
      }
    },
    [message],
  )

  /* ---------- 首屏 ---------- */
  useEffect(() => {
    let cancelled = false
    const bootstrap = async () => {
      try {
        const list = await api.audits(50)
        if (cancelled) return
        setAudits(list.audits)
        setSelectedId(list.audits[0]?.id ?? null)
      } catch {
        if (!cancelled) message.error('加载失败，请检查服务是否运行')
      } finally {
        if (!cancelled) setListLoading(false)
      }
      await loadMeta()
    }
    void bootstrap()
    return () => {
      cancelled = true
      detailRequestRef.current += 1
    }
  }, [loadMeta, message])

  /* ---------- 详情 ---------- */
  const openAudit = useCallback(
    async (id: string) => {
      const requestId = detailRequestRef.current + 1
      detailRequestRef.current = requestId
      setSelectedId(id)
      setDetailLoading(true)
      setDetailError(null)
      try {
        const result = await api.audit(id)
        if (detailRequestRef.current === requestId) setDetail(result)
      } catch (error) {
        if (detailRequestRef.current !== requestId) return
        const text = error instanceof ApiError ? error.message : '加载失败，请检查服务是否运行'
        setDetail(null)
        setDetailError(text)
        message.error('加载失败，请检查服务是否运行')
      } finally {
        if (detailRequestRef.current === requestId) setDetailLoading(false)
      }
    },
    [message],
  )

  // 首屏选中的 id 变化时拉详情；点击列表项走 selectAudit，避免重复请求
  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      return
    }
    if (detail?.id === selectedId) return
    void openAudit(selectedId)
    // detail 只作为「已经加载过」的判断，不作为依赖，防止进入循环
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId])

  const selectAudit = useCallback(
    (id: string) => {
      if (id === selectedId && detail?.id === id) return
      void openAudit(id)
    },
    [detail?.id, openAudit, selectedId],
  )

  /* ---------- 运行审计 ---------- */
  const runAudit = useCallback(async () => {
    setRunning(true)
    try {
      const created = await api.createAudit()
      message.success(`审计完成：${created.report?.findingCount ?? 0} 类问题`)
      // 新审计直接展示，同时刷新列表与统计快照
      setDetail(created)
      setSelectedId(created.id)
      setDetailError(null)
      detailRequestRef.current += 1
      await Promise.allSettled([loadAudits({ selectFirst: true, silent: true }), loadMeta()])
    } catch (error) {
      if (error instanceof ApiError && error.status === 429) {
        const wait = error.retryAfter
        message.warning(
          wait === null
            ? 'LLM 并发已满，请稍后重试'
            : `LLM 并发已满，请 ${wait} 秒后重试`,
        )
      } else {
        const text = error instanceof Error ? error.message : '运行失败，请检查服务是否运行'
        message.error(`运行失败：${text}`)
      }
    } finally {
      setRunning(false)
    }
  }, [loadAudits, loadMeta, message])

  const refreshList = useCallback(async () => {
    setRefreshing(true)
    try {
      await loadAudits({ silent: true })
      await loadMeta()
      message.success('已刷新')
    } catch {
      message.error('加载失败，请检查服务是否运行')
    } finally {
      setRefreshing(false)
    }
  }, [loadAudits, loadMeta, message])

  const latestAudit = audits[0] ?? null
  const healthy = health?.ok === true
  const healthStatus: 'success' | 'warning' | 'error' =
    health === null ? 'error' : healthy ? 'success' : 'warning'
  const healthText =
    health === null ? '服务不可达' : healthy ? '数据可读' : '数据异常'

  const llmP95 = useMemo(() => metrics?.llm.p95Ms ?? null, [metrics])

  return (
    <div>
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark">CC</span>
          <span>
            <span className="brand-text">Config Check</span>
            <span className="brand-sub" style={{ marginLeft: 8 }}>
              产品配置审计台
            </span>
          </span>
        </div>
        <div className="header-spacer" />
        <Badge status={healthStatus} text={healthText} />
        {health && (
          <Space size={6} className="header-meta">
            <Tag color={health.llmMode === 'real' ? 'orange' : 'default'} style={{ margin: 0 }}>
              {health.llmMode === 'real' ? '真实模型' : 'mock'}
            </Tag>
            <span className="mono" style={{ fontSize: 12, opacity: 0.7 }}>
              {health.model}
            </span>
            <Tag style={{ margin: 0 }}>{health.llmAsync ? '异步' : '同步'}</Tag>
          </Space>
        )}
        <Tooltip title={themeName === 'dark' ? '切换到浅色' : '切换到深色'}>
          <Button
            type="text"
            icon={themeName === 'dark' ? <SunOutlined /> : <MoonOutlined />}
            onClick={onToggleTheme}
            aria-label="切换主题"
          />
        </Tooltip>
      </header>

      <main className="app-main">
        <div className="page-head">
          <div>
            <h1 className="page-title">产品配置审计台</h1>
            <p className="page-desc">
              逐产品执行 8 条确定性规则，统计影响面后按「可靠性 × 影响面」定级；模型只负责把判据与后果讲清楚，
              不参与检出、不做定级、不修改数据。
            </p>
          </div>
          <Button
            type="primary"
            size="large"
            icon={<PlayCircleFilled />}
            loading={running}
            onClick={runAudit}
          >
            运行审计
          </Button>
        </div>

        <div className="stat-grid">
          <Card size="small">
            <Statistic title="产品数" value={health?.products ?? '—'} suffix="个" />
          </Card>
          <Card size="small">
            <Statistic title="审计总数" value={health?.audits ?? '—'} suffix="次" />
          </Card>
          <Card size="small">
            <Statistic
              title="最近一次问题数"
              value={latestAudit?.findingCount ?? '—'}
              suffix={latestAudit ? '类' : ''}
              valueStyle={
                latestAudit && latestAudit.findingCount > 0 ? { color: '#d97706' } : undefined
              }
            />
          </Card>
          <Card size="small">
            <Statistic
              title="LLM P95"
              value={llmP95 === null ? '—' : Math.round(llmP95)}
              suffix={llmP95 === null ? '' : 'ms'}
            />
          </Card>
        </div>

        {/* 左 360px 历史 + 右详情：用 index.css 里的 .split（窄屏自动单列） */}
        <div className="split">
          <AuditList
            audits={audits}
            selectedId={selectedId}
            loading={listLoading}
            refreshing={refreshing}
            onSelect={selectAudit}
            onRefresh={refreshList}
          />
          <div>
            <AuditDetailPanel
              detail={detail}
              loading={detailLoading}
              error={detailError}
              hasSelection={Boolean(selectedId)}
              onRetry={() => selectedId && openAudit(selectedId)}
            />
            {selectedId && detail && (
              <div style={{ marginTop: 12, textAlign: 'right', fontSize: 12, opacity: 0.6 }}>
                <Button
                  type="link"
                  size="small"
                  icon={<ReloadOutlined />}
                  onClick={() => openAudit(selectedId)}
                >
                  重新加载详情
                </Button>
                <span className="mono" style={{ marginLeft: 8 }}>
                  {detail.traceId} · {formatMs(detail.durationMs)}
                </span>
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  )
}
