import type { TelegramReceipt, TelegramReceiptState } from '../api/telegram'

const STATE_COPY: Record<TelegramReceiptState, string> = {
  queued: '排队中',
  sending: '发送中',
  sent: '已送达',
  failed: '失败',
}

const ERROR_COPY: Record<string, string> = {
  TELEGRAM_TOKEN_INVALID: '401 Unauthorized',
  TELEGRAM_NOT_CONFIGURED: '未配置 token 或 chat',
  TELEGRAM_DELIVERY_FAILED: '网络 / 限流 / chat 被阻塞',
  TELEGRAM_CONFLICT: 'chat 不存在或 bot 被阻止',
}

function stateTone(state: TelegramReceiptState): string {
  if (state === 'sent') {
    return 'chip chip--ok'
  }
  if (state === 'failed') {
    return 'chip chip--danger'
  }
  if (state === 'sending') {
    return 'chip chip--warn'
  }
  return 'chip chip--muted'
}

/** One receipt's state chip with text (never color alone). */
export function TelegramReceiptStateChip({ state }: { state: TelegramReceiptState }) {
  return (
    <span className={stateTone(state)} data-testid={`telegram-receipt-state-${state}`}>
      {STATE_COPY[state]}
    </span>
  )
}

/** The stable, non-secret failure reason for one failed receipt. */
export function telegramReceiptSummary(receipt: TelegramReceipt): string {
  if (receipt.state === 'sent') {
    return receipt.message_id === null
      ? '已送达'
      : `message ${receipt.message_id} · ${receipt.sent_at ?? ''}`.trim()
  }
  if (receipt.state === 'failed') {
    const code = receipt.last_error_code ?? ''
    return ERROR_COPY[code] ?? (code || '投递失败；其他 chat 不受影响')
  }
  if (receipt.state === 'sending') {
    return '发送中；失败会退避重试'
  }
  return '排队中；未送达前不计为失败'
}

/** The latest test-send receipt (message id, time, or failure reason). */
export function TelegramTestReceipt({ receipt }: { receipt: TelegramReceipt | null }) {
  // Always mounted so a result inserted after an explicit send is announced by
  // assistive technology (WCAG 4.1.3); the visible detail follows it.
  const announcement =
    receipt === null
      ? '尚未发送测试消息。'
      : `测试消息${STATE_COPY[receipt.state]}：${telegramReceiptSummary(receipt)}`
  return (
    <>
      <p className="visually-hidden" role="status" data-testid="telegram-test-status">
        {announcement}
      </p>
      {receipt === null ? (
        <p className="status-message" data-testid="telegram-test-receipt-empty">
          尚未发送测试消息。测试发送是单独动作，不会自动执行。
        </p>
      ) : (
        <dl className="kv" data-testid="telegram-test-receipt">
          <dt>状态</dt>
          <dd>
            <TelegramReceiptStateChip state={receipt.state} />
          </dd>
          <dt>Message ID</dt>
          <dd className="mono">{receipt.message_id ?? '—'}</dd>
          <dt>送达时间</dt>
          <dd className="mono">{receipt.sent_at ?? '—'}</dd>
          <dt>原因</dt>
          <dd>{telegramReceiptSummary(receipt)}</dd>
        </dl>
      )}
    </>
  )
}

/** A bounded newest-first receipts table; each chat's state is independent. */
export function TelegramReceiptTable({ receipts }: { receipts: TelegramReceipt[] }) {
  if (receipts.length === 0) {
    return (
      <p className="status-message" data-testid="telegram-receipts-empty">
        尚无投递回执。测试发送或事件推送后会出现。
      </p>
    )
  }
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- scrollable table
    <div className="table-wrap" role="region" aria-label="Telegram 投递回执" tabIndex={0}>
      <table className="data-table" data-testid="telegram-receipts">
        <caption>Telegram 投递回执（最新在前，最多 6 条）</caption>
        <thead>
          <tr>
            <th scope="col">MESSAGE ID</th>
            <th scope="col">EVENT</th>
            <th scope="col">CHAT</th>
            <th scope="col">STATE</th>
            <th scope="col">TIME</th>
            <th scope="col">SUMMARY</th>
          </tr>
        </thead>
        <tbody>
          {receipts.map((receipt) => (
            <tr key={receipt.event_key}>
              <td className="mono">{receipt.message_id ?? '—'}</td>
              <td className="mono">{receipt.event_key}</td>
              <td className="mono">{receipt.chat_id}</td>
              <td>
                <TelegramReceiptStateChip state={receipt.state} />
              </td>
              <td className="mono">{receipt.sent_at ?? receipt.created_at}</td>
              <td className="explain">{telegramReceiptSummary(receipt)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
