"""Verify offline tool composition, original evidence and incomplete-source semantics."""
# ruff: noqa: S101 - Research tests use pytest assertions.

import json
import sqlite3
from contextlib import closing

import pytest
from jsonschema import Draft202012Validator

from playground.graphub import test_change_git, test_landings
from playground.graphub.test_change_index import add, prepare, pull
from playground.graphub.text_index import build

from .github import GitHub

canonical = test_landings.canonical
repository = test_change_git.repository


@pytest.fixture
def corpus(canonical, repository, tmp_path):
    db, _ = canonical
    before = test_change_git.commit(repository, {b"widget.py": (b"100644", b"before")})
    after = test_change_git.commit(repository, {b"widget.py": (b"100644", b"after")}, (before,))
    add(db, 1, 1, "issue", {"title": "Original needle", "body": "original 中文\nsecond line",
                            "html_url": "https://example.test/issues/1"})
    add(db, 2, 2, "issue", {"title": "Relevant PR", "body": "needle proposal", "pull_request": {}})
    add(db, 3, 2, "issue-comments", [{"body": "needle discussion"}])
    add(db, 4, 2, "pull", pull(before, after, merged=True, landing=after))
    add(db, 5, 3, "issue", {"title": "oldcanary", "body": "old complete"})
    add(db, 6, 3, "issue", {"title": "newcanary", "body": "known partial"}, coverage="partial")
    add(db, 20, 4, "issue", {"title": "futurecanary", "body": "future"})
    changes, text, source = (tmp_path / name for name in ("changes.sqlite3", "text.sqlite3", "source.sqlite3"))
    scope = prepare(db, repository, changes)
    build(db, text, set(), scope)
    db.commit()
    with closing(sqlite3.connect(source)) as destination:
        db.backup(destination)
    return source, text, changes, repository, scope


@pytest.mark.asyncio
async def test_tools_compose_queries_with_exact_original_evidence_and_native_endpoints(corpus):
    async with GitHub(*corpus) as github:
        tools = {tool.name: tool for tool in github.tools()}
        assert set(tools) == {"GitHubSearch", "GitHubChanges", "GitHubRead"}
        for tool in tools.values():
            Draft202012Validator.check_schema(tool.parameters)
        result = json.loads(await tools["GitHubSearch"].invoke({"query": "needle"}))
        assert {hit["number"] for hit in result["matches"]} == {1, 2}
        assert len(result["matches"]) == 2
        first = json.loads(await tools["GitHubRead"].invoke({"number": 1, "location": "/value/body", "characters": 10}))
        rest = json.loads(await tools["GitHubRead"].invoke({"number": 1, "location": "/value/body",
                                                           "offset": first["next_offset"]}))
        assert first["text"] + rest["text"] == "original 中文\nsecond line"
        assert first["source"]["observation"] == 1 and first["encoding"] == "original_string"
        changed = json.loads(await tools["GitHubChanges"].invoke({"paths": ["widget.py"], "kind": "landing"}))
        hit, = changed["matches"]
        assert hit["number"] == 2 and hit["score"] == 1
        assert hit["sources"][0]["observation"] == 4
        assert hit["matched"] == [{"path": "widget.py", "hex": b"widget.py".hex()}]
        assert changed["verified"]["changes"] == 1
        assert github.changed(["other/widget.py"], "landing")["matches"] == []
    with pytest.raises(sqlite3.ProgrammingError):
        github.db.execute("SELECT 1")


@pytest.mark.asyncio
async def test_missing_partial_and_future_observations_are_not_negative_facts(corpus):
    async with GitHub(*corpus) as github:
        assert github.search("oldcanary OR newcanary OR futurecanary")["matches"] == []
        partial = github.read(3, location="/value/body")
        assert partial["source"]["observation"] == 6 and partial["source"]["coverage"] == "partial"
        assert partial["text"] == "known partial"
        for number, family in ((999, "issue"), (4, "issue"), (1, "issue-comments")):
            absent = github.read(number, family)
            assert absent["source"]["coverage"] == "not_observed" and "text" not in absent


@pytest.mark.asyncio
async def test_indexed_text_corruption_is_rejected_against_original_bytes(corpus):
    with closing(sqlite3.connect(corpus[1])) as index, index:
        index.execute("UPDATE docs SET text='invented needle' WHERE number=1")
    async with GitHub(*corpus) as github:
        with pytest.raises(ValueError, match="original evidence"):
            github.search("invented")


@pytest.mark.asyncio
async def test_benchmark_exclusions_cannot_silently_narrow_the_agent_corpus(corpus):
    with closing(sqlite3.connect(corpus[1])) as index, index:
        index.execute("UPDATE meta SET value='[1]' WHERE key='excluded'")
    with pytest.raises(ValueError, match="unexcluded"):
        async with GitHub(*corpus):
            pass


@pytest.mark.asyncio
async def test_observation_cutoff_and_changed_file_attestation_are_required(corpus):
    source, text, changes, git, scope = corpus
    with pytest.raises(ValueError, match="boundary"):
        async with GitHub(source, text, changes, git, scope | {"cutoff": 20}):
            pass
    with closing(sqlite3.connect(changes)) as index, index:
        index.execute("DELETE FROM files")
    with pytest.raises(ValueError, match="attestation"):
        async with GitHub(*corpus):
            pass
