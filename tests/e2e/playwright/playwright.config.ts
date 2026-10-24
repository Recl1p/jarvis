import { defineConfig } from '@playwright/test';

// Addresses come from tests/e2e/run.sh, which starts the containers on one Docker network.
const FRESH = process.env.JARVIS_FRESH || 'http://jarvis-fresh:8642';
const EXT = process.env.JARVIS_EXT || 'http://jarvis-ext:8642';
const LIVE = process.env.JARVIS_LIVE || '';

export default defineConfig({
  testDir: './specs',
  // The phases share one JARVIS each and build on each other (set up -> install -> use), so one worker, in order.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  // One report and artifact folder per phase (run.sh sets PHASE), so a later phase doesn't wipe an earlier one's.
  reporter: [['list'], ['html', { outputFolder: `report/${process.env.PHASE || 'all'}`, open: 'never' }],
    ['junit', { outputFile: `results/${process.env.PHASE || 'all'}/junit.xml` }]],
  outputDir: `results/${process.env.PHASE || 'all'}/artifacts`,
  use: {
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    viewport: { width: 1440, height: 900 },
    actionTimeout: 20_000,
  },
  projects: [
    { name: 'setup', testMatch: /(05-login|10-setup)\.spec\.ts/, use: { baseURL: FRESH } },
    { name: 'install', testMatch: /20-install\.spec\.ts/, dependencies: ['setup'], use: { baseURL: FRESH } },
    { name: 'dashboard', testMatch: /(30-dashboard|40-api|50-settings)\.spec\.ts/, dependencies: ['install'], use: { baseURL: FRESH } },
    { name: 'external', testMatch: /60-external\.spec\.ts/, use: { baseURL: EXT } },
    ...(LIVE ? [{ name: 'smoke', testMatch: /90-smoke\.spec\.ts/, use: { baseURL: LIVE } }] : []),
  ],
});
