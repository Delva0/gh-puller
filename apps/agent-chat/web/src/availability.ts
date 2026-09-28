import type { ValidationState } from './types';

export interface Availability { available: boolean; pending: boolean; issues: ValidationState['issues']; reason?: string }

export function availability(report?: ValidationState, drafts: Record<string, string> = {}): Availability {
  const issues: ValidationState['issues'] = { ...report?.issues, ...Object.fromEntries(Object.entries(drafts).map(([key, reason]) => [key, { valid: false, reason }])) };
  return { available: Boolean(report?.valid && !Object.keys(issues).length), issues,
    pending: Object.keys(issues).length ? Object.values(issues).every(issue => issue.pending) : !report };
}
