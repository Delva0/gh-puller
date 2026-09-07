"""Validate DeepWiki HTTP requests and responses at the application boundary.

The engine remains free of Pydantic and request objects; this module is the sole wire
schema authority.
"""

from __future__ import annotations

from typing import Any, Literal

# Pydantic resolves these annotations from the runtime module namespace.
from gh_puller.utils import RepoType, TaskStatus  # noqa: TC002 - Required during model construction.
from pydantic import BaseModel, Field, computed_field, field_validator

# --- Requests ---


class RepoRequestBase(BaseModel):
    repo_url: str = Field(..., description="URL or local path of the repository")
    type: RepoType = Field("github", description="Repository type")
    token: str | None = Field(None, description="PAT for private repositories")
    target: dict[str, Any] = Field(
        default_factory=dict,
        description="Generator selection and configuration; credentials are request-only",
    )
    language: str = Field("en", description="Language for content generation")
    excluded_dirs: list[str] = Field(
        default_factory=list,
        description="List or newline-separated string of directories to exclude from processing",
    )
    excluded_files: list[str] = Field(
        default_factory=list,
        description="List or newline-separated string of file patterns to exclude from processing",
    )
    included_dirs: list[str] = Field(
        default_factory=list,
        description="List or newline-separated string of directories to include exclusively",
    )
    included_files: list[str] = Field(
        default_factory=list,
        description="List or newline-separated string of file patterns to include exclusively",
    )

    @field_validator(
        "excluded_dirs",
        "excluded_files",
        "included_dirs",
        "included_files",
        mode="before",
    )
    @classmethod
    def validate_path(cls, value: list[str] | str) -> list[str]:
        """Normalize a list or newline-delimited frontend string to a list."""
        if isinstance(value, str):
            value = [p.strip() for p in value.split("\n") if p.strip()]
        return value


class ChatMessage(BaseModel):
    role: str  # 'user' or 'assistant'
    content: str
    mode: Literal["normal", "deep_research"] = Field(default="normal")


class ChatCompletionRequest(RepoRequestBase):
    messages: list[ChatMessage] = Field(..., description="List of chat messages")
    research_iteration: int = Field(
        default=1,
        ge=1,
        description="Current deep research iteration (1-based). Only used when the request is in deep_research mode.",
    )


class RepoPrepareRequest(RepoRequestBase):
    """Validate an index-warmup request without chat messages."""


class WikiTaskRequest(RepoRequestBase):
    owner: str
    repo: str
    comprehensive: bool = Field(True, description="Comprehensive vs concise wiki")

    @property
    def repo_key(self) -> str:
        return f"{self.type}_{self.owner}_{self.repo}"


class CodeMapRequest(RepoRequestBase):
    question: str = Field(..., description="The user's how-to / usage question")


class AuthorizationConfig(BaseModel):
    code: str = Field(..., description="Authorization code")


# --- Responses ---


class RepoInfo(BaseModel):
    owner: str
    repo: str
    type: str
    token: str | None = None
    localPath: str | None = None
    repoUrl: str | None = None


class WikiPage(BaseModel):
    id: str
    title: str
    content: str
    filePaths: list[str]
    importance: str  # 'high' | 'medium' | 'low'
    relatedPages: list[str]


class WikiSection(BaseModel):
    id: str
    title: str
    pages: list[str]
    subsections: list[str] | None = None


class WikiStructureModel(BaseModel):
    id: str
    title: str
    description: str
    pages: list[WikiPage]
    sections: list[WikiSection] | None = None
    rootSections: list[str] | None = None


class WikiExportRequest(BaseModel):
    repo_url: str = Field(..., description="URL of the repository")
    pages: list[WikiPage] = Field(..., description="List of wiki pages to export")
    format: Literal["markdown", "json"] = Field(..., description="Export format")


class ProcessedProjectEntry(BaseModel):
    id: str  # Filename
    owner: str
    repo: str
    name: str  # owner/repo
    repo_type: str
    submittedAt: int
    language: str
    digest: str = ""


class WikiTaskSummary(BaseModel):
    id: str
    owner: str
    repo: str
    repo_type: str
    language: str
    status: TaskStatus
    # Legacy cache names have no public target digest suffix.
    digest: str = ""
    pages_done: int = Field(default=0, ge=0)
    pages_total: int = Field(default=0, ge=0)
    current_page_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    submitted_at: int = Field(..., ge=0)

    @computed_field
    @property
    def name(self) -> str:
        return f"{self.owner}/{self.repo}"


class WikiTaskStatus(WikiTaskSummary):
    wiki_structure: WikiStructureModel | None = None
