/** dsh-token-meter/client 类型 shim(投影形状照 dsh 源)。 */
export interface TokenUsageProjection {
  uncachedInputTokens: number
  outputTokens: number
  cacheReadTokens: number
  cacheWriteTokens: number
}

export interface ContextPressureProjection {
  pressureTokens?: number
  projectedTokens?: number
  contextWindow?: number
}

export interface ContextBreakdownProjection {
  systemTokens: number
  toolsTokens: number
  messageTokens: number
}

declare module '@dsh/session-projection' {
  interface SessionProjectionMap {
    tokenUsage: TokenUsageProjection
    contextPressure: ContextPressureProjection
    contextBreakdown: ContextBreakdownProjection
  }
}
