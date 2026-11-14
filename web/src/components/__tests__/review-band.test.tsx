import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { JournalReviewData } from '../../api/journal'
import { ReviewBand, ReviewProvenance } from '../review-band'

function reviewFixture(overrides: Partial<JournalReviewData> = {}): JournalReviewData {
  return {
    trade_id: 'TRD-1',
    decision_id: 'REV-1',
    status: 'FOUND',
    advisory: true,
    rationale: '入场与信号一致；滑点未越过失效线。',
    caveats: ['单样本不构成优势', '仅建议，不自动修改规则'],
    confidence: 0.62,
    validation_status: 'VALID',
    provenance: {
      provider: 'stub',
      model: 'fixture',
      prompt_id: 'review',
      prompt_version: 1,
      cost: { amount_usd: '0.0100', currency: 'USD' },
      usage: { total_tokens: 120 },
      knowledge_artifact_hash: 'artifact-hash',
    },
    reason: null,
    idempotent_replay: false,
    ...overrides,
  }
}

describe('ReviewBand', () => {
  it('labels the band advisory and never offers a rule-change action', () => {
    render(<ReviewBand review={reviewFixture()} onGenerate={vi.fn()} />)

    expect(
      screen.getByRole('heading', { name: 'LLM 复盘（仅建议，不自动修改规则）' }),
    ).toBeInTheDocument()
    expect(screen.getAllByText(/仅建议，不自动修改规则/).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: /应用|apply/i })).not.toBeInTheDocument()
  })

  it('renders a FOUND review as text with confidence, provenance, and a regenerate control', () => {
    const onGenerate = vi.fn()
    render(<ReviewBand review={reviewFixture()} onGenerate={onGenerate} />)

    expect(screen.getByText('入场与信号一致；滑点未越过失效线。')).toBeInTheDocument()
    expect(screen.getByText('单样本不构成优势')).toBeInTheDocument()
    expect(screen.getByText('0.62')).toBeInTheDocument()
    expect(screen.getByText('中')).toBeInTheDocument()
    expect(screen.getByText('VALID')).toBeInTheDocument()
    expect(screen.getByText('REV-1')).toBeInTheDocument()
    expect(screen.getByText('stub')).toBeInTheDocument()
    expect(screen.getByText('fixture')).toBeInTheDocument()
    expect(screen.getByText(/review v1/)).toBeInTheDocument()
    expect(screen.getByText(/0\.0100 USD/)).toBeInTheDocument()
    expect(screen.getByText('120')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /重新生成/ }))
    expect(onGenerate).toHaveBeenCalledTimes(1)
  })

  it('renders a MISSING review with a generate control', () => {
    const onGenerate = vi.fn()
    render(
      <ReviewBand
        review={reviewFixture({
          status: 'MISSING',
          decision_id: null,
          rationale: null,
          confidence: null,
          validation_status: null,
          provenance: null,
          caveats: [],
          reason: 'no advisory review is linked to this trade',
        })}
        onGenerate={onGenerate}
      />,
    )

    expect(screen.getByText(/尚未生成复盘/)).toBeInTheDocument()
    expect(screen.getByText(/no advisory review is linked/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /生成复盘/ }))
    expect(onGenerate).toHaveBeenCalledTimes(1)
  })

  it('renders a PARTIAL review reason without advisory text or an action', () => {
    render(
      <ReviewBand
        review={reviewFixture({
          status: 'PARTIAL',
          decision_id: 'REV-1',
          rationale: null,
          confidence: null,
          validation_status: null,
          provenance: null,
          caveats: [],
          reason: 'the linked decision is not a REVIEW',
        })}
        onGenerate={vi.fn()}
      />,
    )

    expect(screen.getByText(/the linked decision is not a REVIEW/)).toBeInTheDocument()
    expect(screen.queryByText('入场与信号一致；滑点未越过失效线。')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /重新生成/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /生成复盘/ })).not.toBeInTheDocument()
  })

  it('shows a retry that reuses the logical request after a POST failure', () => {
    const onGenerate = vi.fn()
    render(
      <ReviewBand
        review={reviewFixture({
          status: 'MISSING',
          decision_id: null,
          rationale: null,
          confidence: null,
          validation_status: null,
          provenance: null,
          caveats: [],
          reason: 'no advisory review is linked to this trade',
        })}
        actionError="review provider down"
        actionErrorKind="unknown"
        onGenerate={onGenerate}
      />,
    )

    const alert = screen.getByTestId('journal-review-error')
    expect(alert).toHaveTextContent(/review provider down/)
    expect(alert).toHaveTextContent(/结果未知/)
    expect(alert).toHaveTextContent(/Idempotency-Key/)
    // The retry control is the single stable action button, not a button
    // nested inside the alert region.
    expect(alert).not.toContainElement(screen.getByRole('button', { name: /重试生成/ }))
    fireEvent.click(screen.getByRole('button', { name: /重试生成/ }))
    expect(onGenerate).toHaveBeenCalledTimes(1)
  })

  it('shows a definitive failure without claiming the outcome is unknown', () => {
    render(
      <ReviewBand
        review={reviewFixture()}
        actionError="schema validation failed"
        actionErrorKind="definitive"
        onGenerate={vi.fn()}
      />,
    )

    const alert = screen.getByTestId('journal-review-error')
    expect(alert).toHaveTextContent(/schema validation failed/)
    expect(alert).not.toHaveTextContent(/结果未知/)
  })

  it('notes an idempotent replay', () => {
    render(<ReviewBand review={reviewFixture({ idempotent_replay: true })} onGenerate={vi.fn()} />)

    expect(screen.getByTestId('journal-review-replay')).toHaveTextContent('幂等重放：复用已存结果')
  })

  it('keeps the generation control focusable and described in read-only mode', () => {
    const onGenerate = vi.fn()
    render(<ReviewBand review={reviewFixture()} readonly onGenerate={onGenerate} />)

    const button = screen.getByRole('button', { name: /重新生成/ })
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button).not.toBeDisabled()
    expect(button).toHaveAccessibleDescription(/移动端只读/)
    button.focus()
    expect(button).toHaveFocus()
    fireEvent.click(button)
    expect(onGenerate).not.toHaveBeenCalled()
    expect(screen.getByText(/移动端只读/)).toBeInTheDocument()
  })

  it('renders a read failure with a retry-read control', () => {
    const onRetryRead = vi.fn()
    render(
      <ReviewBand
        review={null}
        readError="review read down"
        onGenerate={vi.fn()}
        onRetryRead={onRetryRead}
      />,
    )

    expect(screen.getByText(/复盘读取失败：review read down/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /重试读取复盘/ }))
    expect(onRetryRead).toHaveBeenCalledTimes(1)
  })

  it('keeps a FOUND review visible when a refresh read fails', () => {
    render(
      <ReviewBand
        review={reviewFixture()}
        readError="review refresh down"
        onGenerate={vi.fn()}
        onRetryRead={vi.fn()}
      />,
    )

    expect(screen.getByText('入场与信号一致；滑点未越过失效线。')).toBeInTheDocument()
    expect(screen.getByText('单样本不构成优势')).toBeInTheDocument()
    expect(screen.getByText(/复盘读取失败：review refresh down/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重试读取复盘/ })).toBeInTheDocument()
  })

  it('marks the generation body busy, keeps the live region outside it, and shows visible progress', () => {
    render(
      <ReviewBand
        review={reviewFixture({
          status: 'MISSING',
          decision_id: null,
          rationale: null,
          confidence: null,
          validation_status: null,
          provenance: null,
          caveats: [],
          reason: 'no advisory review is linked to this trade',
        })}
        generating
        onGenerate={vi.fn()}
      />,
    )

    const busyBody = screen.getByTestId('review-band-body')
    expect(busyBody).toHaveAttribute('aria-busy', 'true')
    const status = screen.getByTestId('journal-review-status')
    expect(status).toHaveTextContent('正在生成复盘…')
    // A polite live region nested in an aria-busy subtree is not announced.
    expect(busyBody).not.toContainElement(status)
    expect(status).toHaveAttribute('aria-live', 'polite')
    // The progress is visible, not only visually hidden.
    expect(screen.getByTestId('journal-review-generating')).toHaveTextContent('正在生成复盘…')
    const button = screen.getByRole('button', { name: /生成复盘/ })
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button).not.toBeDisabled()
  })

  it('keeps the action button focusable and guarded while generating', () => {
    const onGenerate = vi.fn()
    render(<ReviewBand review={reviewFixture()} generating onGenerate={onGenerate} />)

    const button = screen.getByRole('button', { name: /重新生成/ })
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button).not.toBeDisabled()
    fireEvent.click(button)
    expect(onGenerate).not.toHaveBeenCalled()
  })

  it('announces a completed generation with the decision id', () => {
    render(
      <ReviewBand review={reviewFixture()} announcement="复盘已生成：REV-9" onGenerate={vi.fn()} />,
    )

    expect(screen.getByTestId('journal-review-status')).toHaveTextContent('复盘已生成：REV-9')
    expect(screen.getByTestId('review-band-body')).toHaveAttribute('aria-busy', 'false')
  })

  it('keeps the retry action visible when a background read also failed', () => {
    const onGenerate = vi.fn()
    render(
      <ReviewBand
        review={reviewFixture({
          status: 'MISSING',
          decision_id: null,
          rationale: null,
          confidence: null,
          validation_status: null,
          provenance: null,
          caveats: [],
          reason: 'no advisory review is linked to this trade',
        })}
        readError="review read down"
        actionError="review provider down"
        actionErrorKind="unknown"
        onGenerate={onGenerate}
        onRetryRead={vi.fn()}
      />,
    )

    expect(screen.getByText(/复盘读取失败：review read down/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /重试读取复盘/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /重试生成/ }))
    expect(onGenerate).toHaveBeenCalledTimes(1)
  })

  it('renders an undefined confidence as an em dash without throwing', () => {
    render(
      <ReviewBand
        review={reviewFixture({ confidence: undefined as unknown as number | null })}
        onGenerate={vi.fn()}
      />,
    )

    expect(screen.getByText('Decision')).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('drops unknown provenance keys and renders allowlisted hostile values as text', () => {
    const { container } = render(
      <ReviewProvenance
        provenance={{
          provider: 'stub',
          model: '<img src=x onerror="alert(1)">',
          prompt_id: 'javascript:alert(1)',
          system_prompt: 'leaked system prompt',
          prompt_body: 'leaked prompt body',
          api_key: 'sk-live-secret',
          secret_key: 'super-secret',
        }}
      />,
    )

    // Unknown keys are never rendered.
    expect(screen.queryByText(/leaked system prompt/)).not.toBeInTheDocument()
    expect(screen.queryByText(/leaked prompt body/)).not.toBeInTheDocument()
    expect(screen.queryByText(/sk-live-secret/)).not.toBeInTheDocument()
    expect(screen.queryByText(/super-secret/)).not.toBeInTheDocument()
    // Only allowlisted rows render.
    const terms = container.querySelectorAll('dt')
    expect(Array.from(terms).map((term) => term.textContent)).toEqual([
      'Provider',
      'Model',
      'Prompt',
    ])
    // Allowlisted hostile values render as text nodes, never HTML.
    expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeInTheDocument()
    expect(screen.getByText('javascript:alert(1)')).toBeInTheDocument()
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
  })
})
