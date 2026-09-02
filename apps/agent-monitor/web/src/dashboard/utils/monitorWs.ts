/** Resolve an explicit monitor endpoint or the viewer's same-origin `/ws`. */
export const monitorWsUrl = (): string => {
  const meta = import.meta as { env?: Record<string, string | undefined> };
  const explicit = meta.env?.VITE_MONITOR_WS_URL;
  if (explicit) {
    return explicit.replace(/\/+$/, '').replace(/^http/, 'ws');
  }
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/ws`;
};
