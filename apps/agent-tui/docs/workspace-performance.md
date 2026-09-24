# Workspace verification and performance

Measured on 2026-09-24, Intel Core i7-10700 (8 cores / 16 logical CPUs), Linux
6.18.33.2-microsoft-standard-WSL2, glibc 2.39, Rust 1.98.1. Both builds use release
optimization, thin LTO and one codegen unit, on the same machine and 120×42 PTY.
CPU percentages use one logical core as 100%; RSS is peak sampled resident memory.

## Results

The delivered release met the **p95 ≤ 50 ms** target in all three tested configurations.
Every final run paired 100 sent keys with 100 single-input frames. Every monitored file,
including hidden tabs, consumed all 102,001 records after the append phase.
[Raw performance report](../../../playground/graphub-v4-reasoning-cost/runs/agent-tui-workspace-20260924/delivery-release/performance.json),
[previous single-file baseline](../../../playground/graphub-v4-reasoning-cost/runs/agent-tui-workspace-20260924/baseline/performance.json).

| Build | Monitored / visible | p95 ms | Max ms | Live CPU | Idle CPU | RSS MiB | Load s |
|---|---:|---:|---:|---:|---:|---:|---:|
| Previous single-file release | 1 / 1 | 18.82 | 20.22 | 27.7% | 3.0% | 249.6 | 2.86 |
| Workspace | 1 / 1 | 18.73 | 21.00 | 26.9% | 2.0% | 250.3 | 2.71 |
| Workspace | 8 / 1 | 18.86 | 21.41 | 173.0% | 8.0% | 1566.0 | 3.46 |
| Workspace | 8 / 4 | 19.23 | 22.29 | 204.8% | 9.0% | 1567.2 | 3.61 |

Each file starts with 100,000 synthetic canonical events from `tools/benchmark.py`.
The producer appends one live request and 200 delta events/s/file for ten seconds,
2,001 new records per file. The driver sends `t` at 10 Hz, changing the workspace theme
and exercising every visible pane. The four-pane run really displayed file IDs
`[0, 1, 3, 2]`; the other four files remained background tabs. The original single-file
baseline uses the existing benchmark's identical event fixture and input workload.
The baseline observer has its original single-file chrome; the workspace includes tabs
and a workspace hint row.

Final input latency uses shared `CLOCK_MONOTONIC` timestamps immediately before PTY input
and after terminal frame/OSC 8 flush. Per-sample send timestamps, frame timestamps and
latencies are preserved in each scenario's `samples.jsonl`, alongside the input trace.
The baseline used the original wall-clock timestamp pairing. These numbers include PTY
input delivery and terminal output, but exclude a GUI terminal emulator/compositor.
They measure this fixture and theme input, not the worst case for arbitrary large records,
full-history search or mass expansion. One run per configuration is not evidence of a
small speedup over the baseline. Eight retained contexts and card models use about
1.53 GiB here; hiding a tab preserves its model and reading state.

## Rendering and behavior checks

Only each pane's active tab renders; hidden views receive model updates without requesting
Markdown layout. Clean visible panes reuse cached cells, invalidated by file changes,
reading actions, geometry or theme. Focus changes and global overlays do not re-render
stable cards. Tests count actual view renders and inspect document queues;
this behavior is application-owned, while hypertile owns geometry and ratios.
See [render/cache tests](../src/workspace.rs#L1643) and
[hidden-layout test](../src/ui.rs#L824).

- **63 Rust tests passed** (45 unit, 18 reconstruction); the original 43 single-file tests
  remain included. `cargo clippy --locked --all-targets -- -D warnings` and `cargo fmt --check`
  passed. [Test log](../../../playground/graphub-v4-reasoning-cost/runs/agent-tui-workspace-20260924/delivery-tests.log), [Clippy log](../../../playground/graphub-v4-reasoning-cost/runs/agent-tui-workspace-20260924/delivery-clippy.log).
- Source normalization, overlapping roots, missing directories/files, directory symlink
  exclusion and cancellable full discovery queues are covered in
  [sources.rs](../src/sources.rs#L185). Existing reader coverage retains partial UTF-8 and
  the held-handle atomic-replacement contract; [reader.rs](../src/reader.rs#L242) also checks
  shutdown with a full update queue.
- Hidden-file updates, malformed-file isolation and independent end state are checked in
  [session.rs](../src/session.rs#L98). File command/shortcut isolation is checked in
  [ui.rs](../src/ui.rs#L853).
- Drag reorder, preview/Esc, center merge, edge split, divider drag, keyboard resize,
  maximize/restore, small terminals, selection capture and wide-character link coordinates
  are checked in [workspace.rs](../src/workspace.rs#L1437). Character reading anchors across
  temporary zoom-to-fit and unwrapped horizontal navigation are checked in
  [ui.rs](../src/ui.rs#L785).
- Offline adapter verification: **16 Python adapter tests**, **14 recorded sessions / 268
  prefixes**, all Rust folds and compact replays matched Python `fold_state()`.
  [Parity report](../../../playground/graphub-v4-reasoning-cost/runs/agent-tui-workspace-20260924/parity/parity.json). Graphub observer integration: **12 tests passed**
  in `tests/v5/test_tui_observation.py`. No paid model was called.
- [Four-pane screenshot](../assets/workspace.png) is a rasterization of actual TestBackend
  cells, with distinct Completed, Failed, Cancelled and Running file footers. Mouse/keyboard
  geometry was checked through injected Ratatui/crossterm events; OS clipboard permissions
  and a GUI terminal's native Ctrl-click handling were not exercised by this harness.

## Source and retained records

Baseline source checkout: `0df7361f06f6def7bfb84bf64ab5ab27856dbb4b` (the observer was unchanged at
that checkout). Baseline binary SHA-256:
`e4511a893b08be09670b56ea9b15345714d59f0e30fdcc842b4eda4c06b48cf0`.
Delivered binary SHA-256:
`0e49981d66abb694f7dbc3167210eb1ca8d022c46d9f7613a2a55b6f93602eef`.
Exact source-file hashes and toolchain identity are in the
[source manifest](../../../playground/graphub-v4-reasoning-cost/runs/agent-tui-workspace-20260924/delivery-source.json). The release build log is
[retained here](../../../playground/graphub-v4-reasoning-cost/runs/agent-tui-workspace-20260924/delivery-build.log).

The host has no default `cc`. The successful baseline and delivery builds used the same
existing GCC 14 sysroot through the local `runs/agent-tui-workspace-20260924/cc` wrapper, passed with
`CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER`; no compiler configuration is embedded in
this application. Earlier attempts with missing `cc` and without the GCC sysroot failed
before a usable baseline was built.

All raw fixtures, traces, binaries and logs stay in the experiment worktree under
`runs/agent-tui-workspace-20260924/`, outside Git. Prior measurement directories (`workspace-release`, `final-release`,
`optimized-release`) are retained, including one intermediate wall-clock run with a
3,393.20 ms maximum and 18.29 ms p95. Its cause is unresolved: that older helper did not
persist send timestamps, so the individual pair cannot be reconstructed from its trace.
The final helper records both clocks and per-sample pairs and measures with the monotonic
clock. Earlier data were not replaced. No shared archive or historical session log was
modified. Canonical events, FileSink, adapters and agent control are outside this change.

## Reproduction

From the gh-puller root with a native C linker and Rust installed:

```bash
cargo build --release --locked --manifest-path apps/agent-tui/Cargo.toml
cargo test --locked --manifest-path apps/agent-tui/Cargo.toml
cargo clippy --locked --all-targets --manifest-path apps/agent-tui/Cargo.toml -- -D warnings
uv run --frozen python apps/agent-tui/tools/workspace_benchmark.py \
  --binary apps/agent-tui/target/release/agent-tui --output /path/to/new-run
agent-tui --workspace-snapshot cells.json first.jsonl second.jsonl third.jsonl fourth.jsonl
uv run --frozen python apps/agent-tui/tools/screenshot.py cells.json workspace.png
```

The benchmark output directory must be new. In the independent Graphub worktree, run UV
from its root, use `../../apps/agent-tui/...`, and import `graphub` before the adapter parity
helper so the adjacent `gh_puller` package follows that worktree's initialization contract.
