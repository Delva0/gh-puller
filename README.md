# gh-puller

> 将 GitHub Issue、Pull Request 与关联代码保存为可增量更新、可离线回放的本地事实档案。

[![Version 0.1.0](https://img.shields.io/badge/version-0.1.0-6f42c1?style=flat-square)][project-metadata]
[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-3776AB?style=flat-square&logo=python&logoColor=white)][project-metadata]
[![Platform POSIX](https://img.shields.io/badge/platform-POSIX-3E8E7E?style=flat-square)][github-package]
[![Archive SQLite + bare Git](https://img.shields.io/badge/archive-SQLite_%2B_bare_Git-003B57?style=flat-square&logo=sqlite&logoColor=white)][github-archive]

gh-puller 是一个面向代码仓库研究与知识系统的 monorepo。根包的主入口
`gh_puller.github` 负责 GitHub 归档；DeepWiki、Agent 观测、代码图谱 MCP 和评测框架是按需选择的独立入口。

```text
GitHub REST / GraphQL --+
                        +--> gh_puller.github --+--> SQLite observations
Git branches / PR refs -+                       +--> bare Git objects
                                                        |
                                           offline replay / mining / diff
```

归档保存支持的源操作真实观测到的事实，而不是抓取网页或推断不可观测状态。静默删除、未产生增量信号的子资源变化，以及 GitHub 未开放的数据不在完整性承诺内。

## 归档一个仓库

需要 POSIX 环境、Python 3.13、[uv] 和 Git；systemd 服务仅适用于 Linux。准备一个能够读取目标仓库的 GitHub token，并在仓库根目录的 `.env` 中写入：

```dotenv
GH_TOKEN=github_pat_your_token
```

`GH_TOKEN` 优先于 `GITHUB_TOKEN`；`.env` 已被 Git 忽略。执行一次归档：

```bash
uv run -m gh_puller.github once OWNER/REPO archives/repository.sqlite3
```

将 `OWNER/REPO` 替换为目标仓库。首次运行会完整遍历可见的 Issue 和 PR；大型仓库可能消耗较多时间与 GitHub API 配额。

进度写入 stderr。成功时 stdout 只输出一行 JSON；以下代表性输出来自 [CLI 契约测试][github-cli-test]：

```json
{"checkpoint_from": null, "completed_at": "2026-09-05T10:07:00Z", "cycle_id": 1, "discovered_items": 3, "requests": 5, "started_at": "2026-09-05T10:07:00Z"}
```

运行结果落在两个互相绑定的本地存储中：

- `archives/repository.sqlite3` 保存细粒度事实观测、历史与可恢复任务。
- `archives/repository.sqlite3.git` 保存上游 Git 对象与选中 PR 的稳定历史引用。

## 归档语义

- **增量但不臆测**：冷启动遍历完整 Issue/PR 目录；后续读取 GitHub 提供的父对象与评论变化信号。
- **事实闭合即可读**：每个 API 或 Git 语义集合按真实读取窗口追加发布，无需等待整个同步 cycle 结束。
- **失败可恢复**：目录页、任务和发布键持久化；重启继续未完成工作，不重拉已闭合事实。
- **讨论与代码同行**：SQLite 保留源 JSON 和独立观测时间，bare Git 固定 PR 的 base、head 与比较基准。
- **离线读取**：下游可使用 `iter_observations`、`iter_current_facts`、`iter_facts_as_of`、普通 SQL 和标准 Git 命令。

观测时间、完整性边界、存储格式、定时调度、systemd 服务与 PR diff 见 [GitHub 归档设计与运维文档][github-archive]。

## 代码仓库图化

`gh_puller.codebase` 把 Git 的 root-first topo commit 序列构建成一个可恢复、可随机读取的
Merkle 代码图归档。运行时解析一次经过实验门禁晋升的 CBM 二进制，并把其 SHA-256 固定在
每个新增 commit 中；运行中的构建不会跟随 `accepted.json` 更新。

```bash
uv run -m gh_puller.codebase build \
  --repo /path/to/repository \
  --build-dir archives/repository-codebase \
  --max-commits 10000
```

最终交付物是 `archive.kga` 和 `summary.json`。同一命令提高 `--max-commits` 即可续跑；
`--out-dir` 可复制已有归档后从副本继续。CBM 构建与查询边界、Merkle 历史压缩、二进制
解析、增量配置和读取 API 见[代码图文档][codebase-graph]。

## 选择入口

仓库内的应用按用途独立运行，不要求同时启动：

| 想完成的任务 | 从这里开始 |
| --- | --- |
| 持续归档 GitHub Issue、PR 与代码对象 | [`gh_puller.github`][github-package]：根 CLI 与离线读取 API；详见[归档文档][github-archive] |
| 构建可恢复、可随机读取的逐 commit 代码图 | [`gh_puller.codebase`][codebase-package]：CBM 图化与 Merkle 历史压缩；详见[代码图文档][codebase-graph] |
| 生成代码 Wiki、Code Map 并进行问答 | [DeepWiki WebUI]：FastAPI + Next.js 的 DeepWiki 兼容应用 |
| 记录并查看 Agent Context、模型与工具活动 | [Agent monitor]：JSONL / WebSocket 查看器；语义契约见[事件模型][agent-events] |
| 通过 MCP 暴露代码图谱工具 | [gh-puller MCP]：对 `codebase-memory-mcp` CLI 的 MCP 服务封装 |
| 按版本向 vllm-kb 提供代码图谱 | [vllm-kb adapter]：校验快照并路由到内部 MCP 索引 |
| 评测兼容 `POST /ask` 的问答服务 | [benchmark]：一套题库对一个 endpoint 的 REST 评测框架 |

## 开发

根 Python 包使用 `uv.lock`；各 Python 应用拥有独立锁文件，Web 应用与共享
`@gh-puller/ui` 使用根 `pnpm-lock.yaml`。进入子项目时，请采用其 README 中的
`uv --directory ...` 或 `pnpm --dir ...` 命令。

发布包默认只安装 GitHub 归档依赖；Agent、评测和代码图入口分别对应 `agent`、
`benchmark`、`codebase` extras。仓库的默认 `dev` 组会安装这三组依赖；本地 DSH SDK
仍通过 `uv sync --group dsh` 单独启用。

```bash
uv sync --frozen
pnpm test
uvx ruff check
```

分支与提交约定见[贡献指南]。

[uv]: https://docs.astral.sh/uv/
[project-metadata]: pyproject.toml
[github-package]: gh_puller/github/
[github-archive]: docs/github-puller.md
[codebase-package]: gh_puller/codebase/
[codebase-graph]: docs/codebase-graph.md
[github-cli-test]: tests/github/test_cli.py
[deepwiki webui]: apps/deepwiki-webui/README.md
[agent monitor]: apps/agent-monitor/README.md
[agent-events]: docs/agent-monitor.md
[gh-puller mcp]: apps/gh-puller-mcp/README.md
[vllm-kb adapter]: apps/vllm-kb-adapter/README.md
[benchmark]: gh_puller/benchmark/README.md
[贡献指南]: CONTRIBUTING.md
