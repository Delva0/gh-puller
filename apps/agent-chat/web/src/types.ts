import { z } from 'zod';

export const agentSchema = z.enum(['github', 'gitcode', 'code', 'web']);
export type Agent = z.infer<typeof agentSchema>;
export const settingsSchema = z.object({
  base_url: z.string().max(2048), model: z.string().min(1).max(200),
  backend: z.string(), ptc: z.enum(['off', 'A', 'B']),
  max_steps: z.number().int().min(1).max(128), concurrency: z.number().int().min(1).max(16),
  reasoning_effort: z.enum(['low', 'high', 'max']), thinking: z.boolean(),
  max_tokens: z.number().int().min(256).max(32768),
  web_search_backend: z.enum(['auto', 'brave', 'duckduckgo']),
  web_search_concurrency: z.number().int().min(1).max(8),
  web_search_interval: z.number().min(0).max(60), multimodal: z.boolean(),
});
export type Settings = z.infer<typeof settingsSchema>;
export type Credentials = { api_key: string; github_token: string; gitcode_token: string; brave_api_key: string };
export const emptyCredentials: Credentials = { api_key: '', github_token: '', gitcode_token: '', brave_api_key: '' };
export const fallbackSettings: Settings = {
  base_url: '', model: 'deepseek-v4.1-flash', backend: 'dsl', ptc: 'off', max_steps: 32,
  concurrency: 8, reasoning_effort: 'high', thinking: true, max_tokens: 8192,
  web_search_backend: 'brave', web_search_concurrency: 1, web_search_interval: 2, multimodal: true,
};
export const eventSchema = z.object({
  seq: z.number().int().positive(), type: z.string().max(100), at: z.string(),
  query_id: z.string().nullable(), data: z.record(z.string(), z.unknown()),
  source_seq: z.number().optional(), elapsed_ms: z.number().optional(),
});
export type ChatEvent = z.infer<typeof eventSchema>;
export interface Conversation {
  id: string; server_id?: string; title: string; created: string; agent: Agent;
  settings: Settings; events: ChatEvent[]; readonly: boolean; renamed?: boolean;
}
export interface SessionView {
  id: string; agent: Agent; title: string; created: string; running: boolean;
  query_id: string | null; seq: number; settings: Settings | null;
  has_credentials: boolean; readonly: boolean;
}
export interface Capability {
  id: Agent; name: string; available: boolean; reason: string;
  backends: string[]; ptc: boolean; web: boolean;
}
export interface Catalog { agents: Capability[]; defaults: Settings; revision: string; idle_minutes: number }

export function settingsFor(agent: Agent, settings: Settings): Settings {
  return { ...settings, backend: agent === 'web' || agent === 'code' ? '' :
    ['dsl', 'rest'].includes(settings.backend) ? settings.backend : 'dsl',
  ptc: agent === 'web' || agent === 'code' ? 'off' : settings.ptc };
}

export function isRunning(chat: Conversation): boolean {
  const last = chat.events.filter(e => e.type === 'query/start' || e.type === 'query/end').at(-1);
  return last?.type === 'query/start' && !chat.readonly;
}

export function publicData<T>(input: T, values: string[] = []): T {
  function clean(value: unknown): unknown {
    if (typeof value === 'string') {
      for (const secret of values.filter(Boolean).sort((a, b) => b.length - a.length)) {
        value = (value as string).split(secret).join('[redacted]');
      }
      return value;
    }
    if (Array.isArray(value)) return value.map(clean);
    if (value && typeof value === 'object') {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key,
        /(api.?key|access.?token|private.?token|password|authorization|credential|secret|token)$/i.test(key)
          ? '[redacted]' : clean(item)]));
    }
    return value;
  }
  return clean(input) as T;
}
