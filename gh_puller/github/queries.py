"""定义 GitHub GraphQL 原子读取操作的静态查询文档。

本模块不执行网络请求或解释响应；游标闭合、事实映射与配额选择由 client 负责。
查询只选择归档承诺的字段，GraphQL 来源原文由调用操作随稳定事实一同返回。
"""

REPOSITORY_ITEM_COUNT = """
query RepositoryItemCount($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    issues(states: [OPEN, CLOSED]) { totalCount }
    pullRequests(states: [OPEN, CLOSED, MERGED]) { totalCount }
  }
}
"""

PULL_REQUEST_DETAIL = """
query PullRequestDetail($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id
      fullDatabaseId
      number
      url
      state
      locked
      title
      body
      authorAssociation
      createdAt
      updatedAt
      closedAt
      mergedAt
      isDraft
      merged
      mergeable
      mergeStateStatus
      canBeRebased
      maintainerCanModify
      additions
      deletions
      changedFiles
      baseRefName
      baseRefOid
      baseRepository { id name nameWithOwner url isFork owner { login avatarUrl url } }
      headRefName
      headRefOid
      headRepository { id name nameWithOwner url isFork owner { login avatarUrl url } }
      mergeCommit { oid }
      author {
        __typename
        login
        avatarUrl
        url
        ... on User { id databaseId name email isSiteAdmin }
        ... on Organization { id databaseId name email }
        ... on Bot { id databaseId }
        ... on Mannequin { id databaseId email }
      }
      comments { totalCount }
      commits { totalCount }
    }
  }
}
"""

PULL_REVIEWS = """
query PullReviews($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number
      reviews(first: 100, after: $cursor) {
        totalCount
        nodes {
          id
          fullDatabaseId
          body
          state
          authorAssociation
          submittedAt
          createdAt
          updatedAt
          url
          commit { oid }
          author {
            __typename
            login
            avatarUrl
            url
            ... on User { id databaseId name email isSiteAdmin }
            ... on Organization { id databaseId name email }
            ... on Bot { id databaseId }
            ... on Mannequin { id databaseId email }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

PULL_COMMITS = """
query PullCommits($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number
      commits(first: 100, after: $cursor) {
        totalCount
        nodes {
          id
          url
          commit {
            id
            oid
            url
            message
            authoredDate
            committedDate
            additions
            deletions
            changedFilesIfAvailable
            author { name email date user { id databaseId login avatarUrl url isSiteAdmin } }
            committer { name email date user { id databaseId login avatarUrl url isSiteAdmin } }
            tree { oid }
            parents(first: 100) {
              totalCount
              nodes { oid }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

_REVIEW_COMMENT_FRAGMENT = """
fragment ReviewCommentFields on PullRequestReviewComment {
  id
  fullDatabaseId
  body
  authorAssociation
  createdAt
  updatedAt
  url
  diffHunk
  path
  line
  originalLine
  originalStartLine
  startLine
  outdated
  subjectType
  state
  commit { oid }
  originalCommit { oid }
  replyTo { fullDatabaseId }
  pullRequestReview { fullDatabaseId }
  reactions { totalCount }
  author {
    __typename
    login
    avatarUrl
    url
    ... on User { id databaseId name email isSiteAdmin }
    ... on Organization { id databaseId name email }
    ... on Bot { id databaseId }
    ... on Mannequin { id databaseId email }
  }
}
"""

PULL_REVIEW_COMMENTS = """
query PullReviewComments($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number
      reviewThreads(first: 100, after: $cursor) {
        totalCount
        nodes {
          id
          comments(first: 100) {
            totalCount
            nodes { ...ReviewCommentFields }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""" + _REVIEW_COMMENT_FRAGMENT

REVIEW_THREAD_COMMENTS = """
query ReviewThreadComments($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      id
      comments(first: 100, after: $cursor) {
        totalCount
        nodes { ...ReviewCommentFields }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""" + _REVIEW_COMMENT_FRAGMENT

_ISSUE_COMMENT_FRAGMENT = """
fragment IssueCommentFields on IssueComment {
  id
  fullDatabaseId
  body
  bodyHTML
  bodyText
  authorAssociation
  createdAt
  updatedAt
  url
  reactions { totalCount }
  author {
    __typename
    login
    avatarUrl
    url
    ... on User { id databaseId name email isSiteAdmin }
    ... on Organization { id databaseId name email }
    ... on Bot { id databaseId }
    ... on Mannequin { id databaseId email }
  }
}
"""

ISSUE_COMMENTS = """
query IssueComments($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    issueOrPullRequest(number: $number) {
      __typename
      ... on Issue {
        number
        comments(first: 100, after: $cursor) {
          totalCount
          nodes { ...IssueCommentFields }
          pageInfo { hasNextPage endCursor }
        }
      }
      ... on PullRequest {
        number
        comments(first: 100, after: $cursor) {
          totalCount
          nodes { ...IssueCommentFields }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }
}
""" + _ISSUE_COMMENT_FRAGMENT

REACTIONS = """
query Reactions($id: ID!, $cursor: String) {
  node(id: $id) {
    id
    ... on Reactable {
      reactions(first: 100, after: $cursor) {
        totalCount
        nodes {
          id
          databaseId
          content
          createdAt
          user { __typename id databaseId login avatarUrl url isSiteAdmin }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
