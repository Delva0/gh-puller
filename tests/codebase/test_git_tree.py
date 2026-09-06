import os
import subprocess

from gh_puller.codebase.git_tree import materialize_full, materialize_incremental


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_incremental_tree_matches_git_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "keep.txt").write_text("one")
    (repo / "delete.txt").write_text("delete")
    os.symlink("keep.txt", repo / "link")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "one")
    first = git(repo, "rev-parse", "HEAD")

    tree = tmp_path / "tree"
    materialize_full(repo, first, tree)
    (repo / "keep.txt").write_text("two")
    (repo / "delete.txt").unlink()
    (repo / "new.txt").write_text("new")
    (repo / "link").unlink()
    os.symlink("new.txt", repo / "link")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "two")
    second = git(repo, "rev-parse", "HEAD")

    assert materialize_incremental(repo, first, second, tree) == 4
    assert (tree / "keep.txt").read_text() == "two"
    assert not (tree / "delete.txt").exists()
    assert (tree / "new.txt").read_text() == "new"
    assert (tree / "link").is_symlink()
    assert os.readlink(tree / "link") == "new.txt"
