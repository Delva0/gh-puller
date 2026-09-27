# Missing REST enrichment for GraphQL review-comment commit IDs

Status: closed without implementation, 2026-09-17. No automatic REST fallback or
bulk refresh is required.

## Reproduced case

Repository: `vllm-project/vllm`; PR `32770`; review comment `2711788713`;
review `3686289418`.

The archived native GraphQL response contains both fields, but both are null:

```json
{"fullDatabaseId": "2711788713", "commit": null, "originalCommit": null}
```

This is present in `archives/vllm-v10.sqlite3` in both observations:

- `182111`, `pull-review-comments`: imported GraphQL capture.
- `572686`, `pull-review-threads`: native GraphQL capture on
  `2026-09-05T18:38:19.298909Z`–`2026-09-05T18:38:20.574838Z`.

A read-only request on 2026-09-17 to the
[GitHub REST review-comment endpoint](https://api.github.com/repos/vllm-project/vllm/pulls/comments/2711788713)
returned HTTP 200 with:

```json
{
  "id": 2711788713,
  "commit_id": "662919aeba36aab8d8c828bf4e057b6b88df0ac5",
  "original_commit_id": "662919aeba36aab8d8c828bf4e057b6b88df0ac5"
}
```

This establishes that REST can supply information absent from the archived
GraphQL result. It does not establish why GraphQL returned null, or what REST
returned at the earlier capture time.

## Collection path

- [client.py](../client.py): `_REVIEW_COMMENT_FRAGMENT` explicitly requests
  `commit { oid }` and `originalCommit { oid }`; `rest_review_comment` maps null
  objects to null REST-compatible fields. The field selection and conversion
  are not dropping an available SHA.
- [collector.py](../collector.py): `pull_review_comments` derives the collection
  directly from `pull-review-threads` when its coverage is `COMPLETE`, without
  REST enrichment. Complete pagination does not imply equal metadata coverage
  between GraphQL and REST.
- The L0 Wiki faithfully receives these null fields. It cannot identify the
  reviewed version from this record, so it retains the archived `diff_hunk`.
  Missing IDs may also leave gaps in commit-reference extraction and Git
  object collection.

## Resolution

A read-only archive scan found 42,865 review comments with at least one null
GraphQL commit field across 6,648 PRs. Limited refreshes then tested 24 PRs
spanning old and recent numbers and all three null patterns: `commit` only,
`originalCommit` only, and both fields. REST supplied the missing scalar IDs,
but every SHA recovered only from REST was unavailable from the managed store,
the PR ref, the known fork branch, and upstream branches and tags.

A control sample selected 46 SHAs returned directly by GraphQL across 30 PRs.
Forty-five Git objects were available. The remaining object stayed unavailable
after a targeted refresh even though GraphQL still returned its SHA. This is a
strong correlation, not an availability invariant: API identity and Git object
availability remain separate observations.

The REST-only SHA is therefore a historical identifier, not recoverable code.
No current L0 consumer requires that identifier without its Git object, while
the archived `diff_hunk` already preserves the available review context.
Automatically rereading REST would add one comment-list request for each
affected PR and trigger unproductive Git acquisition attempts.

The collector will continue deriving `pull-review-comments` from complete
GraphQL review threads. Non-null GraphQL commit IDs still produce structured
commit references and Git-object checks. Null IDs remain faithfully archived;
they do not trigger REST enrichment or a historical backfill. This keeps REST
traffic and collection policy minimal.

## Read-only reproduction

```bash
curl -fsS https://api.github.com/repos/vllm-project/vllm/pulls/comments/2711788713 \
  | jq '{id, commit_id, original_commit_id, pull_request_review_id}'
```
