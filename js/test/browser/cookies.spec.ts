// The cookies preset and interruptions in a real browser: the preset's XPaths run in the
// browser's own engine, a banner over the page is handled before the action, and one that
// opens while the action waits gets the action tried once more.

import { open } from 'mendwright';
import { expect, test } from 'mendwright/test';

import { ONETRUST, SIGNUP_V1, serve, tempStore } from './helpers';

const page1 = (banner: string) => SIGNUP_V1.replace('<main>', '<main>' + banner);

test.describe('the cookies preset', () => {
  test.use({ mendwrightOptions: { cookies: 'reject', timeoutMs: 1000 }, mendwrightStore: tempStore('cookies') });

  test("rejects a OneTrust banner that covers the page before the action", async ({ page, heal }) => {
    await serve(page, { '/signup': page1(ONETRUST) });
    await page.fill('#email', 'ada@example.com');
    expect(await page.evaluate(() => (window as any).consent)).toBe('reject');
    expect(heal.handledInterruptions).toEqual([{ key: 'cookie_banner', before: '#email' }]);
  });

  test('rejects a banner no listed manager made, by its button text', async ({ page }) => {
    const banner = `<div class="cookie-consent" style="position: fixed; inset: 0; background: #fff">This site uses cookies.
      <button onclick="this.parentElement.remove(); window.consent = 'necessary'">Only necessary</button>
      <button onclick="this.parentElement.remove(); window.consent = 'all'">Accept all cookies</button></div>`;
    await serve(page, { '/signup': page1(banner) });
    await page.check('#terms');
    expect(await page.evaluate(() => (window as any).consent)).toBe('necessary');
  });

  test('clicks nothing on a consent screen that is not about cookies', async ({ page }) => {
    const oauth = `<div id="consent" role="dialog">Shop wants to see your profile.
      <button onclick="window.clicked = 'deny'">Deny</button><button onclick="window.clicked = 'allow'">Allow</button></div>`;
    await serve(page, { '/signup': page1(oauth) });
    await page.fill('#email', 'x');
    expect(await page.evaluate(() => (window as any).clicked)).toBeUndefined();
  });
});

test("cookies: 'accept' accepts", async ({ page }) => {
  await serve(page, { '/signup': page1(ONETRUST) });
  const heal = open(page, { cookies: 'accept', storePath: tempStore('accept'), timeoutMs: 1000 });
  await heal.locator('email', '#email').fill('x');
  expect(await page.evaluate(() => (window as any).consent)).toBe('accept');
});

test('a popup registered with heal.interrupt() is closed before the action', async ({ page }) => {
  const late = `<script>setTimeout(() => {
    const popup = document.createElement('div');
    popup.id = 'nl';
    popup.style.cssText = 'position: fixed; inset: 0; background: #fff';
    const close = document.createElement('button');
    close.id = 'nl-close';
    close.textContent = 'No thanks';
    close.onclick = () => popup.remove();
    popup.append('Join us ', close);
    document.body.prepend(popup);
  }, 300)</script>`;
  await serve(page, { '/signup': page1(late) });
  const heal = open(page, { storePath: tempStore('late'), timeoutMs: 1000 });
  heal.interrupt('newsletter', '#nl-close');
  await page.locator('#nl').waitFor();
  await heal.locator('email', '#email').fill('ada@example.com', { timeout: 1500 });
  await expect(page.locator('#email')).toHaveValue('ada@example.com');
  expect(heal.handledInterruptions.map((e) => e.key)).toEqual(['newsletter']);
});

test('an action intercepted by an element no interruption matches fails with the browser error', async ({ page }) => {
  const cover = '<div id="cover" style="position: fixed; inset: 0; background: #fff">Maintenance</div>';
  await serve(page, { '/signup': page1(cover) });
  const lines: string[] = [];
  const heal = open(page, { cookies: 'reject', storePath: tempStore('cover'), timeoutMs: 1000, log: (_l, m) => lines.push(m) });
  // A click checks what is on top of its element (a fill does not).
  await expect(heal.locator('email', '#email').click({ timeout: 1000 })).rejects.toThrow(/intercepts pointer events/);
  expect(lines.some((l) => l.includes('none of the interruptions mendwright looks for (the cookies preset) matched'))).toBe(true);
});
