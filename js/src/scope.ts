// Where a heal runs (the Python package's facade.heal_place, HealScope and
// Healer._heal_scope): on the page, or inside the iframe where the key's element was last
// found, when that iframe is on the page now. A scope hands the heal the driver that every one
// of its reads goes through, and the place what it applies is saved with. A key whose element
// was last found inside an open shadow root is refused instead (see Healer.heal). A scope
// inside a shadow root, through its hosts, can join FrameScope as a second kind of HealScope.

import type { PlaywrightDriver } from './driver';
import { chainHosts, hostsOf, type Hosts } from './shadow';
import type { Candidate, SelectorStore } from './store';

/** What a heal reads a document through: the capture, the inventory, the fingerprint tier's
 * snapshot, the model's tools and both halves of the gate. A PlaywrightDriver on the page, or
 * on the Frame of an iframe. */
export type HealDriver = Pick<PlaywrightDriver, 'content' | 'lib' | 'inventory' | 'snapshot' | 'count' | 'describe'
  | 'isVisible'>;

/** A heal inside an iframe: driver is a PlaywrightDriver on the iframe's own Frame, and frame
 * the selector of the iframe in the page, which an applied heal is saved with as its
 * frameXPath. */
export interface FrameScope {
  readonly kind: 'frame';
  readonly frame: string;
  readonly driver: HealDriver;
}

/** Where a heal runs when it does not run on the page. */
export type HealScope = FrameScope;

/** The key's active candidate that matched most recently, in any run, or null (the Python
 * package's facade.latest_success). */
export function latestSuccess(store: SelectorStore, key: string): Candidate | null {
  const found = store.candidates(key).filter((c) => c.lastSucceededAt);
  if (!found.length) return null;
  return found.reduce((best, c) => (String(c.lastSucceededAt) > String(best.lastSucceededAt) ? c : best));
}

/** Where a heal of key runs (the Python package's facade.heal_place, without the refusals it
 * gives for a shadow root): frame, the selector of the iframe its latest match was in ('' for
 * none), and hosts, the shadow hosts of the open shadow root its element was last found in:
 * those its latest match runs inside, else those the chain of a heal names when the Python
 * package 0.9.0 dropped its shadowHosts (shadow.chainHosts), else the key's shadowHosts ([]
 * for none). */
export function healPlace(store: SelectorStore, key: string): { frame: string; hosts: Hosts } {
  const best = latestSuccess(store, key);
  const frame = best && typeof best.frameXPath === 'string' && best.frameXPath ? best.frameXPath : '';
  let hosts: Hosts = hostsOf(best);
  if (!hosts.length) hosts = chainHosts(best);
  if (!hosts.length) hosts = store.shadowHosts(key);
  return { frame, hosts };
}
