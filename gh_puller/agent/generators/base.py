"""生成器共享骨架:BaseGenerator 基类(session with 语义/收尾编排/stream、result 契约定形)。

本层只依赖标准库、事件层(.events)、envs 与包内 utils(共享失败/异常分类);每个
生成器文件(cc/openai/codex/dsh)自持本体与 config 世界,差异经本基类下沉。
API 契约(def stream/result 语义、config 构造期注入、session with 语义)见包 docstring(__init__.py)。
"""

import contextlib
import sys
from collections.abc import AsyncIterator

from ... import envs  # 模块对象绑定:属性一律调用时取(patch/强刷活性)
from ..events import EventRecorder, _session_id


class BaseGenerator:
    """生成器共享骨架:cc = 单一权威合成;dsh = 双权威投影(对齐器);llm = 直连 HTTP。

    子类只要写差异驱动循环(stream)与终局语义(result);公共 kwarg(会话/run
    元数据)逐参数一致。config 在**构造时期**注入(副本存 self.config),运行时
    方法不再收 config(契约见包 docstring)。

    生成器 = 对应 client 的包装;**唯一 with 入口是 session()**:`async with
    cc.session(...)` = 一次上游对话 —— 进入 = 会话级 event_recorder 装配并启动
    (init_config → session/start)+ 客户端进入(子进程 spawn,_enter),退出 =
    客户端回收(_exit)+ 收尾(error 事件 + finish)。会话元数据(session/
    session_name/run_id/context/retry/meta)经 session() 注入;stream/result 只收
    运行时载荷(prompt/payload),必须在 session 块内调用(块外 → RuntimeError)。

    监控侧 agent 自身 meta 只 generator 一项 + config(事件 envelope 即
    generator/model 与各 config 派生事件)。
    """

    generator = ""  # 生成管线 id(cc|dsh|codex|llm)
    provider = ""  # 会话快照的后端域(cc=anthropic/dsh=deepseek/codex=openai/llm=openai)

    def __init__(self, config: dict):
        self.config = dict(config)  # 副本防 SDK 篡改
        self._event_recorder: EventRecorder | None = None  # 会话级监控装配(session 进入时启动;块外 None)

    @contextlib.asynccontextmanager
    async def session(self, *, session: str | None = None, session_ns: str | None = None,
                      run_id: str | None = None, session_name: str | None = None,
                      context: list[dict] | None = None, retry: dict | None = None,
                      meta: dict | None = None,
                      error_stage=None, epilogue=True, prologue=True):
        """一次上游对话(客户端 + 监控同寿):进入即装配、退出即收官。

        编排:会话级 event_recorder 装配并启动(init_config → session/start)→
        客户端进入(_enter,子进程 spawn)→ yield(stream/result 在块内调用)→
        退出:客户端回收(_exit)+ 收尾(error 事件 + finish)。子类如不同(codex 协议
        归 parse、dsh 双权威 epilogue、start prologue、llm http/parse 分类)覆写
        本方法转发差异参数。一次会话一次对话(实例不并发复用)。
        """
        event_recorder = self._recorder(session=session, session_ns=session_ns,
                        run_id=run_id, session_name=session_name, meta=meta)
        event_recorder.init_config(self.config)
        event_recorder.start(context=context, retry=retry, prologue=prologue)
        self._event_recorder = event_recorder  # 已启动的会话级装配;stream/result 直接引用
        ok = False
        try:
            # 保鲜:会话期间每 interval 触一次会话文件 mtime(只动时间戳、不发事件、
            # 绝不 busy-loop);hub 侧租约据此区别"活着但静默"与"进程已死"。退出先停
            # 保鲜再收尾:停后 mtime 冻结 → 崩溃残留由租约判定 aborted;session/end
            # 恒为最后一条合法行。≤0 → 不起保鲜任务。
            heartbeat_secs = envs.AGENT_MONITOR_HEARTBEAT_SECS
            if heartbeat_secs and heartbeat_secs > 0:
                event_recorder.start_keepwarm(heartbeat_secs)
            await self._enter()  # 客户端进入(子进程 spawn);失败仍经收尾(不含 error 事件)
            try:
                yield
            except Exception as exc:
                # 只捕获 Exception(消费者提前关闭的 GeneratorExit、CancelledError 为
                # BaseException,不落 error 事件,靠下面 finally 兜底 finish(False));
                event_recorder.error(exc, error_stage(exc) if error_stage else "run")
                raise
            finally:
                await self._exit(sys.exc_info())  # 客户端回收(异常上下文原样)
            ok = True
        finally:
            await event_recorder.stop_keepwarm()
            event_recorder.finish(ok, epilogue=epilogue() if callable(epilogue) else epilogue)
            self._event_recorder = None

    def _require_event_recorder(self) -> EventRecorder:
        """会话级 event_recorder(session 进入时已启动);块外调用 stream/result → 契约错误。"""
        if self._event_recorder is None:
            raise RuntimeError("stream/result 只能在 async with gen.session(...) 块内调用")
        return self._event_recorder

    async def _enter(self) -> None:
        """子类钩子(随 session 生命周期):进入对应客户端(与底层 client __aenter__ 同语义)。"""
        raise NotImplementedError

    async def _exit(self, exc) -> None:
        """子类钩子(随 session 生命周期):回收对应客户端(exc = 异常上下文三元组)。"""
        raise NotImplementedError

    def _recorder(self, *, session: str | None = None, session_ns: str | None = None,
             run_id: str | None = None, session_name: str | None = None,
             meta: dict | None = None,
             generator: str | None = None) -> EventRecorder:
        """公共 kwarg → EventRecorder(session id 规则/信封 generator 取类属性;不含 context/retry)。

        provider/model 取类属性与 config(会话快照,随 session/start data 落地)。
        """
        return EventRecorder(_session_id(session, session_ns, run_id, session_name),
                    generator=generator or self.generator,
                    provider=self.provider or None,
                    model=self.config.get("model") or None,
                    label=session_name, run_id=run_id, meta=meta)

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        """流式应答(子类必写):assistant 文本增量 async generator;只在 session 块内调用。

        载荷 = 运行时入参(prompt;llm 为 payload + 请求级 timeout/headers),会话元数据全在 session()。
        """
        raise NotImplementedError

    async def result(self, prompt: str) -> str:
        """非流式最终结果(子类必写):只拿最后一轮 assistant 输出;只在 session 块内调用。"""
        raise NotImplementedError
