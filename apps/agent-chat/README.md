# 循迹 · Agent Chat

独立的 React / TypeScript / Vite 页面与 FastAPI 服务，直接使用主仓 `gh-puller[search]`。
前端组件和样式位于本目录，不依赖 `ui/`、实验 TUI 或研究归档。

## 本地运行

从主仓的 `apps/agent-chat/` 执行：

```bash
uv sync --frozen
cd web
npm ci
npm run build
cd ..
read -rs -p '私人访问口令（至少 12 字符）: ' CHAT_ACCESS_PASSWORD
export CHAT_ACCESS_PASSWORD
CHAT_SECURE_COOKIE=false uv run --frozen uvicorn agent_chat.app:application \
  --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

打开 `http://127.0.0.1:8000`。访问口令在终端静默输入，模型与平台密钥在页面设置中输入。
前端开发时另开终端，在 `web/` 执行 `npm run dev`，Vite 将 `/api` 转发到本机 8000 端口。
应用不自动读取任何 `.env`。`CHAT_SECURE_COOKIE=false` 仅用于本机 HTTP；公网保持 `true`。

Python 依赖锁在 `uv.lock`，前端依赖锁在 `web/package-lock.json`。本地的主包来源是 `../..`，
修改主仓源码会直接反映到本地应用。`gh-puller[search]` 的 agent 依赖包含 Codex 和 Claude SDK；
它们的发行包自带 CLI，因此首次同步下载量较大。本应用查询使用 HTTP 模型适配器。

## 使用与数据生命周期

- 新会话默认 GitHub / DSL，第一条问题生成简短标题。第一次提交后 agent 和工具配置固定，模型配置和密钥仍可更新。
- GitHub 支持 DSL、REST、GraphQL、Split；GitCode 支持 DSL、REST；Web 提供搜索和下载。
  PTC A/B 需要服务端 Node；Code 需要显式配置 Docker 容器连接，免费部署中显示为不可用。
- 设置默认沿用实验的模型地址与 `deepseek-v4.1-flash`，最大步数 32、工具并发 8。
  Brave 需要密钥，也可选择 Auto / DuckDuckGo。模型供应商须支持所选 thinking / reasoning 参数。
- Enter 发送，Shift+Enter 换行。执行区域显示实际模型、reasoning 与工具事件。停止会取消服务端任务；关闭页面或 SSE 断线不会取消任务。
- GitHub DSL 首次初始化在后台线程完成，页面显示实际初始化状态，期间仍可停止查询或访问健康检查。
- 同一浏览器的历史、事件与非敏感设置保存在 IndexedDB。搜索、重命名、删除与 JSON 导入导出均可在侧栏操作。
  导入只生成只读历史，不将展示消息写回 agent 上下文。
- 模型和平台密钥只在页面与活跃服务端会话内存中保留。刷新不回填密钥；服务端上下文仍存在时可继续使用。
  新建会话需要重新提供刷新后丢失的密钥。
- 页面导出 `events.json`，历史导出 `chat-history.json`。服务端临时记录为脱敏 JSONL，保留必要 ToolStorage 文件，
  不保存源码快照、原始模型流或研究归档。提供的密钥及跨分片的完整密钥值会被脱敏。
- 空闲会话 60 分钟后释放，运行中不按空闲时间回收。退出会删除该浏览器的服务端会话；删除活跃会话会先停止任务。
  服务重启、清理或免费实例休眠后，浏览器历史保留为只读。

```mermaid
sequenceDiagram
  participant Browser as 浏览器 / IndexedDB
  participant API as FastAPI 单进程
  participant Agent as 主仓 agent
  Browser->>API: Cookie 认证 + 问题 + request_id
  API-->>Browser: 202 已接受（重复标识不重复调用）
  API->>Agent: 活跃上下文中执行
  Browser->>API: SSE after=已收到的序号
  Agent->>API: 模型 / 工具事件
  API-->>Browser: 脱敏并编号的事件
  Note over Browser,API: 断线后补发，按序号去重；查询继续
  Note over API,Agent: 重启后不能从展示消息重建工具上下文
```

服务限制为全局最多 8 个活跃会话、2 个同时运行的问题；每次最长 15 分钟，每会话最多 100 次提问、
16 MiB 事件记录与 64 MiB 临时工具文件。超限会给出明确状态，导出后新建会话。
模型 URL 只接受公网 HTTP(S) 地址、80/443 端口，无 URL 凭据；每次连接检查 DNS 并固定实际地址。
Markdown 不执行原始 HTML，远程图片显示为链接。

## 镜像

在主仓根目录构建。Git 提交同时标识应用和 `gh-puller` 源码，依赖使用锁文件：

```bash
docker build -f apps/agent-chat/Dockerfile \
  --build-arg APP_REVISION="$(git rev-parse HEAD)" -t agent-chat:local .
read -rs -p '私人访问口令（至少 12 字符）: ' CHAT_ACCESS_PASSWORD
export CHAT_ACCESS_PASSWORD
docker run --rm --name agent-chat-local --memory=512m -p 127.0.0.1:8000:10000 \
  -e CHAT_ACCESS_PASSWORD -e CHAT_SECURE_COOKIE=false agent-chat:local
```

多阶段构建分别产出前端与 Python wheels，运行层采用非 root 用户、一个 Uvicorn worker。
源码包在安装时强制重新构建，避免 uv 的 wheel 缓存沿用旧源码；第三方依赖仍复用缓存。
Dockerfile 专属 ignore 文件仅允许主包、应用代码与构建元数据进入上下文，不包含实验目录、本机 `.env`、
宿主机虚拟环境、浏览器历史或测试替身。Node 仅用于 PTC；镜像不配置 Docker daemon。
`APP_REVISION` 用于本机镜像，Render 运行时优先读取 `RENDER_GIT_COMMIT`。

环境变量：

| 变量 | 用途 |
| --- | --- |
| `CHAT_ACCESS_PASSWORD` | 必填，至少 12 字符的私人访问口令 |
| `CHAT_SECURE_COOKIE` | 默认 `true`；本机 HTTP 才设 `false` |
| `PORT` | 镜像监听端口，默认 10000 |
| `CHAT_STATIC_DIR` | 前端构建产物位置，镜像已配置 |
| `CHAT_CODE_CONTAINER` / `CHAT_CODE_WORKDIR` | 可选，仅在自行提供 Docker CLI 与容器连接时启用 Code |

## Render 免费部署

根目录 [render.yaml](../../render.yaml) 定义一个新加坡区域的 Free Docker Web Service，单实例，无数据库、磁盘、
付费附加服务或自动升级。源码仓库为 `Delva0/gh-puller`，部署分支 `agent-chat`；构建上下文是主仓根目录，
Dockerfile 是 `apps/agent-chat/Dockerfile`，健康检查 `/api/health`，自动部署关闭。

当前公网地址：[循迹 Agent Chat](https://agent-chat-pdl4.onrender.com)。
服务管理页：[Render Dashboard](https://dashboard.render.com/web/srv-dasjnn8473hc738kgnk0)。

1. 将验证过的应用提交推送到 `agent-chat` 分支。
2. 在 Render 的 **My Workspace → New → Blueprint** 连接 `Delva0/gh-puller`，选择 `agent-chat` 分支和根目录 `render.yaml`。
3. 在 Blueprint 提示中填写 `CHAT_ACCESS_PASSWORD`，确认服务方案为 **Free**，应用配置。
4. 部署完成后打开服务的 `https://…onrender.com` 地址。在登录页输入访问口令，在设置中输入模型与平台密钥。
5. 后续部署选择已验证的具体提交；对照 `/api/health` 的 `revision` 与 Render deploy 的 commit SHA。

当前 Render 插件的服务创建工具不支持完整 Docker 配置，因此首次 Blueprint 创建需要 Dashboard。
创建后可通过插件检查部署、日志和指标。Schema 验证使用 [Render 官方 JSON Schema](https://render.com/schema/render.yaml.json)。

### 后续更新

在主仓 `/home/delva/projects/gh-puller` 修改代码，完成下文的相关验证并提交，再推送部署分支：

```bash
git -C /home/delva/projects/gh-puller push origin HEAD:agent-chat
```

当前推送不会自动发布。在服务管理页选择 **Manual Deploy → Deploy latest commit**，或通过 Render 插件触发部署。
部署结束后确认状态为 `Live`，并核对 `/api/health` 的 `revision` 与本次提交相同。服务地址保持不变。
Graphub 是独立实验仓，其提交不会自动进入此部署分支。

如需推送后自动部署，先确认 Render 已连接 GitHub，再将 `render.yaml` 中的 `autoDeployTrigger: "off"`
改为 `autoDeployTrigger: commit` 并同步 Blueprint；之后推送 `agent-chat` 即触发构建。
参见 [Render 部署说明](https://render.com/docs/deploys)。

[Render Free](https://render.com/docs/free) 有休眠、冷启动、资源及月度额度限制；空闲 15 分钟可能休眠，唤醒通常约一分钟，
内存与临时文件不能跨重启保留。Free 实例内存 512 MB，不适合大量并发或无限历史上下文。
托管按 Free 方案配置；模型和收费搜索 API 的调用费由各供应商收取。

## 验证

```bash
# apps/agent-chat/
uv run --frozen pytest -q
uv run --frozen ruff check agent_chat tests
cd web
npm run build
npx playwright install chromium --only-shell
npm test
```

浏览器测试启动真实 FastAPI 服务和主仓 agent，仅替换外部模型 / 来源 HTTP 传输。覆盖登录、密钥设置、流式输出、
工具展开、复制、会话切换和搜索、刷新继续、停止、历史导入导出、会话失效只读，以及移动端长代码、表格和历史列表。
测试报告与截图保存在 `web/playwright-report/` 和 `web/test-results/`，不会提交。

公网验收必须另行在真实 HTTPS 服务上执行 GitHub、GitCode、Web 的真实查询，并检查刷新、停止、事件导出和内存。
自动模拟测试通过不代表公网验收完成。实际交付状态记录于 [VERIFICATION.md](VERIFICATION.md)。

真实浏览器验收脚本是 `web/scripts/live-acceptance.mjs`。在进程环境中提供 `CHAT_TEST_URL`、
`CHAT_ACCESS_PASSWORD`、`OPENAI_API_KEY`、`GH_TOKEN`、`GITCODE_TOKEN`、`BRAVE_SEARCH_API_KEY`，
然后从 `web/` 执行 `node scripts/live-acceptance.mjs`。可选 `OPENAI_BASE_URL` 和 `CHAT_TEST_MODEL` 覆盖模型设置。
脚本不启用浏览器 trace、不输出密钥；脱敏事件与截图保存到忽略的 `verification/live-browser/`。
该脚本会发生真实供应商调用费用；每条验收查询最多 8 步、4096 输出 tokens。结束后退出并释放测试会话。
