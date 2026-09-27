export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: method === 'GET' ? undefined : JSON.stringify(body ?? {}), signal: AbortSignal.timeout(30000),
    });
  } catch {
    throw new ApiError(0, '服务连接超时或网络中断。免费服务唤醒可能需要约一分钟，请稍后重试。');
  }
  const data = await response.json().catch(() => ({ detail: '服务正在启动或暂时不可用，请稍后重试。' }));
  if (!response.ok) throw new ApiError(response.status, data.detail || '请求失败');
  return data as T;
}
