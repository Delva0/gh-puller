import { useEffect, useState } from 'react';
import { api, ApiError } from './api';
import { availability } from './availability';
import type { Catalog, Credentials, Preferences, ValidationReport, ValidationState } from './types';

export function useValidation(catalog: Catalog | null, preferences: Preferences, credentials: Credentials,
  sessionId: string | undefined, enabled: boolean, drafts: Record<string, string>) {
  const [epoch, setEpoch] = useState(0);
  const [result, setResult] = useState<{ identity: string; report?: ValidationReport; error?: string }>();
  const payload = JSON.stringify({ agents: preferences.agents, tools: preferences.tools,
    credentials: Object.fromEntries(Object.entries(credentials).filter(([key]) => key !== 'api_key')), session_id: sessionId });
  const identity = payload + epoch;
  useEffect(() => {
    if (!enabled || !catalog) return;
    let disposed = false;
    const timer = setTimeout(async () => {
      try {
        let report: ValidationReport;
        try { report = await api<ValidationReport>('/configuration/validate', 'POST', JSON.parse(payload)); }
        catch (error) {
          if (!(error instanceof ApiError) || error.status !== 404) throw error;
          report = await api<ValidationReport>('/configuration/validate', 'POST', { ...JSON.parse(payload), session_id: undefined });
        }
        if (!disposed) setResult({ identity, report });
      } catch (error) {
        if (!disposed) setResult({ identity, error: error instanceof Error ? error.message : '操作失败，请重试' });
      }
    }, 120);
    return () => { disposed = true; clearTimeout(timer); };
  }, [payload, identity, catalog, enabled]);
  const current = result?.identity === identity;
  const report = result?.report;
  const tools = report?.tools ?? [...new Map(catalog?.agents.flatMap(agent => agent.tool_catalog).map(tool => [tool.id, tool])).values()];
  function state(value?: ValidationState, issues: Record<string, string> = {}) {
    if (current && result.error) return availability({ valid: false, issues: { validation: { valid: false, reason: result.error } } }, issues);
    return availability(current ? value : undefined, issues);
  }
  function tool(id: string) {
    const item = report?.tools.find(tool => tool.id === id);
    const errors = Object.fromEntries(Object.entries(drafts).filter(([key]) => key.startsWith('tool:') &&
      tools.find(tool => tool.id === id)?.configuration.includes(key.slice(5))).map(([key, value]) => [key.slice(5), value]));
    return state(item, errors);
  }
  function agent(id: string) {
    const capability = catalog?.agents.find(agent => agent.id === id);
    const fields = new Set((report?.agents[id]?.tools ?? capability?.tools ?? []).flatMap(tool => tool.configuration));
    const errors = Object.fromEntries(Object.entries(drafts).filter(([key]) => key.startsWith(`agent:${id}:`) ||
      key.startsWith('tool:') && fields.has(key.slice(5))).map(([key, value]) => [key.split(':').at(-1)!, value]));
    return state(report?.agents[id], errors);
  }
  const agentTools = (id: string) => report?.agents[id]?.tools ?? catalog?.agents.find(agent => agent.id === id)?.tools ?? [];
  return { tools, fields: report?.fields ?? {}, agent, tool, agentTools, refresh: () => setEpoch(value => value + 1) };
}
export type ConfigurationValidation = ReturnType<typeof useValidation>;
