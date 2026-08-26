"""生成器类约定 / resolve_generator 纯函数 / 统一分派 的本地测试。

零 SDK / 零网络 / 零 token:适配器实例只被 mock(直接 monkeypatch 单例的方法),
resolve_generator 纯函数驱动(get_env 注入替代真实环境),llm 路以假 stream 捕获
url/payload/api_key。覆盖:generator 映射与类属性(config_kind/缺省/env 名)、
file 类路径解析与白名单校验、object 类解析(provider 唯一 openai)、未知 id /
混键 / 文件不存在运行前失败、分派 options 透传与 RequestFailedError 包装、
事件顶层含 generator/provider/model 且不含凭证。
"""

import json
import types

import pytest

from gh_puller.agent import (
    GENERATORS,
    RequestFailedError,
    generate_result,
    generate_stream,
    generators,
    resolve_generator,
)


def _duck_options(**kw):
    return types.SimpleNamespace(**kw)


def _get_env(**values):
    return lambda key: values.get(key, "")


# ---------------------------------------------------------------------------
# generator 映射与类属性(极简:id → 实例;配置认知在类上)
# ---------------------------------------------------------------------------


def test_generators_map_covers_four_routes():
    assert sorted(GENERATORS) == ["cc", "codex", "dsh", "llm"]
    assert isinstance(GENERATORS["cc"], generators.ClaudeCode)
    assert isinstance(GENERATORS["dsh"], generators.Dsh)
    assert isinstance(GENERATORS["codex"], generators.Codex)
    assert isinstance(GENERATORS["llm"], generators.OpenAI)
    assert GENERATORS["cc"].config_kind == "file"
    assert GENERATORS["dsh"].config_kind == "file"
    assert GENERATORS["codex"].config_kind == "file"
    assert GENERATORS["llm"].config_kind == "object"
    assert GENERATORS["cc"].config_default is not None  # ~/.claude/settings.json
    assert GENERATORS["dsh"].config_path_env == "DEEPWIKI_DSH_CORDIS"
    assert GENERATORS["llm"].provider == "openai"


# ---------------------------------------------------------------------------
# file 类:config_path 解析(显式 > env > 缺省;存在性校验)
# ---------------------------------------------------------------------------


def test_resolve_file_default_path():
    gen, resolved = resolve_generator("cc", {}, get_env=_get_env())  # 无 env → 类缺省
    assert gen.id == "cc"
    assert resolved["config_path"] == GENERATORS["cc"].config_default  # ~/.claude/settings.json


def test_resolve_file_explicit_beats_env(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{}", encoding="utf-8")
    gen, r1 = resolve_generator("cc", {"config_path": str(p)}, get_env=_get_env())
    assert r1["config_path"] == str(p)
    p2 = tmp_path / "env.json"
    p2.write_text("{}", encoding="utf-8")
    gen, r2 = resolve_generator("cc", {},
                                get_env=_get_env(DEEPWIKI_CC_CONFIG=str(p2)))
    assert r2["config_path"] == str(p2)  # env > class 缺省


def test_resolve_file_tilde_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / "s.json"
    p.write_text("{}", encoding="utf-8")
    _, resolved = resolve_generator("cc", {"config_path": f"~/{p.name}"})
    assert resolved["config_path"] == str(p)


def test_resolve_file_missing_path_fails():
    with pytest.raises(ValueError, match="配置文件不存在"):
        resolve_generator("cc", {"config_path": "/no/such/file.json"})


def test_resolve_file_empty_keeps_default(tmp_path):
    """dsh 无 class 缺省:空配置 → config_path ""(适配器内置隔离组合)。"""
    gen, resolved = resolve_generator("dsh", {})
    assert resolved == {"config_path": ""}


# ---------------------------------------------------------------------------
# object 类(llm)解析 + 白名单
# ---------------------------------------------------------------------------


def test_resolve_object_values_and_defaults():
    gen, r = resolve_generator("llm", {"provider": "openai", "model": "m1",
                                       "api_key": "sk-1", "base_url": "https://x/v1"})
    assert (r["model"], r["api_key"], r["base_url"]) == ("m1", "sk-1", "https://x/v1")
    gen, r2 = resolve_generator("llm", {}, get_env=_get_env(LLM_MODEL="em",
                                                            OPENAI_API_KEY="ek",
                                                            OPENAI_BASE_URL="https://e/v1"))
    assert (r2["model"], r2["api_key"], r2["base_url"]) == ("em", "ek", "https://e/v1")
    gen, r3 = resolve_generator("llm", {"provider": "openai"})
    assert (r3["model"], r3["api_key"]) == ("", None)  # → 端点缺省
    assert r3["base_url"] == "https://api.openai.com/v1"


def test_resolve_unknown_and_mixed_keys_rejected(tmp_path):
    with pytest.raises(ValueError, match="未知 generator"):
        resolve_generator("claude", {})
    p = tmp_path / "s.json"
    p.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="仅接受.*config_path"):
        resolve_generator("cc", {"config_path": str(p), "provider": "openai"})
    with pytest.raises(ValueError, match="非法键"):
        resolve_generator("llm", {"foo": "bar"})
    with pytest.raises(ValueError, match="非法组合"):
        resolve_generator("llm", {"provider": "anthropic"})


# ---------------------------------------------------------------------------
# 分派:agent 路 options 原样透传 + RequestFailedError 包装;llm 路 model 注入
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_agent_passthrough_and_wrap(monkeypatch):
    captured: dict = {}

    async def fake_cc(options, prompt, **kw):
        captured["options"] = options
        yield "cc"

    monkeypatch.setattr(generators._claude, "stream", fake_cc)
    opts = _duck_options(settings="/tmp/x.json")
    out = [c async for c in generate_stream("q", target={"generator": "cc"}, options=opts)]
    assert out == ["cc"] and captured["options"] is opts  # 原样透传

    async def failing_cc(options, prompt, **kw):
        raise RequestFailedError("boom")
        yield ""  # pragma: no cover

    monkeypatch.setattr(generators._claude, "stream", failing_cc)
    with pytest.raises(RuntimeError, match="agent 执行失败: boom"):
        async for _ in generate_stream("q", target={"generator": "cc"}, options=opts):
            pass


@pytest.mark.asyncio
async def test_generate_result_agent_path_passes_options(monkeypatch):
    """回归:generate_result agent 路传 options(曾引用已删变量 bound → NameError)。"""
    captured: dict = {}

    async def fake_cc_result(options, prompt, **kw):
        captured["options"] = options
        return "final"

    monkeypatch.setattr(generators._claude, "result", fake_cc_result)
    opts = _duck_options(settings="/tmp/dw-settings.json")
    out = await generate_result("q", target={"generator": "cc"}, options=opts)
    assert out == "final"
    assert captured["options"] is opts


@pytest.mark.asyncio
async def test_dispatch_llm_injects_model_and_creds(monkeypatch):
    captured: dict = {}

    async def fake_stream(*, url, payload, api_key=None, **kw):
        captured.update(url=url, payload=payload, api_key=api_key)
        yield "llm"

    monkeypatch.setattr(generators._openai, "stream", fake_stream)
    await generate_stream("", target={
        "generator": "llm",
        "generator_config": {"provider": "openai", "model": "llm-m",
                             "api_key": "sk-l", "base_url": "https://llm/v1"}},
        options={"messages": [{"role": "user", "content": "q"}]}).__anext__()
    assert captured["url"] == "https://llm/v1"
    assert captured["api_key"] == "sk-l"
    assert captured["payload"]["model"] == "llm-m"
    assert "sk-l" not in json.dumps(captured["payload"])  # 凭证不进请求体


@pytest.mark.asyncio
async def test_run_envelope_stamps_generator_and_keeps_creds_out():
    """事件信封顶层含 generator/provider/model;凭证绝不进事件流。"""
    from gh_puller.agent.events import EventRecorder
    from gh_puller.agent.sinks import configure, ensure_bus

    configure(file=False, ws_urls=[], otel_urls=[])
    got: list = []

    async def _recv(evt):
        got.append(evt)

    ensure_bus().add(_recv)
    try:
        run = EventRecorder("s1", "deepseek", "m1", generator="dsh", label="t", run_id="r1")
        run.start()
        run.user_message({"role": "user", "content": [{"type": "text", "text": "q"}]})
        run.finish(True)
        import asyncio
        await asyncio.sleep(0.02)
        start = next(g for g in got if g["type"] == "session/start")
        assert (start["generator"], start["provider"], start["model"]) == (
            "dsh", "deepseek", "m1")
        assert "api_key" not in json.dumps(got) and "base_url" not in json.dumps(got)
    finally:
        configure(file=False, ws_urls=[], otel_urls=[])
