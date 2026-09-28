import { z } from 'zod';

export const agentSchema = z.string().min(1).max(100);
export type Agent = z.infer<typeof agentSchema>;
const valuesSchema = z.record(z.string(), z.json());
export const settingsSchema = z.object({
  base_url: z.string().max(2048), model: z.string().max(200),
  reasoning_effort: z.string(), thinking: z.boolean(), max_tokens: z.number().int().min(0),
  options: valuesSchema.default({}),
}).passthrough();
export type Settings = z.infer<typeof settingsSchema>;
export type ConfigValues = z.infer<typeof valuesSchema>;
export type Credentials = Record<string, string>;
export const emptyCredentials: Credentials = { api_key: '' };
const modelSettingsSchema = z.object({ model: z.string(), reasoning_effort: z.string(), thinking: z.boolean() });
export type ModelSettings = z.infer<typeof modelSettingsSchema>;
export const preferencesSchema = z.object({
  version: z.literal(2), connection: z.object({ base_url: z.string().max(2048) }).partial(),
  model: modelSettingsSchema.partial(), agents: z.record(z.string(), valuesSchema), tools: valuesSchema,
});
export type Preferences = z.infer<typeof preferencesSchema>;
export const emptyPreferences: Preferences = { version: 2, connection: {}, model: {}, agents: {}, tools: {} };
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
export interface ConfigField {
  key: string; default: ConfigValues[string]; type: string; nullable: boolean; description: string;
  choices: { value: ConfigValues[string]; reason: string }[]; tool: string | null;
}
export interface ToolConfig { id: string; credentials: string[] }
export interface Capability {
  id: Agent; name: string; available: boolean; reason: string;
  defaults: ConfigValues; fields: ConfigField[]; tools: ToolConfig[];
}
export interface Catalog {
  agents: Capability[]; defaults: Settings; default_agent: string;
  revision: string; idle_minutes: number;
}

export function settingsFor(agent: Agent, preferences: Preferences, catalog: Catalog): Settings {
  const capability = catalog.agents.find(item => item.id === agent);
  const options: ConfigValues = {};
  for (const field of capability?.fields ?? []) {
    const saved = field.tool ? preferences.tools : preferences.agents[agent];
    options[field.key] = saved && field.key in saved ? saved[field.key] : field.default;
  }
  return { ...catalog.defaults, ...preferences.connection, ...preferences.model, options };
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
