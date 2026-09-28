import { expect, test } from '@playwright/test';
import { toolSources } from '../src/sources';

const sources = (name: string, result: unknown) => toolSources({ id: 'call', name, args: {}, result: JSON.stringify(result) });

test('search sources count returned URLs, deduplicate batches and reject unsafe links', () => {
  const result = sources('web_search', { results: [
    { total_count: 9000, items: [{ title: 'Release one', href: 'https://github.com/o/r/releases/1', body: 'Release notes' },
      { title: 'Unsafe', href: 'javascript:alert(1)' }] },
    { items: [{ title: 'Duplicate', href: 'https://github.com/o/r/releases/1' },
      { title: 'Documentation', href: 'https://example.org/docs', body: '<b>Docs</b> excerpt' }] },
  ] });
  expect(result).toHaveLength(2);
  expect(result[0]).toMatchObject({ title: 'Release one', description: 'Release notes', site: 'GitHub' });
  expect(result[1]).toMatchObject({ title: 'Documentation', description: 'Docs excerpt', site: 'example.org' });
});

test('fetch sources describe fetched documents and keep successes from partial failures', () => {
  const result = { results: [{ url: 'https://example.org/doc', title: 'Document', text: 'Original excerpt' },
    { url: 'https://example.org/failure', error: { message: 'Failed' } }] };
  expect(sources('web_fetch', result)).toEqual([{ url: 'https://example.org/doc', title: 'Document',
    description: 'Original excerpt', site: 'example.org' }]);
  expect(toolSources({ id: 'partial', name: 'web_fetch', args: {}, error: { message: JSON.stringify(result) } })).toHaveLength(1);
});

test('GitHub aliases, projected REST and GraphQL sources exclude author profiles and request metadata', () => {
  expect(sources('github', { data: { repository: { nameWithOwner: 'psf/requests', url: 'https://github.com/psf/requests',
    description: 'A simple, yet elegant, HTTP library.' } } })[0].title).toBe('psf/requests');
  const item = { html_url: 'https://github.com/o/r/issues/1', title: 'An issue', body: 'Evidence',
    user: { name: 'Author', html_url: 'https://github.com/author' } };
  for (const name of ['github', 'github_rest']) expect(sources(name, { results: [
    { api_url: 'https://api.github.com/search/issues', data: { total_count: 100, items: [item] } },
    { api_url: 'https://api.github.com/search/issues', content: JSON.stringify([item]) },
  ] })).toHaveLength(1);
  for (const name of ['github_graphql', 'github_dsl', 'gitcode_dsl']) {
    const result = sources(name, { data: { customAlias: { nodes: [{ title: 'Issue two', url: 'https://gitcode.com/o/r/issues/2',
      body: 'Another source', author: { name: 'Author', url: 'https://gitcode.com/author' } }] } }, errors: [{ message: 'Partial result' }] });
    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({ title: 'Issue two', site: 'GitCode' });
  }
});

test('each successful resource read can provide its own fallback source, but empty searches cannot', () => {
  expect(sources('gitcode_api', { results: ['one', 'two'].map(name => ({
    api_url: `https://api.gitcode.com/api/v5/repos/o/${name}`, data: { name, description: 'Repository' },
  })) }).map(source => source.url)).toEqual(['https://gitcode.com/o/one', 'https://gitcode.com/o/two']);
  expect(sources('github_rest', { results: [{ api_url: 'https://api.github.com/search/issues', data: { items: [] } }] })).toEqual([]);
  expect(sources('github_graphql', { results: [{ api_url: 'https://api.github.com/graphql', data: {} }] })).toEqual([]);
  expect(sources('bash', { results: [{ url: 'https://example.org', title: 'Unrelated' }] })).toEqual([]);
});
