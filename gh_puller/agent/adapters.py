"""LLM 调用适配器:SDK/HTTP 对象 → 事件溯源事件 dict 的归一化与调用包装。

外部只经本层模块级函数调用(经 gh_puller.agent __init__ 再导出),不再直接触碰
ClaudeSDKClient / httpx(无感,对外语义不变):
- cc_stream / cc_text / cc_result:Claude Code(SDK)调用。文本增量 StreamEvent
  `text_delta` 优先、AssistantMessage 兜底(仅在未产出任何增量时)、
  ResultMessage.is_error → RuntimeError("agent 执行失败: ...") —— 与 deepwiki
  原 `_agent_stream` 漏斗逐字节一致;thinking/工具增量只进监控事件流,不改变产出。
- llm_complete / llm_stream:OpenAI 兼容端点(httpx);异常原样抛,重试留给调用方。
- dsh_stream / dsh_text / dsh_result:DeepSeek Harness(SDK)调用。dsh 原生 session
  事件 1:1 投影为监控事件流(事件模型同源,events.py);finish_reason 非 completed
  → RuntimeError("agent 执行失败: ...")(与 cc is_error 语义对齐)。
- codex_stream / codex_text / codex_result:OpenAI Codex(SDK)调用。codex 通知流
  (item/agentMessage/delta 文本增量、item/completed 整块兜底)合成 TAXONOMY ——
  与 cc 同为"唯一权威"合成路线(codex 通知无 seq/生命周期编号,见 _CodexSynth);
  turn 非 completed → RuntimeError("agent 执行失败: ...")。

事件语义(对齐 deepseek-harness 事件溯源模型,规范见 gh_puller.agent.events):
单次运行一个 session(流式事件流内 seq 从 0 连续);进入即 session/start →
(context:* 说明事件)→ turn/start → step/start → user/message →
request/header(cc 路径 partial=true:SDK 不暴露请求体,system/tools 只能取调用方
options)→ 逐次 assistant/chunk → assistant/message(+ 工具则 tool/call /
tool/result)→ ... → step[end] → session/end。上下文每时每刻可恢复:折叠 surface
前缀(见 events.py 模块规范),任意请求平面 X = 该 step 首条 assistant/chunk 的 seq。

会话 id 默认 <ns>/<uuid4>(ns 归上层业务定:显式 session_ns → run_id →
session_name → "agent"),见 _session_id;文件侧只落非流式事件流
(NON_STREAM_TYPES,逐行跳过 assistant/chunk → 文件 seq 有洞,契约见 events.py)。

管线:适配器归一化 SDK/HTTP 对象 → 事件 dict → EventBus 扇出(sinks.EventBus,
publish 仅 put_nowait 到每 sink 的 asyncio.Queue,永不阻塞调用)→ sink worker 消费。

架构(单文件分层):
公共层(模块级 helper,为语义单测入口):_Run / _session_id / _normalize_usage /
_header/消息归一化等 → 共享基类 BaseAdapter(_run 装配、_guard 事件守卫、
text/result 缺省实现)→ 每路一个子类只写差异驱动循环(ClaudeCodeAdapter /
OpenAIAdapter / DshAdapter)→ 底部单例绑定(cc_stream = _claude.stream 等,签名
逐参数等于现状)+ ADAPTERS 元数据注册表。扩展第 N 种 agent:新建 XhAdapter(
BaseAdapter)(provider 类属性;stream 必写,text/result 用缺省或覆盖);SDK 库在
方法体内 lazy import(供测试 sys.modules 注入);事件只经 run.* 发(new_event 对
未知 type 抛 ValueError → 只发 TAXONOMY);若新 SDK 自带 seq/turn/step 生命周期
(如 dsh)→ 参照 _DshProj 做投影对齐,严禁把对方 seq 当 gh seq;尾部注册进 ADAPTERS
并加导出。
"""

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from .events import TAXONOMY, new_event
from .sinks import ensure_bus

# ---------------------------------------------------------------------------
# 事件发布器(适配器共用):信封/turn/step/seq
# ---------------------------------------------------------------------------


def _session_id(session: str | None, session_ns: str | None, run_id: str | None,
                session_name: str | None) -> str:
    """会话 id:显式 session 原样;否则 <ns>/<uuid4>(ns 由上层业务决定分类命名空间)。

    ns 解析序:显式 session_ns 参数 → run_id → session_name → "agent";
    会话 id 形如 judge:llm/0460e1e9-5155-4014-9054-a39986462b20 —— grep
    session/start 的 session 字段即知来源;文件名只取 "/" 后段(见 FileSink)。
    """
    if session:
        return session
    ns = session_ns or run_id or session_name or "agent"
    return f"{ns}/{uuid.uuid4()}"


class _Run:
    """单次运行的事件发布器:维护会话信封/turn/step/seq 计数,归一化后广播事件。"""

    def __init__(self, session: str, provider: str, model: str, *, label: str | None = None,
                 run_id: str | None = None, meta=None):
        self.session = session
        self.label = label or session
        self.provider = provider
        self.model = model
        self.run_id = run_id
        self.meta = meta
        self.seq = 0
        self.turn = 1  # 每 run 一个 dsh-style turn:单一用户消息 → 最终回答
        self.step = 1  # 一次 LLM 请求 = 一个 step;工具结果后的新请求 +1
        self.text_chars = 0
        self.t0 = time.monotonic()  # run 起点(与 start() 方法分名)
        self.tool_names: dict[str, str] = {}  # tool_use_id → 工具名(tool/result 归一化用)
        self._tool_pending = False  # 本轮工具结果已发 → 下个 assistant 消息段开新 step
        self._active_tool_use: dict[int, str] = {}  # 块 index → tool_use_id
        self._tool_use_pieces: dict[int, list[str]] = {}  # 块 index → input_json_delta 碎片
        self._tool_result: dict | None = None
        self._chunk_seqs: list[int] = []  # 本 step 的 assistant/chunk seq(消息 sourceSeqs)
        self._call_seqs: dict[str, int] = {}  # callId → tool/call 的 seq
        self._step_open = False
        self._ended = False
        self._reason: str | None = None  # error 事件后供 session/end.reason 使用
        self.result_usage: dict | None = None
        self.result_stop_reason: str | None = None
        self.result_cost_usd: float | None = None

    def event(self, evt_type: str, **data) -> dict | None:
        """造信封并发布;返回事件(seq 已分配);无 sink 时返回 None(零开销短路)。"""
        bus = ensure_bus()
        if not bus.enabled:
            return None
        evt = new_event(evt_type, **data)
        evt["session"] = self.session
        evt["run_id"] = self.run_id
        evt["label"] = self.label
        evt["provider"] = self.provider
        evt["model"] = self.model
        evt["seq"] = self.seq
        self.seq += 1
        bus.publish(evt)
        return evt

    def start(self, *, context: list[dict] | None = None, retry: dict | None = None,
              prologue: bool = True) -> None:
        """运行进入:session/start(带 retry 元数据)→ context 说明事件 → turn/start → step/start。

        prologue=False(dsh 投影路径专用):turn/step 生命周期由 dsh 原生事件自带,
        不合成 turn/start + step/start,且 _step_open 保持 False(dsh 路径不调 step_boundary)。
        """
        self.event("session/start", run_id=self.run_id, label=self.label, provider=self.provider,
                   model=self.model, retry=retry, meta=self.meta)
        for ctx in context or []:
            self.event(ctx["type"], **ctx["data"])  # 日志型说明事件:重放于 turn 之前
        if prologue:
            self.event("turn/start", turn=self.turn)
            self.event("step/start", turn=self.turn, step=self.step)
            self._step_open = True

    def step_boundary(self) -> None:
        """上一步完成、新一步开始(工具结果后新一轮 LLM 请求);本 step 增量清空。"""
        if self._stepping():
            self.event("step/end", turn=self.turn, step=self.step)
        self.step += 1
        self._chunk_seqs = []
        self._step_open = True
        self.event("step/start", turn=self.turn, step=self.step)

    def user_message(self, message: dict, *, source: dict | None = None,
                     surface_op: str | dict = "append") -> None:
        """user/message surface 事件(source 缺省 human 用户)。"""
        if source is None:
            source = {"kind": "user"}
        self.event("user/message", turn=self.turn, step=self.step, message=message,
                   source=source, surfaceOp=surface_op)

    def chunk(self, chunk: dict) -> None:
        """assistant/chunk 原始增量;seq 记入本 step 的 sourceSeqs。"""
        evt = self.event("assistant/chunk", turn=self.turn, step=self.step, chunk=chunk)
        if evt is not None:
            self._chunk_seqs.append(evt["seq"])

    def text(self, text: str, *, index: int = 0) -> None:
        self.text_chars += len(text)
        self.chunk({"type": "text", "index": index, "text": text})

    def tool_call(self, call_id: str, name: str | None, arguments: str) -> None:
        evt = self.event("tool/call", turn=self.turn, step=self.step, callId=call_id,
                         name=name, arguments=arguments)
        if evt is not None:
            self._call_seqs[call_id] = evt["seq"]

    def tool_result(self, message: dict, *, call_id: str, name: str | None,
                    is_error: bool, src_seq: int | None = None) -> None:
        data = {"turn": self.turn, "step": self.step, "message": message, "is_error": is_error,
                "surfaceOp": "append", "callId": call_id}
        if name:
            data["name"] = name
        if src_seq is not None:
            data["sourceSeqs"] = [src_seq]
        self.event("tool/result", **data)

    def result_meta(self, msg) -> None:
        """ResultMessage → session/end 汇总字段(usage/stop_reason/cost)。"""
        self.result_usage = _normalize_usage(getattr(msg, "usage", None))
        self.result_stop_reason = getattr(msg, "stop_reason", None)
        self.result_cost_usd = getattr(msg, "total_cost_usd", None)

    def finish(self, ok: bool, *, epilogue: bool = True) -> None:
        """finally 兜底:step/end → turn/end → session/end(幂等)。

        epilogue=False(dsh 投影路径专用):turn/step 终局事件已由 dsh 原生事件转发,
        只收尾 session/end(状态汇总字段组装不变)。
        """
        if self._ended:
            return
        self._ended = True
        state = "completed" if ok else "aborted"
        data = {"state": state, "ok": ok,
                "duration_ms": int((time.monotonic() - self.t0) * 1000),
                "text_chars": self.text_chars, "num_steps": self.step}
        for k, v in (("usage", self.result_usage), ("stop_reason", self.result_stop_reason),
                     ("total_cost_usd", self.result_cost_usd)):
            if v is not None:
                data[k] = v
        if not ok and self._reason:
            data["reason"] = self._reason
        if epilogue:
            if self._step_open:
                self.event("step/end", turn=self.turn, step=self.step)
                self._step_open = False
            self.event("turn/end", turn=self.turn, reason="completed" if ok else "error")
        self.event("session/end", **data)

    def error(self, exc: Exception, stage: str) -> None:
        """error 事件(全量 message,不截断);session/end.reason 取首 2000 字符。"""
        self.event("error", stage=stage, exc_type=type(exc).__name__, message=str(exc))
        self._reason = str(exc)[:2000]

    def _stepping(self) -> bool:
        return self._step_open


# ---------------------------------------------------------------------------
# 适配器通用:SDK/HTTP 对象 → 事件 dict(纯 dict 可单测)
# ---------------------------------------------------------------------------


def _norm_token(u, keys: tuple[str, ...]):
    """从 SDK 对象/字典取值(映射 prompt/completion_tokens 命名)的通用取值器。"""
    for k in keys:
        v = getattr(u, k, None)
        if v is None and isinstance(u, dict):
            v = u.get(k)
        if v is not None:
            return v
    return None


def _normalize_usage(u) -> dict | None:
    """SDK/HTTP usage → 统一结构 {input_tokens, output_tokens, cache_read_input_tokens}。"""
    if not u:
        return None
    return {
        "input_tokens": _norm_token(u, ("input_tokens", "prompt_tokens", "inputTokens")),
        "output_tokens": _norm_token(u, ("output_tokens", "completion_tokens", "outputTokens")),
        "cache_read_input_tokens": _norm_token(u, ("cache_read_input_tokens", "cacheReadTokens")),
    }


def _stage_of(exc: Exception) -> str:
    """error 事件 stage 分类:http(网络/状态码)/ parse(响应结构)/ run(其余)。"""
    if isinstance(exc, httpx.HTTPError):
        return "http"
    if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError)):
        return "parse"
    return "run"


def _dsh_session_id(session: str) -> str:
    """gh session id → dsh session_id:取最后一个 '/' 之后(与 FileSink 文件名规则一致)。"""
    return (session or "agent").rsplit("/", 1)[-1]


def _dsh_options_fields(options) -> dict:
    """鸭子类型 dsh options → DeepSeekHarness 构造 kwargs(None 跳过 → 走 SDK 配置缺省)。

    DeepSeekHarnessConfig 是 dataclass 而非 pydantic(无 model_dump),且
    DeepSeekHarness.__init__(config=None, **kwargs) 同时传 config 与 kwargs 会
    TypeError —— 恒走 kwargs;调用方自组装 options(仿 cc 的 ClaudeAgentOptions 透传)。
    """
    names = ("provider", "model", "max_tokens", "cwd", "runtime_cwd", "session_root",
             "cordis", "env", "runtime_bin", "launch_args_override",
             "request_timeout_seconds", "shutdown_timeout_seconds", "base_url", "api_key")
    return {k: v for k in names if (v := getattr(options, k, None)) is not None}


def _dsh_stage(exc: Exception, protocol_errors: tuple) -> str:
    """dsh 异常 stage:协议解析失败(SdkProtocolError/JsonRpcError)归 parse,其余沿 _stage_of。

    protocol_errors = 函数内 lazy import 的错误类元组(测试经假模块注入)。
    """
    if isinstance(exc, protocol_errors):
        return "parse"
    return _stage_of(exc)


_DSH_CORDIS_FILE: str | None = None  # 内容哈希键控缓存(每次调用少写盘;内容变了自然换文件名)


def dsh_cordis_path() -> str:
    """dsh 内置隔离组合文件路径(DSH_CORDIS_CONFIG 只认文件路径;按内容哈希落盘一次)。

    组合蓝图见下方 `_DSH_CORDIS_YAML`(镜像 dsh 官方 minimal.cordis.yml;SDK/
    JSON-RPC 路径只读这一个文件,无用户级 patch 层/无 XDG 配置 —— 组合即隔离
    边界)。语义同 cc `setting_sources=[]` 的默认位:上层不传 cordis(None)时,
    dsh_stream 回退本组合(默认隔离);上层显式提供 cordis(如
    envs.DEEPWIKI_DSH_CORDIS)则全权交给上层(隔离与否由上层组合保证)。
    """
    global _DSH_CORDIS_FILE
    if _DSH_CORDIS_FILE is None:
        digest = hashlib.sha1(_DSH_CORDIS_YAML.encode()).hexdigest()[:8]
        path = Path(tempfile.gettempdir()) / "gh-puller" / f"dsh-cordis-{digest}.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(_DSH_CORDIS_YAML, encoding="utf-8")
        _DSH_CORDIS_FILE = str(path)
    return _DSH_CORDIS_FILE


# 组合层隔离逐项(cordis.yml 之外再无配置注入面;余下环境残留 DSH_HOME/之类
# 无任何已装载组件读取 —— SDK 路径无 home patch/settings/credentials 装载):
# workspaceContext(禁 $DSH_HOME/AGENTS.md 与 checkout 的 AGENTS.md/CLAUDE.md 链)、
# skills(禁用户 $DSH_HOME/skills、$DSH_AGENTS_HOME/skills、项目 .dsh/skills 与
# 捆绑技能目录)、includeHarnessIdentity/includeRuntimeContext(提示词注入)、
# toolBash/toolJobs/goals(面模型工具域);随后显式装载 graphify(内置 mcp-client
# 单服务器行,无 .mcp.json/全局发现;graphifyy 自带 stdio MCP,工具对 agent 为
# mcp__graphify__query_graph)。hooks 未装载(配置文件型,须显式挂)。
_DSH_CORDIS_YAML = """- id: sdk-jsonrpc-server
  name: '@deepseek-ai/dsh-sdk-jsonrpc-server'
  config:
    maxTokensAsSuccess: false
- id: llm-deepseek
  name: '@deepseek-ai/dsh-llm-deepseek'
- id: sandbox
  name: '@deepseek-ai/dsh-sandbox-local'
- id: sandbox-policy
  name: '@deepseek-ai/dsh-sandbox-policy'
  config:
    mode: danger-full-access
    workspaceRoot: !!js process.env.DSH_CWD ?? process.cwd()
- id: subprocess
  name: '@deepseek-ai/dsh-subprocess-local'
- id: pty
  name: '@deepseek-ai/dsh-terminal'
- id: terminal-bash
  name: '@deepseek-ai/dsh-terminal-bash'
  config:
    timeoutMs: 300000
- id: fs-local
  name: '@deepseek-ai/dsh-fs-local'
  config:
    cwd: !!js process.env.DSH_CWD ?? process.cwd()
- id: agent-spine
  name: '@deepseek-ai/dsh-agent-spine-demo'
  config:
    includeHarnessIdentity: false
    includeRuntimeContext: false
    persona: !!js process.env.DSH_SYSTEM_PROMPT ?? 'You are a helpful software engineer assistant.'
    workspaceContext: false
    skills:
      enabled: false
    toolBash: false
    toolJobs: false
    goals: false
- id: persistent-bash
  name: '@deepseek-ai/dsh-tool-bash-persistent'
  config:
    timeoutMs: 300000
- id: str-replace-editor
  name: '@deepseek-ai/dsh-tool-str-replace-editor'
  config:
    maxOutputChars: 16000
- id: mcp-graphify
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: graphify
    transport: stdio
    command: !!js process.env.GRAPHIFY_MCP_PYTHON ?? 'python3'
    args: ['-m', 'graphify.serve']
    cwd: !!js process.env.DSH_CWD ?? process.cwd()
    failOnStartupError: true
    reconnect:
      enabled: false
- id: sessions
  name: '@deepseek-ai/dsh-session-persistence-jsonl'
  config:
    root: !!js process.env.DSH_SESSION_ROOT ?? './.sessions'
    compression: none
"""


def _handle_stream_event(run: _Run, event: dict) -> None:
    """归一化 SDK 原始流事件(cursor 型)→ 监控事件;不改动文本产出路径(产出在 cc_stream)。

    映射:content_block_delta → assistant/chunk(原始增量,含 thinking/tool_input);
    tool_use 收尾 → tool/call(原始 arguments JSON 字符串);tool_result 收尾 →
    tool/result(全量内容);message_start 且工具结果后 → step 边界。
    """
    typ = event.get("type")
    if typ == "message_start":
        if run._tool_pending and (event.get("message") or {}).get("role") == "assistant":
            run.step_boundary()
            run._tool_pending = False
        return
    if typ == "content_block_start":
        cb = event.get("content_block") or {}
        idx = event.get("index", -1)
        btype = cb.get("type")
        if btype == "tool_use":
            tid = cb.get("id") or ""
            run.tool_names[tid] = cb.get("name") or ""
            run._active_tool_use[idx] = tid
            run._tool_use_pieces[idx] = []
        elif btype == "tool_result":
            run._tool_pending = True
            run._tool_result = {
                "id": cb.get("tool_use_id") or "", "pieces": [], "is_error": bool(cb.get("is_error")),
            }
        return
    if typ == "content_block_delta":
        delta = event.get("delta") or {}
        dtype = delta.get("type")
        idx = event.get("index", -1)
        if run._tool_result is not None and dtype == "text_delta":  # 工具结果内容归属 tool/result
            run._tool_result["pieces"].append(delta.get("text") or "")
            return
        if dtype == "text_delta":
            run.text(delta.get("text") or "", index=idx)
            return
        if dtype == "thinking_delta":
            run.chunk({"type": "thinking", "index": idx, "text": delta.get("thinking") or ""})
            return
        if dtype == "input_json_delta":
            piece = delta.get("partial_json") or ""
            run._tool_use_pieces.setdefault(idx, []).append(piece)
            run.chunk({"type": "tool_input", "index": idx, "partial_json": piece})
        return
    if typ == "content_block_stop":
        idx = event.get("index", -1)
        if run._tool_result is not None:
            text = "".join(run._tool_result["pieces"])
            tid = run._tool_result["id"]
            run.tool_result(
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": text,
                     "is_error": run._tool_result["is_error"]},
                ]},
                call_id=tid, name=run.tool_names.get(tid),
                is_error=run._tool_result["is_error"], src_seq=run._call_seqs.get(tid),
            )
            run._tool_result = None
            return
        if idx in run._active_tool_use:
            tid = run._active_tool_use[idx]
            raw = "".join(run._tool_use_pieces.get(idx, []))
            run.tool_call(tid, run.tool_names.get(tid), raw)
            run._active_tool_use.pop(idx, None)
            run._tool_use_pieces.pop(idx, None)
            return
        return  # text/thinking 收尾:增量已逐条事件化(块型由 chunk.content 索引决定)


def _handle_assistant_message(run: _Run, msg, already_yielded: bool) -> None:
    """整块消息:未产出增量时 text 增量一次并事件化;此后发全量 assistant/message。

    sourceSeqs = 本 step 已发 chunk 的 seq;文本/思考/tool_use 块全量入 message;
    无流事件的 tool_use 兜底补合成 tool/call(流路径已由 content_block_stop 发射)。
    """
    content = []
    for b in msg.content:
        t = getattr(b, "type", None)
        if t == "text":
            text = getattr(b, "text", None) or ""
            if text and not already_yielded:
                run.text(text)
            content.append({"type": "text", "text": text})
        elif t == "thinking":
            content.append({"type": "thinking", "thinking": getattr(b, "thinking", None) or ""})
        elif t == "tool_use":
            entry = {"type": "tool_use", "id": getattr(b, "id", None) or "",
                     "name": getattr(b, "name", None) or ""}
            if getattr(b, "input", None) is not None:
                entry["input"] = b.input
            content.append(entry)
    run.event(
        "assistant/message", turn=run.turn, step=run.step,
        message={"role": "assistant", "content": content},
        usage=_normalize_usage(getattr(msg, "usage", None)),
        stop_reason=getattr(msg, "stop_reason", None),
        surfaceOp="append", sourceSeqs=list(run._chunk_seqs),
    )
    for block in msg.content:  # 兜底:无 input_json_delta 的 SDK 路径
        if getattr(block, "type", None) != "tool_use":
            continue
        tid = getattr(block, "id", None) or ""
        if tid and tid not in run._call_seqs:
            run.tool_call(tid, getattr(block, "name", None),
                          json.dumps(getattr(block, "input", None) or {}))


def _options_header(options) -> dict:
    """ClaudeAgentOptions → request/header 的 header 快照(partial 语义见调用方)。

    SDK 不暴露 rendered system / resolved 工具 schema,只取调用方 options:
    system_prompt 全量、工具名清单(allowed_tools + mcp 前缀),tools 无 schema。
    """
    system = getattr(options, "system_prompt", None) or ""
    names = list(getattr(options, "allowed_tools", None) or [])
    names.extend(f"mcp__{name}__" for name in (getattr(options, "mcp_servers", None) or {}))
    return {"config": {"provider": "claude", "model": getattr(options, "model", None) or ""},
            "system": system or None, "tools": [{"name": n} for n in names] or None}


def _llm_header(payload: dict) -> dict:
    """OpenAI payload → request/header 的 header 快照(请求体全可见 → 精确)。

    system 消息并入 system 字符串;tools 归一 {name, description?, input_schema?};
    config 透传 model 与常用标量(JSON 可序列化)。
    """
    system = "\n\n".join(str(m.get("content") or "")
                         for m in payload.get("messages", []) if m.get("role") == "system")
    tools: list[dict] = []
    for t in payload.get("tools") or []:
        f = t.get("function") or t
        tools.append({"name": f.get("name") or "", "description": f.get("description"),
                      "input_schema": f.get("input_schema") or f.get("parameters")})
    config: dict = {"provider": "openai", "model": payload.get("model")}
    for k in ("temperature", "max_tokens", "top_p", "response_format"):
        if payload.get(k) is not None:
            config[k] = payload[k]
    return {"config": config, "system": system or None, "tools": tools or None}


def _llm_emit_messages(run: _Run, payload: dict) -> None:
    """payload messages → surface 事件(仅 user/assistant 折叠;system 已入 header)。"""
    for m in payload.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant"):
            continue
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        message = {"role": role, "content": blocks}
        if role == "user":
            run.user_message(message)
        else:  # 历史 assistant 消息:无 usage/停止原因,仅内容折叠
            run.event("assistant/message", turn=run.turn, step=run.step, message=message,
                      surfaceOp="append")


def _llm_headers(headers: dict | None, api_key: str | None) -> dict:
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if api_key:
        hdrs.setdefault("Authorization", f"Bearer {api_key}")
    return hdrs


# ---------------------------------------------------------------------------
# 共享基类:run 装配 / 事件守卫 / text、result 缺省实现
# ---------------------------------------------------------------------------


class BaseAdapter:
    """三路共享骨架:cc = 单一权威合成;dsh = 双权威投影(对齐器);llm = 直连 HTTP。

    子类只要写差异驱动循环(stream / complete);text、result 的整收缺省已含。
    公共 kwarg(会话/run 元数据)逐参数与历史模块级函数一致 —— 底部以单例绑定
    回模块级公开名(cc_stream = _claude.stream 等),签名不变。
    """

    provider = ""  # 事件封套 provider 字段值(claude|openai|dsh|...)

    def _run(self, *, model: str, session: str | None = None, session_ns: str | None = None,
             run_id: str | None = None, session_name: str | None = None,
             meta: dict | None = None) -> _Run:
        """公共 kwarg → _Run(session id 规则/信封 provider 取类属性;不含 context/retry)。"""
        return _Run(_session_id(session, session_ns, run_id, session_name), self.provider,
                    model, label=session_name, run_id=run_id, meta=meta)

    @contextlib.asynccontextmanager
    async def _guard(self, run: _Run, *, error_stage=None, epilogue=True):
        """统一收尾:正常 → finish(ok=True);异常 → error(stage)+ raise + finish(False)。

        error_stage:设 lambda 指定 error 事件 stage(None 等价 cc 的硬编码 "run");
        epilogue:bool 或 callable(dsh 传 lambda: not proj.saw_turn_end,调用期求值)。
        只捕获 Exception(消费者提前关闭的 GeneratorExit、CancelledError 为
        BaseException,不落 error 事件,靠 finally 兜底 finish(False) —— 与历史
        try/except/finally 语义一致);run.finish 本身幂等。
        """
        ok = False
        try:
            yield
            ok = True
        except Exception as exc:
            run.error(exc, error_stage(exc) if error_stage else "run")
            raise
        finally:
            run.finish(ok, epilogue=epilogue() if callable(epilogue) else epilogue)

    async def stream(self, options, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None):
        """流式应答(子类必写):文本增量 async generator;监控事件经 run 信封发布。"""
        raise NotImplementedError

    async def text(self, options, prompt: str, *, session: str | None = None,
                   session_name: str | None = None, run_id: str | None = None,
                   session_ns: str | None = None,
                   context: list[dict] | None = None, retry: dict | None = None,
                   meta: dict | None = None) -> str:
        """整收应答缺省:收集流式文本(cc_text / dsh_text 共用)。"""
        parts: list[str] = []
        async for chunk in self.stream(options, prompt, session=session, session_name=session_name,
                                       run_id=run_id, session_ns=session_ns,
                                       context=context, retry=retry, meta=meta):
            parts.append(chunk)
        return "".join(parts)

    async def result(self, options, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None) -> str:
        """非流式最终结果缺省:整收 + 空结果 → RuntimeError(dsh_result 语义)。

        不适用于需要专用最终结果通道的 SDK(如 cc 的 ResultMessage.result,见子类覆盖)。
        """
        text = await self.text(options, prompt, session=session, session_name=session_name,
                               run_id=run_id, session_ns=session_ns,
                               context=context, retry=retry, meta=meta)
        if not text:
            raise RuntimeError("agent 未产出最终结果")
        return text


# ---------------------------------------------------------------------------
# Claude Code(SDK)包装:cc_stream / cc_text / cc_result
# ---------------------------------------------------------------------------


class ClaudeCodeAdapter(BaseAdapter):
    """cc:SDK 原料流 → 本地合成(唯一权威,无投影层;why 见模块 docstring)。"""

    provider = "claude"

    async def stream(self, options, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None):
        """Claude Code 流式应答(监控 + 执行)。

        对外产出:文本增量(StreamEvent text_delta 优先,AssistantMessage 兜底,
        ResultMessage.is_error → RuntimeError("agent 执行失败: ..."))—— 与 deepwiki
        原漏斗语义一致;thinking/工具增量仅进事件流。options 整体透传(调用方自组装,
        如 deepwiki 的进程内 MCP 闭包)。context = 上下文说明事件列表
        (context/inject|modify,{type,data} 形),重放于 session/start 之后。
        """
        from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, StreamEvent

        run = self._run(model=getattr(options, "model", None) or "",
                        session=session, session_ns=session_ns, run_id=run_id,
                        session_name=session_name, meta=meta)
        run.start(context=context, retry=retry)
        run.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        run.event("request/header", header=_options_header(options), reason="initial", partial=True)
        yielded = False
        async with self._guard(run), ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    _handle_stream_event(run, msg.event)
                    event = msg.event
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yielded = True
                            yield delta["text"]
                elif isinstance(msg, AssistantMessage):
                    _handle_assistant_message(run, msg, yielded)
                    if not yielded:
                        # 兜底:无 partial 事件时整块取文本(ThinkingBlock 无 text 属性,天然跳过)
                        for block in msg.content:
                            text = getattr(block, "text", None)
                            if text:
                                yield text
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        detail = (msg.errors or [])[-1] if msg.errors else msg.result
                        raise RuntimeError(f"agent 执行失败: {detail or msg.subtype}")
                    run.result_meta(msg)

    async def result(self, options, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None) -> str:
        """非流式取最终结果(judge 用):失败或无结果 → RuntimeError(调用方降级)。

        覆盖基类缺省:返回 ResultMessage.result 本身(非流式文本拼装),保留
        "agent 未产出最终结果" 语义。
        """
        from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, StreamEvent

        run = self._run(model=getattr(options, "model", None) or "",
                        session=session, session_ns=session_ns, run_id=run_id,
                        session_name=session_name, meta=meta)
        run.start(context=context, retry=retry)
        run.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        run.event("request/header", header=_options_header(options), reason="initial", partial=True)
        async with self._guard(run), ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    _handle_stream_event(run, msg.event)
                elif isinstance(msg, AssistantMessage):
                    _handle_assistant_message(run, msg, already_yielded=run.text_chars > 0)
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        detail = (msg.errors or [])[-1] if msg.errors else msg.result
                        raise RuntimeError(f"agent 执行失败: {detail or msg.subtype}")
                    run.result_meta(msg)
                    result = msg.result
                    if not result:
                        raise RuntimeError("agent 未产出最终结果")
                    return result
            raise RuntimeError("agent 未产出最终结果")


# ---------------------------------------------------------------------------
# OpenAI 兼容(httpx)包装:llm_complete / llm_stream
# ---------------------------------------------------------------------------


class OpenAIAdapter(BaseAdapter):
    """OpenAI 兼容(httpx 直连):无 agent 形态,入口为 complete(非流式)/ stream(SSE)。"""

    provider = "openai"

    async def complete(
        self, *, url: str, payload: dict, api_key: str | None = None,
        timeout: httpx.Timeout | None = None, headers: dict | None = None,
        session: str | None = None, session_name: str | None = None,
        run_id: str | None = None, session_ns: str | None = None,
        context: list[dict] | None = None, retry: dict | None = None,
        meta: dict | None = None,
    ) -> str:
        """OpenAI 兼容非流式补全(异常原样抛,重试留给调用方)。

        payload 为 chat/completions 请求体(须含 model/messages;其余键原样透传,
        如 response_format/temperature/max_tokens —— 兼容题库扩展点),HTTP body 与直连一致。
        事件:payload 全量消息 → surface(可折叠恢复该请求输入);响应当次
        text 增量 + assistant/message + 每 tool_call 一个 tool/call(原始 arguments 字符串)。
        """
        model = payload["model"]
        run = self._run(model=model, session=session, session_ns=session_ns, run_id=run_id,
                        session_name=session_name, meta=meta)
        run.start(context=context, retry=retry)
        _llm_emit_messages(run, payload)
        run.event("request/header", header=_llm_header(payload), reason="initial")
        run.event("request/context", provider="openai", model=model)
        async with self._guard(run, error_stage=_stage_of), httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{url}/chat/completions", json=payload,
                                     headers=_llm_headers(headers, api_key))
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"] or {}
            content = msg.get("content") or ""
            usage = data.get("usage") or {}
            if content:
                run.text(content)
            blocks = [{"type": "text", "text": content}] if content else []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments") or ""
                try:
                    parsed = json.loads(args) if args else None
                except json.JSONDecodeError:
                    parsed = args
                blocks.append({"type": "tool_use", "id": tc.get("id"),
                               "name": fn.get("name") or "", "input": parsed})
                run.tool_call(tc.get("id") or "", fn.get("name"), args)
            norm_usage = _normalize_usage(usage)
            run.event(
                "assistant/message", turn=run.turn, step=run.step,
                message={"role": "assistant", "content": blocks},
                usage=norm_usage, stop_reason=msg.get("stop_reason"),
                surfaceOp="append", sourceSeqs=list(run._chunk_seqs),
            )
            run.result_usage = norm_usage
            run.result_stop_reason = msg.get("stop_reason")
            return content

    async def stream(
        self, *, url: str, payload: dict, api_key: str | None = None,
        timeout: httpx.Timeout | None = None, headers: dict | None = None,
        session: str | None = None, session_name: str | None = None,
        run_id: str | None = None, session_ns: str | None = None,
        context: list[dict] | None = None, retry: dict | None = None,
        meta: dict | None = None,
    ):
        """OpenAI 兼容流式补全(SSE 逐 delta):payload 语义同 complete,附加 stream=True。"""
        run = self._run(model=payload["model"], session=session, session_ns=session_ns,
                        run_id=run_id, session_name=session_name, meta=meta)
        run.start(context=context, retry=retry)
        _llm_emit_messages(run, payload)
        run.event("request/header", header=_llm_header(payload), reason="initial")
        run.event("request/context", provider="openai", model=payload["model"])
        full = ""
        body: dict = {**payload, "stream": True}
        async with (self._guard(run, error_stage=_stage_of),
                    httpx.AsyncClient(timeout=timeout) as client,
                    client.stream("POST", f"{url}/chat/completions", json=body,
                                  headers=_llm_headers(headers, api_key)) as resp):
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    break
                choices = json.loads(payload).get("choices") or []
                text = (choices[0].get("delta") or {}).get("content") or ""
                if text:
                    full += text
                    run.text(text)
                    yield text
        run.event(
            "assistant/message", turn=run.turn, step=run.step,
            message={"role": "assistant", "content": [{"type": "text", "text": full}]},
            surfaceOp="append", sourceSeqs=list(run._chunk_seqs),
        )


# ---------------------------------------------------------------------------
# DeepSeek Harness(SDK)投影全家(模块级:纯 dict 构造,测试可喂假 Notification)
# ---------------------------------------------------------------------------


class _DshProj:
    """dsh 会话事件 → gh 事件流的投影状态(单次 dsh_stream 调用一个实例)。

    seq_map: dsh session 事件 seq → gh 封套 seq。dsh seq 统计包括被跳过的插件
    事件(字段悬空),gh seq 只算本项目 TAXONOMY 投影 —— sourceEventSeqs 必须
    经本表映射为 gh sourceSeqs,未映射者丢弃;任何时刻不拷贝原始 dsh seq。
    tool_names: callId → 工具名(tool/result 补 name,来自 dsh tool/call 转发);
    tool_pieces: 本 step 内块 index → {id, name, pieces}(tool-call-delta 碎片,
    step/start 归零 —— 块 index 每 step 从 0 重新计数);
    saw_user_message / saw_turn_end:兜底与终局判定;last_finish_kind:最近 finish
    chunk 的 reason(assistant/message 与 session/end 的 stop_reason 来源)。

    为什么区分"合成"与"投影"(dsh 事件同源却比 cc 多一套 _DshProj):
    - cc 是唯一权威 → 合成器。Claude SDK 给出的是原料流(增量 StreamEvent、聚合
    AssistantMessage/ResultMessage),不携带 session/turn/step 编号、无生命周期事件、
    无 sourceSeqs。适配器只维护一套自己造的编号(seq 自己数、turn/step 自开合、
    tool/call|result 自合成、sourceSeqs 引用自己的 _chunk_seqs),正确性靠自洽,
    去重是布尔判断(already_yielded、tid in _call_seqs)。
    - dsh 是第二权威 → 对齐器。词汇虽与 TAXONOMY 同源,但 dsh 已把会话语义做掉一半,
    且用的是 dsh 自己的编号:seq 按 log.length 对全部会话事件计数(含被跳过的插件
    事件),turn/step 生命周期由 dsh 发,同一 tool-call 既有原料(block-end 完整
    arguments)又有成品(显式 tool/call),sourceEventSeqs 引用 dsh 编号空间,
    字段命名(camelCase usage、tool-result/toolCallId/isError 卡片)也不同。适配器
    不能发明、只能对账:seq_map 重映射 + 生命周期让渡(prologue=False /
    epilogue=not saw_turn_end)+ synth 双表达去重 + 字段改名 —— 即 _DshProj。
    """

    def __init__(self, run: _Run, prompt: str, session_id: str):
        self.run = run
        self.prompt = prompt
        self.session_id = session_id  # dsh session_id(JSONL 文件名;子代理会话过滤用)
        self.seq_map: dict[int, int] = {}
        self.tool_names: dict[str, str] = {}
        self.tool_pieces: dict[int, dict] = {}
        self.synth: dict[str, int] = {}  # callId → 块端已合成 tool/call 的 gh seq(显式事件去重)
        self.saw_user_message = False
        self.saw_turn_end = False
        self.last_finish_kind: str | None = None

    def track(self, dsh_seq: int, action) -> int | None:
        """执行一次恰好发布单事件的 action(如 run.text/tool_call),记录 dsh_seq → gh seq。

        bus disabled 时 run.seq 不增长(事件不构造)→ 不记录映射,防悬空 seq;
        返回 gh seq(未发布返回 None)。
        """
        before = self.run.seq
        action()
        if self.run.seq > before and dsh_seq is not None:
            self.seq_map[dsh_seq] = self.run.seq - 1
            return self.run.seq - 1
        return None

    def forward(self, dsh_seq: int, evt_type: str, **data) -> dict | None:
        """发布 gh 事件并记录 seq 映射;返回事件(无 sink 时 None)。"""
        evt = self.run.event(evt_type, **data)
        if evt is not None and dsh_seq is not None:
            self.seq_map[dsh_seq] = evt["seq"]
        return evt

    def source_seqs(self, envelope: dict) -> list[int]:
        """dsh sourceEventSeqs → gh sourceSeqs(经 seq_map 映射;未映射者丢弃)。"""
        return [self.seq_map[s] for s in (envelope.get("sourceEventSeqs") or [])
                if s in self.seq_map]


def _project_dsh_chunk(run: _Run, proj: _DshProj, dsh_seq, chunk: dict) -> list[str]:
    """dsh StreamChunk → gh assistant/chunk 增量投影(逐条),返回文本增量。

    文本增量走 run.text(计入 text_chars 且 yield);thinking/tool_input 只进
    事件流不改变产出(与 cc 漏斗一致);tool-call 收尾经 block-end 的完整
    arguments 合成 tool/call(整串优先,缺失时拼 delta 碎片)。
    """
    ctype = chunk.get("type")
    if ctype == "text-delta":
        text = chunk.get("text") or ""
        proj.track(dsh_seq, lambda: run.text(text, index=chunk.get("index", 0)))
        return [text] if text else []
    if ctype == "reasoning-delta":
        proj.track(dsh_seq, lambda: run.chunk({"type": "thinking", "index": chunk.get("index", 0),
                                               "text": chunk.get("text") or ""}))
        return []
    if ctype == "tool-call-delta":
        idx = chunk.get("index", -1)
        slot = proj.tool_pieces.setdefault(
            idx, {"id": chunk.get("id") or "", "name": chunk.get("name") or "", "pieces": []})
        if chunk.get("name"):
            slot["name"] = chunk["name"]
        piece = chunk.get("argumentsDelta") or ""
        slot["pieces"].append(piece)
        proj.track(dsh_seq, lambda: run.chunk({"type": "tool_input", "index": idx,
                                               "partial_json": piece}))
        return []
    if ctype == "block-end":
        block = chunk.get("block") or {}
        if block.get("type") == "tool-call":
            slot = proj.tool_pieces.pop(chunk.get("index", -1), None)
            call_id = block.get("id") or (slot or {}).get("id") or ""
            name = block.get("name") or (slot or {}).get("name")
            if call_id and name:
                proj.tool_names[call_id] = name
            args = block.get("arguments")
            if not isinstance(args, str) or args == "":
                args = "".join((slot or {}).get("pieces", []))
            # 会话日志随后仍有显式 tool/call 同 id 事件:先合成,后者去重映射(见 tool/call 分支)
            gh_seq = proj.track(dsh_seq, lambda: run.tool_call(call_id, name, args or ""))
            if gh_seq is not None and call_id:
                proj.synth[call_id] = gh_seq
        return []
    if ctype == "usage":
        usage = chunk.get("usage")
        if usage:
            run.result_usage = _normalize_usage(usage)
        return []
    if ctype == "finish":
        reason = chunk.get("reason")
        kind = reason.get("kind") if isinstance(reason, dict) else reason
        if kind:
            proj.last_finish_kind = kind
            run.result_stop_reason = kind
        return []
    return []  # block-start 与未知块:信息无需事件(增量已逐条投影)


def _project_dsh_event(run: _Run, proj: _DshProj, notif) -> list[str]:
    """dsh 通知 → gh TAXONOMY 事件投影(纯 dict 构造,测试可喂假 Notification)。

    返回本事件产生的文本增量(供 dsh_stream yield)。规则:
    - 只认 session.event 且 sessionId 匹配本运行(SDK 在过滤前即回调所有会话通知,
      子代理等其它会话在此静默丢弃);
    - 非 TAXONOMY 类型(dsh 插件扩展事件)静默跳过 —— new_event 对未知 type 抛 ValueError;
    - surfaceOp/sourceEventSeqs 在信封层(sourceEventSeqs 经 proj.seq_map 映射);
    - user/message 扁平化重塑、tool/result 卡片改名(规范见 events.py 折叠契约)。
    """
    if getattr(notif, "method", None) != "session.event":
        return []
    payload = getattr(notif, "payload", None) or {}
    if payload.get("sessionId") != proj.session_id:
        return []
    envelope = payload.get("event") or {}
    evt_type = envelope.get("type")
    if evt_type not in TAXONOMY:
        return []
    data = envelope.get("data") or {}
    dsh_seq = envelope.get("seq")

    # 兜底:极罕见流缺 user/message 时,首个 assistant 事件前合成 prompt 消息
    # (绝不在 prologue 预发 —— dsh 会发自己的,且含插件/注入消息)。
    if evt_type.startswith("assistant/") and not proj.saw_user_message:
        proj.saw_user_message = True
        run.user_message({"role": "user",
                          "content": [{"type": "text", "text": proj.prompt}]})

    if evt_type == "turn/start":
        run.turn = data.get("turn", run.turn)
        proj.forward(dsh_seq, "turn/start", turn=run.turn)
    elif evt_type == "step/start":
        run.turn = data.get("turn", run.turn)
        run.step = data.get("step", run.step)
        proj.tool_pieces = {}  # 块 index 每 step 从 0 重新计数
        run._step_open = True  # 崩溃路径(无 step/end)epilogue 有可合的 step/end
        proj.forward(dsh_seq, "step/start", turn=run.turn, step=run.step)
    elif evt_type == "step/end":
        run.turn = data.get("turn", run.turn)
        run.step = data.get("step", run.step)
        run._step_open = False
        proj.forward(dsh_seq, "step/end", turn=run.turn, step=run.step)
    elif evt_type == "turn/end":
        run.turn = data.get("turn", run.turn)
        proj.saw_turn_end = True
        reason = data.get("reason")
        kind = reason.get("kind") if isinstance(reason, dict) else reason
        detail = {"turn": run.turn, "reason": kind}
        if isinstance(reason, dict):
            rest = {k: v for k, v in reason.items() if k != "kind"}
            if rest:
                detail["detail"] = rest  # 结构化失败细目透传(UI 排查素材)
        if not proj.last_finish_kind and kind:
            run.result_stop_reason = kind  # 无 finish chunk 的兜底 source
        proj.forward(dsh_seq, "turn/end", **detail)
    elif evt_type == "user/message":
        proj.saw_user_message = True
        message = {"role": data.get("role", "user"), "content": data.get("content") or []}
        proj.track(dsh_seq, lambda: run.user_message(
            message, source=data.get("source"),
            surface_op=envelope.get("surfaceOp") or "append"))
    elif evt_type == "request/header":
        proj.forward(dsh_seq, "request/header", **data)  # 完整 header(真实 system/tools)
    elif evt_type == "request/context":
        proj.forward(dsh_seq, "request/context", **data)
    elif evt_type == "assistant/chunk":
        run.turn = data.get("turn", run.turn)
        run.step = data.get("step", run.step)
        return _project_dsh_chunk(run, proj, dsh_seq, data.get("chunk") or {})
    elif evt_type == "assistant/message":
        run.turn = data.get("turn", run.turn)
        run.step = data.get("step", run.step)
        evt_data = {
            "turn": run.turn, "step": run.step,
            "message": data.get("message") or {},
            "surfaceOp": envelope.get("surfaceOp") or "append",
        }
        if "usage" in data:
            evt_data["usage"] = _normalize_usage(data["usage"])
        if data.get("interrupted"):
            evt_data["interrupted"] = True
        if proj.last_finish_kind:
            evt_data["stop_reason"] = proj.last_finish_kind
        src = proj.source_seqs(envelope)
        if src:
            evt_data["sourceSeqs"] = src
        proj.forward(dsh_seq, "assistant/message", **evt_data)
        if data.get("usage"):
            run.result_usage = _normalize_usage(data["usage"])  # 末条为准 → session/end
    elif evt_type == "tool/call":
        run.turn = data.get("turn", run.turn)
        run.step = data.get("step", run.step)
        call_id = data.get("callId") or ""
        name = data.get("name")
        if call_id and name:
            proj.tool_names[call_id] = name
        if call_id in proj.synth:
            # 块端(assistant/chunk block-end)已合成同 id 的 tool/call:显式事件不再重复,
            # 其 dsh seq 映射到已合成事件 —— 供 tool/result.sourceEventSeqs 溯源到同一 gh 事件
            if dsh_seq is not None:
                proj.seq_map[dsh_seq] = proj.synth[call_id]
        else:
            proj.track(dsh_seq, lambda: run.tool_call(call_id, name, data.get("arguments") or ""))
    elif evt_type == "tool/result":
        run.turn = data.get("turn", run.turn)
        run.step = data.get("step", run.step)
        msg = data.get("message") or {}
        card = ((msg.get("content") or [{}])[0]
                if isinstance(msg.get("content"), list) else {})
        call_id = (msg.get("source") or {}).get("callId") or (card.get("toolCallId") or "")
        is_error = bool(card.get("isError"))
        # 卡片改名:dsh tool-result/toolCallId/isError → gh tool_result/tool_use_id/is_error;
        # 文本块拼接为 content 字符串(cc 先例:全量不截断)
        text = "".join(b.get("text") or "" for b in (card.get("content") or [])
                       if isinstance(b, dict) and b.get("type") == "text")
        evt_data = {
            "turn": run.turn, "step": run.step,
            "message": {"role": "user", "content": [{"type": "tool_result",
                                                     "tool_use_id": call_id,
                                                     "content": text, "is_error": is_error}]},
            "callId": call_id, "is_error": is_error, "surfaceOp": "append",
        }
        if call_id in proj.tool_names:
            evt_data["name"] = proj.tool_names[call_id]
        src = proj.source_seqs(envelope)
        if src:
            evt_data["sourceSeqs"] = src
        if isinstance(data.get("error"), dict):
            evt_data["error"] = data["error"]
        proj.forward(dsh_seq, "tool/result", **evt_data)
    return []


def _dsh_worker(fields: dict, prompt: str, session_id: str, pump):
    """同步线程体:构造 harness(子进程 spawn + initialize)→ 阻塞 run 至 idle。

    整个体在 executor 线程执行;即使外层 asyncio task 被取消(消费者提前退场),
    线程仍会自然跑完 —— with 块负责回收子进程,不泄漏。
    """
    from deepseek_harness import DeepSeekHarness  # lazy:调用时构造(与 cc 同法)

    with DeepSeekHarness(**fields) as harness:
        return harness.run(prompt, session_id=session_id, on_notification=pump)


# ---------------------------------------------------------------------------
# DeepSeek Harness(SDK)包装:dsh_stream / dsh_text / dsh_result
# ---------------------------------------------------------------------------


class DshAdapter(BaseAdapter):
    """dsh:事件词汇同源但 dsh 是第二权威 → _DshProj 投影对齐(why 见模块 docstring)。"""

    provider = "dsh"

    async def stream(self, options, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None):
        """dsh(DeepSeek Harness SDK)流式应答(监控 + 执行)。

        对外产出:与 cc_stream 同契约 —— 文本增量(assistant/chunk 的 text-delta),
        非 completed 的 finish_reason → RuntimeError("agent 执行失败: ...");
        thinking/工具增量只进事件流,不改变产出。dsh 原生事件 1:1 投影为监控
        事件流(turn/step/stop_reason 均取自主干事件,见 _project_dsh_event);
        session/start|end 由本层合成(dsh 仅有 session/end-seed)。

        options = 调用方组装的鸭子类型配置(仿 cc 的 options 透传):provider(缺省
        "deepseek-official" 经 SDK 侧域)、model、cwd、session_root、cordis、env、
        max_tokens、base_url、api_key 等,None 跳过(见 _dsh_options_fields);其中
        **cordis 缺省回退内置隔离组合**(dsh_cordis_path,上层未提供即默认隔离;
        显式提供则全权交上层)。

        SDK run() 为同步阻塞:经 asyncio.to_thread 执行,on_notification 回调在线程
        侧经 call_soon_threadsafe 泵入队列,本生成器逐条消费 —— 保持 asyncio 接口
        形态不变。阻塞性:单次运行一 turn to_idle,无逐 prompt 取消(见 dsh 协议)。
        """
        from deepseek_harness import errors as _dsh_errors

        run = self._run(model=getattr(options, "model", None) or "",
                        session=session, session_ns=session_ns, run_id=run_id,
                        session_name=session_name, meta=meta)
        proj = _DshProj(run, prompt, _dsh_session_id(run.session))
        fields = _dsh_options_fields(options)
        fields.setdefault("cordis", dsh_cordis_path())  # 上层未提供 → 缺省隔离组合(见 dsh_cordis_path)
        queue: asyncio.Queue = asyncio.Queue()  # 无界:1:1 于运行时事件流,永不阻塞发布侧
        loop = asyncio.get_running_loop()

        def pump(notif) -> None:  # 运行时线程回调:跨线程入队(loop 已关闭竞态静默丢)
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, ("notif", notif))

        async def _worker() -> None:
            try:
                result = await asyncio.to_thread(_dsh_worker, fields, prompt, proj.session_id, pump)
                loop.call_soon_threadsafe(queue.put_nowait, ("done", result))
            except Exception as exc:  # noqa: BLE001 —— 异常经队列送消费侧统一 error/finish
                loop.call_soon_threadsafe(queue.put_nowait, ("exc", exc))

        run.start(context=context, retry=retry, prologue=False)  # dsh 自带 turn/step 生命周期
        async with self._guard(
            run,
            error_stage=lambda exc: _dsh_stage(exc, (_dsh_errors.SdkProtocolError,
                                                     _dsh_errors.JsonRpcError)),
            epilogue=lambda: not proj.saw_turn_end,
        ):
            task = asyncio.create_task(_worker())
            try:
                while True:
                    kind, item = await queue.get()
                    if kind == "notif":
                        for delta in _project_dsh_event(run, proj, item):
                            yield delta
                    elif kind == "exc":
                        raise item
                    else:  # ("done", RunResult):run() 返回前所有通知已交付,无竞态
                        if item.finish_reason != "completed":
                            raise RuntimeError(f"agent 执行失败: {item.finish_reason}")
                        break
            finally:
                if not task.done():
                    task.cancel()  # 消费者提前退场:executor 线程继续自然跑完(见 _dsh_worker)


# ---------------------------------------------------------------------------
# Codex(OpenAI Codex SDK)包装:codex_stream / codex_text / codex_result
# ---------------------------------------------------------------------------


def _codex_val(x):
    """枚举成员 → 值(pydantic/codex 枚举 .value;普通值原样)—— 状态比较统一走值。"""
    return getattr(x, "value", x)


def _codex_lookup(v, enum_cls, label):
    """options 的 Sandbox/ApprovalMode(名字/值/枚举成员)→ SDK 枚举;None → None。

    调用方(deepwiki)常传字符串("full_access" / "auto_review");高级用户可传 SDK 枚举。
    名字与值双匹配 —— 防 str 子类枚举当 str 用后按名匹配失真。
    """
    if v is None:
        return None
    if isinstance(v, enum_cls):
        return v
    raw = _codex_val(v)
    for member in enum_cls:
        if raw in (member.name, member.value):
            return member
    raise ValueError(f"codex {label} 取值非法(可选 {[m.name for m in enum_cls]}): {v!r}")


def _codex_config_fields(options) -> dict:
    """鸭子类型 codex options → CodexConfig 构造 kwargs(None 跳过 → 走 SDK 缺省)。

    与 _dsh_options_fields 同法:调用方自组装 options(仿 cc 的 ClaudeAgentOptions 透传);
    env 由调用流合并 CODEX_HOME 后整传入 —— SDK 从不自设 CODEX_HOME,隔离点见
    codex_home_path / _codex_home_setup。
    """
    names = ("cwd", "codex_bin", "config_overrides", "launch_args_override", "env")
    return {k: v for k, v in ((k, getattr(options, k, None)) for k in names) if v is not None}


def _codex_thread_fields(options) -> dict:
    """codex options → AsyncCodex.thread_start 透传 kwargs(sandbox/approval_mode 由
    stream 内经 _codex_lookup 转换后塞入,此处只保留其余自由字段)。

    system_prompt 缺省映射 base_instructions(与 cc 的 system_prompt 同位语义)。
    """
    names = ("base_instructions", "developer_instructions", "personality", "ephemeral",
             "model", "model_provider", "service_tier", "config", "cwd",
             "session_start_source", "thread_source")
    fields = {k: v for k, v in ((k, getattr(options, k, None)) for k in names) if v is not None}
    if getattr(options, "system_prompt", None) and "base_instructions" not in fields:
        fields["base_instructions"] = options.system_prompt
    return fields


def _codex_turn_fields(options) -> dict:
    """codex options → AsyncThread.turn 透传 kwargs。"""
    names = ("cwd", "effort", "output_schema", "personality", "service_tier", "summary")
    return {k: v for k, v in ((k, getattr(options, k, None)) for k in names) if v is not None}


def _codex_header(options) -> dict:
    """codex options → request/header 的 header 快照(partial=True:SDK 不暴露请求体)。

    tools 缺省 = 隔离 config.toml 装载的 graphify 工具(见 _codex_home_setup),与
    _agent_note 的 mcp__graphify__query_graph 呼应(deepwiki 侧注入指引文本);
    仅监控用,无 schema。
    """
    system = getattr(options, "system_prompt", None) or ""
    names = list(getattr(options, "tools", None) or ["mcp__graphify__query_graph"])
    return {"config": {"provider": "codex", "model": getattr(options, "model", None) or ""},
            "system": system or None, "tools": [{"name": n} for n in names] or None}


def _codex_args_json(arguments) -> str:
    """codex 工具 arguments(Any:dict/str/None)→ tool/call 的原始 JSON 字符串。

    与 cc 的 raw 字符串契约一致:dict → json.dumps,str 原样(可能本身是 JSON 文本),
    None/空 → ""(UI 端解析失败原样展示)。
    """
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments)


def _codex_tool_name(server: str, tool: str) -> str:
    """codex MCP 工具名归一:mcp__{server}__{tool}(server 缺省时裸 tool)。"""
    return f"mcp__{server}__{tool}" if server else tool


def _codex_stage(exc: Exception, protocol_errors: tuple) -> str:
    """codex 异常 stage:JSON-RPC 协议层(JsonRpcError 家族)归 parse,其余沿 _stage_of。

    protocol_errors = 方法内 lazy import 的错误类元组(测试经假模块注入);turn failed
    的 RuntimeError("agent 执行失败: ...")不是协议错误 → run(正确语义)。
    """
    if isinstance(exc, protocol_errors):
        return "parse"
    return _stage_of(exc)


def codex_home_path() -> str:
    """codex 隔离 home 路径(缺省 ~/.gh-puller/codex-home;显式经 options.codex_home)。

    与 dsh_cordis_path 同位语义:app-server 只读本目录(CODEX_HOME 下 config.toml /
    auth.json / sessions),不读用户 ~/.codex —— 目录即隔离边界;目录稳定(会话持久,
    thread_resume 可用),区别于 dsh_cordis 的 temp+内容哈希(文件名固定 config.toml,
    无法按内容换名,见 _codex_home_setup 的内容比对改写)。
    """
    return str(Path.home() / ".gh-puller" / "codex-home")


_CODEX_GRAPHIFY_COMMAND: str | None = None  # 一次解析(sys.executable 随 venv 定,不随调用变)


def _codex_graphify_command() -> str:
    """graphify MCP 启动解释器:GRAPHIFY_MCP_PYTHON 显式优先,否则 sys.executable。

    sys.executable 保证 graphifyy 可导入(同一 venv);命令写进 config.toml,
    值变化时 _codex_home_setup 内容比对自然重写。
    """
    global _CODEX_GRAPHIFY_COMMAND
    if _CODEX_GRAPHIFY_COMMAND is None:
        _CODEX_GRAPHIFY_COMMAND = os.environ.get("GRAPHIFY_MCP_PYTHON") or sys.executable
    return _CODEX_GRAPHIFY_COMMAND


def _codex_config_toml_content(*, command: str) -> str:
    """隔离 config.toml 内容:仅 graphify 一行 mcp 服务器(组合即隔离边界,镜像 dsh 的
    _DSH_CORDIS_YAML —— 无用户 model_provider/keys/mcp/hook/高级设置)。

    env_vars = ["GRAPHIFY_OUT"] 白名单:值不内联(config.toml 静态 —— 多仓库并发无文件
    竞态),每 run 经 CodexConfig.env 注入 app-server 进程环境,再由 rmcp stdio 启动器
    (create_env_for_mcp_server)按白名单取走(MCP 子进程 env_clear 后仅带该白名单)。
    """
    command = json.dumps(command)  # TOML 基本字符串(JSON 转义兼容)
    return (
        "[mcp_servers.graphify]\n"
        f"command = {command}\n"
        'args = ["-m", "graphify.serve"]\n'
        'env_vars = ["GRAPHIFY_OUT"]\n'
        "startup_timeout_sec = 30\n"
        "required = true\n"
    )


def _codex_home_setup(home, *, graphify_command: str | None = None,
                      auth_src: str | Path | None = None) -> str:
    """确保 codex 隔离 home 就绪:config.toml(graphify 单服务器,内容不变不重写)+ auth 引导。

    auth 引导(cc 同形:凭证通道不隔离,隔离只管设置面 —— 见 cc setting_sources=[]):
    home/auth.json 缺失且 auth_src(缺省 ~/.codex/auth.json)存在 → **符号链接**——
    与 cc 的 CLI 自持凭证一样实时引用(重新登录/revoke 即跟随,无副本陈旧);旧副本
    (非符号链接)存在时替换为链接。符号链接不可用(windows/文件系统限制)时回落复制。
    auth_src 传 False 可关闭引导(纯隔离无凭证态);显式 token 走 login_api_key 时
    app-server 自写 home/auth.json(先删符号链接再真写,不冲突)。
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    cfg = _codex_config_toml_content(command=graphify_command or _codex_graphify_command())
    cfg_path = home / "config.toml"
    if not cfg_path.exists() or cfg_path.read_text(encoding="utf-8") != cfg:
        cfg_path.write_text(cfg, encoding="utf-8")
    auth = home / "auth.json"
    if auth_src is not False and not auth.is_symlink():
        src = Path(auth_src) if auth_src else Path.home() / ".codex" / "auth.json"
        if src.exists():
            if auth.exists() or auth.is_symlink():
                auth.unlink()  # 旧副本(含悬空链接)→ 替换为链接或复制
            try:
                auth.symlink_to(src)
            except OSError:  # windows/不支持符号链接 → 复制兜底(陈旧风险可忽略:覆写即重置)
                shutil.copyfile(src, auth)
    return str(home)


class _CodexSynth:
    """codex 通知 → gh 事件流的合成状态(单次 codex_stream 调用一个实例)。

    为什么是合成而非投影(对比 _DshProj):codex 通知不携带 seq/turn/step 编号、无
    生命周期事件、无 sourceEventSeqs —— 流顺序是唯一权威(与 cc 同构);合成器只维护
    自洽编号(run.seq 自己数、turn/step 自开合、tool/call|result 自合成),去重是
    字典/布尔判断,没有第二套编号要伺候。
    """

    def __init__(self, run: _Run, prompt: str):
        self.run = run
        self.prompt = prompt
        self.turn_id: str | None = None  # turn/started 的 turn.id(记录用;路由已由 SDK 按 turn 过滤)
        self.agent_pieces: dict[str, list[str]] = {}  # itemId → agentMessage 增量碎片(去重/消息组装)
        self.reasoning_seen: set[str] = set()  # 已流式化 thinking 的 reasoning itemId(completed 兜底去重)
        self.tool_round_open = False  # 本轮已发 tool/result → 下次 LLM item 开一次 step 边界(聚合并行工具)
        self.plan_items: set[str] = set()  # 已发 plan 文本的 itemId(防 delta/completed 双投)
        self.saw_turn_completed = False


def _codex_item(item) -> Any:
    """ThreadItem(RootModel)→ 实际 item(RootModel 不代理属性访问;普通对象原样)。"""
    return getattr(item, "root", item)


def _codex_item_type(item) -> str:
    return getattr(_codex_item(item), "type", None) or ""


def _codex_tool_result(item, itype: str) -> dict:
    """工具类 completed item → tool/call|result 归一数据块(name/content/is_error/arguments)。

    工具项只在 item/completed 合成(完整 arguments/结果一处齐;item/started 的
    arguments 可能不完整 —— v1 舍弃提前位,未来可提前到 started 只发 tool/call)。
    """
    if itype == "mcpToolCall":
        name = _codex_tool_name(getattr(item, "server", None) or "",
                                getattr(item, "tool", None) or "")
        content_parts = []
        result = getattr(item, "result", None)
        if result is not None:
            for part in getattr(result, "content", None) or []:
                inner = _codex_item(part)
                if getattr(inner, "text", None):
                    content_parts.append(inner.text)
            structured = getattr(result, "structured_content", None)
            if structured is not None:
                content_parts.append(json.dumps(structured))
        is_error = (getattr(item, "error", None) is not None
                    or _codex_val(getattr(item, "status", None)) == "failed")
        arguments = _codex_args_json(getattr(item, "arguments", None))
    elif itype == "dynamicToolCall":
        name = getattr(item, "tool", None) or ""
        content_parts = [
            getattr(_codex_item(ci), "text", None) or ""
            for ci in (getattr(item, "content_items", None) or [])
            if _codex_item_type(ci) == "inputText"
        ]
        is_error = (getattr(item, "success", None) is False
                    or _codex_val(getattr(item, "status", None)) == "failed")
        arguments = _codex_args_json(getattr(item, "arguments", None))
    else:  # commandExecution
        name = "shell"
        content_parts = [getattr(item, "aggregated_output", None) or ""]
        command = getattr(item, "command", None) or ""
        cwd = getattr(item, "cwd", None) or ""
        is_error = (getattr(item, "exit_code", None) not in (None, 0)
                    or _codex_val(getattr(item, "status", None)) == "failed")
        # 字段为 LegacyAppPathString(pydantic 路径类型,str 子类)→ 归一为纯 str 再 JSON
        arguments = json.dumps({k: str(v) for k, v in (("command", command), ("cwd", cwd)) if v})
    return {"name": name, "content": "\n".join(p for p in content_parts if p),
            "is_error": is_error, "arguments": arguments}


def _codex_item_completed(run: _Run, st: _CodexSynth, payload) -> list[str]:
    """item/completed → surface/工具事件合成(纯 dict 构造,测试可喂假 Notification)。"""
    item = _codex_item(payload.item)
    itype = _codex_item_type(item)
    item_id = getattr(item, "id", None) or ""
    if itype == "agentMessage":
        pieces = st.agent_pieces.get(item_id) or []
        text = "".join(pieces) or (getattr(item, "text", None) or "")
        if not pieces and text:
            run.text(text)  # 兜底:无增量事件(流缺 chunk)→ 整块一次(cc AssistantMessage 兜底对齐)
        message = {"role": "assistant", "content": [{"type": "text", "text": text}]}
        phase = getattr(item, "phase", None)
        if phase is not None:
            message["content"][0]["phase"] = _codex_val(phase)
        run.event("assistant/message", turn=run.turn, step=run.step, message=message,
                  surfaceOp="append", sourceSeqs=list(run._chunk_seqs))
        return [text] if not pieces and text else []
    if itype in ("dynamicToolCall", "mcpToolCall", "commandExecution"):
        st.tool_round_open = True
        info = _codex_tool_result(item, itype)
        call_id = item_id
        run.tool_call(call_id, info["name"], info["arguments"])
        run.tool_result(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                          "content": info["content"], "is_error": info["is_error"]}]},
            call_id=call_id, name=info["name"], is_error=info["is_error"],
            src_seq=run._call_seqs.get(call_id),
        )
        return []
    if itype == "reasoning":
        # thinking 已逐条流式化(见 reasoning/textDelta);仅无 delta 的项整块兜底一次
        content = getattr(item, "content", None) or []
        if content and item_id not in st.reasoning_seen:
            run.chunk({"type": "thinking", "index": 0, "text": "\n".join(content)})
        return []
    if itype == "plan":
        text = getattr(item, "text", None) or ""
        if text and item_id not in st.plan_items:
            st.plan_items.add(item_id)
            run.chunk({"type": "plan", "index": 0, "text": text})
        return []
    return []  # userMessage 已由 run.user_message 合成;fileChange/webSearch/子代理等 v1 静默跳过


def _handle_codex_notification(run: _Run, st: _CodexSynth, notif) -> list[str]:
    """codex 通知 → gh TAXONOMY 事件合成(纯鸭子读取,测试可喂假 Notification)。

    返回本事件产生的文本增量(供 codex_stream yield);codex 无 seq,顺序即通知流顺序;
    turn/step 生命周期由 run.start / step_boundary 合成(prologue 同 cc),codex 的
    turn/started|completed 只贡献 stop_reason / 失败判定。
    """
    method = getattr(notif, "method", "")
    payload = getattr(notif, "payload", None)
    if method == "turn/started":
        turn = getattr(payload, "turn", None)
        st.turn_id = getattr(turn, "id", None)
        return []
    if method == "turn/completed":
        st.saw_turn_completed = True
        turn = getattr(payload, "turn", None) or {}
        kind = _codex_val(getattr(turn, "status", None))
        run.result_stop_reason = kind if isinstance(kind, str) else None
        if kind != "completed":
            error = getattr(turn, "error", None) or {}
            detail = getattr(error, "message", None) or kind
            raise RuntimeError(f"agent 执行失败: {detail}")
        return []
    if method == "item/started":
        item = _codex_item(getattr(payload, "item", None))
        itype = _codex_item_type(item)
        if itype == "agentMessage":
            st.agent_pieces.setdefault(getattr(item, "id", None) or "", [])
        if itype in ("agentMessage", "reasoning", "plan") and st.tool_round_open:
            st.tool_round_open = False
            run.step_boundary()  # 工具结果后新一轮 LLM 请求 → 新 step(单次翻转,聚合并行工具)
        return []
    if method == "item/agentMessage/delta":
        text = getattr(payload, "delta", None) or ""
        if not text:
            return []
        st.agent_pieces.setdefault(getattr(payload, "item_id", None) or "", []).append(text)
        run.text(text)
        return [text]
    if method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
        delta = getattr(payload, "delta", None) or ""
        if delta:
            st.reasoning_seen.add(getattr(payload, "item_id", None) or "")
            index = getattr(payload, "content_index", None)
            run.chunk({"type": "thinking", "index": index if index is not None else 0,
                       "text": delta})
        return []
    if method == "item/completed":
        return _codex_item_completed(run, st, payload)
    if method == "thread/tokenUsage/updated":
        usage = getattr(payload, "token_usage", None) or {}
        breakdown = getattr(usage, "total", None) or getattr(usage, "last", None)
        if breakdown is not None:
            run.result_usage = _normalize_usage(breakdown)  # 末条为准 → session/end(同 dsh)
        return []
    return []  # plan/delta、outputDelta、progress 等:增量已由项目侧逐条投影或属日志型,v1 不进流


class CodexAdapter(BaseAdapter):
    """codex:SDK 原料通知流 → 本地合成(唯一权威,无投影层;why 见 _CodexSynth docstring)。"""

    provider = "codex"

    async def stream(self, options, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None):
        """Codex(OpenAI Codex SDK)流式应答(监控 + 执行)。

        对外产出:与 cc_stream 同契约 —— 文本增量(item/agentMessage/delta 优先、
        item/completed 整块兜底),turn 非 completed → RuntimeError("agent 执行失败: ...");
        thinking/plan/工具增量只进事件流,不改变产出。codex 通知 1:1 合成 TAXONOMY
        (无 seq 编号 → 本地合成,见 _CodexSynth);session/turn/step 生命周期由本层合成。

        options = 调用方组装的鸭子类型配置(仿 dsh 的 options 透传,字段见
        _codex_field_* 白名单):codex_home/sandbox(full_access 缺省态在 deepwiki 侧
        定)/approval_mode/model/system_prompt → base_instructions/token/env/cwd/
        codex_bin/config_overrides/launch_args_override/effort/output_schema 等;
        **codex_home 缺省回退内置隔离目录**(codex_home_path:config.toml 仅 graphify +
        auth 引导复制,隔离语义同 dsh_cordis_path),显式提供则全权交上层。

        凭证(cc 同形:环境隔离不隔离凭证通道 —— 隔离只管 config.toml/sessions 设置面,
        凭证明细见 _codex_home_setup):零配置缺省符号链接引用真实 ~/.codex/auth.json
        (如 cc 复用 claude CLI 本地登录);显式 options.token → login_api_key 写本隔离
        home(断链后落盘,不碰用户真实凭证文件);两者皆无且无本地凭证则认证失败由 SDK 报错。
        """
        from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, JsonRpcError, Sandbox

        run = self._run(model=getattr(options, "model", None) or "",
                        session=session, session_ns=session_ns, run_id=run_id,
                        session_name=session_name, meta=meta)
        run.start(context=context, retry=retry)
        run.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        run.event("request/header", header=_codex_header(options), reason="initial", partial=True)
        st = _CodexSynth(run, prompt)
        home = getattr(options, "codex_home", None) or codex_home_path()
        home = _codex_home_setup(home, graphify_command=getattr(options, "graphify_command", None))

        fields = _codex_config_fields(options)
        env = dict(getattr(options, "env", None) or {})
        env["CODEX_HOME"] = home  # SDK 从不设 CODEX_HOME;隔离凭它生效(见 codex_home_path)
        fields["env"] = env
        config = CodexConfig(**fields)

        thread_fields = _codex_thread_fields(options)
        if (sandbox := getattr(options, "sandbox", None)) is not None:
            thread_fields["sandbox"] = _codex_lookup(sandbox, Sandbox, "Sandbox")
        if (approval := getattr(options, "approval_mode", None)) is not None:
            thread_fields["approval_mode"] = _codex_lookup(approval, ApprovalMode, "approval_mode")
        token = getattr(options, "token", None) or ""
        timeout = getattr(options, "timeout_seconds", None)

        guard = self._guard(run, error_stage=lambda exc: _codex_stage(exc, (JsonRpcError,)))
        async with guard, AsyncCodex(config=config) as codex:
            if token:
                # 显式 token → 登录凭证属本隔离 home:先断符号链接防穿透写坏用户 ~/.codex/auth.json
                auth = Path(home) / "auth.json"
                if auth.is_symlink():
                    auth.unlink()
                await codex.login_api_key(token)
            thread = await codex.thread_start(**thread_fields)
            handle = await thread.turn(prompt, **_codex_turn_fields(options))

            async def _consume():
                async for notif in handle.stream():
                    for delta in _handle_codex_notification(run, st, notif):
                        yield delta
                if not st.saw_turn_completed:
                    raise RuntimeError("agent 执行失败: turn 未收到完成事件")

            if timeout is not None:
                async with asyncio.timeout(timeout):  # 兜底 review/approval 等待挂流
                    async for chunk in _consume():
                        yield chunk
            else:
                async for chunk in _consume():
                    yield chunk


# ---------------------------------------------------------------------------
# 模块级绑定:公开名与签名逐参数等于历史自由函数(调用方按名导入不受影响)
# ---------------------------------------------------------------------------

_claude = ClaudeCodeAdapter()
_openai = OpenAIAdapter()
_dsh = DshAdapter()
_codex = CodexAdapter()

cc_stream = _claude.stream
cc_text = _claude.text
cc_result = _claude.result

llm_complete = _openai.complete
llm_stream = _openai.stream

dsh_stream = _dsh.stream
dsh_text = _dsh.text
dsh_result = _dsh.result

codex_stream = _codex.stream
codex_text = _codex.text
codex_result = _codex.result

# 扩展发现口(元数据,非运行时强制分派:deepwiki/evaluators 仍按名导入)
ADAPTERS: dict[str, BaseAdapter] = {a.provider: a for a in (_claude, _openai, _dsh, _codex)}
