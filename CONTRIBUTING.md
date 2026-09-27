# Contributing

This guide defines the repository-wide contribution workflow. Read the
[project overview](README.md) first, then consult the relevant component README
when one exists for runtime configuration and external dependencies.

## Development environment

The core repository requires a POSIX environment, Git, Python 3.13, and
[uv](https://docs.astral.sh/uv/). JavaScript changes also require Node.js and
the pnpm version declared in [`package.json`](package.json).

Install the locked dependencies from the repository root:

```bash
uv sync --frozen
pnpm install --frozen-lockfile
```

The root Python package and each Python application under `apps/` have an
independent `pyproject.toml` and `uv.lock`. Run application commands through
that application's directory rather than changing the root environment:

```bash
uv --directory apps/vllm-kb-adapter run pytest -q
```

pnpm owns all JavaScript workspace dependencies through the root
`pnpm-lock.yaml`. Address a single workspace by its package name:

```bash
pnpm --filter agent-monitor test
```

Change dependency declarations and their owning lockfile together. Generate
lockfile updates with uv or pnpm; do not edit lockfiles manually.

## Scope changes deliberately

`master` is the only long-lived branch. Create a short-lived branch named
`<type>/<kebab-case-topic>`, such as `feat/archive-reactions`,
`fix/graph-resume`, or `docs/contribution-workflow`, and delete it after merge.

Each branch and pull request should deliver one coherent outcome. A change may
cross packages when one contract requires coordinated producer, consumer,
test, or documentation updates. Keep unrelated cleanup and independent
protocol changes separate.

When changing behavior:

- Add or update the smallest tests that demonstrate the contract.
- Update user-facing documentation in the same change.
- Preserve existing package boundaries and follow the style of nearby code.
- Do not commit credentials, local configuration, caches, generated builds,
  runtime archives, or experiment output.

## Follow the code conventions

Python is linted with Ruff using the configuration in
[`pyproject.toml`](pyproject.toml). Keep code concise and avoid redundant
defensive handling when an existing contract already guarantees the input.

- Every Python module needs a docstring stating that module's responsibility.
- Add symbol docstrings only for contracts the signature cannot express. Core
  protocol APIs additionally document every parameter's semantics under
  `Args`; pure forwarding overrides inherit their base documentation.
- Write comments in English and use them to explain decisions or invariants,
  not straightforward syntax.
- Describe the current behavior rather than the history of a change.

For TypeScript and React, preserve the conventions of the workspace being
changed and run its tests and type checker.

## Verify the change

Run focused checks while developing. Representative commands are:

```bash
uv run pytest -q tests/github
uv --directory apps/gh-puller-mcp run pytest -q
pnpm --filter @gh-puller/ui test
pnpm --filter agent-monitor typecheck
pnpm --filter deepwiki-webui typecheck
```

Before submitting a code change, run the repository-wide deterministic checks:

```bash
uvx ruff check
pnpm test
git diff --check HEAD
```

Changes under `csrc/` or to the native integration must also build the native
helper. Set `CBM_ROOT` when `codebase-memory-mcp` is not in the default sibling
directory:

```bash
make -f Makefile.native native-helper
```

The default test commands exclude suites that need credentials, paid
providers, external executables, or other mutable state. Run relevant `real`
or `e2e` suites only after configuring the prerequisites documented by that
component. Mention any suite that was not run and why in the pull request.

Documentation-only changes do not require unrelated test suites, but their
commands, links, and examples must still be checked.

## Write consistent commits

Use this subject format:

```text
<type>(<scope>): <English summary>
```

The scope is optional for repository-wide changes. Use the package or subsystem
name when it adds useful context, for example `github`, `codebase`, `agent`,
`deepwiki`, `ui`, `mcp`, `vllm-kb`, or `native`.

| Type | Purpose |
| --- | --- |
| `feat` | Add or extend a capability. |
| `fix` | Correct a defect. |
| `refactor` | Restructure code without changing its contract. |
| `perf` | Improve performance without changing its contract. |
| `test` | Add or revise tests without changing production behavior. |
| `docs` | Change documentation only. |
| `chore` | Maintain dependencies, tooling, or repository infrastructure. |
| `research` | Record reproducible experimental work or evidence. |

Write the summary as a short imperative phrase without a trailing period:

```text
feat(github): archive review thread events
fix(codebase): preserve graph identities
docs: clarify development workflow
```

Keep commits atomic, including their directly related tests and documentation.
Use the commit body to explain motivation, constraints, and non-obvious tradeoffs.
Mark deliberately incompatible contracts with `!` and a `BREAKING CHANGE:`
footer. Preserve the message generated by `git revert` for reverts.

## Prepare the pull request

Before requesting review:

- Update the branch against the latest `master` and resolve conflicts intentionally.
- Explain the problem, the chosen approach, and any compatibility impact.
- Link the relevant issue or research evidence when one exists.
- List the exact checks run and their results.
- Include tests for behavior changes and documentation for user-visible changes.
- Confirm that dependency changes update only the appropriate lockfiles.
- Review the final diff for secrets, generated artifacts, debug code, and
  unrelated edits.

Keep follow-up work out of the pull request unless it is required for the
change to be correct.
