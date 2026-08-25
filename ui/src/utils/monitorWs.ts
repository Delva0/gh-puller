// 监控 hub WS 地址:VITE_MONITOR_WS_URL 优先(去尾斜杠、http→ws 归一),
// 缺省同源 + /ws(页面由 hub 同端口服务,hub 的 WS 端点是 /ws);模式取自 webui websocketClient.ts。
export const monitorWsUrl = (): string => {
  // @gh-puller/ui 按库编译(无 vite/client 类型),环境变量经窄化读取
  const meta = import.meta as { env?: Record<string, string | undefined> };
  const explicit = meta.env?.VITE_MONITOR_WS_URL;
  if (explicit) {
    return explicit.replace(/\/+$/, '').replace(/^http/, 'ws');
  }
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/ws`;
};
