# gh-puller MCP

gh-puller 的 MCP 服务器,提供代码库知识图谱工具桌:15 个工具、`explore_codebase`/`review_change_impact` 提示词、analysis/scout 工具面档位。工具面固定为 `codebase-memory-mcp` v0.10.8 的 C 服务器版本;可选的 `tests/e2e/test_manifest_source.py` 会从显式指定的该版本 C 源码重新提取并校验字节一致性。实现按工具逐文件:`gh_puller_mcp/tools/<tool>.py` 捆绑该工具的逐字面数据(`TOOL = ToolDef(...)`)+ 行为(`@register` 函数,缺省 `passthrough`),`manifest.py` 只留协议常量/提示词/指令并聚合成 `TOOLS`/`TOOL_ANNOTATIONS`。扩展或定制某个工具只改那一个文件(只改行为,schema 受守门保护)。

后端是一个随本服务存活的原生 MCP frontend:

```
MCP client ──stdio/HTTP──> gh-puller-mcp ──persistent stdio MCP──> codebase-memory-mcp ──IPC──> shared daemon
```

服务只启动并初始化一次 `codebase-memory-mcp`，随后在同一条 newline-framed MCP 流上发送 `tools/call`，原样转发原生 `CallToolResult` 信封(content / structuredContent / isError)。这个 frontend 会复用或启动 CBM 的共享 daemon，并以持续会话阻止 session-managed daemon 在相邻请求之间退出；服务关闭时 EOF 释放会话。CBM 自带的 `cli` 是 one-shot daemon client：每个进程都会连接 daemon，无其他会话时还会启动一个临时 daemon，完成后立即断开，因此不用于服务热路径。

面向调用方的 wire/protocol 机制(stdio framing、JSON-RPC、handshake、notifications、unknown-method 错误)来自官方 **`mcp` SDK**(PyPI `mcp` 2.x,`uv add mcp`);本包只保留 codebase-memory-mcp 特有语义:逐字工具面 / 档位 / 分页规则、信封规则、提示词模板和持久后端桥接。协议面以 **codebase-memory-mcp v0.10.8** 的 `src/mcp/mcp.c` 为 ground truth;不宣称跟随当前 HEAD。

## Run

```bash
uv --directory apps/gh-puller-mcp run python -m gh_puller_mcp [--tool-profile analysis|scout] [--binary PATH] [--debug] [--timeout SEC]
# 跨机暴露:加 --http [--host HOST] [--port PORT] [--path PATH](见下节)
```

* 默认档位 `all` 暴露 15 个工具;`analysis`(11)/`scout`(7)收紧工具面并切换 `initialize` 指令,与 C 服务器的 `--tool-profile` 完全一致。
* `--binary`(或 env `GH_PULLER_MCP_BINARY`)覆盖二进制;解析序:flag → env → `shutil.which("codebase-memory-mcp")` → `~/.local/bin/codebase-memory-mcp`。
* 环境继承(`CBM_CACHE_DIR` 决定索引根，`CBM_RUNTIME_DIR` 决定 daemon rendezvous；相同账户、build、cache 和 runtime 的 CBM 会话共享 daemon)。
* 干净 EOF / framing 停止退出码 0(对应 C 服务器);bad flags 退出码 2。
* stdio 模式在第一次合法工具调用时启动后端，随后复用至 EOF；HTTP 模式在监听端口前完成后端初始化，因此请求不承担 CBM 进程、build fingerprint 或 daemon 冷启动成本。
* 工具请求不自动重试；超时或后端退出会结束当前 frontend，下一次调用可建立新会话。
* `--http` 切到 Streamable HTTP 传输(stdio 仍是缺省;`--tool-profile` 等旗标组合照常生效),语义见下节。

## HTTP 传输(Streamable HTTP,跨机暴露)

```bash
uv --directory apps/gh-puller-mcp run python -m gh_puller_mcp --http --host 0.0.0.0 --port 8787 --path /gh-puller/graph
```

* 形态是**单端点 MCP JSON-RPC**(`tools/list`、`tools/call`、`prompts/*`…),不是每工具一个 URL;`json_response`(每次 POST 回纯 JSON,无 SSE 流)与 `stateless_http`(无会话、免 `initialize` 握手,每个 POST 独立)由实现定死,不暴露开关。
* HTTP 请求无调用方会话，但后端有服务级持续会话。工具调用在线程中等待后端，uvicorn event loop 仍可响应 `ping`、`tools/list` 和其他连接；一个原生 frontend 按序执行工具调用。
* 任意 HTTP 客户端可直接 POST;MCP 客户端(streamable http)连同一端点:

```bash
curl -s localhost:8787/gh-puller/graph -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_projects","arguments":{"limit":1}}}'
```

* host 语义:`--host` 同时决定 uvicorn 绑定与传输层防护——`127.0.0.1`/`localhost`/`[::1]` 绑定自动启用 DNS rebinding 防护(Host 头须落在白名单且**带端口**,如 `localhost:8787`;跨机 Host 得 421),其余绑定(如 `0.0.0.0`)不启用防护,跨机直接可达。
* 服务机要求与 stdio 模式相同:cbm 二进制解析序与 `CBM_CACHE_DIR`/`CBM_RUNTIME_DIR` 继承不变,无新增 env。
* 退出语义:SIGINT/SIGTERM 走优雅关停,但 uvicorn ≥0.52 关停后会重发收到的信号,进程按该信号状态退出(143/130),不是 0。

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
* 持久后端是普通的 C frontend 会话，因此沿用 CBM daemon 的 session context、auto-watch 和后台任务语义；本服务不提供 C UI / daemon 控制面(`--port` 9749 UI 不在范围内，`--http` 暴露的是 Streamable HTTP MCP)。外层 SDK 的 `notifications/cancelled` 仍是 no-op。
* 后端启动、传输或超时失败时，服务器合成 `"backend error: …"` 信封；不会重放可能已经执行的工具请求。

## Tests

```bash
uv --directory apps/gh-puller-mcp run pytest -q
GH_PULLER_MCP_ORACLE_BINARY=/path/to/codebase-memory-mcp \
GH_PULLER_MCP_C_SOURCE=/path/to/mcp.c \
uv --directory apps/gh-puller-mcp run pytest -q -m e2e
```

The default command is deterministic and excludes process-level E2E tests. The E2E command compares the Python server
with the configured oracle binary and re-extracts the tool table from the explicitly pinned C source.

## Manual smoke

```bash
uv --directory apps/gh-puller-mcp run python - <<'PY'
import json
import os
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory() as cache_dir, tempfile.TemporaryDirectory() as runtime_dir:
    env = os.environ.copy()
    env["CBM_CACHE_DIR"] = cache_dir
    env["CBM_RUNTIME_DIR"] = runtime_dir
    process = subprocess.Popen(
        [sys.executable, "-m", "gh_puller_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    def request(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        return json.loads(process.stdout.readline())

    print(request({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "readme-smoke", "version": "1"},
        },
    }))
    process.stdin.write(json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }) + "\n")
    process.stdin.flush()
    response = request({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "list_projects", "arguments": {"limit": 1}},
    })
    assert response["result"].get("isError") is not True
    print(response)
    process.stdin.close()
    raise SystemExit(process.wait(timeout=15))
PY
```
