# gh-puller MCP

gh-puller 的 MCP 服务器,提供代码库知识图谱工具桌:15 个工具、`explore_codebase`/`review_change_impact` 提示词、analysis/scout 工具面档位。工具面与 `codebase-memory-mcp` v0.10.8 的 C 服务器 1:1(面随官方版本锁定;`tests/test_manifest.py` 对 C 源码有字节级再提取守门);实现按工具逐文件:`gh_puller_mcp/tools/<tool>.py` 捆绑该工具的逐字面数据(`TOOL = ToolDef(...)`)+ 行为(`@register` 函数,缺省 `passthrough`),`manifest.py` 只留协议常量/提示词/指令并聚合成 `TOOLS`/`TOOL_ANNOTATIONS`。扩展或定制某个工具只改那一个文件(只改行为,schema 受守门保护)。

后端机制:每个工具调用作为子进程透传给客户端二进制:

```
codebase-memory-mcp cli --json <tool>          # args on stdin as JSON
```

`--json` 让 CLI 把原始 MCP `CallToolResult` 信封(content / structuredContent / isError)打印到 stdout;信封 `isError` 时退出码为 1。stdout 信封原样透传。wire/protocol 机制(stdio framing、JSON-RPC、handshake、notifications、unknown-method 错误)来自官方 **`mcp` SDK**(PyPI `mcp` 2.x,`uv add mcp`);本包只保留 codebase-memory-mcp 特有语义:逐字工具面 / 档位 / 分页规则、信封规则、提示词模板和 `cli` 透传。协议面拷贝自 **codebase-memory-mcp v0.10.8**(面级已验证 `v0.10.8 == HEAD`;C 源码 `src/mcp/mcp.c` 是 ground truth)。

## Run

```bash
uv --directory apps/gh-puller-mcp run python -m gh_puller_mcp [--tool-profile analysis|scout] [--binary PATH] [--debug] [--timeout SEC]
```

* 默认档位 `all` 暴露 15 个工具;`analysis`(11)/`scout`(7)收紧工具面并切换 `initialize` 指令,与 C 服务器的 `--tool-profile` 完全一致。
* `--binary`(或 env `GH_PULLER_MCP_BINARY`)覆盖二进制;解析序:flag → env → `shutil.which("codebase-memory-mcp")` → `~/.local/bin/codebase-memory-mcp`。
* 环境继承(`CBM_CACHE_DIR`、`CBM_RUNTIME_DIR` 决定缓存根与 CLI 所附着的守护进程)。
* 干净 EOF / framing 停止退出码 0(对应 C 服务器);bad flags 退出码 2。
* 每次工具调用花费约 1.9 s(C 二进制自身启动)加上它的结果;无缓存、无重试。

## mcp surface (1:1 with the C server)

| method | behavior |
|---|---|
| `initialize` | protocol negotiation `["2025-11-25","2025-06-18","2025-03-26","2024-11-05"]`; `serverInfo{name:"codebase-memory-mcp"}`; `capabilities{tools,prompts}` (+ SDK-level keys below); profile `instructions` |
| `ping` | `{}` |
| `tools/list` | **no `cursor` key → the full profile list, no pagination**; with `cursor` → page of 8 offset by the cursor (invalid/too-large cursor → empty page) |
| `tools/call` | envelope passthrough; `trace_call_path` is a legacy alias for `trace_path`; unknown/profile-blocked names are *envelope* errors (`isError`), never JSON-RPC errors |
| `prompts/list` / `prompts/get` | `explore_codebase` / `review_change_impact` with verbatim templates (incl. the `title` fields, non-standard but present in the C server); argument problems are JSON-RPC `-32602` errors |
| unknown method | `-32601 "Method not found"` (SDK adds `data: method`) |

No tool declares `outputSchema` (deliberate: the C server omits it to keep `structuredContent` optional — see mcp.c comment).

## Verified divergences from the C server (SDK-driven, documented)

* **Wire key order differs**: the SDK serializes JSON keys in its own (alphabetical) order; content of `content[0].text` remains byte-identical.
* `initialize` capabilities carry the SDK's `experimental` key (and would advertise `resources`/`logging`/`completions` if their handlers were registered).
* `tools/call` with a *missing* `name` is rejected by the SDK with `-32602 Invalid request parameters` (the C server returned an `isError` envelope "missing tool name").
* `resources/list` / `resources/templates/list` are not served (-32601; the C server returned empty arrays) and not advertised.
* No background auto-index / watcher registration on `initialize` (a C-only side effect invisible in any response); no HTTP UI / daemon mode (`--port` 9749 UI out of scope); the SDK stdio loop is serial too and `notifications/cancelled` is a no-op.
* On a subprocess failure the server synthesizes an envelope with `"backend error: …"` (the C server never fails this way locally).

## Tests

```bash
uv --directory apps/gh-puller-mcp run pytest -q   # unit + wire e2e + oracle parity vs the real binary
```

`tests/test_manifest.py` also re-extracts the tool table from the C source (when the checkout exists) and asserts byte equality against the baked-in manifest.

## Manual smoke

```bash
printf '%s' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n' \
  | uv --directory apps/gh-puller-mcp run python -m gh_puller_mcp --debug
# then list_projects(limit=1); its content[0].text must be byte-identical to:
echo '{"limit":1}' | codebase-memory-mcp cli --json list_projects   # (content[0].text)
```
