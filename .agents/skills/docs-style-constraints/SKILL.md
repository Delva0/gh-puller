---
name: docs-style-constraints
description: Create or substantially revise maintainable, repository-grounded Markdown documentation under docs/ with durable source-path citations, verified commands and links, and diagrams or tables only when they improve understanding. Use for full pages or significant edits, not typo-only changes or files outside docs/.
---

# Project Documentation

Create Markdown under `docs/` that is ready to commit as written and remains useful as the repository evolves. Treat the file written to disk as the final artifact: never rely on DeepWiki prompt substitution, citation rewriting, or any other post-processing step.

Follow the user's requested scope and the applicable `AGENTS.md`. Do not modify implementation code merely to make it agree with the documentation unless the user separately requests that change.

## Scope

- Apply the full contract to new pages and substantial revisions.
- For a focused edit, preserve the existing page structure and update the affected claims, citations, source inventory, links, and examples without rewriting unrelated sections.
- Use the requested document path and language. If either is unspecified, infer a clear `docs/` path and match the predominant language of related project documentation.

## Research Before Writing

1. Read the applicable `AGENTS.md`, the existing target page when present, and closely related pages under `docs/`.
2. Trace the topic through its canonical implementation, public entry points, configuration, tests, and operational surfaces. Read source contents rather than relying on filenames or search snippets.
3. Choose the smallest sufficient set of authoritative source packages or files. Prefer one cohesive package boundary that summarizes the evidence; do not split it into child packages or files merely because several internals were inspected. Narrow the path only when the broader package would obscure ownership. Name an individual file only when the document discusses that file, one of its symbols, or a contract that has no useful package-level boundary. There is no minimum source count. Every selected path must materially support the document.
4. Separate repository behavior from external contracts. Use current primary documentation for an external API or tool when it is necessary to explain that contract, and cite it with a normal working URL.
5. Omit unsupported claims. If an unresolved fact is essential, report the uncertainty instead of filling it with conventional behavior or inference.

## Editorial Focus

Write a technical article, not an implementation inventory. Its value comes from a clear engineering point of view: explain the few ideas that let a reader understand, use, or reason about the subject.

- Establish one central question or design claim, then keep only sections that advance it.
- Prefer a small number of stable concepts, boundaries, or trade-offs over exhaustive coverage.
- Do not enumerate complete event taxonomies, protocol fields, environment variables, internal modules, or test cases unless the page is explicitly intended as a reference.
- Select one representative flow, example, table, or diagram when it carries the argument. Do not present several views of the same design.
- Keep introductions short, avoid recap conclusions, and remove details that a reader can discover directly from the cited source without losing the article's argument.
- Never increase length merely to demonstrate research completeness. Source research should improve the precision of the prose, not appear wholesale in the prose.

## Full-Page Structure

For a new page or substantial rewrite, start with this source inventory. Do not place a preface before it.

```markdown
<details>
<summary>Relevant sources</summary>

The following source packages were used as context for this document:

- [gh_puller/deepwiki/](../gh_puller/deepwiki/)
- [tests/](../tests/)
</details>

# Page Title
```

Replace the example entries with all and only the repository packages or files materially used for the page.

- Keep the visible label as the full repository-relative path; never shorten it to a bare package or filename.
- Calculate each link target relative to the documentation file. The examples above are correct for a page directly under `docs/`; adjust the leading `../` segments for nested pages.
- Prefer a package directory when it sufficiently identifies the owner of the documented behavior. Link to a file only when the prose explicitly names that file or a symbol it owns, or when a broader package would make the evidence ambiguous.
- Put the H1 title immediately after the inventory.
- Follow with a concise introduction explaining the feature's purpose, scope, and place in the project.
- Organize the body by the topic's actual concepts using H2 and H3 headings. Do not force a fixed section list.
- Add a conclusion only when it contributes information instead of repeating the introduction.

Do not add or rebuild the full-page wrapper for a typo-only or similarly narrow edit.

## Durable Source Citations

Ground implementation-specific claims, commands, diagrams, tables, and examples in the repository sources. Place a consolidated citation after the paragraph or block it supports:

```markdown
Sources: [gh_puller/deepwiki/](../gh_puller/deepwiki/); [tests/](../tests/)
```

- Keep the full repository-relative path visible and use a non-empty link target relative to the documentation file.
- Cite the owning package by default. Narrow the citation to a file and name relevant classes, functions, configuration keys, or commands in inline code only when that materially improves navigation or supports a file-specific claim.
- Treat symbol names as plain text. Do not invent source-code heading anchors.
- Never cite line numbers or line ranges. They drift under ordinary edits and can silently point at unrelated code.
- Every local citation must appear in the source inventory, and every inventory entry must support at least one significant part of the page. A file citation is covered only by that exact file entry, not by an inventory entry for its parent package.
- Consolidate shared citations at paragraph, table, diagram, or section granularity. Do not append the same citation to every sentence.
- When updating a page, recheck the entire source inventory and remove paths that no longer support the document.

## Content That Ages Well

- Describe only the current design, behavior, and usage. Keep migration history, removed behavior, and implementation chronology in commits or changelogs.
- Define each core concept or contract in one authoritative documentation location. Link to that location from other pages instead of maintaining parallel explanations.
- Prefer stable responsibilities, boundaries, invariants, data flow, failure semantics, and public usage over incidental call sequences or private helper inventories.
- Do not freeze volatile counts, exhaustive field lists, defaults, or implementation details into prose unless readers need them as a current reference. When included, verify every value and cite its owning source.
- Use tables for genuine mappings or comparisons. Do not turn ordinary prose into a table merely to appear comprehensive.
- Keep code excerpts short and necessary. Prefer verified commands and configuration examples over copied implementation bodies.
- Use repository-relative example paths and placeholder secrets. Avoid developer-machine absolute paths unless an absolute deployment path is itself part of the documented contract.
- Use precise technical language without marketing claims, filler, acknowledgements, or generic summaries.

## Commands and Examples

- Run Python through UV. Use `uv run ...` at the repository root and `uv --directory <subproject> run ...` for a subproject.
- Use `pnpm --dir <subproject> ...` for a frontend subproject.
- Never express subproject commands as `cd <path> && <command>`.
- Verify flags, defaults, environment variables, filenames, and output examples against the current CLI, configuration source, or tests.
- Keep comments inside code examples in English.
- Run documented commands only when doing so is safe and proportionate. Otherwise validate them against their implementation and state clearly in the task handoff that they were not executed.

## Diagrams and Navigation

- Use a diagram only when it materially clarifies a multi-step flow, architecture, ownership boundary, hierarchy, or state transition.
- Model stable responsibilities and interactions rather than every private function. Explain the diagram briefly and cite the sources that establish it.
- Use `flowchart TD` or `graph TD` for flow diagrams; do not use left-to-right flow. Keep node labels concise.
- In sequence diagrams, declare all participants before messages and use sequence-diagram message syntax with colon-separated labels.
- Link to another project document only when the target file exists. Prefer a file-level relative link; add a heading anchor only when the specific section matters and the anchor has been verified.
- Use descriptive labels for external links and prefer primary, authoritative sources.

## Final Verification

Before finishing:

1. Re-read the page as a user-facing explanation and remove statements that describe an earlier implementation or an unsupported future design.
2. Resolve every local Markdown target from the document's directory and confirm that the file exists.
3. Confirm that cross-document anchors and external links used by the change are valid.
4. Check that there are no empty Markdown link destinations, line-number citations, unresolved placeholders, stale source inventory entries, or bare-filename citations.
5. Confirm that every material implementation claim, diagram, table, and operational command has sufficient evidence without citation spam.
6. Compare commands and examples with the current implementation and run safe, relevant validation where useful.
7. Review the final diff for accidental edits outside the requested documentation scope.
