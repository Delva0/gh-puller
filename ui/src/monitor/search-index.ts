// 轨迹/对话搜索索引(仿 dsh ui-trajectory/src/client/trajectory-search-index.ts):
// 小写子串匹配,按节点 seq 索引命中;节流由调用方(Toolbar 的 input)控制。

import type { ChatNode } from './types';

export class TrajectorySearchIndex {
  private entries: Array<{ seq: number; text: string }> = [];

  setNodes(nodes: ChatNode[]): void {
    this.entries = nodes
      .filter((n) => n.kind !== 'turn-tail')
      .map((n) => ({
        seq: n.seq,
        text: [
          n.message?.content?.map((b) => {
            const b2 = b as Record<string, unknown>;
            return typeof b2.text === 'string' ? b2.text : typeof b2.thinking === 'string' ? b2.thinking : '';
          }).join(' '),
          n.contextText ?? '',
          n.name ?? '',
          n.callId ?? '',
        ].join(' ').toLowerCase(),
      }));
  }

  /** 查询 → 命中节点 seq 集;空查询 → 空集(全不命中)。 */
  query(q: string): Set<number> {
    const needle = q.trim().toLowerCase();
    if (!needle) return new Set();
    const hits = new Set<number>();
    for (const e of this.entries) {
      if (e.text.includes(needle)) hits.add(e.seq);
    }
    return hits;
  }
}
