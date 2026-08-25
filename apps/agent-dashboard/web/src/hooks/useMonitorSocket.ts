// 监控 hub 连接核心:连接/2s 退避重连/帧分发/双 id 去重/事件环形缓冲
import { useCallback, useEffect, useRef, useState } from 'react';
import { monitorWsUrl } from '@gh-puller/ui';
import type { LlmLine, MonitorFrame, SessionMeta } from '../types';

export type ConnStatus = 'connecting' | 'connected' | 'closed';

const EVT_RING = 500; // 事件环形缓冲上限(与 hub 事件环同量级,drop-oldest)

export function useMonitorSocket() {
  const [status, setStatus] = useState<ConnStatus>('connecting');
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [lines, setLines] = useState<LlmLine[]>([]);
  const [events, setEvents] = useState<Record<string, unknown>[]>([]);
  const [llmReady, setLlmReady] = useState(false);
  const [evtReady, setEvtReady] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const timerRef = useRef<number | null>(null);
  const currentRef = useRef<string | null>(null);
  const seenLlmRef = useRef<Set<number>>(new Set());
  const seenEvtRef = useRef<Set<string>>(new Set());

  const send = useCallback((obj: unknown) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }, []);

  // 选会话:清空状态并重置去重,同时订 LLM 行与原始事件(幂等:hub 无视重复帧)
  const select = useCallback((session: string) => {
    currentRef.current = session;
    seenLlmRef.current.clear();
    seenEvtRef.current.clear();
    setLines([]);
    setEvents([]);
    setLlmReady(false);
    setEvtReady(false);
    setCurrent(session);
    send({ type: 'llm-subscribe', session });
    send({ type: 'evt-subscribe', session });
  }, [send]);

  useEffect(() => {
    let disposed = false;

    const handleFrame = (frame: MonitorFrame) => {
      if (frame.type === 'index') {
        setSessions(frame.sessions);
      } else if (frame.type === 'llm') {
        if (frame.session !== currentRef.current || seenLlmRef.current.has(frame.id)) {
          return;
        }
        seenLlmRef.current.add(frame.id);
        // 回放以 session.start 起始:重连重放时整体重建,避免与 live 乱序拼接
        setLines((prev) => (
          frame.line.type === 'session.start' ? [frame.line] : prev.concat(frame.line)
        ));
      } else if (frame.type === 'evt') {
        if (frame.session !== currentRef.current) return;
        const id = String((frame.event as { id?: unknown }).id ?? '');
        if (id) {
          if (seenEvtRef.current.has(id)) return;
          seenEvtRef.current.add(id);
        }
        setEvents((prev) => {
          const next = prev.concat(frame.event);
          return next.length > EVT_RING ? next.slice(next.length - EVT_RING) : next;
        });
      } else if (frame.type === 'llm_ready') {
        setLlmReady(true);
      } else if (frame.type === 'evt_ready') {
        setEvtReady(true);
      }
    };

    const connect = () => {
      if (disposed) return;
      setStatus('connecting');
      const ws = new WebSocket(monitorWsUrl());
      wsRef.current = ws;
      ws.onopen = () => {
        if (disposed) return;
        setStatus('connected');
        send({ type: 'index' });
        if (currentRef.current) {
          send({ type: 'llm-subscribe', session: currentRef.current });
          send({ type: 'evt-subscribe', session: currentRef.current });
        }
      };
      ws.onmessage = (m) => {
        try {
          handleFrame(JSON.parse(m.data as string) as MonitorFrame);
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
    // 只跑一次:send 经 ref 取当前 ws,闭包稳定
  }, [send]);

  return { status, sessions, current, lines, events, llmReady, evtReady, select };
}
