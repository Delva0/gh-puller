import { describe, expect, it } from 'vitest'

import * as plugin from '../src/index.js'
import type { Context, ToolDefinition } from '../src/index.js'
import { WorkerClient } from '../src/worker.js'

/** 最小假 ctx:只承载 tools.register 与 effect。 */
function fakeCtx(): {
  ctx: Context
  defs: ToolDefinition[]
  disposers: Array<() => void>
} {
  const defs: ToolDefinition[] = []
  const disposers: Array<() => void> = []
  return {
    defs,
    disposers,
    ctx: {
      tools: {
        register: (def) => {
          defs.push(def)
          return () => {}
        },
      },
      effect: (cb) => {
        const d = cb()
        if (typeof d === 'function') disposers.push(d as () => void)
        return undefined
      },
    },
  }
}

describe('插件契约', () => {
  it('导出 name / inject / apply', () => {
    expect(plugin.name).toBe('graphify')
    expect(plugin.inject).toEqual(['tools'])
    expect(typeof plugin.apply).toBe('function')
  })

  it('apply 注册 graphify_query / graphify_index 两个工具', () => {
    const { ctx, defs, disposers } = fakeCtx()
    plugin.apply(ctx as never, { serverDir: '/x' })
    expect(defs.map((d) => d.name)).toEqual(['graphify_query', 'graphify_index'])
    for (const d of disposers) d()
  })

  it('参数契约:query 的 question 必填、repo_type 枚举;输出为 string + 文本块', () => {
    const { ctx, defs, disposers } = fakeCtx()
    plugin.apply(ctx as never, { serverDir: '/x' })
    const query = defs.find((d) => d.name === 'graphify_query')!
    expect((query.parameters as Record<string, { required?: true }>).question.required).toBe(true)
    expect((query.parameters as Record<string, unknown>).repo_type).toMatchObject({
      enum: ['github', 'gitlab', 'bitbucket', 'local'],
    })
    const rendered = (query.output.render as (a: unknown, v: string) => unknown[])({}, 'hello')
    expect(rendered).toEqual([{ type: 'text', text: 'hello' }])
    for (const d of disposers) d()
  })

  it('execute 全链路:注入假 spawner,帧走协议、返回 worker 文本', async () => {
    const { EventEmitter } = await import('node:events')
    const calls: string[] = []
    const child = new EventEmitter()
    child.stdin = { write: (s: string) => (calls.push(s), true), end: () => {} }
    child.stdout = new EventEmitter()
    child.stdout.setEncoding = () => {}
    child.stderr = new EventEmitter()
    child.exitCode = null
    child.killed = false
    child.kill = () => {}
    const spawner = () => child

    const worker = new WorkerClient('/x', spawner)
    const defs = plugin.buildTools(worker)
    const exec = { signal: new AbortController().signal }
    const pending = defs[0].execute({ question: 'main' }, exec)
    expect(JSON.parse(calls[0])).toMatchObject({ action: 'query', question: 'main' })
    child.stdout.emit('data', JSON.stringify({ id: 1, ok: true, text: 'NODE main [src=a.py loc=L1]' }) + '\n')
    await expect(pending).resolves.toContain('NODE main')
  })
})
