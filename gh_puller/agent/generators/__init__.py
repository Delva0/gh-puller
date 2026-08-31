"""生成器层:逐生成器独立文件的分类学;GENERATORS(id → 类)为唯一注册真源。

层级纪律(本包 = 生成器层/SDK/HTTP + 各生成器 config 世界同文件):
- 依赖只指向标准库、事件层(../events)、envs 与包内 base/utils;不认识上层
  业务(prompt/任务/缓存)—— 工具桌经 config 通用注入(mcp_servers),零具体
  工具名/业务 env 硬编码;SDK 字段映射/header 投影/隔离组合装配随各生成器文件;
- 每文件一个生成器 = 独立扩展点:各文件含该生成器的本体(id 类属性/config
  元数据/适配器)与 config 世界(TypedDict、CLI/SDK 字段映射、隔离组合装配);
  base.py = 共享骨架(BaseGenerator);utils.py = 包内共享原子(失败异常/异常分类);
- config 在**构造时期**注入 —— `ClaudeCode(config)` 之类,stream/result 只收
  运行时参数(prompt/会话/run 元数据),无 config 参数;
- 无注册表实例:集合就是 `GENERATORS`(id → 类的简单映射),上层自排/校验
  config(键集白名单见各生成器文件 TypedDict)后直接 `GENERATORS[id](config)`
  构造适配器实例、直呼其 stream/result;失败抛 RequestFailedError(detail 为
  失败原因;llm 异常原样,重试留给调用方)。
- with 语义(生成器 = 对应 client 的包装,**唯一入口是 session**):`async with
  GENERATORS[id](config).session(...)` = 一次上游对话 —— 会话元数据(session/
  session_name/run_id/context/retry/meta)在 session 注入,进入 = recorder 装配 +
  session/start + 客户端 spawn(子类 _enter/_exit 钩子随 session 生命周期),退出
  = 收尾(finish/error) + 客户端回收 —— 监控与客户端同寿;
  - stream/result 只收运行时载荷(prompt/payload),**必须在 session 块内调用**
    (元数据不进调用;块外调用 → RuntimeError)。

API 契约(人类开发者正式定义):
- `stream(prompt)`:流式输出 agent 所有 message 产出,其中 assistant 输出包含
  chunk —— 即逐段文本增量 async generator;thinking/工具调用只进监控事件流,
  不构成产出。
- `result(prompt) -> str`:非流式,只拿 agent 最后一轮(最后一次生成轮)的
  assistant 输出;对 llm 而言 result 就是其输出(complete 语义,payload =
  OpenAI 兼容请求体;实现内部经流式端点抽取 —— 事件粒度与 stream 同构,非
  单发整段)。
- 无 text() API。

config 概念契约(与上层 API 契约互补,人类开发者正式定义):
- 配置形态两类:file 类 = 生成器配置是一条 CLI 原生配置文件路径(config_path,
  模型/凭证/服务端点全在文件内,上层原样透传)/ object 类 = 键集即字段
  (provider/model/base_url/api_key)。
- 键名跨生成器收敛:config_path/system_prompt/model/api_key/base_url/cwd/
  mcp_servers/allowed_tools/env;各键到 SDK/CLI 的映射与隔离组合装配属各
  生成器文件(各文件 docstring 自述)。
"""

import asyncio
import os

from .base import BaseGenerator
from .cc import ClaudeCode, ClaudeConfig
from .codex import Codex, CodexConfig, codex_home_path
from .dsh import Dsh, DshConfig, dsh_cordis_path
from .openai import OpenAI, OpenAIConfig
from .opencode import OpenCode, OpenCodeConfig
from .utils import RequestFailedError

__all__ = [
    "GENERATORS",
    "BaseGenerator",
    "ClaudeCode",
    "ClaudeConfig",
    "Codex",
    "CodexConfig",
    "Dsh",
    "DshConfig",
    "OpenAI",
    "OpenAIConfig",
    "OpenCode",
    "OpenCodeConfig",
    "RequestFailedError",
    "codex_home_path",
    "dsh_cordis_path",
]

# ---------------------------------------------------------------------------
# 生成器映射:id → 类(简单映射;构造 = GENERATORS[id](config),config 契约见各文件)
# ---------------------------------------------------------------------------

GENERATORS: dict[str, type[BaseGenerator]] = {"cc": ClaudeCode, "dsh": Dsh,
                                             "codex": Codex, "opencode": OpenCode,
                                             "llm": OpenAI}


# ---------------------------------------------------------------------------
# 直白 API 用法演示:各生成器真实任务(stream/result 上层 API 参考)
# `python -m gh_puller.agent.generators`
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # 各生成器 stream/result 演示(真实任务):访问 GitHub 仓库并一句话介绍 ——
    # result() 最后一轮语义(多轮工具用后只取终稿)在真实任务下可验;附加配置
    # 与缺省隔离见 _demo 内联分支与各生成器文件。
    # 会话语义:一次 `async with cc.session(...)` = 一次上游对话(监控装配/客户端
    # spawn → 收尾/回收);stream/result 只收 prompt(元数据全在 session)。
    # 注:dsh 不在此演示(载体未构建,循环跳过)。

    QUESTION = "请访问 https://github.com/yankils/hello-world 并写一句话介绍这个仓库。"

    async def _demo(gid: str) -> None:
        config: dict = {}
        if gid == "cc":
            # 访问类任务:开放 WebFetch/WebSearch,多轮预算放宽(工具轮次 + 终局)
            config = {"allowed_tools": ["WebFetch", "WebSearch"], "max_turns": 6}
        elif gid == "codex":
            config = {"web_search": True,  # Codex 内置网络搜索:默认安全关闭,须显式启用
                      "sandbox": "full_access", "approval_mode": "auto_review"}
        elif gid == "opencode":
            config = {}  # opencode 自持模型路由/凭据(--pure/--auto 由生成器恒置)
        else:  # llm
            config = {
                "base_url": os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
                "model": os.environ.get("LLM_MODEL") or "gpt-5.6-luna",
                "api_key": os.environ.get("OPENAI_API_KEY"),
            }
        cc = GENERATORS[gid](config)  # 构造期注入 config = 拿到客户端包装
        prompt = (
            {"messages": [{"role": "user", "content": QUESTION}], "max_tokens": 256}
            if gid == "llm" else QUESTION
        )

        print(f"[{gid}] 流式(stream):")
        async with cc.session(session_name=f"demo:{gid}"):  # 一次会话(监控与客户端同寿)
            parts = [c async for c in cc.stream(prompt)]
        print("".join(parts) or "(无产出)")

        print(f"[{gid}] 终局(result):")
        async with cc.session(session_name=f"demo-result:{gid}"):
            final = await cc.result(prompt)
        print(final or "(空)")

    async def _main() -> None:
        for gid in GENERATORS:
            if gid == "dsh":
                continue  # 载体未构建,见 dsh 真机测试 TODO(不阻塞演示)
            try:
                await _demo(gid)
            except Exception as e:
                print(f"[{gid}] 失败: {type(e).__name__}: {e}")
        print("演示结束")

    asyncio.run(_main())
