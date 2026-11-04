import type { AuditDetail, AuditSummary, Health, Metrics } from './types'

const API_BASE = import.meta.env.VITE_API_BASE || ''

/**
 * 结构化错误：`status` 为 0 表示网络层失败（超时 / 连不上），
 * 429 时额外带 `retryAfter`（秒），用于提示用户何时重试。
 */
export class ApiError extends Error {
  status: number
  retryAfter: number | null

  constructor(status: number, message: string, retryAfter: number | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.retryAfter = retryAfter
  }
}

function parseRetryAfter(value: string | null): number | null {
  if (!value) return null
  const seconds = Number(value)
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null
}

/**
 * 统一请求：显式超时 + 结构化错误。
 * POST /api/audits 在真实模型下可能等待较久，这里给 120 秒；查询类接口 10 秒足够。
 */
async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 10_000): Promise<T> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    const headers = new Headers(init.headers)
    if (init.body && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json')
    }
    const res = await fetch(`${API_BASE}${path}`, { ...init, headers, signal: controller.signal })
    const text = await res.text()
    let data: unknown = null
    if (text) {
      try {
        data = JSON.parse(text)
      } catch {
        data = { detail: text }
      }
    }
    if (!res.ok) {
      // 429 的 body 是 {error, inflight, queueDepth}，与 FastAPI 默认的 {detail} 不同
      const body = data as { detail?: unknown; error?: unknown } | null
      const detail = body?.detail ?? body?.error ?? `请求失败（HTTP ${res.status}）`
      throw new ApiError(res.status, String(detail), parseRetryAfter(res.headers.get('Retry-After')))
    }
    return data as T
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError(0, '请求超时，请确认服务仍在运行')
    }
    if (error instanceof ApiError) throw error
    throw new ApiError(0, '网络错误：无法连接后端')
  } finally {
    window.clearTimeout(timer)
  }
}

export const api = {
  health: () => request<Health>('/api/health', {}, 5_000),

  metrics: () => request<Metrics>('/api/metrics', {}, 5_000),

  audits: (limit = 50) => request<{ audits: AuditSummary[] }>(`/api/audits?limit=${limit}`),

  audit: (id: string) => request<AuditDetail>(`/api/audits/${encodeURIComponent(id)}`),

  /** 同步执行一次审计；真实模型下要等模型跑完，等待上限放宽到 120 秒。 */
  createAudit: () => request<AuditDetail>('/api/audits', { method: 'POST' }, 120_000),

  /** 服务端渲染的 HTML 报告，前端只做外链跳转。 */
  reportHtmlUrl: (id: string) => `${API_BASE}/api/audits/${encodeURIComponent(id)}/report`,
}
