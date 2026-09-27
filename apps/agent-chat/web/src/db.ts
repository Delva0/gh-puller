import { z } from 'zod';
import { agentSchema, eventSchema, settingsSchema, publicData, type Conversation, type Settings } from './types';

let connection: Promise<IDBDatabase> | undefined;
const savedSeq = new Map<string, number>();
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
    savedSeq.set(chat.id, entries.at(-1)?.seq ?? 0);
    return { ...chat, settings: settingsSchema.parse(chat.settings), events: entries };
  });
}
export async function saveChat(chat: Conversation) {
  const { events, ...metadata } = publicData(chat);
  const db = await database();
  const tx = db.transaction(['chats', 'events'], 'readwrite');
  tx.objectStore('chats').put(metadata);
  const after = savedSeq.get(chat.id) ?? 0;
  for (const event of events) if (event.seq > after) tx.objectStore('events').put({ chat_id: chat.id, ...event });
  await complete(tx);
  savedSeq.set(chat.id, events.at(-1)?.seq ?? 0);
}
export async function deleteChat(id: string) {
  const db = await database();
  const tx = db.transaction(['chats', 'events'], 'readwrite');
  tx.objectStore('chats').delete(id);
  tx.objectStore('events').delete(IDBKeyRange.bound([id, 0], [id, Number.MAX_SAFE_INTEGER]));
  await complete(tx);
  savedSeq.delete(id);
}
export async function saveSettings(settings: Settings) {
  const db = await database();
  const tx = db.transaction('preferences', 'readwrite');
  tx.objectStore('preferences').put(settingsSchema.parse(settings), 'settings');
  await complete(tx);
}
export async function loadSettings(): Promise<Settings | undefined> {
  const db = await database();
  const value = await result(db.transaction('preferences').objectStore('preferences').get('settings'));
  return value ? settingsSchema.parse(value) : undefined;
}

const exportChat = z.object({
  title: z.string().min(1).max(100), created: z.string(), agent: agentSchema,
  settings: settingsSchema, events: z.array(eventSchema).max(100000), renamed: z.boolean().optional(),
});
const historySchema = z.object({ version: z.literal(1), conversations: z.array(exportChat).max(200) });
export function exportHistory(chats: Conversation[]) {
  return historySchema.parse(publicData({ version: 1, conversations: chats }));
}
export function importHistory(value: unknown): Conversation[] {
  return historySchema.parse(publicData(value)).conversations.map(chat => {
    if (chat.events.some((event, index) => index > 0 && event.seq <= chat.events[index - 1].seq)) {
      throw new Error('事件序号须严格递增');
    }
    return { ...chat, id: crypto.randomUUID(), readonly: true };
  });
}
export function download(name: string, value: unknown) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
