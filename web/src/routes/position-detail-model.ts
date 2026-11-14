import type { OpportunityData } from '../api/opportunities'
import type { PositionData, PositionHistoryData } from '../api/positions'
import { markState } from '../api/positions'
import type { ReportData } from '../api/reports'
import type { TriggerPayload } from '../api/triggers'

export interface DetailState {
  state: 'loading' | 'ready' | 'error'
  position?: PositionData
  history?: PositionHistoryData
  /** Set when the position loaded but its history projection did not. */
  historyError?: string | null
  trigger?: TriggerPayload | null
  opportunity?: OpportunityData | null
  report?: ReportData | null
  linkError?: string | null
  killSwitch?: boolean | null
  message?: string
  /** True only for a definitive 404; other failures are "unavailable". */
  notFound?: boolean
}

/** The immutable trigger -> opportunity -> report chain for one position. */
export interface LinkChain {
  trigger: TriggerPayload | null
  opportunity: OpportunityData | null
  report: ReportData | null
  error: string | null
}

export const EMPTY_CHAIN: LinkChain = {
  trigger: null,
  opportunity: null,
  report: null,
  error: null,
}

/**
 * Whether a polled position snapshot may replace the rendered one. A snapshot
 * whose version (or, when version is absent, whose ``updated_at``) is older may
 * have been captured before a management mutation and must not regress it.
 */
export function positionSupersedes(
  incoming: PositionData,
  previous: PositionData | undefined,
): boolean {
  if (!previous) {
    return true
  }
  if (typeof incoming.version === 'number' && typeof previous.version === 'number') {
    return incoming.version >= previous.version
  }
  const incomingAt = Date.parse(incoming.updated_at)
  const previousAt = Date.parse(previous.updated_at)
  if (!Number.isNaN(incomingAt) && !Number.isNaN(previousAt)) {
    return incomingAt >= previousAt
  }
  return true
}

/**
 * The one reason a management request is unavailable, or null when it can run.
 * The kill switch is tri-state: only an explicit `false` (inactive) enables the
 * action; an unknown state fails closed.
 */
export function managementBlockedReason(
  position: PositionData,
  mark: ReturnType<typeof markState>,
  killSwitch: boolean | null,
  narrow: boolean,
): string | null {
  if (position.state !== 'OPEN') {
    return `持仓状态为 ${position.state}：只有 OPEN 持仓可以请求管理决策。`
  }
  if (mark === 'MISSING') {
    return 'mark 价格缺失（STALE）：管理动作已禁用，不伪造 PnL 或风控输入。'
  }
  if (mark === 'STALE') {
    return 'mark 价格已过期（STALE）：管理动作已禁用，等待更新的 resolved mark。'
  }
  if (killSwitch === null) {
    return 'Kill switch 状态未知：管理动作已禁用，等待 kill-switch 状态确认。'
  }
  if (killSwitch) {
    return 'Kill switch 已启用：新订单与 LLM 管理执行已被阻断。'
  }
  if (narrow) {
    return '375px 只读：请在更宽的窗口请求管理决策。'
  }
  return null
}
