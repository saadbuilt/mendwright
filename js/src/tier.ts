// fingerprint.find() of the Python package, on the live page: look for the element a stored
// fingerprint describes among the elements that have the key's role, and answer with an
// XPath only when the match is confident.

import type { PlaywrightDriver } from './driver';
import {
  MARGIN, THRESHOLD, containerIds, fmt, fromRaw, score, valid, type Fingerprint,
} from './fingerprint';

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
async function liveVisible(driver: PlaywrightDriver, xpath: string): Promise<boolean | null> {
  try {
    return (await driver.count(xpath)) === 1 && (await driver.isVisible(xpath));
  } catch {
    return null;
  }
}

export async function findMatch(fp: Fingerprint | null, role: string, driver: PlaywrightDriver,
                                options: { threshold?: number; margin?: number } = {}): Promise<Match> {
  const threshold = options.threshold ?? THRESHOLD;
  const margin = options.margin ?? MARGIN;
  if (!valid(fp)) return { xpath: null, score: 0, runnerUp: 0, reason: 'the stored fingerprint is not one this version reads' };
  let snap;
  try {
    snap = await driver.snapshot(role);
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
        if ((await liveVisible(driver, xpath)) === false) continue;
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
