// difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), ported from CPython's
// Lib/difflib.py. The fingerprint tier compares texts with it, so its scores match the
// Python package's to the last digit. Strings are compared by code point, as Python does.

interface Block {
  i: number;
  j: number;
  size: number;
}

class SequenceMatcher {
  private readonly a: string[];
  private readonly b: string[];
  private readonly b2j = new Map<string, number[]>();

  constructor(a: string[], b: string[]) {
    this.a = a;
    this.b = b;
    // __chain_b with no junk and autojunk=False: every element of b is indexed.
    b.forEach((elt, j) => {
      const list = this.b2j.get(elt);
      if (list) list.push(j);
      else this.b2j.set(elt, [j]);
    });
  }

  findLongestMatch(alo: number, ahi: number, blo: number, bhi: number): Block {
    const { a, b, b2j } = this;
    let besti = alo;
    let bestj = blo;
    let bestsize = 0;
    let j2len = new Map<number, number>();
    for (let i = alo; i < ahi; i++) {
      const newj2len = new Map<number, number>();
      for (const j of b2j.get(a[i]) ?? []) {
        if (j < blo) continue;
        if (j >= bhi) break;
        const k = (j2len.get(j - 1) ?? 0) + 1;
        newj2len.set(j, k);
        if (k > bestsize) {
          besti = i - k + 1;
          bestj = j - k + 1;
          bestsize = k;
        }
      }
      j2len = newj2len;
    }
    // With no junk these extensions never fire; they are kept as CPython has them.
    while (besti > alo && bestj > blo && a[besti - 1] === b[bestj - 1]) {
      besti -= 1;
      bestj -= 1;
      bestsize += 1;
    }
    while (besti + bestsize < ahi && bestj + bestsize < bhi
           && a[besti + bestsize] === b[bestj + bestsize]) {
      bestsize += 1;
    }
    return { i: besti, j: bestj, size: bestsize };
  }

  matches(): number {
    const queue: [number, number, number, number][] = [[0, this.a.length, 0, this.b.length]];
    let total = 0;
    while (queue.length) {
      const [alo, ahi, blo, bhi] = queue.pop() as [number, number, number, number];
      const { i, j, size } = this.findLongestMatch(alo, ahi, blo, bhi);
      if (size) {
        total += size;
        if (alo < i && blo < j) queue.push([alo, i, blo, j]);
        if (i + size < ahi && j + size < bhi) queue.push([i + size, ahi, j + size, bhi]);
      }
    }
    return total;
  }
}

/** SequenceMatcher(None, a, b, autojunk=False).ratio(). */
export function ratio(a: string, b: string): number {
  const left = Array.from(a);
  const right = Array.from(b);
  const length = left.length + right.length;
  if (!length) return 1.0;
  return (2.0 * new SequenceMatcher(left, right).matches()) / length;
}
