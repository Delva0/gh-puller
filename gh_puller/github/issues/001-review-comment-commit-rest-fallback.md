# Missing REST enrichment for GraphQL review-comment commit IDs

Status: recorded only, 2026-09-17. No collector or archive changes made.

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

## Follow-up scope

Consider targeted REST enrichment for review comments with missing commit IDs,
joined by the original comment ID. Preserve the native GraphQL response and the
REST provenance separately; do not overwrite raw observations or infer the
reviewed version from a PR head, merge commit or timestamp.

Existing affected archives need an explicit backfill strategy, including
derived commit references and Git objects where applicable. A present SHA does
not itself guarantee that the referenced Git object remains retrievable.

Deterministic regression cases should cover GraphQL-null/REST-present fields,
both sources returning null, conflicting non-null values, failed enrichment,
and unchanged review-thread/reply identities. No LLM is needed.

## Read-only reproduction

```bash
curl -fsS https://api.github.com/repos/vllm-project/vllm/pulls/comments/2711788713 \
  | jq '{id, commit_id, original_commit_id, pull_request_review_id}'
```
