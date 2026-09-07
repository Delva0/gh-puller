"""Verify literal native hunk evidence on independent Git object fixtures."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

import pytest

from . import test_change_git
from .change_git import empty_tree, run
from .patch_evidence import hunks, lines, raw_files, read_comparison, verify_lines
from .test_change_git import commit, individual

repository = test_change_git.repository


def read(store, before, after, **limits):
    return read_comparison(store, before, after, individual(store, before, after), **limits)


def test_native_patch_preserves_byte_paths_newlines_and_literal_pathspecs(repository):
    names = [b"normal", b"with\nnewline", b"with\ttab", b"bad\xffutf8", b"-option", b":(glob)*", b"with space"]
    before = b"context\r\nold\n@@ header-looking data\nlast without newline"
    after = b"context\r\nnew\n@@ header-looking data\nlast without newline"
    a = commit(repository, dict.fromkeys(names, (b"100644", before)))
    b = commit(repository, dict.fromkeys(names, (b"100644", after)), (a,))
    result = read(repository, a, b)
    assert result["status"] == "complete"
    assert {item["file"] for item in result["files"]} == set(names)
    for item in result["files"]:
        assert item["status"] == "text"
        assert item["hunks"] == result["files"][0]["hunks"]
        assert item["hunks"][0]["lines"][-1]["text"] == b"last without newline"
    assert result["verified_line_sides"] == 8 * len(names)
    assert read(repository, a, b) == result


def test_native_add_delete_empty_and_multiple_hunks(repository):
    before = b"old\n" + b"unchanged\n" * 20 + b"tail\n"
    after = b"new\n" + b"unchanged\n" * 20 + b"other tail\n"
    a = commit(repository, {b"file": (b"100644", before), b"deleted": (b"100644", b"gone")})
    b = commit(repository, {b"file": (b"100644", after), b"added": (b"100644", b"fresh")}, (a,))
    result = read(repository, a, b)
    files = {item["file"]: item for item in result["files"]}
    assert len(files[b"file"]["hunks"]) == 2
    assert files[b"added"]["hunks"][0]["old_count"] == 0
    assert files[b"deleted"]["hunks"][0]["new_count"] == 0
    assert read(repository, a, a) == {"status": "complete", "files": [], "verified_line_sides": 0, "patch_bytes": 0}
    root = read(repository, empty_tree(repository), a)
    assert all(item["change"] == "A" for item in root["files"])


def test_native_type_mode_binary_and_gitlink_evidence_stays_distinct(repository):
    a = commit(repository, {b"mode": (b"100644", b"same"), b"type": (b"100644", b"target"),
                             b"binary": (b"100644", b"a\0b")})
    b = commit(repository, {b"mode": (b"100755", b"same"), b"type": (b"120000", b"target"),
                             b"binary": (b"100644", b"a\0c"), b"sub": (b"160000", a.encode())}, (a,))
    result = read(repository, a, b)
    files = {item["file"]: item for item in result["files"]}
    assert files[b"mode"]["hunks"] == []
    assert files[b"binary"]["status"] == "binary" and files[b"binary"]["hunks"] == []
    assert files[b"sub"]["status"] == "gitlink" and files[b"sub"]["hunks"] == []
    assert files[b"type"]["change"] == "T" and len(files[b"type"]["hunks"]) == 2
    assert result["verified_line_sides"] == 2


def test_patch_limits_precede_content_reads_and_missing_blob_is_not_empty(repository):
    a = commit(repository, {})
    b = commit(repository, {b"large": (b"100644", b"x" * 101)}, (a,))
    assert read(repository, a, b, max_files=0)["status"] == "file_limit"
    assert read(repository, a, b, max_blob_bytes=100)["status"] == "blob_limit"
    assert read_comparison(repository, "1" * 40, a, [])["status"] == "native_unavailable"


def test_missing_blob_with_readable_tree_has_explicit_patch_state(repository):
    tree = run(repository, "mktree", "--missing", data=b"100644 blob " + b"1" * 40 + b"\tfile\n")
    tree.check_returncode()
    result = read(repository, empty_tree(repository), tree.stdout.decode().strip())
    assert result["status"] == "blob_unavailable"
    assert "files" not in result


def test_patch_rejects_index_file_tampering_and_ambiguous_refs(repository):
    a = commit(repository, {})
    b = commit(repository, {b"file": (b"100644", b"content")}, (a,))
    with pytest.raises(ValueError, match="indexed comparison"):
        read_comparison(repository, a, b, [(b"other", "A")])
    with pytest.raises(ValueError, match="exact Git"):
        read_comparison(repository, "HEAD", b, [])


@pytest.mark.parametrize("raw", [b"@@ -1 +1 @@\n-old\n", b"@@ invalid\n", b"\\ No newline at end of file\n",
                                 b"@@ -1,0 +1 @@\n old\n", b"@@ -1 +1 @@\nmetadata\n"])
def test_malformed_hunk_framing_is_rejected(raw):
    with pytest.raises(ValueError, match=r"[Nn]ative"):
        hunks(raw)


def test_line_verification_is_independent_of_hunk_counts():
    parsed = hunks(b"@@ -1 +1 @@\n-old\n+new\n")
    assert verify_lines(parsed, b"old\n", b"new\n") == 2
    with pytest.raises(ValueError, match="blob coordinate"):
        verify_lines(parsed, b"unrelated\n", b"new\n")
    parsed[0]["lines"][0]["old_line"] = 0
    with pytest.raises(ValueError, match="blob coordinate"):
        verify_lines(parsed, b"old\n", b"new\n")
    assert lines(b"a\rb\nlast") == [b"a\rb\n", b"last"]


def test_raw_patch_metadata_rejects_unexpected_status_and_truncated_pairs():
    with pytest.raises(ValueError, match="metadata"):
        raw_files(b":100644 100644 a b R100\0old\0")
    with pytest.raises(ValueError, match=r"zip\(\)"):
        raw_files(b":100644 100644 a b M\0")


def test_line_coordinate_matches_do_not_hide_omitted_or_duplicate_changes():
    parsed = hunks(b"@@ -1 +1 @@\n-old\n+new\n")
    with pytest.raises(ValueError, match="complete blob difference"):
        verify_lines(parsed, b"old\nhidden old\n", b"new\nhidden new\n")
    with pytest.raises(ValueError, match="repeat"):
        verify_lines(parsed * 2, b"old\n", b"new\n")


def test_sha256_native_patch_uses_full_blob_identity(tmp_path):
    store = tmp_path / "sha256.git"
    run(store, "init", "--bare", "--object-format=sha256", str(store)).check_returncode()
    a = commit(store, {b"file": (b"100644", b"before\n")})
    b = commit(store, {b"file": (b"100644", b"after\n")}, (a,))
    result = read(store, a, b)
    assert result["status"] == "complete" and len(result["files"][0]["old"]) == 64
