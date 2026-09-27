# Agent Chat 验证记录

本记录区分可重复的自动测试与真实公网验收。源码身份以应用提交和 `/api/health` 返回的 Git SHA 为准。

| 检查 | 当前结果 |
| --- | --- |
| 后端应用测试 | 29 项通过，覆盖认证、隔离、补发、取消、去重、脱敏、配额、清理、初始化期间响应和 PTC A/B 嵌套工具 |
| 主仓相关回归测试 | 26 项通过：search、tool_storage、events、OpenAI adapter |
| Ruff | 应用与后端测试通过 |
| 前端 TypeScript / Vite | 通过 |
| Render Blueprint | 官方 JSON Schema 通过；已移除 Free 不支持的 maxShutdownDelaySeconds，平台验收待继续 |
| 本地真实模型与工具接入 | GitHub、GitCode、Web 均通过，见下表 |
| 浏览器 E2E / 视觉检查 | 4 组通过；桌面 1440×1000、手机 390×844 截图已检查 |
| 512 MB 生产镜像 | 通过真实浏览器验收；0.1 CPU，峰值约 135 MiB，无 OOM，健康检查无超时 |
| HTTPS 公网真实模型与工具 E2E | 待完成，尚无已验收公网地址 |

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

以上地址是本机 HTTP，Render HTTPS 公网部署、平台冷启动和公网资源指标仍未验收。
