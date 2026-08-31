"""DeepWiki 兼容后端(公共门面):前端契约沿用 deepwiki-open,运行引擎替换为 Claude Code agent + 代码图谱。

来源与协议:
- 前端契约与提示词来自 deepwiki-open(MIT License, Copyright (c) 2024 Sheing Ng)。
- 引擎无本地向量检索:索引 = 图产物本地 AST 建图
  (<DEEPWIKI_ROOT>/图产物根/<repo>/graph.json);检索 = 图查询封装为 agent 的
  图工具 —— 图谱知识(建图/查询/检索/MCP 装配)在 webui 组装层
  apps/deepwiki-webui/server/generators.py,经 generator_config(覆盖构造参数集)
  注入:agent 路得 mcp_servers/env/工具名与工具指引文本(tool_note/codemap_note),
  所有路得图服务(generator_config["graph"]:ready/context);本包零
  claude_agent_sdk 依赖且不做任何工具假设。
- 双路生成(缺省开关 = 上层 webui 在边界注入的 DEEPWIKI_GENERATOR;引擎空选型内建
  cc):agent 路(Claude Agent SDK
  或同类 harness 自读代码 + 图工具,wiki 交付件 Write 落盘)/ llm 路(deepwiki-open 原式
  单次补全,检索上下文 = 图服务子图 → 真实代码行窗)。提示词全部为 deepwiki-open 原文。

已知简化(v1,详见 gh_puller/envs.py):克隆走 git CLI;token 上限按字符数/4 估算;
语言仅 en/zh;聊天记忆由单次 agent 会话内承。模型极简:选型 = generator +
generator_config 两个散装参数(术语与解析唯一知识源见 utils.py docstring;
agent 侧契约见 gh_puller.agent.generators 各生成器文件)。

本包是引擎(无 FastAPI 依赖):HTTP 端点层(SSE/WS)在 apps/deepwiki-webui/server/app.py;
任务状态机/调度/进度投影在其 server/tasks.py。续跑落盘(resume_*)语义见 ./wiki.py。

子模块结构(本文件只做公共白名单 re-export,不含任何实现):
- utils      wiki/chat/codemap 共用 helper 与判等/解析知识(见其 docstring)
- wiki / chat / codemap  三个功能主线,各自 docstring 自述

私有成员(下划线名)不经此门面 —— 直接从对应子模块导入;
monkeypatch 同理打在属主子模块上(如 deepwiki.utils.llm_stream、
AgentWikiPipeline._deliver 或 agent 适配器单例方法)。
"""

from .. import envs  # noqa: F401 —— 测试 pop+delattr 强刷依赖包顶这一绑定
from .chat import chat_stream
from .codemap import (
    CodeMap,
    CodeMapCitation,
    CodeMapSection,
    CodeMapStep,
    codemap_of,
    generate_codemap,
)
from .utils import repo_key_of
from .wiki import (
    AgentWikiPipeline,
    LlmWikiPipeline,
    WikiPage,
    WikiPipeline,
    WikiSection,
    WikiStructureModel,
    delete_resume_state,
    delete_wiki_cache,
    export_wiki,
    list_processed_projects,
    list_wiki_cache,
    read_resume_state,
    read_wiki_cache,
    save_generated_wiki,
    save_wiki_cache,
    wiki_cache_exists,
    wiki_structure_of,
    write_resume_state,
)

__all__ = [
    "AgentWikiPipeline",
    "CodeMap",
    "CodeMapCitation",
    "CodeMapSection",
    "CodeMapStep",
    "LlmWikiPipeline",
    # 契约 dataclass 族(wiki / codemap)
    "WikiPage",
    "WikiPipeline",
    "WikiSection",
    "WikiStructureModel",
    # 服务入口:chat/codemap/wiki 三主线
    "chat_stream",
    # 构造器(dict → model)
    "codemap_of",
    "delete_resume_state",
    "delete_wiki_cache",
    "export_wiki",
    "generate_codemap",
    "list_processed_projects",
    "list_wiki_cache",
    "read_resume_state",
    "read_wiki_cache",
    # 仓库键 — utils
    "repo_key_of",
    # 缓存/续跑状态/导出:utils
    "save_generated_wiki",
    "save_wiki_cache",
    "wiki_cache_exists",
    "wiki_structure_of",
    "write_resume_state",
]
