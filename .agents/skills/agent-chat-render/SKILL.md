---
name: agent-chat-render
description: 部署、持续交付和运维本仓 Agent Chat 的 Render 免费 Docker 服务。用于首次上线、发布更新、核对公网版本、真实浏览器验收及排查构建、日志、资源和 agent 工具错误。
---

# Agent Chat on Render

以主仓 `apps/agent-chat/`、根目录 `render.yaml` 和实际 Render 服务状态为依据。
这份技能记录本项目的操作约定与已验证经验，不替代当前用户指令或 Render 的最新套餐规则。

## 项目定位

| 项目 | 已确认信息 |
| --- | --- |
| 主仓 | `Delva0/gh-puller`；本机 `/home/delva/projects/gh-puller` |
| 应用 | `apps/agent-chat/`；React / Vite + FastAPI，同源 Docker 服务 |
| 部署分支 | `agent-chat`；日常主仓分支与部署分支可以不同 |
| Render 工作区 | 用户已确认 **My Workspace**，`tea-dasguj0473hc7388hebg` |
| Render 服务 | `agent-chat`，`srv-dasjnn8473hc738kgnk0` |
| 公网地址 | https://agent-chat-pdl4.onrender.com |
| 控制台 | https://dashboard.render.com/web/srv-dasjnn8473hc738kgnk0 |
| 既定托管配置 | Singapore，Free，单实例，自动部署关闭；操作前核对实际状态 |

工作区确认可在当前任务及其后续操作中复用，调用 Render 工具时显式传 `workspaceId`。
`get_selected_workspace` 不反映请求参数里的工作区，返回空不代表用户的授权失效。
账号或目标发生变化、历史确认不适用时，再让用户选择工作区；不要仅凭列表只有一项自行选定。
部署表单不一定显示工作区选择框，聊天中的插件确认与该表单是两件事。

## 按任务读取

- 首次创建服务、修改 Blueprint、处理镜像或依赖构建：读 [部署](references/deployment.md)。
- 修改代码、推送、发布、回滚、证明线上代码身份：读 [持续交付](references/delivery.md)。
- 查询地址、健康状态、日志、指标、处理会话或工具错误：读 [运维](references/operations.md)。

保留用户已授权的工作范围：部署请求包括准备、验证、提交、推送和发布；单纯查询状态不触发发布或重启。
本项目要求免费托管；不因限额、日志故障或验收困难自动购买服务或升级套餐。

## 共用约定

- 在主仓验证和提交应用；`playground/graphub` 是独立实验仓，其提交不会自动进入部署。
- Python 通过 `uv` 执行。应用检查使用 `apps/agent-chat` 的锁文件和虚拟环境。
- 单个后端进程持有会话、密钥和工具上下文；多 worker / 多实例不是可直接打开的性能选项。
- `CHAT_ACCESS_PASSWORD` 是网站口令。模型、GitHub / GitCode 和搜索凭据在页面与活跃后端内存中使用。
  `.env`、浏览器凭据、原始模型流、实验归档和宿主机虚拟环境均不进入镜像或 Git。
- 新建 GitHub / GitCode 会话默认 REST；具体验收仍核对事件里的实际 backend、模型参数和源码版本。
  首次提问后工具配置固定；发布或重启后失去服务端上下文的历史只能查看。
- JSON Schema 通过、镜像能启动、页面可访问、真实 agent 查询完成是不同证据；分别记录结果。

## 可执行入口

从主仓根目录执行只读健康检查；此脚本仅使用标准库，不为检查网址同步模型 SDK：

```bash
uv run --no-project python .agents/skills/agent-chat-render/scripts/check_deployment.py \
  --url https://agent-chat-pdl4.onrender.com --revision EXPECTED_FULL_COMMIT_SHA
```

`--revision` 可省略以只查看健康状态。提供时必须与线上完整 SHA 一致；文档提交可能领先于部署版本。

真实浏览器验收使用 [run_live_browser.py](scripts/run_live_browser.py) 包装应用现有脚本，
详细用法、凭据来源和费用边界见 [持续交付](references/delivery.md#真实公网验收)。

修改技能时运行 `skill-creator` 的 `quick_validate.py`，并实测有变化的辅助脚本。
验收记录维护在主仓 `apps/agent-chat/VERIFICATION.md`；不要把该技能写成不断追加的逐次运行日志。
