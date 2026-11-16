import type {
  BudgetEnvelope,
  BudgetUpdateEnvelope,
  BudgetUpdatePayload,
} from '../api/budget'
import type { OnboardingProgressData } from '../api/onboarding'
import type { ScanSettingsEnvelope } from '../api/settings'
import type { StarEnvelope, WatchlistEnvelope } from '../api/watchlist'
import {
  BudgetPanel,
  IssueSummary,
  ReviewPanel,
  SchedulePanel,
  StarList,
  Step5SavedPanel,
} from './onboarding-watchlist-panels'
import { focusField, useWatchlistDraft } from './use-watchlist-draft'

export interface WatchlistStepProps {
  fetchWatchlistFn: (signal?: AbortSignal) => Promise<WatchlistEnvelope>
  setStarFn: (symbol: string, starred: boolean, signal?: AbortSignal) => Promise<StarEnvelope>
  fetchScanSettingsFn: (signal?: AbortSignal) => Promise<ScanSettingsEnvelope>
  updateScanPeriodFn: (
    period: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<ScanSettingsEnvelope>
  fetchBudgetFn: (signal?: AbortSignal) => Promise<BudgetEnvelope>
  updateBudgetThresholdsFn: (
    payload: BudgetUpdatePayload,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<BudgetUpdateEnvelope>
  idempotencyKeyFn: (label: string) => string
  clearIdempotencyKeyFn: (label: string) => void
  rotateIdempotencyKeyFn: (label: string) => string
  announce: (message: string) => void
  /** Resolves with the server progress (or null on failure). */
  onFinish: () => Promise<OnboardingProgressData | null>
  finishing: boolean
  narrow: boolean
  /** The server says every active step is complete: render the review. */
  allComplete: boolean
}

/**
 * Step 5: watchlist, scan period, budget, and the review.
 *
 * Reuses the real watchlist/scan/budget clients, persists only changed values,
 * discloses truncation and data quality, and focuses the review heading when
 * the wizard finishes. A background star failure is announced inline without
 * stealing focus; submit/validation failures focus the field-scoped summary.
 * The draft/mutation state lives in `useWatchlistDraft`; the sections are
 * presentational (see `onboarding-watchlist-panels`).
 */
export function WatchlistStep({
  fetchWatchlistFn,
  setStarFn,
  fetchScanSettingsFn,
  updateScanPeriodFn,
  fetchBudgetFn,
  updateBudgetThresholdsFn,
  idempotencyKeyFn,
  clearIdempotencyKeyFn,
  rotateIdempotencyKeyFn,
  announce,
  onFinish,
  finishing,
  narrow,
  allComplete,
}: WatchlistStepProps) {
  const draft = useWatchlistDraft({
    fetchWatchlistFn,
    setStarFn,
    fetchScanSettingsFn,
    updateScanPeriodFn,
    fetchBudgetFn,
    updateBudgetThresholdsFn,
    idempotencyKeyFn,
    clearIdempotencyKeyFn,
    rotateIdempotencyKeyFn,
    announce,
    onFinish,
    finishing,
    narrow,
    allComplete,
  })

  return (
    <div className="stack">
      {draft.state === 'loading' && (
        <p className="status-message" role="status">
          加载引导数据…
        </p>
      )}
      {draft.state === 'error' && (
        <>
          <p className="status-message status-message--error" role="alert">
            引导数据不可用：{draft.loadMessage ?? '未知错误'}
          </p>
          <div className="settings-actions">
            <button type="button" className="control-btn" onClick={draft.retry}>
              重试
            </button>
          </div>
        </>
      )}

      {(draft.issues.length > 0 || draft.summaryMessage !== '') && (
        <IssueSummary
          issues={draft.issues}
          summaryLead={draft.summaryLead}
          summaryMessage={draft.summaryMessage}
          summaryRef={draft.summaryRef}
          onFocusField={focusField}
        />
      )}

      {draft.starError !== null && (
        <p
          className="status-message status-message--error"
          role="status"
          data-testid="onboarding-star-error"
        >
          {draft.starError}
        </p>
      )}

      {draft.state === 'ready' && (
        <form
          className="stack"
          aria-label="Watchlist、周期与预算"
          noValidate
          onSubmit={(event) => {
            event.preventDefault()
            if (!draft.reviewLocked) {
              void draft.finish()
            }
          }}
        >
          <StarList
            visibleRows={draft.visibleRows}
            pendingStars={draft.pendingStars}
            disabled={draft.narrowDisabled}
            reviewLocked={draft.reviewLocked}
            dataQuality={draft.dataQuality}
            starredCount={draft.starredCount}
            rowsDisclosed={draft.rowsDisclosed}
            total={draft.total}
            truncated={draft.truncated}
            onToggle={(row) => void draft.toggleStar(row)}
          />

          <SchedulePanel
            scan={draft.scan}
            periodDraft={draft.periodDraft}
            periodInvalid={draft.periodInvalid}
            disabled={draft.narrowDisabled}
            reviewLocked={draft.reviewLocked}
            onChange={draft.changePeriod}
          />

          <BudgetPanel
            budgetDraft={draft.budgetDraft}
            issueFor={draft.issueFor}
            disabled={draft.narrowDisabled}
            reviewLocked={draft.reviewLocked}
            onChange={draft.changeBudget}
          />

          <section className="panel" aria-labelledby="onboarding-env-heading">
            <div className="panel-head">
              <h2 id="onboarding-env-heading">环境</h2>
            </div>
            <p className="status-message">
              <span className="chip chip--ok">PAPER</span> Live 未启用；引导不提供 Live 选项。
            </p>
          </section>

          {!draft.reviewLocked && (
            <div className="settings-actions">
              <button type="submit" className="control-btn" disabled={draft.narrowDisabled}>
                {draft.busy ? '正在完成…' : '完成引导并查看首次扫描'}
              </button>
              <span className="hint">
                完成后下一次扫描运行；等同 CLI <code>oft scan run</code>。
              </span>
            </div>
          )}

          {!draft.reviewLocked && draft.savedResumedStep !== null && (
            <Step5SavedPanel resumedStep={draft.savedResumedStep} />
          )}

          {draft.reviewLocked && draft.scan && draft.budget && (
            <ReviewPanel
              scan={draft.scan}
              budget={draft.budget}
              starredCount={draft.starredCount}
              reviewRef={draft.reviewRef}
            />
          )}
        </form>
      )}
    </div>
  )
}
