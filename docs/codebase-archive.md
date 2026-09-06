# 代码图历史归档

`gh_puller.codebase` 将一个 Git 仓库按 root-first topo 顺序归档。每个 commit 保存节点树和
边树的 Merkle root；未变化 page 跨 commit 复用，因此可以在不保留逐 commit SQLite 或
完整图快照的前提下随机读取任意已完成 commit。

```text
Git commit tree
  -> persistent CBM process
  -> native change journal / exact generation diff
  -> node and edge Merkle updates
  -> durable commit checkpoint
  -> archive.kga + summary.json
```

## CBM 二进制

构建只使用启动时解析到的一个二进制身份。解析顺序如下；前一项存在时不会查看后一项：

| 优先级 | 来源 |
| ---: | --- |
| 1 | API/CLI 显式 `binary` / `--binary` |
| 2 | API/CLI 显式 manifest / `--cbm-manifest` |
| 3 | `GH_PULLER_CODEBASE_CBM_BINARY` |
| 4 | `GH_PULLER_CODEBASE_CBM_MANIFEST` |
| 5 | `${XDG_DATA_HOME:-~/.local/share}/gh-puller/cbm/accepted.json` |
| 6 | `PATH` 中的 `codebase-memory-mcp` |

实验仓把通过 correctness、漂移、性能和 sanitizer 门禁的候选复制到
`objects/<sha256>/codebase-memory-mcp`，随后原子替换 `accepted.json`。生产构建不读取
playground、CBM 源码仓或浮动分支。它会重新计算 object digest、执行 `--version`，并从
MCP `tools/list` 验证所需的 force-full、granular-delta 和 persistent transport 能力。

每个 commit manifest 记录 `cbm_binary_sha256`，最终 metadata 和 `summary.json` 记录完整
provenance。使用不同二进制续跑默认失败；确认接受新的语义边界时显式增加
`--allow-cbm-upgrade`。没有二进制记录的早期归档同样需要在第一次续跑时增加该参数，后续
续跑便重新受 digest 门禁保护。

## 构建与续跑

构建前 10,000 个 topo commit：

```bash
uv run -m gh_puller.codebase build \
  --repo /path/to/repository \
  --build-dir archives/repository-codebase \
  --max-commits 10000
```

提高上限即可原地续跑：

```bash
uv run -m gh_puller.codebase build \
  --repo /path/to/repository \
  --build-dir archives/repository-codebase \
  --max-commits 10100
```

保留原归档并从副本继续：

```bash
uv run -m gh_puller.codebase build \
  --repo /path/to/repository \
  --build-dir archives/repository-topo-200 \
  --out-dir archives/repository-topo-300 \
  --max-commits 300
```

默认 transport 是单个长期存活的 MCP 进程。完整参数可由
`uv run -m gh_puller.codebase build --help` 查看。`--force-full` 强制 CBM 每个 commit 走
完整 pipeline；其余 `--delta-*` 参数分别控制 closure、dependent scope、新 surface、
reference fanout 和 pair/vector 输出，不在 Python 层隐式组合成 policy。

## Python API

读取归档不需要运行 CBM：

```python
from gh_puller.codebase import Archive

archive = Archive("archives/repository-codebase/archive.kga")
rows = archive.load_rows(archive.latest_commit)
snapshot = archive.load_raw(archive.latest_commit)
graph = archive.load(archive.latest_commit)
```

`load_rows()` 和 `load_raw()` 保留并行边及完整属性。`load()` 是 NetworkX `DiGraph` 聚合
视图，同一 `(source, target)` 的并行边位于 `edge_keys` 和 `data` 列表中。

也可以用显式配置调用构建 API：

```python
from gh_puller.codebase import BuildOptions, IncrementalConfig, build_archive

build_archive(
    BuildOptions(
        repo="/path/to/repository",
        build_dir="archives/repository-codebase",
        max_commits=10000,
        incremental=IncrementalConfig(),
    )
)
```

成功后，`Archive.verify_index()` 以索引和逻辑 root 验证正常读取路径；离线完整审计可调用
`Archive.verify()`，它会读取、解压并 hash 整个文件。
