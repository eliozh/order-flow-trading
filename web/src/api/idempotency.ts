/**
 * Shared session-scoped idempotency + CSRF helpers.
 *
 * Both mutating API clients (journal review, position management) need the same
 * behaviour: one stable key per logical request kept across retries, a fresh key
 * only for a deliberate new request, and the double-submit CSRF header on every
 * mutation. Only the storage prefix and the key-name prefix differ, so both
 * build their helpers through ``createPendingKeyStore``.
 */

/** Generate a fresh, prefixed Idempotency-Key for one logical request. */
export function newIdempotencyKey(prefix: string): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `${prefix}-${crypto.randomUUID()}`
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

/** Read one cookie value (browser only; null during SSR). */
export function readCookie(name: string): string | null {
  if (typeof document === 'undefined') {
    return null
  }
  const match = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name}=`))
  return match ? decodeURIComponent(match.slice(name.length + 1)) : null
}

/** The headers every same-origin mutation sends (CSRF double-submit + key). */
export function mutationHeaders(idempotencyKey: string): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'Idempotency-Key': idempotencyKey,
  }
  const csrf = readCookie('oft_csrf')
  if (csrf) {
    headers['X-CSRF-Token'] = csrf
  }
  return headers
}

export interface PendingKeyStore {
  /** The pending key for one id, or null when none is stored. */
  storedKey(id: string): string | null
  /** Create and store a new key (a deliberate new request). */
  rotateKey(id: string): string
  /** The stored key for this id, or a fresh one when none exists. */
  keyFor(id: string): string
  /** Forget the pending key after a success or a definitive refusal. */
  clearKey(id: string): void
}

/**
 * One session-scoped pending-key store.
 *
 * ``prefix`` seeds the generated key (``<prefix>-<id>-<uuid>``) and
 * ``keyPrefix`` names the sessionStorage entry. When sessionStorage is
 * unavailable (private mode, SSR) an in-memory fallback keeps the behaviour.
 */
export function createPendingKeyStore(prefix: string, keyPrefix: string): PendingKeyStore {
  const memoryKeys = new Map<string, string>()

  function storage(): Storage | null {
    if (typeof window === 'undefined') {
      return null
    }
    try {
      return window.sessionStorage
    } catch {
      return null
    }
  }

  function storageKey(id: string): string {
    return `${keyPrefix}${id}`
  }

  function storedKey(id: string): string | null {
    const store = storage()
    if (store) {
      try {
        const stored = store.getItem(storageKey(id))
        if (stored !== null) {
          return stored
        }
      } catch {
        // Fall through to the in-memory fallback.
      }
    }
    return memoryKeys.get(id) ?? null
  }

  function rotateKey(id: string): string {
    const key = newIdempotencyKey(`${prefix}-${id}`)
    const store = storage()
    if (store) {
      try {
        store.setItem(storageKey(id), key)
        memoryKeys.delete(id)
        return key
      } catch {
        // Fall through to the in-memory fallback.
      }
    }
    memoryKeys.set(id, key)
    return key
  }

  function keyFor(id: string): string {
    return storedKey(id) ?? rotateKey(id)
  }

  function clearKey(id: string): void {
    const store = storage()
    if (store) {
      try {
        store.removeItem(storageKey(id))
      } catch {
        // The in-memory fallback is cleared below regardless.
      }
    }
    memoryKeys.delete(id)
  }

  return { storedKey, rotateKey, keyFor, clearKey }
}
