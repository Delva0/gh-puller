# 部署

## 先核对现有服务

优先使用已连接的 Render 插件 / MCP。先读取服务的 repo、branch、runtime、plan、region、Docker 路径、URL 和部署状态。
本项目的已确认标识见 [SKILL.md](../SKILL.md)；存在目标服务时更新它，避免另建同名服务。
插件已安装不代表已认证；认证完成也不代表已选择工作区。按接口错误区分这两种状态。

初次连接 Codex CLI 插件时，以当前 CLI 提供的插件认证入口为准。
本次 CLI 的插件详情只有返回、卸载和元数据，`/apps` 也不可用；不要反复指导用户寻找不存在的设置按钮。
用 Render 工具的实际连接结果确认认证完成，缺少入口时查看当前插件 / CLI 官方说明。

## 首次 Docker 创建

本次 Render 创建工具虽在 runtime 枚举中列出 `docker`，但说明明确不支持完整 Docker 创建，
也没有 Dockerfile / context 参数。以实际工具能力为准，不把原生 Python 服务当作等价部署。
当工具仍有此限制时，使用已推送的 Blueprint；若以后可用 API 支持完整 Docker 配置，可在既有授权内直接创建。

当前 Blueprint 在仓库根目录，核心字段应对应现有 `render.yaml`：

| 字段 | 本项目配置 |
| --- | --- |
| `runtime` / `plan` | `docker` / `free` |
| `branch` | `agent-chat` |
| `dockerfilePath` | `./apps/agent-chat/Dockerfile` |
| `dockerContext` | `.`，使用主仓根目录 |
| `healthCheckPath` | `/api/health` |
| `autoDeployTrigger` | `"off"`，由交付流程决定何时发布 |
| `numInstances` | `1` |
| `CHAT_ACCESS_PASSWORD` | `sync: false`，在平台输入至少 12 字符的私人访问口令 |
| `CHAT_SECURE_COOKIE` / `PORT` | `"true"` / `"10000"` |

不要给 Free 服务设置 `maxShutdownDelaySeconds`，包括显式写默认值 `30`：本次平台明确拒绝该字段。
官方 JSON Schema 没拦住这个套餐限制；Schema 验证与平台语义验证应分别记录。
YAML 的 `off` 加引号，避免 YAML 1.1 解析器把它当成布尔值。

准备完代码、验证和推送后，再提供 [Blueprint 创建链接](https://dashboard.render.com/blueprint/new?repo=https://github.com/Delva0/gh-puller)：

1. 蓝图名称可以是 `agent-chat`，它是管理名称。
2. 分支明确选 `agent-chat`；主仓默认分支可能没有最新 Blueprint。
3. 蓝图路径留空或填 `render.yaml`。
4. 填写访问口令，核对 Free，再 Apply。

说明需要用户点 Apply 的实际原因是当前 Docker 创建接口能力不足，不能把它说成任意部署都需重复批准。
服务创建后用 Render 返回的 URL；不要根据服务名猜测 `onrender.com` 子域名。

## 镜像与依赖

构建、版本锁定和运行命令以 `apps/agent-chat/Dockerfile`、`uv.lock`、`web/package-lock.json`、`start.sh` 为准。
多阶段分别构建前端与 Python wheels，运行层由非 root 用户启动一个 Uvicorn worker。
本地使用相邻主包源码；生产安装不可编辑 wheel，源码身份由同一 Git 提交确定。

重要的缓存边界：

- 主包与应用包源码改变后，要保留 Dockerfile 中两项 `--reinstall-package`。
  uv 对本地目录的 wheel 缓存可能只根据构建元数据判定有效；一次重建曾沿用旧 `runtime.py`，
  镜像构建成功而修复未生效。主包 / 应用包强制重建，第三方包继续复用缓存。
- 核对的是镜像里安装后的文件。需要排除陈旧源码时，对照 Python 文件及声明的 package-data 哈希，
  不要求源码目录中的 README、基准数据等未打包文件出现在 wheel 中。
- 使用 Dockerfile 专属 `Dockerfile.dockerignore`。白名单放开父目录后需重新排除子项，
  否则可能意外把 `node_modules` 等重新纳入上下文。本次曾出现约 145 MB 的多余上下文。
- 镜像不带 `archive/`、`playground/`、`.env`、宿主机 `.venv`、测试替身或原始验收记录。
  容器验收不挂载这些宿主目录。
- 基础镜像按 digest 固定；现有运行镜像已经满足 Node 所需库，不要未经检查再跑 apt 安装。

SDK 下载慢的已知来源：`gh-puller[search] → gh-puller[agent] → openai-codex → openai-codex-cli-bin`。
Python SDK 依赖指定版本的二进制发行包，PATH 中已有 Codex CLI 不能满足包管理器的依赖。
本次下载约 132 MiB，Claude SDK 也较大；这些是构建依赖，不是 Render 部署脚本额外安装的开发工具。
查看当前进程、缓存和传输证据再报告进度，不虚构百分比、不反复重启下载，也不未经任务要求重构主仓 extras。
如缓存预热确有必要，只复用锁定版本的公共 wheel，不复制环境或凭据。

## Free 与凭据边界

本次 Free Web Service 的公开规格是 0.1 CPU / 512 MB。运行套餐看 `serviceDetails.plan`；
`buildPlan` 是另一个字段，不能仅凭它出现 `starter` 就断言运行实例已升级。
免费 Web 服务当前没有固定到期日，但会空闲休眠，有共享实例时长、流量和构建额度。
不要承诺永远免费或持续在线；绑定支付方式后的流量 / 构建超额可能收费，模型及付费搜索调用费用另计。

应用不自动读取 `.env`。Render 中只配置运行所需环境与网站口令，用户在页面提供模型 / 平台凭据。
本地验收读取用户明确提供的凭据文件并传给子进程内存；不打印整个环境、不把值放入 URL、命令行参数或报告。
不为方便验收擅自覆盖用户已经生成的网站口令。

参考：[Blueprint 规范](https://render.com/docs/blueprint-spec)、[Docker](https://render.com/docs/docker)、
[免费限制](https://render.com/docs/free)。涉及套餐、CLI 入口或 API 能力变化时重新核实。
