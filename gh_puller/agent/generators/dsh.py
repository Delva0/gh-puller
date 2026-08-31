"""dsh:DeepSeek Harness(SDK)包装 —— 配置世界(DshConfig/挂载映射/内置隔离组合)+ 投影适配器。

本文件 = dsh 的独立扩展点(双权威投影 = 对齐器;why 见 _DshProj docstring);
DshConfig → DeepSeekHarness kwargs(dsh_fields:config_path → cordis、system_prompt
→ env.DSH_SYSTEM_PROMPT;未提供 cordis 时回退内置隔离组合),模型/凭证随组合配置
(SDK 读进程环境兜底)。隔离组合装配(dsh_cordis_path,镜像官方 minimal.cordis.yml,
零用户级补丁)同文件 —— 组合即隔离边界;SDK 类型仅函数内懒导入(测试经假模块
注入),模块 import 面零 SDK。
"""

import asyncio
import contextlib
import hashlib
import tempfile
from pathlib import Path
from typing import TypedDict

from ..events import TAXONOMY, EventRecorder, _normalize_usage
from .base import BaseGenerator
from .utils import RequestFailedError, _stage_of

# ---------------------------------------------------------------------------
# config 类型(每生成器一种 dict schema;键可省略,键集即解析层白名单语义)
# ---------------------------------------------------------------------------


class DshConfig(TypedDict, total=False):
    """dsh 运行时 config:映射 DeepSeekHarness kwargs(config_path → cordis,system_prompt → env.DSH_SYSTEM_PROMPT)。"""

    provider: str
    model: str
    max_tokens: int
    cwd: str
    runtime_cwd: str
    session_root: str
    env: dict
    config_path: str
    mcp_servers: list[dict]
    base_url: str
    api_key: str
    runtime_bin: str
    launch_args_override: list[str]
    request_timeout_seconds: float
    shutdown_timeout_seconds: float


# ---------------------------------------------------------------------------
# SDK 字段映射(config dict → SDK 构造 kwargs;None 跳过 → 走 SDK 缺省)
# ---------------------------------------------------------------------------


def dsh_fields(config: dict) -> dict:
    """DshConfig → DeepSeekHarness 构造 kwargs(概念键映射 + 缺省隔离组合)。

    DeepSeekHarnessConfig 是 dataclass 而非 pydantic(无 model_dump),且
    DeepSeekHarness.__init__(config=None, **kwargs) 同时传 config 与 kwargs 会
    TypeError —— 恒走 kwargs;调用方自组装 config(键集见 DshConfig)。
    config_path → SDK cordis;system_prompt → env.DSH_SYSTEM_PROMPT(已有 env 键
    优先);未提供 cordis 时经 mcp_servers 描述回退内置隔离组合(见 dsh_cordis_path)。
    """
    names = ("provider", "model", "max_tokens", "cwd", "runtime_cwd", "session_root",
             "env", "runtime_bin", "launch_args_override",
             "request_timeout_seconds", "shutdown_timeout_seconds", "base_url", "api_key")
    fields = {k: v for k, v in ((k, config.get(k)) for k in names) if v is not None}
    if config.get("config_path"):
        fields["cordis"] = config["config_path"]
    if config.get("system_prompt"):
        env = dict(fields.get("env") or {})
        env.setdefault("DSH_SYSTEM_PROMPT", config["system_prompt"])
        fields["env"] = env
    fields.setdefault("cordis", dsh_cordis_path(config.get("mcp_servers")))
    return fields


# ---------------------------------------------------------------------------
# config → SDK 对象装配(dict 到 SDK 类型/单次运行件;懒导入,测试喂假模块)
# ---------------------------------------------------------------------------


def dsh_harness(config: dict):
    """DshConfig → DeepSeekHarness 实例(懒导入;kwargs 映射见 dsh_fields)。"""
    from deepseek_harness import DeepSeekHarness  # lazy:测试可喂假模块

    return DeepSeekHarness(**dsh_fields(config))


# ---------------------------------------------------------------------------
# dsh 内置隔离组合装配(cordis 文件;SDK/JSON-RPC 只读这一个文件,无用户级 patch 层)
# ---------------------------------------------------------------------------

_DSH_CORDIS_FILE: str | None = None  # 内容哈希键控缓存(每次调用少写盘;内容变了自然换文件名)


def dsh_cordis_path(mcp_servers: list[dict] | None = None) -> str:
    """dsh 内置隔离组合文件路径(DSH_CORDIS_CONFIG 只认文件路径;按内容哈希落盘一次)。

    组合蓝图见下方 `_DSH_CORDIS_YAML`(镜像 dsh 官方 minimal.cordis.yml;SDK/
    JSON-RPC 路径只读这一个文件,无用户级 patch 层/无 XDG 配置 —— 组合即隔离
    边界)。语义同 cc `setting_sources=[]` 的默认位:上层不传 cordis(None)时,
    回退本组合(默认隔离);上层显式提供 cordis(如组合文件缺省路径)则全权交给
    上层(隔离与否由上层组合保证)。
    mcp_servers = 调用方经 config 注入的通用工具桌描述(附加 mcp 服务器段)。
    """
    global _DSH_CORDIS_FILE
    if mcp_servers is None:
        if _DSH_CORDIS_FILE is None:
            _DSH_CORDIS_FILE = _dsh_cordis_write(None)
        return _DSH_CORDIS_FILE
    return _dsh_cordis_write(mcp_servers)  # 带工具桌:按内容哈希直写(不占默认缓存)


def _dsh_cordis_write(mcp_servers: list[dict] | None) -> str:
    """最小隔离组合 + 可选附加 mcp 服务器段(通用描述,内容哈希落盘一次)。

    调用方(上层装配层)以 mcp_servers=[{"id","serverName","command","args",...}]
    注入引擎工具桌 —— 本层只按通用结构渲染,不认识任何具体工具名。
    """
    # sha1 仅作内容指纹派生缓存文件名,非安全用途
    digest = hashlib.sha1(_DSH_CORDIS_YAML.encode()).hexdigest()[:8]  # noqa: S324
    text = _DSH_CORDIS_YAML
    for spec in mcp_servers or []:
        text = _dsh_mcp_section(spec) + text
        digest = hashlib.sha1((digest + repr(sorted(spec.items()))).encode()).hexdigest()[:8]  # noqa: S324
    path = Path(tempfile.gettempdir()) / "gh-puller" / f"dsh-cordis-{digest}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    return str(path)


def _dsh_mcp_section(spec: dict) -> str:
    """通用 mcp 服务器描述 → 组合 YAML 段(dsh-mcp-client 行,零具体工具名)。"""
    args = " ".join(f"'{a}'" for a in spec.get("args") or [])
    return (
        f"- id: {spec.get('id', 'mcp-server')}\n"
        f"  name: '@deepseek-ai/dsh-mcp-client'\n"
        f"  config:\n"
        f"    serverName: {spec.get('serverName', '')}\n"
        f"    transport: stdio\n"
        f"    command: {spec.get('command', 'python3')}\n"
        f"    args: [{args}]\n"
        f"    cwd: !!js process.env.DSH_CWD ?? process.cwd()\n"
        f"    failOnStartupError: true\n"
        f"    reconnect:\n      enabled: false\n"
    )


# 组合层隔离逐项(cordis.yml 之外再无配置注入面;余下环境残留 DSH_HOME/之类
# 无任何已装载组件读取 —— SDK 路径无 home patch/settings/credentials 装载):
# workspaceContext(禁 $DSH_HOME/AGENTS.md 与 checkout 的 AGENTS.md/CLAUDE.md 链)、
# skills(禁用户 $DSH_HOME/skills、$DSH_AGENTS_HOME/skills、项目 .dsh/skills 与
# 捆绑技能目录)、includeHarnessIdentity/includeRuntimeContext(提示词注入)、
# toolBash/toolJobs/goals(面模型工具域)。附加工具桌(引擎 MCP 服务器)由调用方经
# mcp_servers 通用描述注入(_dsh_mcp_section 渲染;无 .mcp.json/全局发现)。
# hooks 未装载(配置文件型,须显式挂)。
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
- id: sessions
  name: '@deepseek-ai/dsh-session-persistence-jsonl'
  config:
    root: !!js process.env.DSH_SESSION_ROOT ?? './.sessions'
    compression: none
"""


# ---------------------------------------------------------------------------
# DeepSeek Harness(SDK)投影全家(模块级:纯 dict 构造,测试可喂假 Notification)
# ---------------------------------------------------------------------------


def _dsh_session_id(session: str) -> str:
    """gh session id → dsh session_id:取最后一个 '/' 之后(与 FileSink 文件名规则一致)。"""
    return (session or "agent").rsplit("/", 1)[-1]


def _dsh_stage(exc: Exception, protocol_errors: tuple) -> str:
    """dsh 异常 stage:协议解析失败(SdkProtocolError/JsonRpcError)归 parse,其余沿 _stage_of。

    protocol_errors = 函数内 lazy import 的错误类元组(测试经假模块注入)。
    """
    if isinstance(exc, protocol_errors):
        return "parse"
    return _stage_of(exc)


class _DshProj:
    """dsh 会话事件 → gh 事件流的投影状态(单次 stream 调用一个实例)。

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

    def __init__(self, event_recorder: EventRecorder, prompt: str, session_id: str):
        self.event_recorder = event_recorder
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
        """执行一次恰好发布单事件的 action(如 event_recorder.text/tool_call),记录 dsh_seq → gh seq。

        bus disabled 时 event_recorder.seq 不增长(事件不构造)→ 不记录映射,防悬空 seq;
        返回 gh seq(未发布返回 None)。
        """
        before = self.event_recorder.seq
        action()
        if self.event_recorder.seq > before and dsh_seq is not None:
            self.seq_map[dsh_seq] = self.event_recorder.seq - 1
            return self.event_recorder.seq - 1
        return None

    def forward(self, dsh_seq: int, evt_type: str, **data) -> dict | None:
        """发布 gh 事件并记录 seq 映射;返回事件(无 sink 时 None)。"""
        evt = self.event_recorder.event(evt_type, **data)
        if evt is not None and dsh_seq is not None:
            self.seq_map[dsh_seq] = evt["seq"]
        return evt

    def source_seqs(self, envelope: dict) -> list[int]:
        """dsh sourceEventSeqs → gh sourceSeqs(经 seq_map 映射;未映射者丢弃)。"""
        return [self.seq_map[s] for s in (envelope.get("sourceEventSeqs") or [])
                if s in self.seq_map]


def _project_dsh_chunk(event_recorder: EventRecorder, proj: _DshProj, dsh_seq, chunk: dict) -> list[str]:
    """dsh StreamChunk → gh assistant/chunk 增量投影(逐条),返回文本增量。

    文本增量走 event_recorder.text(计入 text_chars 且 yield);thinking/tool_input 只进
    事件流不改变产出(与 cc 漏斗一致);tool-call 收尾经 block-end 的完整
    arguments 合成 tool/call(整串优先,缺失时拼 delta 碎片)。
    """
    ctype = chunk.get("type")
    if ctype == "text-delta":
        text = chunk.get("text") or ""
        proj.track(dsh_seq, lambda: event_recorder.text(text, index=chunk.get("index", 0)))
        return [text] if text else []
    if ctype == "reasoning-delta":
        proj.track(dsh_seq, lambda: event_recorder.chunk({"type": "thinking", "index": chunk.get("index", 0),
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
        proj.track(dsh_seq, lambda: event_recorder.chunk({"type": "tool_call", "index": idx,
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
            gh_seq = proj.track(dsh_seq, lambda: event_recorder.tool_call(call_id, name, args or ""))
            if gh_seq is not None and call_id:
                proj.synth[call_id] = gh_seq
        return []
    if ctype == "usage":
        usage = chunk.get("usage")
        if usage:
            event_recorder.result_usage = _normalize_usage(usage)
        return []
    if ctype == "finish":
        reason = chunk.get("reason")
        kind = reason.get("kind") if isinstance(reason, dict) else reason
        if kind:
            proj.last_finish_kind = kind
            event_recorder.result_stop_reason = kind
        return []
    return []  # block-start 与未知块:信息无需事件(增量已逐条投影)


def _project_dsh_event(event_recorder: EventRecorder, proj: _DshProj, notif) -> list[str]:
    """dsh 通知 → gh TAXONOMY 事件投影(纯 dict 构造,测试可喂假 Notification)。

    返回本事件产生的文本增量(供 stream yield)。规则:
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
        event_recorder.user_message({"role": "user",
                          "content": [{"type": "text", "text": proj.prompt}]})

    if evt_type == "turn/start":
        event_recorder.turn = data.get("turn", event_recorder.turn)
        proj.forward(dsh_seq, "turn/start", turn=event_recorder.turn)
    elif evt_type == "step/start":
        event_recorder.turn = data.get("turn", event_recorder.turn)
        event_recorder.step = data.get("step", event_recorder.step)
        proj.tool_pieces = {}  # 块 index 每 step 从 0 重新计数
        event_recorder._step_open = True  # 崩溃路径(无 step/end)epilogue 有可合的 step/end
        proj.forward(dsh_seq, "step/start", turn=event_recorder.turn, step=event_recorder.step)
    elif evt_type == "step/end":
        event_recorder.turn = data.get("turn", event_recorder.turn)
        event_recorder.step = data.get("step", event_recorder.step)
        event_recorder._step_open = False
        proj.forward(dsh_seq, "step/end", turn=event_recorder.turn, step=event_recorder.step)
    elif evt_type == "turn/end":
        event_recorder.turn = data.get("turn", event_recorder.turn)
        proj.saw_turn_end = True
        reason = data.get("reason")
        kind = reason.get("kind") if isinstance(reason, dict) else reason
        detail = {"turn": event_recorder.turn, "reason": kind}
        if isinstance(reason, dict):
            rest = {k: v for k, v in reason.items() if k != "kind"}
            if rest:
                detail["detail"] = rest  # 结构化失败细目透传(UI 排查素材)
        if not proj.last_finish_kind and kind:
            event_recorder.result_stop_reason = kind  # 无 finish chunk 的兜底 source
        proj.forward(dsh_seq, "turn/end", **detail)
    elif evt_type == "user/message":
        proj.saw_user_message = True
        message = {"role": data.get("role", "user"), "content": data.get("content") or []}
        proj.track(dsh_seq, lambda: event_recorder.user_message(
            message, source=data.get("source"),
            surface_op=envelope.get("surfaceOp") or "append"))
    elif evt_type == "assistant/chunk":
        event_recorder.turn = data.get("turn", event_recorder.turn)
        event_recorder.step = data.get("step", event_recorder.step)
        return _project_dsh_chunk(event_recorder, proj, dsh_seq, data.get("chunk") or {})
    elif evt_type == "assistant/message":
        event_recorder.turn = data.get("turn", event_recorder.turn)
        event_recorder.step = data.get("step", event_recorder.step)
        evt_data = {
            "turn": event_recorder.turn, "step": event_recorder.step,
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
            event_recorder.result_usage = _normalize_usage(data["usage"])  # 末条为准 → session/end
    elif evt_type == "tool/call":
        event_recorder.turn = data.get("turn", event_recorder.turn)
        event_recorder.step = data.get("step", event_recorder.step)
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
            proj.track(dsh_seq, lambda: event_recorder.tool_call(call_id, name, data.get("arguments") or ""))
    elif evt_type == "tool/result":
        event_recorder.turn = data.get("turn", event_recorder.turn)
        event_recorder.step = data.get("step", event_recorder.step)
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
            "turn": event_recorder.turn, "step": event_recorder.step,
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


def _dsh_worker(harness, prompt: str, session_id: str, pump):
    """同步线程体:在生成器 __aenter__ 已进入的 harness 上阻塞 run() 至 idle。

    整个体在 executor 线程执行;即使外层 asyncio task 被取消(消费者提前退场),
    线程仍会自然跑完 —— 回收经 Dsh._exit(提前退场时 detach 到后台任务等待线程
    跑完再退出 harness;见 _wait_and_exit),不泄漏也不与 run 竞态。
    """
    return harness.run(prompt, session_id=session_id, on_notification=pump)


# ---------------------------------------------------------------------------
# DeepSeek Harness(SDK)包装
# ---------------------------------------------------------------------------


class Dsh(BaseGenerator):
    """dsh: DeepSeek Harness composition. Config shape: file-class — config_path points at a cordis file.

    DshConfig → DeepSeekHarness kwargs (dsh_fields: config_path → cordis,
    system_prompt → env.DSH_SYSTEM_PROMPT; no cordis → built-in isolated composition);
    model/credentials ride the composition (SDK reads the process env as fallback). The
    event vocabulary is same-sourced but dsh is the second authority → _DshProj
    projection alignment (why in _DshProj docstring). Harness object built at
    construction (dsh_harness bound to config): one instance = one harness = one dsh
    session; child spawn/initialize at session enter (_enter hook). dsh's client is a
    synchronous context manager and run() blocks → enter/reap bridged via
    asyncio.to_thread (harness used on an executor thread; premature consumer exit
    detaches reaping to the background, see _wait_and_exit).
    """

    generator = "dsh"
    provider = "deepseek"

    def __init__(self, config: dict):
        super().__init__(config)
        self._harness = dsh_harness(config)
        self._run_task: asyncio.Task | None = None  # 在飞 run(asyncio.to_thread);_exit 回收依据
        self._teardown: asyncio.Task | None = None  # 提前退场时的后台收尸任务(持引用防 GC)
        self._proj: _DshProj | None = None  # 最近一次运行的投影状态(会话 epilogue 读 turn/end)

    async def _enter(self):
        await asyncio.to_thread(self._harness.__enter__)  # 子进程 spawn/initialize(同步 CM,线程桥)

    async def _exit(self, exc):
        task, self._run_task = self._run_task, None
        if task is not None and not task.done():
            # 消费者提前退场:run 仍在 executor 线程(自然跑完)。收尸交给后台任务:
            # 等线程结束再 __exit__ —— 不绊住消费者,也不与 run 竞态。
            self._teardown = asyncio.create_task(self._wait_and_exit(task, exc))
            return
        await asyncio.to_thread(self._harness.__exit__, *exc)

    async def _wait_and_exit(self, task: asyncio.Task, exc):
        """Reap the harness after the thread-side run ends naturally (background; waits even on cancel)."""
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.to_thread(self._harness.__exit__, *exc)

    @contextlib.asynccontextmanager
    async def session(self, **kw):
        """dsh session: protocol errors → parse; epilogue per projected turn/end.

        Dual authority: dsh owns the final, teardown supplements on crash paths only.
        """
        from deepseek_harness import errors as _dsh_errors

        async with super().session(
            error_stage=lambda exc: _dsh_stage(exc, (_dsh_errors.SdkProtocolError,
                                                     _dsh_errors.JsonRpcError)),
            epilogue=lambda: not bool(self._proj and self._proj.saw_turn_end),
            prologue=False,  # dsh 自带 turn/step 生命周期
            **kw):
            yield

    def _run_assembly(self, prompt: str) -> tuple[EventRecorder, _DshProj, asyncio.Queue]:
        """Assemble per-run parts for stream/result (proj on the instance; worker not cancelled here)."""
        event_recorder = self._require_event_recorder()
        proj = _DshProj(event_recorder, prompt, _dsh_session_id(event_recorder.session))
        self._proj = proj
        queue: asyncio.Queue = asyncio.Queue()  # 无界:1:1 于运行时事件流,永不阻塞发布侧
        loop = asyncio.get_running_loop()

        def pump(notif) -> None:  # 运行时线程回调:跨线程入队(loop 已关闭竞态静默丢)
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, ("notif", notif))

        async def _worker() -> None:
            try:
                result = await asyncio.to_thread(_dsh_worker, self._harness, prompt, proj.session_id, pump)
                loop.call_soon_threadsafe(queue.put_nowait, ("done", result))
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("exc", exc))

        self._run_task = asyncio.create_task(_worker())
        return event_recorder, proj, queue

    async def stream(self, prompt: str):
        """Project dsh native events 1:1; yield assistant text deltas.

        Turn non-completed → RequestFailedError; thinking/tool increments only to the
        event stream. SDK run() is sync-blocking → asyncio.to_thread (one turn to_idle
        per run, no per-prompt cancellation).
        """
        event_recorder, proj, queue = self._run_assembly(prompt)
        while True:
            kind, item = await queue.get()
            if kind == "notif":
                for delta in _project_dsh_event(event_recorder, proj, item):
                    yield delta
            elif kind == "exc":
                raise item
            else:  # ("done", RunResult):run() 返回前所有通知已交付,无竞态
                if item.finish_reason != "completed":
                    raise RequestFailedError(item.finish_reason)
                break

    async def result(self, prompt: str) -> str:
        """Return the final output: RunResult.final_response; non-completed/no output → RequestFailedError."""
        event_recorder, proj, queue = self._run_assembly(prompt)
        while True:
            kind, item = await queue.get()
            if kind == "notif":
                for _ in _project_dsh_event(event_recorder, proj, item):
                    pass
            elif kind == "exc":
                raise item
            else:
                if item.finish_reason != "completed":
                    raise RequestFailedError(item.finish_reason)
                final = item.final_response or ""
                if not final:
                    raise RequestFailedError("未产出最终结果")
                return final
