import { fetchWithTimeout } from './request-timeout'
import type { ApiError, ApiMeta } from './status'

export type { ApiError, ApiMeta }

export interface PaperPosition {
  position_id: string
  symbol: string
  side: string
  state?: string
  size: string
  entry_price: string
  avg_entry_price?: string
  mark_price: string | null
  notional_usd?: string
  unrealized_pnl_usd?: string | null
  stop_price?: string | null
  leverage?: number
  margin_usd?: string
  risk_pct?: number
  opened_at?: string
  updated_at?: string
  source_trigger_id?: string | null
  source_decision_id?: string | null
}

export interface PaperAccountData {
  account_id: string
  environment: string
  currency: string
  initial_balance: string
  cash_balance: string
  balance: string
  positions: PaperPosition[]
  open_positions: number
  unrealized_at_last_mark: string
  equity: string
  stale_inputs: string[]
  updated_at: string
}

export interface PaperOrder {
  order_id: string
  client_order_id?: string
  symbol: string
  side: string
  type: string
  quantity: string
  price: string | null
  status: string
  filled_quantity?: string
  avg_fill_price?: string | null
  created_at: string
  environment?: string
}

export interface PaperFill {
  fill_id: string
  order_id: string
  symbol: string
  side: string
  price: string
  quantity: string
  fee: string | null
  fee_asset?: string
  maker?: boolean
  filled_at: string
}

export interface PaperListData<T> {
  items: T[]
  total: number
}

export interface PaperEnvelope<T> {
  ok: boolean
  data: T | null
  error: ApiError | null
  meta: ApiMeta
}

export type PaperAccountEnvelope = PaperEnvelope<PaperAccountData>
export type PaperOrdersEnvelope = PaperEnvelope<PaperListData<PaperOrder>>
export type PaperFillsEnvelope = PaperEnvelope<PaperListData<PaperFill>>

export interface KillSwitchStatus {
  active: boolean
  sentinel_path: string
  checked_at: string
}

export type KillSwitchEnvelope = PaperEnvelope<KillSwitchStatus>

export class PaperApiError extends Error {
  readonly code: string | undefined

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'PaperApiError'
    this.code = code
  }
}

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') {
    return null
  }
  const match = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name}=`))
  return match ? decodeURIComponent(match.slice(name.length + 1)) : null
}

function csrfHeader(): Record<string, string> {
  const csrf = readCookie('oft_csrf')
  return csrf ? { 'X-CSRF-Token': csrf } : {}
}

async function requestEnvelope<T>(
  path: string,
  options: { method?: 'GET' | 'POST' | 'DELETE'; signal?: AbortSignal } = {},
): Promise<PaperEnvelope<T>> {
  const method = options.method ?? 'GET'
  const response = await fetchWithTimeout(
    path,
    {
      method,
      headers:
        method === 'GET'
          ? { Accept: 'application/json' }
          : { Accept: 'application/json', ...csrfHeader() },
      credentials: 'same-origin',
    },
    options.signal,
  )
  let envelope: PaperEnvelope<T> | null = null
  try {
    envelope = (await response.json()) as PaperEnvelope<T>
  } catch {
    envelope = null
  }
  if (!response.ok || !envelope || !envelope.ok || envelope.data === null) {
    throw new PaperApiError(
      envelope?.error?.message ?? `Paper request failed with HTTP ${response.status}`,
      envelope?.error?.code,
    )
  }
  return envelope
}

export async function fetchPaperAccount(signal?: AbortSignal): Promise<PaperAccountEnvelope> {
  return requestEnvelope<PaperAccountData>('/api/v1/paper/account', { signal })
}

export async function fetchPaperOrders(signal?: AbortSignal): Promise<PaperOrdersEnvelope> {
  return requestEnvelope<PaperListData<PaperOrder>>('/api/v1/paper/orders', { signal })
}

export async function fetchPaperFills(signal?: AbortSignal): Promise<PaperFillsEnvelope> {
  return requestEnvelope<PaperListData<PaperFill>>('/api/v1/paper/fills', { signal })
}

export async function fetchKillSwitch(signal?: AbortSignal): Promise<KillSwitchEnvelope> {
  return requestEnvelope<KillSwitchStatus>('/api/v1/kill-switch', { signal })
}

export async function activateKillSwitch(signal?: AbortSignal): Promise<KillSwitchEnvelope> {
  return requestEnvelope<KillSwitchStatus>('/api/v1/kill-switch', { method: 'POST', signal })
}

export async function deactivateKillSwitch(
  token: string,
  signal?: AbortSignal,
): Promise<KillSwitchEnvelope> {
  return requestEnvelope<KillSwitchStatus>(
    `/api/v1/kill-switch?token=${encodeURIComponent(token)}`,
    { method: 'DELETE', signal },
  )
}
