// The pages of the list tests, as the Python package's tests/test_lists.py and
// tests/test_lists_emptied.py build them, and a model that must never be asked about a list.

import type { HealProvider } from '../../src/providers/base';
import type { Proposal } from '../../src/prompt';

export const PRODUCTS: [string, string][] = [['Blue mug', '$12.00'], ['Red mug', '$13.00'], ['Green mug', '$14.00'],
  ['Tea pot', '$29.00']];

export function card(cls: string, name: string, price: string, tag = 'div'): string {
  return '<' + tag + " class='" + cls + "'><img src='/i/" + name.replace(/ /g, '-') + ".png' alt='" + name + "'><h3>"
    + name + "</h3><span class='price'>" + price + "</span><button type='button'>Add to cart</button></" + tag + '>';
}

export function shop(cards: string, extra = '', grid = "id='grid' class='grid'"): string {
  return "<html><body><header><nav><a href='/'>Home</a><a href='/shop'>Shop</a><a href='/cart'>Cart</a></nav></header>"
    + "<main id='content'><h1>Shop</h1><div " + grid + '>' + cards + '</div>' + extra + '</main>'
    + '<footer><ul><li>About</li><li>Help</li></ul></footer></body></html>';
}

export function grid(cls: string, products: [string, string][] = PRODUCTS, tag = 'div'): string {
  return products.map(([n, p]) => card(cls, n, p, tag)).join('');
}

export const V1 = shop(grid('product-card'));
export const V2 = shop(grid('product-tile'));   // the card class changed
export const TILE = "//div[contains(concat(' ', normalize-space(@class), ' '), ' product-tile ')]";
export const CARDS = "//div[contains(@class,'product-card')]";

// The cart of the Python package's tests/test_lists_emptied.py: no id or test id, the cart's
// rows, then those saved for later.
export const ITEMS: [string, string, string][] = [['Blue mug', 'blue-mug', '$12.00'], ['Red mug', 'red-mug', '$13.00'],
  ['Tea pot', 'tea-pot', '$29.00']];

export function rows(cls: string, items: [string, string, string][], qty = true): string {
  return items.map(([name, slug, price]) => "<div class='" + cls + "'><a href='/p/" + slug + "'>" + name + '</a>'
    + "<span class='price'>" + price + '</span>' + (qty ? "<input class='qty' value='1'>" : '')
    + "<button class='delete'>Delete</button></div>").join('');
}

/** The cart page (test_lists_emptied.cart): full, the cart holds the rows (class row);
 * emptied, every item is saved for later. */
export function cart(full: boolean, options: { row?: string; cartClass?: string } = {}): string {
  const inside = rows(options.row ?? 'cart-item', full ? ITEMS : []) || '<p>Your cart is empty.</p>';
  return "<html><body><main><h1>Shopping cart</h1><section class='" + (options.cartClass ?? 'cart') + "'>" + inside
    + "</section><h2>Saved for later</h2><section class='saved'>" + rows('saved-item', full ? [] : ITEMS, false)
    + '</section></main></body></html>';
}

/** A model that must never be asked: every call is kept, and throws. */
export class NoModel implements HealProvider {
  readonly model = 'none';
  readonly calls: unknown[] = [];

  async propose(bundle: unknown): Promise<Proposal | null> {
    this.calls.push(bundle);
    throw new Error('mendwright asked a model about a list');
  }
}
