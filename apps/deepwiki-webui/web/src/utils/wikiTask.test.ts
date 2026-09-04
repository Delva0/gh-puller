import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  getWikiTask,
  submitWikiTask,
  subscribeWikiTask,
  type WikiTaskStatusDto,
} from './wikiTask';

function response(data: unknown, status = 200): Response {
  return {
    json: async () => data,
    ok: status >= 200 && status < 300,
    status,
    text: async () => String(data),
  } as Response;
}

class FakeEventSource {
  static readonly CLOSED = 2;
  static instance: FakeEventSource;

  readonly listeners = new Map<string, EventListener>();
  readyState = 1;

  constructor(readonly url: string) {
    FakeEventSource.instance = this;
  }

  addEventListener(type: string, listener: EventListener): void {
    this.listeners.set(type, listener);
  }

  close(): void {
    this.readyState = FakeEventSource.CLOSED;
  }

  emit(type: string, data?: string): void {
    this.listeners.get(type)?.({ data } as MessageEvent);
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('wiki task HTTP contract', () => {
  it('drops empty top-level fields without flattening the target contract', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(response({ task_id: 'task-1' }));
    vi.stubGlobal('fetch', fetchMock);

    await submitWikiTask({
      repo_url: '/repo',
      type: 'local',
      owner: 'local',
      repo: 'repo',
      comprehensive: false,
      token: '',
      excluded_dirs: undefined,
      target: {
        generator: 'llm',
        provider: 'openai',
        model: 'm',
        api_key: 'secret',
      },
    });

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(String(init?.body))).toEqual({
      repo_url: '/repo',
      type: 'local',
      owner: 'local',
      repo: 'repo',
      comprehensive: false,
      target: {
        generator: 'llm',
        provider: 'openai',
        model: 'm',
        api_key: 'secret',
      },
    });
  });

  it('encodes task identifiers and treats a missing task as absent state', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(response({}, 404));
    vi.stubGlobal('fetch', fetchMock);

    await expect(getWikiTask('owner/task')).resolves.toBeNull();
    expect(fetchMock).toHaveBeenCalledWith('/api/wiki/tasks/owner%2Ftask');
  });
});

describe('wiki task event stream', () => {
  it('delivers progress and terminal state before closing', () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    const onProgress = vi.fn();
    const onDone = vi.fn();
    const onError = vi.fn();
    const unsubscribe = subscribeWikiTask('owner/task', { onProgress, onDone, onError });
    const stream = FakeEventSource.instance;
    const status = { id: 'task-1', status: 'generating' } as WikiTaskStatusDto;

    expect(stream.url).toBe('/api/wiki/tasks/owner%2Ftask/stream');
    stream.emit('progress', JSON.stringify(status));
    stream.emit('progress', 'not-json');
    stream.emit('done', JSON.stringify({ ...status, status: 'completed' }));

    expect(onProgress).toHaveBeenCalledOnce();
    expect(onProgress).toHaveBeenCalledWith(status);
    expect(onDone).toHaveBeenCalledWith({ ...status, status: 'completed' });
    expect(onError).not.toHaveBeenCalled();
    expect(stream.readyState).toBe(FakeEventSource.CLOSED);
    unsubscribe();
  });

  it('distinguishes a server error frame from a closed connection', () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    const onError = vi.fn();
    subscribeWikiTask('task-1', { onError });
    const stream = FakeEventSource.instance;

    stream.emit('error', JSON.stringify({ error: 'generation failed' }));
    expect(onError).toHaveBeenLastCalledWith('generation failed');

    stream.readyState = FakeEventSource.CLOSED;
    stream.emit('error');
    expect(onError).toHaveBeenLastCalledWith('Task stream connection lost');
  });
});
