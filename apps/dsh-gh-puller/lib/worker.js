/**
 * graphify worker 客户端:常驻子进程(`uv run --project <server> gh-graphify-worker`),
 * NDJSON 按 id 关联请求/响应;子进程崩溃时对当前请求惰性重启一回合再试。
 *
 * 只依赖 node:child_process,不 import 任何 dsh 框架包(加载器解析域无关)。
 * 协议线(与 Python worker.py/协议帧一致):请求 {id, action, ...args};
 * 响应 {id, ok: true, text} / {id, ok: false, error}。
 */
import { spawn } from 'node:child_process';
export class WorkerError extends Error {
}
const defaultSpawn = (args, cwd) => spawn('uv', args, { cwd, stdio: ['pipe', 'pipe', 'pipe'] });
export class WorkerClient {
    serverDir;
    spawner;
    child = null;
    buffer = '';
    nextId = 1;
    pending = new Map();
    disposed = false;
    constructor(serverDir, spawner = defaultSpawn) {
        this.serverDir = serverDir;
        this.spawner = spawner;
    }
    args() {
        return ['run', '--no-sync', '--project', this.serverDir, 'gh-graphify-worker'];
    }
    /** 请求一次(ok:false 时把 error 作为说明文本返回);传输层失败重试一回合后抛 WorkerError。 */
    async request(action, params = {}, signal) {
        let last;
        for (let attempt = 0; attempt < 2; attempt++) {
            try {
                const resp = await this.dispatch(action, params, signal);
                return resp.ok ? (resp.text ?? '(empty response)') : (resp.error ?? '(worker error)');
            }
            catch (e) {
                last = e;
            }
        }
        throw new WorkerError(`graphify worker unavailable: ${last instanceof Error ? last.message : String(last)}`);
    }
    /** 发送并在同 id 的响应帧上挂起;子进程死亡则所有挂起请求以失败结算。 */
    dispatch(action, params, signal) {
        if (signal?.aborted)
            return Promise.reject(new WorkerError('aborted'));
        const child = this.ensure();
        const id = this.nextId++;
        return new Promise((resolve, reject) => {
            const onAbort = () => finish(new WorkerError('aborted'));
            const finish = (result) => {
                signal?.removeEventListener('abort', onAbort);
                this.pending.delete(id);
                if (result instanceof Error)
                    reject(result);
                else
                    resolve(result);
            };
            this.pending.set(id, finish);
            if (signal)
                signal.addEventListener('abort', onAbort, { once: true });
            try {
                child.stdin.write(JSON.stringify({ id, action, ...params }) + '\n');
            }
            catch (e) {
                finish(new WorkerError(`worker write failed: ${String(e)}`));
            }
        });
    }
    /** 确保子进程在线(惰性拉起);崩溃后由清除逻辑置 null,下次请求重新 spawn。 */
    ensure() {
        if (this.disposed)
            throw new WorkerError('worker disposed');
        if (this.child && this.child.exitCode === null)
            return this.child;
        const child = this.spawner(this.args(), this.serverDir);
        child.stdout.setEncoding('utf-8');
        child.stdout.on('data', (chunk) => this.onData(chunk));
        child.stderr.on('data', () => void 0); // 协议帧只走 stdout;worker 日志走 stderr,忽略
        child.on('close', () => {
            if (this.child !== child)
                return;
            this.child = null;
            this.flushPending(new WorkerError('worker exited'));
        });
        child.on('error', (e) => {
            if (this.child !== child)
                return;
            this.child = null;
            this.flushPending(new WorkerError(`worker spawn failed: ${e.message}`));
        });
        this.child = child;
        return child;
    }
    /** 逐行解析 stdout(尾行不完整则留在缓冲中,等下次 data 拼上)。 */
    onData(chunk) {
        this.buffer += chunk;
        for (;;) {
            const newline = this.buffer.indexOf('\n');
            if (newline === -1)
                return;
            const line = this.buffer.slice(0, newline);
            this.buffer = this.buffer.slice(newline + 1);
            if (!line.trim())
                continue;
            try {
                const resp = JSON.parse(line);
                if (typeof resp.id === 'number')
                    this.pending.get(resp.id)?.(resp);
            }
            catch {
                // 坏帧忽略(协议外字节不进入 STDIN/STDOUT 侧的路径)
            }
        }
    }
    flushPending(error) {
        for (const finish of [...this.pending.values()])
            finish(error);
        this.pending.clear();
    }
    dispose() {
        this.disposed = true;
        this.flushPending(new WorkerError('worker disposed'));
        this.child?.kill('SIGTERM');
        this.child = null;
    }
}
