/**
 * graphify worker 客户端:常驻子进程(`uv run --project <server> gh-graphify-worker`),
 * NDJSON 按 id 关联请求/响应;子进程崩溃时对当前请求惰性重启一回合再试。
 *
 * 只依赖 node:child_process,不 import 任何 dsh 框架包(加载器解析域无关)。
 * 协议线(与 Python worker.py/协议帧一致):请求 {id, action, ...args};
 * 响应 {id, ok: true, text} / {id, ok: false, error}。
 */
import { type ChildProcess } from 'node:child_process';
/** 一行响应帧(协议字段的最小形态)。 */
export interface WorkerResponse {
    id?: number;
    ok: boolean;
    text?: string;
    error?: string;
}
export type Spawner = (args: string[], cwd: string) => ChildProcess;
export declare class WorkerError extends Error {
}
export declare class WorkerClient {
    private readonly serverDir;
    private readonly spawner;
    private child;
    private buffer;
    private nextId;
    private pending;
    private disposed;
    constructor(serverDir: string, spawner?: Spawner);
    private args;
    /** 请求一次(ok:false 时把 error 作为说明文本返回);传输层失败重试一回合后抛 WorkerError。 */
    request(action: string, params?: Record<string, unknown>, signal?: AbortSignal): Promise<string>;
    /** 发送并在同 id 的响应帧上挂起;子进程死亡则所有挂起请求以失败结算。 */
    private dispatch;
    /** 确保子进程在线(惰性拉起);崩溃后由清除逻辑置 null,下次请求重新 spawn。 */
    private ensure;
    /** 逐行解析 stdout(尾行不完整则留在缓冲中,等下次 data 拼上)。 */
    private onData;
    private flushPending;
    dispose(): void;
}
