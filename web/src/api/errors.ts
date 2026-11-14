/** A safe, user-facing message for an unknown thrown value. */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Unknown error'
}
