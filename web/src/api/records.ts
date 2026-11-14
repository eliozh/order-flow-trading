/** Read one scalar out of an untyped API record, or null when it is absent. */
export function recordField(
  record: Record<string, unknown> | null | undefined,
  key: string,
): string | null {
  const value = record?.[key]
  return typeof value === 'string' || typeof value === 'number' ? String(value) : null
}
