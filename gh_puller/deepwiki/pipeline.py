"""生成协议层(双路包装类 + 服务入口)——拆分中间态(commit 1):跨功能通用
helper 已迁出(提示词共性/四路装配/检索簇/research 协议/索引保障 → utils.py;
wiki 的解析/渲染/页面提示词 → wiki.py;codemap 提示词/接地 → codemap.py;
chat 延续回退 → chat.py),本模块经 re-export 保持裸名解析(类仍暂居,待
二次迁移随类入 wiki/chat/codemap 后本文件删除)。

类职责注释(仍适用直至迁移):
- 提示词组装、对话历史转写、检索上下文注入、agent 交付件路径与 llm 单次补全
  生成链收进各 pipeline 类(self._xxx());协议方法只收 request(**零任务运行时
  包装**:进度/状态/去重均为 app 侧 runtime,见 apps/deepwiki-webui/server/tasks.py),
  页面内容在返回前完成终态格式化(_finalize_page_content,与续跑水合同式)。
- claude_agent_sdk import 已随四路装配迁 utils(本模块 sdk-free 化)。
- 服务分派:_wiki_pipeline 按 choice.generator 双路(wiki;chat/codemap 同开关,
  模块级 chat_stream/generate_codemap 包装);RequestFailedError → RuntimeError
  包装(_failure)在 utils。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path
from typing import Any

from .. import envs  # 模块对象绑定:属性一律调用时取(patch/强刷活性)
from ..agent import RequestFailedError
from ..utils import (
    Repo,
    _estimate_tokens,
    _event,
    _extract_json,
    _find_readme_path,
    _phase,
    _sanitize_path_seg,
)

# ---------------------------------------------------------------------------
# 拆分 shim:已迁定义经此保持裸名解析(类仍在本模块;测试直导私有名保持可用)。
# 本文件仅剩这些转发 + 下方类;转发名不加 noqa 的为仍在本模块被调用者。
# ---------------------------------------------------------------------------
from . import utils  # noqa: E402 —— 类体内经 utils.xxx 属性调用(monkeypatch 位点活性,见本文件头)
from .cache import _AGENT_CACHE_DIRNAME, _generator_digest, _index_ready, _wiki_cache_dir
from .chat import _resolve_chat_continuation  # noqa: E402,F401 —— 类(chat_stream)裸名调用/补全 shim
from .codemap import (  # noqa: E402,F401
    _CODEMAP_ENRICH_PROMPT,
    _CODEMAP_SKELETON_PROMPT,
    _ground_citations,
    _locate_snippet,
)
from .models import WikiPage, WikiStructureModel, codemap_of
from .utils import (  # noqa: E402,F401
    _FILE_INLINE_CAP,
    _LANGUAGE_NAMES,
    _LANGUAGE_NAMES_RAW,
    _SIMPLE_CHAT_SYSTEM_PROMPT,
    _adapter,
    _agent_note,
    _build_service_prompt,
    _failure,
    _format_subgraph_context,
    _graphify_context,
    _graphify_mcp,
    _graphify_server,
    _is_token_limit_error,
    _language_name,
    _llm_research_chat,
    _llm_research_stream,
    _log,
    _prompt_fmt,
    _resolve_generator,
    _run_extract,
    _subgraph_hits,
    _subgraph_src_blocks,
    ensure_index,
    llm_complete,
    llm_stream,
)
from .wiki import (  # noqa: E402,F401
    _COMPREHENSIVE_STRUCTURE,
    _CONCISE_STRUCTURE,
    RepoUrlContext,
    _build_page_prompt,
    _finalize_page_content,
    parse_wiki_structure,
    post_process_wiki_content,
    render_file_links,
)

# ---------------------------------------------------------------------------
# 双路包装类分派总则
# ---------------------------------------------------------------------------
# wiki 生成(结构/页面)按 choice.generator 经 _wiki_pipeline() 选择;chat /
# codemap 同开关(choice 缺省走 env 默认)。
# 边界:语义属于单路生成协议的 helper(提示词组装/历史转写/检索上下文注入/
# agent 交付件路径/llm 一次问答生成链)收进对应 pipeline 类为 self._xxx();
# 共用构建件与低层支撑保持模块级(迁 utils/wiki/chat/codemap)。类方法体内一律
# 以模块全局名引用 _adapter/llm_stream/llm_complete/envs.*(调用时动态解析,
# 保证调用时动态解析:测试 monkeypatch 与 envs 切换均生效,不得实例捕获或模块级快照)。


class WikiPipeline:
    """双路共同协议;基类默认实现即 llm 路语义(无交付文件、无续跑水合、占位不落盘)。

    全部方法为散装参数(helper-funcs 思想,包内无 Request 概念):域聚类经 Repo
    对象携带(repo_url/repo_type/token),其余字段逐个 keyword 显式传入。
    """

    def needs_structure_regenerate(self, *, project_key: str, choice: dict | None) -> bool:
        """结构是否需要强制重生成(cc 路:structure 交付文件缺失;llm 路恒 False)。"""
        return False

    async def hydrate_pages(
        self, *, project_key: str, choice: dict | None, repo: Repo,
        structure: WikiStructureModel, default_branch: str,
    ) -> dict[str, WikiPage]:
        """从已落盘交付文件返回页快照(cc 路;llm 路无交付文件 → 空 dict)。

        以返回值交付,不触碰任何任务运行时字段(进度/去重/落盘均由 app 侧 runtime 负责)。
        """
        return {}

    def write_error_page(
        self, *, project_key: str, choice: dict | None, page: WikiPage, content: str,
    ) -> None:
        """重试耗尽后的占位页持久化(cc 路写交付文件供续跑跳过;llm 路 no-op)。"""

    async def determine_structure(
        self, *, choice: dict | None, repo: Repo, owner: str, repo_name: str,
        file_tree: list[str], readme: str, comprehensive: bool, language: str, run_id: str,
    ) -> WikiStructureModel:
        raise NotImplementedError

    async def generate_page(
        self, *, choice: dict | None, repo: Repo, page: WikiPage,
        language: str, default_branch: str, run_id: str,
    ) -> str:
        """单页生成,返回**终态格式化**内容(围栏/引用后处理已收口)。"""
        raise NotImplementedError

    async def chat_stream(
        self, *, choice: dict | None, repo: Repo, messages: list[dict],
        language: str = "en", research_iteration: int = 1,
    ):
        raise NotImplementedError

    async def generate_codemap(
        self, *, choice: dict | None, repo: Repo, question: str, language: str = "en",
    ):
        raise NotImplementedError


class AgentWikiPipeline(WikiPipeline):
    """cc(agent)路对外 API 包装:Claude Code agent 自读仓库代码,交付件 Write 落盘(文件为权威)。"""

    # deep_research 折叠版(cc/agent 路专用):一次回答完成整轮研究(agent 内部
    # 多轮工具调用);LLM 路仍走 LlmWikiPipeline 的 FIRST/INTERMEDIATE/FINAL 三模板(原版 5 轮协议)。
    # 标题字符串必须逐字匹配前端 Ask.tsx 的提取/完成判定正则(Research Plan /
    # Research Update {n} / Final Conclusion);禁 "## Next Steps"(它会截断 plan 提取
    # 并影响完成判定)与 "## Conclusion"/"## Summary"(完成判定的次选触发词)。
    _DEEP_RESEARCH_ONE_SHOT_PROMPT = """<role>
You are an expert code analyst examining the {repo_type} repository: {repo_url} ({repo_name}).
You are conducting a COMPLETE single-run Deep Research of the latest user query, aimed at a definitive answer rather than an intermediate round.
IMPORTANT:You MUST respond in {language_name} language.
</role>

<guidelines>
- This is the ONLY response of the research process: finish the entire investigation in this one answer.
- USE YOUR TOOLS (Read / Grep / Glob / graphify_query) to inspect the repository code for evidence before writing; repeated tool rounds are expected.
- Your answer MUST contain EXACTLY these sections, in this order:
  1. Begin with "## Research Plan" - the approach and initial findings for the investigation
  2. Then one or more progress sections "## Research Update 1", "## Research Update 2", ... with deeper findings from your tool exploration
  3. End with "## Final Conclusion" - a complete, definitive answer to the original question, citing specific files and line numbers
- NEVER stop after the Plan section: keep investigating until you can write a Final Conclusion.
- NEVER write "## Next Steps"; NEVER respond with "Continue the research".
- Do NOT use "## Conclusion" or "## Summary" as section headings (only "## Final Conclusion").
- Focus EXCLUSIVELY on the user's query; cite specific files and code sections when relevant.
</guidelines>"""

    def _proj_key(self, project_key: str, choice: dict | None) -> str:
        """项目键 {repo_key}_{digest}:digest = choice 判等摘要
        (同一仓库/语言下不同 choice 的交付文件并存,与成品缓存同规则)。"""
        return _sanitize_path_seg(f"{project_key}_{_generator_digest(choice)}")

    def _agent_cache_dir(self, project_key: str, choice: dict | None) -> Path:
        return Path(_wiki_cache_dir()) / _AGENT_CACHE_DIRNAME / self._proj_key(project_key, choice)

    def _agent_cache_structure_path(self, project_key: str, choice: dict | None) -> Path:
        """cc 结构交付文件:{proj}-structure.md。"""
        proj = self._proj_key(project_key, choice)
        return self._agent_cache_dir(project_key, choice) / f"{proj}-structure.md"

    def _agent_cache_page_path(self, project_key: str, choice: dict | None, page_id: str) -> Path:
        """cc 页面交付文件:{proj}-<id>.md(id 形如 page-N 时直接采用,否则 {proj}-page_<id>;id 经安全化)。"""
        proj = self._proj_key(project_key, choice)
        seg = _sanitize_path_seg(page_id)
        name = f"{proj}-{seg}" if seg.startswith("page-") else f"{proj}-page_{seg}"
        return self._agent_cache_dir(project_key, choice) / f"{name}.md"

    def _render_natural_history(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """agent 路对话历史:自然转写(无 <turn>/<conversation_history> 伪标签),含裁剪说明事件。"""
        history_parts: list[str] = []
        context: list[dict] = []
        if len(messages) > 1:
            last = messages[-1]
            if _estimate_tokens(last.get("content", "")) > envs.CHAT_TOKEN_LIMIT_ESTIMATE:
                _log(f"输入过大(估算 {_estimate_tokens(last.get('content', ''))} tokens),省略对话历史")
                context.append({"type": "context/modify",
                                "data": {"target": "chat-history", "kind": "trim",
                                         "cause": "token-limit", "detail": "省略对话历史",
                                         "removed": {"n_turns": len(messages) - 1,
                                                     "est_tokens": _estimate_tokens(last.get("content", ""))}}})
            else:
                for i in range(0, len(messages) - 1, 2):
                    user, assistant = messages[i], messages[i + 1]
                    if user.get("role") == "user" and assistant.get("role") == "assistant":
                        history_parts.append(
                            f"User: {user.get('content', '')}\nAssistant: {assistant.get('content', '')}"
                        )
        if history_parts:
            return "Previous conversation:\n" + "\n\n".join(history_parts) + "\n\n", context
        return "", context

    def _codemap_note(self) -> str:
        """codemap 指引(仅 cc/agent 路用):先查图谱再构造,引用行号取自 Source 标记。"""
        return (
            "<note>Before answering, use the graphify_query tool to inspect the repository "
            "code graph (its result carries `Source: <file path> L<line>` markers). "
            "When filling citation.file_path / start_line / end_line, use those paths and line "
            "numbers, and make the 'snippet' a verbatim substring of the code shown in the result.</note>\n\n"
        )

    # ------------------------------------------------------------------
    # agent 交付通道(适配器构造统一经模块级 _adapter;直呼 stream/result)
    # ------------------------------------------------------------------

    async def _deliver(
        self, adapter: Any, prompt: str, out_path: Path,
        label: str | None = None, *, run_id: str | None = None,
    ) -> str:
        """agent 交付件统一落盘口:提示词只给路径,agent 用自身工具读码并把成品写入
        out_path;产生以文件为准(流式文本仅作监控/错误检测),未产出文件即任务失败。

        adapter 为构造期注入 config 的实例(config 由模块级 _adapter 装配);
        label 作为监控会话名(wiki:structure / wiki:page:<id>),run_id 关联任务级会话组。
        """
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)  # add_dirs 指向目录须先存在(agent Write 可直接落)
        try:
            async for _ in adapter.stream(prompt, session_name=label, run_id=run_id):
                pass
        except RequestFailedError as e:
            raise _failure(e) from e
        text = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
        if not text.strip():
            raise RuntimeError(f"agent 未产出交付文件: {out_path}")
        return text

    def needs_structure_regenerate(self, *, project_key: str, choice: dict | None) -> bool:
        """structure 交付文件被删即强制重生成(续跑失效)。"""
        return not self._agent_cache_structure_path(project_key, choice).exists()

    async def determine_structure(
        self, *, choice: dict | None, repo: Repo, owner: str, repo_name: str,
        file_tree: list[str], readme: str, comprehensive: bool, language: str, run_id: str,
    ) -> WikiStructureModel:
        """cc(agent)结构:交付文件已存在即跳过 agent(续跑);否则 agent 落盘 structure.md 后读回解析。"""
        struct_path = self._agent_cache_structure_path(run_id, choice)
        if struct_path.exists():
            content = await asyncio.to_thread(struct_path.read_text, encoding="utf-8")
        else:
            adapter = _adapter(choice, repo=repo,
                               agent_output_dir=str(struct_path.parent),
                               agent_write_mode=True)
            readme_path = _find_readme_path(file_tree)
            prompt = self._build_structure_prompt(
                owner, repo_name, "\n".join(file_tree), readme_path,
                os.path.abspath(repo.save_path), comprehensive, language,
                str(struct_path), generator=adapter.generator,
            )
            content = await self._deliver(adapter, prompt, struct_path,
                                          label="wiki:structure", run_id=run_id)
        return parse_wiki_structure(content, comprehensive=comprehensive)

    @staticmethod
    def _build_structure_prompt(
        owner: str, repo_name: str, file_tree: str, readme_path: str | None,
        repo_root: str, comprehensive: bool, language: str, out_path: str,
        generator: str = "cc",
    ) -> str:
        """cc(agent)结构提示词(现代 agent 风格):输入只给路径(文件树+README 路径),不内联内容;
        成品 XML 由 agent 用 Write 工具直接落盘 out_path,而不是作为 result 文本返回。"""
        structure_format = _COMPREHENSIVE_STRUCTURE if comprehensive else _CONCISE_STRUCTURE
        page_count = "8-12" if comprehensive else "4-6"
        kind = "comprehensive" if comprehensive else "concise"
        readme_line = (
            f"2. The README file of the project is at: {repo_root}/{readme_path}\n"
            "   Read it yourself with the Read tool.\n"
            if readme_path
            else "2. No README file was found in this repository; skip it.\n"
        )
        return f"""IMPORTANT: you are working INSIDE the repository (cwd = repository root at {repo_root}).
The file contents are NOT inlined in this prompt — read source files yourself with the
Read/Grep/Glob tools. All paths below are relative to the repository root.

Analyze this repository {owner}/{repo_name} and create a wiki structure for it.

1. The complete relative file tree of the project:
<file_tree>
{file_tree}
</file_tree>

{readme_line}
I want to create a wiki for this repository. Determine the most logical structure for a wiki based on the repository's content.

IMPORTANT: The wiki content will be generated in {_language_name(language)} language.

When designing the wiki structure, include pages that would benefit from visual diagrams, such as:
- Architecture overviews
- Data flow descriptions
- Component relationships
- Process workflows
- State machines
- Class hierarchies
{structure_format}
IMPORTANT FORMATTING INSTRUCTIONS:
- Return ONLY the valid XML structure specified above
- DO NOT wrap the XML in markdown code blocks (no ``` or ```xml)
- DO NOT include any explanation text before or after the XML
- Ensure the XML is properly formatted and valid
- Start directly with <wiki_structure> and end with </wiki_structure>

DELIVERABLE: Write the complete XML to the file {out_path} using the Write tool (create the file;
do not use Edit). Do NOT return the XML in your message text — the written file is the only deliverable.

IMPORTANT:
1. Create {page_count} pages that would make a {kind} wiki for this repository
2. Each page should focus on a specific aspect of the codebase (e.g., architecture, key features, setup)
3. The relevant_files should be actual files from the repository that would be used to generate that page
4. Do not inline file contents into this prompt — use your tools to read the files.
{_agent_note(generator)}"""

    @staticmethod
    def _build_page_prompt(title: str, file_paths: list[str], out_path: str, language: str) -> str:
        """cc(agent)页面提示词(现代 agent 风格):只给相关文件相对路径,内容由 agent 自读;成品经 Write 落盘 out_path。"""
        paths = "\n".join(f"- [{p}]({p})" for p in file_paths)
        return (
            "IMPORTANT: you are working INSIDE the repository (cwd = repository root). "
            "The file contents are NOT inlined in this prompt — read the relevant source files "
            "yourself with the Read/Grep/Glob tools. All paths below are relative to the repository root.\n\n"
            + _build_page_prompt(title, paths, language)
            + f"\n\nDELIVERABLE: Write the complete generated Markdown page to `{out_path}` using the "
              "Write tool (create the file; do not use Edit). Do NOT return the page in your message "
              "text — the written file is the only deliverable."
        )

    async def generate_page(
        self, *, choice: dict | None, repo: Repo, page: WikiPage,
        language: str, default_branch: str, run_id: str,
    ) -> str:
        """cc 路单页:交付文件已存在即读回(续跑,文件为权威);否则 agent 落盘 page_<id>.md;
        读回内容经终态格式化后返回(与续跑水合同式)。"""
        out_path = self._agent_cache_page_path(run_id, choice, page.id)
        if out_path.exists():
            content = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
        else:
            adapter = _adapter(choice, repo=repo,
                               agent_output_dir=str(out_path.parent),
                               agent_write_mode=True)
            prompt = _agent_note(adapter.generator) + self._build_page_prompt(
                page.title, list(page.filePaths), str(out_path), language
            )
            content = await self._deliver(adapter, prompt, out_path,
                                          label=f"wiki:page:{page.id}", run_id=run_id)
        ctx = RepoUrlContext(type=repo.repo_type, repo_url=repo.repo_url, default_branch=default_branch)
        return _finalize_page_content(content, page, ctx)

    async def hydrate_pages(
        self, *, project_key: str, choice: dict | None, repo: Repo,
        structure: WikiStructureModel, default_branch: str,
    ) -> dict[str, WikiPage]:
        """cc 路径:从已落盘的页交付文件水合(文件为权威,覆盖 state 旧文本);
        返回页快照 dict(不触碰任务运行时字段;无文件的页留给调用方生成)。"""
        ctx = RepoUrlContext(type=repo.repo_type, repo_url=repo.repo_url, default_branch=default_branch)
        generated: dict[str, WikiPage] = {}
        for page in structure.pages:
            out_path = self._agent_cache_page_path(project_key, choice, page.id)
            if not out_path.exists():
                continue
            content = await asyncio.to_thread(out_path.read_text, encoding="utf-8")
            generated[page.id] = dataclasses.replace(
                page, content=_finalize_page_content(content, page, ctx)
            )
        return generated

    def write_error_page(
        self, *, project_key: str, choice: dict | None, page: WikiPage, content: str,
    ) -> None:
        """重试耗尽(cc 路):占位文本也落盘,续跑跳过占位页;用户删除该文件即可重试。"""
        try:
            out_path = self._agent_cache_page_path(project_key, choice, page.id)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
        except OSError as e:  # noqa: BLE001 - 占位写入失败不阻断任务完成
            _log(f"写入占位页文件失败: {page.id} - {e}")

    async def chat_stream(
        self, *, choice: dict | None, repo: Repo, messages: list[dict],
        language: str = "en", research_iteration: int = 1,
    ):
        """一次 chat 问答的流式应答(纯文本 chunk 序列,前后端协议同原 research_chat);
        现代 agent 模式:一次提问,agent 内部多轮工具调用完成,不做协议级轮转。"""
        if not messages:
            raise ValueError("No messages provided")
        last = messages[-1]
        if last.get("role") != "user":
            raise ValueError("Last message must be from the user")

        # 注:未索引的前置校验属端点守卫,已上移到应用层,生成器内不再重复
        fmt = _prompt_fmt(repo, language=language)
        is_deep = last.get("mode") == "deep_research"
        if is_deep:
            # 折叠:一次回答完成整轮研究(agent 内部多轮工具调用);continuation 回退
            # 保留作偏差兜底(首轮缺 Final Conclusion 时前端续跑轮为同一问题重跑)
            _resolve_chat_continuation(last, messages)
            system = self._DEEP_RESEARCH_ONE_SHOT_PROMPT.format(**fmt)
        else:
            system = _SIMPLE_CHAT_SYSTEM_PROMPT.format(**fmt)

        # 对话历史自然转写(无 <turn> 伪标签);输入过大时省略历史。
        # 裁剪/注记作为监控上下文说明事件(context/modify)伴跑,不改变折叠结果
        adapter = _adapter(choice, system_prompt=system, repo=repo)
        history, context = self._render_natural_history(messages)
        context.append({"type": "context/modify",
                        "data": {"target": "user-message", "phase": "prompt-assembly",
                                 "provenance": "deepwiki:note", "text": _agent_note(adapter.generator)}})

        prompt = history + _agent_note(adapter.generator) + f"<query>\n{last.get('content', '')}\n</query>\n\nAssistant: "
        try:
            async for chunk in adapter.stream(
                prompt, session_name=f"chat:{repo.name}",
                run_id=f"chat:{repo.name}", context=context,
            ):
                yield chunk
        except Exception as e:  # 执行期失败降级为可读错误文本(同原 stream_and_fallback 语义)
            err = _failure(e)  # RequestFailedError 先转「agent 执行失败」再降级(同原包装时序)
            _log(f"chat agent 错误: {err}")
            yield f"\n\n(抱歉,本次请求处理失败: {err})"

    async def generate_codemap(
        self, *, choice: dict | None, repo: Repo, question: str, language: str = "en",
    ):
        """两阶段 codemap 生成(骨架 → 指南/图),NDJSON 事件流;阶段失败语义与原相同。"""
        yield _phase("analyzing", "start")
        if not _index_ready(repo):
            yield _phase("analyzing", "done", chunk_count=0)
            yield _event(type="error", stage="analyzing", message=f"仓库尚未索引,请先 /repo/prepare: {repo.name}")
            return
        yield _phase("analyzing", "done", chunk_count=0)

        fmt = _prompt_fmt(repo, language=language)

        async def _run_json(prompt: str, attempts: int = 3) -> dict:
            """整收 + 解析 JSON,失败重试(每轮新 agent);system 恒用骨架提示词。"""
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    adapter = _adapter(
                        choice, system_prompt=_CODEMAP_SKELETON_PROMPT.format(**fmt), repo=repo
                    )
                    raw = await adapter.result(
                        prompt, session_name="codemap:skeleton",
                        run_id=f"codemap:{repo.name}",
                        retry={"attempt": attempt, "prev_error": str(last_error)} if last_error else None,
                    )
                    return _extract_json(raw)
                except Exception as e:  # noqa: BLE001 - 重试预算兜底(RequestFailedError 先转文案)
                    last_error = _failure(e)
                    _log(f"codemap JSON 解析尝试 {attempt}/{attempts} 失败: {last_error}")
            raise ValueError(f"Model did not return valid JSON after {attempts} attempts: {last_error}")

        # 阶段 1:骨架
        yield _phase("initial_codemap", "start")
        skeleton_prompt = self._codemap_note() + f"<query>\n{question}\n</query>\n\nAssistant: "
        try:
            skeleton = codemap_of(await _run_json(skeleton_prompt))
        except Exception as e:  # noqa: BLE001
            _log(f"codemap 骨架失败: {e}")
            yield _event(type="error", stage="initial_codemap", message=str(e))
            return
        yield _phase("initial_codemap", "done", section_count=len(skeleton.sections))

        # 阶段 2:指南/图;i/骨架失败不致命 — 退化为骨架
        yield _phase("diagrams", "start")
        enrich_query = (
            f"{question}\n\n<SKELETON>\n{json.dumps(dataclasses.asdict(skeleton))}\n</SKELETON>"
        )
        enrich_prompt = self._codemap_note() + f"<query>\n{enrich_query}\n</query>\n\nAssistant: "
        final = skeleton
        try:
            adapter = _adapter(
                choice, system_prompt=_CODEMAP_ENRICH_PROMPT.format(**fmt), repo=repo
            )
            raw = await adapter.result(
                enrich_prompt, session_name="codemap:enrich",
                run_id=f"codemap:{repo.name}",
            )
            final = codemap_of(_extract_json(raw))
            yield _phase("diagrams", "done")
        except Exception as e:  # noqa: BLE001
            err = _failure(e)  # RequestFailedError 先转「agent 执行失败」再降级(同原包装时序)
            _log(f"codemap 指南/图失败,使用骨架: {err}")
            yield _phase("diagrams", "done", degraded=True)

        _ground_citations(final, repo.save_path)
        yield _event(type="codemap", data=dataclasses.asdict(final))
        yield _event(type="done")


class LlmWikiPipeline(WikiPipeline):
    """llm 路对外 API 包装:deepwiki-open 原式单次补全(内容内联进 prompt,无工具)。"""

    _DEEP_RESEARCH_FIRST_ITERATION_PROMPT = """<role>
You are an expert code analyst examining the {repo_type} repository: {repo_url} ({repo_name}).
You are conducting a multi-turn Deep Research process to thoroughly investigate the specific topic in the user's query.
Your goal is to provide detailed, focused information EXCLUSIVELY about this topic.
IMPORTANT:You MUST respond in {language_name} language.
</role>

<guidelines>
- This is the first iteration of a multi-turn research process focused EXCLUSIVELY on the user's query
- Start your response with "## Research Plan"
- Outline your approach to investigating this specific topic
- If the topic is about a specific file or feature (like "Dockerfile"), focus ONLY on that file or feature
- Clearly state the specific topic you're researching to maintain focus throughout all iterations
- Identify the key aspects you'll need to research
- Provide initial findings based on the information available
- End with "## Next Steps" indicating what you'll investigate in the next iteration
- Do NOT provide a final conclusion yet - this is just the beginning of the research
- Do NOT include general repository information unless directly relevant to the query
- Focus EXCLUSIVELY on the specific topic being researched - do not drift to related topics
- Your research MUST directly address the original question
- NEVER respond with just "Continue the research" as an answer - always provide substantive research findings
- Remember that this topic will be maintained across all research iterations
</guidelines>

<style>
- Be concise but thorough
- Use markdown formatting to improve readability
- Cite specific files and code sections when relevant
</style>"""

    _DEEP_RESEARCH_INTERMEDIATE_ITERATION_PROMPT = """<role>
You are an expert code analyst examining the {repo_type} repository: {repo_url} ({repo_name}).
You are currently in iteration {research_iteration} of a Deep Research process focused EXCLUSIVELY on the latest user query.
Your goal is to build upon previous research iterations and go deeper into this specific topic without deviating from it.
IMPORTANT:You MUST respond in {language_name} language.
</role>

<guidelines>
- CAREFULLY review the conversation history to understand what has been researched so far
- Your response MUST build on previous research iterations - do not repeat information already covered
- Identify gaps or areas that need further exploration related to this specific topic
- Focus on one specific aspect that needs deeper investigation in this iteration
- Start your response with "## Research Update {research_iteration}"
- Clearly explain what you're investigating in this iteration
- Provide new insights that weren't covered in previous iterations
- If this is iteration 3, prepare for a final conclusion in the next iteration
- Do NOT include general repository information unless directly relevant to the query
- Focus EXCLUSIVELY on the specific topic being researched - do not drift to related topics
- If the topic is about a specific file or feature (like "Dockerfile"), focus ONLY on that file or feature
- NEVER respond with just "Continue the research" as an answer - always provide substantive research findings
- Your research MUST directly address the original question
- Maintain continuity with previous research iterations - this is a continuous investigation
</guidelines>

<style>
- Be concise but thorough
- Focus on providing new information, not repeating what's already been covered
- Use markdown formatting to improve readability
- Cite specific files and code sections when relevant
</style>"""

    _DEEP_RESEARCH_FINAL_ITERATION_PROMPT = """<role>
You are an expert code analyst examining the {repo_type} repository: {repo_url} ({repo_name}).
You are in the final iteration of a Deep Research process focused EXCLUSIVELY on the latest user query.
Your goal is to synthesize all previous findings and provide a comprehensive conclusion that directly addresses this specific topic and ONLY this topic.
IMPORTANT:You MUST respond in {language_name} language.
</role>

<guidelines>
- This is the final iteration of the research process
- CAREFULLY review the entire conversation history to understand all previous findings
- Synthesize ALL findings from previous iterations into a comprehensive conclusion
- Start with "## Final Conclusion"
- Your conclusion MUST directly address the original question
- Stay STRICTLY focused on the specific topic - do not drift to related topics
- Include specific code references and implementation details related to the topic
- Highlight the most important discoveries and insights about this specific functionality
- Provide a complete and definitive answer to the original question
- Do NOT include general repository information unless directly relevant to the query
- Focus exclusively on the specific topic being researched
- NEVER respond with "Continue the research" as an answer - always provide a complete answer
- If the topic is about a specific file or feature (like "Dockerfile"), focus ONLY on that file or feature
- Ensure your conclusion builds on and references key findings from previous iterations
</guidelines>

<style>
- Be concise but thorough
- Use markdown formatting to improve readability
- Cite specific files and code sections when relevant
- Structure your response with clear headings
- End with actionable insights or recommendations when appropriate
</style>"""

    def _build_turn_history(self, messages: list[dict]) -> str:
        """LLM 路对话历史:恒拼 <turn> 成对序列(原版无裁剪;输入过大仅跳过检索上下文)。"""
        turns = ""
        for i in range(0, len(messages) - 1, 2):
            user, assistant = messages[i], messages[i + 1]
            if user.get("role") == "user" and assistant.get("role") == "assistant":
                turns += (
                    f"<turn>\n<user>{user.get('content', '')}</user>\n"
                    f"<assistant>{assistant.get('content', '')}</assistant>\n</turn>\n"
                )
        return f"<conversation_history>\n{turns}</conversation_history>\n\n" if turns else ""

    async def determine_structure(
        self, *, choice: dict | None, repo: Repo, owner: str, repo_name: str,
        file_tree: list[str], readme: str, comprehensive: bool, language: str, run_id: str,
    ) -> WikiStructureModel:
        """llm 路结构:原版经 research_chat(结构提示词为查询,SIMPLE 角色模板 +
        检索上下文注入 + prompt_builder 拼装);内容错误时解析失败 → 任务 FAILED。"""
        prompt = self._build_structure_prompt(
            owner, repo_name, "\n".join(file_tree), readme, comprehensive, language
        )
        system = _SIMPLE_CHAT_SYSTEM_PROMPT.format(**_prompt_fmt(repo, language=language))
        parts: list[str] = []
        async for chunk in _llm_research_chat(
            system, prompt, choice=choice, repo=repo,
            session_name="wiki:structure", run_id=run_id,
        ):
            parts.append(chunk)
        return parse_wiki_structure("".join(parts), comprehensive=comprehensive)

    @staticmethod
    def _build_structure_prompt(
        owner: str, repo_name: str, file_tree: str, readme: str,
        comprehensive: bool, language: str,
    ) -> str:
        """wiki 结构确定提示词(移植 determineWikiStructure)。"""
        structure_format = _COMPREHENSIVE_STRUCTURE if comprehensive else _CONCISE_STRUCTURE
        page_count = "8-12" if comprehensive else "4-6"
        kind = "comprehensive" if comprehensive else "concise"
        return f"""Analyze this GitHub repository {owner}/{repo_name} and create a wiki structure for it.

1. The complete file tree of the project:
<file_tree>
{file_tree}
</file_tree>

2. The README file of the project:
<readme>
{readme}
</readme>

I want to create a wiki for this repository. Determine the most logical structure for a wiki based on the repository's content.

IMPORTANT: The wiki content will be generated in {_language_name(language)} language.

When designing the wiki structure, include pages that would benefit from visual diagrams, such as:
- Architecture overviews
- Data flow descriptions
- Component relationships
- Process workflows
- State machines
- Class hierarchies
{structure_format}
IMPORTANT FORMATTING INSTRUCTIONS:
- Return ONLY the valid XML structure specified above
- DO NOT wrap the XML in markdown code blocks (no ``` or ```xml)
- DO NOT include any explanation text before or after the XML
- Ensure the XML is properly formatted and valid
- Start directly with <wiki_structure> and end with </wiki_structure>

IMPORTANT:
1. Create {page_count} pages that would make a {kind} wiki for this repository
2. Each page should focus on a specific aspect of the codebase (e.g., architecture, key features, setup)
3. The relevant_files should be actual files from the repository used to generate that page
4. Return ONLY valid XML with the structure specified above, with no markdown code block delimiters"""

    async def generate_page(
        self, *, choice: dict | None, repo: Repo, page: WikiPage,
        language: str, default_branch: str, run_id: str,
    ) -> str:
        """llm 路单页:原版同式——页面提示词(仅文件链接,不内联内容)
        作为查询经 research_chat 等价流(检索上下文注入;流错误为内容而非抛出,
        重试只覆盖校验/检索前置异常——与原版一致)。返回前经终态格式化(同 cc 路)。"""
        ctx = RepoUrlContext(type=repo.repo_type, repo_url=repo.repo_url, default_branch=default_branch)
        file_links = render_file_links(list(page.filePaths), ctx)
        prompt = _build_page_prompt(page.title, file_links, language)
        system = _SIMPLE_CHAT_SYSTEM_PROMPT.format(**_prompt_fmt(repo, language=language))
        parts: list[str] = []
        async for chunk in _llm_research_chat(
            system, prompt, choice=choice, repo=repo,
            session_name=f"wiki:page:{page.id}", run_id=run_id,
        ):
            parts.append(chunk)
        return _finalize_page_content("".join(parts), page, ctx)

    async def chat_stream(
        self, *, choice: dict | None, repo: Repo, messages: list[dict],
        language: str = "en", research_iteration: int = 1,
    ):
        """llm 路 chat:原版 research_chat 等价(模式/迭代选模板、原版历史拼接、
        输入过大跳过检索、prompt_builder 拼装、token 超限简化重试)。"""
        if not messages:
            raise ValueError("No messages provided")
        last = messages[-1]
        if last.get("role") != "user":
            raise ValueError("Last message must be from the user")

        fmt = _prompt_fmt(repo, language=language)
        is_deep = last.get("mode") == "deep_research"
        if is_deep:
            # 原版复刻:5 轮迭代由前端驱动,后端只按迭代号选模板 + 拼历史
            _resolve_chat_continuation(last, messages)
            if research_iteration == 1:
                system = self._DEEP_RESEARCH_FIRST_ITERATION_PROMPT.format(**fmt)
            elif research_iteration >= 5:
                system = self._DEEP_RESEARCH_FINAL_ITERATION_PROMPT.format(**fmt)
            else:
                system = self._DEEP_RESEARCH_INTERMEDIATE_ITERATION_PROMPT.format(
                    **fmt, research_iteration=research_iteration
                )
        else:
            system = _SIMPLE_CHAT_SYSTEM_PROMPT.format(**fmt)

        history = self._build_turn_history(messages)
        async for chunk in _llm_research_chat(
            system, last.get("content", ""), choice=choice, repo=repo,
            session_name=f"chat:{repo.name}", run_id=f"chat:{repo.name}",
            conversation_history=history,
        ):
            yield chunk

    async def generate_codemap(
        self, *, choice: dict | None, repo: Repo, question: str, language: str = "en",
    ):
        """llm 路 codemap(原版等价):analyzing 阶段完成检索(chunk_count=窗口数);
        双提示词经 prompt_builder(与 chat 同构);JSON 解析失败重试——骨架 3 次、
        富化 2 次(传输错误直接上抛);富化失败 degraded;引用接地两路共用。"""
        # ---- 阶段 1a:analyzing(原版 RAG 检索;此处 = 图谱子图→真实代码窗) ----
        yield _phase("analyzing", "start")
        if not _index_ready(repo):
            yield _phase("analyzing", "done", chunk_count=0)
            yield _event(type="error", stage="analyzing", message=f"仓库尚未索引,请先 /repo/prepare: {repo.name}")
            return
        ctx = await _graphify_context(repo, question)
        yield _phase("analyzing", "done", chunk_count=len(ctx["blocks"]))

        fmt = _prompt_fmt(repo, language=language)
        context: list[dict] = []
        for b in ctx["blocks"]:
            context.append({"type": "context/modify",
                            "data": {"target": "codemap", "phase": "prompt-assembly",
                                     "provenance": f"deepwiki:graph:{b['path']}", "text": b["text"]}})
        if ctx["degraded"]:
            context.append({"type": "context/modify",
                            "data": {"target": "codemap", "kind": "degrade",
                                     "cause": "token-limit", "detail": "检索上下文超限"}})
        if ctx["error"]:
            context.append({"type": "context/modify",
                            "data": {"target": "codemap", "kind": "degrade",
                                     "cause": "graph-error",
                                     "detail": f"代码图谱不可用: {ctx['error']}"}})
        context_text = _format_subgraph_context(ctx["blocks"])

        async def _run_llm_json(prompt: str, attempts: int, session_name: str) -> dict:
            """整收 + 解析 JSON;仅解析失败重试(原版 _generate_json 语义:
            传输异常直接上抛,由阶段 try 处理)。"""
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                raw = await utils.llm_complete(
                    prompt, choice=choice,
                    session_name=session_name, run_id=f"codemap:{repo.name}",
                    context=context,
                )
                try:
                    return _extract_json(raw)
                except Exception as e:  # noqa: BLE001
                    last_error = e
                    _log(f"codemap JSON 解析尝试 {attempt}/{attempts} 失败: {e}")
            raise ValueError(f"Model did not return valid JSON after {attempts} attempts: {last_error}")

        # ---- 阶段 1b:骨架 ----------------------------------------------------
        yield _phase("initial_codemap", "start")
        skeleton_prompt = _build_service_prompt(
            _CODEMAP_SKELETON_PROMPT.format(**fmt), question, context=context_text
        )
        try:
            skeleton = codemap_of(await _run_llm_json(skeleton_prompt, 3, "codemap:skeleton"))
        except Exception as e:  # noqa: BLE001
            _log(f"codemap 骨架失败: {e}")
            yield _event(type="error", stage="initial_codemap", message=str(e))
            return
        yield _phase("initial_codemap", "done", section_count=len(skeleton.sections))

        # ---- 阶段 2:指南/图;失败不致命 — 退化为骨架 -------------------------
        yield _phase("diagrams", "start")
        enrich_query = (
            f"{question}\n\n<SKELETON>\n{json.dumps(dataclasses.asdict(skeleton))}\n</SKELETON>"
        )
        enrich_prompt = _build_service_prompt(
            _CODEMAP_ENRICH_PROMPT.format(**fmt), enrich_query, context=context_text
        )
        final = skeleton
        try:
            final = codemap_of(
                await _run_llm_json(enrich_prompt, 2, "codemap:enrich")
            )
            yield _phase("diagrams", "done")
        except Exception as e:  # noqa: BLE001
            _log(f"codemap 指南/图失败,使用骨架: {e}")
            yield _phase("diagrams", "done", degraded=True)

        _ground_citations(final, repo.save_path)
        yield _event(type="codemap", data=dataclasses.asdict(final))
        yield _event(type="done")


def _wiki_pipeline(choice: dict | None = None) -> WikiPipeline:
    """按解析后的 choice.generator 选路;调用时解析(测试 monkeypatch envs 生效)。

    agent 类后段(cc/dsh/codex)共用 AgentWikiPipeline —— 适配器构造统一经
    模块级 _adapter,管线逻辑(结构/页面/缓存)后端无关;llm 走 LlmWikiPipeline
    (原式单次补全)。chat/codemap 服务入口与 wiki 主流程同开关共用本函数。
    """
    gen = _resolve_generator(choice)[0]
    return AgentWikiPipeline() if gen in ("cc", "dsh", "codex") else LlmWikiPipeline()


# ---------------------------------------------------------------------------
# chat / codemap 服务入口(双路包装;端点层从 app.py 直呼)
# ---------------------------------------------------------------------------


async def chat_stream(
    *, choice: dict | None, repo: Repo, messages: list[dict],
    language: str = "en", research_iteration: int = 1,
):
    """一次 chat 问答的流式应答(纯文本 chunk 序列,前后端协议同原 research_chat);按 choice.generator 分派双路。"""
    async for chunk in _wiki_pipeline(choice).chat_stream(
        choice=choice, repo=repo, messages=messages,
        language=language, research_iteration=research_iteration,
    ):
        yield chunk


async def generate_codemap(
    *, choice: dict | None, repo: Repo, question: str, language: str = "en",
):
    """两阶段 codemap 生成(骨架 → 指南/图),NDJSON 事件流;阶段失败语义与原相同;按 choice.generator 分派双路。"""
    async for ev in _wiki_pipeline(choice).generate_codemap(
        choice=choice, repo=repo, question=question, language=language,
    ):
        yield ev
