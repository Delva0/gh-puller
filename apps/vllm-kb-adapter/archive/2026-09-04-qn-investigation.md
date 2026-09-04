# `trace_path` qn compatibility investigation

Date: 2026-09-04

## Conclusion

`codebase-memory-mcp` v0.10.8 accepts both a short symbol name and the complete
qualified name returned by `search_graph`:

```text
<project>.<module>.<symbol>
```

The `<project>.` prefix is part of CBM's canonical qualified name. Removing it
produces a module-relative name that CBM does not resolve. The vllm-kb adapter
must therefore forward complete qns unchanged.

No CBM change and no adapter-side prefix stripping are required.

## Source evidence

CBM commit
[`9818730`](https://github.com/DeusData/codebase-memory-mcp/commit/9818730056f14041498a2d51b9cf0c77317cfed6)
added the `trace_path` qualified-name fallback and is included in v0.10.8.
`handle_trace_call_path` first searches the bare `name` column. If that misses,
it calls `cbm_store_find_node_by_qn` with the requested project and the complete
input qn. The store query requires both fields to match exactly:

```sql
WHERE project = ?1 AND qualified_name = ?2
```

`apps/gh-puller-mcp` delegates `trace_path` arguments unchanged to the CBM CLI.
The adapter replaces the public logical project with `snapshot.index_name`,
which is also the name used when the snapshot is prebuilt, and otherwise leaves
`function_name` untouched.

## Runtime verification

The installed `codebase-memory-mcp 0.10.8` was tested against the indexed
`home-delva-projects-gh-puller` project:

| `function_name` | Result |
|---|---|
| Complete qn beginning with `home-delva-projects-gh-puller.` | Success |
| The same qn with the project prefix removed | `function not found` |
| Short name `index_repository` | Success |

This verifies that the complete search result is the valid precise identifier;
the prefix-stripped form is not.

## Integration invariant

For an exact qn lookup, these values must describe the same snapshot:

```text
trace_path.arguments.project == snapshot.index_name
trace_path.arguments.function_name starts with snapshot.index_name + "."
```

If a deployed environment rejects a complete qn, check the actual CBM binary
version, the post-adapter project value, the qn returned by `search_graph`, and
the selected snapshot version. Treat that failure as an environment or routing
mismatch rather than stripping the qn prefix.

Short-name uniqueness prechecks remain useful for genuinely short input. A
complete qn should stay complete so it retains its disambiguation and remains
stable across cursor pages.
