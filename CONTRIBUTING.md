# 贡献指南

## 分支流程

- `master`：唯一长期分支（稳定基线）
- 每项工作一个短生命周期分支，按主题命名：`feat/benchmark-*`、`feat/methods-*`、`feat/protocol-*`、`fix/*`、`docs/*`
- 完成即合并回 `master` 并删除分支；**一个分支不混两个子包的改动**

## 提交规范

格式：`<类型>: <中文描述>`

| 类型 | 适用 |
|---|---|
| `benchmark` | 评测框架 |
| `docs` | 文档（README / CONTRIBUTING 等） |
| `chore` | 杂项（构建、配置） |

原子性：一个提交只动一个子包；协议改动独立提交。
