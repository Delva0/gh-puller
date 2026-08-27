## Agent 代码行为规则
- **语音输入容错**：用户通过语音转文本输入。忽略字面意义上的拼写错误、同音词或填充词。理解整体意图和上下文。
- **交互式协调（编写前讨论）**：收到任务后，不要立即编写代码或实施更改。首先，简要说明您对需求的理解，并提出您的计划方案。在开始实施之前，请等待用户确认或反馈。
- **通过 UV 执行 Python 代码**：始终使用 `uv run` 执行 Python 脚本或命令。请勿使用标准的 `python` 或 `python3` 命令。
- **代码风格与一致性**：采用简洁的研究型或竞赛型编程风格。编写零冗余代码，并尽可能减少防御性编程。所有注释均使用中文。最重要的是，与本项目中所有现有核心代码保持严格的风格一致性。

## 本代码仓
- **不允许查看范围**：archive/
- **apps/ 项目结构**：`apps/<name>/server/`(Python 后端,独立 uv 项目,path 依赖根 `gh-puller` 包)+ `apps/<name>/web/`(前端,根 pnpm 工作区成员)聚合为一个应用,一个应用一个家。命名用应用名本体(如 `deepwiki-webui`、`agent-monitor`)。
- **`ui/` 为本项目共享基础 UI 组件包 `@gh-puller/ui`**:`ui/` 下自持 package.json(exports 直指 `src/index.ts`,无构建,workspace 成员),apps 经 `workspace:*` 依赖直接 import 源码。
- **文档命令写法**：文档中的命令一律用 `--dir`/`--directory` 定位子项目(如 `uv --directory apps/agent-monitor/server run ...`、`pnpm --dir apps/agent-monitor/web dev`),禁止 `cd xxx && xxx`。
