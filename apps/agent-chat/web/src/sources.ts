import type { TraceTool } from './components';

export interface Source { url: string; title: string; description: string; site: string }
export interface SourceSelection { turnId: string; callId: string }
type RecordValue = Record<string, unknown>;
const object = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {};
const text = (...values: unknown[]) => values.find(value => typeof value === 'string' && value.trim()) as string | undefined ?? '';
const parse = (value: unknown): unknown => {
  if (typeof value !== 'string') return value;
  try { return JSON.parse(value); } catch { return undefined; }
};
const excerpt = (value: string) => value.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 360);

export function sourceUrl(value: unknown): string {
  if (typeof value !== 'string') return '';
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : '';
  } catch { return ''; }
}

export function sourceSite(url: string): string {
  const host = new URL(url).hostname.replace(/^www\./, '');
  if (host === 'github.com' || host.endsWith('.github.com') || host === 'raw.githubusercontent.com') return 'GitHub';
  if (host === 'gitcode.com' || host.endsWith('.gitcode.com')) return 'GitCode';
  return host;
}

// Tool-specific presentation stays here; counts describe the sources present in this response.
export function toolSources(tool: TraceTool): Source[] {
  if (!/^(web_search|web_fetch|github(?:_.+)?|gitcode(?:_.+)?)$/.test(tool.name)) return [];
  const body = object(parse(tool.result ?? object(tool.error).message));
  const sources = new Map<string, Source>();
  const add = (raw: unknown, title: string, description = '') => {
    const url = sourceUrl(raw);
    if (!url) return;
    const previous = sources.get(url);
    sources.set(url, { url, title: previous?.title || excerpt(title) || url,
      description: previous?.description || excerpt(description), site: sourceSite(url) });
  };
  const results = Array.isArray(body.results) ? body.results : [body];
  if (tool.name === 'web_search') {
    for (const result of results) for (const item of Array.isArray(object(result).items) ? object(result).items as unknown[] : []) {
      const row = object(item);
      add(row.href ?? row.url, text(row.title), text(row.body, row.description, row.snippet));
    }
  } else if (tool.name === 'web_fetch') {
    for (const result of results) {
      const row = object(result);
      if (!row.error) add(row.url, text(row.title, row.url), text(row.text, row.content));
    }
  } else {
    const metadata = new Set(['author', 'owner', 'user', 'committer', 'license', 'assignees', 'labels', 'avatar', '_links']);
    const visit = (value: unknown): boolean => {
      if (Array.isArray(value)) return value.map(visit).some(Boolean);
      const row = object(value);
      if (row.error) return false;
      const title = text(row.title, row.full_name, row.nameWithOwner, row.name_with_namespace, row.name, row.path, row.tag_name,
        object(row.commit).message, row.message);
      const description = text(row.description, row.body, row.text, row.summary, object(row.commit).message);
      const url = sourceUrl(row.html_url ?? row.web_url ?? row.url);
      let found = Boolean(url && (title || description));
      if (found) add(url, title || excerpt(description).slice(0, 100), description);
      for (const [key, child] of Object.entries(row)) if (!metadata.has(key) && child && typeof child === 'object') found = visit(child) || found;
      return found;
    };
    for (const result of results) {
      const row = object(result);
      if (row.error) continue;
      const content = row.data ?? parse(row.content) ?? row;
      const found = visit(content);
      // A successful resource read can carry text instead of a projected source URL.
      const apiUrl = sourceUrl(row.api_url);
      if (apiUrl && !Array.isArray(content) && !object(content).items && !new URL(apiUrl).pathname.endsWith('/graphql')) {
        const data = object(content);
        if (!found && !row.incomplete_results && !new URL(apiUrl).pathname.startsWith('/search/')) {
          const url = new URL(apiUrl);
          const path = url.pathname.replace(/^\/api\/v5/, '');
          const repo = path.match(/^\/repos\/([^/]+\/[^/]+)(\/(?:issues|pulls|releases)\/[^/]+)?\/?$/);
          add(repo ? `https://${tool.name.startsWith('github') ? 'github.com' : 'gitcode.com'}/${repo[1]}${repo[2] ?? ''}` : apiUrl,
            text(data.title, data.full_name, data.name, repo?.[1], row.api_url), text(data.description, data.body, row.content));
        }
      }
    }
  }
  return [...sources.values()];
}
