// Reference self-heal gateway (Cloudflare Worker) for the `mendwright` library.
// Model-agnostic: set PROVIDER=anthropic|openai and MODEL=<model id>. The provider key
// lives only here.
//
//   POST /heal          {key, expectedRole, expectedText?, destructive?, page?, lastPage?,
//                        candidates?, inventory?, html?, rejectedXpath?, rejectReason?}
//                        or, for a list heal, {list: true, key, expectedRole, count?,
//                        candidates?, groups: [{xpath, count, container, items}], ...}
//                        -> {status:'ok', xpath, fallbacks, reasoning} | {status:'needs_human', reason}
//                        When the model says no element on the page fits the step, the
//                        reason is 'the model says no element on this page fits: <its reason>'.
//   POST /heal/outcome  {key, xpath, success} -> {status:'ok'}
//
// Settings (see wrangler.toml):
//   vars:     PROVIDER ("anthropic" or "openai") and MODEL (the model id, required; there
//             is no default, the worker answers 500 on /heal until it is set). For older
//             deployments, ANTHROPIC_MODEL or OPENAI_MODEL is read when MODEL is unset.
//   secrets:  HEAL_KEY (client auth, checked against X-Heal-Key), and the provider key
//             (ANTHROPIC_API_KEY or OPENAI_API_KEY).
//   binding:  HEAL_KV (rate limit + audit).
//
// SECURITY MODEL (read before deploying):
//  - The prompt is built from client-supplied, untrusted page content (html/inventory/
//    candidates). Treat the model's reply as untrusted: the mendwright client re-validates
//    every proposed xpath against its own captured HTML (exactly-one + role + destructive
//    action-text + no brittle paths) before applying, and never auto-heals a destructive
//    control. That client-side gate, not this worker, is the real safety boundary.
//  - All endpoints require the X-Heal-Key and are capped by a global hourly limit, so a
//    client that varies the per-request `key` cannot bypass rate limiting.

const MAX_PER_KEY_PER_HOUR = 3;
const MAX_GLOBAL_PER_HOUR = 60;   // backstop independent of the client-controlled `key`
const MAX_BODY = 512 * 1024;
const MAX_PROMPT_PART = 60000;    // same cut as _MAX in mendwright/prompt.py

// Sanitize a client-supplied value before using it in a KV key (no injection/oversize).
const safe = (v) => String(v || '').replace(/[^A-Za-z0-9_.:-]/g, '_').slice(0, 128);

const json = (o, s = 200) => new Response(JSON.stringify(o),
  { status: s, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } });

async function sameKey(a, b) {
  const enc = new TextEncoder();
  const x = new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode(a)));
  const y = new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode(b)));
  let d = 0;
  for (let i = 0; i < x.length; i++) d |= x[i] ^ y[i];
  return d === 0;
}

function providerName(env) {
  return (env.PROVIDER || 'anthropic').toLowerCase() === 'openai' ? 'openai' : 'anthropic';
}

// The model id comes from the environment only. MODEL is the documented name; the
// provider-specific names are read so a deployment that already sets them keeps working.
export function modelId(env) {
  const legacy = providerName(env) === 'openai' ? env.OPENAI_MODEL : env.ANTHROPIC_MODEL;
  return String(env.MODEL || legacy || '').trim();
}

// --- Python parity helpers ------------------------------------------------------------
// buildPrompt/parseProposal/notHereReason mirror mendwright/prompt.py exactly, so a heal through this
// gateway sends the model the same text as a direct provider. These helpers reproduce the
// Python behaviour the prompt relies on: str(), truthiness, json.dumps() defaults
// (", " and ": " separators, non-ASCII escaped as \uXXXX) and slicing by code point.

function pyStr(v) {
  if (v === null || v === undefined) return 'None';
  if (v === true) return 'True';
  if (v === false) return 'False';
  return String(v);
}

function pyTruthy(v) {
  if (v === null || v === undefined || v === false || v === 0 || v === '') return false;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === 'object') return Object.keys(v).length > 0;
  return true;
}

const PY_ESCAPES = { '"': '\\"', '\\': '\\\\', '\n': '\\n', '\r': '\\r', '\t': '\\t',
                     '\b': '\\b', '\f': '\\f' };

function pyJsonString(s) {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    const code = s.charCodeAt(i);
    if (PY_ESCAPES[ch]) out += PY_ESCAPES[ch];
    else if (code < 0x20 || code > 0x7e) out += '\\u' + code.toString(16).padStart(4, '0');
    else out += ch;
  }
  return out + '"';
}

export function pyJson(v) {
  if (v === null || v === undefined) return 'null';
  if (v === true) return 'true';
  if (v === false) return 'false';
  if (typeof v === 'number') {
    if (Number.isNaN(v)) return 'NaN';
    if (!Number.isFinite(v)) return v > 0 ? 'Infinity' : '-Infinity';
    return String(v);
  }
  if (typeof v === 'string') return pyJsonString(v);
  if (Array.isArray(v)) return '[' + v.map(pyJson).join(', ') + ']';
  if (typeof v === 'object') {
    return '{' + Object.keys(v).map((k) => pyJsonString(k) + ': ' + pyJson(v[k])).join(', ') + '}';
  }
  return 'null';
}

// Python slices strings by code point; JS strings are UTF-16, so count surrogate pairs once.
function pySlice(s, n) {
  let units = 0;
  let points = 0;
  while (units < s.length && points < n) {
    const c = s.charCodeAt(units);
    const pair = c >= 0xd800 && c <= 0xdbff && units + 1 < s.length
      && (s.charCodeAt(units + 1) & 0xfc00) === 0xdc00;
    units += pair ? 2 : 1;
    points += 1;
  }
  return s.slice(0, units);
}

// What a Python for loop goes over: a list as it is, a string's characters, a dict's keys.
function pyIter(v) {
  if (Array.isArray(v)) return v;
  if (typeof v === 'string') return Array.from(v);
  if (v !== null && typeof v === 'object') return Object.keys(v);
  return [];
}

export function buildPrompt(b) {
  if (pyTruthy(b.list)) return buildListPrompt(b);
  const destructive = pyTruthy(b.destructive);
  const expectedRole = b.expectedRole;
  const expectedText = pyTruthy(b.expectedText) ? b.expectedText : [];
  let system =
    'You repair ONE broken web-automation xpath selector. Reply with ONLY a JSON object: ' +
    '{"xpath":"...","fallbacks":["..."],"reasoning":"..."}. ' +
    'Prefer role + nearby visible label text + stable attributes (href, formcontrolname, type) ' +
    'over structure. NEVER return absolute positional paths like /div[3]/div[2]. ' +
    "The xpath must match EXACTLY ONE element whose role is '" + pyStr(expectedRole) + "'. " +
    'If no element on this page is the one the step means, for example because the step ' +
    'moved to another page or the element was removed, reply {"xpath":null,"reason":"..."} ' +
    'instead. Do not pick an element that is only similar.';
  if (destructive) {
    system += ' This is a DESTRUCTIVE control: its exact visible text MUST be one of '
      + pyJson(expectedText) + '.';
  }
  const user =
    'key: ' + pyStr(b.key) +
    '\nexpectedRole: ' + pyStr(expectedRole) +
    '\nexpectedText: ' + pyJson(expectedText) +
    (pyTruthy(b.intent) ? '\nintent: ' + pyStr(b.intent) : '') +
    '\ndestructive: ' + (destructive ? 'true' : 'false') +
    (pyTruthy(b.page) ? '\npage: ' + pyStr(b.page) : '') +
    (pyTruthy(b.lastPage) ? '\nlast found on page: ' + pyStr(b.lastPage) : '') +
    '\nbroken candidates:\n' + (pyTruthy(b.candidates) ? b.candidates : []).join('\n') +
    (pyTruthy(b.rejectReason)
      ? '\nYour previous proposal ' + pyStr(b.rejectedXpath) + ' was rejected: '
        + pyStr(b.rejectReason) + '. Propose a DIFFERENT selector.'
      : '') +
    '\nDOM inventory (redacted):\n'
      + pySlice(pyJson(pyTruthy(b.inventory) ? b.inventory : []), MAX_PROMPT_PART) +
    '\nHTML snapshot (redacted):\n'
      + pySlice(pyStr(pyTruthy(b.html) ? b.html : ''), MAX_PROMPT_PART);
  return { system, user };
}

// The prompt for a list heal (a bundle with list: true): the model picks one of the
// repeated groups by its xpath. Mirrors build_list_prompt() in mendwright/prompt.py.
export function buildListPrompt(b) {
  const role = b.expectedRole;
  const system =
    'You repair ONE broken web-automation selector for a LIST of elements. On an earlier ' +
    'run it matched every item of a list, such as the cards of a product grid, and now it ' +
    'matches nothing. Below are the groups of elements that repeat on the page now, each ' +
    'with its xpath. Reply with ONLY a JSON object: {"xpath":"...","reasoning":"..."}, ' +
    'where xpath is the xpath of the group whose items are the items of that list, copied ' +
    "exactly. Each item's role must be '" + pyStr(role) + "'. If no group is that list, for " +
    'example because the list is empty now or moved to another page, reply ' +
    '{"xpath":null,"reason":"..."} instead. Do not pick a group that is only similar, ' +
    'such as another list elsewhere on the page.';
  const lines = [];
  let number = 0;
  for (const group of pyIter(pyTruthy(b.groups) ? b.groups : [])) {
    number += 1;
    if (group === null || typeof group !== 'object' || Array.isArray(group)) continue;
    lines.push('[' + number + '] ' + pyStr(group.xpath));
    lines.push('    ' + pyStr(group.count) + ' items in ' + pyStr(group.container));
    for (const item of pyIter(pyTruthy(group.items) ? group.items : [])) {
      lines.push('    item: ' + pyStr(item));
    }
  }
  const user =
    'key: ' + pyStr(b.key) +
    '\nitemRole: ' + pyStr(role) +
    (pyTruthy(b.intent) ? '\nintent: ' + pyStr(b.intent) : '') +
    (pyTruthy(b.page) ? '\npage: ' + pyStr(b.page) : '') +
    (pyTruthy(b.lastPage) ? '\nlast found on page: ' + pyStr(b.lastPage) : '') +
    (pyTruthy(b.count) ? '\nitems when it last matched: ' + pyStr(b.count) : '') +
    '\nbroken candidates:\n' + (pyTruthy(b.candidates) ? b.candidates : []).join('\n') +
    (pyTruthy(b.rejectReason)
      ? '\nYour previous proposal ' + pyStr(b.rejectedXpath) + ' was rejected: '
        + pyStr(b.rejectReason) + '. Pick a DIFFERENT group.'
      : '') +
    '\nrepeated groups (redacted):\n' + pySlice(lines.join('\n'), MAX_PROMPT_PART);
  return { system, user };
}

// The JSON object in a model reply (text around it is allowed), or null.
function replyObject(text) {
  if (!text) return null;
  const match = String(text).match(/\{[\s\S]*\}/);
  if (!match) return null;
  let o;
  try { o = JSON.parse(match[0]); } catch { return null; }
  if (o === null || typeof o !== 'object' || Array.isArray(o)) return null;
  return o;
}

export function parseProposal(text) {
  const o = replyObject(text);
  if (o === null || !pyTruthy(o.xpath)) return null;
  return {
    xpath: pyStr(o.xpath),
    fallbacks: Array.isArray(o.fallbacks) ? o.fallbacks.slice(0, 4).map(pyStr) : [],
    reasoning: pyStr(pyTruthy(o.reasoning) ? o.reasoning : ''),
  };
}

// The model's reason when its reply says no element on the page fits the step
// ({"xpath": null, "reason": "..."}), else null.
export function notHereReason(text) {
  const o = replyObject(text);
  if (o === null || pyTruthy(o.xpath)) return null;
  if (typeof o.reason === 'string' && o.reason.trim()) return o.reason.trim();
  return null;
}

// What callModel() makes of a reply: {proposal} or {proposal: null, reason}.
function readReply(text) {
  const proposal = parseProposal(text);
  if (proposal) return { proposal, reason: null };
  const notHere = notHereReason(text);
  return { proposal: null,
           reason: notHere ? 'the model says no element on this page fits: ' + notHere.slice(0, 300) : null };
}

async function callModel(bundle, env, model) {
  const { system, user } = buildPrompt(bundle);
  if (providerName(env) === 'openai') {
    const r = await fetch('https://api.openai.com/v1/chat/completions', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'Authorization': 'Bearer ' + env.OPENAI_API_KEY },
      body: JSON.stringify({ model, temperature: 0,
        response_format: { type: 'json_object' },
        messages: [{ role: 'system', content: system }, { role: 'user', content: user }] }),
    });
    if (!r.ok) return { proposal: null, reason: null };
    const data = await r.json();
    return readReply(data.choices?.[0]?.message?.content || '');
  }
  // default: anthropic
  const r = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: { 'x-api-key': env.ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json' },
    body: JSON.stringify({ model, max_tokens: 1024, system,
      messages: [{ role: 'user', content: user }] }),
  });
  if (!r.ok) return { proposal: null, reason: null };
  const data = await r.json();
  return readReply((data.content || []).map((p) => p.text || '').join(''));
}

async function audit(env, kind, data) {
  try {
    if (env.HEAL_KV) await env.HEAL_KV.put('audit:' + Date.now() + ':' + Math.random().toString(36).slice(2, 8),
      JSON.stringify({ kind, t: new Date().toISOString(), ...data }), { expirationTtl: 60 * 60 * 24 * 30 });
  } catch {}
}

export default {
  async fetch(request, env) {
    const providerKey = providerName(env) === 'openai' ? env.OPENAI_API_KEY : env.ANTHROPIC_API_KEY;
    if (!env.HEAL_KEY || !providerKey) return json({ status: 'error', reason: 'not configured' }, 503);
    if (request.method !== 'POST') return json({ status: 'error', reason: 'method' }, 405);
    if (!(await sameKey(request.headers.get('X-Heal-Key') || '', env.HEAL_KEY)))
      return json({ status: 'error', reason: 'auth' }, 401);

    const raw = await request.text();
    if (raw.length > MAX_BODY) return json({ status: 'error', reason: 'too large' }, 413);
    let bundle;
    try { bundle = JSON.parse(raw); } catch { return json({ status: 'error', reason: 'bad json' }, 400); }

    const url = new URL(request.url);
    if (url.pathname !== '/heal' && url.pathname !== '/heal/outcome')
      return json({ status: 'error', reason: 'not found' }, 404);
    if (!bundle || !bundle.key) return json({ status: 'error', reason: 'incomplete bundle' }, 400);

    // A missing model is a deployment mistake, not something a client can fix. Say so
    // before any rate-limit counter moves, so the misconfiguration costs no quota.
    const model = modelId(env);
    if (url.pathname === '/heal' && !model) {
      return json({ status: 'error', reason: 'MODEL is not set on the gateway. Add MODEL = '
        + '"<model id>" under [vars] in wrangler.toml and redeploy.' }, 500);
    }

    // Global hourly cap, enforced on both endpoints and keyed independently of the
    // client-supplied `key`, so varying the key can't bypass the limit (parity).
    const hour = Math.floor(Date.now() / 3600000);
    if (env.HEAL_KV) {
      const gk = 'rate:__global__:' + hour;
      const gUsed = Number(await env.HEAL_KV.get(gk)) || 0;
      if (gUsed >= MAX_GLOBAL_PER_HOUR) {
        await audit(env, 'rate-limited', { scope: 'global' });
        return json({ status: 'needs_human', reason: 'rate limit' });
      }
      await env.HEAL_KV.put(gk, String(gUsed + 1), { expirationTtl: 3700 });
    }

    if (url.pathname === '/heal/outcome') {
      await audit(env, 'outcome', { key: safe(bundle.key), xpath: String(bundle.xpath || '').slice(0, 2000),
                                    success: !!bundle.success });
      return json({ status: 'ok' });
    }
    if (!bundle.expectedRole) return json({ status: 'error', reason: 'incomplete bundle' }, 400);

    if (env.HEAL_KV) {
      const rk = 'rate:' + safe(bundle.key) + ':' + hour;
      const used = Number(await env.HEAL_KV.get(rk)) || 0;
      if (used >= MAX_PER_KEY_PER_HOUR) {
        await audit(env, 'rate-limited', { key: safe(bundle.key) });
        return json({ status: 'needs_human', reason: 'rate limit' });
      }
      await env.HEAL_KV.put(rk, String(used + 1), { expirationTtl: 3700 });
    }

    const { proposal, reason } = await callModel(bundle, env, model);
    await audit(env, 'heal', { key: safe(bundle.key), proposal: proposal ? proposal.xpath : null });
    if (!proposal) return json({ status: 'needs_human', reason: reason || 'no valid proposal' });
    return json({ status: 'ok', ...proposal });
  },
};
