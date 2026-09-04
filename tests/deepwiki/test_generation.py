"""Test DeepWiki generator-cache paths and file production."""

import contextlib
from pathlib import Path

import pytest

from gh_puller import deepwiki
from gh_puller.agent import AGENTS, RequestFailedError
from gh_puller.deepwiki.wiki import (
    _generator_cache_dir,
    _generator_cache_page_path,
    _generator_cache_structure_path,
    _produce_file,
)
from tests.deepwiki._support import _digest_of, _gen_kwargs, _make_request


def test_generator_cache_path_naming():
    request = {"repo_url": "/x", "type": "local", "owner": "local",
               "repo": "deepwiki-open", "language": "en", "target": {}}
    project_key = deepwiki.repo_key_of("local", "local", "deepwiki-open")
    prefix = f"{project_key}_{_digest_of(request['target'])}"
    gen_kw = _gen_kwargs(request["target"])
    assert _generator_cache_structure_path(project_key, **gen_kw).name == f"{prefix}-structure.md"
    assert _generator_cache_page_path(project_key, "page-1", **gen_kw).name == f"{prefix}-page-1.md"
    assert _generator_cache_page_path(project_key, "overview", **gen_kw).name == \
        f"{prefix}-page_overview.md"

def test_sanitize_page_id_no_escape():
    r = _make_request("sanitize-io", "demo")
    project_key = deepwiki.repo_key_of(r["type"], r["owner"], r["repo"])
    out = _generator_cache_page_path(project_key, "../../evil", **_gen_kwargs(r["target"]))
    assert out.parent == _generator_cache_dir(project_key, **_gen_kwargs(r["target"]))
    assert out.relative_to(_generator_cache_dir(project_key, **_gen_kwargs(r["target"]))).parent == Path(".")

@pytest.mark.asyncio
async def test_produce_file_wraps_request_failure(monkeypatch, tmp_path):
    """Wrap request failures while retaining the original SDK error as the cause."""

    @contextlib.asynccontextmanager
    async def fake_session(self, **kw):  # Avoid constructing an SDK client.
        yield self

    async def boom_stream(self, prompt, **kw):
        raise RequestFailedError("sdk exploded")
        yield  # Keep the stub an async generator.

    monkeypatch.setattr(AGENTS["cc"], "session", fake_session)  # Patch the registered adapter instance.
    monkeypatch.setattr(AGENTS["cc"], "stream", boom_stream)
    out_path = tmp_path / "out" / "page.md"
    with pytest.raises(RuntimeError) as ei:
        await _produce_file(AGENTS["cc"]({}), "", out_path,
                            label="wiki:page:p1", run_id="r1")
    assert str(ei.value) == "generator 执行失败: sdk exploded"
    assert isinstance(ei.value.__cause__, RequestFailedError)
    assert not out_path.exists()  # Failed generation must not leave an output file.
