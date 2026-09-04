import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  buildTargetRequest,
  clearCreds,
  loadCreds,
  normalizeWithRegistry,
  saveCreds,
  strippedTarget,
  type GeneratorsConfig,
} from './target';

const CONFIG: GeneratorsConfig = {
  generators: [
    {
      id: 'cc',
      name: 'Claude Code',
      configKind: 'file',
      capability: 'agent',
      defaultProvider: '',
      providers: [],
      defaultModelEnv: null,
      configDefault: '/config/claude.json',
    },
    {
      id: 'llm',
      name: 'LLM',
      configKind: 'object',
      capability: 'chat',
      defaultProvider: 'openai',
      providers: ['openai'],
      defaultModelEnv: null,
      configDefault: null,
    },
  ],
  providers: [
    {
      id: 'openai',
      name: 'OpenAI',
      apiKeyEnv: 'OPENAI_API_KEY',
      baseUrlEnv: null,
      baseUrlDefault: 'https://api.openai.com/v1',
      models: ['gpt-test'],
      supportsCustomModel: true,
    },
  ],
  defaultGenerator: 'cc',
  defaultTarget: { generator: 'cc', generator_config: {} },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('target persistence boundary', () => {
  it('removes request-only credentials from persistent state', () => {
    const target = strippedTarget({
      generator: 'llm',
      provider: 'openai',
      model: 'gpt-test',
      api_key: 'secret',
      base_url: 'https://private.example/v1',
    });

    expect(target).toEqual({
      generator: 'llm',
      config_path: undefined,
      provider: 'openai',
      model: 'gpt-test',
    });
    expect(target).not.toHaveProperty('api_key');
    expect(target).not.toHaveProperty('base_url');
  });

  it('keeps credentials scoped by repository in session storage', () => {
    const values = new Map<string, string>();
    vi.stubGlobal('sessionStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      removeItem: (key: string) => values.delete(key),
      setItem: (key: string, value: string) => values.set(key, value),
    });

    saveCreds(' /repo/a ', { api_key: 'a', base_url: 'https://a.example' });
    saveCreds('/repo/b', { api_key: 'b' });
    expect(loadCreds('/repo/a')).toEqual({ api_key: 'a', base_url: 'https://a.example' });
    expect(loadCreds('/repo/b')).toEqual({ api_key: 'b' });
    clearCreds('/repo/a');
    expect(loadCreds('/repo/a')).toEqual({});
    expect(loadCreds('/repo/b')).toEqual({ api_key: 'b' });
  });
});

describe('target request boundary', () => {
  it('fills registry defaults without crossing file and object configuration kinds', () => {
    expect(normalizeWithRegistry({ generator: '' }, CONFIG)).toMatchObject({
      generator: 'cc',
      config_path: '/config/claude.json',
    });
    expect(normalizeWithRegistry({ generator: 'llm' }, CONFIG)).toMatchObject({
      generator: 'llm',
      provider: 'openai',
      model: 'gpt-test',
    });
  });

  it('sends only fields allowed by each generator configuration kind', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue({ ok: true, json: async () => CONFIG } as Response),
    );

    await expect(
      buildTargetRequest({
        generator: 'cc',
        provider: 'ignored',
        model: 'ignored',
        api_key: 'ignored',
      }),
    ).resolves.toEqual({
      generator: 'cc',
      generator_config: { config_path: '/config/claude.json' },
    });
    await expect(
      buildTargetRequest({
        generator: 'llm',
        config_path: '/ignored',
        provider: 'openai',
        model: 'gpt-test',
        api_key: 'secret',
        base_url: 'https://private.example/v1',
      }),
    ).resolves.toEqual({
      generator: 'llm',
      generator_config: {
        provider: 'openai',
        model: 'gpt-test',
        api_key: 'secret',
        base_url: 'https://private.example/v1',
      },
    });
  });
});
