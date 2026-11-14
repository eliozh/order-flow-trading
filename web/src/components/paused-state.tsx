interface PausedStampProps {
  testId?: string
}

/** The visible DESIGN A6 PAUSED stamp shared by the paused surfaces. */
export function PausedStamp({ testId }: PausedStampProps) {
  return (
    <span className="stamp stamp--stale" lang="en" data-testid={testId}>
      PAUSED
    </span>
  )
}

interface PausedPanelProps {
  testId?: string
}

/**
 * The shared paused-state panel for the positions list and detail.
 *
 * It is intentionally not a live region (``role="note"``): the single global
 * announcement lives in the status bar's ``role="status"`` region, so a pause
 * is announced exactly once rather than once per page panel.
 */
export function PausedPanel({ testId }: PausedPanelProps) {
  return (
    <div className="panel" role="note" data-testid={testId}>
      <div className="panel-head">
        <h2>实时更新已暂停</h2>
        <PausedStamp />
      </div>
      <p className="panel-foot">
        自动刷新、窗口聚焦与标签页可见性触发的重新校验均已冻结；已发出的请求已中止。按状态栏的
        “继续实时更新”恢复，恢复后会立即重新校验一次。
      </p>
    </div>
  )
}
