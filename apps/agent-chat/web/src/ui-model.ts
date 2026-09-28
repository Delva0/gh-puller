import type { Catalog, ConfigField, ConfigValues, Preferences } from './types';
import { translator, type Language } from './i18n';
import type { Availability } from './availability';

// Presentation policy belongs here; availability and actual defaults come from the package catalog.
const fields: Record<string, { label: [string, string]; description?: [string, string]; min?: number; max?: number; step?: number }> = {
  concurrency: { label: ['工具并发', 'Tool concurrency'], min: 1, max: 64,
    description: ['此 Agent 向各工具提供的并发预算。', 'This agent’s concurrency budget for its tool providers.'] },
  backend: { label: ['查询后端', 'Query backend'] },
  ptc: { label: ['工具编排（PTC）', 'Tool orchestration (PTC)'] },
  web_search_backend: { label: ['网页搜索后端', 'Web search backend'] },
  web_search_concurrency: { label: ['搜索并发', 'Search concurrency'], min: 1, max: 16 },
  web_search_interval: { label: ['搜索间隔（秒）', 'Search interval (seconds)'], min: 0, max: 300, step: 0.1,
    description: ['相邻搜索请求之间的最小等待时间。', 'Minimum time between search requests.'] },
  tool_result_num_user_query: { label: ['结果保留提问数', 'Result retention (questions)'], min: 1, max: 1000 },
  tool_result_num_tool_query: { label: ['结果保留调用数', 'Result retention (calls)'], min: 1, max: 10000 },
  tool_result_preview_lines: { label: ['结果预览行数', 'Result preview lines'], min: 1, max: 1000 },
  tool_result_preview_chars: { label: ['结果预览字符数', 'Result preview characters'], min: 1, max: 100000 },
};
const labels: Record<string, [string, string]> = {
  github: ['GitHub', 'GitHub'], gitcode: ['GitCode', 'GitCode'], web: ['网页', 'Web'], tool_results: ['工具结果', 'Tool results'],
  github_token: ['GitHub API Key', 'GitHub API key'], gitcode_token: ['GitCode API Key', 'GitCode API key'],
  brave_api_key: ['Brave API Key', 'Brave API key'],
};
const tools: Record<string, [string, string]> = {
  web_search: ['搜索网页', 'Search the web'], web_fetch: ['读取网页', 'Fetch web content'],
  github: ['查询 GitHub', 'Query GitHub'], github_graphql: ['查询 GitHub GraphQL', 'Query GitHub GraphQL'],
  gitcode: ['查询 GitCode', 'Query GitCode'], run_code: ['执行工具编排', 'Run tool orchestration'],
  get_tool_result: ['读取工具结果', 'Retrieve tool results'], early_answer: ['发布阶段回答', 'Share an interim answer'],
  bash: ['执行命令', 'Run a command'], Bash: ['执行命令', 'Run a command'],
  task_output: ['读取任务输出', 'Read task output'], task_stop: ['停止任务', 'Stop a task'],
  codebase: ['检索代码库', 'Search the codebase'],
};
const readable = (key: string) => key.split('_').map(word => word.length <= 3 ? word.toUpperCase() : word[0].toUpperCase() + word.slice(1)).join(' ');
export const uiLabel = (key: string, language: Language) => (fields[key]?.label ?? labels[key])?.[language === 'zh' ? 0 : 1] ?? readable(key);
export const fieldDescription = (field: ConfigField, language: Language) => fields[field.key]?.description?.[language === 'zh' ? 0 : 1] ?? field.description;
export const toolLabel = (key: string, language: Language) => tools[key]?.[language === 'zh' ? 0 : 1] ?? readable(key);
export function availabilityHint(status: Availability, language: Language) {
  const t = translator(language);
  return Object.entries(status.issues).map(([key, issue]) => `${uiLabel(key, language)}: ${t(issue.reason)}`).join(' · ')
    || t(status.pending ? '正在校验配置' : status.available ? '可用' : '不可用');
}
export const fieldValue = (field: ConfigField, value: ConfigValues[string]) => value ?? field.effective_default ?? field.default;
export function numericPolicy(field: ConfigField) {
  const rule = fields[field.key];
  return { min: rule?.min ?? 0, max: rule?.max ?? 1000000, step: rule?.step ?? (field.type === 'integer' ? 1 : 'any') };
}
export function credentialTestHint(key: string, language: Language) {
  return key === 'brave_api_key' ? (language === 'zh' ? '测试连接（会使用一次搜索请求）' : 'Test connection (uses one search request)') : '';
}
export function validPreferences(value: Preferences, catalog: Catalog) {
  for (const agent of catalog.agents) for (const field of agent.fields) {
    const saved = field.tool ? value.tools : value.agents[agent.id];
    const item = saved?.[field.key];
    if (item === undefined || item === null && field.nullable) continue;
    if (field.choices.length) {
      if (!field.choices.some(choice => JSON.stringify(choice.value) === JSON.stringify(item) && !choice.reason)) return false;
      continue;
    }
    if (field.type === 'number' || field.type === 'integer') {
      const policy = numericPolicy(field);
      if (typeof item !== 'number' || item < policy.min || item > policy.max || field.type === 'integer' && !Number.isInteger(item)) return false;
    } else if (['string', 'boolean'].includes(field.type) && typeof item !== field.type) return false;
  }
  return true;
}
export function functionDescription(name: string, value: unknown): string {
  if (typeof value === 'string') { try { value = JSON.parse(value); } catch { return `${name}(${value})`; } }
  function literal(item: unknown): string {
    if (item === null) return 'None';
    if (typeof item === 'boolean') return item ? 'True' : 'False';
    if (Array.isArray(item)) return `[${item.map(literal).join(', ')}]`;
    if (item && typeof item === 'object') return `{${Object.entries(item).map(([key, val]) => `${key}=${literal(val)}`).join(', ')}}`;
    return typeof item === 'string' ? JSON.stringify(item) : String(item);
  }
  return `${name}(${value && typeof value === 'object' && !Array.isArray(value)
    ? Object.entries(value).map(([key, val]) => `${key}=${literal(val)}`).join(', ') : literal(value)})`;
}
