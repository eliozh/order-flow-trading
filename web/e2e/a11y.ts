import AxeBuilder from '@axe-core/playwright'
import { expect, type Page } from '@playwright/test'

/**
 * One shared WCAG 2.2 A/AA tag set for every route audit.
 *
 * ``wcag22aa`` is the only WCAG 2.2 tag axe-core defines; there is no
 * ``wcag22a``. The rules under that tag include SC 2.5.8 target size, but
 * axe-core ships ``target-size`` disabled, so every audit below enables it
 * explicitly and then proves the rule actually ran.
 */
export const AXE_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] as const

export type AxeAuditResults = Awaited<ReturnType<AxeBuilder['analyze']>>

export interface AxeAuditOptions {
  /** Restrict the audit to one subtree, for example the route content. */
  include?: string
  /** Known, pre-existing violation ids that this route may carry. */
  allow?: readonly string[]
}

/**
 * Audit the current page with the shared tags and the enabled target-size rule.
 *
 * Violations not named in ``allow`` fail the test. The returned results are the
 * raw axe results so callers can write their per-route JSON artifact.
 */
export async function runAxeAudit(
  page: Page,
  label: string,
  options: AxeAuditOptions = {},
): Promise<AxeAuditResults> {
  let builder = new AxeBuilder({ page })
  if (options.include !== undefined) {
    builder = builder.include(options.include)
  }
  const results = await builder
    .options({
      runOnly: { type: 'tag', values: [...AXE_TAGS] },
      rules: { 'target-size': { enabled: true } },
    })
    .analyze()

  const allowed = options.allow ?? []
  const violations = results.violations.filter((violation) => !allowed.includes(violation.id))
  const summary = violations.map(
    (violation) =>
      `${violation.id} (${violation.impact ?? 'unknown'}): ${violation.nodes
        .map((node) => node.target.join(' '))
        .join(' | ')}`,
  )
  expect(violations, `${label} axe violations:\n${summary.join('\n')}`).toEqual([])

  const targetSizeRan = [...results.passes, ...results.incomplete, ...results.violations].some(
    (result) => result.id === 'target-size',
  )
  expect(targetSizeRan, `${label}: target-size rule did not run`).toBe(true)
  return results
}
