'use client';

// 会话页主面板:头部(标题/状态芯片区)+ 对话/轨迹 双 Tab(仿 dsh ConversationSession 的
// tab 结构;gh-puller 的 host 只渲染主面板,侧栏与状态条由宿主 App 组装)

import type { ReactNode } from 'react';
import { useLanguage } from '../contexts/LanguageContext';

export type MonitorView = 'chat' | 'trajectory';

interface Props {
  view: MonitorView;
  onView: (v: MonitorView) => void;
  children?: ReactNode;
}

const TABS: Array<{ id: MonitorView; key: string }> = [
  { id: 'chat', key: 'view.chat' },
  { id: 'trajectory', key: 'view.trajectory' },
];

export default function MonitorConversation({ view, onView, children }: Props) {
  const { t } = useLanguage();
  return (
    <div className="flex h-full min-w-0 flex-1 flex-col">
      <div className="flex items-center gap-3 border-b border-[var(--border-color)] px-4 py-1.5">
        <div className="flex overflow-hidden rounded-md border border-[var(--border-color)] text-xs">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              onClick={() => onView(tab.id)}
              className={`px-3 py-1 transition-colors ${
                view === tab.id
                  ? 'bg-[var(--accent-primary)]/15 text-[var(--accent-primary)]'
                  : 'text-[var(--muted)] hover:text-[var(--foreground)]'
              }`}
            >
              {t(tab.key)}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  );
}
