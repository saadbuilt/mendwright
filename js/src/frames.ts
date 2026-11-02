// Iframes one level down (the Python package's Healer._follow_into_frame and its helpers, and
// drivers.base.frame_selector). After a key's selectors missed on the page, and before any
// heal, mendwright looks for each of the key's own selectors that are looked up in the page
// itself (no frameXPath, no shadowHosts) in the frames of the page. When one matches nothing
// on the page but exactly one element inside exactly one iframe directly in it, the key is
// looked up in that iframe from now on, with no model call: the selector is the key's own, and
// only its place changed. It is saved with its 'xpath=' or 'css=' prefix written out
// (selectors.frameSpelling) and the iframe's selector as its frameXPath, built from the
// iframe's id, name or title, or its src up to the query.
//
// This module holds the rules and the texts, which the tests compare with the Python
// package's. The driver reads the frames (PlaywrightDriver.frameMatches), and the healer
// follows the key (Healer.followIntoFrame).

import { cssString } from './css';
import { isKey } from './getby';
import { generated } from './providers/candidates';
import { hostname, port, pyLen, pyRepr, pyStrip, urlsplit } from './py';
import { frameSpelling } from './selectors';
import { hostsOf } from './shadow';
import type { SelectorStore } from './store';

/** A frame of the page in which one of a key's selectors matches (see
 * PlaywrightDriver.frameMatches): the frame's name and url, how many elements the selector
 * matches there, whether its iframe is inside another iframe, and a CSS selector that matches
 * its iframe element in the page and nothing else (frameSelector), or null when nothing tells
 * it apart or it is nested. */
export interface FrameMatch {
  name: string;
  url: string;
  count: number;
  nested: boolean;
  selector: string | null;
}

// A src that names no page of its own.
const SRC_SKIP = ['about:', 'data:', 'javascript:', 'blob:'];

/** A CSS selector that matches one iframe element and nothing else, built from its attributes
 * ({ tag, id, name, title, src }, as the driver reads them), or null (the Python package's
 * frame_selector). Tried in order: its id, name and title, then its src up to and including
 * the first ? or #, as [src^=...] (or the whole src, as [src=...]), so /embed never matches
 * /embed-ads and the query, which can hold a token, is never saved. count(selector) must be 1.
 * A generated-looking id or name is skipped, and so is a value longer than 200 characters (a
 * src longer than 300). The bare tag is never used: a later page could have one other iframe,
 * and a heal would run there. */
export function frameSelector(info: unknown, count: (selector: string) => unknown): string | null {
  if (!info || typeof info !== 'object' || Array.isArray(info)) return null;
  const attrs = info as Record<string, unknown>;
  const tag = attrs.tag === 'iframe' || attrs.tag === 'frame' ? attrs.tag : 'iframe';
  const options: string[] = [];
  for (const name of ['id', 'name', 'title']) {
    const value = attrs[name];
    if (typeof value === 'string' && pyStrip(value) && pyLen(value) <= 200
        && !((name === 'id' || name === 'name') && generated(value))) {
      options.push(tag + '[' + name + '=' + cssString(value) + ']');
    }
  }
  const src = attrs.src;
  if (typeof src === 'string' && pyStrip(src) && !SRC_SKIP.some((skip) => src.startsWith(skip))) {
    const cut = src.search(/[?#]/);
    const base = cut >= 0 ? src.slice(0, cut) : src;
    if (base && pyLen(base) <= 300) {
      // The src as far as its query or fragment, and that character.
      if (cut >= 0) options.push(tag + '[src^=' + cssString(base + src[cut]) + ']');
      else options.push(tag + '[src=' + cssString(base) + ']');
    }
  }
  for (const option of options) {
    try {
      if (count(option) === 1) return option;
    } catch {
      continue;
    }
  }
  return null;
}

/** Every selector frameSelector() may try for these attributes, in order, so their counts can
 * be read from the page first. */
export function frameOptions(info: unknown): string[] {
  const tried: string[] = [];
  frameSelector(info, (selector) => {
    tried.push(selector);
    return 0;
  });
  return tried;
}

// The schemes urllib.parse writes '//' after even with no host (urllib.parse.uses_netloc).
const USES_NETLOC = ['', 'ftp', 'http', 'gopher', 'nntp', 'telnet', 'imap', 'wais', 'file', 'mms', 'https', 'shttp',
  'snews', 'prospero', 'rtsp', 'rtsps', 'rtspu', 'rsync', 'svn', 'svn+ssh', 'sftp', 'nfs', 'git', 'git+ssh', 'ws',
  'wss', 'itms-services'];

/** urllib.parse.urlunsplit() of a URL with no query and no fragment. */
function urlunsplit(scheme: string, netloc: string, path: string): string {
  let url = path;
  if (netloc) {
    if (url && !url.startsWith('/')) url = '/' + url;
    url = '//' + netloc + url;
  } else if (url.startsWith('//')) {
    url = '//' + url;
  } else if (scheme && USES_NETLOC.includes(scheme) && (!url || url.startsWith('/'))) {
    url = '//' + url;
  }
  return scheme ? scheme + ':' + url : url;
}

/** A frame's URL without user info, query or fragment, for a message, or '' (the Python
 * package's facade._bare_url). */
export function bareUrl(url: string): string {
  try {
    const parts = urlsplit(url);
    const at = port(parts.netloc);
    return urlunsplit(parts.scheme, (hostname(parts.netloc) ?? '') + (at ? ':' + at : ''), parts.path);
  } catch {
    return '';
  }
}

/** What decides whether a key may be followed into an iframe (see framePlan). */
export interface PlanRules {
  /** MENDWRIGHT_DISABLE=1. */
  disabled: boolean;
  dryRun: boolean;
  /** The key is marked destructive. */
  destructive: boolean;
  /** The store's status of a selector of the key ('active', 'quarantined', ...), or null. */
  status: (selector: string) => string | null;
}

/** [the iframe's selector, the selector to save] when key can be followed with selector, its
 * own, into the one iframe it matches in (frames, see FrameMatch), else [null, why not], why
 * being null when nothing tells the iframe apart (the Python package's Healer._frame_plan). */
export function framePlan(key: string, selector: string, frames: FrameMatch[],
                          rules: PlanRules): [string, string] | [null, string | null] {
  if (rules.disabled) return [null, 'healing is turned off by MENDWRIGHT_DISABLE=1'];
  if (rules.dryRun) return [null, 'dry-run mode is on'];
  if (rules.destructive) return [null, "'" + key + "' is marked as a destructive control"];
  if (frames.length > 1) return [null, 'it matches in more than one iframe'];
  const frame = frames[0];
  if (frame.nested) return [null, 'that iframe is inside another iframe'];
  if (frame.count !== 1) return [null, 'it matches ' + frame.count + ' elements there'];
  if (!frame.selector) return [null, null];
  let alt: string | null;
  try {
    alt = frameSpelling(selector);
  } catch {
    alt = null;
  }
  if (alt === null) return [null, 'a Playwright engine selector is not followed into an iframe'];
  const status = rules.status(alt);
  if (status === 'quarantined') {
    return [null, 'it was looked up in an iframe before and an action on it failed there, so that lookup is '
      + 'quarantined'];
  }
  if (status === 'superseded') return [null, 'it was looked up in an iframe before and that lookup was superseded'];
  return [frame.selector, alt];
}

/** Why a key whose selector matches only inside an iframe was not followed there (the Python
 * package's facade._frame_message), with the TypeScript way to open mendwright on that frame:
 * by its name, else by its URL, else a Frame from page.frames(). frames are where selector
 * matches, the first one named; why says why not, null when nothing tells that iframe apart.
 * For the key of a getBy* call, the advice passes healGetBy: true. */
export function frameMessage(key: string, selector: string, frames: FrameMatch[], why: string | null): string {
  const frame = frames[0];
  const name = frame.name || '';
  const url = bareUrl(frame.url || '');
  let where: string;
  let how: string;
  if (name) {
    where = 'the iframe named ' + pyRepr(name);
    how = 'page.frame({ name: ' + pyRepr(name) + ' })';
  } else if (url) {
    where = 'the iframe at ' + url;
    how = 'page.frame({ url: ' + pyRepr(url) + ' })';
  } else {
    where = 'an iframe';
    how = 'the Frame from page.frames()';
  }
  if (frames.length > 1) where += ' (and ' + (frames.length - 1) + ' more)';
  if (isKey(key)) how += ', { healGetBy: true }';     // else heal.page.getBy*() there would not heal
  const because = why || 'nothing tells that iframe apart from the others on the page';
  return "'" + key + "' is inside an iframe: " + selector + ' matches nothing in the page mendwright was opened on, '
    + 'but matches inside ' + where + '. mendwright did not look for it there, because ' + because + ', and did '
    + 'not ask a model to heal it. Open mendwright on that Frame instead: heal = open(' + how + ').';
}

/** The selectors of key that are looked up in the page itself, with no frameXPath and no
 * shadowHosts, in the order they are tried (the Python package's Healer._frame_candidates). */
export function frameCandidates(store: SelectorStore, key: string): string[] {
  return store.candidates(key)
    .filter((c) => !c.frameXPath && !hostsOf(c).length && typeof c.xpath === 'string')
    .map((c) => c.xpath);
}
