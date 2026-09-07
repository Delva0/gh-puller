"""Expose the DeepWiki-compatible wiki, chat, and codemap engine APIs.

Prompts and frontend contracts derive from deepwiki-open (MIT License, Copyright
(c) 2024 Sheing Ng). This package owns generation and persistence; HTTP routing,
task scheduling, and graph-tool assembly remain in ``apps/deepwiki-webui``.
Generator behavior is injected through ``generator`` and ``generator_config``.
"""

from .. import envs  # noqa: F401 - Tests refresh this package-level binding.
from .chat import chat_stream
from .codemap import (
    CodeMap,
    CodeMapCitation,
    CodeMapSection,
    CodeMapStep,
    codemap_of,
    generate_codemap,
)
from .utils import repo_key_of
from .wiki import (
    WikiPage,
    WikiSection,
    WikiStructureModel,
    delete_resume_state,
    delete_wiki_cache,
    determine_structure,
    export_wiki,
    generate_page,
    hydrate_pages,
    list_processed_projects,
    list_wiki_cache,
    needs_structure_regenerate,
    read_resume_state,
    read_wiki_cache,
    save_generated_wiki,
    save_wiki_cache,
    wiki_cache_exists,
    wiki_structure_of,
    write_error_page,
    write_resume_state,
)

__all__ = [
    "CodeMap",
    "CodeMapCitation",
    "CodeMapSection",
    "CodeMapStep",
    "WikiPage",
    "WikiSection",
    "WikiStructureModel",
    "chat_stream",
    "codemap_of",
    "delete_resume_state",
    "delete_wiki_cache",
    "determine_structure",
    "export_wiki",
    "generate_codemap",
    "generate_page",
    "hydrate_pages",
    "list_processed_projects",
    "list_wiki_cache",
    "needs_structure_regenerate",
    "read_resume_state",
    "read_wiki_cache",
    "repo_key_of",
    "save_generated_wiki",
    "save_wiki_cache",
    "wiki_cache_exists",
    "wiki_structure_of",
    "write_error_page",
    "write_resume_state",
]
