/**
 * DshConversationPanel:对话+轨迹两面板的宿主出口(agent-dashboard 专用)。
 *
 * 外壳角色 = dsh ConversationRoot 的去 hero/composer 版:根条目('root')组件
 * 持有滚动容器(scrollBody 复用 dsh ConversationRoot.module.css),经 standardKit
 * 提供的 renderSlot 渲出会话头与会话体。dsh 的 composer/hero/侧栏/详情列均
 * 不在本宿主渲染;语言切换由宿主经 locale prop 驱动(isDshLocale)。
 */
import { createElement, useLayoutEffect, useMemo, type ReactNode } from 'react'
import { installDsh, type DshInstall } from './install'
import { sessionStore } from '../../../hooks/useMonitorSession'
import { createSlotRenderer } from '../ui-renderer/src/client/scoped-slots.tsx'
import css from '../ui-conversation/src/client/skeleton/ConversationRoot.module.css'

export interface DshConversationPanelProps {
  /** 宿主语言(识别 en,其余按 zh 缺省)。 */
  locale?: string | null | undefined
}

interface ShellProps {
  useSessions: <S>(sel: (s: unknown) => S) => S
  renderSlot: (key: string, owner: object, opts?: { only?: string }) => ReactNode
}

/** 根条目壳:dsh ConversationRoot 的会话体部分(无 hero/composer/workspace 选择)。 */
function DshShell({ useSessions, renderSlot }: ShellProps) {
  const sessionList = useSessions((s: unknown) => s) as { byId: Record<string, unknown> }
  const sessionId = Object.keys(sessionList.byId)[0]
  return (
    <div className={css.root}>
      <div className={css.scrollBody} data-conversation-scroll="">
        {sessionId !== undefined && renderSlot('conversation.session.header', {})}
        {renderSlot('conversation.session', {})}
      </div>
    </div>
  )
}

let pendingInstall: DshInstall | null = null

export default function DshConversationPanel({ locale }: DshConversationPanelProps) {
  const store = useMemo(() => sessionStore.getBridge(), [])
  const install = useMemo(() => {
    if (pendingInstall === null) {
      pendingInstall = installDsh(store, DshShell as never)
    }
    return pendingInstall
  }, [store])

  // 语言切换 → dsh renderer locale revision
  useLayoutEffect(() => {
    install.setLocale(locale ?? null)
  }, [locale, install])

  const node = useMemo(() => {
    const registry = install.registry as unknown as { getRendererHost(): unknown }
    const host = registry.getRendererHost()
    const renderer = createSlotRenderer()
    return renderer.renderRoot(host as never, {})
  }, [install])

  return createElement('div', { className: 'ghp-dsh-host', style: { display: 'contents' } }, node)
}
