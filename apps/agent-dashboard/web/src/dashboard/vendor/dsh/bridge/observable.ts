/**
 * 最小 HostObservable 实现(桥接层工具):快照源 + 订阅。
 */
export interface HostObservable<T> {
  getSnapshot(): T
  subscribe(fn: () => void): () => void
}

export function createObservable<T>(initial: T): HostObservable<T> & { set(next: T): void } {
  let value = initial
  const listeners = new Set<() => void>()
  return {
    getSnapshot: () => value,
    subscribe(fn) {
      listeners.add(fn)
      return () => { listeners.delete(fn) }
    },
    set(next) {
      if (Object.is(next, value)) return
      value = next
      for (const fn of [...listeners]) fn()
    },
  }
}

/** 版本承载的可观测(递变计数驱动订阅者)。 */
export function createVersioned<T>(initial: T): HostObservable<T> & { set(next: T): void; version(): number } {
  const source = createObservable(initial)
  let version = 0
  return {
    ...source,
    set(next) {
      if (Object.is(next, source.getSnapshot())) return
      version += 1
      source.set(next)
    },
    version: () => version,
  }
}
