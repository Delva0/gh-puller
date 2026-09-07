---
name: docs-style-constraints
description: Create or substantially revise maintainable, repository-grounded Markdown documentation under docs/ with verified commands and links, reader-oriented source navigation, and diagrams or tables only when they improve understanding. Use for full pages or significant edits, not typo-only changes or files outside docs/.
---

# Project Documentation

Create Markdown under `docs/` that is complete on disk, ready to commit as written, and useful as the repository evolves. The document carries its own links, citations, and final wording independently of later processing.

Documentation changes follow the user's requested scope and the applicable `AGENTS.md`. Implementation work belongs to a separately requested scope.

## Decision Standard

A document contains every concept whose absence could change how a reader operates the feature, interprets its data or results, or judges its guarantees and limits. Once those concepts are present, reduce cognitive load through ordering, grouping, precise names, progressive detail, and source navigation. Compress implementation detail that does not affect those reader outcomes.

## Scope

- Apply the full contract to new pages and substantial revisions.
- For a focused edit, preserve the existing page structure and limit changes to the affected claims, citations, source inventory, links, and examples.
- Use the requested document path and language. If either is unspecified, infer a clear `docs/` path and match the predominant language of related project documentation.

## Research Before Writing

1. Read the existing target page when present, and closely related pages under `docs/`.
2. Trace the topic through its canonical implementation, public entry points, configuration, tests, and operational surfaces. Read the complete passages that establish each material claim.
3. Identify the cohesive repository boundaries that own the documented behavior and the narrower sources needed to verify specific claims.
4. Separate repository behavior from external contracts. Use current primary documentation for an external API or tool when it is necessary to explain that contract, and cite it with a normal working URL.
5. State each claim only as strongly as its evidence allows. Make essential unresolved uncertainty explicit.

## Editorial Focus

Write a technical article around one central question or design claim. Include a concept when the Decision Standard says it can affect the reader's outcome, and connect supporting concepts to that center before adding their detail.

- For an explanatory page, select the protocol fields, configuration, modules, and examples needed to operate or reason correctly. For a reference page, provide the complete set promised by its scope.
- Give each flow, example, table, or diagram a distinct explanatory job. Multiple views earn their place when they expose materially different relationships.
- Introduce a concept before details that depend on it, and put qualifications next to the claim they constrain.
- Let source research increase the precision of the explanation rather than the visible size of its implementation inventory.

## Full-Page Structure

A new page or substantial rewrite starts with this source inventory:

```markdown
<details>
<summary>Relevant sources</summary>

- [gh_puller/deepwiki/](../gh_puller/deepwiki/)
- [tests/](../tests/)
</details>

# Page Title
```

Replace the example entries with the relevant source boundaries selected under Reader-Oriented Source Citations.

- Use the full repository-relative path as the visible label.
- Calculate each link target relative to the documentation file. The examples above are correct for a page directly under `docs/`; adjust the leading `../` segments for nested pages.
- Put the H1 title immediately after the inventory.
- Follow with a concise introduction explaining the feature's purpose, scope, and place in the project.
- Organize the body by the topic's actual concepts using H2 and H3 headings. Each heading should name one cohesive idea, and unfamiliar terms are defined where they first matter. Let the subject determine the section structure.
- Include a conclusion when it contributes information beyond the introduction.

A focused edit retains the existing page wrapper unless its scope genuinely requires a structural change.

## Reader-Oriented Source Citations

The source inventory maps the page to the cohesive repository areas that establish it. A section-level citation supplies a shorter route to an implementation owner when that route materially helps the reader.

Select a source path in two stages. First verify that its contents establish the claim; this defines the accurate candidates. Among equally accurate candidates, choose the highest-level cohesive directory that lets a reader locate the owner without searching unrelated subsystems. Select a file when no directory preserves that precision. Accuracy determines the candidates, and reader effort breaks ties.

When a precise citation has reader value, place it after the cohesive claim or block it supports:

```markdown
Sources: [gh_puller/deepwiki/](../gh_puller/deepwiki/); [tests/](../tests/)
```

- Let the inventory carry evidence shared across the page. Place a more precise citation at the nearest paragraph, table, diagram, or section whose concentrated ownership makes it useful.
- Show the full repository-relative path and end the link at a stable directory or file. Refer to relevant symbols as plain text.
- Cover every section-level citation with an inventory entry. An inventory directory covers its descendants, and every inventory entry supports a significant part of the page.

## Content That Ages Well

- Describe the current design, behavior, and usage. Place migration history and implementation chronology in commits or changelogs.
- Define each core concept or contract in one authoritative documentation location. Link to that location from other pages instead of maintaining parallel explanations.
- Prefer stable responsibilities, boundaries, invariants, data flow, failure semantics, and public usage over incidental call sequences or private helper inventories.
- Include volatile counts, exhaustive field lists, defaults, and implementation details when readers need them as a current reference. Verify each included value against its owning source and add precise provenance when useful.
- Use tables for genuine mappings or comparisons and prose for linear explanations.
- Keep code excerpts short and necessary. Prefer verified commands and configuration examples over copied implementation bodies.
- Use repository-relative example paths and placeholder secrets; use absolute deployment paths when they are part of the documented contract.

## Commands and Examples

- Run Python through UV. Use `uv run ...` at the repository root and `uv --directory <subproject> run ...` for a subproject.
- Use `pnpm --dir <subproject> ...` for a frontend subproject.
- Verify flags, defaults, environment variables, filenames, and output examples against the current CLI, configuration source, or tests.
- Keep comments inside code examples in English.
- Run documented commands when execution is safe and proportionate. For other commands, validate them against their implementation and identify that validation method in the task handoff.

## Diagrams and Navigation

- Use a diagram only when it materially clarifies a multi-step flow, architecture, ownership boundary, hierarchy, or state transition.
- Model stable responsibilities and interactions rather than every private function. Explain the diagram briefly; cite its implementation source only when that improves navigation.
- Use `flowchart TD` or `graph TD` to orient flow diagrams from top to bottom. Keep node labels concise.
- In sequence diagrams, declare all participants before messages and use sequence-diagram message syntax with colon-separated labels.
- Link to existing project documents with file-level relative paths, adding a verified heading anchor when the specific section matters.
- Use descriptive labels for external links and prefer primary, authoritative sources.

## Final Verification

Before handoff:

1. Compare every material behavior, guarantee, limit, diagram, table, and example with its owning source.
2. Resolve every local Markdown destination from the document's directory and verify each referenced heading anchor. Check material external links when the task depends on them.
3. Confirm that the source inventory accurately covers the page, each section citation is covered by it, and each listed path still provides useful evidence.
4. Execute safe, proportionate commands. Otherwise verify them against the CLI, configuration, or tests and report that method in the handoff.
5. Read the page in order and confirm that every outcome-changing concept is present, introduced before use, and distinguished from neighboring concepts.
6. Review the final diff against the requested documentation scope.
