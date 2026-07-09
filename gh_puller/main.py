import asyncio
from datetime import datetime, timezone
from time import perf_counter

from gh_puller.gh import PER_PAGE, Item, epoch, fetch_issue_timeline, fetch_issues, merge_issue_timeline

# 二维平面：{epoch_id: {issue_id: {"item":item_id, "last_item":prev_item_id}}}
blocks: dict[int, dict[int, dict]] = {}
# item ID → Item 对象映射
items: dict[int, Item] = {}

_last_item_id: dict[int, int] = {}

T0 = perf_counter()


def _elapsed() -> str:
    return f"[{perf_counter() - T0:6.1f}s]"


async def write_updated_issues(
    owner: str,
    repo: str,
    issues: list[dict],
    before_epoch: int,
    after_epoch: int,
    concurrency: int = 20,  # gh rest api不太限制并发，主要限制次数5000/h
) -> None:
    """按时间块聚拢 issue 时间线事件，增量写入模块级 _blocks。

    区间 [after_epoch, before_epoch) 左闭右开。
    """
    t0 = perf_counter()
    print(
        f"{_elapsed()} write_updated_issues: 进入, issues={len(issues)}, "
        f"区间=[{after_epoch}, {before_epoch}), 并发={concurrency}",
        flush=True,
    )

    sem = asyncio.Semaphore(concurrency)

    async def fetch_one(iss: dict):
        t1 = perf_counter()
        async with sem:
            tl_raw = await fetch_issue_timeline(owner, repo, iss["number"])
        t2 = perf_counter()
        tl = merge_issue_timeline(iss, tl_raw)
        return iss, tl, t1, t2

    incoming: dict[int, dict[int, Item]] = {}
    tasks = [fetch_one(iss) for iss in issues]
    completed = 0
    for coro in asyncio.as_completed(tasks):
        iss, tl, t1, t2 = await coro
        prev_blocks = len(incoming)
        for e in tl:
            incoming.setdefault(e.epoch, {})[e.issue_id] = e
        new_blocks = len(incoming) - prev_blocks
        completed += 1
        print(
            f"{_elapsed()}   [{completed}/{len(issues)}] #{iss['number']}: "
            f"timeline={len(tl)}条, 新增{new_blocks}块, "
            f"拉取={t2 - t1:.1f}s",
            flush=True,
        )

    if not incoming:
        print(f"{_elapsed()} write_updated_issues: incoming 为空，跳过", flush=True)
        return

    # 区间过滤：[after_epoch, before_epoch)
    incoming = {k: v for k, v in incoming.items() if after_epoch <= k < before_epoch}
    print(
        f"{_elapsed()} write_updated_issues: 过滤后块数={len(incoming)}, "
        f"事件总数={sum(len(v) for v in incoming.values())}",
        flush=True,
    )

    for k, v in incoming.items():
        block = blocks.setdefault(k, {})
        for issue_id, item in v.items():
            block[issue_id] = {
                "item": item.id,
                "last_item": _last_item_id.get(issue_id, -1),
            }
            _last_item_id[issue_id] = item.id
            items[item.id] = item

    print(f"{_elapsed()} write_updated_issues: 写入完成, 总耗时={perf_counter() - t0:.1f}s", flush=True)


def lcs_len(a: list[int], b: list[int]) -> tuple[int, list[int], list[int]]:
    """返回 (最长公共子序列长度, a中匹配下标序列, b中匹配下标序列)。"""
    t0 = perf_counter()
    n, m = len(a), len(b)
    # 完整 DP 表，n 上限为 PER_PAGE，内存可控
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = dp[i - 1][j] if dp[i - 1][j] > dp[i][j - 1] else dp[i][j - 1]

    # 回溯匹配下标
    idxs_in_a: list[int] = []
    idxs_in_b: list[int] = []
    i, j = n, m
    while i > 0 and j > 0:
        if a[i - 1] == b[j - 1]:
            idxs_in_a.append(i - 1)
            idxs_in_b.append(j - 1)
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    idxs_in_a.reverse()
    idxs_in_b.reverse()

    elapsed = perf_counter() - t0
    print(f"{_elapsed()}   lcs_len: n={n}, m={m}, 结果={dp[n][m]}, 耗时={elapsed:.2f}s", flush=True)
    return dp[n][m], idxs_in_a, idxs_in_b


async def main_loop(
    owner: str,
    repo: str,
    max_per_epoch: int = 500,  # 尽量足够大，以保证对于任何epoch>0能触发"early break"
    num_epochs: int = 1,
    sleep_secs: int = 60,
) -> None:
    """双循环主逻辑：按 epoch 逐页拉取 issues，每次 append 后做 LCS 早期退出。"""
    prev_seq: list[dict] | None = None
    prev_before_epoch_idx: int | None = None
    epochs_done = 0

    while True:
        # 等待进入新的时间块
        print(f"{_elapsed()} 等待下一epoch... prev={prev_before_epoch_idx}", flush=True)
        while True:
            epoch_start_time = datetime.now(timezone.utc)
            before_epoch_idx = epoch(epoch_start_time)
            if prev_before_epoch_idx is not None and before_epoch_idx == prev_before_epoch_idx:
                print(f"{_elapsed()}   同一epoch {before_epoch_idx}, sleep({sleep_secs}s)...", flush=True)
                await asyncio.sleep(sleep_secs)
            else:
                break

        print(f"{_elapsed()} epoch {before_epoch_idx} 开始, epochs_done={epochs_done}", flush=True)

        t_epoch = perf_counter()
        seq: list[dict] = []
        page = 0
        while len(seq) < max_per_epoch:
            t_page = perf_counter()
            batch = await fetch_issues(owner, repo, page=page)
            t_fetch = perf_counter()
            seq.extend(batch)
            print(
                f"{_elapsed()}   page={page}: 拉取={t_fetch - t_page:.1f}s, "
                f"batch={len(batch)}, total={len(seq)}, "
                f"before_epoch_idx={before_epoch_idx}",
                flush=True,
            )

            lcs_broke = False
            idxs_in_b: list[int] = []
            if prev_seq is not None:
                cur_ids = [x["id"] for x in batch]
                prev_ids = [x["id"] for x in prev_seq]
                t_lcs = perf_counter()
                lcs, _, idxs_in_b = lcs_len(cur_ids, prev_ids)
                t_lcs_done = perf_counter()
                print(f"{_elapsed()}   page={page}: lcs={lcs}, lcs总耗时={t_lcs_done - t_lcs:.1f}s", flush=True)

                # "early break"：一旦出现这种情况，几乎可以认定前方都是没更新的issue了
                if lcs == PER_PAGE:
                    print(f"{_elapsed()}   -> lcs {lcs} == {PER_PAGE}, break 内层", flush=True)
                    lcs_broke = True
                    break

            page += 1

        print(
            f"{_elapsed()} epoch {before_epoch_idx}: 内层结束, seq={len(seq)}, "
            f"拉取总耗时={perf_counter() - t_epoch:.1f}s",
            flush=True,
        )

        t_write = perf_counter()
        await write_updated_issues(
            owner,
            repo,
            seq,
            after_epoch=prev_before_epoch_idx if prev_before_epoch_idx is not None else -1,
            before_epoch=before_epoch_idx,
        )
        print(
            f"{_elapsed()} epoch {before_epoch_idx}: write_updated_issues 总耗时={perf_counter() - t_write:.1f}s",
            flush=True,
        )
        print(
            f"{_elapsed()} epoch {before_epoch_idx}: 累计 {len(blocks)} blocks, "
            f"{sum(len(v) for v in blocks.values())} events",
            flush=True,
        )

        if lcs_broke:
            # seq 因 lcs == PER_PAGE 提前截断，用 prev_seq 尾部补齐完整序列
            seq_ids = {x["id"] for x in seq}
            tail = [x for x in prev_seq[idxs_in_b[0] :] if x["id"] not in seq_ids]
            prev_seq = seq + tail
        else:
            prev_seq = seq
        prev_before_epoch_idx = before_epoch_idx
        epochs_done += 1
        if epochs_done >= num_epochs:
            break

    # 最终输出
    print(f"\n{_elapsed()} === final: {len(blocks)} blocks ===", flush=True)
    for k in sorted(blocks):
        print(f"  block {k}: {len(blocks[k])} events", flush=True)


def main():
    asyncio.run(main_loop("vllm-project", "vllm-ascend"))


if __name__ == "__main__":
    main()
