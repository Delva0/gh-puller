"""Tool providers and catalogs; the agent chooses the active providers for each session."""

from .common import ToolStorage
from .github_api import API_VERSION, BACKEND, GitHubUnavailableError
from .tool_bash import BASH_DEFINITIONS, BashTool
from .tool_gitcode import API_TOOL_DEFINITIONS as GITCODE_TOOL_DEFINITIONS
from .tool_gitcode import BACKEND as GITCODE_BACKEND
from .tool_gitcode import DSL_TOOL_DEFINITIONS as GITCODE_DSL_TOOL_DEFINITIONS
from .tool_gitcode import GitCodeDSLTool, GitCodeTool
from .tool_github import DSL_TOOL_DEFINITIONS as GITHUB_DSL_TOOL_DEFINITIONS
from .tool_github import GRAPHQL_TOOL_DEFINITIONS as GITHUB_GRAPHQL_TOOL_DEFINITIONS
from .tool_github import REST_TOOL_DEFINITIONS as GITHUB_REST_TOOL_DEFINITIONS
from .tool_github import GitHubDSLTool, GitHubGraphQLTool, GitHubRESTTool
from .tool_web import DEFAULT_SEARCH_INTERVAL, WEB_TOOL_DEFINITIONS, WebTools

TOOL_DEFINITIONS = (GITHUB_DSL_TOOL_DEFINITIONS + GITHUB_REST_TOOL_DEFINITIONS
                    + GITHUB_GRAPHQL_TOOL_DEFINITIONS + WEB_TOOL_DEFINITIONS
                    + GITCODE_TOOL_DEFINITIONS + GITCODE_DSL_TOOL_DEFINITIONS)

__all__ = [
    "API_VERSION", "BACKEND", "BASH_DEFINITIONS", "DEFAULT_SEARCH_INTERVAL",
    "GITCODE_BACKEND", "GITCODE_DSL_TOOL_DEFINITIONS", "GITCODE_TOOL_DEFINITIONS", "GITHUB_DSL_TOOL_DEFINITIONS",
    "GITHUB_GRAPHQL_TOOL_DEFINITIONS", "GITHUB_REST_TOOL_DEFINITIONS", "TOOL_DEFINITIONS",
    "BashTool", "GitCodeDSLTool", "GitCodeTool", "GitHubDSLTool", "GitHubGraphQLTool", "GitHubRESTTool",
    "GitHubUnavailableError",
    "ToolStorage", "WebTools",
]
