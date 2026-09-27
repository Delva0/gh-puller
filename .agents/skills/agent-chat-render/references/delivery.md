# 持续交付

## 修改、验证和发布

主仓负责应用与生产代码，Graphub 实验仓保持独立。先检查主仓当前分支和未提交修改，按实际改动验证：

```bash
cd /home/delva/projects/gh-puller/apps/agent-chat
uv run --frozen pytest -q
uv run --frozen ruff check agent_chat tests
cd web
npm run build
npm test
```

主包变更额外运行相关主包测试。纯文档修改不要求重跑模型或重建镜像。
前端默认值变更同时检查服务端 `PublicSettings` / catalog、前端 fallback、`settingsFor`、新会话初始化和已保存偏好。
首次提问后工具配置固定；不要为了迁移默认值改写旧会话的工具上下文。

只提交本次任务涉及的文件。验证后的主仓提交推送到部署分支：

```bash
git -C /home/delva/projects/gh-puller push origin HEAD:refs/heads/agent-chat
```

不要误在 Graphub 仓执行，也不要把例行发布改成强制推送。
推送前确认工作树的应用变更已提交；推送后确认远端 SHA。

当前自动部署关闭，推送与发布是两个动作：

- Render 插件 `trigger_deploy(serviceId, workspaceId)`；通常保留构建缓存。
- Dashboard：Manual Deploy → Deploy latest commit；需要固定历史版本时选 Deploy a specific commit。

触发前读取部署状态，已有相同提交正在构建时观察现有部署，不重复触发。
`autoDeployTrigger` 已启用时，推送已触发部署，不再手动触发一次。
若用户要求自动持续交付，确认 GitHub 已连接，把 Blueprint 的 `autoDeployTrigger` 改为 `commit` 并同步平台。
只有 CI 确实存在且覆盖发布条件时才选 `checksPass`；没有检查时它不会按预期发布。
通过公共仓库 URL 部署但未连接 Git provider 的服务可能只支持手动发布。

发布完成的依据是：Render deploy `live`、其 commit SHA 正确、HTTPS 健康检查返回同一 SHA。
不能只看工具调用返回成功或构建完成。`RENDER_GIT_COMMIT` 在 Render 上优先于本机镜像的 `APP_REVISION`。
更新既有服务会保留 URL；应用重启会失去活跃会话上下文，浏览器历史转为只读。

构建失败时查失败阶段与最后有效错误，修复后再发布；不循环清缓存重试。
运行故障需要回滚时，使用已验证且仍可用的历史提交，随后重新检查 health / SHA。
Free 回滚保留范围有限，以平台当前可用记录为准。不要用回滚掩盖未诊断的外部日志接口故障。

## 生产镜像验收

从主仓根目录构建，并明确传入源码身份：

```bash
docker build -f apps/agent-chat/Dockerfile \
  --build-arg APP_REVISION="$(git rev-parse HEAD)" -t agent-chat:verification .
```

需要资源复验时使用 512 MB / 0.1 CPU 限制；访问口令经环境传入，不写在命令参数里。
检查非 root、无 editable 安装、没有宿主 `.env` / 实验目录、前端与后端同源、单 worker。
本次初始镜像约 385 MB；首次 GitHub DSL 结构初始化在弱 CPU 下较慢，已改为共享后台线程，
要同时观察健康检查和取消响应。版本默认改为 REST 后仍保留 DSL 可选能力。

更改 Docker 构建依赖或缓存策略时核对最终安装文件。只变更 Git SHA 标签且运行层相同时，
可复用已完成的行为验收，并验证新镜像的 health / 文件哈希，不重复付费查询。

## 真实公网验收

应用脚本 `apps/agent-chat/web/scripts/live-acceptance.mjs` 操作真实网页与主仓 agents，
验证登录、Cookie 属性、GitHub / GitCode / Web、工具展开、刷新续问、停止、导出与事件去重。
它使用真实模型与工具，不开启浏览器 trace；每次会产生供应商调用费用。
验收参数目前为最多 8 步、4096 输出 tokens；不要与交互默认 32 步 / 8192 tokens 或其他实验参数直接比较耗时。

在本来就包含公网验收的任务中复用已有授权。只查询状态或编写说明时不顺带调用模型。
凭据只从用户允许的环境 / 文件读取；必需 `CHAT_ACCESS_PASSWORD`、`OPENAI_API_KEY`，
其余使用 `GH_TOKEN`、`GITCODE_TOKEN`、`BRAVE_SEARCH_API_KEY`，可选 `OPENAI_BASE_URL`、`CHAT_TEST_MODEL`。
缺少 Brave 凭据时脚本使用 DuckDuckGo，实际搜索后端应在结果中记录。

从主仓根目录运行包装器，`--revision` 填本次线上部署的完整 SHA：

```bash
uv run --project apps/agent-chat --frozen python \
  .agents/skills/agent-chat-render/scripts/run_live_browser.py \
  --url https://agent-chat-pdl4.onrender.com \
  --revision EXPECTED_FULL_COMMIT_SHA --env-file .env
```

额外凭据文件须显式追加 `--env-file`，后面的同名变量覆盖前面的；不隐式读取实验目录。
包装器先检查公网版本，再读取凭据并运行现有浏览器脚本。默认结果放在应用已忽略的 `verification/` 下。
一次失败先保留证据、判断原因；只有代码修复、参数纠正或已辨明的临时故障才重跑，避免连续产生未知费用。

本机 Playwright 曾遇到：Chromium 尚未安装、缺少 nss/nspr/asound 库、中文字体缺失。
按实际错误安装浏览器与系统依赖；无 sudo 时可把已验证的系统库解包到本机临时目录并配置 `LD_LIBRARY_PATH`。
此开发机目前可用路径是 `/tmp/agent-chat-browser-libs/usr/lib/x86_64-linux-gnu`，临时目录可能已消失，应先检查。
本机 Noto Sans SC 用于截图验收；这些浏览器依赖不进入生产镜像。

验收出口：逐项阅读答案、工具结果与失败记录，保存实际请求参数、源码 SHA 和数据来源。
脚本 exit 0 不表示每次工具调用都成功；工具失败次数和最终问题是否解决分别记录。
交付时报告公网 URL、部署提交、测试结果及剩余限制，证据留在已忽略的 `verification/`。
文档提交可以单独推送；它不会自动证明线上已运行该文档提交。

参考：[Render 部署与回滚](https://render.com/docs/deploys)、[应用说明](../../../../apps/agent-chat/README.md)。
