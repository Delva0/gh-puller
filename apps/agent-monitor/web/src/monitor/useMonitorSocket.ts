'use client';

/** Connect the viewer, load complete compact histories, and feed the selected fold. */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { EventEnvelope } from '../events/types';
import { mergeEvents } from './protocol';
import type { HubFrame, SessionMeta } from './protocol';
import { sessionStore } from './useMonitorSession';

export type ConnStatus = 'connecting' | 'connected' | 'closed';

function monitorWsUrl(): string {
  const explicit = (import.meta as { env?: Record<string, string | undefined> })
    .env?.VITE_MONITOR_WS_URL;
  if (explicit) return explicit.replace(/\/+$/, '').replace(/^http/, 'ws');
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
}

export function useMonitorSocket() {
  const [status, setStatus] = useState<ConnStatus>('connecting');
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [current, setCurrent] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const timerRef = useRef<number | null>(null);
  const currentRef = useRef<string | null>(null);
  const loadingRef = useRef(false);
  const historyRef = useRef<EventEnvelope[]>([]);
  const pendingRef = useRef<EventEnvelope[]>([]);
  const liveRef = useRef<EventEnvelope[]>([]);
  const rafRef = useRef<number | null>(null);

  const send = useCallback((frame: unknown) => {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frame));
  }, []);

  const remove = useCallback((session: string) => {
    send({ type: 'delete', session });
  }, [send]);

  const requestHistory = useCallback((beforeSeq?: number) => {
    send({ type: 'history', session: currentRef.current, beforeSeq, max: 1000 });
  }, [send]);

  const select = useCallback((session: string) => {
    currentRef.current = session;
    loadingRef.current = true;
    historyRef.current = [];
    pendingRef.current = [];
    liveRef.current = [];
    sessionStore.reset();
    setCurrent(session);
    send({ type: 'subscribe', session });
  }, [send]);

  useEffect(() => {
    let disposed = false;

    const flushLive = () => {
      rafRef.current = null;
      const session = currentRef.current;
      const events = liveRef.current.filter(evt => evt.session === session);
      liveRef.current = [];
      if (events.length > 0 && sessionStore.ingestBatch(events) === 'gap' && session !== null) {
        select(session);
      }
    };

    const queueLive = (events: EventEnvelope[]) => {
      const selected = events.filter(evt => evt.session === currentRef.current);
      if (selected.length === 0) return;
      if (loadingRef.current) {
        pendingRef.current.push(...selected);
        return;
      }
      liveRef.current.push(...selected);
      if (rafRef.current === null) rafRef.current = window.requestAnimationFrame(flushLive);
    };

    const handleFrame = (frame: HubFrame) => {
      if (frame.type === 'index') {
        setSessions(frame.sessions);
        if (currentRef.current !== null
            && !frame.sessions.some(session => session.session === currentRef.current)) {
          currentRef.current = null;
          loadingRef.current = false;
          historyRef.current = [];
          pendingRef.current = [];
          liveRef.current = [];
          setCurrent(null);
          sessionStore.reset();
        }
        return;
      }
      if (frame.type === 'evt_ready') {
        if (frame.session !== currentRef.current) return;
        sessionStore.ready(frame.lastSeq);
        requestHistory();
        return;
      }
      if (frame.type === 'history') {
        if (frame.session !== currentRef.current || !loadingRef.current) return;
        historyRef.current = mergeEvents(historyRef.current, frame.events);
        if (frame.hasMore && frame.nextBeforeSeq !== null) {
          requestHistory(frame.nextBeforeSeq);
          return;
        }
        const events = mergeEvents(historyRef.current, pendingRef.current);
        historyRef.current = [];
        pendingRef.current = [];
        loadingRef.current = false;
        sessionStore.applyBatch(events);
        return;
      }
      if (frame.type === 'evt') queueLive([frame.event]);
      else if (frame.type === 'evts') queueLive(frame.events);
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
        if (currentRef.current !== null) select(currentRef.current);
      };
      ws.onmessage = (message) => {
        try {
          handleFrame(JSON.parse(message.data as string) as HubFrame);
        } catch {
          // Ignore malformed frames without disturbing the current fold.
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
  }, [requestHistory, select, send]);

  return { status, sessions, current, select, remove };
}
