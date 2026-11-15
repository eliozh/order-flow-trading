import type { ApiError, ApiMeta } from './status'

export type { ApiError, ApiMeta }

export interface SymbolContract {
  symbol: string
  base_asset: string
  quote_asset: string
  contract_type: string
  underlying_type: string | null
  status: string
  tick_size: string
  step_size: string
  min_qty: string
  min_notional: string
  price_precision: number
  quantity_precision: number
  onboard_date: number
  tradifi: boolean
  listed_date: string
}

export interface SymbolCoverage {
  status: string
  coverage_pct: number | null
  history_start: string
  book_ticker: string
  liquidation_history: string
  latest_metric_at: string | null
  notes: string[]
}

export interface SymbolDetail {
  contract: SymbolContract
  coverage: SymbolCoverage
}

export interface SymbolListData {
  items: SymbolContract[]
  total: number
  query: string | null
  group: string | null
  contract_type?: string | null
}

export interface SymbolListEnvelope {
  ok: boolean
  data: SymbolListData | null
  error: ApiError | null
  meta: ApiMeta
}

export interface SymbolDetailEnvelope {
  ok: boolean
  data: SymbolDetail | null
  error: ApiError | null
  meta: ApiMeta
}

export interface SymbolQuery {
  q?: string
  group?: 'crypto' | 'tradfi'
  contract_type?: string
  limit?: number
  offset?: number
}

/** A typed symbol-registry failure (message from the API error envelope). */
export class SymbolApiError extends Error {
  readonly code: string | undefined

  constructor(message: string, code?: string) {
    super(message)
    this.name = 'SymbolApiError'
    this.code = code
  }
}

async function getEnvelope<T>(path: string): Promise<{
  ok: boolean
  data: T | null
  error: ApiError | null
  meta: ApiMeta
}> {
  const response = await fetch(path, {
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  })
  let envelope: { ok: boolean; data: T | null; error: ApiError | null; meta: ApiMeta } | null = null
  try {
    envelope = (await response.json()) as {
      ok: boolean
      data: T | null
      error: ApiError | null
      meta: ApiMeta
    }
  } catch {
    envelope = null
  }
  if (!response.ok || !envelope || !envelope.ok || envelope.data === null) {
    throw new SymbolApiError(
      envelope?.error?.message ?? `Symbol request failed with HTTP ${response.status}`,
      envelope?.error?.code,
    )
  }
  return envelope
}

function queryString(query: SymbolQuery): string {
  const params = new URLSearchParams()
  if (query.q !== undefined && query.q !== '') {
    params.set('q', query.q)
  }
  if (query.group !== undefined) {
    params.set('group', query.group)
  }
  if (query.contract_type !== undefined && query.contract_type !== '') {
    params.set('contract_type', query.contract_type)
  }
  if (query.limit !== undefined) {
    params.set('limit', String(query.limit))
  }
  if (query.offset !== undefined) {
    params.set('offset', String(query.offset))
  }
  const encoded = params.toString()
  return encoded ? `?${encoded}` : ''
}

/** Fetch the full registry: the preview universe has ~942 symbols. */
export const SYMBOL_FETCH_LIMIT = 2000

export async function fetchSymbols(query: SymbolQuery = {}): Promise<SymbolListEnvelope> {
  const limit = query.limit ?? SYMBOL_FETCH_LIMIT
  return getEnvelope<SymbolListData>(`/api/v1/symbols${queryString({ ...query, limit })}`)
}

export async function fetchSymbol(symbol: string): Promise<SymbolDetailEnvelope> {
  return getEnvelope<SymbolDetail>(`/api/v1/symbols/${encodeURIComponent(symbol)}`)
}

/** Exact symbol match first, then case-insensitive substring on symbol/base. */
export function filterSymbols<T extends SymbolContract>(items: T[], query: string): T[] {
  const needle = query.trim().toUpperCase()
  if (!needle) {
    return items
  }
  const matches = items.filter(
    (item) =>
      item.symbol.toUpperCase().includes(needle) || item.base_asset.toUpperCase().includes(needle),
  )
  return [...matches].sort((left, right) => {
    const leftExact = left.symbol.toUpperCase() === needle ? 1 : 0
    const rightExact = right.symbol.toUpperCase() === needle ? 1 : 0
    if (leftExact !== rightExact) {
      return rightExact - leftExact
    }
    return left.symbol.localeCompare(right.symbol)
  })
}

export function groupSymbols<T extends SymbolContract>(items: T[]): {
  crypto: T[]
  tradfi: T[]
} {
  return {
    crypto: items.filter((item) => !item.tradifi),
    tradfi: items.filter((item) => item.tradifi),
  }
}
