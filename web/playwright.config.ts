import { defineConfig, devices } from '@playwright/test'

const systemBaseURL = process.env.OFT_E2E_BASE_URL ?? 'http://127.0.0.1:7622'
const symbolsBaseURL = process.env.OFT_E2E_SYMBOLS_BASE_URL ?? 'http://127.0.0.1:7632'
const reportsBaseURL = process.env.OFT_E2E_REPORT_BASE_URL ?? 'http://127.0.0.1:7642'
const screensDir = process.env.OFT_E2E_SCREENS_DIR ?? '../.claude/ecc-workflow/screens/e2e02'
const reportDir = process.env.OFT_E2E_REPORT_DIR ?? `${screensDir}/report`

/**
 * Chromium-only E2E config for the walking-skeleton `/system` journey, the
 * slice-02 read-only symbol registry journeys, the slice-08 report journeys,
 * the slice-29 positions journey, the slice-30 journal journeys, and the
 * slice-33 read-only navigation-shell journey.
 *
 * Three daemons are started by `e2e/run-e2e.sh` (temporary data dirs, fixture
 * scan, exchangeInfo seed, backfill/report, one seeded OPEN paper position, and
 * one seeded CLOSED paper trade already applied). All suites are read-only
 * except the positions journey (one paper management POST) and the journal
 * journey (one advisory review POST), both against the reports daemon. Run
 * them with:
 *
 *   npm --prefix web run test:e2e
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  outputDir: `${screensDir}/artifacts`,
  reporter: [
    ['list'],
    ['junit', { outputFile: `${reportDir}/junit.xml` }],
    ['html', { outputFolder: `${reportDir}/html`, open: 'never' }],
  ],
  use: {
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'positions',
      testMatch: /positions\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], baseURL: reportsBaseURL },
    },
    {
      name: 'journal',
      testMatch: /journal\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], baseURL: reportsBaseURL },
    },
    {
      // Read-only shell journey over the reports daemon, which already serves
      // every existing route and seeds the data those routes render.
      name: 'navigation',
      testMatch: /navigation\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], baseURL: reportsBaseURL },
    },
    {
      name: 'system',
      testMatch: /system\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], baseURL: systemBaseURL },
    },
    {
      name: 'symbols',
      testMatch: /symbols\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], baseURL: symbolsBaseURL },
    },
    {
      name: 'reports',
      testMatch: /reports\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], baseURL: reportsBaseURL },
    },
    {
      // Slice 38: watchlist star/search journey over the symbols daemon. Last
      // on purpose so the read-only symbols suite observes its seeded stars
      // before this suite mutates them.
      name: 'watchlist',
      testMatch: /watchlist\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], baseURL: symbolsBaseURL },
    },
  ],
})
