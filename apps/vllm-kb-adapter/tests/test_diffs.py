"""Unified-diff parsing and bounded Cypher construction tests."""

from vllm_kb_adapter.diffs import (
    ChangedFile,
    LineRange,
    build_impact_query,
    build_seed_query,
    parse_unified_diff,
)


def test_parse_modified_deleted_and_new_files() -> None:
    diff = """diff --git a/vllm/a.py b/vllm/a.py
--- a/vllm/a.py
+++ b/vllm/a.py
@@ -10,3 +10,4 @@
 context
diff --git a/vllm/deleted.py b/vllm/deleted.py
--- a/vllm/deleted.py
+++ /dev/null
@@ -4,2 +0,0 @@
-gone
diff --git a/vllm/new.py b/vllm/new.py
--- /dev/null
+++ b/vllm/new.py
@@ -0,0 +1,2 @@
+new
"""

    assert parse_unified_diff(diff) == (
        ChangedFile("vllm/a.py", (LineRange(10, 12),)),
        ChangedFile("vllm/deleted.py", (LineRange(4, 5),)),
        ChangedFile("vllm/new.py", (LineRange(1, 1),)),
    )


def test_parse_header_only_diff_and_reject_parent_path() -> None:
    diff = """diff --git a/x.py b/x.py
+line
diff --git a/../secret b/../secret
+line
"""

    assert parse_unified_diff(diff) == (ChangedFile("x.py", ()),)


def test_parse_rename_uses_base_path_and_merges_ranges() -> None:
    diff = """diff --git a/old.py b/new.py
similarity index 90%
rename from old.py
rename to new.py
--- a/old.py
+++ b/new.py
@@ -2,2 +2,2 @@
@@ -4 +4 @@
"""

    assert parse_unified_diff(diff) == (ChangedFile("old.py", (LineRange(2, 4),)),)


def test_queries_select_seeds_before_fixed_hop_traversal() -> None:
    changes = (ChangedFile("vllm/a.py", (LineRange(10, 12),)),)

    seed_query = build_seed_query(changes)
    impact_query = build_impact_query(changes, direction="both", hop=2)

    assert seed_query.startswith('MATCH (seed) WHERE ((seed.file_path = "vllm/a.py"')
    assert "seed.start_line <= 12 AND seed.end_line >= 10" in seed_query
    assert "NOT seed:Module" in seed_query
    assert "seed.file_path AS file" in seed_query
    assert "(impact)-[:CALLS*2..2]->(seed)" in impact_query
    assert "(seed)-[:CALLS*2..2]->(impact)" in impact_query
    assert " UNION " in impact_query
