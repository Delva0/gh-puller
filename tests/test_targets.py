"""Generator / Provider / Model 注册表与统一 target 分派的本地测试。

零 SDK / 零网络 / 零 token:适配器实例只被 mock(直接 monkeypatch 单例的方法),
resolve/bind 纯函数驱动,llm 路以假 llm_stream 捕获 url/payload/api_key。
覆盖 Test Plan:注册表覆盖三 provider/四 generator/四合法组合、
codex+openai 与 llm+openai 共享 provider 配置但 wire 不同、显式 target > env >
SDK 缺省的逐字段优先级、非法组合/能力不匹配运行前失败、四路收到正确
model/key/base URL 且日志无凭证、并发 target 不修改全局环境/不串凭证、
事件顶层含 generator/provider/model 且不含凭证。
"""

import json
import sys
import types

import pytest

from gh_puller.agent import (
    GENERATORS,
    PROVIDERS,
    ResolvedTarget,
    adapters,
    generate_stream,
    resolve_target,
)


# ---------------------------------------------------------------------------
# 注册表覆盖
# ---------------------------------------------------------------------------


def test_registry_covers_providers_generators_combos():
    """注册表:三个 providers、四个 generators、四个合法组合与各自能力。"""
    assert sorted(PROVIDERS) == ["anthropic", "deepseek", "openai"]
    assert sorted(GENERATORS) == ["cc", "codex", "dsh", "llm"]
    expected = {"cc": "anthropic", "dsh": "deepseek", "codex": "openai", "llm": "openai"}
    for gid, pid in expected.items():
        spec = GENERATORS[gid]
        assert spec.default_provider == pid
        assert (pid,) == spec.providers
        assert spec.capability in PROVIDERS[pid].capabilities
    # 同一 openai provider 描述两种协议面(由 generator 决定 surface,不另设 provider)
    assert "responses" in PROVIDERS["openai"].capabilities
    assert "chat-completions" in PROVIDERS["openai"].capabilities
    assert "openai-compatible" not in PROVIDERS


def test_resolve_default_target_falls_back_to_env():
    """空 target → env(DEEPWIKI_GENERATOR + 默认 provider)回退;gen 缺省 cc。"""
    from gh_puller import envs as e

    t = resolve_target(None)
    assert t.generator == e.DEEPWIKI_GENERATOR
    assert t.provider == GENERATORS[t.generator].default_provider


# ---------------------------------------------------------------------------
# 逐字段优先级:显式 target > provider/generator 环境变量 > SDK 缺省
# ---------------------------------------------------------------------------


def test_resolve_priority_explicit_beats_env(monkeypatch):
    from gh_puller import envs as e

    monkeypatch.setattr(e, "CC_MODEL", "env-model")
    monkeypatch.setattr(e, "ANTHROPIC_API_KEY", "env-key")
    monkeypatch.setattr(e, "ANTHROPIC_BASE_URL", "https://env/v1")
    t = resolve_target({"generator": "cc", "model": "m1", "api_key": "sk-1",
                        "base_url": "https://x/v1"})
    assert (t.model, t.api_key, t.base_url) == ("m1", "sk-1", "https://x/v1")
    t2 = resolve_target({"generator": "cc"})
    assert (t2.model, t2.api_key, t2.base_url) == (
        "env-model", "env-key", "https://env/v1")
    t3 = resolve_target({"generator": "cc", "provider": "anthropic",
                         "api_key": "", "base_url": ""})
    assert t3.api_key == "env-key"  # 空串视同未提供(env 回退)


def test_resolve_sdk_default_when_no_env(monkeypatch):
    from gh_puller import envs as e

    monkeypatch.setattr(e, "CC_MODEL", "")
    monkeypatch.setattr(e, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(e, "ANTHROPIC_BASE_URL", "")
    t = resolve_target({"generator": "cc", "provider": "anthropic"})
    assert t.model == ""  # → SDK 缺省
    assert t.api_key is None  # → SDK 原生登录
    assert t.base_url is None  # → SDK 原生端点


def test_resolve_openai_base_url_default():
    """openai provider 的 base_url 缺省 = 官方端点(兼容服务经自定义 base_url 接入)。"""
    t = resolve_target({"generator": "llm", "provider": "openai"})
    assert t.base_url == "https://api.openai.com/v1"


def test_resolve_dsh_sdk_route_not_reversed():
    """gh provider 语义(deepseek)与 dsh SDK 原生路由名(deepseek-official)区隔。"""
    t = resolve_target({"generator": "dsh"})
    assert t.provider == "deepseek"
    assert t.generator == "dsh"


# ---------------------------------------------------------------------------
# 非法组合 / 能力不匹配:运行前失败
# ---------------------------------------------------------------------------


def test_resolve_illegal_combo_fails_before_run():
    with pytest.raises(ValueError, match="非法组合"):
        resolve_target({"generator": "llm", "provider": "anthropic"})
    with pytest.raises(ValueError, match="非法组合"):
        resolve_target({"generator": "cc", "provider": "openai"})
    with pytest.raises(ValueError, match="未知 generator"):
        resolve_target({"generator": "claude"})
    # llm 只支持 openai:openai-compatible 是 openai 的部署形态,不是独立 id
    with pytest.raises(ValueError, match="非法组合"):
        resolve_target({"generator": "llm", "provider": "openai-compatible"})


@pytest.mark.asyncio
async def test_dispatcher_rejects_illegal_combo_before_run():
    with pytest.raises(ValueError, match="非法组合"):
        async for _ in generate_stream("hi", target={"generator": "codex",
                                                     "provider": "anthropic"}):
            pass


# ---------------------------------------------------------------------------
# 绑定:codex/llm 共享 openai provider 配置,wire 不同
# ---------------------------------------------------------------------------


def _duck_options(**kw):
    return types.SimpleNamespace(**kw)


def test_bind_cc_injects_env_and_model():
    """cc + anthropic:resolved key/base URL 注入 options.env(副本,原地不动)。"""
    opts = types.SimpleNamespace(model="", env={"DSH_X": "1"})
    bound = adapters._bind_cc_options(
        opts, ResolvedTarget("cc", "anthropic", "claude-sonnet-5",
                             "sk-cc", "https://proxy/v1"))
    assert bound.env == {"DSH_X": "1", "ANTHROPIC_API_KEY": "sk-cc",
                         "ANTHROPIC_BASE_URL": "https://proxy/v1"}
    assert bound.model == "claude-sonnet-5"
    assert opts.env == {"DSH_X": "1"}  # 原对象不动:并发 target 不互串


def test_bind_dsh_passthrough_model_key_base():
    opts = _duck_options(provider="deepseek-official")
    bound = adapters._bind_dsh_options(
        opts, ResolvedTarget("dsh", "deepseek", "deepseek-v4-flash",
                             "sk-d", "https://ds/v1"))
    assert bound.provider == "deepseek-official"  # SDK 路由名不受扰
    assert (bound.model, bound.api_key, bound.base_url) == (
        "deepseek-v4-flash", "sk-d", "https://ds/v1")
    assert not hasattr(opts, "model")  # 原对象不被写


def test_bind_codex_uses_model_provider_openai_and_token():
    """codex + openai:model_provider=openai;key 走 token 登录;base_url 经 config override。"""
    opts = _duck_options(model="x", config_overrides={"a": 1})
    bound = adapters._bind_codex_options(
        opts, ResolvedTarget("codex", "openai", "gpt-5.6-luna",
                             "sk-c", "https://codex/v1"))
    assert bound.model_provider == "openai"
    assert bound.model == "gpt-5.6-luna"
    assert bound.token == "sk-c"
    assert bound.config_overrides == {
        "model_providers": {"openai": {"base_url": "https://codex/v1"}}, "a": 1}
    assert getattr(opts, "token", None) is None  # 原对象不动


def test_bind_llm_payload_model_injected():
    """llm + openai:model 入 payload;url/key 由 dispatcher 传(见 test_dispatcher_llm)。"""
    payload = adapters._bind_llm_payload(
        {"messages": [{"role": "user", "content": "q"}]},
        ResolvedTarget("llm", "openai", "m-llm", "sk-l", "http://llm/v1"))
    assert payload["model"] == "m-llm"
    assert len(payload["messages"]) == 1


# ---------------------------------------------------------------------------
# dispatcher:四路收到正确 model/key/base URL;无凭证进事件/错误文本
# ---------------------------------------------------------------------------

_PUBLIC_OK = (
    ("cc", "anthropic"), ("dsh", "deepseek"), ("codex", "openai"), ("llm", "openai"))


@pytest.mark.asyncio
async def test_dispatcher_binds_active_target_per_generator(monkeypatch):
    """四路 dispatcher:mock 每路 stream 捕获绑定后的 options/payload 字段。"""
    captured = {}

    async def fake_cc(options, prompt, **kw):
        captured["cc"] = options
        yield "cc"

    async def fake_dsh(options, prompt, **kw):
        captured["dsh"] = options
        yield "dsh"

    async def fake_codex(options, prompt, **kw):
        captured["codex"] = options
        yield "codex"

    monkeypatch.setattr(adapters._claude, "stream", fake_cc)
    monkeypatch.setattr(adapters._dsh, "stream", fake_dsh)
    monkeypatch.setattr(adapters._codex, "stream", fake_codex)

    async def fake_llm(*, url, payload, api_key=None, **kw):
        captured["llm"] = (url, payload, api_key)
        yield "llm"

    monkeypatch.setattr(adapters, "llm_stream", fake_llm)

    target = {"generator": "cc", "provider": "anthropic", "model": "cc-m",
              "api_key": "sk-cc","base_url": "https://cc/v1"}
    assert [c async for c in generate_stream("q", target=target,
                                             options=_duck_options(model="x"))] == ["cc"]
    cc_opts = captured["cc"]
    assert cc_opts.model == "cc-m"  # 绑定覆盖 options 原 model
    assert cc_opts.env == {"ANTHROPIC_API_KEY": "sk-cc",
                           "ANTHROPIC_BASE_URL": "https://cc/v1"}

    target["generator"] = "dsh"
    target["provider"] = "deepseek"
    target["api_key"] = "sk-d"
    target["base_url"] = "https://ds/v1"
    assert [c async for c in generate_stream("q", target=target,
                                             options=_duck_options())] == ["dsh"]
    dsh_opts = captured["dsh"]
    assert (dsh_opts.model, dsh_opts.api_key, dsh_opts.base_url) == (
        "cc-m", "sk-d", "https://ds/v1")

    target["generator"] = "codex"
    target["provider"] = "openai"
    target["api_key"] = "sk-c"
    target["base_url"] = "https://cx/v1"
    assert [c async for c in generate_stream("q", target=target,
                                             options=_duck_options())] == ["codex"]
    codex_opts = captured["codex"]
    assert codex_opts.model_provider == "openai"
    assert codex_opts.token == "sk-c"
    assert codex_opts.config_overrides["model_providers"]["openai"]["base_url"] == "https://cx/v1"

    target["generator"] = "llm"
    target["provider"] = "openai"
    target["api_key"] = "sk-l"
    target["base_url"] = "https://llm/v1"
    target["model"] = "llm-m"
    await generate_stream("", target=target,
                          options={"messages": [{"role": "user", "content": "q"}]}).__anext__()
    url, payload, api_key = captured["llm"]
    assert url == "https://llm/v1"
    assert api_key == "sk-l"
    assert payload["model"] == "llm-m"
    # 凭证不进请求体
    blob = json.dumps(payload)
    assert "sk-l" not in blob


@pytest.mark.asyncio
async def test_run_envelope_stamps_generator_and_keeps_creds_out():
    """事件信封顶层含 generator/provider/model(session/start 的 data 同样),
    凭证(api_key/base_url)绝不进事件流(信封只持久化公开三元组)。"""
    from gh_puller.agent.adapters import _Run
    from gh_puller.agent.sinks import configure, ensure_bus

    configure(file=False, ws_urls=[], otel_urls=[])
    got: list = []

    async def _recv(evt):
        got.append(evt)

    ensure_bus().add(_recv)
    try:
        run = _Run("s1", "deepseek", "m1", generator="dsh", label="t", run_id="r1")
        run.start()
        run.user_message({"role": "user", "content": [{"type": "text", "text": "q"}]})
        run.finish(True)
        import asyncio
        await asyncio.sleep(0.02)
        start = next(g for g in got if g["type"] == "session/start")
        assert (start["generator"], start["provider"], start["model"]) == (
            "dsh", "deepseek", "m1")
        assert start["data"]["generator"] == "dsh"  # OTel 等 sink 从 data 读取
        assert "api_key" not in json.dumps(got) and "base_url" not in json.dumps(got)
    finally:
        configure(file=False, ws_urls=[], otel_urls=[])


def test_copy_options_leaves_original_for_concurrency():
    """并发 target:共享的 options 副本绑定,原对象零改动(不串 provider/cred)。"""
    opts = types.SimpleNamespace(model="base", env={"K": "1"})
    a = adapters._bind_cc_options(opts, ResolvedTarget("cc", "anthropic", "ma", "ka", None))
    b = adapters._bind_cc_options(opts, ResolvedTarget("cc", "anthropic", "mb", "kb", None))
    assert (a.model, a.env.get("ANTHROPIC_API_KEY")) == ("ma", "ka")
    assert (b.model, b.env.get("ANTHROPIC_API_KEY")) == ("mb", "kb")
    assert (opts.model, opts.env) == ("base", {"K": "1"})
