// 会话状态徽章:running(绿点闪烁)/completed(灰)/aborted(红);配色由消费方 Tailwind 约定
interface StateBadgeProps {
  state: string;
  label?: string;
}

const CLASS: Record<string, string> = {
  running: 'bg-emerald-500/15 text-emerald-500',
  completed: 'bg-[var(--muted)]/15 text-[var(--muted)]',
  aborted: 'bg-red-500/15 text-red-500',
};

export default function StateBadge({ state, label }: StateBadgeProps) {
  const cls = CLASS[state] ?? CLASS.completed;
  return (
    <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-mono ${cls}`}>
      {state === 'running' && (
        <span className="dot-blink inline-block size-1.5 rounded-full bg-emerald-500" />
      )}
      {label ?? state}
    </span>
  );
}
