"""DeepWiki 兼容后端(公共门面):前端契约沿用 deepwiki-open,运行引擎替换为 Claude Code agent + graphify。

来源与协议:
- 前端契约与提示词来自 deepwiki-open(MIT License, Copyright (c) 2024 Sheing Ng)。
- 原后端 RAG(adalflow + FAISS 检索)整体切除:索引 = graphify.extract 纯本地 AST
  建图(<DEEPWIKI_ROOT>/graphify/<repo>/graph.json);检索 = graphify.query 封装为
  agent 的图工具。
- 双路生成(envs.DEEPWIKI_GENERATOR 统一开关):agent 路(cc/dsh/codex,Claude Agent SDK
  或同类 harness 自读代码 + 图工具,wiki 交付件 Write 落盘)/ llm 路(deepwiki-open 原式
  单次补全,检索上下文 = graphify 子图 → 真实代码行窗)。提示词全部为 deepwiki-open 原文。

已知简化(v1,详见 gh_puller/envs.py):克隆走 git CLI;token 上限按字符数/4 估算;
语言仅 en/zh;聊天记忆由单次 agent 会话内承。模型极简:选型 dict(choice)= {generator,
generator_config};解析/校验唯一知识源在 deepwiki.utils._resolve_generator 纯函数
(agent 侧契约见 gh_puller.agent.configs.py)。

本包是引擎(无 FastAPI 依赖):HTTP 端点层(SSE/WS)在 apps/deepwiki-webui/server/app.py;
任务状态机/调度/进度投影在其 server/tasks.py。续跑落盘(deepwiki_resume_*)语义见 ./cache.py。

子模块结构(本文件只做公共白名单 re-export,不含任何实现):
- utils      generator 选型/判等/凭证规则簇 + 域内日志(_log)+ repo 键
- models     引擎契约 dataclass 族(零 pydantic)+ dict→model 构造器
- cache      产物布局(graphify 图/wiki 成品缓存/续跑状态) + 导出 + 判等摘要族
- pipeline   生成协议层(双路包装类 + 四路装配 + 模型产出解析/引用渲染 +
             索引保障服务 + chat/codemap 服务入口;离核心概念近的 helper 收编本层)

私有成员(下划线名)不经此门面 —— 直接从对应子模块导入;
monkeypatch 同理打在属主子模块上(如 deepwiki.pipeline.llm_stream、
AgentWikiPipeline._deliver 或 agent 适配器单例方法)。
"""

from .. import envs, graphify  # noqa: F401 —— 测试 pop+delattr 强刷依赖包顶这一绑定
from .cache import (
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
    write_resume_state,
)
from .models import (
    CodeMap,
    CodeMapCitation,
    CodeMapSection,
    CodeMapStep,
    RepoInfo,
    WikiPage,
    WikiSection,
    WikiStructureModel,
    codemap_of,
    wiki_structure_of,
)
from .pipeline import (
    AgentWikiPipeline,
    LlmWikiPipeline,
    WikiPipeline,
    chat_stream,
    ensure_index,
    generate_codemap,
)
from .utils import repo_key_of

__all__ = [
    # models(契约 dataclass 族)
    "RepoInfo",
    "WikiPage",
    "WikiSection",
    "WikiStructureModel",
    "CodeMap",
    "CodeMapCitation",
    "CodeMapStep",
    "CodeMapSection",
    # 构造器(dict → model)
    "codemap_of",
    "wiki_structure_of",
    # 仓库键(utils)
    "repo_key_of",
    # 索引保障服务(pipeline;/repo/prepare 与 wiki 任务主流程共用)
    "ensure_index",
    # 缓存 / 续跑状态 / 导出(cache)
    "save_generated_wiki",
    "save_wiki_cache",
    "read_wiki_cache",
    "delete_wiki_cache",
    "list_wiki_cache",
    "list_processed_projects",
    "export_wiki",
    "wiki_cache_exists",
    "write_resume_state",
    "read_resume_state",
    "delete_resume_state",
    # 服务入口(pipeline)
    "chat_stream",
    "generate_codemap",
    "WikiPipeline",
    "AgentWikiPipeline",
    "LlmWikiPipeline",
]
