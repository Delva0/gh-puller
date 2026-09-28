"""Expose concrete search agents built on gh_puller.agent and gh_puller.tools.

Install gh-puller[search] to use these implementations. The generic lifecycle,
adapters and event observers remain available independently in gh_puller.agent.
"""

from .agent_search import CodeAgent, GitCodeAgent, GitHubAgent, WebAgent
from .registry import AGENTS, register

__all__ = ["AGENTS", "CodeAgent", "GitCodeAgent", "GitHubAgent", "WebAgent", "register"]
