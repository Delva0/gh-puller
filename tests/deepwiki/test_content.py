"""Test DeepWiki structure parsing and rendered source references."""

import pytest

from gh_puller.deepwiki.codemap import _locate_snippet
from gh_puller.deepwiki.wiki import (
    RepoUrlContext,
    parse_wiki_structure,
    post_process_wiki_content,
    render_file_links,
)
from gh_puller.utils import _extract_json, _repair_json


def test_parse_wiki_structure_full():
    xml = """<wiki_structure>
<title>Demo</title>
<description>desc</description>
<page id="p1"><title>Overview</title><file_path>app.py</file_path><importance>high</importance></page>
<page id="p2"><title>Setup</title><file_path>readme.md</file_path><related>p1</related></page>
</wiki_structure>"""
    s = parse_wiki_structure(xml, comprehensive=False)
    assert s.title == "Demo" and s.description == "desc"
    assert [p.id for p in s.pages] == ["p1", "p2"]
    assert s.pages[0].importance == "high"
    assert s.pages[1].importance == "medium"  # Missing importance normalizes to medium.
    assert s.pages[1].relatedPages == ["p1"]


def test_parse_wiki_structure_truncated():
    """Recover complete blocks from a response truncated before its closing tag."""
    xml = """<wiki_structure>
<title>Demo</title>
<page id="p1"><title>A</title><file_path>app.py</file_path></page>
"""
    s = parse_wiki_structure(xml, comprehensive=False)
    assert s.title == "Demo" and [p.id for p in s.pages] == ["p1"]


def test_parse_wiki_structure_sections():
    xml = """<wiki_structure>
<title>T</title>
<section id="s1"><title>S1</title><page_ref>p1</page_ref></section>
<section id="s2"><title>S2</title><section_ref>s1</section_ref></section>
<page id="p1"><title>A</title></page>
</wiki_structure>"""
    s = parse_wiki_structure(xml, comprehensive=True)
    assert [sec.id for sec in s.sections] == ["s1", "s2"]
    assert s.sections[0].pages == ["p1"]
    assert s.rootSections == ["s2"]  # s2 references s1, so only s2 is a root.


def test_parse_wiki_structure_invalid_raises():
    with pytest.raises(ValueError, match="No valid <wiki_structure> XML found"):
        parse_wiki_structure("no xml here", comprehensive=False)


def test_post_process_github_links():
    ctx = RepoUrlContext(type="github", repo_url="https://github.com/foo/bar", default_branch="main")
    content = "See [app.py]() and [Sources: app.py:10]()\n\n<!-- tail -->"
    out = post_process_wiki_content(content, ["app.py"], ctx)
    assert "https://github.com/foo/bar/blob/main/app.py" in out
    # Rewriting source links must not leave empty link targets behind.
    assert "Source: [" in out or "[Sources: " not in out
    assert "(]()" not in out


def test_post_process_details_block():
    ctx = RepoUrlContext(type="local", repo_url="", default_branch="main")
    out = post_process_wiki_content("plain text", ["app.py"], ctx)
    assert "plain text" in out  # Local repositories leave prose without URLs unchanged.
    assert "Relevant source files" in out or "[\u200bapp.py]" in out  # Details remain present.
    # Local repository links fall back to bare paths.
    assert "app.py" in out


def test_render_file_links_canonical_escaped():
    """Escape brackets in labels while preserving GitHub blob URLs."""
    ctx = RepoUrlContext(type="github", repo_url="https://github.com/foo/bar", default_branch="main")
    assert render_file_links(["docs/[x].md", "src/a.py"], ctx) == (
        "- [docs/\\[x\\].md](https://github.com/foo/bar/blob/main/docs/[x].md)\n"
        "- [src/a.py](https://github.com/foo/bar/blob/main/src/a.py)"
    )
    local_ctx = RepoUrlContext(type="local", repo_url="", default_branch="main")
    assert render_file_links(["src/a.py"], local_ctx) == "- [src/a.py](src/a.py)"


def test_locate_snippet():
    canvas = "line zero\nabc def\nghi\n"
    assert _locate_snippet(canvas, "abc def") == (2, 2)
    assert _locate_snippet(canvas, "def") == (2, 2)  # A partial match falls back to its first line.
    assert _locate_snippet(canvas, "zzz") is None


def test_repair_and_extract_json():
    assert _repair_json('{"a": "b",}') == '{"a": "b"}'
    assert _extract_json('```json\n{"x": 1}\n```') == {"x": 1}
    assert _extract_json('prefix {"a": [1, 2,]} suffix') == {"a": [1, 2]}
