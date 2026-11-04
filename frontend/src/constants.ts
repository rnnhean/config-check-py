import dayjs from 'dayjs'
import type { SeverityKey } from './types'

/** 严重度展示元信息：中文名、进度条颜色、Tag 颜色与后端 severity 字段一一对应。 */
export const SEVERITY_META: Record<SeverityKey, { label: string; color: string; tagColor: string }> = {
  critical: { label: '高危', color: '#dc2626', tagColor: 'red' },
  high: { label: '较高', color: '#d97706', tagColor: 'orange' },
  medium: { label: '中等', color: '#2563eb', tagColor: 'blue' },
  low: { label: '较低', color: '#64748b', tagColor: 'default' },
}

export const SEVERITY_ORDER: SeverityKey[] = ['critical', 'high', 'medium', 'low']

/** 与 app/pipeline.py 的 NODE_TITLES 保持一致，前端只做展示，不参与计算。 */
export const NODE_LABELS: Record<string, string> = {
  load_catalog: '载入产品目录',
  scan_rules: '逐条执行检查规则',
  quantify: '量化影响面',
  assess: '判定严重程度',
  draft_fixes: '生成解释与修复建议',
  compile: '汇总报告',
}

/** 指标表按图的执行顺序展示已知节点，未知节点追加在后面。 */
export const NODE_ORDER: string[] = [
  'load_catalog',
  'scan_rules',
  'quantify',
  'assess',
  'draft_fixes',
  'compile',
]

export const AUDIT_STATUS_LABEL: Record<string, string> = {
  done: '已完成',
  failed: '失败',
  running: '运行中',
}

export const AUDIT_STATUS_COLOR: Record<string, string> = {
  done: 'green',
  failed: 'red',
  running: 'blue',
}

export const TRACE_STATUS_LABEL: Record<string, string> = {
  done: '完成',
  failed: '失败',
  pending: '等待',
}

export const TRACE_STATUS_COLOR: Record<string, string> = {
  done: 'green',
  failed: 'red',
  pending: 'default',
}

/** 后端时间统一是 UTC ISO 字符串（带 Z），dayjs 会按浏览器时区转成本地时间展示。 */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const parsed = dayjs(iso)
  return parsed.isValid() ? parsed.format('YYYY-MM-DD HH:mm:ss') : iso
}

export function formatUsd(value: number): string {
  if (!Number.isFinite(value) || value === 0) return '$0'
  return value < 0.01 ? `$${value.toFixed(6)}` : `$${value.toFixed(4)}`
}

export function formatNumber(value: number): string {
  return Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—'
}

export function formatMs(value: number): string {
  return `${(Math.round(value * 100) / 100).toLocaleString('zh-CN')} ms`
}

/** affectedShare 是 0~1 的小数，转成 0~100 的百分比（保留一位小数）。 */
export function sharePercent(share: number): number {
  if (!Number.isFinite(share)) return 0
  return Math.round(share * 1000) / 10
}
