# dsh-gh-puller

dsh(DeepSeek Harness)插件:在 dsh 会话内注册两个**原生工具**——`graphify_query`(查代码图)与
`graphify_index`(把本地仓库建为代码图),检索结果带真实文件路径与行号(节点行 `src=<file> loc=L<n>`,
边行 `at=<file>:L<n>`),全程本地执行、无 LLM、无 API key。

**为什么是原生工具而不是 MCP**:dsh 自带 `@deepseek-ai/dsh-mcp-client`,需要 MCP 时直接加载即可、
无须插件;做成本插件即原生工具注册——工具裸名(无 `mcp__` 前缀,与 `gh_puller/deepwiki.py`
agent 会话中的 `graphify_query` 同名)、宿主进程内注册直通。Python 侧仍需要一个进程
(graphify 是 Python 库),以**普通跨进程通信**(NDJSON stdio 常驻 worker)对接,不上 MCP。

## 布局与原理

- `package.json` + `cordis.patch.yml` + `src/`(构建产物 `lib/`):dsh 插件包。零框架运行时导入
  (工具定义对象直接按 `@deepseek-ai/dsh-tools` 契约构造),不依赖 dsh 加载器的解析域。
- `server/`(独立 uv 项目,`gh-puller-dsh`):Python worker。插件经
  `uv run --project <server> gh-graphify-worker` 拉起,常驻摊薄 Python 导入与图加载成本;
  请求/响应为单行 NDJSON(协议侧 stdout 只出帧),逻辑复用 `gh_puller.graphify.query/extract`。
- 图目录约定与 deepwiki 完全一致:`<DEEPWIKI_ROOT>/graphify/{repo_type}_{name}/graph.json`
  (URL → `github_{owner}_{repo}`,本地路径 → `local_{basename}`)。deepwiki 已建的图直接可查,
  插件建的图 deepwiki 同样复用。

## 前置

- dsh(本地源码 rc.8 或更新),`uv`。
- gh-puller 仓库,且 Python 侧项目就绪:`cd apps/dsh-gh-puller/server && uv sync`。
- (可选)期望 `DEEPWIKI_ROOT` 指向自定义目录时,先设好环境变量再启 dsh(wrapper 子进程继承之)。

## 启用

```bash
# bundle 式(推荐):插件随 dsh plugin 自动并入 profile
dsh plugin --profile web add /abs/path/gh-puller/apps/dsh-gh-puller

# 开发式:直接挂 patch 层
dsh web --patch /abs/path/gh-puller/apps/dsh-gh-puller/cordis.patch.yml
```

启动后 dsh 会话内可用 `graphify_query` / `graphify_index`。插件包已随仓库重定位:
`server/` 在插件包内,`uv run --project <server>` 的路径由插件经 `import.meta.url` 自解析。

## 工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `graphify_query` | `question`(必填)、`repo`(URL/本地路径)、`repo_type` | 代码图问答(BFS/DFS,本地);`repo` 缺省回退配置的默认图。 |
| `graphify_index` | `path`(必填,本地仓库绝对路径)、`repo_type` | code_only AST 建图(离线);同目录已存在的图会被重建。 |

`repo_type`:github | gitlab | bitbucket | local;缺省 URL→github、本地路径→local,须与建图时类型一致。

## 配置与环境变量

- 插件行 `config`:`serverDir`(worker 所在目录,缺省插件包内 `../server`)、
  `defaultGraph`(缺省 graph.json 路径)。
- `DEEPWIKI_ROOT`:图产物根,默认 `~/.gh-puller/deepwiki`(与 deepwiki 共用)。
- 注意:dsh 拉起子进程时会剥离凭证型(`KEY/PASSWORD/SECRET/TOKEN` 形状)与 `DSH_*` 环境变量。
  本插件全部本地执行(**code_only**,无 key 需求),不受影响;语义化建图(带 LLM 凭证)请在 dsh 外完成。

## 构建/自检(开发)

```bash
cd apps/dsh-gh-puller
pnpm install && pnpm exec tsc && pnpm vitest run     # 插件包;
cd server && uv sync && uv run pytest                # Python worker(全离线)
```
