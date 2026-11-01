// autoProvider(): a provider from the environment, read with the same variables and in the
// same order as the Python package's auto_provider():
//
//   ANTHROPIC_API_KEY            -> AnthropicProvider        (model: ANTHROPIC_MODEL)
//   OPENAI_API_KEY               -> OpenAICompatibleProvider (model: OPENAI_MODEL)
//   GEMINI_API_KEY               -> not in this package yet: an error says so
//   MENDWRIGHT_GATEWAY           -> not in this package yet: an error says so
//   MENDWRIGHT_LOCAL_MODEL       -> LocalModelProvider       (host: OLLAMA_HOST)
//   OLLAMA_MODEL or OLLAMA_HOST  -> Ollama through OpenAICompatibleProvider.forOllama
//
// MENDWRIGHT_PROVIDER forces one of them. There is no default model: a key without its
// *_MODEL variable fails with an error that names the variable. A variable this package
// cannot use yet stops the search instead of falling through to the next provider, so the
// page never goes to another provider than the one the environment picks.

import type { HealProvider } from './base';
import { AnthropicProvider } from './anthropic';
import { LocalModelProvider, ollamaHost } from './local';
import { OpenAICompatibleProvider } from './openai';

const SELECT: [string, string[]][] = [
  ['anthropic', ['ANTHROPIC_API_KEY']],
  ['openai', ['OPENAI_API_KEY']],
  ['gemini', ['GEMINI_API_KEY']],
  ['gateway', ['MENDWRIGHT_GATEWAY']],
  ['local', ['MENDWRIGHT_LOCAL_MODEL']],
  ['ollama', ['OLLAMA_MODEL', 'OLLAMA_HOST']],
];
const NAMES = SELECT.map(([name]) => name);

export const NOTHING_CONFIGURED = 'mendwright: no model provider is configured. Set one of these, checked in '
  + 'this order: ANTHROPIC_API_KEY and ANTHROPIC_MODEL (Anthropic); OPENAI_API_KEY and OPENAI_MODEL '
  + '(OpenAI or a compatible host, with OPENAI_BASE_URL); MENDWRIGHT_LOCAL_MODEL (mendwright\'s local selector '
  + 'model in Ollama); OLLAMA_MODEL, plus OLLAMA_HOST if Ollama is not on http://127.0.0.1:11434 (local '
  + 'Ollama). Or set MENDWRIGHT_PROVIDER to one of: anthropic, openai, local, ollama, or pass provider to open().';

function env(name: string): string | undefined {
  const value = process.env[name];
  return value ? value : undefined;
}

function requireModel(variable: string, model: string | undefined): string {
  const resolved = model || env(variable);
  if (!resolved) {
    throw new TypeError('mendwright: no model configured for this provider. Set the ' + variable
      + " environment variable (use a current id from your provider's model list; mendwright has no default model).");
  }
  return resolved;
}

function unsupported(name: string): never {
  const what = name === 'gemini' ? 'GEMINI_API_KEY is set, but mendwright for TypeScript has no Gemini provider yet'
    : 'MENDWRIGHT_GATEWAY is set, but mendwright for TypeScript has no gateway provider yet';
  throw new TypeError('mendwright: ' + what + ', so it does not pick another provider in its place. Unset it, '
    + 'set MENDWRIGHT_PROVIDER to anthropic, openai, local or ollama, or pass provider to open().');
}

const BUILDERS: Record<string, (model?: string) => HealProvider> = {
  anthropic: (model) => new AnthropicProvider({ model: requireModel('ANTHROPIC_MODEL', model) }),
  openai: (model) => new OpenAICompatibleProvider({ model: requireModel('OPENAI_MODEL', model) }),
  gemini: () => unsupported('gemini'),
  gateway: () => unsupported('gateway'),
  local: (model) => new LocalModelProvider({ model: model || env('MENDWRIGHT_LOCAL_MODEL') }),
  // Keyless: '' never falls back to OPENAI_API_KEY, so a cloud key is not sent to the local host.
  ollama: (model) => OpenAICompatibleProvider.forOllama(requireModel('OLLAMA_MODEL', model), {
    apiKey: '', baseUrl: env('OLLAMA_HOST') ? ollamaHost(env('OLLAMA_HOST')) + '/v1' : undefined,
  }),
};

/** The provider the environment selects. Throws TypeError, with the variables to set, when
 * nothing is configured, a model variable is missing, or MENDWRIGHT_PROVIDER names a
 * provider this package does not have. */
export function autoProvider(model?: string): HealProvider {
  const forced = env('MENDWRIGHT_PROVIDER');
  if (forced) {
    const name = forced.trim().toLowerCase();
    if (!(name in BUILDERS)) {
      throw new TypeError('mendwright: MENDWRIGHT_PROVIDER=' + forced + ' is not recognised. Valid values: '
        + NAMES.join(', ') + '.');
    }
    return BUILDERS[name](model);
  }
  for (const [name, variables] of SELECT) {
    if (variables.some((v) => env(v))) return BUILDERS[name](model);
  }
  throw new TypeError(NOTHING_CONFIGURED);
}
