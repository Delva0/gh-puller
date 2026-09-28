import { settingsFor, type Capability, type Catalog, type ConfigValues, type Preferences, type ToolConfig } from './types';

export interface Availability { available: boolean; missing: string[]; reason?: string }

export function toolAvailability(tool: ToolConfig, values: ConfigValues, configured: Set<string>): Availability {
  const missing = Object.entries(tool.credential_requirements).filter(([key, when]) =>
    Object.entries(when).every(([name, value]) => JSON.stringify(values[name]) === JSON.stringify(value)) && !configured.has(key),
  ).map(([key]) => key);
  return { available: !missing.length, missing };
}

export function agentAvailability(agent: Capability, preferences: Preferences, catalog: Catalog, configured: Set<string>): Availability {
  const values = settingsFor(agent.id, preferences, catalog).options;
  const missing = [...new Set(agent.tools.flatMap(tool => toolAvailability(tool, values, configured).missing))];
  return { available: agent.available && !missing.length, missing, reason: agent.reason };
}
