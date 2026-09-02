/** Configure React's test renderer contract for Vitest. */

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

const stored = new Map<string, string>();
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: {
    clear: () => stored.clear(),
    getItem: (key: string) => stored.get(key) ?? null,
    key: (index: number) => [...stored.keys()][index] ?? null,
    get length() { return stored.size; },
    removeItem: (key: string) => stored.delete(key),
    setItem: (key: string, value: string) => stored.set(key, value),
  },
});
