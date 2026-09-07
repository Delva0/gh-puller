"""Check faithful log extraction, path matching, and leakage-free lexical indexing."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

import json
import sqlite3
import zlib
from hashlib import sha256

import pytest

from .case_probe import error_log, frames, references
from .lexical_probe import fuse, queries
from .text_index import build, search, verify_matches


def test_error_log_keeps_native_error_before_python_wrapper():
    body = ("## Environment\n```\nversion 1.2.3\n```\n```sh\nfile.cc:3: error: missing symbol\n"
            "Traceback (most recent call last):\nRuntimeError: build failed\n```\nAnswer outside the log")
    log = error_log(body)
    assert log["text"] == body[log["start"]:log["end"]]
    assert log["text"].startswith("file.cc:3: error: missing symbol")
    assert "Answer outside" not in log["text"]
    assert "version 1.2.3" not in log["text"]


def test_short_inner_fence_does_not_close_long_fence():
    body = "````text\n```\nTraceback (most recent call last):\nRuntimeError: test\n````"
    assert error_log(body)["text"].startswith("```\nTraceback")


@pytest.mark.parametrize("prefix", ["", "> ", "13:14:33 ERROR worker.py:10] "])
@pytest.mark.parametrize("ending", ["## Next section\nAnswer outside", "```sh\nother-command\n```", "</details>"])
def test_unfenced_log_keeps_physical_prefix_and_marks_weaker_boundary(prefix, ending):
    text = prefix + "Traceback (most recent call last):\nRuntimeError: test\n"
    body = "## Environment\nversion 1.2.3\n\n" + text + ending
    log = error_log(body)
    assert log["text"] == text == body[log["start"]:log["end"]]
    assert log["boundary"] == "unfenced_traceback_section"


def test_unfenced_log_can_end_at_input_end():
    body = "Traceback (most recent call last):\nRuntimeError: test"
    assert error_log(body) == {"start": 0, "end": len(body), "text": body,
                               "boundary": "unfenced_traceback_section"}


def test_indented_traceback_source_line_does_not_become_a_markdown_boundary():
    body = "Traceback (most recent call last):\n    # source comment\nRuntimeError: test\n## Next section"
    assert "RuntimeError: test" in error_log(body)["text"]


def test_unterminated_containing_fence_requires_explicit_handling():
    with pytest.raises(ValueError, match="closed fenced"):
        error_log("```\nTraceback (most recent call last):\nRuntimeError: test")


def test_repository_paths_are_not_project_name_heuristics():
    text = ('File "C:\\checkout\\pkg\\core.py", line 10, in run\n'
            'File "/opt/lib/external/core.py", line 1, in call\n'
            'File "<stdin>", line 1, in <module>\n')
    result = frames(text, {"pkg/core.py", "core.py"})
    assert result[0]["file"] == "pkg/core.py"
    assert result[1]["file"] is None
    assert result[1]["path_candidates"] == ["core.py"]
    assert result[2]["file"] is None


def test_escaped_newline_does_not_become_part_of_function_name():
    text = r'File "/repo/pkg/core.py", line 1, in run\n    fail()'
    assert frames(text, {"pkg/core.py"})[0]["name"] == "run"


@pytest.mark.parametrize("envelope", [{}, {"repository": "Example/Project"}, {"legacy_import": True}])
def test_closing_link_ownership_comes_from_archive_binding(envelope):
    value = {**envelope, "value": [{"number": 1, "repository": {"nameWithOwner": "example/project"}}]}
    with closing_source(value) as db:
        digest = db.execute("SELECT payload_digest FROM fact_observations").fetchone()[0]
        case = {"number": 1, "closing_pulls": [{"pull": 2, "observation": 10, "digest": digest, "pointer": "/value/0"}]}
        result = references(db, None, case, None, 10)
        assert result[0]["number"] == 2 and result[0]["pull_observation"]["coverage"] == "not_observed"


@pytest.mark.parametrize(("owner", "envelope"), [("foreign/project", {}),
                                                  ("example/project", {"repository": "foreign/project"})])
def test_closing_link_rejects_cross_repository_identity(owner, envelope):
    value = {**envelope, "value": [{"number": 1, "repository": {"nameWithOwner": owner}}]}
    with closing_source(value) as db:
        digest = db.execute("SELECT payload_digest FROM fact_observations").fetchone()[0]
        case = {"number": 1, "closing_pulls": [{"pull": 2, "observation": 10, "digest": digest, "pointer": "/value/0"}]}
        with pytest.raises(ValueError, match="selected issue"):
            references(db, None, case, None, 10)


def closing_source(value):
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE archive_meta (key TEXT, value TEXT);
        INSERT INTO archive_meta VALUES ('repository','example/project');
        CREATE TABLE fact_observations (id INTEGER, family TEXT, resource_number INTEGER, coverage TEXT,
                                       payload_digest TEXT, observed_until INTEGER, observed_from INTEGER);
        CREATE TABLE payload_blobs (digest TEXT, payload BLOB);
    """)
    raw = json.dumps(value).encode()
    digest = sha256(raw).hexdigest()
    db.execute("INSERT INTO payload_blobs VALUES (?,?)", (digest, zlib.compress(raw)))
    db.execute("INSERT INTO fact_observations VALUES (10,'pull-closing-issues',2,'complete',?,1,1)", (digest,))
    return db


@pytest.fixture
def source():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE selected (id INTEGER, family TEXT, resource_number INTEGER, "
                   "coverage TEXT, payload_digest TEXT)")
        db.execute("CREATE TABLE payload_blobs (digest TEXT, payload BLOB)")
        values = [
            (1, "issue", 1, "complete", {"title": "heldoutcanary", "body": "needle"}),
            (2, "issue-comments", 1, "complete", [{"body": "heldoutcommentcanary"}]),
            (3, "issue", 2, "complete", {"title": "Relevant", "body": "needle"}),
            (4, "issue-comments", 2, "complete", [{"body": "needle needle"}]),
            (5, "issue", 3, "partial", {"title": "partialcanary", "body": "needle"}),
        ]
        for oid, family, number, coverage, value in values:
            raw = json.dumps({"value": value}).encode()
            digest = sha256(raw).hexdigest()
            db.execute("INSERT INTO selected VALUES (?,?,?,?,?)", (oid, family, number, coverage, digest))
            db.execute("INSERT INTO payload_blobs VALUES (?,?)", (digest, zlib.compress(raw)))
        db.execute("CREATE VIEW fact_observations AS SELECT * FROM selected")
        yield db


@pytest.fixture
def corpus(source, tmp_path):
    path = tmp_path / "text.sqlite3"
    build(source, path, {1}, {"fixture": True})
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as index:
        yield index


def test_excluded_threads_do_not_affect_search_or_vocabulary(corpus):
    assert search(corpus, "heldoutcanary OR heldoutcommentcanary OR partialcanary") == []
    assert corpus.execute("SELECT doc FROM vocabulary WHERE term='heldoutcanary'").fetchone() is None
    assert corpus.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 2


def test_search_collapses_documents_into_threads(corpus):
    result = search(corpus, "needle", limit=1)
    assert len(result) == 1 and result[0]["number"] == 2
    assert result[0]["digest"] and result[0]["pointer"].startswith("/value")


def test_search_requires_positive_limit(corpus):
    with pytest.raises(ValueError, match="positive"):
        search(corpus, "needle", limit=0)


def test_build_does_not_overwrite_existing_file(tmp_path):
    path = tmp_path / "existing.sqlite3"
    path.touch()
    with pytest.raises(FileExistsError):
        build(None, path, set(), {})


def test_primary_message_omits_exception_namespace_and_reference_enumeration(corpus):
    text = "vendor.core.ValidationError: MissingWidget is not supported. Supported widgets: OtherWidget"
    result = queries(corpus, text, [], primary_messages=True)
    assert result["diagnostic_identifiers"] == '"MissingWidget"'


def test_fusion_is_independent_of_arm_order():
    assert fuse([[1, 2], [3, 2]]) == fuse([[3, 2], [1, 2]])


def test_text_verification_reads_canonical_payloads(source, corpus):
    report = verify_matches(source, corpus, search(corpus, "needle"), 5)
    assert report["source_documents_verified"] == 1
    assert report["index_quick_check"] == "ok"


def test_text_verification_rejects_conflicting_duplicate_identities(source, corpus):
    match = search(corpus, "needle")[0]
    with pytest.raises(ValueError, match="conflicting source identities"):
        verify_matches(source, corpus, [match, {**match, "number": 999}], 5)


def test_text_verification_rejects_invalid_source_digest(source, corpus):
    match = search(corpus, "needle")[0]
    with pytest.raises(ValueError, match="source identity"):
        verify_matches(source, corpus, [{**match, "digest": "0" * 64}], 5)
