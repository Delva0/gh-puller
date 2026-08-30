"""HTTP 边界数据模型(FastAPI 请求/响应校验;验证只属于端点层)。

引擎(gh_puller.deepwiki)是纯数据/纯函数(零 pydantic,无 Request 概念):
本模块即 wire 契约的唯一验证面 —— 请求族(含 list|str 换行归一化 validator)、
响应族(含计算字段 name 与 ProcessedProjectEntry 的 submittedAt 键)。
"""

from __future__ import annotations

from typing import Any, Literal

# pydantic 在模型构建期按模块命名空间解析字段注解,RepoType/TaskStatus 必须是
# 真实模块级名字,不能收进 TYPE_CHECKING 块(否则运行时报 class not found)。
from gh_puller.utils import RepoType, TaskStatus  # noqa: TC002 - pydantic 运行时解析注解需此名
from pydantic import BaseModel, Field, computed_field, field_validator

# ---------------------------------------------------------------------------
# 请求族(HTTP 入参;与 deepwiki-open api/schemas 同形)
# ---------------------------------------------------------------------------


class RepoRequestBase(BaseModel):
    repo_url: str = Field(..., description="URL or local path of the repository")
    type: RepoType = Field("github", description="Repository type")
    token: str | None = Field(None, description="PAT for private repositories")
    target: dict[str, Any] = Field(
        default_factory=dict,
        description="target 请求形态:generator + generator_config(file 类:config_path;"
                    "object 类:provider/model/凭证;api_key/base_url 仅请求态不落盘)",
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
        """list 或换行分隔字符串(前端以字符串发送;边界处归一化为 list)。"""
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
    """POST /repo/prepare 的请求体(索引预热,无消息)。"""


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


# ---------------------------------------------------------------------------
# 响应族(HTTP 出参;字段名即 wire 键 —— camelCase 原样保留前端契约)
# ---------------------------------------------------------------------------


class Model(BaseModel):
    id: str
    name: str


class Provider(BaseModel):
    id: str
    name: str
    models: list[Model] = Field(default_factory=list)
    supportsCustomModel: bool = Field(False, description="Whether this provider supports custom models")


class ModelConfig(BaseModel):
    providers: list[Provider] = Field(..., description="Available model providers")
    defaultProvider: str = Field(..., description="ID of the default provider")


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
    # 列尾公开 target 摘要(同一仓库多 target 并存;缺省无摘要=旧格式兼容)
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
