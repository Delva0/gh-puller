'use client';

// 监控 hub 连接核心(协议 v2):连接/2s 退避重连/帧分发;
// 订阅窗:subscribe → evt_ready{lastSeq} → history 尾页(loading 期间 live 帧入缓冲)→
// applyBatch 合并;loading 外 live 按 animation frame 入增量 fold;旧历史由 UI 显式翻页。
// 注:依赖 window.location(monitorWsUrl),仅限浏览器宿主使用(勿在 SSR 端导入)。
import { useCallback, useEffect, useRef, useState } from 'react';
import { monitorWsUrl } from '../utils/monitorWs';
import { mergeEvents } from '../monitor-data';
import type { EventEnvelope, HubFrame, SessionMeta } from '../monitor-data';
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
  const liveRef = useRef<EventEnvelope[]>([]);
  const rafRef = useRef<number | null>(null);
  const olderBeforeRef = useRef<number | null>(null);
  const olderLoadingRef = useRef(false);

  const send = useCallback((obj: unknown) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }, []);

  const sessionsRef = useRef<SessionMeta[]>([]);

  /** 删除会话(本端或经侧栏 ··· 菜单触发):hub 广播 index 回响推进。 */
  const remove = useCallback((session: string) => {
    send({ type: 'delete', session });
  }, [send]);

  const requestHistory = useCallback((beforeSeq?: number) => {
    send({ type: 'history', session: currentRef.current, beforeSeq, max: 200 });
  }, [send]);

  const loadOlderRegistered = useRef(false);

  const select = useCallback((session: string) => {
    currentRef.current = session;
    pendingRef.current = [];
    liveRef.current = [];
    loadingRef.current = true;
    olderBeforeRef.current = null;
    olderLoadingRef.current = false;
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

    const flushLive = () => {
      rafRef.current = null;
      const session = currentRef.current;
      const events = liveRef.current.filter((evt) => evt.session === session);
      liveRef.current = [];
      if (events.length) sessionStore.ingestBatch(events);
    };

    const queueLive = (events: EventEnvelope[]) => {
      const current = currentRef.current;
      const selected = events.filter((evt) => evt.session === current);
      if (!selected.length) return;
      if (loadingRef.current) {
        pendingRef.current.push(...selected);
        return;
      }
      liveRef.current.push(...selected);
      if (rafRef.current === null) rafRef.current = window.requestAnimationFrame(flushLive);
    };

    const handleFrame = (frame: HubFrame) => {
      if (frame.type === 'index') {
        sessionsRef.current = frame.sessions;
        setSessions(frame.sessions);
        // 当前项被删(本端或别的查看端触发)→ 复原视图(与 select 的空态同路径)
        if (currentRef.current !== null
            && !frame.sessions.some((s) => s.session === currentRef.current)) {
          currentRef.current = null;
          pendingRef.current = [];
          liveRef.current = [];
          setCurrent(null);
          sessionStore.reset(null);
        }
      } else if (frame.type === 'evt_ready') {
        if (frame.session !== currentRef.current) return;
        sessionStore.ready(frame.lastSeq);
        requestHistory();
      } else if (frame.type === 'history') {
        if (frame.session !== currentRef.current) return;
        const initial = loadingRef.current;
        const events = initial ? mergeEvents(frame.events, pendingRef.current) : frame.events;
        if (initial) {
          pendingRef.current = [];
          loadingRef.current = false;
        }
        olderBeforeRef.current = frame.nextBeforeSeq;
        olderLoadingRef.current = false;
        sessionStore.applyBatch(events, frame.hasMore);
        sessionStore.getBridge().setLoadingOlder(false);
        if (!loadOlderRegistered.current) {
          loadOlderRegistered.current = true;
          sessionStore.getBridge().setLoadOlder(async () => {
            const before = olderBeforeRef.current;
            if (before === null || olderLoadingRef.current) return false;
            olderLoadingRef.current = true;
            sessionStore.getBridge().setLoadingOlder(true);
            requestHistory(before);
            return true;
          });
        }
      } else if (frame.type === 'evt') {
        queueLive([frame.event]);
      } else if (frame.type === 'evts') {
        queueLive(frame.events);
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
      if (rafRef.current !== null) window.cancelAnimationFrame(rafRef.current);
      wsRef.current?.close();
    };
    // 只跑一次:send/select 经 ref 取当前 ws,闭包稳定
  }, [send, select, requestHistory]);

  return { status, sessions, current, select, remove };
}
