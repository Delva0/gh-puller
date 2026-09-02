// Generator → generator_config 统一 target 契约(前端侧)。配置形态分类
// (file/object,configKind)见 gh_puller/agent/adapters/__init__.py config 概念
// 契约;本文件只写前端特有的持久化与提交规则(与后端 _strip_creds 落盘形态一致):
// - URL/localStorage:公开部分(strippedTarget)—— file 类 = generator + config_path
//   (路径非凭证);object 类 = generator + provider/model;
// - api_key/base_url 仅当前标签页 sessionStorage(见 saveCreds/loadCreds),绝不进
//   URL/localStorage/请求日志;file 类的生成器配置不携带凭证字段(422 语义)。
// 组装为请求体的统一入口:asyncTargetRequest(按注册表 configKind 收窄字段)。

export interface GeneratorConfigItem {
  id: string;
  name: string;
  configKind: 'file' | 'object';
  capability: string;
  defaultProvider: string; // object 类有效(file 类仅旧投影展示)
  providers: string[]; // object 类选择列表(file 类为空)
  defaultModelEnv: string | null; // object 类
  configDefault: string | null; // file 类(默认路径提示)
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

/** 浏览器端 target 选择:公开部分按 kind 分字段;api_key/base_url 仅 object 类请求态。 */
export interface TargetConfig {
  generator: string;
  config_path?: string; // file 类
  provider?: string; // object 类
  model?: string; // object 类
  api_key?: string; // object 类凭证(仅 sessionStorage,见 saveCreds/loadCreds)
  base_url?: string; // object 类凭证(同上)
}

/** 落盘/判等安全副本:剥凭证(api_key/base_url);file 类保留 config_path(非凭证)。 */
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

// ---- 请求体组装(注册表 configKind 收窄;注册表一次性缓存) ----

/** 请求体中 target 的嵌套形态(与后端 TargetInput 同形)。 */
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
      configCache = null; // 网络瞬断允许重试
      throw err;
    });
  }
  return configCache;
}

/**
 * target(平面选择态)→ 请求体 target(按 generator 的 configKind 收窄字段)。
 *
 * file 类:仅 config_path(默认取注册表 configDefault;空 → 服务端 env/缺省解析),
 * 丢弃任何凭证/模型字段 —— 与后端 422 语义一致;object 类:provider/model/凭证。
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

/** 依据注册表把 target 内尽量补齐(空 generator → defaultGenerator;给各 kind 的缺省位填默认值)。 */
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
