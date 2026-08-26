'use client';

// 监控 hub 连接核心(协议 v2):连接/2s 退避重连/帧分发;
// 订阅窗:subscribe → evt_ready{lastSeq} → history 尾页(loading 期间 live 帧入缓冲)→
// applyBatch 合并;loading 外 live 经 seq 守卫入 fold,间隙(早/漏帧)→ history(beforeSeq) 补片。
// 注:依赖 window.location(monitorWsUrl),仅限浏览器宿主使用(勿在 SSR 端导入)。
import { useCallback, useEffect, useRef, useState } from 'react';
import { monitorWsUrl } from '../utils/monitorWs';
import { mergeEvents } from '../monitor';
import type { EventEnvelope, HubFrame, SessionMeta } from '../monitor';
import { sessionStore } from './useMonitorSession';

export type ConnStatus = 'connecting' | 'connected' | 'closed';

export function useMonitorSocket() {
  const [status, setStatus] = useState<ConnStatus>('connecting');
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [current, setCurrent] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const timerRef = useRef<number | null>(null);
  const currentRef = useRef<string | null>(null);
  const loadingRef = useRef(false); // history 尾页加载中:live 帧入缓冲
  const pendingRef = useRef<EventEnvelope[]>([]);

  const send = useCallback((obj: unknown) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }, []);

  const sessionsRef = useRef<SessionMeta[]>([]);

  const requestHistory = useCallback((beforeSeq?: number) => {
    send({ type: 'history', session: currentRef.current, beforeSeq, max: 200 });
  }, [send]);

  const loadOlderRegistered = useRef(false);

  const select = useCallback((session: string) => {
    currentRef.current = session;
    pendingRef.current = [];
    loadingRef.current = true;
    // 清空旧会话折叠与快照(事实来自会话列表行)
    const row = sessionsRef.current.find((s) => s.session === session);
    sessionStore.reset({
      sessionId: session,
      label: row?.label ?? session,
      runId: row?.run_id ?? null,
      provider: row?.provider ?? '',
      model: row?.model ?? '',
      generator: row?.generator ?? '',
      state: (row?.state === 'completed' || row?.state === 'aborted')
        ? row.state
        : 'running',
    });
    setCurrent(session);
    send({ type: 'subscribe', session });
    // 尾页请求在 subscribe 应答(evt_ready)后发出:先登记订阅,再拉历史,无缝衔接
  }, [send]);

  useEffect(() => {
    let disposed = false;

    const handleFrame = (frame: HubFrame) => {
      if (frame.type === 'index') {
        sessionsRef.current = frame.sessions;
        setSessions(frame.sessions);
      } else if (frame.type === 'evt_ready') {
        sessionStore.ready(frame.lastSeq);
        requestHistory();
      } else if (frame.type === 'history') {
        const events = mergeEvents(frame.events, pendingRef.current);
        pendingRef.current = [];
        loadingRef.current = false;
        sessionStore.applyBatch(events);
        sessionStore.getBridge().setHasMore(frame.hasMore);
        if (!loadOlderRegistered.current) {
          loadOlderRegistered.current = true;
          sessionStore.getBridge().setLoadOlder(async () => {
            // 旧页翻页:以最新尾部空白为止,返回是否有变化(简化:翻到哪算哪)
            const before = sessionStore.snapshot().lastSeq;
            requestHistory(before ?? undefined);
            return true;
          });
        }
        // 余量预告:漏页继续翻(单页 200 上限,超长会话翻旧)
        if (frame.hasMore && frame.nextBeforeSeq !== null) {
          requestHistory(frame.nextBeforeSeq);
        }
      } else if (frame.type === 'evt') {
        if (loadingRef.current) {
          pendingRef.current.push(frame.event);
          return;
        }
        const r = sessionStore.ingest(frame.event);
        if (r === 'gap') {
          const from = sessionStore.snapshot().gapFrom;
          if (from !== null) requestHistory(from);
        }
      }
    };

    const connect = () => {
      if (disposed) return;
      loadOlderRegistered.current = false;
      setStatus('connecting');
      const ws = new WebSocket(monitorWsUrl());
      wsRef.current = ws;
      ws.onopen = () => {
        if (disposed) return;
        setStatus('connected');
        send({ type: 'index' });
        if (currentRef.current) {
          select(currentRef.current);
        }
      };
      ws.onmessage = (m) => {
        try {
          handleFrame(JSON.parse(m.data as string) as HubFrame);
        } catch {
          /* 非 JSON 帧忽略 */
        }
      };
      ws.onclose = () => {
        if (disposed) return;
        setStatus('closed');
        timerRef.current = window.setTimeout(connect, 2000);
      };
    };

    connect();
    return () => {
      disposed = true;
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      wsRef.current?.close();
    };
    // 只跑一次:send/select 经 ref 取当前 ws,闭包稳定
  }, [send, select, requestHistory]);

  return { status, sessions, current, select };
}
