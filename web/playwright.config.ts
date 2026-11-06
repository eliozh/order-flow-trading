import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.OFT_E2E_BASE_URL ?? 'http://127.0.0.1:7622'
const screensDir = process.env.OFT_E2E_SCREENS_DIR ?? '../.claude/ecc-workflow/screens/e2e'

/**
 * Chromium-only E2E config for the walking-skeleton `/system` journey.
 *
 * The daemon is started by `e2e/run-e2e.sh` (temporary data dir, fixture scan
 * already run) so the suite itself is read-only. Run it with:
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
    ['junit', { outputFile: `${screensDir}/report/junit.xml` }],
    ['html', { outputFolder: `${screensDir}/report/html`, open: 'never' }],
  ],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
