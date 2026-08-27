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
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from gh_puller import agent
from gh_puller.agent import sinks
from gh_puller.agent.events import TAXONOMY

# 与 test_agent_real.py 同约定:cc/llm 统一模型走 env 覆写;codex 常量直写(无 env 面)
MODEL = os.environ.get("GH_PULLER_MODEL", "deepseek-v4-flash")
MODEL_CODEX = "gpt-5.6-sol"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1"

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
    agent.configure(file=False, ws_urls=[], otel_urls=[])  # 关闭文件 sink + 取消 worker
    await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# 捕获基建:bus 挂收集 sink + JSONL 落盘 + 信封全键契约断言
# ---------------------------------------------------------------------------


def _jsonl_consume(events: list[dict]):
    """收集 sink:recorder 发布的事件原样入列表(不投影、不丢字段)。"""

    async def consume(evt: dict) -> None:
        events.append(dict(evt))

    return consume


async def _capture(runner):
    """一次生成器运行的事件捕获:返回 (产出, 事件列表)。

    configure(file=False, ...) 后 ensure_bus 是空 bus,显式 .add() 收集 sink 才
    enabled —— recorder 的零开销短路(无 sink 直接不构造事件)不会误触发。
    """
    agent.configure(file=False, ws_urls=[], otel_urls=[])
    events: list[dict] = []
    sinks.ensure_bus().add(_jsonl_consume(events))
    result = await runner()
    await asyncio.sleep(0.05)  # bus drain 一拍:put_nowait 后 worker 消费
    return result, events


def _dump(name: str, events: list[dict]) -> Path:
    """事件列表 → JSONL(一行一条,原始信封,ensure_ascii=False);回读校验可解析。"""
    _DUMP_DIR.mkdir(parents=True, exist_ok=True)
    path = _DUMP_DIR / f"{name}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
    [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    print(f"[{name}] {len(events)} 事件 → {path}")
    return path


def _assert_full(events: list[dict]) -> None:
    """正式生成源契约:信封全键一个都不能缺;seq 稠密单调;终态 session/end。"""
    assert events, "至少一个事件"
    assert events[0]["type"] == "config/init"  # 构造期配置快照先于 session/start
    assert events[1]["type"] == "session/start"
    assert events[-1]["type"] == "session/end"
    for evt in events:
        missing = _FULL_ENVELOPE - set(evt)
        assert not missing, f"事件字段缺失: {evt.get('type')} 缺 {sorted(missing)}"
        assert evt["type"] in TAXONOMY, f"未知事件 type: {evt.get('type')!r}"
    assert [e["seq"] for e in events] == list(range(len(events))), "seq 应稠密单调"


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
    """llm key:env DEEPSEEK_API_KEY 优先,回退 ~/.dsh/.credentials.yaml(与真机测试同源)。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        creds = Path.home() / ".dsh" / ".credentials.yaml"
        if creds.exists():
            for line in creds.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("DEEPSEEK_API_KEY:"):
                    key = line.split(":", 1)[1].strip()
    if not key:
        pytest.skip("DEEPSEEK_API_KEY 缺失(env 与 ~/.dsh/.credentials.yaml 均无)")
    return {"model": MODEL, "base_url": DEEPSEEK_API_URL, "api_key": key}


_LLM_PAYLOAD = {"messages": [{"role": "user", "content": QUESTION}],
                "max_tokens": 256, "temperature": 0}


async def _stream_text(aiter) -> str:
    return "".join([c async for c in aiter])


# ---------------------------------------------------------------------------
# cc:stream / result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cc_stream_events_jsonl(tmp_path):
    """cc stream 真机:真实增量粒度 → 逐段 assistant/chunk;WebFetch 真实工具调用。"""
    gen = agent.ClaudeCode(_cc_config(tmp_path))
    out, events = await _capture(
        lambda: _stream_text(gen.stream(QUESTION, session_name="obs:cc:stream",
                                        run_id="r-cc-stream")))
    assert out, "cc 后端应有文本产出"
    assert "assistant/chunk" in _types(events)  # 全流捕获(FileSink 会投影剔除)
    _assert_full(events)
    assert events[-1]["data"]["state"] == "completed"
    _report_run("cc-stream", out, events)


@pytest.mark.asyncio
async def test_cc_result_events_jsonl(tmp_path):
    """cc result 真机:终局语义(ResultMessage.result);事件与 stream 同源全量。"""
    gen = agent.ClaudeCode(_cc_config(tmp_path))
    out, events = await _capture(
        lambda: gen.result(QUESTION, session_name="obs:cc:result", run_id="r-cc-result"))
    assert out, "cc 后端应有终局产出"
    _assert_full(events)
    assert events[-1]["data"]["state"] == "completed"
    _report_run("cc-result", out, events)


# ---------------------------------------------------------------------------
# codex:stream / result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_stream_events_jsonl(tmp_path):
    """codex stream 真机:通知流 1:1 合成,真实工具调用(web_search)。"""
    gen = agent.Codex(_codex_config(tmp_path))
    out, events = await _capture(
        lambda: _stream_text(gen.stream(QUESTION, session_name="obs:codex:stream",
                                        run_id="r-codex-stream")))
    assert out, "codex 后端应有文本产出"
    _assert_full(events)
    assert events[-1]["data"]["state"] == "completed"
    _report_run("codex-stream", out, events)


@pytest.mark.asyncio
async def test_codex_result_events_jsonl(tmp_path):
    """codex result 真机:与 stream 同构消费通知流 —— 事件全量(不再是纯生命周期)。"""
    gen = agent.Codex(_codex_config(tmp_path))
    out, events = await _capture(
        lambda: gen.result(QUESTION, session_name="obs:codex:result", run_id="r-codex-result"))
    assert out, "codex 后端应有终局产出"
    _assert_full(events)
    assert "assistant/message" in _types(events)  # 结果路径事件全量(修复前仅生命周期)
    assert events[-1]["data"]["state"] == "completed"
    _report_run("codex-result", out, events)


# ---------------------------------------------------------------------------
# llm:result / stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_result_events_jsonl():
    """llm result 真机:单发 complete —— 整段内容一次到达 → 单条 chunk(忠实非流式语义)。"""
    out, events = await _capture(
        lambda: agent.OpenAI(_llm_config()).result(
            _LLM_PAYLOAD,
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0),
            session_name="obs:llm:result", run_id="r-llm-result"))
    assert out, "llm 后端应有文本产出"
    _assert_full(events)
    assert events[-1]["data"]["state"] == "completed"
    _report_run("llm-result", out, events)


@pytest.mark.asyncio
async def test_llm_stream_events_jsonl():
    """llm stream 真机:SSE 逐 delta → 逐条 chunk(真实 token 粒度)。

    llm 是单发 complete 客户端,工具轮询在 dispatch 层 —— 真机路径无工具调用,
    chunk 条数 = DeepSeek SSE 实际分段数。
    """
    out, events = await _capture(
        lambda: _stream_text(agent.OpenAI(_llm_config()).stream(
            _LLM_PAYLOAD,
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0),
            session_name="obs:llm:stream", run_id="r-llm-stream")))
    assert out, "llm 后端应有文本产出"
    assert "assistant/chunk" in _types(events)
    _assert_full(events)
    assert events[-1]["data"]["state"] == "completed"
    _report_run("llm-stream", out, events)
