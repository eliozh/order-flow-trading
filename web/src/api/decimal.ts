/** Exact decimal string addition used by CALC summaries (no binary floats). */
export function addDecimals(values: readonly string[]): string {
  let scale = 0
  const parsed = values.map((raw) => {
    const negative = raw.startsWith('-')
    const unsigned = negative ? raw.slice(1) : raw
    if (!/^\d+(?:\.\d+)?$/.test(unsigned)) {
      throw new Error(`invalid decimal: ${raw}`)
    }
    const [whole, fraction = ''] = unsigned.split('.')
    scale = Math.max(scale, fraction.length)
    return { negative, whole, fraction }
  })
  let total = 0n
  for (const value of parsed) {
    const padded = value.fraction.padEnd(scale, '0')
    const magnitude = BigInt(`${value.whole}${padded || ''}`)
    total += value.negative ? -magnitude : magnitude
  }
  if (scale === 0) {
    return total.toString()
  }
  const negative = total < 0n
  const digits = (negative ? -total : total).toString().padStart(scale + 1, '0')
  const whole = digits.slice(0, digits.length - scale)
  const fraction = digits.slice(digits.length - scale)
  return `${negative ? '-' : ''}${whole}.${fraction}`
}
