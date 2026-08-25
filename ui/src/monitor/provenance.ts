// 上下文注入来源的投影(仿 dsh runtime/sessions/context-provenance.ts):
// user/message 的 source 决定"这是一条用户消息还是一次注入",以及标注的形态。

import type { SourceInfo } from './types';

export interface Provenance {
  kind: 'user' | 'context';
  form: 'instructions' | 'notice' | 'snapshot' | 'catalog';
  label?: string; // 注入来源标签(组件经 useLanguage t() 渲染)
}

export function contextProvenance(source?: SourceInfo): Provenance {
  if (!source || source.kind === 'user') {
    return { kind: 'user', form: 'instructions' };
  }
  const form = (['instructions', 'notice', 'snapshot', 'catalog'] as const).includes(
    source.form as never,
  )
    ? (source.form as 'instructions' | 'notice' | 'snapshot' | 'catalog')
    : 'notice';
  return { kind: 'context', form, label: source.label ?? source.form ?? 'notice' };
}

/** 注入文案 vs 整段注入(表单/快照类)的形态差异(对话视图行样式用)。 */
export function contextForm(source?: SourceInfo): 'notice' | 'block' {
  const p = contextProvenance(source);
  return p.form === 'notice' ? 'notice' : 'block';
}
