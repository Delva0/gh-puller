## Agent behavior

- **Tolerate speech-to-text errors**: The user dictates through speech recognition. Infer
  meaning from the overall intent and context, ignoring spelling mistakes, homophones, and
  filler words.
- **Discuss before writing**: After receiving a task, briefly state your understanding and
  plan. Wait for confirmation only when a key decision is required, the scope is ambiguous,
  or an operation is irreversible. Continue immediately when the user explicitly requests
  execution.
- **Run Python through UV**: Use `uv` for every Python command.
- **Preserve code style and consistency**: Follow the concise, zero-redundancy style of
  research or competitive-programming code, minimizing defensive programming where
  practical. Write all comments in English and follow the style of the existing core code
  exactly.
- **Verify changes**: Select appropriate tools and checks for every change.

## Python documentation and comment rules

Mechanical requirements are enforced by `ruff` (`uvx ruff check` must pass, using
`select=ALL` with targeted `ignore` entries). The following rules cover qualities that
linting cannot determine.

- **Module docstrings are required** (enforced by lint):
  - **Content**: Begin with one sentence that states the module's responsibility. Add
    architecture-level boundaries, dependency contracts (what it depends on and what
    depends on it), internal organization (the roles of modules or files), and public
    exports only when needed.
  - **Scope**: Do not list functions or classes, symbol-level details, or implementation
    counts such as the number of routes or generators. Put symbol details in the symbol's
    own docstring.
  - **File boundary**: Each file documents only itself. Every new file must provide its own
    module docstring; do not rewrite an existing file merely to explain a new file.
- **Class and function docstrings depend on information content, not visibility**:
  - **General rule**: Document only information the signature cannot express, including
    non-obvious parameter semantics, failure or fallback behavior, timing or concurrency
    invariants, and cross-file contracts. Add `Returns` when return semantics are
    non-obvious. Add `Raises` only when callers need to catch the failure.
  - **Core protocol API exception**: Functions and public methods on a module or package
    contract surface must provide `Args` entries for every parameter, including independent
    parameters, dict or config construction, defaults, and contract constraints. Describe
    semantics, not types. When the signature already shows a default value, document only
    its contractual meaning.
  - **Pure forwarding overrides**: Do not add a separate docstring. Inherit the contract
    and `Args` documentation from the base class.
- **Keep one source of truth for shared contracts and core concepts**: Anchor each
  definition in the docstring of the package or module that owns the concept. Consumers
  should use the concept's name or refer to that source without citing line numbers. When
  adding or revising a concept description, edit only its anchor.
- **Inline comments explain only why**: Use them for traps, invariants, design tradeoffs,
  and extension points, never to explain Python syntax or straightforward APIs. Explain
  intentional anomalies such as broad exception handling, lazy imports, and lint
  suppressions in place. Every `TODO` must name an action. In large files, use
  `# --- Section ---` banners whose titles match their contents.
- **Describe the current state, not change history**: README files and docstrings explain
  only the current design and usage. Put change history in commit messages and changelogs.
- **Use diagrams for complex logic**: When a diagram communicates a relationship or flow
  more clearly than linear prose, use one.

## Repository map

- **Do not inspect**: `archive/`
- **Main Python package**: `gh_puller/`
- **Experimental workspace**: `playground/`
- **Shared UI packages**: `ui/`
