// Generator / Provider / Model 统一 target 契约(前端侧)。
//
// 语义与 gh_puller.agent.adapters 注册表一一对应:
// - generator:生成管线(cc/dsh/codex/llm),决定 API surface/编排/工具生命周期;
// - provider:模型服务提供方(anthropic/deepseek/openai),决定连接(API key/base URL);
// - model:provider 下的模型标识。openai-compatible 只是 openai + 自定义 base_url 的形态。
//
// 持久化规则(跨页面一律只走公开三元组):
// - URL/localStorage:仅 generator/provider/model(publicTargetOf);
// - api_key/base_url:仅当前标签页 sessionStorage(见 saveCreds/loadCreds),绝不进
//   URL/localStorage/请求日志。

export interface GeneratorConfigItem {
  id: string;
  name: string;
  defaultProvider: string;
  providers: string[];
  capability: string;
  defaultModelEnv: string | null;
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
  defaultTarget: { generator: string; provider: string; model: string };
}

/** 浏览器端 target 选择:公开三元组 + 请求态凭证(凭证仅存 sessionStorage)。 */
export interface TargetConfig {
  generator: string;
  provider: string;
  model: string;
  api_key?: string;
  base_url?: string;
}

/** 发布/持久化安全副本:清空凭证。 */
export function publicTargetOf(t: TargetConfig): TargetConfig {
  return { generator: t.generator, provider: t.provider, model: t.model };
}

export const DEFAULT_LOAD_GENERATORS_CONFIG = async (): Promise<GeneratorsConfig> => {
  const response = await fetch('/api/generators/config');
  if (!response.ok) {
    throw new Error(`Error fetching generators config: ${response.status}`);
  }
  return response.json();
};

// ---- 凭证:仅 sessionStorage(每标签页),按仓库键隔离 ----

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
    // 存储不可用(隐私模式等):仅影响本次会话,请求取空凭证走环境缺省
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

/** 依据注册表把 target 内尽量补齐(空 generator → defaultGenerator;空 provider → 该 generator 默认)。 */
export function normalizeWithRegistry(
  t: TargetConfig,
  cfg: GeneratorsConfig,
): TargetConfig {
  const generator = t.generator || cfg.defaultGenerator;
  const gen = cfg.generators.find((g) => g.id === generator);
  let provider = t.provider;
  if (!provider && gen) provider = gen.defaultProvider;
  const prov = cfg.providers.find((p) => p.id === provider);
  let model = t.model;
  if (!model && prov && prov.models.length > 0) model = prov.models[0];
  return { generator, provider: provider || '', model, api_key: t.api_key, base_url: t.base_url };
}
