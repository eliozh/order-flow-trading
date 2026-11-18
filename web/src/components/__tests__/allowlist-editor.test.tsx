import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { AllowlistEditor } from '../allowlist-editor'

interface HarnessProps {
  initial?: number[]
  allowNegative?: boolean
  maximum?: number
  onChangeSpy?: (values: number[]) => void
}

function Harness({ initial = [], allowNegative = true, maximum = 10, onChangeSpy }: HarnessProps) {
  const [values, setValues] = useState<number[]>(initial)
  return (
    <AllowlistEditor
      id="chat"
      label="Chat ID"
      hint="chat 必须是非零整数。"
      values={values}
      onChange={(next) => {
        onChangeSpy?.(next)
        setValues(next)
      }}
      readOnly={false}
      allowNegative={allowNegative}
      maximum={maximum}
    />
  )
}

function typeAndAdd(value: string): void {
  fireEvent.change(screen.getByTestId('chat-input'), { target: { value } })
  fireEvent.click(screen.getByTestId('chat-add'))
}

describe('AllowlistEditor — validation', () => {
  it('rejects non-integer input, ties the error to the input, and mutates nothing', () => {
    const onChangeSpy = vi.fn()
    render(<Harness initial={[1001]} onChangeSpy={onChangeSpy} />)

    typeAndAdd('abc')

    const input = screen.getByTestId('chat-input')
    expect(onChangeSpy).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('必须是')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAttribute('aria-describedby', 'chat-hint chat-error')
    expect(screen.getByRole('button', { name: '移除 Chat ID 1001' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '移除 Chat ID abc' })).toBeNull()
  })

  it('rejects zero and a negative user id', () => {
    const onChangeSpy = vi.fn()
    const { rerender } = render(<Harness allowNegative={false} onChangeSpy={onChangeSpy} />)

    typeAndAdd('0')
    expect(onChangeSpy).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('正整数')

    typeAndAdd('-5')
    expect(onChangeSpy).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('正整数')

    rerender(<Harness allowNegative onChangeSpy={onChangeSpy} />)
    typeAndAdd('0')
    expect(onChangeSpy).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('非零整数')
  })

  it('rejects input beyond the maximum without changing the list', () => {
    const onChangeSpy = vi.fn()
    render(<Harness initial={[1, 2, 3]} maximum={3} onChangeSpy={onChangeSpy} />)

    typeAndAdd('4')

    expect(onChangeSpy).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('最多 3 个 id')
    expect(screen.getAllByRole('button', { name: /移除 Chat ID/ })).toHaveLength(3)
  })

  it('sorts and de-duplicates accepted values', () => {
    const onChangeSpy = vi.fn()
    render(<Harness onChangeSpy={onChangeSpy} />)

    typeAndAdd('3003, 1001, 1001')

    expect(onChangeSpy).toHaveBeenCalledWith([1001, 3003])
    expect(
      screen
        .getAllByRole('button', { name: /移除 Chat ID/ })
        .map((button) => button.getAttribute('aria-label')),
    ).toEqual(['移除 Chat ID 1001', '移除 Chat ID 3003'])
  })
})

describe('AllowlistEditor — focus and announcements', () => {
  it('moves focus to the next remove button and announces a removal', () => {
    render(<Harness initial={[1001, 1002]} />)

    fireEvent.click(screen.getByRole('button', { name: '移除 Chat ID 1001' }))

    expect(screen.getByTestId('chat-announce')).toHaveTextContent('已移除 Chat ID 1001')
    expect(screen.getByRole('button', { name: '移除 Chat ID 1002' })).toHaveFocus()
  })

  it('moves focus to the add input when the last chip is removed', () => {
    render(<Harness initial={[1001]} />)

    fireEvent.click(screen.getByRole('button', { name: '移除 Chat ID 1001' }))

    expect(screen.getByTestId('chat-announce')).toHaveTextContent('已移除 Chat ID 1001')
    expect(screen.getByTestId('chat-input')).toHaveFocus()
  })

  it('announces an added chip through the live region', () => {
    render(<Harness />)

    typeAndAdd('1001')

    expect(screen.getByTestId('chat-announce')).toHaveAttribute('role', 'status')
    expect(screen.getByTestId('chat-announce')).toHaveTextContent('已添加 Chat ID 1001')
  })
})
