"""opencode:OpenCode CLI 包装 —— headless `opencode run --format json` 子进程 + 合成器。

本文件 = opencode 的独立扩展点(镜像 codex:合成器无投影层;why 见 _OpencodeSynth
docstring);OpenCodeConfig → CLI 装配(opencode run argv / OPENCODE_CONFIG_CONTENT
注入段 / 子进程环境),config 纯透传(OPENCODE_CONFIG env,file 类契约 ——
opencode 仍合并用户全局配置,引擎注入面经 config_content 覆盖冲突键),
mcp_servers 通用注入工具桌(渲染为 opencode mcp 配置段:command 数组 +
environment 内联 map),system_prompt → 临时 instructions 文件(CLI 无系统提示词
旗标,唯一注入点)。

零 SDK 导入(纯 stdlib + 事件层原子)。JSONL 事件流 → gh TAXONOMY 合成:事件信封为
{"type", "timestamp", "sessionID", "part"}逐行一个对象(实测 v1.18.23 捕获),
无 seq/turn/step 编号 —— 流顺序是唯一权威;type 集合
step_start/text/reasoning/tool_use/step_finish/error,text 与 reasoning 均按
part.id 整段快照(_CUMULATIVE_TEXT 差分;reasoning 仅在 config.thinking 打开时入流,
思考块 = 流式 assistant/chunk(增量)+ assistant/message 的 thinking 块(全文,置顶
于文本,与 cc/codex 契约同形;不产面向用户的文本),tool_use 的 completed/error 两态
(error = state.error 文本,无 output/metadata.exit),step_finish 带
reason(stop/tool-calls)+ tokens + cost;**schema 上游无正式文档** —— 解析器对未知
type/非 JSON 行静默跳过,真机捕获注记见验证节。子进程 stderr 有界尾缓冲、退出码/
完成事件双检。
"""

import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, TypedDict

from ..events import EventRecorder, _normalize_usage
from .base import BaseGenerator
from .utils import RequestFailedError, _stage_of

# ---------------------------------------------------------------------------
# config 类型(每生成器一种 dict schema;键可省略,键集即解析层白名单语义)
# ---------------------------------------------------------------------------

# text 事件实测(open code v1.18.23)为按 part.id 的整段快照(每 part 一次全量);
# 差分法兼容两种形态 —— 快照 → 取前缀增量,增量 → 原样。翻转 False = 视为增量流。
_CUMULATIVE_TEXT = True


class OpenCodeConfig(TypedDict, total=False):
    """opencode 运行时 config:映射 opencode run argv + OPENCODE_CONFIG_CONTENT 注入段。"""

    model: str            # --model <provider/model>
    system_prompt: str    # → 临时 instructions 文件(CLI 无系统提示词旗标,唯一注入点)
    cwd: str              # 子进程工作目录(仓库根固定,cc/codex 同位)
    opencode_bin: str     # 可执行路径(缺省 "opencode";测试喂假脚本)
    config: str           # → OPENCODE_CONFIG (opencode.json path; file-class pure passthrough)
    agent: str            # --agent 预定义 agent 名
    variant: str          # --variant 推理预算(provider 特定)
    auto: bool            # --auto 权限未明拒即自动批准(引擎缺省 True,见 adapter)
    thinking: bool        # --thinking 打开思考块入流(JSON 流默认不发 reasoning 事件)
    session: str          # --session 会话续接(前次 run 的 sessionID)
    env: dict             # 子进程环境注入(GRAPHIFY_OUT 等;opencode mcp environment 取值源)
    mcp_servers: list[dict]  # 通用工具桌描述(id/command/args/env_vars)→ mcp 段
    timeout_seconds: float   # asyncio.timeout 兜底(codex 同形)


# ---------------------------------------------------------------------------
# config → CLI 装配(opencode run argv / 子进程环境 / OPENCODE_CONFIG_CONTENT 注入段)
# ---------------------------------------------------------------------------


def _opencode_argv(config: dict, prompt: str) -> list[str]:
    """OpenCodeConfig + prompt → opencode run argv(纯字符串;bin 缺省 "opencode")。

    --pure 恒传(禁外部插件,镜像 cc setting_sources=[] 隔离哲学);--dir 不传
    (子进程 cwd 即工作目录);auto 缺省 True(无头无人值守,权限未明拒即批准)。
    """
    argv = [config.get("opencode_bin") or "opencode", "--pure", "run"]
    for key, flag in (("model", "--model"), ("agent", "--agent"),
                      ("variant", "--variant"), ("session", "--session")):
        if config.get(key):
            argv += [flag, config[key]]
    if config.get("auto", True):
        argv.append("--auto")
    # --thinking:JSON 流默认不发 reasoning 事件(实测仅 flag 打开),由 config.thinking
    # 显式打开(引擎/webui 经 generator_config 注入;缺省关 —— 依用户定调不恒传)。
    if config.get("thinking"):
        argv.append("--thinking")
    argv += ["--format", "json", prompt]
    return argv


def _opencode_env(config: dict) -> dict:
    """OpenCodeConfig → 子进程环境(os.environ 基准 + env 覆盖 + OPENCODE_CONFIG)。

    缺省隔离本机 Claude Code 面:opencode 的 claude-code 兼容层默认加载
    ~/.claude 技能(实测 run:模型调用 skill codebase-memory 注入本机技能全文)
    —— setdefault 关闭技能装载(与 cc setting_sources=[] / --pure 同哲学;
    config.env 可覆写;整组面另有 OPENCODE_DISABLE_CLAUDE_CODE)。
    """
    env = dict(os.environ)
    env.setdefault("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", "1")
    env.update(config.get("env") or {})
    if cfg := config.get("config"):
        env["OPENCODE_CONFIG"] = cfg
    return env


def _opencode_mcp_section(mcp_servers: list[dict], env: dict) -> dict:
    """通用工具桌描述 → opencode mcp 配置段(组合即隔离边界,镜像 codex 的 config.toml 渲染)。

    每条 spec 应带 id/command/args/env_vars(与 codex 通用描述同式;env_vars 为
    名称白名单,值经运行 env 解析 —— 不内联凭据);opencode 段形状 =
    {"type": "local", "command": [command, *args], "enabled": True} +
    environment 内联 map(运行 env 中白名单键的非空值)。mcp_servers 空 → 空段。
    """
    section: dict[str, dict] = {}
    for spec in mcp_servers or []:
        entry: dict[str, Any] = {
            "type": "local",
            "command": [spec.get("command", ""), *spec.get("args", [])],
            "enabled": True,
        }
        env_vars = {k: env[k] for k in spec.get("env_vars", []) if env.get(k)}
        if env_vars:
            entry["environment"] = env_vars
        section[spec.get("id", "mcp-server")] = entry
    return section


def _opencode_config_content(config: dict, instruction_path: str | None, env: dict) -> dict:
    """OpenCodeConfig → OPENCODE_CONFIG_CONTENT 注入段(JSONC tier 6,最高用户覆写层)。

    空 → {} (调用方不设 env,余键随用户配置合并);非空装配:instructions
    (system_prompt 临时文件路径,追加语义)+ mcp(工具桌)。两者皆空 → {}。
    """
    content: dict = {}
    if instruction_path:
        content["instructions"] = [instruction_path]
    if config.get("mcp_servers"):
        content["mcp"] = _opencode_mcp_section(config["mcp_servers"], env)
    return content


def _opencode_args_json(arguments) -> str:
    """opencode 工具 arguments(Any:dict/str/None)→ tool/call 的原始 JSON 字符串。

    与 _codex_args_json 同契:dict → json.dumps,str 原样(可能本身是 JSON 文本),
    None/空 → ""(UI 端解析失败原样展示)。
    """
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments)


# ---------------------------------------------------------------------------
# 适配器:opencode JSONL 事件 → 事件 dict(纯 dict 鸭子读取,测试可喂假事件行)
# ---------------------------------------------------------------------------


class _OpencodeSynth:
    """opencode 事件 → gh 事件流的合成状态(单次 stream 调用一个实例)。

    为什么是合成而非投影(对比 _DshProj):opencode 事件无 seq/turn/step 编号、无
    生命周期事件 —— 流顺序是唯一权威(与 codex 同构);合成器只维护自洽编号
    (event_recorder.seq 自己数、turn 单轮、step 自开合、tool/call|result 自合成)。
    """

    def __init__(self, event_recorder: EventRecorder, prompt: str):
        self.event_recorder = event_recorder
        self.prompt = prompt
        self.parts: dict[str, str] = {}  # part.id → 整段文本(累积快照差分基准)
        self.step_parts: dict[str, str] = {}  # 本 step 的文本 parts(step_finish 合成 content 消息)
        self.step_thinking = ""  # 本 step 的思考全文(chunk 差分增量累计;think 消息拼接源)
        self.think_msg_sent = False  # think 消息已定型(think 批完成后置位,防重复)
        self.pending_tools: list[dict] = []  # 本回合缓冲的工具参包(step_finish 回补,先于工具发 content 消息)
        self.open_steps = 0  # 已见 step_start 数(首事件与 prologue step/start 重合,恒忽略)
        self.saw_stop = False  # 收到 reason=="stop" 的 step_finish(完成信号)
        self.final_response = ""  # 末条 text 整段(result 终局语义)
        self.stderr_tail: list[str] = []  # 子进程 stderr 尾部(退出码错误诊断)


def _flush_thinking_message(event_recorder: EventRecorder, st: _OpencodeSynth) -> None:
    """think 块定型:assistant/message(thinking)× 1 = m 个 thinking chunk 的全量拼接。

    设计契约(块式):assistant/chunk(thinking)× m → assistant/message(thinking)× 1;
    content 批同式。发射锚在"think 批完成"—— 下一条非 reasoning 事件到来即定型
    (批序恒先于 content 批;工具穿插时也先出),sourceSeqs = 本 step thinking chunk 的
    seqs(m 个)。幂等(think_msg_sent 防重)。
    """
    if st.think_msg_sent or not st.step_thinking:
        return
    event_recorder.event(
        "assistant/message", turn=event_recorder.turn, step=event_recorder.step,
        message={"role": "assistant",
                 "content": [{"type": "thinking", "text": st.step_thinking}]},
        surfaceOp="append",
        sourceSeqs=list(event_recorder._chunk_type_seqs.get("thinking", [])),
    )
    st.think_msg_sent = True


def _flush_step_message(event_recorder: EventRecorder, st: _OpencodeSynth) -> None:
    """content 块定型:assistant/message(content)× 1 = n 个 content chunk 的全量拼接。

    发射锚在 step_finish(回合末):CLI 的 text 事件被工具执行回调穿插 —— 按到达序
    直发将得"工具结果在前、语言在后"的错序;回合内容统一在此批量到语义序
    (本函数出队 content 消息置顶 → _flush_pending_tools 按 CLI 相对序回补工具,
    恒先于同 step 工具,cf. test_opencode_stream_* 契约)。sourceSeqs = 本 step
    content chunk 的 seqs(n 个)。step_parts 清空,同 step 后续文本另成一条 surface。
    """
    if not st.step_parts:
        return
    event_recorder.event(
        "assistant/message", turn=event_recorder.turn, step=event_recorder.step,
        message={"role": "assistant",
                 "content": [{"type": "content", "text": t} for t in st.step_parts.values()]},
        surfaceOp="append",
        sourceSeqs=list(event_recorder._chunk_type_seqs.get("content", [])),
    )
    st.step_parts = {}


def _flush_pending_tools(event_recorder: EventRecorder, st: _OpencodeSynth) -> None:
    """本回合缓冲的工具参包 → tool/call + tool/result 成对回补(CLI 到达序;幂等)。

    tool_use 事件到达时只缓存不发布(与文本同属回合统一缓冲);step_finish /
    中断兜底时按语义序回发。成对合成:call 先於 result 且 seq 连续,
    result.sourceSeqs 经 _call_seqs 回溯到 call 的 seq(延迟不影响)。
    """
    for spec in st.pending_tools:
        event_recorder.tool_call(spec["call_id"], spec["name"], spec["arguments"])
        event_recorder.tool_result(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": spec["call_id"],
                                          "content": spec["content"], "is_error": spec["is_error"]}]},
            call_id=spec["call_id"], name=spec["name"], is_error=spec["is_error"],
            src_seq=event_recorder._call_seqs.get(spec["call_id"]),
        )
    st.pending_tools = []


def _handle_opencode_line(event_recorder: EventRecorder, st: _OpencodeSynth, evt: dict) -> list[str]:
    """opencode JSONL 事件 → gh TAXONOMY 事件合成(纯 dict 读取)。

    返回本事件产生的文本增量(供 stream yield);工具/生命周期只进事件流。未知
    type 与污染字段静默跳过(schema 无正式文档 —— 最坏 = 空产出 + 未收到完成事件
    的明确错误,不崩)。
    """
    kind = evt.get("type", "")
    part = evt.get("part") or {}
    if kind != "reasoning":
        _flush_thinking_message(event_recorder, st)  # think 批完成即定型(非 reasoning 事件到达)
    if kind == "step_start":
        st.open_steps += 1
        if st.open_steps >= 2:
            event_recorder.step_boundary()  # 工具结果后新一轮 LLM 请求 → 新 step
        st.step_parts = {}
        st.step_thinking = ""
        st.think_msg_sent = False
        return []
    if kind == "text":
        text = part.get("text") or ""
        if not text:
            return []
        pid = part.get("id") or ""
        if _CUMULATIVE_TEXT:
            prev = st.parts.get(pid, "")
            delta = text[len(prev):] if prev and text.startswith(prev) else text
            st.parts[pid] = text
            st.step_parts[pid] = text
        else:
            delta = text
            if delta:
                st.step_parts[pid] = (st.step_parts.get(pid) or "") + delta
        if delta:
            st.final_response = text
            event_recorder.text(delta)
            return [delta]
        return []
    if kind == "reasoning":
        # 思考块(--thinking 打开后 CLI 才发;part.text 整段,同 text 差分规则):
        # chunk(流式增量)× m → 非 reasoning 事件到达时定型 message(thinking)× 1
        # (块式设计:thinking 批先于 content 批);不产出面向用户的文本。
        text = part.get("text") or ""
        if not text:
            return []
        pid = part.get("id") or ""
        if _CUMULATIVE_TEXT:
            prev = st.parts.get(pid, "")
            delta = text[len(prev):] if prev and text.startswith(prev) else text
            st.parts[pid] = text
        else:
            delta = text
        if delta:
            st.step_thinking += delta
            event_recorder.chunk({"type": "thinking", "index": 0, "text": delta})
        return []
    if kind == "tool_use":
        state = part.get("state") or {}
        status = state.get("status")
        if status not in ("completed", "error"):
            return []  # 防御性跳过其他态(运行中/未知;实测 CLI 只发 completed/error)
        call_id = part.get("callID") or ""
        name = part.get("tool") or ""
        if not call_id:
            return []
        # 回合统一缓冲(不立即发布):CLI 的 text 事件被工具执行回调穿插 —— 按到达序
        # 直发将得"工具结果在前、语言在后";step_finish 时语义序回补(见
        # _flush_step_message/_flush_pending_tools docstring)。
        output = state.get("output")
        content = output if isinstance(output, str) else (
            json.dumps(output, ensure_ascii=False) if output else (state.get("error") or "")
        )
        input_state = state.get("input") or {}
        # error 面三判:status=error / input.error(不可用工具被拦截:opencode 合成
        # tool="invalid" 的 completed 事件,input={"tool": ..., "error": 错误全文})/ exit 非零
        is_error = (status == "error" or (isinstance(input_state, dict) and bool(input_state.get("error")))
                    or (state.get("metadata") or {}).get("exit") not in (None, 0))
        st.pending_tools.append({
            "call_id": call_id, "name": name,
            "arguments": _opencode_args_json(state.get("input")),
            "content": content, "is_error": is_error,
        })
        return []
    if kind == "step_finish":
        tokens = part.get("tokens") or {}
        cache = tokens.get("cache") or {}
        usage = {"input_tokens": tokens.get("input"), "output_tokens": tokens.get("output"),
                 "cache_read_input_tokens": cache.get("read")}
        if any(v is not None for v in usage.values()):
            # opencode tokens 生键(input/output/cache)→ _normalize_usage 统一键(末条为准)
            event_recorder.result_usage = _normalize_usage(usage)
        cost = part.get("cost")
        if isinstance(cost, (int, float)):
            event_recorder.result_cost_usd = float(cost)
        if part.get("reason") == "stop":
            st.saw_stop = True
            event_recorder.result_stop_reason = "stop"
        _flush_step_message(event_recorder, st)  # 回合文本置顶(先于本回合工具)
        _flush_pending_tools(event_recorder, st)  # 工具按 CLI 到达序回补
        return []
    if kind == "error":
        err = evt.get("error") or {}
        detail = (((err.get("data") or {}).get("message") or "") or err.get("name")) \
            or "opencode run 失败"
        raise RequestFailedError(detail)
    return []  # 其余/未知 type(如 reasoning/summary 变体):v1 不进流,静默跳过


# ---------------------------------------------------------------------------
# 子进程生命周期(stderr 并发尾缓冲;退出码/完成事件双检;stream/result 共用)
# ---------------------------------------------------------------------------

_STDERR_TAIL_LINES = 50
_STDERR_TAIL_CHARS = 4096


async def _opencode_stderr_tail(stream, st: _OpencodeSynth) -> None:
    """stderr 有界尾缓冲(独立并发读者防 pipe 满死锁;超限截断只留末尾)。"""
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip()
        st.stderr_tail.append(text)
        if sum(len(x) for x in st.stderr_tail) > _STDERR_TAIL_CHARS:
            st.stderr_tail[:] = st.stderr_tail[-_STDERR_TAIL_LINES:]


@contextlib.asynccontextmanager
async def _opencode_subprocess(argv: list[str], *, cwd: str | None, env: dict,
                               st: _OpencodeSynth):
    """opencode run 子进程(stdout → 调用方逐行读;stderr → 尾缓冲);回收兜底 terminate+wait。

    过早退出(调用方取消)→ 未回收则 terminate + ≤5s wait,超时 kill;stderr 读者
    随退出取消(尾缓冲已保有界内容)。
    """
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    err_task = asyncio.create_task(_opencode_stderr_tail(proc.stderr, st))
    try:
        yield proc
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        err_task.cancel()


async def _opencode_drain(proc, event_recorder: EventRecorder, st: _OpencodeSynth):
    """opencode stdout JSONL → 文本增量(stream/result 共用);退出码/完成事件双检。

    result 与 stream 同构:终局文本取 st.final_response(末条 text 整段)。进程
    退出码非 0 / 未见 reason=="stop" 的 step_finish → RequestFailedError
    (同 codex "turn 未收到完成事件" 语义)。
    """
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue  # stdout 污染行防御性跳过(schema 无正式文档,见模块 docstring)
            if not isinstance(evt, dict):
                continue
            for delta in _handle_opencode_line(event_recorder, st, evt):
                yield delta
    finally:
        _flush_thinking_message(event_recorder, st)  # 中断兜底:think 未定型补发(正常路径已置位)
        _flush_step_message(event_recorder, st)      # 中断兜底:未出队回合内容补发(正常路径 step_finish 已清空)
        _flush_pending_tools(event_recorder, st)
        rc = await proc.wait()  # 收尸(仅 await wait;不 raise —— 异常在飞时不得遮蔽)
    if rc != 0:
        detail = f"opencode 退出码 {rc}" + (f": {'; '.join(st.stderr_tail[-20:])}" if st.stderr_tail else "")
        raise RequestFailedError(detail)
    if not st.saw_stop:
        raise RequestFailedError("turn 未收到完成事件")


# ---------------------------------------------------------------------------
# OpenCode(CLI 子进程)包装
# ---------------------------------------------------------------------------


class OpenCode(BaseGenerator):
    """opencode: OpenCode CLI (headless run --format json). Config: file-class, config → opencode.json.

    CLI child process → local synthesis (single authority, no projection layer; why in
    _OpencodeSynth docstring); OpenCodeConfig → CLI assembly (argv/env/config_content):
    system_prompt → temporary instructions file, mcp_servers generic tool-desk injection
    (--pure always passed, mirroring cc's isolation philosophy), config pure
    passthrough (OPENCODE_CONFIG env). The credential channel is not isolated: opencode
    holds its own credentials (~/.local/share/opencode/auth.json); this layer is only
    the config injection surface. No client binding at construction (the child spawns
    at call time, no __aenter__ lifecycle).
    """

    generator = "opencode"
    provider = "opencode"  # 会话快照后端域 = CLI 本体(模型路由随 opencode 配置,不冒充)

    async def _enter(self) -> None:
        """No client lifecycle: the child spawns during stream/result calls (base session hooks no-op)."""

    async def _exit(self, exc) -> None:
        """Same: nothing to reap at the client layer (child reaping in _opencode_subprocess)."""

    @contextlib.asynccontextmanager
    async def session(self, **kw):
        """opencode session: error stage via the package _stage_of (JSON parse errors → parse, else run)."""
        async with super().session(error_stage=_stage_of, **kw):
            yield

    async def stream(self, prompt: str):
        """Stream opencode JSONL events into assistant text deltas (payload-only; metadata via session()).

        Yielded text: text-event diffs; non-zero exit / no completion →
        RequestFailedError; tool/lifecycle events only to the event stream. JSONL
        events synthesize TAXONOMY 1:1 (no seq — local synthesis, _OpencodeSynth);
        turn/step lifecycle synthesized from prologue and step_boundary.

        config keys = OpenCodeConfig in this file (assembly in _opencode_argv/_opencode_env/
        _opencode_config_content); a non-empty system_prompt builds the temporary
        instructions file (CLI has no system-prompt flag — the only injection point).
        """
        config = self.config
        event_recorder = self._require_event_recorder()
        event_recorder.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        st = _OpencodeSynth(event_recorder, prompt)
        with tempfile.TemporaryDirectory(prefix="gh-puller-opencode-") as tmp:
            instruction_path = None
            if system_prompt := config.get("system_prompt"):
                path = Path(tmp) / "instructions.md"
                path.write_text(system_prompt, encoding="utf-8")
                instruction_path = str(path)
            env = _opencode_env(config)
            content = _opencode_config_content(config, instruction_path, env)
            if content:
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(content, ensure_ascii=False)
            argv = _opencode_argv(config, prompt)
            async with _opencode_subprocess(argv, cwd=config.get("cwd"), env=env, st=st) as proc:
                if (timeout := config.get("timeout_seconds")) is not None:
                    async with asyncio.timeout(timeout):  # 兜底无头挂流(权限等待等)
                        async for chunk in _opencode_drain(proc, event_recorder, st):
                            yield chunk
                else:
                    async for chunk in _opencode_drain(proc, event_recorder, st):
                        yield chunk

    async def result(self, prompt: str) -> str:
        """Return the final output: the last text segment (st.final_response); no output → RequestFailedError."""
        config = self.config
        event_recorder = self._require_event_recorder()
        event_recorder.user_message({"role": "user", "content": [{"type": "text", "text": prompt}]})
        st = _OpencodeSynth(event_recorder, prompt)
        with tempfile.TemporaryDirectory(prefix="gh-puller-opencode-") as tmp:
            instruction_path = None
            if system_prompt := config.get("system_prompt"):
                path = Path(tmp) / "instructions.md"
                path.write_text(system_prompt, encoding="utf-8")
                instruction_path = str(path)
            env = _opencode_env(config)
            content = _opencode_config_content(config, instruction_path, env)
            if content:
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(content, ensure_ascii=False)
            argv = _opencode_argv(config, prompt)
            async with _opencode_subprocess(argv, cwd=config.get("cwd"), env=env, st=st) as proc:
                if (timeout := config.get("timeout_seconds")) is not None:
                    async with asyncio.timeout(timeout):
                        async for _ in _opencode_drain(proc, event_recorder, st):
                            pass
                else:
                    async for _ in _opencode_drain(proc, event_recorder, st):
                        pass
        final = st.final_response or ""
        if not final:
            raise RequestFailedError("未产出最终结果")
        return final
