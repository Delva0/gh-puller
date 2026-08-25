import { EventEmitter } from 'node:events'

import { describe, expect, it, vi } from 'vitest'

import { WorkerClient, type Spawner } from '../src/worker.js'

/** 假子进程:记录 spawn 参数与 stdin 帧,可注入响应帧 / 模拟崩溃。 */
class FakeChild extends EventEmitter {
  stdin = {
    write: (data: string) => {
      this.written += data
      return true
    },
    end: () => {},
  }
  stdout = new EventEmitter()
  stderr = new EventEmitter()
  exitCode: number | null = null
  killed = false
  written = ''
  constructor(
    readonly args: string[],
    readonly cwd: string,
  ) {
    super()
    this.stdout.setEncoding = () => {}
  }

  pushData(line: string): void {
    this.stdout.emit('data', line)
  }

  crash(): void {
    this.exitCode = 1
    this.emit('close')
  }

  kill(): void {
    this.killed = true
    this.exitCode = 0
    this.emit('close')
  }
}

function fakeSpawner(children: FakeChild[]): Spawner {
  return (args, cwd) => {
    const child = new FakeChild(args, cwd)
    children.push(child)
    return child
  }
}

describe('WorkerClient', () => {
  it('spawn 参数与帧格式、同 id 关联解析', async () => {
    const children: FakeChild[] = []
    const client = new WorkerClient('/srv', fakeSpawner(children))
    const pending = client.request('query', { question: 'main' })
    const child = children[0]
    expect(child.args).toEqual(['run', '--no-sync', '--project', '/srv', 'gh-graphify-worker'])
    expect(child.cwd).toBe('/srv')
    expect(JSON.parse(child.written)).toEqual({ id: 1, action: 'query', question: 'main' })
    child.pushData(JSON.stringify({ id: 1, ok: true, text: 'NODE f [src=a.py loc=L1]' }) + '\n')
    await expect(pending).resolves.toContain('NODE f')
  })

  it('子进程崩溃后同请求重启一回合', async () => {
    const children: FakeChild[] = []
    const client = new WorkerClient('/srv', fakeSpawner(children))
    const pending = client.request('query', { question: 'main' })
    children[0].crash()
    await vi.waitFor(() => expect(children).toHaveLength(2)) // 重试 spawn 在微任务内,等待其到来
    expect(JSON.parse(children[1].written)).toMatchObject({ id: 2, action: 'query' })
    children[1].pushData(JSON.stringify({ id: 2, ok: true, text: 'ok' }) + '\n')
    await expect(pending).resolves.toBe('ok')
  })

  it('连续两次中途崩溃抛 WorkerError', async () => {
    const children: FakeChild[] = []
    const client = new WorkerClient('/srv', fakeSpawner(children))
    const pending = client.request('query', { question: 'main' })
    children[0].crash()
    await vi.waitFor(() => expect(children).toHaveLength(2))
    children[1].crash()
    await expect(pending).rejects.toThrow('graphify worker unavailable')
  })

  it('ok:false 帧的错误文本作为结果返回', async () => {
    const children: FakeChild[] = []
    const client = new WorkerClient('/srv', fakeSpawner(children))
    const pending = client.request('index', { path: '/no' })
    children[0].pushData(JSON.stringify({ id: 1, ok: false, error: 'Index failed: path not found' }) + '\n')
    await expect(pending).resolves.toBe('Index failed: path not found')
  })

  it('abort 信号拒绝挂起请求', async () => {
    const children: FakeChild[] = []
    const client = new WorkerClient('/srv', fakeSpawner(children))
    const ac = new AbortController()
    const pending = client.request('query', { question: 'x' }, ac.signal)
    ac.abort()
    await expect(pending).rejects.toThrow('aborted')
  })

  it('dispose 杀子进程并拒绝所有挂起请求', async () => {
    const children: FakeChild[] = []
    const client = new WorkerClient('/srv', fakeSpawner(children))
    const pending = client.request('query', { question: 'x' })
    client.dispose()
    expect(children[0].killed).toBe(true)
    await expect(pending).rejects.toThrow('worker disposed')
    await expect(client.request('query', { question: 'x' })).rejects.toThrow('worker disposed')
  })

  it('一次请求后常驻复用同一进程', async () => {
    const children: FakeChild[] = []
    const client = new WorkerClient('/srv', fakeSpawner(children))
    const p1 = client.request('query', { question: 'a' })
    children[0].pushData(JSON.stringify({ id: 1, ok: true, text: 'a' }) + '\n')
    await p1
    const p2 = client.request('query', { question: 'b' })
    children[0].pushData(JSON.stringify({ id: 2, ok: true, text: 'b' }) + '\n')
    await p2
    expect(children).toHaveLength(1)
  })
})
