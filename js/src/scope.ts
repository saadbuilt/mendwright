// Where a heal runs (the Python package's facade.heal_place, HealScope and
// Healer._heal_scope): on the page, inside the iframe where the key's element was last found,
// when that iframe is on the page now, or inside the open shadow root where it was last found.
// A scope hands the heal the driver that every one of its reads goes through, and the place
// what it applies is saved with.
//
// A key last found inside a shadow root heals only inside that shadow root, never on the page
// around its host, where an element that looks the same can be. Its scope's driver is a
// ShadowDriver (the Python package's shadow.ShadowDriver): it enters the shadow root strictly
// through the hosts each time, runs CSS only, counts only what matches inside that shadow
// root, and reads what the shadow root holds and nothing of the page around it. When that
// shadow root cannot be entered now, or sits in an iframe, or behind a host nothing tells
// apart, the heal is refused with the reason (healPlace, shadowUnreachable).

import { isParser, type Description, type InventoryRow, type LibName, type PlaywrightDriver, type Snapshot } from './driver';
import { InvalidSelector } from './errors';
import type { RawElement } from './fingerprint';
import { asDocument, chainHosts, hostCss, hostsOf, innerCss, placeText, type Hosts } from './shadow';
import type { Candidate, SelectorStore } from './store';

/** What a heal reads a document through: the capture, the inventory, the fingerprint tier's
 * snapshot, the model's tools and both halves of the gate. A PlaywrightDriver on the page, or
 * on the Frame of an iframe, or a ShadowDriver for the inside of a shadow root. */
export interface HealDriver {
  content(): Promise<string>;
  lib<T>(name: LibName, arg?: unknown): Promise<T>;
  inventory(limit?: number): Promise<InventoryRow[]>;
  /** The elements of role, for the fingerprint tier. html is the captured document, which a
   * driver that reads no live elements (the ShadowDriver) scores; the page's driver reads the
   * page's own elements. */
  snapshot(role: string, html?: string): Promise<Snapshot>;
  count(selector: string, frameXPath?: string | null): Promise<number>;
  describe(selector: string, frameXPath?: string | null): Promise<Description | null>;
  isVisible(selector: string, frameXPath?: string | null): Promise<boolean>;
}

/** A driver for the inside of the open shadow root that hosts lead to (the Python package's
 * shadow.ShadowDriver), over the page's driver. Every selector runs inside that shadow root,
 * through the hosts, and must be CSS: an XPath, which reaches into no shadow root, or a frame
 * throws InvalidSelector. A host must match exactly one element in the document or shadow
 * root before it and have an open shadow root, and only an element in that shadow root
 * counts, never one in the host's light DOM, a shadow root inside it or another host
 * (PlaywrightDriver.count with hosts). content() and inventory() give what the shadow root
 * holds, and lib() runs only the page library's parsers, which read the HTML they are given:
 * nothing reads the page around the shadow root. */
export class ShadowDriver implements HealDriver {
  /** The selectors of the hosts, outermost first, as saved. */
  readonly hosts: string[];

  constructor(private readonly base: PlaywrightDriver, hosts: string[]) {
    this.hosts = [...hosts];
  }

  /** [the CSS to run, the CSS of the hosts]. */
  private target(selector: string, frameXPath?: string | null): [string, string[]] {
    if (frameXPath) {
      throw InvalidSelector.build(null, 'an iframe inside a shadow root is not supported.',
        'Open mendwright on that frame instead.');
    }
    return [innerCss(selector), this.hosts.map(hostCss)];
  }

  async count(selector: string, frameXPath?: string | null): Promise<number> {
    const [css, hosts] = this.target(selector, frameXPath);
    return this.base.count(css, null, hosts);
  }

  async isVisible(selector: string, frameXPath?: string | null): Promise<boolean> {
    const [css, hosts] = this.target(selector, frameXPath);
    return this.base.isVisible(css, null, hosts);
  }

  async describe(selector: string, frameXPath?: string | null): Promise<Description | null> {
    const [css, hosts] = this.target(selector, frameXPath);
    return this.base.describe(css, null, hosts);
  }

  async scrollIntoView(selector: string, frameXPath?: string | null): Promise<void> {
    const [css, hosts] = this.target(selector, frameXPath);
    await this.base.scrollIntoView(css, null, hosts);
  }

  /** What the shadow root holds (its innerHTML), or null when the hosts lead to no open
   * shadow root now. */
  innerHtml(): Promise<string | null> {
    return this.base.shadowHtml(this.hosts);
  }

  /** The shadow root's HTML as a document (shadow.asDocument), for the gate and the model; ''
   * when the hosts lead to none. */
  async content(): Promise<string> {
    const html = await this.innerHtml();
    return html === null ? '' : asDocument(html);
  }

  inventory(limit = 200): Promise<InventoryRow[]> {
    return this.base.shadowInventory(this.hosts, limit);
  }

  /** A helper of the page library that parses the HTML it is given; any other one would read
   * the page around the shadow root, and throws TypeError. */
  lib<T>(name: LibName, arg?: unknown): Promise<T> {
    if (!isParser(name)) {
      return Promise.reject(new TypeError('mendwright: ' + name + '() reads the page around the shadow root, and a heal '
        + 'inside a shadow root reads only that shadow root.'));
    }
    return this.base.lib<T>(name, arg);
  }

  /** The elements of role in html, the shadow root's HTML as a document (else read now), with
   * their raw reads, as the Python package's fingerprint tier scores them: the XPath of one is
   * built and checked in that HTML, where an XPath runs. Throws for an empty html (the hosts
   * led to no shadow root when it was read), which the Python package cannot parse either. */
  async snapshot(role: string, html?: string): Promise<Snapshot> {
    const doc = html ?? (await this.content());
    if (!doc.trim()) throw new Error('mendwright: the shadow root could not be read');
    const read = await this.base.lib<{ raws?: unknown; indexes?: unknown } | null>('collectHtml', { html: doc, role });
    const raws = read && Array.isArray(read.raws) ? (read.raws as RawElement[]) : [];
    const indexes = read && Array.isArray(read.indexes) ? (read.indexes as number[]) : [];
    return {
      raws,
      xpathFor: (index) => this.base.lib<string | null>('xpathAt', { html: doc, index: indexes[index] }),
      outside: (index, ids) => this.base.lib<string | null>('outsideAt', { html: doc, index: indexes[index], ids }),
      dispose: async () => undefined,
    };
  }
}

/** A ShadowDriver for the shadow root that hosts lead to, or null when a host is not saved as
 * a selector (the Python package's driver_shadowed). */
export function driverShadowed(driver: PlaywrightDriver, hosts: Hosts): ShadowDriver | null {
  if (!hosts.length || hosts.some((h) => !h)) return null;
  return new ShadowDriver(driver, hosts as string[]);
}

/** A heal inside an iframe: driver is a PlaywrightDriver on the iframe's own Frame, and frame
 * the selector of the iframe in the page, which an applied heal is saved with as its
 * frameXPath. */
export interface FrameScope {
  readonly kind: 'frame';
  readonly frame: string;
  readonly driver: HealDriver;
}

/** A heal inside the open shadow root that hosts lead to: driver is a ShadowDriver for it, and
 * an applied heal is saved as the chain of the hosts and a CSS selector, with the hosts as its
 * shadowHosts. */
export interface ShadowScope {
  readonly kind: 'shadow';
  readonly hosts: string[];
  readonly driver: ShadowDriver;
}

/** Where a heal runs when it does not run on the page. */
export type HealScope = FrameScope | ShadowScope;

/** The key's active candidate that matched most recently, in any run, or null (the Python
 * package's facade.latest_success). */
export function latestSuccess(store: SelectorStore, key: string): Candidate | null {
  const found = store.candidates(key).filter((c) => c.lastSucceededAt);
  if (!found.length) return null;
  return found.reduce((best, c) => (String(c.lastSucceededAt) > String(best.lastSucceededAt) ? c : best));
}

/** Where a heal of key runs (the Python package's facade.heal_place): frame, the selector of
 * the iframe its latest match was in ('' for none); hosts, the shadow hosts of the open shadow
 * root its element was last found in: those its latest match runs inside, else those the
 * chain of a heal names when the Python package 0.9.0 dropped its shadowHosts
 * (shadow.chainHosts), else the key's shadowHosts ([] for none); and why, why no heal may run
 * anywhere ('' else): an element inside a shadow root inside an iframe, or behind a host
 * nothing tells apart. A key last found inside a shadow root is never healed on the page
 * outside it. */
export function healPlace(store: SelectorStore, key: string): { frame: string; hosts: Hosts; why: string } {
  const best = latestSuccess(store, key);
  const frame = best && typeof best.frameXPath === 'string' && best.frameXPath ? best.frameXPath : '';
  let hosts: Hosts = hostsOf(best);
  if (!hosts.length) hosts = chainHosts(best);
  if (!hosts.length) hosts = store.shadowHosts(key);
  if (hosts.length && frame) {
    return { frame, hosts, why: "'" + key + "' was last found inside a shadow root in the iframe " + frame + ', and '
      + 'mendwright heals a key inside a shadow root only in the page or frame it was opened on, so it did not heal it, '
      + 'there or anywhere else. Open mendwright on that frame to heal it there.' };
  }
  if (hosts.some((h) => h === null)) {
    return { frame: '', hosts, why: "'" + key + "' was last found inside a shadow root whose host nothing tells apart "
      + 'from the elements around it (it has no test id, id or name that no other element of its tag there has, and '
      + 'is not the only element of its custom tag), so mendwright cannot find that shadow root again, and did not '
      + 'heal the key on the page outside it either. Give the host an id or a test id, or fix the selector by hand.' };
  }
  return { frame, hosts, why: '' };
}

/** Why a heal of key, last found inside the open shadow root that hosts lead to, cannot run
 * there now: the hosts lead to no open shadow root on the page (the Python package's
 * facade.shadow_unreachable, whose driver may not reach into shadow roots; this package's
 * always does). */
export function shadowUnreachable(key: string, hosts: string[]): string {
  return "'" + key + "' was last found inside " + placeText(hosts) + ', and mendwright cannot enter that shadow root '
    + 'now: one of its hosts (' + hosts.join(' >> ') + ') is not on the page, matches more than one element, or has a '
    + 'closed shadow root. mendwright heals the key only inside that shadow root, never on the page outside it, so it '
    + 'did not heal it. Fix the selector in your code.';
}
