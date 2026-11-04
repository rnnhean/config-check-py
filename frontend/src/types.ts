// 与后端 API 一一对应的类型定义。
// 字段名以 FastAPI 实际返回为准（app/main.py、app/pipeline.py、app/rules.py、app/observability.py）。

export type SeverityKey = 'critical' | 'high' | 'medium' | 'low'

export interface Health {
  ok: boolean
  version: string
  llmMode: 'mock' | 'real'
  llmAsync: boolean
  model: string
  maxConcurrentLlm: number
  dataPath: string
  products: number
  audits: number
}

export interface AuditSummary {
  id: string
  traceId: string
  status: string
  createdAt: string
  durationMs: number
  total: number
  digest: string
  findingCount: number
  distinctAffected: number
  headline: string
}

export interface FindingSample {
  key: string
  value: string
  en_name: string
  terminals: string[]
  detail: string
}

export interface Finding {
  ruleId: string
  ruleLabel: string
  criterion: string
  consequence: string
  severity: SeverityKey
  severityLabel: string
  affected: number
  affectedShare: number
  scope: 'product' | 'terminal'
  reliability: number
  samples: FindingSample[]
  needsHumanReview: boolean
  reviewQuestion: string
  explanation: string
  fixAction: string
}

export type SeverityBreakdown = Record<SeverityKey, number>

export interface AuditReport {
  headline: string
  executiveSummary: string
  method: string
  generatedAt: string
  total: number
  findingCount: number
  affectedTotal: number
  distinctAffected: number
  severityBreakdown: SeverityBreakdown
  findings: Finding[]
  dataset: {
    source: string
    digest: string
    extractedAt: string
  }
}

export interface NodeTrace {
  name: string
  status: string
  durationMs: number
  detail: Record<string, unknown>
  error?: string
}

export interface LlmStats {
  calls: number
  attempts: number
  failures: number
  totalMs: number
  promptTokens: number
  completionTokens: number
  tokens: number
  costUsd: number
  degraded: boolean
  budgetExhausted: boolean
}

export interface AuditDataset {
  source: string
  digest: string
  extractedAt: string
  total: number
}

export interface AuditDetail {
  id: string
  traceId: string
  status: string
  createdAt: string
  durationMs: number
  dataset: AuditDataset
  traces: NodeTrace[]
  report: AuditReport | null
  llm: LlmStats
  error: string | null
}

export interface NodeMetric {
  count: number
  avgMs: number
  maxMs: number
  totalMs: number
}

export interface LlmMetrics {
  calls: number
  failures: number
  promptTokens: number
  completionTokens: number
  tokens: number
  costUsd: number
  inflight: number
  queueDepth: number
  timeoutsTotal: number
  retriesTotal: number
  latencyCount: number
  p50Ms: number
  p95Ms: number
}

export interface Metrics {
  audits: {
    total: number
    failed: number
    running: number
  }
  llm: LlmMetrics
  nodes: Record<string, NodeMetric>
  llm_inflight: number
  llm_queue_depth: number
  llm_timeouts_total: number
  llm_retries_total: number
  llm_p50_ms: number
  llm_p95_ms: number
}
