// The real-browser tests (test/browser). Chromium always; MENDWRIGHT_BROWSERS=chromium,firefox,webkit
// adds the others when their Playwright builds are installed. The pages are served by
// page.route() in each test, so nothing reaches the network.

import { defineConfig, devices } from '@playwright/test';

const wanted = (process.env.MENDWRIGHT_BROWSERS || 'chromium').split(',').map((b) => b.trim()).filter(Boolean);
const DEVICES: Record<string, (typeof devices)[string]> = {
  chromium: devices['Desktop Chrome'],
  firefox: devices['Desktop Firefox'],
  webkit: devices['Desktop Safari'],
};

export default defineConfig({
  testDir: './test/browser',
  timeout: 60_000,
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  reporter: process.env.CI ? [['list'], ['github']] : 'list',
  use: { headless: true },
  projects: wanted.map((name) => ({ name, use: { ...DEVICES[name] } })),
});
