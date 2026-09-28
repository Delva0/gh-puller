import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api';

function cached(url: string): string[] {
  try { return JSON.parse(localStorage.getItem('trace-models') ?? '{}')[url] ?? []; } catch { return []; }
}
export function useModels(baseUrl: string, key: string, sessionId?: string, enabled = true) {
  const [models, setModels] = useState<string[]>([]);
  const [status, setStatus] = useState<'idle' | 'loading' | 'success' | 'error'>('idle');
  const [error, setError] = useState('');
  const generation = useRef(0);
  const refresh = useCallback(async () => {
    const attempt = ++generation.current;
    if (!enabled || !key && !sessionId) { setStatus('idle'); return; }
    setStatus('loading'); setError('');
    try {
      const result = await api<{ models: string[] }>('/models', 'POST', { base_url: baseUrl, api_key: key, session_id: sessionId });
      if (attempt !== generation.current) return;
      setModels(result.models); setStatus('success');
      localStorage.setItem('trace-models', JSON.stringify({ [baseUrl]: result.models }));
    } catch (cause) {
      if (attempt !== generation.current) return;
      setStatus('error'); setError(cause instanceof Error ? cause.message : 'Model discovery failed');
    }
  }, [baseUrl, key, sessionId, enabled]);
  useEffect(() => {
    generation.current++;
    setModels(cached(baseUrl)); setStatus('idle'); setError('');
    const timer = setTimeout(() => void refresh(), 650);
    return () => { clearTimeout(timer); generation.current++; };
  }, [baseUrl, refresh]);
  return { models, status, error, refresh };
}
export type ModelDiscovery = ReturnType<typeof useModels>;
