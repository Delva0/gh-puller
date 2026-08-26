/**
 * dsh-session-projection/types 最小基座:开放投影映射(由 shims 增强)。
 * dsh 原包为 packages/session/session-projection/src/types.ts(空映射 + 主题);
 * 注意:必须为 interface 才能被 shims 模块增强 merge。
 */
export interface SessionProjectionMap {}
export type ThemeProjection = Record<string, unknown>
