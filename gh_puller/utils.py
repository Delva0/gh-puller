"""Provide repository access and serialization utilities shared across engines.

Importing this module has no filesystem side effects. Repository directories are
created only when a ``Repo`` is instantiated, and progress messages use stderr.
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


def _log(msg: str, prefix: str = "gh-puller") -> None:
    print(f"[{prefix}] {msg}", file=sys.stderr, flush=True)


_CLONE_ROOT = os.path.join(envs.DEEPWIKI_ROOT, "repos")


def _sanitize_path_seg(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", s)


# --- Repository access ---

RepoType = Literal["local", "github", "gitlab", "bitbucket"]


def _path_is_url(path: str) -> bool:
    try:
        result = urlparse(path)
        return result.scheme in {"http", "https", "ftp"} and bool(result.netloc)
    except Exception:
        return False


def _clone_url_with_token(repo_url: str, repo_type: str, token: str) -> str:
    """Inject a percent-encoded access token using host-specific credentials."""
    parsed = urlparse(repo_url)
    quoted = quote(token, safe="")
    if repo_type == "github":
        netloc = f"{quoted}@{parsed.netloc}"
    elif repo_type == "gitlab":
        netloc = f"oauth2:{quoted}@{parsed.netloc}"
    else:  # Bitbucket distinguishes HTTP access tokens from app passwords by prefix.
        scheme = "x-bitbucket-api-token-auth" if token.startswith("ATCTT") else "x-token-auth"
        netloc = f"{scheme}:{quoted}@{parsed.netloc}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


class Repo:
    """Represent a remote Git repository or a directly readable local checkout."""

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
        """Clone one shallow branch and redact credentials from Git failures."""
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
                if self.access_token:  # Git may echo either form of the credential.
                    token = self.access_token
                    msg = msg.replace(token, "***TOKEN***").replace(
                        quote(token, safe=""), "***TOKEN***",
                    )
                raise ValueError(msg) from e

    def __repr__(self) -> str:
        return f"{self.repo_type}: {self.name}"


def _should_process_file(
    rel_parts: tuple[str, ...],
    use_inclusion: bool,
    included_dirs: list[str],
    included_files: list[str],
    excluded_dirs: list[str],
    excluded_files: list[str],
) -> bool:
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
    """Return processable repository-relative files under the configured filters."""
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
    """Return processable files and the nearest README text from a checkout."""
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
    for file in sorted(files, key=len):
        if os.path.splitext(file)[0].lower().endswith("readme"):
            return file
    return None


def detect_default_branch(path: str) -> str:
    """Return the checked-out branch, falling back to ``main`` on failure."""
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
    """Read a repository file without allowing path traversal."""
    repo = Repo(repo_url=repo_url, repo_type=repo_type)
    repo_dir = os.path.realpath(repo.save_path)
    target = os.path.realpath(os.path.join(repo_dir, file_path))
    if os.path.commonpath([repo_dir, target]) != repo_dir:
        raise ValueError("Resolved path escapes the repository directory")
    if not os.path.isfile(target):
        raise FileNotFoundError(file_path)
    with open(target, encoding="utf-8", errors="replace") as f:
        return f.read()


# --- Model-output handling ---


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _strip_markdown_fences(content: str) -> str:
    content = re.sub(r"^```markdown\s*", "", content, flags=re.IGNORECASE)
    return re.sub(r"```\s*$", "", content)


def _event(**payload) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _phase(phase: str, status: str, **extra) -> str:
    return _event(type="phase", phase=phase, status=status, **extra)


def _repair_json(candidate: str) -> str:
    """Repair trailing commas and split JSON object keys in model output."""
    repaired = re.sub(r",\s*([}\]])", r"\1", candidate)  # trailing commas
    return re.sub(r'"\s+"(\w+)"\s*:', r'"\1":', repaired)


def _extract_json(text: str) -> dict:
    """Extract one balanced JSON object and repair common model mistakes."""
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
        return json.loads(_repair_json(candidate))


# --- Shared task state ---


class TaskStatus(StrEnum):
    PENDING = "pending"
    INDEXING = "indexing"
    DETERMINING_STRUCTURE = "determining_structure"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"

    def is_terminal(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED)


# --- Repository filters ---

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
_PROCESS_EXTENSIONS = {
    ".py", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".hpp", ".go", ".rs",
    ".jsx", ".tsx", ".html", ".css", ".php", ".swift", ".cs", ".md", ".txt",
    ".rst", ".json", ".yaml", ".yml", ".sh", ".sql", ".toml", ".vue", ".svelte",
}
