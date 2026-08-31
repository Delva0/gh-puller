"""通用基础设施层:与 wiki/具体方法无关的共享代码(从 deepwiki.py 抽出,2026-08)。

内容:
- Repo 族:远端仓库/本地路径统一句柄(git CLI 克隆,local 直读)、URL 判别、
  三 host token 注入、文件树遍历与过滤、README 选取、默认分支探测、
  带路径穿越防护的仓库内文件读取。
- 纯工具:stderr 日志、路径段安全化、token 粗估算、markdown fence 剥离、
  NDJSON 事件序列化、LLM JSON 修复/提取。
- 通用状态枚举:TaskStatus(唯一共享枚举;状态机/注册表 TTL 等任务调度
  已迁至 apps/deepwiki-webui/server/tasks.py —— 全仓唯一消费者,不留底层抽象)。

约定:
- 本模块导入无副作用(不建目录、不建 asyncio 锁/信号量;调度机在 app 侧实例化
  时创建,由其持有者负责业务目录与写锁)。
- _CLONE_ROOT 在导入时对 envs 快照(envs 为导入时单点快照,见 envs.py),
  同一进程内与 deepwiki 的 DEEPWIKI_ROOT 快照保持一致。
- 进度日志走 stderr(机器结果走调用方)。
"""

import json
import os
import re
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse, urlunparse

from . import envs

# ---------------------------------------------------------------------------
# 日志与全局路径
# ---------------------------------------------------------------------------


def _log(msg: str, prefix: str = "gh-puller") -> None:
    """进度日志走 stderr;prefix 由调用方固定(deepwiki 用 partial 固定 [deepwiki])。"""
    print(f"[{prefix}] {msg}", file=sys.stderr, flush=True)


# 克隆仓库根目录(repos 目录与原后端名不同:原为 <root>/repo,此处一致更新为 repos)
_CLONE_ROOT = os.path.join(envs.DEEPWIKI_ROOT, "repos")


def _sanitize_path_seg(s: str) -> str:
    """路径段安全化:page.id 等模型产出值可能含 '/' 或 '..',防目录穿越。"""
    return re.sub(r"[^A-Za-z0-9._-]", "-", s)


# ---------------------------------------------------------------------------
# Repo:git CLI 克隆(local 路径直读)
# ---------------------------------------------------------------------------

RepoType = Literal["local", "github", "gitlab", "bitbucket"]


def _path_is_url(path: str) -> bool:
    """判别 URL 或本地路径(同原 repository.py)。"""
    try:
        result = urlparse(path)
        return result.scheme in {"http", "https", "ftp"} and bool(result.netloc)
    except Exception:
        return False


def _clone_url_with_token(repo_url: str, repo_type: str, token: str) -> str:
    """按 host 方案把 PAT 注入克隆 URL(三 host 前缀不同,移植自原 repository.py)。"""
    parsed = urlparse(repo_url)
    quoted = quote(token, safe="")
    if repo_type == "github":
        netloc = f"{quoted}@{parsed.netloc}"
    elif repo_type == "gitlab":
        netloc = f"oauth2:{quoted}@{parsed.netloc}"
    else:  # bitbucket:ATCTT 前缀的 HTTP access token 用 x-bitbucket-api-token-auth,app password 用 x-token-auth
        scheme = "x-bitbucket-api-token-auth" if token.startswith("ATCTT") else "x-token-auth"
        netloc = f"{scheme}:{quoted}@{parsed.netloc}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


class Repo:
    """远程仓库或本地路径的统一句柄:URL → git CLI 克隆到 <root>/<name>,本地路径 → 直读。"""

    def __init__(
        self,
        repo_url: str,
        repo_type: str | None = None,
        root_path: str = _CLONE_ROOT,
        access_token: str | None = None,
    ):
        self.repo_url = repo_url
        self.repo_type = repo_type or "github"
        self.root_path = root_path
        self.access_token = access_token
        os.makedirs(root_path, exist_ok=True)

    @staticmethod
    def _extract_repo_name(repo_url: str, repo_type: str | None) -> str:
        if _path_is_url(repo_url):
            url_parts = repo_url.rstrip("/").split("/")
            if repo_type in ("github", "gitlab", "bitbucket") and len(url_parts) >= 5:
                # {owner}_{repo};gitlab 的多级 group 取最后两级(与原版同式)
                return f"{url_parts[-2]}_{url_parts[-1].replace('.git', '')}"
            return url_parts[-1].replace(".git", "")
        return os.path.basename(os.path.normpath(repo_url))

    @property
    def name(self) -> str:
        return self._extract_repo_name(self.repo_url, self.repo_type)

    @property
    def is_local(self) -> bool:
        return not _path_is_url(self.repo_url)

    @property
    def save_path(self) -> str:
        return self.repo_url if self.is_local else os.path.join(self.root_path, self.name)

    @property
    def downloaded(self) -> bool:
        return os.path.exists(self.save_path) and bool(os.listdir(self.save_path))

    def download(self, force: bool = False) -> None:
        """git CLI 克隆(depth=1 单分支);失败转 ValueError,错误信息隐藏 token。"""
        if force or (not self.downloaded and not self.is_local):
            os.makedirs(self.save_path, exist_ok=True)
            url = (
                _clone_url_with_token(self.repo_url, self.repo_type, self.access_token)
                if self.access_token
                else self.repo_url
            )
            try:
                subprocess.run(
                    ["git", "clone", "--depth=1", "--single-branch", url, self.save_path],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=600,
                )
            except FileNotFoundError as e:
                raise RuntimeError("Missing `git` in current environment") from e
            except subprocess.CalledProcessError as e:
                msg = (e.stderr or str(e)).strip()
                if self.access_token:  # 一律抹掉 token(原始与百分号编码两种形态)
                    token = self.access_token
                    msg = msg.replace(token, "***TOKEN***").replace(
                        quote(token, safe=""), "***TOKEN***",
                    )
                raise ValueError(msg) from e

    def __repr__(self) -> str:
        return f"{self.repo_type}: {self.name}"


# ---------------------------------------------------------------------------
# 文件树与过滤(原 api/config.py 的 iterate_files + repo.json 规则精简内嵌)
# ---------------------------------------------------------------------------


def _should_process_file(
    rel_parts: tuple[str, ...],
    use_inclusion: bool,
    included_dirs: list[str],
    included_files: list[str],
    excluded_dirs: list[str],
    excluded_files: list[str],
) -> bool:
    """路径片段规则(与原版同逻辑:目录名任意一段包含匹配;文件名完全匹配或 endswith)。"""
    name = rel_parts[-1]
    if use_inclusion:
        if included_dirs:
            for included in included_dirs:
                if included.strip("/") in rel_parts:
                    return True
        if included_files:
            for included_file in included_files:
                if name == included_file or name.endswith(included_file):
                    return True
        return not included_dirs and not included_files
    for excluded in excluded_dirs:
        if excluded.strip("/") in rel_parts:
            return False
    return name not in excluded_files


def iterate_files(
    root_dir: str,
    included_files: list[str] | None = None,
    included_dirs: list[str] | None = None,
    excluded_files: list[str] | None = None,
    excluded_dirs: list[str] | None = None,
) -> list[str]:
    """遍历仓库,返回值得处理的相对路径列表(扩展名限制 + include/exclude 规则)。"""
    root = Path(root_dir).resolve()
    use_inclusion = bool(included_dirs or included_files)
    if use_inclusion:
        inc_dirs = list(set(included_dirs or []))
        inc_files = list(set(included_files or []))
        exc_dirs: list[str] = []
        exc_files: list[str] = []
    else:
        exc_dirs = list(set(_DEFAULT_EXCLUDED_DIRS).union(excluded_dirs or []))
        exc_files = list(set(_DEFAULT_EXCLUDED_FILES).union(excluded_files or []))
        inc_dirs = []
        inc_files = []

    results: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _PROCESS_EXTENSIONS:
            continue
        rel_parts = tuple(p.relative_to(root).parts)
        if _should_process_file(
            rel_parts, use_inclusion, inc_dirs, inc_files, exc_dirs, exc_files,
        ):
            results.append("/".join(rel_parts))
    return results


def read_repo_file_tree(
    path: str,
    included_files: list[str] | None = None,
    included_dirs: list[str] | None = None,
    excluded_files: list[str] | None = None,
    excluded_dirs: list[str] | None = None,
) -> tuple[list[str], str]:
    """遍历克隆/本地仓库 → (文件列表, README.md 文本)。"""
    files = iterate_files(
        root_dir=path,
        included_files=included_files,
        included_dirs=included_dirs,
        excluded_dirs=excluded_dirs,
        excluded_files=excluded_files,
    )
    readme = ""
    for file in sorted(files, key=len):
        if os.path.splitext(file)[0].lower().endswith("readme"):
            try:
                readme = Path(path, file).read_text(encoding="utf-8")
            except OSError as e:
                _log(f"读取 README 失败: {file} - {e}")
                readme = ""
            break
    return files, readme


def _find_readme_path(files: list[str]) -> str | None:
    """从文件列表选出 README 相对路径(与 read_repo_file_tree 的选取规则同式);无则 None。"""
    for file in sorted(files, key=len):
        if os.path.splitext(file)[0].lower().endswith("readme"):
            return file
    return None


def detect_default_branch(path: str) -> str:
    """返回当前检出分支;失败回退 "main"(与原版同)。"""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or "main"
    except (subprocess.SubprocessError, OSError):
        return "main"


def read_repo_file(repo_url: str, repo_type: str | None, file_path: str) -> str:
    """读取仓库内文件,带路径穿越防护(原 codemap.py 同式)。"""
    repo = Repo(repo_url=repo_url, repo_type=repo_type)
    repo_dir = os.path.realpath(repo.save_path)
    target = os.path.realpath(os.path.join(repo_dir, file_path))
    if os.path.commonpath([repo_dir, target]) != repo_dir:
        raise ValueError("Resolved path escapes the repository directory")
    if not os.path.isfile(target):
        raise FileNotFoundError(file_path)
    with open(target, encoding="utf-8", errors="replace") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 纯文本/序列化小工具(LLM 结果清洗与事件输出)
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算:约 4 字符/token(不引 tiktoken,与原 MAX_INPUT_TOKENS 提示语义对齐)。"""
    return max(1, len(text) // 4)


def _strip_markdown_fences(content: str) -> str:
    """剥离整体 markdown code fence(模型偶尔会包一层)。"""
    content = re.sub(r"^```markdown\s*", "", content, flags=re.IGNORECASE)
    return re.sub(r"```\s*$", "", content)


def _event(**payload) -> str:
    """序列化一行 NDJSON 事件。"""
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _phase(phase: str, status: str, **extra) -> str:
    return _event(type="phase", phase=phase, status=status, **extra)


def _repair_json(candidate: str) -> str:
    """修复常见 LLM JSON 瑕疵(尾逗号、`" "key":`)。"""
    repaired = re.sub(r",\s*([}\]])", r"\1", candidate)  # trailing commas
    return re.sub(r'"\s+"(\w+)"\s*:', r'"\1":', repaired)


def _extract_json(text: str) -> dict:
    """从模型输出中尽力提取单个 JSON 对象(剥 fence、平衡花括号扫描、瑕疵修复)。"""
    if not text:
        raise ValueError("Empty model response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("No JSON object found in model response")
    depth = 0
    in_str = False
    escape = False
    candidate = cleaned[start:]
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = cleaned[start : i + 1]
                break
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return json.loads(_repair_json(candidate))  # 仍可能抛 → 调用方重试


# ---------------------------------------------------------------------------
# 任务状态枚举(唯一共享枚举:模型契约/缓存列表/端点;调度机在 server/tasks.py)
# ---------------------------------------------------------------------------


class TaskStatus(StrEnum):
    PENDING = "pending"
    INDEXING = "indexing"
    DETERMINING_STRUCTURE = "determining_structure"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"

    def is_terminal(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED)


# ---------------------------------------------------------------------------
# 模块级文件过滤默认规则(嵌入精简版 repo.json 的 file_filters + extensions)
# TODO: 这里存在一些约定，考虑是否通用，是否值得留在 utils.py
# ---------------------------------------------------------------------------

_DEFAULT_EXCLUDED_DIRS = {
    ".venv", "venv", "env", "node_modules", "bower_components", "jspm_packages",
    ".git", ".svn", ".hg", ".bzr", "vendor", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "out", "target", "bin", "obj",
    "docs", "_docs", "site-docs", "_site", ".idea", ".vscode", ".vs", ".eclipse",
    ".settings", "logs", "log", "tmp", "temp",
}
_DEFAULT_EXCLUDED_FILES = {
    "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json", "poetry.lock",
    "Pipfile.lock", "Cargo.lock", "composer.lock", ".DS_Store", ".gitignore",
    ".gitattributes", ".gitmodules", "README.md", "readme.md", "pyproject.toml",
    "tsconfig.json", "package.json", "package-lock.json",
}
# 与原 repo.json code_extensions(17) + doc_extensions(6) 并集;另补常见脚本与数据文件
_PROCESS_EXTENSIONS = {
    ".py", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".hpp", ".go", ".rs",
    ".jsx", ".tsx", ".html", ".css", ".php", ".swift", ".cs", ".md", ".txt",
    ".rst", ".json", ".yaml", ".yml", ".sh", ".sql", ".toml", ".vue", ".svelte",
}
