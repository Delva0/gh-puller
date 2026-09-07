"""Check native change boundaries and batched Git framing on independent object fixtures."""
# ruff: noqa: INP001, S101 - Standalone research tests use pytest assertions.

import os
import subprocess

import pytest

from .change_git import boundary, compare, empty_tree, object_types, parse_batch, run


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "objects.git"
    subprocess.run(["git", "init", "--bare", str(path)], capture_output=True, check=True)
    return path


def commit(store, files, parents=()):
    entries = []
    for name, (mode, content) in sorted(files.items()):
        if mode == b"160000":
            oid, kind = content, b"commit"
        else:
            blob = run(store, "hash-object", "-w", "--stdin", data=content)
            blob.check_returncode()
            oid, kind = blob.stdout.strip(), b"blob"
        entries.append(mode + b" " + kind + b" " + oid + b"\t" + name + b"\0")
    tree = run(store, "mktree", "-z", data=b"".join(entries))
    tree.check_returncode()
    result = subprocess.run(
        ["git", f"--git-dir={store}", "commit-tree", tree.stdout.decode().strip(),
         *[arg for parent in parents for arg in ("-p", parent)]],
        input=b"fixture\n", capture_output=True, check=True,
        env=os.environ | {"GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
                          "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test",
                          "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"},
    )
    return result.stdout.decode().strip()


def resolve(store, kind, inputs):
    return boundary(store, kind, inputs, object_types(store, inputs))


def individual(store, before, after):
    result = run(store, "diff", "--name-status", "--no-renames", "--no-ext-diff", "--no-textconv",
                 "--ignore-submodules=none", "-z", before, after)
    result.check_returncode()
    fields = result.stdout.split(b"\0")[:-1]
    return [(fields[i + 1], fields[i].decode()) for i in range(0, len(fields), 2)]


def test_batch_matches_individual_diffs_for_empty_reverse_and_hostile_path_names(repository):
    names = [b"with space", b"with\ttab", b"with\nnewline", b"graphub-change:1\n", b"M", b"bad\xffutf8",
             "中文".encode(), b"0" * 40, b"-option"]
    a = commit(repository, dict.fromkeys(names, (b"100644", b"before")))
    b = commit(repository, dict.fromkeys(names, (b"100644", b"after")), (a,))
    pairs = [(a, b), (b, b), (b, a), (a, a)]
    results = compare(repository, pairs)
    for pair, result in zip(pairs, results, strict=True):
        assert result == {"status": "complete", "files": individual(repository, *pair)}
    assert {file for file, _ in results[0]["files"]} == set(names)


def test_root_landing_and_unrelated_proposal_use_native_empty_tree(repository):
    a = commit(repository, {b"a": (b"100644", b"a")})
    b = commit(repository, {b"b": (b"100644", b"b")})
    for kind, inputs in [("proposal", (a, b)), ("landing", (b,))]:
        result = resolve(repository, kind, inputs)
        assert result == {"status": "ready", "before": empty_tree(repository), "after": b,
                          "comparison_kind": "empty_tree"}
        assert compare(repository, [(result["before"], b)])[0]["files"] == [(b"b", "A")]


def test_merge_landing_uses_first_parent_not_proposal_merge_base(repository):
    a = commit(repository, {b"a": (b"100644", b"a")})
    b = commit(repository, {b"a": (b"100644", b"a"), b"b": (b"100644", b"b")}, (a,))
    c = commit(repository, {b"a": (b"100644", b"a"), b"c": (b"100644", b"c")}, (a,))
    merge = commit(repository, {b"a": (b"100644", b"a"), b"b": (b"100644", b"b"),
                                b"c": (b"100644", b"c")}, (b, c))
    assert resolve(repository, "landing", (merge,))["before"] == b
    assert resolve(repository, "proposal", (b, c))["before"] == a


def test_multiple_merge_bases_are_not_arbitrarily_chosen(repository):
    a = commit(repository, {b"a": (b"100644", b"a")})
    b = commit(repository, {b"b": (b"100644", b"b")}, (a,))
    c = commit(repository, {b"c": (b"100644", b"c")}, (a,))
    d = commit(repository, {b"d": (b"100644", b"d")}, (b, c))
    e = commit(repository, {b"e": (b"100644", b"e")}, (c, b))
    assert resolve(repository, "proposal", (d, e)) == {"status": "ambiguous_merge_base", "merge_bases": sorted([b, c])}


def test_missing_endpoint_and_failed_pair_do_not_become_empty_diffs(repository):
    a = commit(repository, {b"a": (b"100644", b"a")})
    missing = "1" * 40
    assert resolve(repository, "proposal", (a, missing))["status"] == "endpoint_unavailable"
    results = compare(repository, [(a, a), (missing, a), (a, a)])
    assert [r["status"] for r in results] == ["complete", "diff_unavailable", "complete"]
    assert "files" not in results[1]


def test_modes_renames_and_submodules_keep_tree_change_semantics(repository):
    a = commit(repository, {b"old": (b"100644", b"same"), b"exec": (b"100644", b"run"),
                             b"link": (b"100644", b"target")})
    b = commit(repository, {b"new": (b"100644", b"same"), b"exec": (b"100755", b"run"),
                             b"link": (b"120000", b"target"), b"sub": (b"160000", a.encode())}, (a,))
    result = compare(repository, [(a, b)])[0]
    assert dict(result["files"]) == {b"old": "D", b"new": "A", b"exec": "M", b"link": "T", b"sub": "A"}
    assert result["files"] == individual(repository, a, b)


@pytest.mark.parametrize("raw", [b"", b"graphub-change:0\nM\0file\0", b"graphub-change:0\nR100\0a\0b\0"])
def test_bad_batch_framing_is_rejected(raw):
    with pytest.raises(ValueError, match="input boundary"):
        parse_batch(raw, [("0" * 40, "1" * 40)])


def test_object_check_rejects_ref_expressions(repository):
    with pytest.raises(ValueError, match="exact Git"):
        object_types(repository, ["HEAD"])


@pytest.mark.parametrize(("mode", "kind", "expected"), [(b"040000", b"tree", "diff_unavailable"),
                                                        (b"100644", b"blob", "complete")])
def test_file_relations_require_tree_traversal_but_do_not_claim_blob_readability(repository, mode, kind, expected):
    missing = b"1" * 40
    tree = run(repository, "mktree", "--missing", data=mode + b" " + kind + b" " + missing + b"\tentry\n")
    tree.check_returncode()
    raw = (b"tree " + tree.stdout.strip() + b"\nauthor Fixture <fixture@example.test> 0 +0000\n"
           b"committer Fixture <fixture@example.test> 0 +0000\n\nfixture\n")
    head = run(repository, "hash-object", "-w", "-t", "commit", "--stdin", data=raw)
    head.check_returncode()
    result = compare(repository, [(empty_tree(repository), head.stdout.decode().strip())])[0]
    assert result["status"] == expected
    if expected == "complete":
        assert result["files"] == [(b"entry", "A")]
        assert object_types(repository, [missing.decode()])[missing.decode()] == "missing"


def test_sha256_git_object_ids_use_the_same_native_protocol(tmp_path):
    store = tmp_path / "sha256.git"
    initialized = run(store, "init", "--bare", "--object-format=sha256", str(store))
    initialized.check_returncode()
    a = commit(store, {b"old": (b"100644", b"old")})
    b = commit(store, {b"new": (b"100644", b"new")}, (a,))
    assert len(a) == len(b) == 64
    selected = resolve(store, "proposal", (a, b))
    assert selected["before"] == a and selected["after"] == b
    assert compare(store, [(a, b)])[0]["files"] == [(b"new", "A"), (b"old", "D")]
