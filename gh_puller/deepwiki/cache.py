"""产物持久化侧:graphify 图路径 + 成品缓存 / 续跑状态 / 导出 + 判等摘要族。

职责边界(deepwiki 包;DEEPWIKI_ROOT 下全部产物的 dir/path 布局与文件 IO 都在此):
- graphify 图产物路径(`_graph_dir/_graph_path/_index_ready`;ready = graph.json 存在);
- 成品缓存(deepwiki_cache_*)、续跑状态(deepwiki_resume_*)、processed 列表、
  wiki 导出;`save_generated_wiki`(完整生成结果落盘服务:判等身份 + 组装 + 写盘);
  判等摘要族 `_generator_digest`/_generator_digest_of/_cache_identity/
  _cache_generator_matches(任务 id、缓存路径共用同一判等身份)。
- 数据形态为纯 dict(成品缓存/续跑状态 passthrough;注解一律 dict,不用
  抽象 Mapping);嵌套结构用引擎 dataclass(.wiki 的 WikiStructureModel/WikiPage,
  TYPE_CHECKING 注解免运行时环);
  generator 选型规则(.utils 判等/凭证簇)、日志(..utils._log)均子模块直连,无包内回取。
- 路径常量驻本模块(cache 域):`_WIKI_PREFIX`/`_RESUME_STATE_PREFIX` 静态;
  `wikicache` 根经 `_wiki_cache_dir()` **调用时**解析 envs.DEEPWIKI_ROOT —— 测试
  pop+delattr 强刷后跟随新根。注:wikicache 因此比 `..utils._CLONE_ROOT`(导入期
  烘焙)更新鲜;两 conftest 都在任何 gh_puller 导入前置 env,实践中一致 —— 属既定取舍,
  后续勿单侧"修正"。
- 任务进度投影(从 live 任务组装快照 + 锁串行)属 runtime 语义,
  在 apps/deepwiki-webui/server/tasks.py;本模块只保留状态文件的读/写/删原语。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .. import envs
from ..utils import Repo, TaskStatus, _log
from .utils import config_kind, generator_identity, resolve_generator

if TYPE_CHECKING:  # 仅注解:wiki.py 反向依赖本模块,避免运行时导入环
    from .wiki import WikiPage, WikiStructureModel

# 缓存目录布局常量(on-disk 契约);根目录动态解析见 _wiki_cache_dir()
_WIKI_PREFIX = "deepwiki_cache_"
_RESUME_STATE_PREFIX = "deepwiki_resume_"
_AGENT_CACHE_DIRNAME = "agent_cache"


def _wiki_cache_dir() -> str:
    """wikicache 根(调用时解析 envs.DEEPWIKI_ROOT —— 测试 pop+delattr 强刷后须跟随新根)。"""
    return os.path.join(envs.DEEPWIKI_ROOT, "wikicache")


# ---------------------------------------------------------------------------
# graphify 图产物路径(extract 的 out_dir 布局 + ready 判定;dir/path 归 cache)
# ---------------------------------------------------------------------------


def _graph_dir(repo: Repo) -> Path:
    """单仓库图产物根(extract 的 out_dir):graphify/{type}_{name},无日期层,路径稳定以支持已缓存即跳过。"""
    return Path(envs.DEEPWIKI_ROOT) / "graphify" / f"{repo.repo_type}_{repo.name}"


def _graph_path(repo: Repo) -> Path:
    """graph.json 规范路径(extract 的 out_dir 即最终目录,无 graphify-out 层)。"""
    return _graph_dir(repo) / "graph.json"


def _index_ready(repo: Repo) -> bool:
    """索引完成信号 = graph.json 已存在(复用 graphify._load_graph 的存在性语义)。"""
    return _graph_path(repo).exists()


# ---------------------------------------------------------------------------
# wiki 缓存与导出(移植 api/services/wiki/io.py;anyio IO → asyncio.to_thread + json)
# ---------------------------------------------------------------------------


def _generator_digest(generator: str | None = None, generator_config: dict | None = None, get_env=None) -> str:
    """generator 选型判等身份(不含凭证)的稳定摘要(8 hex)。

    身份 = generator + 配置摘要:file 类 = config_path(路径是身份;内容随文件,
    不读取);object 类 = provider|model。任务 id / 续跑状态 / 成品缓存路径共用:
    同一仓库与语言下不同选型的结果可以并发并存且互不串用。
    """
    generator_id, resolved = resolve_generator(generator, generator_config, get_env)
    return _generator_digest_of(generator_id, resolved)


def _generator_digest_of(generator_id: str, resolved: dict) -> str:
    return hashlib.sha1(
        f"{generator_id}|{generator_identity(generator_id, resolved)}".encode()
    ).hexdigest()[:8]


def _cache_identity(cache: dict) -> tuple[str, str]:
    """缓存内记录的公开身份(file 类:generator+config_path;object 类:generator+provider|model)。"""
    generator = cache.get("generator") or ""
    config_path = cache.get("config_path") or ""
    if generator and config_path:
        return (generator, config_path)
    return (generator, f"{cache.get('provider') or ''}|{cache.get('model') or ''}")


def _cache_generator_matches(cache: dict, generator: str | None = None, generator_config: dict | None = None) -> bool:
    """成品缓存与公开选型是否同轨(摘要隔离后的二次校验,防手改文件名)。"""
    generator_id, resolved = resolve_generator(generator, generator_config)
    return _cache_identity(cache) == (generator_id, generator_identity(generator_id, resolved))


def _wiki_cache_path(
    owner: str, repo: str, repo_type: str, language: str, digest: str = ""
) -> str:
    suffix = f"_{digest}" if digest else ""
    filename = f"{_WIKI_PREFIX}{repo_type}_{owner}_{repo}_{language}{suffix}.json"
    return os.path.join(_wiki_cache_dir(), filename)


def wiki_cache_exists(owner: str, repo: str, repo_type: str, language: str, digest: str = "") -> bool:
    return os.path.exists(_wiki_cache_path(owner, repo, repo_type, language, digest))


async def read_wiki_cache(
    owner: str, repo: str, repo_type: str, language: str, digest: str = ""
) -> dict | None:
    """读取成品缓存(passthrough dict);无文件/坏 JSON/非 dict → None。"""
    if not wiki_cache_exists(owner, repo, repo_type, language, digest):
        return None
    path = _wiki_cache_path(owner, repo, repo_type, language, digest)
    try:
        text = await asyncio.to_thread(lambda: Path(path).read_text(encoding="utf-8"))
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        _log(f"读取 wiki 缓存失败: {path}")
        return None


async def save_wiki_cache(
    owner: str, repo: str, repo_type: str, language: str,
    wiki_cache: dict, digest: str = "",
) -> bool:
    path = _wiki_cache_path(owner, repo, repo_type, language, digest)
    try:
        await asyncio.to_thread(
            lambda: Path(path).write_text(json.dumps(wiki_cache), encoding="utf-8")
        )
        return True
    except OSError:
        _log(f"写 wiki 缓存失败: {path}")
        return False


async def save_generated_wiki(
    owner: str, repo: str, repo_type: str, repo_url: str,
    structure: WikiStructureModel, pages: dict[str, WikiPage], language: str = "en",
    generator: str | None = None, generator_config: dict | None = None,
) -> bool:
    """把一次完整生成结果落成品缓存(缓存层职责:判等身份 + 组装 + 写盘)。

    从选型解析判等身份(file 类 config_path / object 类 provider|model),
    digest 与任务 id/续跑状态共用同一判等;与旧 `_save` 逐字段等价 —— 组装为纯 dict
    (键集逐字保留),公开身份入缓存、凭证不进(**token=None**);file 类不落
    provider/model(provider=None/model="")。
    """
    generator_id, resolved = resolve_generator(generator, generator_config)
    identity = generator_identity(generator_id, resolved)  # file 类:config_path;object:"provider|model"
    object_parts = identity.split("|", 1)
    cache_record = {
        "wiki_structure": dataclasses.asdict(structure),
        "generated_pages": {pid: dataclasses.asdict(pg) for pid, pg in pages.items()},
        "repo_url": None,  # compatible for old cache
        "repo": {
            "owner": owner,
            "repo": repo,
            "type": repo_type,
            "token": None,  # 缓存文件不落 token
            "localPath": None,
            "repoUrl": repo_url,
        },
        "provider": object_parts[0] if len(object_parts) > 1 else None,  # object 类才落
        "model": object_parts[1] if len(object_parts) > 1 else "",  # 旧缓存兼容字段
        "generator": generator_id,  # 成品缓存记判等身份,cache 命中时校验(见 is_cached)
        "config_path": identity if config_kind(generator_id) == "file" else None,
    }
    return await save_wiki_cache(
        owner=owner,
        repo=repo,
        repo_type=repo_type,
        language=language,
        digest=_generator_digest_of(generator_id, resolved),
        wiki_cache=cache_record,
    )


async def delete_wiki_cache(
    owner: str, repo: str, repo_type: str, language: str, digest: str = ""
) -> bool:
    path = _wiki_cache_path(owner, repo, repo_type, language, digest)
    deleted = False
    if os.path.exists(path):
        os.remove(path)
        deleted = True
    # 删除缓存同时清续跑状态(本选型 + 旧格式无摘要文件),避免裸 state 无清理途径
    for state_path in (_resume_state_path(owner, repo, repo_type, language, digest),
                       _resume_state_path(owner, repo, repo_type, language)):
        if os.path.exists(state_path):
            os.remove(state_path)
            deleted = True
    return deleted


def _resume_state_path(
    owner: str, repo: str, repo_type: str, language: str, digest: str = ""
) -> str:
    suffix = f"_{digest}" if digest else ""
    filename = f"{_RESUME_STATE_PREFIX}{repo_type}_{owner}_{repo}_{language}{suffix}.json"
    return os.path.join(_wiki_cache_dir(), filename)


async def write_resume_state(
    owner: str, repo: str, repo_type: str, language: str,
    state: dict, digest: str = "",
) -> bool:
    """原子写续跑状态(先写 .tmp 再 os.replace,崩溃不产生半截文件)。

    纯 dict 进出(json.dumps);路径带公开选型摘要(与成品缓存同规则):
    不同选型的续跑状态并存。状态内 request.target 恒为 _strip_creds 落盘形态
    (凭证已剥离),组装由 app 侧 _persist_state 负责。
    """
    path = _resume_state_path(owner, repo, repo_type, language, digest)
    tmp = f"{path}.tmp"
    try:
        await asyncio.to_thread(
            lambda: Path(tmp).write_text(json.dumps(state), encoding="utf-8")
        )
        os.replace(tmp, path)
        return True
    except OSError as e:
        _log(f"写续跑状态失败: {path} - {e}")
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return False


async def read_resume_state(
    owner: str, repo: str, repo_type: str, language: str, digest: str = ""
) -> dict | None:
    """读取续跑状态;无文件/坏 JSON/缺 request 键 → None(自动降级为全新生成)。

    浅检(非 dict / 缺 request)回 None:手编坏文件视同"无状态",防下游 KeyError。
    """
    path = _resume_state_path(owner, repo, repo_type, language, digest)
    if not os.path.exists(path):
        return None
    try:
        text = await asyncio.to_thread(lambda: Path(path).read_text(encoding="utf-8"))
        data = json.loads(text)
        if not isinstance(data, dict) or "request" not in data:
            return None
        return data
    except Exception as _e_dbg:
        _log(f"读取续跑状态失败: {path} :: {type(_e_dbg).__name__}: {_e_dbg}")
        return None


async def delete_resume_state(
    owner: str, repo: str, repo_type: str, language: str, digest: str = ""
) -> bool:
    path = _resume_state_path(owner, repo, repo_type, language, digest)
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


async def list_wiki_cache() -> list[dict]:
    """扫描缓存目录,按文件名拆解为 (type, owner, repo, language) 摘要 dict。

    dict 键为 snake summary 契约(id/owner/repo/repo_type/language/status/digest/
    pages_done/pages_total/current_page_ids/error/submitted_at + computed name),
    由 app 响应模型校验出网;status 恒为 COMPLETED(文件存在即完成产物)。
    """
    if not os.path.exists(_wiki_cache_dir()):
        return []
    entries: list[dict] = []
    for filename in await asyncio.to_thread(os.listdir, _wiki_cache_dir()):
        if not (filename.startswith(_WIKI_PREFIX) and filename.endswith(".json")):
            continue
        file_path = os.path.join(_wiki_cache_dir(), filename)
        try:
            stats = await asyncio.to_thread(os.stat, file_path)
            parts = os.path.splitext(filename)[0].removeprefix(_WIKI_PREFIX).split("_")
            # 列尾 _<digest8> 为公开选型摘要(同一仓库多选型并存);缺省无摘要(旧缓存兼容)
            has_digest = len(parts) > 1 and len(parts[-1]) == 8 and re.fullmatch(r"[0-9a-f]+", parts[-1])
            language_idx = -2 if has_digest else -1
            owner = parts[1]
            repo = "_".join(parts[2:language_idx])
            entries.append(
                {
                    "id": filename,
                    "owner": owner,
                    "repo": repo,
                    "repo_type": parts[0],
                    "language": parts[language_idx],
                    "status": TaskStatus.COMPLETED,
                    "digest": parts[-1] if has_digest else "",
                    "pages_done": 0,
                    "pages_total": 0,
                    "current_page_ids": [],
                    "error": None,
                    "submitted_at": int(stats.st_mtime * 1000),
                    "name": f"{owner}/{repo}",
                }
            )
        except Exception:
            _log(f"解析缓存文件失败: {file_path}")
    return entries


async def list_processed_projects() -> list[dict]:
    project_entries = [
        {
            "id": wiki["id"],
            "owner": wiki["owner"],
            "repo": wiki["repo"],
            "name": wiki["name"],
            "repo_type": wiki["repo_type"],
            "submittedAt": wiki["submitted_at"],
            "language": wiki["language"],
            "digest": wiki["digest"],
        }
        for wiki in await list_wiki_cache()
    ]
    project_entries.sort(key=lambda p: p["submittedAt"], reverse=True)
    return project_entries


def export_wiki(
    repo_url: str,
    pages: list[dict],
    format: Literal["json", "markdown"],
    timestamp: datetime | None = None,
) -> str:
    """导出 wiki 为 markdown/json 字符串(与 io.py 同式;pages 为 dict 列表)。"""
    dt = timestamp or datetime.now()
    if format == "json":
        export_data = {
            "metadata": {
                "repository": repo_url,
                "generated_at": dt.isoformat(),
                "page_count": len(pages),
            },
            "pages": list(pages),
        }
        return json.dumps(export_data, indent=2)
    if format == "markdown":
        markdown = f"# Wiki Documentation for {repo_url}\n\n"
        markdown += f"Generated on: {dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        markdown += "## Table of Contents\n\n"
        for page in pages:
            markdown += f"- [{page['title']}](#{page['id']})\n"
        markdown += "\n"
        for page in pages:
            markdown += f"<a id='{page['id']}'></a>\n\n"
            markdown += f"## {page['title']}\n\n"
            if page.get("relatedPages"):
                related_titles = []
                for related_id in page["relatedPages"]:
                    related_page = next((p for p in pages if p["id"] == related_id), None)
                    if related_page:
                        related_titles.append(f"[{related_page['title']}](#{related_id})")
                if related_titles:
                    markdown += "### Related Pages\n\n"
                    markdown += "Related topics: " + ", ".join(related_titles) + "\n\n"
            markdown += f"{page['content']}\n\n"
            markdown += "---\n\n"
        return markdown
    raise NotImplementedError(
        f"Exporting wiki to format {format} is not supported. Must be one of 'markdown' or 'json'."
    )
