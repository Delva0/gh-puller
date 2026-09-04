"""Share request builders across DeepWiki engine tests."""

from gh_puller import deepwiki
from gh_puller.deepwiki import WikiPage, WikiStructureModel
from gh_puller.deepwiki.utils import generator_digest
from gh_puller.utils import Repo


def _make_page(page_id: str) -> WikiPage:
    return WikiPage(
        id=page_id,
        title=f"Page {page_id}",
        content="",
        filePaths=["src/main.py"],
        importance="medium",
        relatedPages=[],
    )


def _make_structure(page_ids: list[str]) -> WikiStructureModel:
    return WikiStructureModel(
        id="wiki", title="T", description="", pages=[_make_page(p) for p in page_ids],
    )


def _make_request(owner: str, repo: str) -> dict:
    return {
        "repo_url": "/tmp/gh-puller-test-repo", "type": "local", "owner": owner,
        "repo": repo, "language": "en", "target": {}, "token": None,
    }


def _digest_of(choice: "dict | None") -> str:
    """Return the production digest for a wire-shaped target selection."""
    return generator_digest((choice or {}).get("generator"), (choice or {}).get("generator_config"))


def _repo_of(req: dict) -> Repo:
    """Build a repository value from a wire-shaped request."""
    return Repo(req["repo_url"], req["type"], access_token=req.get("token"))


def _gen_kwargs(choice: dict | None) -> dict:
    """Split a wire target into the generator keyword arguments."""
    c = choice or {}
    return {"generator": c.get("generator"), "generator_config": dict(c.get("generator_config") or {})}


async def _chat(req: dict) -> list[str]:
    """Collect chat output for a wire-shaped request."""
    return [c async for c in deepwiki.chat_stream(
        **_gen_kwargs(req["target"]), repo=_repo_of(req), messages=req["messages"],
        language=req.get("language", "en"),
        research_iteration=req.get("research_iteration", 1),
    )]
