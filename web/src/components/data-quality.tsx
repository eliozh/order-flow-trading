/** Data-quality stamps with an explicit, non-color explanation (DESIGN §13.2). */

interface DataQualityBadgeProps {
  status: string
  reasons?: string[]
}

const EXPLANATIONS: Record<string, string> = {
  COMPLETE: 'All required inputs are present and fresh.',
  PARTIAL: 'Some inputs are missing; the metric is still usable and marked PARTIAL.',
  STALE: 'Present but older than its freshness budget.',
  MISSING: 'Required input does not exist. No chart is drawn from fabricated data.',
  ESTIMATED: 'Derived by documented resampling; never presented as measured.',
}

const CLASS_NAMES: Record<string, string> = {
  COMPLETE: 'chip chip--ok',
  PARTIAL: 'chip chip--warn',
  STALE: 'chip chip--warn',
  ESTIMATED: 'chip chip--warn',
  MISSING: 'chip chip--danger',
}

export function DataQualityBadge({ status, reasons = [] }: DataQualityBadgeProps) {
  const explanation = EXPLANATIONS[status] ?? 'Unknown data quality.'
  return (
    <span className="quality" data-quality={status}>
      <span
        className={CLASS_NAMES[status] ?? 'chip'}
        role="note"
        lang="en"
        aria-label={`Data quality ${status}. ${explanation}`}
      >
        {status}
      </span>
      {status !== 'COMPLETE' && (
        <span className="quality-reason" lang="en">
          {explanation}
        </span>
      )}
      {reasons.length > 0 && (
        <span className="quality-reason" lang="en">
          {reasons.length} quality reason{reasons.length === 1 ? '' : 's'} recorded.
        </span>
      )}
    </span>
  )
}
