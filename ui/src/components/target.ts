// Browser target contract for the adapter config kinds defined in gh_puller/agent/adapters.
// `strippedTarget` is safe for URLs and localStorage: file targets retain their non-secret config
// path, while object targets retain provider and model. Credentials stay in per-tab sessionStorage
// and request state, never URLs, localStorage, or request logs. File targets never carry credential
// fields. `buildTargetRequest` narrows fields by registered config kind.

export interface GeneratorConfigItem {
  id: string;
  name: string;
  configKind: 'file' | 'object';
  capability: string;
  defaultProvider: string; // Object target default; file-target projections are display-only.
  providers: string[]; // Provider choices for object targets; empty for file targets.
  defaultModelEnv: string | null; // Object targets only.
  configDefault: string | null; // Default path hint for file targets.
}

export interface ProviderConfigItem {
  id: string;
  name: string;
  apiKeyEnv: string | null;
  baseUrlEnv: string | null;
  baseUrlDefault: string | null;
  models: string[];
  supportsCustomModel: boolean;
}

export interface GeneratorsConfig {
  generators: GeneratorConfigItem[];
  providers: ProviderConfigItem[];
  defaultGenerator: string;
  defaultTarget: {
    generator: string;
    generator_config: { config_path?: string; provider?: string; model?: string };
  };
}

/** Browser selection state with kind-specific public fields and request-only object credentials. */
export interface TargetConfig {
  generator: string;
  config_path?: string; // File targets only.
  provider?: string; // Object targets only.
  model?: string; // Object targets only.
  api_key?: string; // Object credential persisted only through saveCreds/loadCreds.
  base_url?: string; // Object credential persisted only through saveCreds/loadCreds.
}

/** Returns a credential-free copy safe for persistence and equality checks. */
export function strippedTarget(t: TargetConfig): TargetConfig {
  return {
    generator: t.generator,
    config_path: t.config_path,
    provider: t.provider,
    model: t.model,
  };
}

export const DEFAULT_LOAD_GENERATORS_CONFIG = async (): Promise<GeneratorsConfig> => {
  const response = await fetch('/api/generators/config');
  if (!response.ok) {
    throw new Error(`Error fetching generators config: ${response.status}`);
  }
  return response.json();
};

// ---- Request assembly and registry cache ----

/** Nested target shape accepted by the backend TargetInput contract. */
export interface TargetRequest {
  generator: string;
  generator_config: {
    config_path?: string;
    provider?: string;
    model?: string;
    base_url?: string;
    api_key?: string;
  };
}

let configCache: GeneratorsConfig | null = null;

async function loadConfigOnce(): Promise<GeneratorsConfig> {
  if (configCache === null) {
    configCache = await DEFAULT_LOAD_GENERATORS_CONFIG().catch((err) => {
      configCache = null; // Let later calls retry transient registry failures.
      throw err;
    });
  }
  return configCache;
}

/**
 * Converts flat browser state into a target request narrowed by the generator config kind.
 *
 * File targets carry only `config_path`, using the registry default when absent; the server
 * resolves an empty path from its environment. Object targets carry provider, model, and
 * credentials. Fields from the other kind are discarded to match backend validation.
 */
export async function buildTargetRequest(t: TargetConfig): Promise<TargetRequest> {
  const cfg = await loadConfigOnce();
  const generator = t.generator || cfg.defaultGenerator;
  const gen = cfg.generators.find((g) => g.id === generator);
  const gc: TargetRequest['generator_config'] = {};
  if (gen?.configKind === 'file') {
    gc.config_path = t.config_path || gen.configDefault || '';
  } else {
    if (t.provider) gc.provider = t.provider;
    if (t.model) gc.model = t.model;
    if (t.base_url) gc.base_url = t.base_url;
    if (t.api_key) gc.api_key = t.api_key;
  }
  return { generator, generator_config: gc };
}

// ---- Per-tab credentials isolated by repository ----

const CREDS_KEY_PREFIX = 'gh-puller-target-creds';

function credsKey(repoUrl: string): string {
  return `${CREDS_KEY_PREFIX}:${repoUrl.trim()}`;
}

export function saveCreds(
  repoUrl: string,
  creds: { api_key?: string; base_url?: string },
): void {
  try {
    sessionStorage.setItem(credsKey(repoUrl), JSON.stringify(creds));
  } catch {
    // Storage may be unavailable in private browsing; empty credentials use server defaults.
  }
}

export function loadCreds(repoUrl: string): { api_key?: string; base_url?: string } {
  try {
    const raw = sessionStorage.getItem(credsKey(repoUrl));
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

export function clearCreds(repoUrl: string): void {
  try {
    sessionStorage.removeItem(credsKey(repoUrl));
  } catch {
    // no-op
  }
}

/** Fills missing kind-specific target fields from registry defaults. */
export function normalizeWithRegistry(
  t: TargetConfig,
  cfg: GeneratorsConfig,
): TargetConfig {
  const generator = t.generator || cfg.defaultGenerator;
  const gen = cfg.generators.find((g) => g.id === generator);
  let config_path = t.config_path;
  if (gen?.configKind === 'file' && !config_path) config_path = gen.configDefault || '';
  let provider = t.provider;
  if (gen?.configKind === 'object' && !provider && gen.defaultProvider) {
    provider = gen.defaultProvider;
  }
  const prov = cfg.providers.find((p) => p.id === provider);
  let model = t.model;
  if (gen?.configKind === 'object' && !model && prov && prov.models.length > 0) {
    model = prov.models[0];
  }
  return { generator, config_path, provider, model, api_key: t.api_key, base_url: t.base_url };
}
