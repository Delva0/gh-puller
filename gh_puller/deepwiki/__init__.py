"""DeepWiki 兼容后端(公共门面):前端契约沿用 deepwiki-open,运行引擎替换为 Claude Code agent + graphify。

来源与协议:
- 前端契约与提示词来自 deepwiki-open(MIT License, Copyright (c) 2024 Sheing Ng)。
- 原后端 RAG(adalflow + FAISS 检索)整体切除:索引 = graphify.extract 纯本地 AST
  建图(<DEEPWIKI_ROOT>/graphify/<repo>/graph.json);检索 = graphify.query 封装为
  agent 的图工具。
- 双路生成(缺省开关 = 上层 webui 在边界注入的 DEEPWIKI_GENERATOR;引擎空选型内建
  cc):agent 路(cc/dsh/codex,Claude Agent SDK
  或同类 harness 自读代码 + 图工具,wiki 交付件 Write 落盘)/ llm 路(deepwiki-open 原式
  单次补全,检索上下文 = graphify 子图 → 真实代码行窗)。提示词全部为 deepwiki-open 原文。

已知简化(v1,详见 gh_puller/envs.py):克隆走 git CLI;token 上限按字符数/4 估算;
语言仅 en/zh;聊天记忆由单次 agent 会话内承。模型极简:选型为 generator +
generator_config 两个散装字段(engine 全部函数同签名;wire 的 target dict 在 app 层拆包);
解析/校验唯一知识源在 deepwiki.utils.resolve_generator 纯函数(agent 侧契约见
gh_puller.agent.configs.py)。

本包是引擎(无 FastAPI 依赖):HTTP 端点层(SSE/WS)在 apps/deepwiki-webui/server/app.py;
任务状态机/调度/进度投影在其 server/tasks.py。续跑落盘(resume_*)语义见 ./wiki.py。

子模块结构(本文件只做公共白名单 re-export,不含任何实现):
- cache      产物布局(graphify 图/wiki 成品缓存/续跑状态) + 导出 + 判等摘要族
- utils      generator 选型/判等/凭证规则簇 + 域内日志(log)+ repo 键 +
             跨功能通用(四路装配 adapter/llm 传输/llm 路补全协议 + 检索工具簇/
             图产物路径与索引/判等摘要族/提示词共性常量/索引保障服务;
             契约 dataclass 已消除全并入功能主线)
- wiki        wiki 主线:契约 dataclass 族 + 成品缓存/续跑状态/导出 IO +
             双路包装类(AgentWikiPipeline/LlmWikiPipeline + _wiki_pipeline
             分派)+ 结构 XML 解析 + 引用渲染 + wiki 提示词
- chat        chat 主线:chat_stream 入口 + 双路实现 + 历史转写 + 深研究模板
- codemap     codemap 主线:契约 dataclass 族 + generate_codemap 入口 + 双路实现 +
             提示词 + 引用接地

私有成员(下划线名)不经此门面 —— 直接从对应子模块导入;
monkeypatch 同理打在属主子模块上(如 deepwiki.utils.llm_stream、
AgentWikiPipeline._deliver 或 agent 适配器单例方法)。
"""

from .. import envs, graphify  # noqa: F401 —— 测试 pop+delattr 强刷依赖包顶这一绑定
from .chat import chat_stream
from .codemap import (
    CodeMap,
    CodeMapCitation,
    CodeMapSection,
    CodeMapStep,
    codemap_of,
    generate_codemap,
)
from .utils import ensure_index, repo_key_of
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
    # 索引保障服务(utils;/repo/prepare 与 wiki 任务主流程共用)
    "ensure_index",
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
