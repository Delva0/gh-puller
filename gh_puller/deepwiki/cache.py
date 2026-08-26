"""wiki 成品缓存 / 续跑状态 / 导出 + 判等摘要族(生成产物的持久化侧)。

职责边界(deepwiki 包,仅缓存与状态文件):
- 成品缓存(deepwiki_cache_*)、续跑状态(deepwiki_taskstate_*)、processed 列表、
  wiki 导出;判等摘要族 `_target_digest`/_request_digest/_cache_identity/
  _cache_target_matches(任务 id、缓存路径共用同一判等身份)。
- 与主干的关系:models(deepwiki.WikiCacheData/deepwiki.WikiTaskState 等)、路径常量
  (_WIKI_CACHE_DIR/_WIKI_PREFIX/_WIKI_STATE_PREFIX/_state_write_lock)、target 解析
  (_resolve_target/_target_identity)与落盘剥离(_strip_creds)留在主干 —— 本模块经
  `deepwiki.` 前缀在**调用时**取(测试 monkeypatch 与导入环安全);_log 经 utils 直连
  (与主干同一函数对象)。
- 循环引用契约:同 pipeline.py —— 顶层仅 `from gh_puller import deepwiki` 绑定模块对象,
  属性全在函数体内取;stdlib/`..utils._log` 可顶层直连。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from gh_puller import deepwiki
from ..utils import _log

# ---------------------------------------------------------------------------
# wiki 缓存与导出(移植 api/services/wiki/io.py;anyio IO → asyncio.to_thread + json)
# ---------------------------------------------------------------------------


def _target_digest(target: Mapping | None = None, get_env=None) -> str:
    """target 判等身份(不含凭证)的稳定摘要(8 hex)。

    身份 = generator + 配置摘要:file 类 = config_path(路径是身份;内容随文件,
    不读取);object 类 = provider|model。任务 id / 续跑状态 / 成品缓存路径共用:
    同一仓库与语言下不同 target 的结果可以并发并存且互不串用。
    """
    generator_id, resolved = deepwiki._resolve_target(target, get_env)
    return _target_digest_of(generator_id, resolved)


def _target_digest_of(generator_id: str, resolved: Mapping) -> str:
    return hashlib.sha1(
        f"{generator_id}|{deepwiki._target_identity(generator_id, resolved)}".encode()
    ).hexdigest()[:8]


def _request_digest(target: Any = None) -> str:
    """请求 target → 摘要(空 target 走 env 缺省解析,与运行期一致)。"""
    return _target_digest(target)


def _cache_identity(cache: "deepwiki.WikiCacheData") -> tuple[str, str]:
    """缓存内记录的公开身份(file 类:generator+config_path;object 类:generator+provider|model)。"""
    if cache.generator and cache.config_path:
        return (cache.generator or "", cache.config_path or "")
    return (cache.generator or "", f"{cache.provider or ''}|{cache.model or ''}")


def _cache_target_matches(cache: "deepwiki.WikiCacheData", t: Any) -> bool:
    """成品缓存与公开 target 是否同轨(摘要隔离后的二次校验,防手改文件名)。"""
    generator_id, resolved = deepwiki._resolve_target(t)
    return _cache_identity(cache) == (generator_id, deepwiki._target_identity(generator_id, resolved))


def _wiki_cache_path(
    owner: str, repo: str, repo_type: str, language: str, digest: str = ""
) -> str:
    suffix = f"_{digest}" if digest else ""
    filename = f"{deepwiki._WIKI_PREFIX}{repo_type}_{owner}_{repo}_{language}{suffix}.json"
    return os.path.join(deepwiki._WIKI_CACHE_DIR, filename)


def wiki_cache_exists(owner: str, repo: str, repo_type: str, language: str, digest: str = "") -> bool:
    return os.path.exists(_wiki_cache_path(owner, repo, repo_type, language, digest))


async def read_wiki_cache(
    owner: str, repo: str, repo_type: str, language: str, digest: str = ""
) -> deepwiki.WikiCacheData | None:
    if not wiki_cache_exists(owner, repo, repo_type, language, digest):
        return None
    path = _wiki_cache_path(owner, repo, repo_type, language, digest)
    try:
        text = await asyncio.to_thread(lambda: Path(path).read_text(encoding="utf-8"))
        return deepwiki.WikiCacheData.model_validate_json(text)
    except Exception:
        _log(f"读取 wiki 缓存失败: {path}")
        return None


async def save_wiki_cache(
    owner: str, repo: str, repo_type: str, language: str,
    wiki_cache: deepwiki.WikiCacheData, digest: str = "",
) -> bool:
    path = _wiki_cache_path(owner, repo, repo_type, language, digest)
    try:
        await asyncio.to_thread(
            lambda: Path(path).write_text(wiki_cache.model_dump_json(), encoding="utf-8")
        )
        return True
    except OSError:
        _log(f"写 wiki 缓存失败: {path}")
        return False


async def delete_wiki_cache(
    owner: str, repo: str, repo_type: str, language: str, digest: str = ""
) -> bool:
    path = _wiki_cache_path(owner, repo, repo_type, language, digest)
    deleted = False
    if os.path.exists(path):
        os.remove(path)
        deleted = True
    # 删除缓存同时清续跑状态(本 target + 旧格式无摘要文件),避免裸 state 无清理途径
    for state_path in (_wiki_state_path(owner, repo, repo_type, language, digest),
                       _wiki_state_path(owner, repo, repo_type, language)):
        if os.path.exists(state_path):
            os.remove(state_path)
            deleted = True
    return deleted


def _wiki_state_path(
    owner: str, repo: str, repo_type: str, language: str, digest: str = ""
) -> str:
    suffix = f"_{digest}" if digest else ""
    filename = f"{deepwiki._WIKI_STATE_PREFIX}{repo_type}_{owner}_{repo}_{language}{suffix}.json"
    return os.path.join(deepwiki._WIKI_CACHE_DIR, filename)


async def write_wiki_task_state(state: deepwiki.WikiTaskState) -> bool:
    """原子写生成状态(先写 .tmp 再 os.replace,崩溃不产生半截文件)。

    路径带公开 target 摘要(与成品缓存同规则):不同 target 的续跑状态并存。
    状态内 request.target 恒为 _strip_creds 落盘形态(凭证已剥离)。
    """
    path = _wiki_state_path(
        state.request.owner, state.request.repo, state.request.type, state.request.language,
        digest=_target_digest(state.request.target),
    )
    tmp = f"{path}.tmp"
    try:
        await asyncio.to_thread(
            lambda: Path(tmp).write_text(state.model_dump_json(), encoding="utf-8")
        )
        os.replace(tmp, path)
        return True
    except OSError as e:
        _log(f"写生成状态失败: {path} - {e}")
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return False


async def read_wiki_task_state(
    owner: str, repo: str, repo_type: str, language: str, digest: str = ""
) -> deepwiki.WikiTaskState | None:
    """读取生成状态;无文件或解析失败 → None(自动降级为全新生成)。"""
    path = _wiki_state_path(owner, repo, repo_type, language, digest)
    if not os.path.exists(path):
        return None
    try:
        text = await asyncio.to_thread(lambda: Path(path).read_text(encoding="utf-8"))
        return deepwiki.WikiTaskState.model_validate_json(text)
    except Exception as _e_dbg:
        _log(f"读取生成状态失败: {path} :: {type(_e_dbg).__name__}: {_e_dbg}")
        return None


async def delete_wiki_task_state(
    owner: str, repo: str, repo_type: str, language: str, digest: str = ""
) -> bool:
    path = _wiki_state_path(owner, repo, repo_type, language, digest)
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


async def _persist_state(task: "deepwiki.WikiTask") -> None:
    """把任务当前进度落盘(结构/已完成页/状态);并发写由模块锁串行。

    落盘即剥离凭证:request.target 存 deepwiki._strip_creds(只含判等身份字段),
    续跑合并用户重新提交的凭证(见 load_resume)。
    """
    request = task.request.model_copy(
        update={"target": deepwiki._strip_creds(task.request.target)}
    )
    state = deepwiki.WikiTaskState(
        request=request,
        status=task.status,
        wiki_structure=task.wiki_structure,
        generated_pages=dict(task.generated_pages),  # 浅拷贝快照:WikiPage 只整替换、不原地改
        default_branch=task.default_branch,
        submitted_at=task.submitted_at,
        error=task.error,
    )
    async with deepwiki._state_write_lock:
        await write_wiki_task_state(state)


async def list_wiki_cache() -> list[deepwiki.WikiTaskSummary]:
    """扫描缓存目录,按文件名拆解为 (type, owner, repo, language) 摘要条目。"""
    if not os.path.exists(deepwiki._WIKI_CACHE_DIR):
        return []
    entries: list[deepwiki.WikiTaskSummary] = []
    for filename in await asyncio.to_thread(os.listdir, deepwiki._WIKI_CACHE_DIR):
        if not (filename.startswith(deepwiki._WIKI_PREFIX) and filename.endswith(".json")):
            continue
        file_path = os.path.join(deepwiki._WIKI_CACHE_DIR, filename)
        try:
            stats = await asyncio.to_thread(os.stat, file_path)
            parts = os.path.splitext(filename)[0].removeprefix(deepwiki._WIKI_PREFIX).split("_")
            # 列尾 _<digest8> 为公开 target 摘要(同一仓库多 target 并存);缺省无摘要(旧缓存兼容)
            has_digest = len(parts) > 1 and len(parts[-1]) == 8 and re.fullmatch(r"[0-9a-f]+", parts[-1])
            language_idx = -2 if has_digest else -1
            entries.append(
                deepwiki.WikiTaskSummary(
                    id=filename,
                    owner=parts[1],
                    repo="_".join(parts[2:language_idx]),
                    repo_type=parts[0],
                    language=parts[language_idx],
                    submitted_at=int(stats.st_mtime * 1000),
                    status=deepwiki.TaskStatus.COMPLETED,
                    digest=parts[-1] if has_digest else "",
                )
            )
        except Exception:
            _log(f"解析缓存文件失败: {file_path}")
    return entries


async def list_processed_projects() -> list[deepwiki.ProcessedProjectEntry]:
    project_entries = [
        deepwiki.ProcessedProjectEntry(
            id=wiki.id,
            owner=wiki.owner,
            repo=wiki.repo,
            name=wiki.name,
            repo_type=wiki.repo_type,
            submittedAt=wiki.submitted_at,
            language=wiki.language,
            digest=wiki.digest,
        )
        for wiki in await list_wiki_cache()
    ]
    project_entries.sort(key=lambda p: p.submittedAt, reverse=True)
    return project_entries


def export_wiki(
    repo_url: str,
    pages: list[WikiPage],
    format: Literal["json", "markdown"],
    timestamp: datetime | None = None,
) -> str:
    """导出 wiki 为 markdown/json 字符串(与 io.py 同式)。"""
    dt = timestamp or datetime.now()
    if format == "json":
        export_data = {
            "metadata": {
                "repository": repo_url,
                "generated_at": dt.isoformat(),
                "page_count": len(pages),
            },
            "pages": [page.model_dump() for page in pages],
        }
        return json.dumps(export_data, indent=2)
    if format == "markdown":
        markdown = f"# Wiki Documentation for {repo_url}\n\n"
        markdown += f"Generated on: {dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        markdown += "## Table of Contents\n\n"
        for page in pages:
            markdown += f"- [{page.title}](#{page.id})\n"
        markdown += "\n"
        for page in pages:
            markdown += f"<a id='{page.id}'></a>\n\n"
            markdown += f"## {page.title}\n\n"
            if page.relatedPages:
                related_titles = []
                for related_id in page.relatedPages:
                    related_page = next((p for p in pages if p.id == related_id), None)
                    if related_page:
                        related_titles.append(f"[{related_page.title}](#{related_id})")
                if related_titles:
                    markdown += "### Related Pages\n\n"
                    markdown += "Related topics: " + ", ".join(related_titles) + "\n\n"
            markdown += f"{page.content}\n\n"
            markdown += "---\n\n"
        return markdown
    raise NotImplementedError(
        f"Exporting wiki to format {format} is not supported. Must be one of 'markdown' or 'json'."
    )



