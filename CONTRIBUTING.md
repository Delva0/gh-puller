# 贡献指南

## 目录结构

| 路径 | 说明 |
|---|---|
| `gh_puller/protocol.md` / `protocol.py` / `types.py` | 协议契约（唯一权威）：文档 + 常量 + 类型，调用方与服务方共引 |
| `gh_puller/benchmark/` | 评测框架（独立发展） |
| `gh_puller/methods/` | 内置方法（独立发展，与 benchmark 互不引用） |

`benchmark` 与 `methods` 独立发展，仅通过协议契约互操作；**协议改动影响两侧，必须单独提交**。

## 分支流程

- `master`：唯一长期分支（稳定基线）
- 每项工作一个短生命周期分支，按主题命名：`feat/benchmark-*`、`feat/methods-*`、`feat/protocol-*`、`fix/*`、`docs/*`
- 完成即合并回 `master` 并删除分支；**一个分支不混两个子包的改动**

## 提交规范

格式：`<类型>: <中文描述>`

| 类型 | 适用 |
|---|---|
| `protocol` | 顶层协议契约（`protocol.md` / `protocol.py` / `types.py`） |
| `benchmark` | 评测框架 |
| `methods` | 内置方法 |
| `docs` | 文档（README / CONTRIBUTING 等） |
| `chore` | 杂项（构建、配置） |

原子性：一个提交只动一个子包；协议改动独立提交。
