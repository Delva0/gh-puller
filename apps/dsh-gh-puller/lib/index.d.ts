/**
 * dsh 原生工具插件:在宿主进程内注册 graphify_query / graphify_index。
 *
 * 零框架导入:注册对象直接构造为 ToolDefinition 契约(parameters 为逐属性 DSL,
 * 由 registry 惰性编译),因此不依赖 dsh 加载器对框架包的解析域,
 * path/--patch/bundle 任一装法均可加载。
 * Python worker 经 `uv run --project <server> gh-graphify-worker` 常驻加载。
 *
 * 类型面说明:@deepseek-ai/* 包为 pre-release 且依赖未发布的内部包,无法作为
 * 可安装依赖使用;此处内联与 @deepseek-ai/dsh-tools(schema.ts 的
 * ParameterSchemaSpec/ValueSchemaSpec/ToolDefinition)同形的最小契约面,
 * 注册前经 dsh `tools.register` 的运行时校验兜底。
 */
import { WorkerClient, type Spawner } from './worker.js';
/** dsh ToolRunContext 最小面(仅用到的信号面)。 */
export interface ToolRunContext {
    signal: AbortSignal;
}
/** dsh 参数逐属性 DSL(与 @deepseek-ai/dsh-tools ParameterSchemaSpec 同形)。 */
export interface ToolParameterSpec {
    type?: 'string' | 'number' | 'integer' | 'boolean' | 'null' | 'array' | 'object' | 'json';
    required?: true;
    description?: string;
    enum?: readonly string[];
    items?: ToolParameterSpec;
    properties?: Record<string, ToolParameterSpec>;
    additionalProperties?: boolean;
}
/** dsh ToolDefinition 最小面(本插件只产出 string 值输出)。 */
export interface ToolDefinition {
    name: string;
    description: string;
    parameters: Record<string, ToolParameterSpec>;
    output: {
        schema: {
            type: 'string';
        };
        render(args: Record<string, unknown>, value: string): Array<{
            type: 'text';
            text: string;
        }>;
    };
    isConcurrencySafe?(args: Record<string, unknown>): boolean;
    execute(args: Record<string, unknown>, exec: ToolRunContext): Promise<string>;
}
/** dsh Cordis ctx 最小面(apply 的实参;仅用 tools.register 与 effect)。 */
export interface Context {
    effect(cb: () => (() => void) | void): unknown;
    tools: {
        register(def: ToolDefinition): () => void;
    };
}
export declare const name = "graphify";
export declare const inject: string[];
/** 插件行 config:serverDir 缺省为插件包内 ../server(与 npm 包同目录,随仓库重定位)。 */
export interface Config {
    serverDir?: string;
    /** 启动缺省图(graph.json);工具未给 repo 参数时回退。 */
    defaultGraph?: string;
}
export declare function createWorker(serverDir: string, spawner?: Spawner): WorkerClient;
/** 工具定义集合(worker 注入,便于测试用假 worker 直测 execute 全链路)。 */
export declare function buildTools(worker: WorkerClient, defaultGraph?: string): ToolDefinition[];
export declare function apply(ctx: Context, config: Config): void;
