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
import { fileURLToPath } from 'node:url';
import { WorkerClient, WorkerError } from './worker.js';
export const name = 'graphify';
export const inject = ['tools'];
const REPO_TYPE_DESC = 'github | gitlab | bitbucket | local;缺省:URL→github,本地路径→local。须与建图时类型一致。';
export function createWorker(serverDir, spawner) {
    return new WorkerClient(serverDir, spawner);
}
/** 执行兜底:传输层异常转可读文本(与 Python 侧错误帧同语义,不打断模型对话)。 */
async function runTool(worker, action, args, exec, extra = {}) {
    try {
        return await worker.request(action, { ...args, ...extra }, exec.signal);
    }
    catch (e) {
        return `Graph worker failed: ${e instanceof WorkerError ? e.message : String(e)}`;
    }
}
/** 文本输出契约:string 规范化值 + 文本块渲染。 */
const textOutput = {
    schema: { type: 'string' },
    render: (_args, value) => [{ type: 'text', text: value }],
};
function queryTool(worker, defaultGraph) {
    return {
        name: 'graphify_query',
        description: '查询仓库代码图(BFS/DFS,本地执行,无 LLM)。返回相关代码子图文本:节点行 '
            + '`NODE <label> [src=<file> loc=L<n> ...]`、边行 `EDGE a --<relation>--> b at=<file>:L<n>`;'
            + '用于获取代码结构、调用关系与行号引用。未给 repo 时回退启动配置的默认图。',
        parameters: {
            question: {
                type: 'string',
                required: true,
                description: '自然语言问题或关键词(函数/模块/概念)。',
            },
            repo: {
                type: 'string',
                description: '仓库 URL 或本地路径;解析到 <DEEPWIKI_ROOT>/graphify/{repo_type}_{name}/graph.json。',
            },
            repo_type: {
                type: 'string',
                enum: ['github', 'gitlab', 'bitbucket', 'local'],
                description: REPO_TYPE_DESC,
            },
        },
        output: textOutput,
        isConcurrencySafe: () => true,
        execute: (args, exec) => runTool(worker, 'query', args, exec, { defaultGraph }),
    };
}
function indexTool(worker) {
    return {
        name: 'graphify_index',
        description: '把本地仓库建为代码图(code_only AST,无 API key,离线)。'
            + '产物写 <DEEPWIKI_ROOT>/graphify/local_<路径名>/graph.json,与 deepwiki 索引同约定、互相复用;'
            + '已存在同目录图会被重建。',
        parameters: {
            path: { type: 'string', required: true, description: '本地仓库绝对路径。' },
            repo_type: { type: 'string', enum: ['github', 'gitlab', 'bitbucket', 'local'], description: REPO_TYPE_DESC },
        },
        output: textOutput,
        isConcurrencySafe: () => false,
        execute: (args, exec) => runTool(worker, 'index', args, exec),
    };
}
/** 工具定义集合(worker 注入,便于测试用假 worker 直测 execute 全链路)。 */
export function buildTools(worker, defaultGraph) {
    return [queryTool(worker, defaultGraph), indexTool(worker)];
}
export function apply(ctx, config) {
    const worker = createWorker(config.serverDir ?? defaultServerDir());
    ctx.effect(() => () => worker.dispose());
    for (const def of buildTools(worker, config.defaultGraph)) {
        ctx.tools.register(def);
    }
}
function defaultServerDir() {
    // lib/index.js 或 src/index.ts 的上两层即插件包根下 server/(file: 安装为 symlink,Node 默认 realpath)
    return fileURLToPath(new URL('../server', import.meta.url));
}
