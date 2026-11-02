// fingerprint.find() of the Python package, on the live page: look for the element a stored
// fingerprint describes among the elements that have the key's role, and answer with an
// XPath only when the match is confident. For a heal inside an iframe, driver is on the
// iframe's Frame (see scope.ts), so the elements scored are the iframe's. For a heal inside
// an open shadow root, driver is a ShadowDriver, which scores the elements of that shadow
// root's HTML, as the Python package does, and whether one is visible is read on the live
// page through a CSS form of it (shadowVisible), since no XPath runs inside a shadow root.

import {
  MARGIN, THRESHOLD, containerIds, fmt, fromRaw, score, valid, type Fingerprint,
} from './fingerprint';
import type { HealDriver } from './scope';
import { formsRead } from './shadow';

export interface Match {
  /** Set only when the match is confident. */
  xpath: string | null;
  score: number;
  runnerUp: number;
  /** Why not, when xpath is null. */
  reason: string;
}

interface Scored {
  value: number;
  strong: boolean;
  index: number;
}

/** True when xpath reaches one visible element on the live page, false when not, null when
 * the page could not be read. */
async function liveVisible(driver: HealDriver, xpath: string): Promise<boolean | null> {
  try {
    return (await driver.count(xpath)) === 1 && (await driver.isVisible(xpath));
  } catch {
    return null;
  }
}

/** For a heal inside a shadow root (the Python package's coordinator._visible_check): whether
 * the element xpath matches in html, the shadow root's HTML as a document, is visible on the
 * live page, read through the first of its CSS forms that matches one element in that shadow
 * root (driver is its ShadowDriver). Playwright's CSS pierces, so a form can also match in a
 * shadow root inside it, and says nothing of the element then. With no such form, or on any
 * error, null: unknown, so the element still counts as a rival. */
export async function shadowVisible(driver: HealDriver, html: string, xpath: string): Promise<boolean | null> {
  let read: unknown;
  try {
    read = await driver.lib('cssForms', { html, xpath });
  } catch {
    return null;
  }
  const found = formsRead(read);
  for (const form of found && found.count === 1 ? found.forms : []) {
    try {
      if ((await driver.count(form)) === 1) return await driver.isVisible(form);
    } catch {
      return null;
    }
  }
  return null;
}

/** options: threshold and margin; html, the captured document a ShadowDriver scores; and
 * visible(xpath), whether that element is visible on the live page (false skips it, null is
 * unknown), by default whether xpath reaches one visible element there. */
export async function findMatch(fp: Fingerprint | null, role: string, driver: HealDriver,
                                options: { threshold?: number; margin?: number; html?: string;
                                           visible?: (xpath: string) => Promise<boolean | null> } = {}): Promise<Match> {
  const threshold = options.threshold ?? THRESHOLD;
  const margin = options.margin ?? MARGIN;
  const visible = options.visible ?? ((xpath: string) => liveVisible(driver, xpath));
  if (!valid(fp)) return { xpath: null, score: 0, runnerUp: 0, reason: 'the stored fingerprint is not one this version reads' };
  let snap;
  try {
    snap = await driver.snapshot(role, options.html);
  } catch {
    return { xpath: null, score: 0, runnerUp: 0, reason: 'the captured page could not be parsed' };
  }
  try {
    const scored: Scored[] = [];
    snap.raws.forEach((raw, index) => {
      const next = fromRaw(raw);
      if (next === null) return;
      const result = score(fp, next);
      scored.push({ value: result.value, strong: result.strong, index });
    });
    if (!scored.length) {
      return { xpath: null, score: 0, runnerUp: 0, reason: "no element on the page has the role '" + role + "'" };
    }
    // sorted(key=score, reverse=True): stable, so equal scores keep page order.
    const order = scored.map((s, i) => ({ s, i })).sort((a, b) => b.s.value - a.s.value || a.i - b.i).map((x) => x.s);
    const viable: (Scored & { xpath: string | null })[] = [];
    let below: number | null = null;
    for (const item of order) {
      if (viable.length && item.value < viable[0].value - margin) {
        below = item.value;   // no rival can come closer than this
        break;
      }
      if (!viable.length && item.value < threshold) {
        below = item.value;
        break;
      }
      const xpath = await snap.xpathFor(item.index);
      if (role !== 'checkbox' && xpath !== null) {
        // A copy hidden on the live page is neither the answer nor a rival.
        let shown: boolean | null;
        try {
          shown = await visible(xpath);
        } catch {
          shown = null;
        }
        if (shown === false) continue;
      }
      viable.push({ ...item, xpath });
      if (viable.length === 2) break;
    }
    if (!viable.length) {
      if (below === null) return { xpath: null, score: 0, runnerUp: 0, reason: 'no visible element scored ' + fmt(threshold) + ' or more' };
      return { xpath: null, score: below, runnerUp: 0,
               reason: 'the best score was ' + fmt(below) + ', below the ' + fmt(threshold) + ' needed' };
    }
    const best = viable[0];
    const runner = viable.length > 1 ? viable[1].value : (below ?? 0);
    if (best.value - runner < margin) {
      return { xpath: null, score: best.value, runnerUp: runner,
               reason: 'two elements score alike (' + fmt(best.value) + ' and ' + fmt(runner)
                 + '), so the fingerprint does not tell them apart' };
    }
    if (!best.strong) {
      return { xpath: null, score: best.value, runnerUp: runner,
               reason: 'the best match (score ' + fmt(best.value) + ') shares no id, test id, name, link, '
                 + 'autocomplete value, label or text with the element that was recorded' };
    }
    const ids = containerIds(fp);
    const away = ids.length ? await snap.outside(best.index, ids) : '';
    if (away === null) {
      return { xpath: null, score: best.value, runnerUp: runner, reason: 'the best match left the page while mendwright looked at it' };
    }
    if (away) {
      return { xpath: null, score: best.value, runnerUp: runner,
               reason: "the best match is outside the element with id '" + away + "', which held the recorded "
                 + 'element and is still on the page' };
    }
    if (best.xpath === null) {
      return { xpath: null, score: best.value, runnerUp: runner,
               reason: 'no selector that is unique on the page could be built for the best match without using its position' };
    }
    return { xpath: best.xpath, score: best.value, runnerUp: runner, reason: '' };
  } finally {
    await snap.dispose();
  }
}
