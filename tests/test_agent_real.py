"""五路适配器真机测试(烧真实 token,默认跳过,显式 opt-in)。

与 test_agent.py(mock 离线)互补:本文件实时走真实后端 —— cc spawn 本地
claude CLI(凭证走本地 ~/.claude 配置,同 "apikey 在配置文件中" 约定)、
llm 经 httpx 直连 OpenAI 兼容端(照生成器 env 面:OPENAI_API_KEY /
OPENAI_BASE_URL / LLM_MODEL;缺省端点 DeepSeek 兼容端,仅环境变量,不读任何
组件私有文件)、dsh 走 DeepSeek Harness SDK(凭证 SDK 自足)、codex 走
openai_codex SDK(隔离 home 符号链接引用真实 ~/.codex/auth.json,与 cc 同形
——验证通道不隔离,隔离只管设置面)、opencode 走 CLI 子进程(模型路由/凭证
随 opencode 自身配置,同 cc 的 CLI 自持凭证约定)。

每路经 agent.configure(file_dir=tmp_path, ws_urls=[], otel_urls=[]) 把
FileSink 根目录注入临时目录,并断言非流式事件流落盘契约正确
(即"filesink 允许注入临时输出路径供测试"的验证)。

全体 opt-in:GH_PULLER_REAL_TESTS=1 才运行;默认 uv run pytest 全量套件整
文件 skip(不烧 token、不 spawn 子进程,见 memory: llm-no-token-tests)。
模型:DeepSeek 三路统一 deepseek-v4-flash(env GH_PULLER_MODEL 可覆写);
codex 常量直写 gpt-5.6-sol(chatgpt OAuth 凭据只认 models_cache 内的 5.x
模型;绝不引入 codex 环境变量——与代码同形)。
"""
import asyncio
import json
import os
import shutil

import httpx
import pytest
import pytest_asyncio

from gh_puller import agent

MODEL = os.environ.get("GH_PULLER_MODEL", "deepseek-v4-flash")  # cc/dsh 路(测试自定)
LLM_MODEL = os.environ.get("LLM_MODEL", MODEL)  # llm 路按生成器 env 面(webui/__main__ 同约定)
MODEL_CODEX = "gpt-5.6-luna"  # 常量直写:codex 无环境变量(同 cc 路线);luna = 本地模型(不烧 token)

# llm 路端点:缺省 DeepSeek(OpenAI 兼容,低成本),经 OPENAI_BASE_URL 覆写(与 webui/__main__ 同约定)
LLM_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1")

pytestmark = pytest.mark.skipif(
    os.environ.get("GH_PULLER_REAL_TESTS") != "1",
    reason="真机测试:需 GH_PULLER_REAL_TESTS=1(烧真实 token)",
)


@pytest_asyncio.fixture(autouse=True)
async def _monitor_cleanup():
    """每测后复元监控配置:撤 ws/otel;文件落盘默认重定向(conftest tmp),防真实 ~/.gh-puller。"""
    yield
    agent.configure(ws_urls=[], otel_urls=[])
    await asyncio.sleep(0.01)


async def _collect(stream) -> list[str]:
    """收满真实后端的全部文本增量,末了等一拍让 sink worker 写盘(EventBus 异步 drain)。"""
    parts = [p async for p in stream]
    await asyncio.sleep(0.05)
    return parts


def _text_of(content) -> str:
    """递归扁平化 message.content(str 或 [{"type":"text","text":...}] 块列表)。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            _text_of(c.get("text") or c.get("content") or "") if isinstance(c, dict) else _text_of(c)
            for c in content
        )
    return ""


async def _read_single_session(tmp_path) -> list[dict]:
    """读 file_dir 下唯一的会话 jsonl(轮询至终态写入;文件名只取会话 uuid 段)。

    EventBus 落盘是异步 drain(同事件循环),轮询必须 await 让出:最多 40 次
    间隔 0.05s,直至最后一行是 session/end(事件带终态,drain 完成即现);
    超时则返回当前内容,由 _assert_flow 收紧报错。文件名不按 ns 匹配,只数
    目录下唯一 jsonl(会话 id = <ns>/<uuid>,文件取 uuid 段)。
    """
    sess = tmp_path
    for _ in range(40):
        files = sorted(sess.glob("*.jsonl"))
        if len(files) == 1:
            lines = files[0].read_text(encoding="utf-8").splitlines()
            if lines and '"type": "session/end"' in lines[-1]:
                return [json.loads(line) for line in lines]
        await asyncio.sleep(0.05)
    files = sorted(sess.glob("*.jsonl"))
    assert len(files) == 1, f"期望恰 1 个 jsonl,实际 {[p.name for p in files]}"
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


def _assert_flow(events: list[dict], provider: str, model: str, generator: str = "") -> None:
    """断言 FileSink 落盘的会话流符合契约(非流式投影,终态 completed)。

    文本产物契约双路取强:折叠 assistant/message 带文本块(非流式后端 +
    整块合成),或完全流式后端(如 DeepSeek anthropic 兼容端:文本全走
    chunk,折叠消息 content 可为空)下由 session/end.text_chars 证明。
    """
    assert events, "根下应有事件落盘"
    assert events[0]["type"] == "session/start", events[0]
    snapshot = events[0]["data"]  # 快照随 session/start 的 data 落地(契约见 events.py start())
    assert snapshot["provider"] == provider, events[0]
    assert snapshot["model"] == model, events[0]
    if generator:
        assert snapshot["generator"] == generator, events[0]
    types = [e["type"] for e in events]
    assert "assistant/chunk" not in types, "文件只落非流式事件流(chunk 应被投影剔除)"
    user = next(e for e in events if e["type"] == "user/message")
    assert "你好" in _text_of(user["data"]["message"]["content"]), user
    assts = [e for e in events if e["type"] == "assistant/message"]
    assert assts, "应有 assistant/message"
    end = events[-1]
    assert end["type"] == "session/end", end
    assert end["data"]["state"] == "completed", end
    assert end["data"]["ok"] is True, end
    folded = any(_text_of(e["data"]["message"]["content"]).strip() for e in assts)
    assert folded or end["data"].get("text_chars", 0) > 0, "文本产物缺失(折叠空且无 text_chars)"


@pytest.mark.asyncio
async def test_cc_stream_real(tmp_path):
    """cc 真机:spawn 本地 claude CLI,凭证走本地配置(不传 setting_sources/api_key/base_url)。"""
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    config = {
        "model": MODEL,
        "cwd": str(tmp_path),
        "allowed_tools": [],  # "你好" 无工具需求,关工具省流量
        "max_turns": 1,
        "include_partial_messages": True,  # 让 StreamEvent 路径真实产 chunk
    }
    gen = agent.ClaudeCode(config)
    async with gen.session(session_name="real:cc", run_id="r-cc"):
        parts = await _collect(gen.stream("你好"))
    print("cc 回复:", "".join(parts))  # pytest -s 查看真机回显
    assert "".join(parts) != "", "cc 后端应有文本增量"
    _assert_flow(await _read_single_session(tmp_path), "anthropic", MODEL, generator="cc")


@pytest.mark.asyncio
async def test_llm_stream_real(tmp_path):
    """llm 真机:httpx 直连 OpenAI 兼容端;凭证照生成器 env 面(OPENAI_API_KEY,webui/__main__ 同约定)。"""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        pytest.skip("OPENAI_API_KEY 缺失(env;不读任何组件私有文件)")

    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    config = {"model": LLM_MODEL, "base_url": LLM_BASE_URL, "api_key": key}
    payload = {"messages": [{"role": "user", "content": "你好"}],
               "max_tokens": 256, "temperature": 0}
    gen = agent.OpenAI(config)
    async with gen.session(session_name="real:llm", run_id="r-llm"):
        parts = await _collect(gen.stream(
            payload, timeout=httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0)))
    assert "".join(parts) != "", "llm 后端应有文本增量"
    print("llm 回复:", "".join(parts))  # pytest -s 查看真机回显
    _assert_flow(await _read_single_session(tmp_path), "openai", LLM_MODEL, generator="llm")


@pytest.mark.skip(
    reason="TODO(dsh 真机):SDK stdio JSON-RPC runtime 载体在本 dev 机未就绪 —— 单文件 "
           "exe 未构建(deepseek-harness runtime 缺 dsh-jsonrpc-agent-pkg-linux-x64)、"
           "demo bin 裸插件以配置文件目录为解析锚点(仓库 node_modules 无 workspace 链接、"
           "npm 无 dsh-jsonrpc-agent 发布版)、pnpm dsh web 为 http 形态不响应 stdio "
           "initialize。恢复点①:packages/examples/jsonrpc-demo 补 13 个 @deepseek-ai/dsh-* "
           "依赖(内置 cordis 置于该包目录);②:scripts/build-exe-for-python-sdk.ts 构建。",
)
@pytest.mark.asyncio
async def test_dsh_stream_real(tmp_path):
    """dsh 真机:DeepSeek Harness SDK(凭证 SDK 自足,不传 api_key/base_url/cordis)。"""
    work = tmp_path / "work"
    work.mkdir()
    sessions = tmp_path / "dsh-sessions"
    sessions.mkdir()
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    config = {
        "provider": "deepseek-official",  # SDK 缺省域(deepseek_harness.api.py)
        "model": MODEL,
        "cwd": str(work),
        "session_root": str(sessions),  # 隔离,dsh 会话不落真实 ~/.gh-puller
        "max_tokens": 1024,
    }
    gen = agent.Dsh(config)
    async with gen.session(session_name="real:dsh", run_id="r-dsh"):
        parts = await _collect(gen.stream("你好"))
    print("dsh 回复:", "".join(parts))  # pytest -s 查看真机回显
    assert "".join(parts) != "", "dsh 后端应有文本增量"
    _assert_flow(await _read_single_session(tmp_path), "deepseek", MODEL, generator="dsh")


@pytest.mark.asyncio
async def test_codex_stream_real(tmp_path):
    """codex 真机:openai_codex SDK;隔离 home 符号链接引用真实 ~/.codex/auth.json(同 cc 路线)。"""
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    config = {
        "codex_home": str(tmp_path / "codex-home"),  # 必须隔离;_codex_home_setup 建符号链接
        "sandbox": "full_access",  # 生产缺省(deepwiki._codex_options 同值)
        "approval_mode": "auto_review",  # 生产缺省同值
        "model": MODEL_CODEX,
        "cwd": str(tmp_path),
        "timeout_seconds": 300,  # 兜底防止 approval 挂流
    }
    gen = agent.Codex(config)
    async with gen.session(session_name="real:codex", run_id="r-codex"):
        parts = await _collect(gen.stream("你好"))
    print("codex 回复:", "".join(parts))  # pytest -s 查看真机回显
    assert "".join(parts) != "", "codex 后端应有文本增量"
    _assert_flow(await _read_single_session(tmp_path), "openai", MODEL_CODEX, generator="codex")


@pytest.mark.asyncio
async def test_opencode_stream_real(tmp_path):
    """opencode 真机:CLI run --format json 子进程;模型路由/凭证随 opencode 自身配置。

    凭证通道不隔离:凭据由 opencode CLI 自持(~/.local/share/opencode/auth.json,
    同 cc 的 CLI 自持凭证约定);隔离只管注入面(--pure/--auto/config)。
    model 不传 —— 引擎无该轴快照时 model=None(模型由 opencode 配置决定)。
    """
    binpath = shutil.which("opencode")
    if not binpath:
        pytest.skip("opencode 可执行文件缺失(which;见 opencode 官网安装)")
    agent.configure(file_dir=str(tmp_path), ws_urls=[], otel_urls=[])
    config = {
        "opencode_bin": binpath,
        "cwd": str(tmp_path),
        "auto": True,  # 无头自动批准(生产缺省由引擎 adapter 恒置)
        "timeout_seconds": 300,  # 兜底防止权限等待挂流
    }
    gen = agent.OpenCode(config)
    async with gen.session(session_name="real:opencode", run_id="r-opencode"):
        parts = await _collect(gen.stream("你好"))
    print("opencode 回复:", "".join(parts))  # pytest -s 查看真机回显
    assert "".join(parts) != "", "opencode 后端应有文本增量"
    _assert_flow(await _read_single_session(tmp_path), "opencode", None, generator="opencode")
