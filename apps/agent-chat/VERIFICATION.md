# Agent Chat 验证记录

本记录区分可重复的自动测试与真实公网验收。源码身份以应用提交和 `/api/health` 返回的 Git SHA 为准。

| 检查 | 当前结果 |
| --- | --- |
| 后端应用测试 | 29 项通过，覆盖认证、隔离、补发、取消、去重、脱敏、配额、清理、初始化期间响应和 PTC A/B 嵌套工具 |
| 主仓相关回归测试 | 26 项通过：search、tool_storage、events、OpenAI adapter |
| Ruff | 应用与后端测试通过 |
| 前端 TypeScript / Vite | 通过 |
| Render Blueprint | 官方 JSON Schema 与平台创建通过；Free 不支持的 maxShutdownDelaySeconds 已移除 |
| 本地真实模型与工具接入 | GitHub、GitCode、Web 均通过，见下表 |
| 浏览器 E2E / 视觉检查 | 4 组通过；桌面 1440×1000、手机 390×844 截图已检查 |
| 512 MB 生产镜像 | 通过真实浏览器验收；0.1 CPU，峰值约 135 MiB，无 OOM，健康检查无超时 |
| HTTPS 公网上线 | 已上线，登录与健康检查通过，版本 b546d18 |
| HTTPS 公网真实模型与工具 E2E | REST 默认配置已完成；三类查询无工具错误，刷新续问、停止和导出通过 |
| Render 复用技能 | 结构校验、Ruff、引用路径检查通过；版本检查和真实浏览器包装器已实测 |

后端外部传输使用固定响应；浏览器测试同样不会调用真实模型。公网验收结果单独记录，不能由模拟测试替代。
JSON Schema 检查不覆盖套餐限制：首次 Dashboard 校验拒绝了 `maxShutdownDelaySeconds`，现使用平台默认关闭行为。

## 本地真实接入

模型 `deepseek-v4.1-flash`，默认实验网关；DSL / PTC off，8 步上限、工具并发 8、最大输出 4096 tokens，
thinking enabled / high，Brave 搜索。真实请求由应用 SessionManager 和主仓 agent 发起，源数据均为在线接口。
主包提交 `9b658ee8e2467fa50b9297315c09227572cc0d87`，应用为提交前工作树。没有使用模型或工具响应替身。

| Agent | 查询 | 耗时 | 模型请求 | 工具调用 | 工具失败 |
| --- | --- | --- | --- | --- | --- |
| GitHub / DSL | `psf/requests` 的默认分支与仓库描述 | 5.185 秒 | 2 | `github` × 1 | 0 |
| GitCode / DSL | `openharmony/docs` 的默认分支与仓库描述 | 2.767 秒 | 2 | `gitcode` × 2 | 0 |
| Web | 搜索并打开 Python 官方 asyncio 文档，概述用途 | 5.463 秒 | 3 | `web_search`、`web_fetch` | 0 |

人工核对了工具结果与最终回答：GitHub 返回 `main` 和 “A simple, yet elegant, HTTP library.”；
GitCode 返回 `master` 和“暂无描述”；Web 打开的官方页面返回 HTTP 200，正文支持 asyncio 用于 async/await 并发与 I/O 的说明。
三条回答均保留来源链接。本地依据：`verification/local-probe/github-events.json:239`（工具）、`:463`（结束回答）；
`gitcode-events.json:519`（仓库结果）、`:756`（回答）；`web-events.json:699`（网页正文）、`:1374`（回答）。
这些脱敏验收文件保留在本机，不随源码提交。JSON 事件已检查不包含提供的模型或平台密钥，临时 ToolStorage 在会话退出时释放。
这些结果只证明本地真实接入，尚不能证明 Render 网络与资源条件下的表现。

## 浏览器检查

Playwright Chromium 覆盖登录、Code 禁用、密钥配置、Enter / Shift+Enter、流式输出、工具与 reasoning 展开、
代码复制、会话新建 / 切换 / 搜索 / 重命名 / 删除、刷新后的密钥不回填与上下文继续、事件与历史导出、只读导入。
另验证 SSE 页面断开不取消任务、刷新后停止、服务端删除后只读，以及输出期间向上阅读不会被拉回。
滚动跟随与向上阅读的回归测试另连续运行 6 次，均通过。

桌面截图确认 280px 侧栏、居中聊天区、右侧用户气泡和底部输入区；手机截图确认抽屉与遮罩、独立滚动的
长代码和表格、43 项历史列表与回到底部入口。恶意 HTML 未产生 script 或 img 元素，未执行。
浏览器 IndexedDB 与导出内容均不含测试模型密钥或访问口令。
截图位于 `web/test-results/`，详细交互证据位于 `web/playwright-report/`，两者均未提交。

## 生产镜像与真实浏览器

使用固定 digest 的 Python / Node / uv 基础镜像，从主仓构建；Python 从锁定依赖和当前主包源码安装 wheels。
检查运行镜像中没有源码 checkout、实验目录、`.env` 或 editable 安装，使用 `chat` 非 root 用户。
本机已有公共 wheel 缓存用于加速 BuildKit，未复制宿主机虚拟环境或凭据。Docker 报告镜像大小 385,316,470 字节。
应用和主包每次源码安装强制重新构建；镜像内所有 Python 源码和已声明的工具资源均与工作树 SHA-256 一致。

在 `--memory=512m --cpus=0.1` 容器中，Playwright 经本机 HTTP 页面执行以下真实调用，模型参数与上述本地接入一致：

| 查询 | 耗时 | 模型请求 | 工具失败 |
| --- | --- | --- | --- |
| GitHub / DSL：`psf/requests` 默认分支和描述 | 30.776 秒（含首次结构初始化） | 2 | 0 |
| GitCode / DSL：`openharmony/docs` 默认分支和描述 | 6.049 秒 | 2 | 0 |
| Web：搜索并读取 Python asyncio 官方文档 | 9.865 秒 | 4 | 0 |

还验证了刷新后密钥框为空、沿用活跃上下文继续提问、实际运行中的停止、事件下载与序号去重。
峰值 cgroup 内存 141,475,840 字节（约 134.9 MiB），日志不含模型 / 平台密钥或访问口令，退出后临时会话目录释放。
运行中定期请求 `/api/health`，全部返回 200，最慢 1.896 秒；首次 GitHub 结构初始化的后台线程没有阻塞 HTTP。
结果仅覆盖上述小型顺序查询，不代表重型上下文或两条并发查询的内存上限。

最终本地证据目录 `verification/container-browser-04/`：`report.json` 为成功结果，`container-report.json` 为内存、健康检查与源码核对结果。
工具结果与最终回答分别位于 `github-events.json:359` / `:570`，`gitcode-events.json:1017` / `:1419`，
`web-events.json:649` / `:1094`，已逐项核对回答与原始结果；截图也保留在同一目录。
验收镜像为 `sha256:f8cc7b718dbc703767c119b839aab41c0165eff2944a0163156dcb3d2c65c21b`。

先前记录均保留：`verification/container-browser/` 是验收脚本按钮定位失败；`container-browser-02/` 通过交互验收，
`container-browser-03/` 增加健康检查后发现旧源码 wheel 被缓存、首次初始化期间 3 次超时。
修复镜像源码包重建后，以上最终验收重新完成全流程。

以上容器验收使用本机 HTTP。Render 公网状态见下节。

## 公网部署

地址：https://agent-chat-pdl4.onrender.com 。服务 `srv-dasjnn8473hc738kgnk0` 位于已确认的 My Workspace，
Singapore / Free，单实例，部署分支 `agent-chat`，自动部署关闭。
首次部署 `dep-dasjno0473hc738kgpqg` 于 2026-09-27 15:55:15 UTC 完成，平台状态为 `live`。
部署提交为 `94e42e39762e188868f23692f4c8c49adbfc3241`，与公网 `/api/health` 返回的 revision 一致。

2026-09-28（北京时间）经 HTTPS 验证：首页与健康检查均返回 200，证书校验通过；
Playwright 打开公网登录页并确认访问口令输入框。记录和截图保存在本机 `verification/render-public/`。
Render 指标在 15:56–16:05 UTC 的空闲内存约 69–70 MiB，限制为 512 MiB；这不是查询负载下的内存结果。

### 首次公网验收（DSL）

2026-09-27 16:09:03–16:10:10 UTC，Playwright 在上述 HTTPS 地址登录并执行真实查询。
模型参数与本地验收相同；部署版本为 `94e42e3`，GitHub / GitCode 使用 DSL，PTC off。
Cookie 的 HttpOnly、Secure、SameSite=Strict 已验证；刷新后的密钥框为空且可沿用服务端上下文继续提问，
实际查询的停止、事件下载及序号去重均通过。导出记录已检查不含提供的访问口令或模型 / 平台密钥。

| 查询 | 耗时 | 模型请求 | 工具调用 | 工具错误 |
| --- | --- | --- | --- | --- |
| GitHub：`psf/requests` 默认分支和描述 | 21.472 秒 | 2 | 1 | 0 |
| GitCode：`openharmony/docs` 默认分支和描述 | 19.155 秒 | 7 | 11 | 6 |
| Web：搜索并读取 Python asyncio 官方文档 | 12.056 秒 | 4 | 3 | 0 |

证据目录为 `verification/render-public/live-01/`。已人工核对工具结果与回答：
`github-events.json:242` / `:466`，`gitcode-events.json:474` / `:3849`，`web-events.json:1033` / `:2007`。
GitCode 的默认分支和描述在首次仓库查询已返回；随后追加核对出现 6 次工具错误：
原生响应缺字段（`:1006`）、两次查询语法错误（`:1469`、`:1499`）、两次 DSL 字段验证错误（`:1664`、`:2887`），
以及查询不存在的 `main` 分支 / README 所得的 404（`:1694`）。最终回答与工具证据一致，执行错误仍单独记录。

`health.json` 记录 11 次 HTTPS 健康请求，全部 200，最慢 2.549 秒。
`render-metrics.json` 中 30 秒分辨率的内存采样最大为 136,740,860 字节（约 130.4 MiB），限制 512 MiB；
采样不能证明瞬时峰值或重型并发查询的内存上限。
平台日志 API 本次返回 503（上游 Loki 502），未完成额外的服务器日志审查；失败响应保存在 `render-logs-unavailable.json`。
上述工具轨迹来自应用 SSE / `events.json`，内存指标来自 Render 带外 API。

### REST 默认配置公网复验

部署 `dep-daskdq8u01pc73cbnnrg` 于 2026-09-27 16:41:37 UTC 成为 `live`，
提交 `b546d185f9a5084ae3b33509233870c08227facf` 与 HTTPS 健康检查、浏览器读取的 revision 一致。
GitHub / GitCode 新会话默认 REST；浏览器脚本直接断言默认值，没有代替用户切换查询后端。
既有 DSL 会话不改写；服务重启后丢失上下文的历史只读。

16:42:09–16:42:43 UTC，通过 `.agents/skills/agent-chat-render/scripts/run_live_browser.py`
在真实 HTTPS 页面完成登录、三类查询、工具展开、刷新续问、停止、事件下载与序号去重。
模型及调用参数与首次公网验收相同，仅 GitHub / GitCode 的查询后端改为 REST；Web 使用 Brave。
Cookie 属性、刷新后密钥框为空、导出不含提供的凭据均通过。

| 查询 | 耗时 | 模型请求 | 工具调用 | 工具错误 |
| --- | --- | --- | --- | --- |
| GitHub：`psf/requests` 默认分支和描述 | 3.977 秒 | 2 | 1 | 0 |
| GitCode：`openharmony/docs` 默认分支和描述 | 5.799 秒 | 2 | 1 | 0 |
| Web：搜索并读取 Python asyncio 官方文档 | 8.096 秒 | 3 | 2 | 0 |

证据目录为 `verification/render-public/live-02-rest/`，`report.json` 记录实际 backend、问题、耗时与回答。
人工核对：`github-events.json:230` / `:467` 返回 `main` 和仓库描述；
`gitcode-events.json:295` / `:749` 返回 `master` 和“暂无描述”，缺少的 `html_url` 在投影遗漏中明确说明；
`web-events.json:441` / `:1181` 中 HTTP 200 的官方正文支持最终用途说明。
这组小型查询无工具错误，不表示所有 REST 调用已覆盖，也不证明 DSL 问题已经修复。

`render-metrics.json` 覆盖 16:41:30–16:42:44 UTC，分辨率 30 秒；新实例 `xc25n` 的内存采样最大为
90,337,280 字节（约 86.2 MiB），限制 512 MiB。切换前旧实例 `bjkv7` 的采样不归入此查询负载。
采样点有限，未测瞬时峰值、重型并发容量或强制休眠后的冷启动。
平台日志接口此次恢复：`render-error-logs.json` 在 16:41:37–16:42:44 UTC 返回空的应用 error 日志，
仅代表该过滤条件与时间窗；首次验收的 503 记录仍保留，不据此宣称所有平台日志均已审计。

默认值修改后的本地检查仍为后端 29 项、Playwright 4 组通过，Ruff、TypeScript 和 Vite 构建通过。
技能健康检查已验证正确 SHA 返回 200，错误 SHA 会在读取凭据和调用模型前阻止验收。
技能及验证记录提交可领先于线上应用提交；纯说明修改没有再次触发部署。
