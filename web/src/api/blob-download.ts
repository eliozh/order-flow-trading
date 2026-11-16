/**
 * Trigger a browser download for a blob without navigating away.
 *
 * The object URL is revoked on the next task so the click has already started
 * the download; callers get a no-op where a DOM or object URLs are unavailable
 * (SSR; jsdom tests must stub `URL.createObjectURL`).
 */
export function triggerBlobDownload(blob: Blob, filename: string): void {
  if (typeof document === 'undefined' || typeof URL === 'undefined') {
    return
  }
  if (typeof URL.createObjectURL !== 'function') {
    return
  }
  const objectUrl = URL.createObjectURL(blob)
  const revoke = typeof URL.revokeObjectURL === 'function' ? URL.revokeObjectURL : null
  const anchor = document.createElement('a')
  anchor.href = objectUrl
  anchor.download = filename
  anchor.rel = 'noopener'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  if (revoke !== null) {
    window.setTimeout(() => revoke.call(URL, objectUrl), 0)
  }
}
