import { useCallback, useEffect, useRef, useState } from 'react'

import { isAbortError } from '../api/abort'
import { errorMessage } from '../api/errors'
import type { OpportunityData, OpportunityEnvelope } from '../api/opportunities'
import type { KillSwitchEnvelope } from '../api/paper'
import {
  PositionApiError,
  type PositionData,
  type PositionDetailEnvelope,
  type PositionHistoryEnvelope,
} from '../api/positions'
import type { ReportData, ReportEnvelope } from '../api/reports'
import type { TriggerDetailEnvelope, TriggerPayload } from '../api/triggers'
import { usePolling } from './use-polling'
import { useRunGeneration } from './use-run-generation'
import {
  EMPTY_CHAIN,
  positionSupersedes,
  type DetailState,
  type LinkChain,
} from '../routes/position-detail-model'

interface UsePositionDetailDataOptions {
  positionId: string
  fetchPositionFn: (positionId: string, signal?: AbortSignal) => Promise<PositionDetailEnvelope>
  fetchHistoryFn: (positionId: string, signal?: AbortSignal) => Promise<PositionHistoryEnvelope>
  fetchTriggerFn: (triggerId: string, signal?: AbortSignal) => Promise<TriggerDetailEnvelope>
  fetchOpportunityFn: (opportunityId: string, signal?: AbortSignal) => Promise<OpportunityEnvelope>
  fetchReportFn: (reportId: string, signal?: AbortSignal) => Promise<ReportEnvelope>
  fetchKillSwitchFn: (signal?: AbortSignal) => Promise<KillSwitchEnvelope>
  refreshMs: number
  paused: boolean
}

interface PositionDetailData {
  load: DetailState
  refreshError: string | null
  linkRefreshError: string | null
  retryInitial: () => void
  revalidate: () => void
  applyManagementPosition: (position: PositionData | null) => void
}

/**
 * Position/detail data: the initial load, the shared polling skeleton, the
 * immutable link chain, and the mutation guard.
 *
 * Position and history are fetched independently: a history-only failure keeps
 * the position (with ``historyError``) instead of replacing the page with
 * "not found". Only a definitive position 404 sets ``notFound``.
 */
export function usePositionDetailData({
  positionId,
  fetchPositionFn,
  fetchHistoryFn,
  fetchTriggerFn,
  fetchOpportunityFn,
  fetchReportFn,
  fetchKillSwitchFn,
  refreshMs,
  paused,
}: UsePositionDetailDataOptions): PositionDetailData {
  const [load, setLoad] = useState<DetailState>({ state: 'loading' })
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const [linkRefreshError, setLinkRefreshError] = useState<string | null>(null)
  const [retryToken, setRetryToken] = useState(0)
  const loadRef = useRef(load)
  loadRef.current = load
  const { begin, isCurrent } = useRunGeneration()
  /** The last successfully loaded link chain (fetched once per position). */
  const chainRef = useRef<LinkChain | null>(null)
  /** Incremented whenever a management result is applied, invalidating polls. */
  const mutationEpoch = useRef(0)

  const fetchChain = useCallback(
    async (sourceTriggerId: string | null, signal: AbortSignal): Promise<LinkChain> => {
      let trigger: TriggerPayload | null = null
      let opportunity: OpportunityData | null = null
      let report: ReportData | null = null
      let error: string | null = null
      if (sourceTriggerId) {
        try {
          trigger = (await fetchTriggerFn(sourceTriggerId, signal)).data
        } catch (caught: unknown) {
          if (signal.aborted || isAbortError(caught)) {
            throw caught
          }
          error = errorMessage(caught)
        }
        const opportunityId = trigger?.opportunity_id ?? null
        if (opportunityId) {
          try {
            opportunity = (await fetchOpportunityFn(opportunityId, signal)).data
          } catch (caught: unknown) {
            if (signal.aborted || isAbortError(caught)) {
              throw caught
            }
            error ??= errorMessage(caught)
          }
        }
        const reportId = opportunity?.source?.report_id ?? null
        if (reportId) {
          try {
            report = (await fetchReportFn(reportId, signal)).data
          } catch (caught: unknown) {
            if (signal.aborted || isAbortError(caught)) {
              throw caught
            }
            error ??= errorMessage(caught)
          }
        }
      }
      return { trigger, opportunity, report, error }
    },
    [fetchTriggerFn, fetchOpportunityFn, fetchReportFn],
  )

  const runLoad = useCallback(
    async (
      signal: AbortSignal,
      initial: boolean,
      refreshLinks: boolean,
      generation: number,
    ): Promise<void> => {
      if (initial) {
        setLoad({ state: 'loading' })
      }
      const epochAtStart = mutationEpoch.current
      const previous = loadRef.current
      const killPromise = fetchKillSwitchFn(signal)
        .then((envelope) => (envelope.data ? envelope.data.active === true : null))
        .catch(() => null)
      const [positionResult, historyResult] = await Promise.allSettled([
        fetchPositionFn(positionId, signal),
        fetchHistoryFn(positionId, signal),
      ])
      if (signal.aborted || !isCurrent(generation)) {
        return
      }

      if (positionResult.status === 'rejected') {
        const error = positionResult.reason
        if (isAbortError(error)) {
          return
        }
        throw error
      }
      const position = positionResult.value.data
      if (!position) {
        throw new Error('The position response was empty.')
      }

      let history = previous.state === 'ready' ? previous.history : undefined
      let historyError: string | null = null
      if (historyResult.status === 'fulfilled') {
        const envelope = historyResult.value
        if (envelope.data) {
          history = envelope.data
        } else {
          historyError = 'The position history response was empty.'
        }
      } else {
        const error = historyResult.reason
        if (isAbortError(error)) {
          return
        }
        // A history-only failure never blanks the position. The last rendered
        // history (if any) is kept and the detail page shows a non-blocking
        // `history-refresh-warning`; with no history it shows the
        // `history-unavailable` panel instead.
        historyError = errorMessage(error)
      }

      // The immutable chain is loaded once per position. A manual revalidation
      // retries it, but a failure keeps the last successful chain (and chart)
      // and surfaces a non-blocking warning instead of blanking it.
      if (initial || refreshLinks || chainRef.current === null) {
        const chain = await fetchChain(position.source_trigger_id, signal)
        if (signal.aborted || !isCurrent(generation)) {
          return
        }
        const previousChain = chainRef.current
        if (chain.error && !initial && previousChain && previousChain.error === null) {
          setLinkRefreshError(chain.error)
        } else {
          chainRef.current = chain
          setLinkRefreshError(null)
        }
      }

      const killSwitch = await killPromise
      if (signal.aborted || !isCurrent(generation)) {
        return
      }
      // A poll that started before a management mutation may carry a pre-trade
      // snapshot; never let it regress the applied result for a cycle.
      if (epochAtStart !== mutationEpoch.current) {
        return
      }
      const chain = chainRef.current ?? EMPTY_CHAIN
      setLoad((current) => {
        if (current.state === 'ready' && !positionSupersedes(position, current.position)) {
          return current
        }
        return {
          state: 'ready',
          position,
          history,
          historyError,
          trigger: chain.trigger,
          opportunity: chain.opportunity,
          report: chain.report,
          linkError: chain.error,
          killSwitch,
        }
      })
      setRefreshError(null)
    },
    [positionId, fetchPositionFn, fetchHistoryFn, fetchChain, fetchKillSwitchFn, isCurrent],
  )

  // The initial load always runs, including while paused.
  useEffect(() => {
    const controller = new AbortController()
    const generation = begin()
    runLoad(controller.signal, true, false, generation).catch((error: unknown) => {
      if (controller.signal.aborted || isAbortError(error)) {
        return
      }
      // A stale initial rejection must never replace a ready page rendered by a
      // newer poll or retry.
      if (!isCurrent(generation)) {
        return
      }
      setLoad({
        state: 'error',
        message: errorMessage(error),
        notFound: error instanceof PositionApiError && error.httpStatus === 404,
      })
    })
    return () => {
      controller.abort()
    }
  }, [runLoad, retryToken, begin, isCurrent])

  const { revalidate } = usePolling(
    (signal, manual) => {
      const generation = begin()
      return runLoad(signal, false, manual, generation).catch((error: unknown) => {
        // A stale poll's failure is not the current page state.
        if (!isCurrent(generation)) {
          return
        }
        throw error
      })
    },
    {
      refreshMs,
      paused,
      onError: (error) => setRefreshError(errorMessage(error)),
    },
  )

  const retryInitial = useCallback((): void => {
    setLoad({ state: 'loading' })
    setRetryToken((value) => value + 1)
  }, [])

  const applyManagementPosition = useCallback((position: PositionData | null): void => {
    mutationEpoch.current += 1
    setLoad((previous) =>
      previous.state === 'ready'
        ? { ...previous, position: position ?? previous.position }
        : previous,
    )
  }, [])

  return {
    load,
    refreshError,
    linkRefreshError,
    retryInitial,
    revalidate,
    applyManagementPosition,
  }
}
