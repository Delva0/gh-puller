"""生成器事件全量捕获(真机):EventRecorder.event 原始输出 → JSONL。

**方法**(与 FileSink 区分):不订阅 filesink —— FileSink 只落非流式事件流
(NON_STREAM_TYPES 投影,逐行跳过 assistant/chunk 且无 session/start 起点则丢弃),
信息缺失。本文件直接把收集 sink 挂在 EventBus 上(`sinks.ensure_bus().add(...)`),
EventRecorder.event 构造后的信封(**id/ts/type/data/session/run_id/label/
generator/model/seq**)原样捕获,一行一条 JSONL 落盘 —— 一个字段都不能缺
(含 assistant/chunk 全量),这正是与 FileSink 的差异断言点。

范围与纪律(默认整文件 skip):
- **真机** = 真实后端 + 真实模型输出,烧真实 token:模块级 opt-in
  `GH_PULLER_REAL_TESTS=1` 才运行(与 test_agent_real.py 同纪律;默认
  `uv run pytest` 绝不烧 token);cc 走本地 claude CLI 凭证、codex 走
  ~/.codex/auth.json 符号链接、llm 走 DeepSeek OpenAI 兼容端(key 唯一显式);
- 六路 = cc / codex / llm × (stream / result)(dsh 排除:SDK runtime 载体未构建,
  恢复点见 test_agent_real.test_dsh_stream_real 的 skip 注释);
- 情景:简单问题 + 工具外驱 —— 每路独立新会话(stream 与 result 分开,事件组
  run_id/label 不同),问题 "访问 GitHub 仓库写一句话介绍":cc(WebFetch/
  WebSearch 授权)与 codex(web_search 启用)会触发真实工具调用(llm 是单发
  complete 客户端,工具循环在 dispatch 层,真机无工具轮);真实输出粒度 =
  provider 增量粒度(逐字/逐段,chunk 条数即真实增量段数,适配器 1:1);
- 断言保持观测化:信封全键/seq 稠密/终态 completed + 产出非空;工具调用与
  增量条数**打印不硬断言**(真实模型行为不保证,记录在 dump 里供观测)。

运行:`uv run pytest -s tests/test_events_jsonl.py`(设 GH_PULLER_REAL_TESTS=1)
输出:`outputs/event-jsonl/{cc,codex,llm}-{stream,result}.jsonl`(gitignored;
env GH_PULLER_EVENTS_DUMP_DIR 可覆写)。观测示例:
`tail -f outputs/event-jsonl/cc-stream.jsonl`、
`jq -s '.[] | [.seq, .type] | @tsv' outputs/event-jsonl/cc-stream.jsonl`
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from gh_puller import agent
from gh_puller.agent import sinks
from gh_puller.agent.events import TAXONOMY

# 与 test_agent_real.py 同约定:cc 模型走 GH_PULLER_MODEL 覆写;llm 路按生成器 env 面
# (LLM_MODEL / OPENAI_API_KEY / OPENAI_BASE_URL,webui/__main__ 同约定);codex 常量直写(无 env 面)
MODEL = os.environ.get("GH_PULLER_MODEL", "deepseek-v4-flash")
LLM_MODEL = os.environ.get("LLM_MODEL", MODEL)  # llm 路模型(生成器 env 面)
MODEL_CODEX = "gpt-5.6-luna"  # luna = 本地模型(不烧 token);sol 改 luna 同用户指示
# llm 路端点:缺省 DeepSeek(OpenAI 兼容,低成本),经 OPENAI_BASE_URL 覆写
LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1")

# 简单问题 + 工具外驱:访问仓库并一句话介绍(模型真实触发 WebFetch/web_search)
QUESTION = "请访问 https://github.com/yankils/hello-world 并写一句话介绍这个仓库。"

# EventRecorder.event 完整信封:new_event(id/ts/type/data)+ recorder 附加(会话属性)
_FULL_ENVELOPE = {"id", "ts", "type", "data", "session", "seq"}

_DUMP_DIR = Path(os.environ.get("GH_PULLER_EVENTS_DUMP_DIR")
                 or Path(__file__).resolve().parents[1] / "outputs" / "event-jsonl")

pytestmark = pytest.mark.skipif(
    os.environ.get("GH_PULLER_REAL_TESTS") != "1",
    reason="真机测试:需 GH_PULLER_REAL_TESTS=1(烧真实 token)",
)


@pytest_asyncio.fixture(autouse=True)
async def _monitor_cleanup():
    yield
    agent.configure(ws_urls=[], otel_urls=[])  # 撤 ws/otel + 取消 worker;文件落盘默认重定向(conftest tmp)
    await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# 捕获基建:bus 挂收集 sink + JSONL 落盘 + 信封全键契约断言
# ---------------------------------------------------------------------------


def _read_jsonl(directory: Path) -> list[dict]:
    """目录下全部 *.jsonl 的行读取(普通函数;异步上下文经 to_thread 调用)。"""
    out: list[dict] = []
    for f in sorted(directory.glob("*.jsonl")):
        out.extend(json.loads(line) for line in f.read_text(encoding="utf-8").splitlines())
    return out


def _jsonl_consume(events: list[dict]):
    """收集 sink:recorder 发布的事件原样入列表(不投影、不丢字段)。"""

    async def consume(evt: dict) -> None:
        events.append(dict(evt))

    return consume


async def _capture(runner):
    """一次生成器运行的事件捕获:返回 (产出, 事件列表, FileSink 落盘事件)。

    configure(...) 撤掉 ws/otel;ensure_bus 恒挂文件 sink(落本目录),此处再显式
    .add() 收集 sink —— recorder 的零开销短路(无 bus 直接不构造事件)不会误触发。
    产品通道(FileSink)落盘事件一并带回:collect 首/尾拍漏收的调度问题由文件通道
    补证(断言见 _assert_full;session/end 恒文件末行是通道设计保证)。
    """
    file_dir = tempfile.mkdtemp(prefix="gh-puller-events-jsonl-")
    agent.configure(file_dir=file_dir, ws_urls=[], otel_urls=[])
    events: list[dict] = []
    b = sinks.ensure_bus()
    b.add(_jsonl_consume(events))
    await asyncio.sleep(0)  # 让 drain 协程先入座(真机慢节奏下防首拍事件竞争)
    result = await runner()
    file_events: list[dict] = []
    # 排空等待:collect/文件都是异步 drain —— 权威完成信号 = 文件末行 session/end
    # (终局事件发布后由 sink 落盘;上限 2s)。
    file_dir_path = Path(file_dir)
    for _ in range(40):
        file_events = await asyncio.to_thread(_read_jsonl, file_dir_path)
        if file_events and file_events[-1]["type"] == "session/end":
            break
        await asyncio.sleep(0.05)
    return result, events, file_events


def _dump(name: str, events: list[dict]) -> Path:
    """事件列表 → JSONL(一行一条,原始信封,ensure_ascii=False);回读校验可解析。"""
    _DUMP_DIR.mkdir(parents=True, exist_ok=True)
    path = _DUMP_DIR / f"{name}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(evt, ensure_ascii=False) + "\n" for evt in events)
    [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    print(f"[{name}] {len(events)} 事件 → {path}")
    return path


def _assert_full(events: list[dict], *, file_events: list[dict] | None = None) -> None:
    """正式生成源契约:信封全键一个都不能缺;seq 稠密单调;终态 session/end。

    序号权威在构造期(seq 恒 0..n-1);收集到达序不是契约(接收端本就按 seq 归组
    排序)—— collect 首/尾拍的调度偏移由产品通道(FileSink)补证:
    - 终态:session/end(非流式,必落文件)恒为文件末行(通道设计保证)且 seq = max;
    - 连续性:collect ∪ 文件的 seq 必须从 0 连续到 max(config/init = seq0 唯一在 collect)。
    """
    assert events, "至少一个事件"
    seqs = {e["seq"] for e in events}
    for evt in events:
        missing = _FULL_ENVELOPE - set(evt)
        assert not missing, f"事件字段缺失: {evt.get('type')} 缺 {sorted(missing)}"
        assert evt["type"] in TAXONOMY, f"未知事件 type: {evt.get('type')!r}"
    if file_events:
        seqs |= {e["seq"] for e in file_events}
        last = file_events[-1]
        assert last["type"] == "session/end", f"文件末行应为 session/end: {last['type']}"
        assert last["seq"] == max(seqs), "session/end 应为最大 seq(文件通道)"
        assert last["data"]["state"] == "completed", f"终态应为 completed: {last['data']['state']}"
    assert seqs and max(seqs) == len(seqs) - 1, "seq 应从 0 连续到 max(collect ∪ 文件)"


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def _report_run(name: str, out: str, events: list[dict]) -> None:
    """观测汇报:落盘 + 轮廓(增量条数/工具/终局文本),供 -s 与 dump 双通道。"""
    _dump(name, events)
    chunks = [e for e in events if e["type"] == "assistant/chunk"]
    tools = [e for e in events if e["type"] in ("tool/call", "tool/result")]
    print(f"[{name}] 产出 {len(out)} 字:{out[:80]!r} | chunk {len(chunks)} 条 "
          f"| tool 事件 {len(tools)} 条"
          f"({'/'.join(t['data'].get('name') or '?' for t in tools) if tools else '无'})")


# ---------------------------------------------------------------------------
# 真机配置(与 test_agent_real.py 同约定)
# ---------------------------------------------------------------------------


def _cc_config(tmp_path) -> dict:
    return {
        "model": MODEL,
        "cwd": str(tmp_path),
        "allowed_tools": ["WebFetch", "WebSearch"],  # 工具外驱:访问类问题真实触发
        "max_turns": 6,
        "include_partial_messages": True,  # StreamEvent 路径真实产 chunk
    }


def _codex_config(tmp_path) -> dict:
    return {
        "codex_home": str(tmp_path / "codex-home"),  # 必须隔离;_codex_home_setup 建符号链接
        "sandbox": "full_access",
        "approval_mode": "auto_review",
        "model": MODEL_CODEX,
        "cwd": str(tmp_path),
        "web_search": True,  # Codex 内置网络搜索:默认安全关闭,须显式启用
        "timeout_seconds": 300,  # 兜底防止 approval 挂流
    }


def _llm_config() -> dict:
    """llm 配置:照生成器 env 面(LLM_MODEL / OPENAI_API_KEY / OPENAI_BASE_URL)。"""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        pytest.skip("OPENAI_API_KEY 缺失(env;不读任何组件私有文件)")
    return {"model": LLM_MODEL, "base_url": LLM_BASE_URL, "api_key": key}


_LLM_PAYLOAD = {"messages": [{"role": "user", "content": QUESTION}],
                "max_tokens": 256, "temperature": 0}


async def _stream_text(stream) -> str:
    return "".join([c async for c in stream])


# ---------------------------------------------------------------------------
# cc:stream / result  # noqa: ERA001 - 中文分节标题注释,非被注释代码
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cc_stream_events_jsonl(tmp_path):
    """cc stream 真机:真实增量粒度 → 逐段 assistant/chunk;WebFetch 真实工具调用。"""
    gen = agent.ClaudeCode(_cc_config(tmp_path))

    async def _run():
        async with gen.session(session_name="obs:cc:stream", run_id="r-cc-stream"):
            return await _stream_text(gen.stream(QUESTION))

    out, events, file_events = await _capture(_run)
    assert out, "cc 后端应有文本产出"
    assert "assistant/chunk" in _types(events)  # 全流捕获(FileSink 会投影剔除)
    _assert_full(events, file_events=file_events)
    _report_run("cc-stream", out, events)


@pytest.mark.asyncio
async def test_cc_result_events_jsonl(tmp_path):
    """cc result 真机:终局语义(ResultMessage.result);事件与 stream 同源全量。"""
    gen = agent.ClaudeCode(_cc_config(tmp_path))

    async def _run():
        async with gen.session(session_name="obs:cc:result", run_id="r-cc-result"):
            return await gen.result(QUESTION)

    out, events, file_events = await _capture(_run)
    assert out, "cc 后端应有终局产出"
    _assert_full(events, file_events=file_events)
    _report_run("cc-result", out, events)


# ---------------------------------------------------------------------------
# codex:stream / result  # noqa: ERA001 - 中文分节标题注释,非被注释代码
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_stream_events_jsonl(tmp_path):
    """codex stream 真机:通知流 1:1 合成,真实工具调用(web_search)。"""
    gen = agent.Codex(_codex_config(tmp_path))

    async def _run():
        async with gen.session(session_name="obs:codex:stream", run_id="r-codex-stream"):
            return await _stream_text(gen.stream(QUESTION))

    out, events, file_events = await _capture(_run)
    assert out, "codex 后端应有文本产出"
    _assert_full(events, file_events=file_events)
    _report_run("codex-stream", out, events)


@pytest.mark.asyncio
async def test_codex_result_events_jsonl(tmp_path):
    """codex result 真机:与 stream 同构消费通知流 —— 事件全量(不再是纯生命周期)。"""
    gen = agent.Codex(_codex_config(tmp_path))

    async def _run():
        async with gen.session(session_name="obs:codex:result", run_id="r-codex-result"):
            return await gen.result(QUESTION)

    out, events, file_events = await _capture(_run)
    assert out, "codex 后端应有终局产出"
    _assert_full(events, file_events=file_events)
    assert "assistant/message" in _types(events)  # 结果路径事件全量(修复前仅生命周期)
    _report_run("codex-result", out, events)


# ---------------------------------------------------------------------------
# llm:result / stream  # noqa: ERA001 - 中文分节标题注释,非被注释代码
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_result_events_jsonl():
    """llm result 真机:单发 complete —— 整段内容一次到达 → 单条 chunk(忠实非流式语义)。"""
    gen = agent.OpenAI(_llm_config())

    async def _run():
        async with gen.session(session_name="obs:llm:result", run_id="r-llm-result"):
            return await gen.result(_LLM_PAYLOAD, timeout=httpx.Timeout(connect=10.0, read=180.0,
                                                                       write=10.0, pool=10.0))

    out, events, file_events = await _capture(_run)
    assert out, "llm 后端应有文本产出"
    _assert_full(events, file_events=file_events)
    _report_run("llm-result", out, events)


@pytest.mark.asyncio
async def test_llm_stream_events_jsonl():
    """llm stream 真机:SSE 逐 delta → 逐条 chunk(真实 token 粒度)。

    llm 是单发 complete 客户端,工具轮询在 dispatch 层 —— 真机路径无工具调用,
    chunk 条数 = DeepSeek SSE 实际分段数。
    """
    gen = agent.OpenAI(_llm_config())

    async def _run():
        async with gen.session(session_name="obs:llm:stream", run_id="r-llm-stream"):
            return await _stream_text(gen.stream(_LLM_PAYLOAD, timeout=httpx.Timeout(
                connect=10.0, read=180.0, write=10.0, pool=10.0)))

    out, events, file_events = await _capture(_run)
    assert out, "llm 后端应有文本产出"
    assert "assistant/chunk" in _types(events)
    _assert_full(events, file_events=file_events)
    _report_run("llm-stream", out, events)
