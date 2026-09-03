"""把 GraphQL 原始节点映射为拉取器稳定消费的事实结构。

本模块不发起网络请求、选择配额或持久化数据。client 负责证明分页完整后调用
这些纯映射，并把未改写的 GraphQL 节点与映射结果一同交给 puller。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .errors import GitHubAPIError


def graphql_pull(payload: dict[str, Any], number: int) -> dict[str, Any]:
    try:
        repository = payload["data"]["repository"]
        pull = repository["pullRequest"]
    except (KeyError, TypeError) as exc:
        raise GitHubAPIError(f"GitHub returned incomplete pull request #{number}") from exc
    if not isinstance(pull, dict) or pull.get("number") != number:
        raise GitHubAPIError(f"GitHub returned no matching pull request #{number}")
    return pull


def graphql_parent(payload: dict[str, Any], number: int) -> dict[str, Any]:
    try:
        repository = payload["data"]["repository"]
        parent = repository["issueOrPullRequest"]
    except (KeyError, TypeError) as exc:
        raise GitHubAPIError(f"GitHub returned incomplete parent #{number}") from exc
    if not isinstance(parent, dict) or parent.get("number") != number:
        raise GitHubAPIError(f"GitHub returned no matching parent #{number}")
    return parent


def graphql_connection(
    pull: dict[str, Any],
    field: str,
    number: int,
) -> dict[str, Any]:
    connection = pull.get(field)
    if not isinstance(connection, dict):
        raise GitHubAPIError(f"pull #{number} has no {field} connection")
    nodes = connection.get("nodes")
    total = connection.get("totalCount")
    page_info = connection.get("pageInfo")
    if (
        not isinstance(nodes, list)
        or any(not isinstance(node, dict) for node in nodes)
        or type(total) is not int
        or total < 0
        or not isinstance(page_info, dict)
        or type(page_info.get("hasNextPage")) is not bool
    ):
        raise GitHubAPIError(f"pull #{number} has invalid {field} pagination")
    return connection


def node_connection(
    node: dict[str, Any],
    field: str,
    context: str,
) -> dict[str, Any]:
    connection = node.get(field)
    if not isinstance(connection, dict):
        raise GitHubAPIError(f"{context} has no {field} connection")
    nodes = connection.get("nodes")
    total = connection.get("totalCount")
    page_info = connection.get("pageInfo")
    if (
        not isinstance(nodes, list)
        or any(not isinstance(item, dict) for item in nodes)
        or type(total) is not int
        or total < 0
        or not isinstance(page_info, dict)
        or type(page_info.get("hasNextPage")) is not bool
    ):
        raise GitHubAPIError(f"{context} has invalid {field} pagination")
    return connection


def rest_pull_detail(
    pull: dict[str, Any],
    base_url: str,
    owner: str,
    repo: str,
) -> dict[str, Any]:
    number = pull["number"]
    api = f"{base_url}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    url = f"{api}/pulls/{number}"
    html_url = str(pull["url"])
    base = _rest_ref(pull, "base", owner)
    head = _rest_ref(pull, "head", owner)
    merge = pull.get("mergeCommit")
    state = str(pull["state"]).lower()
    if state == "merged":
        state = "closed"
    return {
        "id": _database_id(pull.get("fullDatabaseId")),
        "node_id": pull["id"],
        "url": url,
        "html_url": html_url,
        "diff_url": f"{html_url}.diff",
        "patch_url": f"{html_url}.patch",
        "issue_url": f"{api}/issues/{number}",
        "commits_url": f"{url}/commits",
        "review_comments_url": f"{url}/comments",
        "review_comment_url": f"{url}/comments{{/number}}",
        "comments_url": f"{api}/issues/{number}/comments",
        "statuses_url": f"{api}/statuses/{head['sha']}",
        "number": number,
        "state": state,
        "locked": pull["locked"],
        "title": pull["title"],
        "user": _rest_actor(pull.get("author")),
        "body": pull.get("body"),
        "created_at": pull["createdAt"],
        "updated_at": pull["updatedAt"],
        "closed_at": pull.get("closedAt"),
        "merged_at": pull.get("mergedAt"),
        "merge_commit_sha": merge.get("oid") if isinstance(merge, dict) else None,
        "draft": pull["isDraft"],
        "commits": pull["commits"]["totalCount"],
        "comments": pull["comments"]["totalCount"],
        "additions": pull["additions"],
        "deletions": pull["deletions"],
        "changed_files": pull["changedFiles"],
        "head": head,
        "base": base,
        "merged": pull["merged"],
        "mergeable": _mergeable(pull["mergeable"]),
        "rebaseable": pull.get("canBeRebased"),
        "mergeable_state": str(pull["mergeStateStatus"]).lower(),
        "maintainer_can_modify": pull.get("maintainerCanModify"),
        "author_association": pull["authorAssociation"],
        "_links": {
            "self": {"href": url},
            "html": {"href": html_url},
            "issue": {"href": f"{api}/issues/{number}"},
            "comments": {"href": f"{api}/issues/{number}/comments"},
            "review_comments": {"href": f"{url}/comments"},
            "review_comment": {"href": f"{url}/comments{{/number}}"},
            "commits": {"href": f"{url}/commits"},
            "statuses": {"href": f"{api}/statuses/{head['sha']}"},
        },
    }


def rest_review(
    review: dict[str, Any],
    base_url: str,
    owner: str,
    repo: str,
    number: int,
) -> dict[str, Any]:
    pull_url = (
        f"{base_url}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{number}"
    )
    html_url = review["url"]
    commit = review.get("commit")
    return {
        "id": _database_id(review.get("fullDatabaseId")),
        "node_id": review["id"],
        "user": _rest_actor(review.get("author")),
        "body": review["body"],
        "state": review["state"],
        "html_url": html_url,
        "pull_request_url": pull_url,
        "author_association": review["authorAssociation"],
        "_links": {
            "html": {"href": html_url},
            "pull_request": {"href": pull_url},
        },
        "submitted_at": review.get("submittedAt"),
        "commit_id": commit.get("oid") if isinstance(commit, dict) else None,
        "created_at": review["createdAt"],
        "updated_at": review["updatedAt"],
    }


def rest_commit(
    item: dict[str, Any],
    base_url: str,
    owner: str,
    repo: str,
    pull_number: int,
) -> dict[str, Any]:
    commit = item.get("commit")
    if not isinstance(commit, dict):
        raise GitHubAPIError(f"pull #{pull_number} has an invalid commit node")
    sha = commit.get("oid")
    parents = commit.get("parents")
    if not isinstance(sha, str) or not sha or not isinstance(parents, dict):
        raise GitHubAPIError(f"pull #{pull_number} has incomplete commit data")
    parent_nodes = parents.get("nodes")
    if (
        not isinstance(parent_nodes, list)
        or any(not isinstance(parent, dict) for parent in parent_nodes)
        or parents.get("totalCount") != len(parent_nodes)
        or parents.get("pageInfo", {}).get("hasNextPage") is not False
    ):
        raise GitHubAPIError(f"pull #{pull_number} has incomplete commit parents")
    api = f"{base_url}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    tree = commit.get("tree")
    tree_sha = tree.get("oid") if isinstance(tree, dict) else None
    return {
        "sha": sha,
        "node_id": commit["id"],
        "commit": {
            "author": _rest_signature(commit.get("author")),
            "committer": _rest_signature(commit.get("committer")),
            "message": commit["message"],
            "tree": {
                "sha": tree_sha,
                "url": None if tree_sha is None else f"{api}/git/trees/{tree_sha}",
            },
            "url": f"{api}/git/commits/{sha}",
        },
        "url": f"{api}/commits/{sha}",
        "html_url": commit["url"],
        "comments_url": f"{api}/commits/{sha}/comments",
        "author": _rest_actor(_nested_value(commit, "author", "user")),
        "committer": _rest_actor(_nested_value(commit, "committer", "user")),
        "parents": [
            {"sha": parent["oid"], "url": f"{api}/commits/{parent['oid']}"}
            for parent in parent_nodes
        ],
    }


def rest_review_comment(
    comment: dict[str, Any],
    base_url: str,
    owner: str,
    repo: str,
    pull_number: int,
) -> dict[str, Any]:
    comment_id = _database_id(comment.get("fullDatabaseId"))
    api = f"{base_url}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    pull_url = f"{api}/pulls/{pull_number}"
    review = comment.get("pullRequestReview")
    reply = comment.get("replyTo")
    commit = comment.get("commit")
    original_commit = comment.get("originalCommit")
    return {
        "url": f"{api}/pulls/comments/{comment_id}",
        "pull_request_review_id": (
            _optional_database_id(review.get("fullDatabaseId"))
            if isinstance(review, dict)
            else None
        ),
        "id": comment_id,
        "node_id": comment["id"],
        "diff_hunk": comment["diffHunk"],
        "path": comment["path"],
        "commit_id": commit.get("oid") if isinstance(commit, dict) else None,
        "original_commit_id": (
            original_commit.get("oid") if isinstance(original_commit, dict) else None
        ),
        "user": _rest_actor(comment.get("author")),
        "body": comment["body"],
        "created_at": comment["createdAt"],
        "updated_at": comment["updatedAt"],
        "html_url": comment["url"],
        "pull_request_url": pull_url,
        "author_association": comment["authorAssociation"],
        "in_reply_to_id": (
            _optional_database_id(reply.get("fullDatabaseId"))
            if isinstance(reply, dict)
            else None
        ),
        "line": comment.get("line"),
        "original_line": comment.get("originalLine"),
        "original_start_line": comment.get("originalStartLine"),
        "start_line": comment.get("startLine"),
        "subject_type": str(comment["subjectType"]).lower(),
        "reactions": {
            "url": f"{api}/pulls/comments/{comment_id}/reactions",
            "total_count": comment["reactions"]["totalCount"],
        },
    }


def rest_issue_comment(
    comment: dict[str, Any],
    base_url: str,
    owner: str,
    repo: str,
    parent_number: int,
) -> dict[str, Any]:
    comment_id = _database_id(comment.get("fullDatabaseId"))
    api = f"{base_url}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    return {
        "url": f"{api}/issues/comments/{comment_id}",
        "html_url": comment["url"],
        "issue_url": f"{api}/issues/{parent_number}",
        "id": comment_id,
        "node_id": comment["id"],
        "user": _rest_actor(comment.get("author")),
        "created_at": comment["createdAt"],
        "updated_at": comment["updatedAt"],
        "author_association": comment["authorAssociation"],
        "body": comment["body"],
        "body_text": comment["bodyText"],
        "body_html": comment["bodyHTML"],
        "reactions": {
            "url": f"{api}/issues/comments/{comment_id}/reactions",
            "total_count": comment["reactions"]["totalCount"],
        },
    }


def rest_reaction(reaction: dict[str, Any]) -> dict[str, Any]:
    content = {
        "THUMBS_UP": "+1",
        "THUMBS_DOWN": "-1",
        "LAUGH": "laugh",
        "HOORAY": "hooray",
        "CONFUSED": "confused",
        "HEART": "heart",
        "ROCKET": "rocket",
        "EYES": "eyes",
    }.get(reaction.get("content"))
    if content is None:
        raise GitHubAPIError("GitHub returned an invalid reaction content")
    return {
        "id": _database_id(reaction.get("databaseId")),
        "node_id": reaction["id"],
        "user": _rest_actor(reaction.get("user")),
        "content": content,
        "created_at": reaction["createdAt"],
    }


def _rest_signature(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "name": value.get("name"),
        "email": value.get("email"),
        "date": value.get("date"),
    }


def _nested_value(value: dict[str, Any], first: str, second: str) -> Any:
    nested = value.get(first)
    return nested.get(second) if isinstance(nested, dict) else None


def _rest_ref(pull: dict[str, Any], prefix: str, default_owner: str) -> dict[str, Any]:
    repository = pull.get(f"{prefix}Repository")
    name = pull.get(f"{prefix}RefName")
    owner = default_owner
    if isinstance(repository, dict):
        name_with_owner = repository.get("nameWithOwner")
        if isinstance(name_with_owner, str) and "/" in name_with_owner:
            owner = name_with_owner.split("/", 1)[0]
    return {
        "label": f"{owner}:{name}",
        "ref": name,
        "sha": pull[f"{prefix}RefOid"],
        "user": _rest_actor(repository.get("owner") if isinstance(repository, dict) else None),
        "repo": _rest_repository(repository),
    }


def _rest_repository(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    name_with_owner = value.get("nameWithOwner")
    return {
        "node_id": value.get("id"),
        "name": value.get("name"),
        "full_name": name_with_owner,
        "private": None,
        "owner": _rest_actor(value.get("owner")),
        "html_url": value.get("url"),
        "fork": value.get("isFork"),
    }


def _rest_actor(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    actor = {
        "login": value.get("login"),
        "node_id": value.get("id"),
        "avatar_url": value.get("avatarUrl"),
        "html_url": value.get("url"),
        "type": value.get("__typename"),
        "site_admin": value.get("isSiteAdmin", False),
    }
    database_id = value.get("databaseId")
    if database_id is not None:
        actor["id"] = _database_id(database_id)
    return actor


def _database_id(value: Any) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise GitHubAPIError("GitHub returned an invalid database identity")


def _optional_database_id(value: Any) -> int | None:
    return None if value is None else _database_id(value)


def _mergeable(value: Any) -> bool | None:
    if value == "MERGEABLE":
        return True
    if value == "CONFLICTING":
        return False
    if value == "UNKNOWN":
        return None
    raise GitHubAPIError("GitHub returned an invalid mergeable state")


def check_size(
    number: int,
    resource: str,
    expected: int,
    items: list[dict[str, Any]],
) -> None:
    if len(items) != expected:
        raise GitHubAPIError(
            f"pull #{number} advertised {expected} {resource}, got {len(items)}",
        )
