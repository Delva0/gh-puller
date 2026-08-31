"""codex:OpenAI Codex SDK 包装 —— 配置世界(CodexConfig/字段映射/隔离 home)+ 适配器本体。

本文件 = codex 的独立扩展点(与 cc 同构的合成器,无投影层;why 见 _CodexSynth
docstring);CodexConfig → SDK 装配件(codex_home/codex_config/codex_thread/
codex_turn),config_path 纯透传(home config.toml 符号链接),mcp_servers 通用注入
工具桌,隔离 home 装配(codex_home_path / codex_home_setup,零用户级配置)同文件
—— home 即隔离边界;SDK 类型仅函数内懒导入(测试经假模块注入),模块 import
面零 SDK。
"""

import asyncio
import contextlib
import json
import shutil
from pathlib import Path
from typing import Any, TypedDict

from ..events import EventRecorder, _normalize_usage
from .base import BaseGenerator
from .utils import RequestFailedError, _stage_of

# ---------------------------------------------------------------------------
# config 类型(每生成器一种 dict schema;键可省略,键集即解析层白名单语义)
# ---------------------------------------------------------------------------


class CodexConfig(TypedDict, total=False):
    """codex 运行时 config:映射 CodexConfig/thread/turn kwargs;system_prompt → base_instructions。"""

    model: str
    system_prompt: str
    cwd: str
    codex_bin: str
    codex_home: str
    config_path: str
    sandbox: str
    approval_mode: str
    token: str
    env: dict
    timeout_seconds: float
    mcp_servers: list[dict]
    allowed_tools: list[str]
    effort: str
    output_schema: dict
    config_overrides: dict
    launch_args_override: list[str]
    base_instructions: str
    service_tier: str
    summary: dict
    web_search: bool


# ---------------------------------------------------------------------------
# SDK 字段映射(config dict → SDK 构造 kwargs;None 跳过 → 走 SDK 缺省)
# ---------------------------------------------------------------------------


def codex_config_fields(config: dict) -> dict:
    """CodexConfig → CodexConfig(SDK)构造 kwargs(None 跳过 → 走 SDK 缺省)。

    与 dsh_fields 同法:调用方自组装 config(键集见 CodexConfig);env 由调用流
    合并 CODEX_HOME 后整传入 —— SDK 从不自设 CODEX_HOME,隔离点见
    codex_home_path / codex_home_setup。
    """
    names = ("cwd", "codex_bin", "config_overrides", "launch_args_override", "env")
    return {k: v for k, v in ((k, config.get(k)) for k in names) if v is not None}


def codex_thread_fields(config: dict) -> dict:
    """CodexConfig → AsyncCodex.thread_start 透传 kwargs。

    sandbox/approval_mode 由调用方经 codex_lookup 转换后塞入,此处只保留其余自由
    字段;system_prompt 缺省映射 base_instructions(与 cc 的 system_prompt 同位语义)。
    """
    names = ("base_instructions", "developer_instructions", "personality", "ephemeral",
             "model", "model_provider", "service_tier", "config", "cwd",
             "session_start_source", "thread_source")
    fields = {k: v for k, v in ((k, config.get(k)) for k in names) if v is not None}
    if config.get("system_prompt") and "base_instructions" not in fields:
        fields["base_instructions"] = config["system_prompt"]
    return fields


def codex_turn_fields(config: dict) -> dict:
    """CodexConfig → AsyncThread.turn 透传 kwargs。"""
    names = ("cwd", "effort", "output_schema", "personality", "service_tier", "summary")
    return {k: v for k, v in ((k, config.get(k)) for k in names) if v is not None}


# ---------------------------------------------------------------------------
# config → SDK 对象装配(dict 到 SDK 类型/单次运行件;懒导入,测试喂假模块)
# ---------------------------------------------------------------------------


def codex_config(config: dict):
    """CodexConfig → SDK CodexConfig 实例(懒导入)。

    codex_config_fields 的 env 合并 CODEX_HOME(SDK 从不自设;隔离凭它生效,见
    codex_home_path)。
    """
    from openai_codex import CodexConfig  # lazy:测试可喂假模块

    fields = codex_config_fields(config)
    env = dict(config.get("env") or {})
    env["CODEX_HOME"] = codex_home(config)
    fields["env"] = env
    return CodexConfig(**fields)


def codex_thread(config: dict) -> dict:
    """CodexConfig → AsyncCodex.thread_start kwargs。

    sandbox/approval_mode 经 codex_lookup 转枚举;其余见 codex_thread_fields。
    """
    from openai_codex import ApprovalMode, Sandbox  # lazy:测试可喂假模块

    fields = codex_thread_fields(config)
    if (sandbox := config.get("sandbox")) is not None:
        fields["sandbox"] = codex_lookup(sandbox, Sandbox, "Sandbox")
    if (approval := config.get("approval_mode")) is not None:
        fields["approval_mode"] = codex_lookup(approval, ApprovalMode, "approval_mode")
    return fields


def codex_turn(config: dict) -> dict:
    """CodexConfig → AsyncThread.turn kwargs(见 codex_turn_fields)。

    summary 缺省 detailed:CLI 侧 summary=auto 经模型元数据 default_reasoning_summary
    "none" 解析(全模型默认不产可见推理摘要)→ 事件流无 thinking。观测者原则是
    agent 发生什么就落盘什么——显式打开可见推理摘要流;显式 "none"/"auto"/"concise"
    仍尊重。
    """
    fields = codex_turn_fields(config)
    fields.setdefault("summary", "detailed")
    return fields


def codex_home(config: dict) -> str:
    """CodexConfig → 隔离 home(codex_home_setup:config.toml + auth 引导)。"""
    return codex_home_setup(config.get("codex_home") or codex_home_path(),
                            config_path=config.get("config_path"),
                            mcp_servers=config.get("mcp_servers"),
                            web_search=config.get("web_search") or False)


# ---------------------------------------------------------------------------
# 枚举归一(config 的 Sandbox/ApprovalMode 名/值/枚举成员 → SDK 枚举)
# ---------------------------------------------------------------------------


def codex_val(x):
    """枚举成员 → 值(pydantic/codex 枚举 .value;普通值原样)—— 状态比较统一走值。"""
    return getattr(x, "value", x)


def codex_lookup(v, enum_cls, label):
    """config 的 Sandbox/ApprovalMode(名字/值/枚举成员)→ SDK 枚举;None → None。

    调用方常传字符串("full_access" / "auto_review");高级用户可传 SDK 枚举。
    名字与值双匹配 —— 防 str 子类枚举当 str 用后按名匹配失真。
    """
    if v is None:
        return None
    if isinstance(v, enum_cls):
        return v
    raw = codex_val(v)
    for member in enum_cls:
        if raw in (member.name, member.value):
            return member
    raise ValueError(f"codex {label} 取值非法(可选 {[m.name for m in enum_cls]}): {v!r}")


# ---------------------------------------------------------------------------
# codex 隔离 home 装配(CODEX_HOME 目录即隔离边界;零用户级 config.toml/凭证读绕过)
# ---------------------------------------------------------------------------


def codex_home_path() -> str:
    """codex 隔离 home 路径(缺省 ~/.gh-puller/codex-home;显式经 config.codex_home)。

    与 dsh_cordis_path 同位语义:app-server 只读本目录(CODEX_HOME 下 config.toml /
    auth.json / sessions),不读用户 ~/.codex —— 目录即隔离边界;目录稳定(会话持久,
    thread_resume 可用),区别于 dsh_cordis 的 temp+内容哈希(文件名固定 config.toml,
    无法按内容换名,见 codex_home_setup 的内容比对改写)。
    """
    return str(Path.home() / ".gh-puller" / "codex-home")


def _codex_config_toml_content(*, mcp_servers: list[dict] | None = None,
                               web_search: bool = False) -> str:
    """隔离 config.toml 内容:按调用方注入的通用 mcp 服务器描述渲染(组合即隔离边界,镜像 dsh 的 _DSH_CORDIS_YAML)。

    无用户 model_provider/keys/mcp/hook/高级设置。每条 spec 应带 id/command/args/
    env_vars(值不内联,每次运行经 CodexConfig.env 注入 app-server 进程环境,再由
    rmcp stdio 启动器按白名单取走)。mcp_servers 空 → 空 config(无附加工具桌);
    本层不识别任何具体工具名。web_search = True → [features] web_search_request
    (Codex 内置网络搜索工具;CLI 默认出于安全关闭,须显式启用 —— 产物隔离边界内的
    用户级开关)。
    """
    sections = []
    for spec in mcp_servers or []:
        name = spec.get("id", "mcp-server")
        cmd = json.dumps(spec.get("command", ""))
        args = json.dumps(spec.get("args", []))
        env_vars = json.dumps(spec.get("env_vars", []))
        sections.append(
            f"[mcp_servers.{name}]\n"
            f"command = {cmd}\n"
            f"args = {args}\n"
            f"env_vars = {env_vars}\n"
            "startup_timeout_sec = 30\n"
            "required = true\n",
        )
    if web_search:
        sections.append("[features]\nweb_search_request = true\n")
    return "\n".join(sections)


def codex_home_setup(home, *, auth_src: str | Path | None = None,
                     config_path: str | Path | None = None,
                     mcp_servers: list[dict] | None = None,
                     web_search: bool = False) -> str:
    """确保 codex 隔离 home 就绪:config.toml + auth 引导;内容不变不重写。

    config_path(file 类契约,纯透传):home/config.toml 改为指向该文件的**符号链接**
    (与 auth.json 引导同模式:零读取零解析,SDK 经 CODEX_HOME 原生装载所选文件,
    用户全责)。符号链接不可用(windows/文件系统限制)时回落复制。
    缺省(无 config_path):按 mcp_servers(通用描述)渲染 config.toml(空 → 无附加
    工具桌;web_search=True 追加 [features] web_search_request —— user 级开关,
    与 config_path 模式互斥:自定义配置由该文件自定)。
    auth 引导(cc 同形:凭证通道不隔离,隔离只管设置面 —— 见 cc setting_sources=[]):
    home/auth.json 缺失且 auth_src(缺省 ~/.codex/auth.json)存在 → **符号链接**——
    与 cc 的 CLI 自持凭证一样实时引用(重新登录/revoke 即跟随,无副本陈旧);旧副本
    (非符号链接)存在时替换为链接。符号链接不可用(windows/文件系统限制)时回落复制。
    auth_src 传 False 可关闭引导(纯隔离无凭证态);显式 token 走 login_api_key 时
    app-server 自写 home/auth.json(先删符号链接再真写,不冲突)。
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    cfg_path = home / "config.toml"
    if config_path is not None:
        p = Path(config_path)
        if not p.is_file():  # resolve_target 已校验;防御双检
            raise FileNotFoundError(f"codex 配置文件不存在: {p}")
        if cfg_path.is_symlink() or cfg_path.exists():
            cfg_path.unlink()  # 覆盖写穿悬空链接文件(符号链接须先 unlink)
        try:
            cfg_path.symlink_to(p)
        except OSError:  # windows/不可符号链接 → 复制兜底
            shutil.copyfile(p, cfg_path)
        return str(home)
    content = _codex_config_toml_content(mcp_servers=mcp_servers, web_search=web_search)
    if not cfg_path.exists() or cfg_path.is_symlink() \
            or cfg_path.read_text(encoding="utf-8") != content:
        if cfg_path.is_symlink():  # 上一 config_path 世代残留:必须 unlink 防写穿
            cfg_path.unlink()
        cfg_path.write_text(content, encoding="utf-8")
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


# ---------------------------------------------------------------------------
# 适配器:codex 通知流 → 事件 dict(纯鸭子读取,测试可喂假 Notification;合成无投影)
# ---------------------------------------------------------------------------


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


class _CodexSynth:
    """codex 通知 → gh 事件流的合成状态(单次 stream 调用一个实例)。

    为什么是合成而非投影(对比 _DshProj):codex 通知不携带 seq/turn/step 编号、无
    生命周期事件、无 sourceEventSeqs —— 流顺序是唯一权威(与 cc 同构);合成器只维护
    自洽编号(event_recorder.seq 自己数、turn/step 自开合、tool/call|result 自合成),去重是
    字典/布尔判断,没有第二套编号要伺候。
    """

    def __init__(self, event_recorder: EventRecorder, prompt: str):
        self.event_recorder = event_recorder
        self.prompt = prompt
        self.turn_id: str | None = None  # turn/started 的 turn.id(记录用;路由已由 SDK 按 turn 过滤)
        self.agent_pieces: dict[str, list[str]] = {}  # itemId → agentMessage 增量碎片(去重/消息组装)
        self.reasoning_seen: set[str] = set()  # 已流式化 thinking 的 reasoning itemId(completed 兜底去重)
        self.tool_round_open = False  # 本轮已发 tool/result → 下次 LLM item 开一次 step 边界(聚合并行工具)
        self.plan_items: set[str] = set()  # 已发 plan 文本的 itemId(防 delta/completed 双投)
        self.saw_turn_completed = False
        self.final_response = ""  # 末条 agentMessage 文本(result 终局语义,同 TurnResult)


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
                    or codex_val(getattr(item, "status", None)) == "failed")
        arguments = _codex_args_json(getattr(item, "arguments", None))
    elif itype == "dynamicToolCall":
        name = getattr(item, "tool", None) or ""
        content_parts = [
            getattr(_codex_item(ci), "text", None) or ""
            for ci in (getattr(item, "content_items", None) or [])
            if _codex_item_type(ci) == "inputText"
        ]
        is_error = (getattr(item, "success", None) is False
                    or codex_val(getattr(item, "status", None)) == "failed")
        arguments = _codex_args_json(getattr(item, "arguments", None))
    else:  # commandExecution
        name = "shell"
        content_parts = [getattr(item, "aggregated_output", None) or ""]
        command = getattr(item, "command", None) or ""
        cwd = getattr(item, "cwd", None) or ""
        is_error = (getattr(item, "exit_code", None) not in (None, 0)
                    or codex_val(getattr(item, "status", None)) == "failed")
        # 字段为 LegacyAppPathString(pydantic 路径类型,str 子类)→ 归一为纯 str 再 JSON
        arguments = json.dumps({k: str(v) for k, v in (("command", command), ("cwd", cwd)) if v})
    return {"name": name, "content": "\n".join(p for p in content_parts if p),
            "is_error": is_error, "arguments": arguments}


def _codex_item_completed(event_recorder: EventRecorder, st: _CodexSynth, payload) -> list[str]:
    """item/completed → surface/工具事件合成(纯 dict 构造,测试可喂假 Notification)。"""
    item = _codex_item(payload.item)
    itype = _codex_item_type(item)
    item_id = getattr(item, "id", None) or ""
    if itype == "agentMessage":
        pieces = st.agent_pieces.get(item_id) or []
        text = "".join(pieces) or (getattr(item, "text", None) or "")
        if not pieces and text:
            event_recorder.text(text)  # 兜底:无增量事件(流缺 chunk)→ 整块一次(cc AssistantMessage 兜底对齐)
        if text:
            st.final_response = text  # 末条 agentMessage = result 终局文本
        message = {"role": "assistant", "content": [{"type": "content", "text": text}]}
        phase = getattr(item, "phase", None)
        if phase is not None:
            message["content"][0]["phase"] = codex_val(phase)
        # 块式契约:content 消息 sourceSeqs 只引本 step 的 content 批(不得用到达时刻
        # 累计 _chunk_seqs —— 会把 thinking/plan 批 seqs 一并带进)。
        event_recorder.event("assistant/message", turn=event_recorder.turn, step=event_recorder.step, message=message,
                  surfaceOp="append",
                  sourceSeqs=list(event_recorder._chunk_type_seqs.get("content", [])))
        return [text] if not pieces and text else []
    if itype in ("dynamicToolCall", "mcpToolCall", "commandExecution"):
        st.tool_round_open = True
        info = _codex_tool_result(item, itype)
        call_id = item_id
        event_recorder.tool_call(call_id, info["name"], info["arguments"])
        event_recorder.tool_result(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                          "content": info["content"], "is_error": info["is_error"]}]},
            call_id=call_id, name=info["name"], is_error=info["is_error"],
            src_seq=event_recorder._call_seqs.get(call_id),
        )
        return []
    if itype == "reasoning":
        # thinking 已逐条流式化(见 reasoning/textDelta · summaryTextDelta);仅无 delta
        # 的项整块兜底一次:全量 CoT(content)优先,加密模型仅有摘要(summary);
        # completed 即定型:块式契约 chunk×m → assistant/message(thinking)×1
        # (一条 reasoning 项一条消息,sourceSeqs = 本 step thinking 批 seqs)。
        content = getattr(item, "content", None) or []
        pieces = content or (getattr(item, "summary", None) or [])
        if not pieces:
            return []
        if item_id not in st.reasoning_seen:
            event_recorder.chunk({"type": "thinking", "index": 0, "text": "".join(pieces)})
        event_recorder.event(
            "assistant/message", turn=event_recorder.turn, step=event_recorder.step,
            message={"role": "assistant",
                     "content": [{"type": "thinking", "text": "".join(pieces)}]},
            surfaceOp="append",
            sourceSeqs=list(event_recorder._chunk_type_seqs.get("thinking", [])),
        )
        return []
    if itype == "plan":
        text = getattr(item, "text", None) or ""
        if text and item_id not in st.plan_items:
            st.plan_items.add(item_id)
            event_recorder.chunk({"type": "plan", "index": 0, "text": text})
        return []
    if itype == "webSearch":
        # Codex 内置网络搜索(web_search_request):started 是空壳占位(query=""/action=None),
        # 只认 completed 全字段(item 无 error/status —— 失败由 turn 级表达,见 turn/completed)
        st.tool_round_open = True
        act = _codex_item(getattr(item, "action", None))
        action = None
        if act is not None:
            action = {"type": getattr(act, "type", None) or "other"}
            for k in ("query", "queries", "url", "pattern"):
                v = getattr(act, k, None)
                if isinstance(v, (str, list)) and v:
                    action[k] = v
        arguments = json.dumps({"query": getattr(item, "query", None) or "",
                                **({"action": action} if action else {})},
                               ensure_ascii=False)
        results = getattr(item, "results", None) or []  # opaque JSON(SDK 不保证字段形状)
        content = json.dumps(results, ensure_ascii=False, default=str) if results else ""
        event_recorder.tool_call(item_id, "web_search", arguments)
        event_recorder.tool_result(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": item_id,
                                          "content": content, "is_error": False}]},
            call_id=item_id, name="web_search", is_error=False,
            src_seq=event_recorder._call_seqs.get(item_id),
        )
        return []
    return []  # userMessage 已由 event_recorder.user_message 合成;fileChange/子代理等 v1 静默跳过


def _handle_codex_notification(event_recorder: EventRecorder, st: _CodexSynth, notif) -> list[str]:
    """codex 通知 → gh TAXONOMY 事件合成(纯鸭子读取,测试可喂假 Notification)。

    返回本事件产生的文本增量(供 stream yield);codex 无 seq,顺序即通知流顺序;
    turn/step 生命周期由 event_recorder.start / step_boundary 合成(prologue 同 cc),codex 的
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
        kind = codex_val(getattr(turn, "status", None))
        event_recorder.result_stop_reason = kind if isinstance(kind, str) else None
        if kind != "completed":
            error = getattr(turn, "error", None) or {}
            detail = getattr(error, "message", None) or kind
            raise RequestFailedError(detail)
        return []
    if method == "item/started":
        item = _codex_item(getattr(payload, "item", None))
        itype = _codex_item_type(item)
        if itype == "agentMessage":
            st.agent_pieces.setdefault(getattr(item, "id", None) or "", [])
        if itype in ("agentMessage", "reasoning", "plan") and st.tool_round_open:
            st.tool_round_open = False
            event_recorder.step_boundary()  # 工具结果后新一轮 LLM 请求 → 新 step(单次翻转,聚合并行工具)
        return []
    if method == "item/agentMessage/delta":
        text = getattr(payload, "delta", None) or ""
        if not text:
            return []
        st.agent_pieces.setdefault(getattr(payload, "item_id", None) or "", []).append(text)
        event_recorder.text(text)
        return [text]
    if method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
        delta = getattr(payload, "delta", None) or ""
        if delta:
            st.reasoning_seen.add(getattr(payload, "item_id", None) or "")
            # 全量 CoT 增量段位 = content_index;摘要增量段位 = summary_index(真实段序,
            # 段界即 index 跳变;summaryPartAdded 无文本不产事件,段位语义由此承载)
            index = (getattr(payload, "content_index", None)
                     if method == "item/reasoning/textDelta"
                     else getattr(payload, "summary_index", None))
            event_recorder.chunk({"type": "thinking", "index": index if index is not None else 0,
                       "text": delta})
        return []
    if method == "item/plan/delta":
        # plan 与文本并行增量:逐条 plan chunk;completed 兜底防重复见 st.plan_items
        delta = getattr(payload, "delta", None) or ""
        if delta:
            st.plan_items.add(getattr(payload, "item_id", None) or "")
            event_recorder.chunk({"type": "plan", "index": 0, "text": delta})  # 无段位字段,单文档
        return []
    if method == "item/completed":
        return _codex_item_completed(event_recorder, st, payload)
    if method == "thread/tokenUsage/updated":
        usage = getattr(payload, "token_usage", None) or {}
        breakdown = getattr(usage, "total", None) or getattr(usage, "last", None)
        if breakdown is not None:
            event_recorder.result_usage = _normalize_usage(breakdown)  # 末条为准 → session/end(同 dsh)
        return []
    return []  # summaryPartAdded(段位由 delta 的 summary_index 承载)、outputDelta、progress
    # 等:无文本增量或属日志型,v1 不进流


async def _codex_drain(handle, event_recorder: EventRecorder, st: _CodexSynth):
    """codex 通知流 → 文本增量(stream/result 共用):turn 未完成 → RequestFailedError。

    result 与 stream 同构:终局文本取 st.final_response(末条 agentMessage);
    不再直取 TurnResult(handle.run() 不暴露通知,事件流将只有生命周期)。
    """
    async for notif in handle.stream():
        for delta in _handle_codex_notification(event_recorder, st, notif):
            yield delta
    if not st.saw_turn_completed:
        raise RequestFailedError("turn 未收到完成事件")


# ---------------------------------------------------------------------------
# Codex(OpenAI Codex SDK)包装
# ---------------------------------------------------------------------------


class Codex(BaseGenerator):
    """codex: OpenAI Codex command. Config shape: file-class — config_path points at config.toml.

    SDK raw notification stream → local synthesis (single authority, no projection
    layer; why in _CodexSynth docstring); CodexConfig → SDK assembly parts (codex_*
    below: codex_home/codex_config/codex_thread/codex_turn); config_path pure
    passthrough (home config.toml symlink), mcp_servers generic tool-desk injection;
    codex_home falls back to the built-in isolated dir, system_prompt →
    base_instructions. SDK client binding built at construction (AsyncCodex bound to
    config); connection (AsyncCodex enter) at the generator's with block (__aenter__),
    stream/result auto-enter without an explicit with; the thread is created at call time.
    """

    generator = "codex"
    provider = "openai"

    def __init__(self, config: dict):
        super().__init__(config)
        from openai_codex import AsyncCodex  # lazy:测试可喂假模块

        self._codex = AsyncCodex(config=codex_config(config))

    async def _enter(self):
        await self._codex.__aenter__()  # 连接进入

    async def _exit(self, exc):
        await self._codex.__aexit__(*exc)  # 连接回收

    @contextlib.asynccontextmanager
    async def session(self, **kw):
        """codex session: protocol-layer errors (JsonRpcError) go to parse; the rest follows the base orchestration."""
        from openai_codex import JsonRpcError

        async with super().session(error_stage=lambda exc: _codex_stage(exc, (JsonRpcError,)),
                                   **kw):
            yield

    async def stream(self, prompt: str):
        """Stream the codex notification flow into assistant text deltas (payload-only; metadata via session()).

        Yielded text: item/agentMessage/delta first, item/completed whole-block fallback;
        turn non-completed → RequestFailedError; thinking/plan/tool increments only to
        the event stream. Notifications synthesize TAXONOMY 1:1 (no seq — local
        synthesis, _CodexSynth); session/turn/step lifecycle synthesized here.

        config keys = CodexConfig in this file (assembly in codex_config/thread/turn).
        Credentials (cc-shaped: env isolation does not isolate the credential channel):
        the zero-config default symlinks the real ~/.codex/auth.json; explicit
        config.token writes login_api_key into this isolated home.
        """
        config = self.config
        event_recorder = self._require_event_recorder()
        event_recorder.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        st = _CodexSynth(event_recorder, prompt)
        timeout = config.get("timeout_seconds")
        home = codex_home(config)  # config → 隔离 home(装配在本文件)
        if (token := config.get("token") or ""):
            # 显式 token → 登录凭证属本隔离 home:先断符号链接防穿透写坏用户 ~/.codex/auth.json
            auth = Path(home) / "auth.json"
            if auth.is_symlink():
                auth.unlink()
            await self._codex.login_api_key(token)
        thread = await self._codex.thread_start(**codex_thread(config))
        handle = await thread.turn(prompt, **codex_turn(config))

        if timeout is not None:
            async with asyncio.timeout(timeout):  # 兜底 review/approval 等待挂流
                async for chunk in _codex_drain(handle, event_recorder, st):
                    yield chunk
        else:
            async for chunk in _codex_drain(handle, event_recorder, st):
                yield chunk

    async def result(self, prompt: str) -> str:
        """Return the final round's output: the last agentMessage text of the notification stream (st.final_response).

        Turn non-completed / no output → RequestFailedError. Consumes the notification
        stream with the same shape as stream (_codex_drain): full event synthesis.
        """
        config = self.config
        event_recorder = self._require_event_recorder()
        event_recorder.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        st = _CodexSynth(event_recorder, prompt)  # result 与 stream 同构:合成器照常驱动
        timeout = config.get("timeout_seconds")
        home = codex_home(config)  # config → 隔离 home(装配在本文件)
        if (token := config.get("token") or ""):
            auth = Path(home) / "auth.json"
            if auth.is_symlink():
                auth.unlink()
            await self._codex.login_api_key(token)
        thread = await self._codex.thread_start(**codex_thread(config))
        handle = await thread.turn(prompt, **codex_turn(config))
        if timeout is not None:
            async with asyncio.timeout(timeout):  # 兜底 review/approval 等待挂流
                async for _ in _codex_drain(handle, event_recorder, st):
                    pass
        else:
            async for _ in _codex_drain(handle, event_recorder, st):
                pass
        final = st.final_response or ""
        if not final:
            raise RequestFailedError("未产出最终结果")
        return final
