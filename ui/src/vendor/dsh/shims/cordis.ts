/**
 * Cordis 最小 shim:vendor 面板不需要运行时容器(注册表已去 Cordis),
 * 仅存留各处类型/构造占位。gh-puller 宿主不提供 Cordis。
 */
export type Inject = readonly string[]

/** Service 基类占位(注册表不再继承;保留以兼容偶然类型引用)。 */
export class Service {
  constructor(..._args: unknown[]) {}
}

/** 上下文占位类型(vendor 代码只把它当接口使用;interface 以便模块增强 merge)。 */
export interface Context {
  get?<T>(key: string): T | undefined
  effect?(callback: () => void | Iterable<() => void>, label?: string): () => void
  emit?(event: string, payload?: unknown): void
  readonly fiber?: { name?: string }
}
