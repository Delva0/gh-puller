import type { Branch, Conversation } from './types';

export function branches(chat: Conversation): Branch[] {
  const current: Branch = { id: chat.branch_id ?? 'original', events: chat.events, agent: chat.agent, settings: chat.settings };
  const saved = chat.branches ?? [];
  return saved.some(branch => branch.id === current.id) ? saved.map(branch => branch.id === current.id ? current : branch) : [...saved, current];
}
export function fork(chat: Conversation, query: string): Conversation {
  const index = chat.events.findIndex(event => event.type === 'query/start' && event.query_id === query);
  if (index < 0) return chat;
  return { ...chat, branches: branches(chat), branch_id: crypto.randomUUID(), events: chat.events.slice(0, index), live: false,
    source_id: chat.server_id ?? chat.source_id, server_id: undefined };
}
export function versions(chat: Conversation, index: number) {
  const prefix = chat.events.filter(event => event.type === 'query/start').slice(0, index).map(event => event.query_id);
  const found = new Map<string, Branch>();
  for (const branch of branches(chat)) {
    const turns = branch.events.filter(event => event.type === 'query/start');
    if (turns[index]?.query_id && prefix.every((id, position) => turns[position]?.query_id === id)) found.set(turns[index].query_id, branch);
  }
  return [...found.values()];
}
export function selectBranch(chat: Conversation, selected: Branch): Conversation {
  return { ...chat, branches: branches(chat), branch_id: selected.id, events: selected.events,
    agent: selected.agent, settings: selected.settings, live: false, source_id: chat.server_id ?? chat.source_id, server_id: undefined };
}
