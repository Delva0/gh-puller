import { z } from 'zod';
import { agentSchema, emptyPreferences, eventSchema, preferencesSchema, settingsSchema, publicData,
  type Catalog, type Conversation, type Preferences } from './types';

let connection: Promise<IDBDatabase> | undefined;
const savedEvents = new Map<string, Conversation['events']>();
const writes = new Map<string, Promise<void>>();
function serialize(id: string, operation: () => Promise<void>) {
  const next = (writes.get(id) ?? Promise.resolve()).catch(() => {}).then(operation);
  writes.set(id, next);
  return next;
}
function database() {
  if (!connection) connection = new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open('trace-agent-chat', 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore('chats', { keyPath: 'id' });
      request.result.createObjectStore('events', { keyPath: ['chat_id', 'seq'] });
      request.result.createObjectStore('preferences');
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  return connection;
}
function complete(tx: IDBTransaction) {
  return new Promise<void>((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = tx.onabort = () => reject(tx.error ?? new Error('无法保存浏览器历史'));
  });
}
function result<T>(request: IDBRequest<T>) {
  return new Promise<T>((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
export async function loadHistory(): Promise<Conversation[]> {
  const db = await database();
  const tx = db.transaction(['chats', 'events']);
  const [chats, events] = await Promise.all([
    result(tx.objectStore('chats').getAll()), result(tx.objectStore('events').getAll()),
  ]);
  const grouped = new Map<string, Conversation['events']>();
  for (const { chat_id, ...event } of events) {
    if (!grouped.has(chat_id)) grouped.set(chat_id, []);
    grouped.get(chat_id)!.push(eventSchema.parse(event));
  }
  return chats.map(chat => {
    const entries = (grouped.get(chat.id) ?? []).sort((a, b) => a.seq - b.seq);
    savedEvents.set(chat.id, entries);
    return { ...chat, settings: settingsSchema.parse(chat.settings), events: entries, readonly: false, live: false };
  });
}
export function saveChat(chat: Conversation) { return serialize(chat.id, async () => {
  const { events, ...metadata } = chat;
  const db = await database();
  const tx = db.transaction(['chats', 'events'], 'readwrite');
  tx.objectStore('chats').put(publicData(metadata));
  const before = savedEvents.get(chat.id) ?? [];
  let common = 0;
  while (common < events.length && common < before.length && events[common] === before[common]) common++;
  if (common < before.length) tx.objectStore('events').delete(IDBKeyRange.bound([chat.id, common + 1], [chat.id, Number.MAX_SAFE_INTEGER]));
  for (const event of events.slice(common)) tx.objectStore('events').put({ chat_id: chat.id, ...publicData(event) });
  await complete(tx);
  savedEvents.set(chat.id, events);
}); }
export function deleteChat(id: string) { return serialize(id, async () => {
  const db = await database();
  const tx = db.transaction(['chats', 'events'], 'readwrite');
  tx.objectStore('chats').delete(id);
  tx.objectStore('events').delete(IDBKeyRange.bound([id, 0], [id, Number.MAX_SAFE_INTEGER]));
  await complete(tx);
  savedEvents.delete(id);
}); }
export async function saveSettings(settings: Preferences) {
  const db = await database();
  const tx = db.transaction('preferences', 'readwrite');
  tx.objectStore('preferences').put(preferencesSchema.parse(settings), 'settings');
  await complete(tx);
}
export async function loadSettings(catalog: Catalog): Promise<Preferences | undefined> {
  const db = await database();
  const value = await result(db.transaction('preferences').objectStore('preferences').get('settings'));
  if (!value) return undefined;
  if (value.version === 2) return preferencesSchema.parse(value);
  const old = settingsSchema.parse(value);
  return { ...emptyPreferences, connection: { base_url: old.base_url },
    model: { model: old.model, reasoning_effort: old.reasoning_effort, thinking: old.thinking },
    tools: Object.fromEntries(catalog.agents.flatMap(agent => agent.fields)
      .filter(field => field.tool && field.key in old).map(field => [field.key, old[field.key]])) as Preferences['tools'] };
}

const exportChat = z.object({
  title: z.string().min(1).max(100), created: z.string(), agent: agentSchema,
  settings: settingsSchema, events: z.array(eventSchema).max(100000), renamed: z.boolean().optional(),
  branch_id: z.string().optional(), branches: z.array(z.object({ id: z.string(), agent: agentSchema,
    settings: settingsSchema, events: z.array(eventSchema).max(100000) })).max(200).optional(),
});
const historySchema = z.object({ version: z.union([z.literal(1), z.literal(2)]), conversations: z.array(exportChat).max(200) });
export function exportHistory(chats: Conversation[]) {
  return historySchema.parse(publicData({ version: 2, conversations: chats }));
}
export function importHistory(value: unknown): Conversation[] {
  if (value && typeof value === 'object' && 'session' in value && 'events' in value) {
    const item = value as { session: object; events: unknown; branches?: unknown; branch_id?: string };
    value = { version: 2, conversations: [{ created: new Date().toISOString(), ...item.session,
      events: item.events, branches: item.branches, branch_id: item.branch_id }] };
  }
  return historySchema.parse(publicData(value)).conversations.map(chat => {
    if ([chat.events, ...(chat.branches ?? []).map(branch => branch.events)].some(events => events.some((event, index) => event.seq !== index + 1))) {
      throw new Error('事件序号须严格递增');
    }
    return { ...chat, id: crypto.randomUUID(), readonly: false, live: false };
  });
}
export function exportEvents(chat: Conversation) {
  return publicData({ version: 2, session: { title: chat.title, created: chat.created, agent: chat.agent, settings: chat.settings },
    events: chat.events, branches: chat.branches, branch_id: chat.branch_id });
}
export function download(name: string, value: unknown) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
