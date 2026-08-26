/** dsh-client-ui-input-trigger/client 类型 shim(形状照 dsh 源 types.ts)。 */
export interface TokenSpan {
  readonly start: number
  readonly end: number
  readonly draftRev: number
}

export interface SubmitImageAttachment {
  readonly mediaType: 'image/png' | 'image/jpeg' | 'image/webp' | 'image/gif'
  readonly data: string
  readonly name?: string
}

export interface CommandClaim {
  readonly token: string
  readonly hint?: string
  readonly images?: boolean
  submit(args: string, actx: unknown, images: readonly SubmitImageAttachment[]): Promise<SubmitOutcome>
}

export interface ReferenceInsert {
  readonly source: string
  readonly ref: string
  readonly label: string
  readonly appearance?: 'session' | 'file' | 'folder'
  readonly clipboardText: string
}

export interface SubmitOutcome {
  readonly kind: 'success' | 'error'
  readonly text?: string
}

export type PickOutcome =
  | { readonly claim: CommandClaim }
  | { readonly insert: ReferenceInsert }
  | { readonly text: string; readonly continue?: boolean }
  | 'handled'
  | undefined

export type ArbitrateKey = 'up' | 'down' | 'enter' | 'escape'
export type ArbitrateOutcome = 'consumed' | 'pick-highlighted' | 'pass'

export interface ConsumeTokenRequest {
  readonly guard:
    | { readonly kind: 'span'; readonly span: TokenSpan }
    | { readonly kind: 'bare-token'; readonly token: string }
}

declare module '@dsh/ui-slots' {
  interface SlotMap {
    /** 引发表行由 ui-input-trigger 声明(owner 是 ui-conversation composer 条目)。 */
    'conversation.input.overlay': { kind: 'list'; scope: 'session' }
  }
}
