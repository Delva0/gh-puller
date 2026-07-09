"""GitHub REST API 封装 — 裸 HTTP，函数式风格。"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone

import dotenv
import httpx

dotenv.load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
#  模块级 HTTP client 单例
# ═══════════════════════════════════════════════════════════════════════════════

_client = httpx.AsyncClient(
    base_url="https://api.github.com",
    headers={
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN', '')}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    },
    timeout=30,
)

PER_PAGE = 100  # GitHub API 每页上限


async def _paginate(url: str, **params) -> list[dict]:
    """翻页拉取全部结果，返回 JSON 列表。"""
    params.setdefault("per_page", PER_PAGE)
    items: list[dict] = []
    page = 1
    while True:
        params["page"] = page
        r = await _client.get(url, params=params)
        r.raise_for_status()
        body = r.json()
        if not body:
            break
        items.extend(body)
        if len(body) < params["per_page"]:
            break
        page += 1
    return items


# ═══════════════════════════════════════════════════════════════════════════════
#  epoch
# ═══════════════════════════════════════════════════════════════════════════════

_EPOCH_ORIGIN = datetime(1970, 1, 1, tzinfo=timezone.utc)


def epoch(t: datetime) -> int:
    """返回 t 所在的 epoch 编号。

    epoch 以 _EPOCH_ORIGIN 为原点，每 1 小时为一个 block，
    区间 [origin, origin+1h) 为 epoch 0，依此类推。
    """
    return int((t - _EPOCH_ORIGIN).total_seconds()) // 3600


# ═══════════════════════════════════════════════════════════════════════════════
#  issue 列表
# ═══════════════════════════════════════════════════════════════════════════════


async def fetch_issues(
    owner: str,
    repo: str,
    page: int = 0,
) -> list[dict]:
    """按 updated 倒序分页拉取 repo issues，返回原始 dict 列表。"""
    r = await _client.get(
        f"/repos/{owner}/{repo}/issues",
        params={
            "sort": "updated",
            "direction": "desc",
            "per_page": PER_PAGE,
            "page": page + 1,  # GitHub API page 从 1 开始
            "state": "all",
        },
    )
    r.raise_for_status()
    return r.json()


# ═══════════════════════════════════════════════════════════════════════════════
#  issue timeline（单个 issue 维度，issue + comments + 事件穿插）
# ═══════════════════════════════════════════════════════════════════════════════


async def fetch_issue_timeline(
    owner: str,
    repo: str,
    issue_number: int,
) -> list[dict]:
    """拉取 issue 的 timeline 事件，跳过无信息量的 subscribed。

    返回裸 event dict 列表，不含 issue 本体（需通过 merge_issue_timeline 融合）。
    """
    base = f"/repos/{owner}/{repo}/issues/{issue_number}"
    entries: list[dict] = []

    for ev in await _paginate(f"{base}/timeline"):
        event = ev.get("event")
        if event == "subscribed":
            continue
        entries.append(ev)

    entries.sort(key=lambda e: e.get("created_at", ""))
    return entries


@dataclass
class Item:
    """时间线条目，type 标识事件类型，value 为 GitHub API 原始 JSON。"""

    id: str
    type: str
    epoch: int
    issue_id: int
    value: dict


def merge_issue_timeline(issue: dict, timeline: list[dict]) -> list[Item]:
    """融合 issue 本体与 timeline 事件，统一包装为 Item，按 created_at 升序。"""
    entries: list[Item] = [
        Item(
            type="issue",
            value=issue,
            id=f"issue-{issue['number']}",
            epoch=epoch(datetime.fromisoformat(issue["created_at"])),
            issue_id=issue["id"],
        )
    ]
    for ev in timeline:
        entries.append(
            Item(
                type=ev.get("event", ""),
                value=ev,
                id=f"event-{ev['id']}",
                epoch=epoch(datetime.fromisoformat(ev["created_at"])),
                issue_id=issue["id"],
            )
        )
    entries.sort(key=lambda e: e.value.get("created_at", ""))
    return entries
