// LLM 流行渲染:round 分组;thinking 折叠;content → Markdown;tool 块 + 结果;终态汇总条。
// 纯函数式:块由 start/delta/end 行线性推导,增量文本按块拼接(无 DOM ref,回放/去重天然一致)。
import { Markdown, useLanguage } from '@gh-puller/ui';
import type { ReactNode } from 'react';
import type { LlmLine } from '../types';

interface Props {
  lines: LlmLine[];
  expandThinking: boolean;
}

interface OpenedBlock {
  key: string;
  blockType: string;
  toolName?: string | null;
  toolId?: string | null;
  parts: string[];
  toolInput?: unknown;
}

// 内容预览截断(老 viewer 同款 300 字符)
const preview = (text: string) => (text.length > 300 ? text.slice(0, 300) + '…' : text);

export default function StreamView({ lines, expandThinking }: Props) {
  const { t } = useLanguage();

  const nodes: ReactNode[] = [];
  let opened: OpenedBlock | null = null;

  const closeBlock = () => {
    if (!opened) return;
    const b = opened;
    opened = null;
    const text = b.parts.join('');
    if (b.blockType === 'thinking') {
      nodes.push(
        <details key={b.key} open={expandThinking} className="my-1 rounded-md bg-amber-500/10">
          <summary className="cursor-pointer px-2 py-1 text-xs text-amber-600 dark:text-amber-400">💭 {t('stream.thinking')}</summary>
          <pre className="whitespace-pre-wrap px-3 pb-2 font-mono text-xs text-zinc-700 dark:text-zinc-300">{text}</pre>
        </details>,
      );
    } else if (b.blockType === 'tool_use') {
      const input = b.toolInput === null || b.toolInput === undefined ? '' : JSON.stringify(b.toolInput, null, 2);
      nodes.push(
        <div key={b.key} className="my-1 rounded-md border-l-2 border-sky-500 bg-sky-500/10 px-3 py-2">
          <div className="text-xs font-mono text-sky-600 dark:text-sky-400">
            ⚙ {b.toolName || b.toolId || b.blockType}
          </div>
          {input && <pre className="mt-1 whitespace-pre-wrap font-mono text-xs text-zinc-600 dark:text-zinc-400">{input}</pre>}
        </div>,
      );
    } else {
      nodes.push(<div key={b.key} className="my-1"><Markdown content={text} /></div>);
    }
  };

  const renderLine = (line: LlmLine, i: number) => {
    switch (line.type) {
      case 'session.start': // 元信息在顶部统计条,不复绘
        return;
      case 'round.start':
        if (opened) closeBlock();
        nodes.push(
          <div key={`r${i}`} className="mt-4 border-t border-dashed border-zinc-300 pt-2 text-xs text-zinc-500 dark:border-zinc-700 dark:text-zinc-500">
            {t('stream.round', { n: line.round })}
            {line.input_preview && <span className="ml-2 text-zinc-400">「{line.input_preview}」</span>}
          </div>,
        );
        return;
      case 'block.start':
        if (opened) closeBlock();
        opened = { key: `b${line.round}:${line.seq}`, blockType: line.block_type, toolName: line.tool_name, toolId: line.tool_id, parts: [] };
        return;
      case 'block.delta':
        if (opened && opened.key === `b${line.round}:${line.seq}`) opened.parts.push(line.text);
        return;
      case 'block.end':
        if (opened) {
          opened.toolInput = line.tool_input ?? opened.toolInput;
          closeBlock();
        }
        return;
      case 'tool.result':
        if (opened) closeBlock();
        nodes.push(
          <div key={`tr${i}`} className={`my-1 rounded-md border px-3 py-2 text-xs ${line.is_error
            ? 'border-red-500/40 bg-red-500/10 text-red-500'
            : 'border-zinc-200 bg-zinc-50 text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-400'
          }`}>
            {line.is_error ? '✗' : '✓'} {line.tool_name || t('stream.toolInput')}
            {line.is_error && line.content_preview && <span className="ml-2">{preview(line.content_preview)}</span>}
            {!line.is_error && line.content_preview && <span className="ml-2 font-mono">{preview(line.content_preview)}</span>}
          </div>,
        );
        return;
      case 'round.end':
        if (opened) closeBlock();
        nodes.push(
          <div key={`re${i}`} className="mt-2 text-[11px] text-zinc-500 dark:text-zinc-600">
            {t('stream.roundEnded', { n: line.round })}
          </div>,
        );
        return;
      case 'session.end': {
        if (opened) closeBlock();
        const aborted = line.state === 'aborted';
        const stateLabel = t(`session.state.${line.state}`) || String(line.state);
        const secs = line.duration_ms != null && line.duration_ms < 1000
          ? `${line.duration_ms}ms`
          : line.duration_ms != null ? `${Math.round(line.duration_ms / 1000)}s` : '';
        nodes.push(
          <div key={`se${i}`} className={`my-2 rounded-md border-l-2 px-3 py-2 text-xs font-mono ${
            aborted
              ? 'border-red-500 bg-red-500/10 text-red-500'
              : 'border-emerald-500 bg-emerald-500/10 text-emerald-500'
          }`}>
            {aborted ? t('stream.sessionAborted', { reason: line.reason || stateLabel })
                     : t('stream.sessionEnded', { state: stateLabel, n: line.num_rounds ?? 0 })}
            {secs && <span className="ml-2">{secs}</span>}
          </div>,
        );
        return;
      }
    }
  };

  lines.forEach(renderLine);
  if (opened) closeBlock();

  return <div>{nodes}</div>;
}
