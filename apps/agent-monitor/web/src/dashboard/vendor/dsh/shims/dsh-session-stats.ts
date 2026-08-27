/** dsh-session-stats/client 类型 shim:整会话统计投影。 */
export interface SessionStatsProjection {
  turns: number
  steps: number
  llmMs: number
  toolMs: number
  ttftMs: number
  ttftSteps: number
  decodeMs: number
  decodeTokens: number
}

declare module '@dsh/session-projection' {
  interface SessionProjectionMap {
    sessionStats: SessionStatsProjection
  }
}
