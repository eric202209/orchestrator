import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const FRONTEND_URL = process.env.PHASE9E_FRONTEND_URL || 'http://127.0.0.1:3000';
const ARTIFACT_DIR =
  process.env.PHASE9E_ARTIFACT_DIR ||
  'docs/roadmap/reports/evidence-bundles/phase9e-frontend-ui-validation-20260513/screenshots';
const STORAGE_STATE =
  process.env.PHASE9E_STORAGE_STATE ||
  'docs/roadmap/reports/evidence-bundles/phase9e-frontend-ui-validation-20260513/storage-state.json';
const TEST_EMAIL = process.env.PHASE9E_EMAIL || 'phase9-sweep@example.com';
const TEST_PASSWORD = process.env.PHASE9E_PASSWORD || 'Phase9SweepPass!2026';

async function ensureSignedIn(page: Page) {
  let loginResponse = await page.request.post(`${FRONTEND_URL}/api/v1/auth/session/login`, {
    data: { email: TEST_EMAIL, password: TEST_PASSWORD },
  });
  for (let attempt = 0; loginResponse.status() === 429 && attempt < 3; attempt += 1) {
    await page.waitForTimeout(15_000);
    loginResponse = await page.request.post(`${FRONTEND_URL}/api/v1/auth/session/login`, {
      data: { email: TEST_EMAIL, password: TEST_PASSWORD },
    });
  }
  expect(loginResponse.ok(), `login status ${loginResponse.status()}`).toBeTruthy();
  await page.goto(`${FRONTEND_URL}/dashboard`);
  await page.waitForLoadState('networkidle');
  if (await page.getByRole('heading', { name: 'Sign in' }).isVisible()) {
    await page.getByPlaceholder('you@example.com').fill(TEST_EMAIL);
    await page.getByPlaceholder('••••••••').fill(TEST_PASSWORD);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeHidden({ timeout: 30_000 });
  }
}

async function expectNoHorizontalOverflow(page: Page, label: string) {
  const metrics = await page.evaluate(() => ({
    bodyScrollWidth: document.body.scrollWidth,
    bodyClientWidth: document.body.clientWidth,
    rootScrollWidth: document.documentElement.scrollWidth,
    rootClientWidth: document.documentElement.clientWidth,
    viewportWidth: window.innerWidth,
  }));
  const overflow =
    Math.max(metrics.bodyScrollWidth, metrics.rootScrollWidth) -
    Math.max(metrics.bodyClientWidth, metrics.rootClientWidth, metrics.viewportWidth);
  expect(overflow, `${label} horizontal overflow ${JSON.stringify(metrics)}`).toBeLessThanOrEqual(2);
}

async function capture(page: Page, name: string) {
  await page.screenshot({ path: `${ARTIFACT_DIR}/${name}.png`, fullPage: true });
}

async function firstSessionHref(page: Page) {
  return page
    .locator('a[href^="/sessions/"], a[href*="/sessions/"]')
    .evaluateAll((links) => {
      const hrefs = links
        .map((link) => link.getAttribute('href') || '')
        .filter((href) => /^\/sessions\/\d+/.test(href));
      return hrefs[0] || '';
    });
}

test.describe('Phase 9E UI sweep', () => {
  test.use({
    storageState: STORAGE_STATE,
  });

  test('authenticated UI journey covers routes, session recovery, and mobile layout', async ({ page }) => {
    test.setTimeout(180_000);
    await page.context().clearCookies();
    for (const route of ['/login', '/register']) {
      await page.goto(`${FRONTEND_URL}${route}`);
      await page.waitForLoadState('networkidle');
      await capture(page, route.replace('/', '') || 'root');
      await expectNoHorizontalOverflow(page, route);
    }

    await ensureSignedIn(page);

    for (const route of ['/dashboard', '/projects', '/sessions', '/tasks', '/settings']) {
      await page.goto(`${FRONTEND_URL}${route}`);
      await page.waitForLoadState('networkidle');
      await expect(page.locator('body')).not.toContainText('Could not validate credentials');
      await expect(page.locator('body')).not.toContainText('Failed to fetch');
      await capture(page, route.replace('/', '') || 'root');
      await expectNoHorizontalOverflow(page, route);
    }

    await page.goto(`${FRONTEND_URL}/projects`);
    await page.waitForLoadState('networkidle');
    const firstProjectHeading = page.locator('h3').first();
    await expect(firstProjectHeading, 'expected a project card').toBeVisible();
    await firstProjectHeading.click();
    await page.waitForURL(/\/projects\/\d+/);
    await page.waitForLoadState('networkidle');
    await capture(page, 'project-detail');
    await expectNoHorizontalOverflow(page, 'project detail');

    await page.goto(`${FRONTEND_URL}/sessions`);
    await page.waitForLoadState('networkidle');
    const sessionHref = await firstSessionHref(page);
    expect(sessionHref, 'expected a session detail link').toBeTruthy();

    await page.goto(`${FRONTEND_URL}${sessionHref}`);
    await page.waitForLoadState('networkidle');
    await capture(page, 'session-detail-initial');
    await expectNoHorizontalOverflow(page, 'session detail initial');

    for (const tab of ['Timeline', 'Tasks', 'Logs', 'Settings']) {
      await page.getByRole('button', { name: new RegExp(tab, 'i') }).click();
      await page.waitForTimeout(250);
      await capture(page, `session-detail-${tab.toLowerCase()}`);
      await expectNoHorizontalOverflow(page, `session detail ${tab}`);
    }

    await page.goto(`${FRONTEND_URL}/sessions`);
    await page.waitForLoadState('networkidle');
    await expect(page.getByRole('button', { name: 'All' })).toBeVisible();

    for (const filter of ['Active', 'Stopped']) {
      await page.getByRole('button', { name: filter }).click();
      await page.waitForTimeout(150);
      await expectNoHorizontalOverflow(page, `sessions ${filter.toLowerCase()} filter`);
    }

    await page.getByPlaceholder('Search...').fill('phase9');
    await page.waitForTimeout(150);
    await capture(page, 'sessions-filtered-search');
    await expectNoHorizontalOverflow(page, 'sessions filtered search');

    await page.goto(`${FRONTEND_URL}/sessions`);
    await page.waitForLoadState('networkidle');
    await page.getByRole('button', { name: 'Stopped' }).click();
    await page.waitForTimeout(150);
    const stoppedSessionHref = await firstSessionHref(page);
    expect(stoppedSessionHref, 'expected a stopped session detail link').toBeTruthy();

    const startedAt = Date.now();
    await page.goto(`${FRONTEND_URL}${stoppedSessionHref}`);
    await page.waitForLoadState('networkidle');
    await expect(page.getByText('Recovery needed')).toBeVisible({ timeout: 90_000 });
    console.log(`OpenClaw recovery summary visible in ${Date.now() - startedAt} ms`);

    await page.getByRole('button', { name: /Show details/i }).click();
    await expect(page.getByRole('button', { name: /Hide details/i })).toBeVisible();
    await capture(page, 'session-recovery-markdown-open');

    const openProjectArchitect = page.getByRole('button', { name: /Open Project Architect/i });
    if (await openProjectArchitect.isVisible()) {
      await openProjectArchitect.click();
    } else {
      const textarea = page.getByPlaceholder(/Focus on fixing/i);
      await textarea.fill('Phase 9E sweep: verify recovery handoff keeps operator guidance with the replan.');
      await page.getByRole('button', { name: /Save and Send to Project Architect|Send to Project Architect/i }).click();
    }

    await page.waitForURL(/\/projects\/\d+\?tab=planner/, { timeout: 60_000 });
    await expect(page.getByRole('button', { name: 'Project Architect' })).toBeVisible();
    await page.getByRole('button', { name: 'Replan Recovery' }).click();
    await expect(page.getByText('Recovery Sessions', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Interactive' }).click();
    await expect(page.getByPlaceholder(/Describe the feature/i)).toBeVisible();
    await page.getByRole('button', { name: 'Markdown' }).click();
    await expect(page.getByText('Markdown Planner')).toBeVisible();
    await capture(page, 'project-architect-recovery-handoff');
    await expectNoHorizontalOverflow(page, 'project architect recovery handoff');

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${FRONTEND_URL}${stoppedSessionHref}`);
    await page.waitForLoadState('networkidle');
    await page.getByRole('button', { name: /Timeline/i }).click();
    await page.waitForTimeout(250);
    await capture(page, 'mobile-session-detail-timeline');
    await expectNoHorizontalOverflow(page, 'mobile session timeline');
  });
});
