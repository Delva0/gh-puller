"""生成器共享骨架:BaseGenerator 基类(recorder 装配/事件守卫/stream、result 契约定形)。

本层只依赖标准库、事件层(.events)、envs 与包内 utils(共享失败/异常分类);每个
生成器文件(cc/openai/codex/dsh)自持本体与 config 世界,差异经本基类下沉。
API 契约(def stream/result 语义、config 构造期注入)见包 docstring(__init__.py)。
"""

import contextlib
from collections.abc import AsyncIterator

from ... import envs  # 模块对象绑定:属性一律调用时取(patch/强刷活性)
from ..events import EventRecorder, _session_id


class BaseGenerator:
    """生成器共享骨架:cc = 单一权威合成;dsh = 双权威投影(对齐器);llm = 直连 HTTP。

    子类只要写差异驱动循环(stream)与终局语义(result);公共 kwarg(会话/run
    元数据)逐参数一致。config 在**构造时期**注入(副本存 self.config),运行时
    方法不再收 config(契约见包 docstring)。

    监控侧 agent 自身 meta 只 generator 一项 + config(事件 envelope 即
    generator/model 与各 config 派生事件)。
    """

    generator = ""  # 生成管线 id(cc|dsh|codex|llm)

    def __init__(self, config: dict):
        self.config = dict(config)  # 副本防 SDK 篡改

    def _recorder(self, *, session: str | None = None, session_ns: str | None = None,
             run_id: str | None = None, session_name: str | None = None,
             meta: dict | None = None,
             generator: str | None = None) -> EventRecorder:
        """公共 kwarg → EventRecorder(session id 规则/信封 generator 取类属性;不含 context/retry)。"""
        return EventRecorder(_session_id(session, session_ns, run_id, session_name),
                    generator=generator or self.generator,
                    label=session_name, run_id=run_id, meta=meta)

    @contextlib.asynccontextmanager
    async def _guard(self, run: EventRecorder, *, error_stage=None, epilogue=True,
                     heartbeat_secs: float | None = None):
        """统一收尾:正常 → finish(ok=True);异常 → error(stage)+ raise + finish(False)。

        error_stage:设 lambda 指定 error 事件 stage(None 等价 cc 的硬编码 "run");
        epilogue:bool 或 callable(dsh 传 lambda: not proj.saw_turn_end,调用期求值)。
        只捕获 Exception(消费者提前关闭的 GeneratorExit、CancelledError 为
        BaseException,不落 error 事件,靠 finally 兜底 finish(False) —— 与历史
        try/except/finally 语义一致);run.finish 本身幂等。
        heartbeat_secs:会话保鲜间隔(None → envs 常量;≤0 → 不起保鲜任务)。会话期间
        每该间隔触一次会话文件 mtime(只动时间戳、不发事件、绝不 busy-loop),由
        EventRecorder.start_keepwarm 承担(守卫层只注入 cadence),hub 侧租约据此
        区别"活着但静默"与"进程已死"(见 hub.py 租约扫描)。
        """
        if heartbeat_secs is None:
            heartbeat_secs = envs.AGENT_MONITOR_HEARTBEAT_SECS
        if heartbeat_secs and heartbeat_secs > 0:
            run.start_keepwarm(heartbeat_secs)
        ok = False
        try:
            yield
            ok = True
        except Exception as exc:
            run.error(exc, error_stage(exc) if error_stage else "run")
            raise
        finally:
            # 先停保鲜再收尾:停后文件 mtime 冻结 → 崩溃残留由租约判定 aborted;
            # session/end 恒为最后一条合法行(末行断言依赖)。
            await run.stop_keepwarm()
            run.finish(ok, epilogue=epilogue() if callable(epilogue) else epilogue)

    async def stream(self, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None) -> AsyncIterator[str]:
        """流式应答(子类必写):assistant 文本增量 async generator(见模块契约)。"""
        raise NotImplementedError

    async def result(self, prompt: str, *, session: str | None = None,
                     session_name: str | None = None, run_id: str | None = None,
                     session_ns: str | None = None,
                     context: list[dict] | None = None, retry: dict | None = None,
                     meta: dict | None = None) -> str:
        """非流式最终结果(子类必写):只拿最后一轮 assistant 输出(见模块契约)。"""
        raise NotImplementedError
