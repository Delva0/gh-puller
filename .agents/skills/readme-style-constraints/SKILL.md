---
name: readme-style-constraints
description: Create or substantially revise repository-root or component README.md files as concise, proof-driven, visually intentional entry points that help intended readers judge fit, reach a first successful result, and find deeper documentation. Use for significant README work, not technical articles under docs/ or generated reference documentation.
---

# Repository README

Create a README that is ready to commit and useful to a first-time reader. Treat it as the project's decision and activation surface: it should establish relevance, demonstrate the project concretely, lead to a first successful result, and route deeper questions to their authoritative homes.

Keep the work scoped to the requested README. Changes to implementation, repository metadata, hosted settings, or adjacent policy files require separate user intent.

## Scope

- Apply the full contract to a new README or a substantial revision.
- For a focused edit, preserve the existing organization and voice while updating the affected claims, commands, links, and examples.
- Use the requested path and language. If either is unspecified, infer the README level from its directory and match the predominant language of the repository or component.
- Distinguish a repository README from a component README before deciding what belongs in it.

## Research Before Writing

1. Read the target README when present. For a component README, also read the repository README and the nearest authoritative documentation needed to understand the component's role.
2. Trace the reader-facing path through public entry points, package and release metadata, CI configuration, licensing, platform declarations, representative examples, tests, and existing documentation. Inspect implementation only as far as needed to verify public claims; do not turn the README into an internal inventory.
3. Identify the primary reader and the decision or task that brought them to this file. When several audiences exist, choose one primary path and route the others explicitly.
4. Determine every surface that renders the README, such as GitHub, a package registry, or a documentation site. Account for the Markdown, HTML, link, and asset behavior shared by those surfaces.
5. Inventory the verified status and identity signals available for a compact badge row, including package versions, CI state, runtime or platform compatibility, licensing, and recognized standards.
6. Find the strongest current proof of value: a runnable example, observable terminal output, request and response, benchmark with context, or an existing rendered image asset that shows the actual architecture or result.
7. Verify commands, prerequisites, filenames, defaults, links, and visible output against the current repository. Run safe commands when proportionate; otherwise verify them from their owning source and report that limitation in the handoff.
8. Omit claims that cannot be supported by the repository or an authoritative external source.

## Editorial Model

Shape the README around this reader journey:

```text
Audience -> Promise -> Proof -> First success -> Next path
```

- **Audience** is the reader who must recognize that the project fits a real need.
- **Promise** is the concrete outcome the project enables and the important boundary that distinguishes it.
- **Proof** makes that promise credible through real behavior rather than adjectives.
- **First success** is the shortest complete path from the reader's current state to an observable result.
- **Next path** directs deeper usage, configuration, understanding, support, or contribution to the right place.

Let this journey determine the smallest useful set of sections and their order. Use headings that readers naturally scan for; the model's terms are decision criteria, not a required outline.

## README Level

For a repository README, establish the project's identity and primary use case, then expose its canonical first-success path. Include a high-level project map only when readers need it to choose among substantial applications or packages.

For a component README, first locate the component within its parent project and state its responsibility or consumption boundary. Keep local setup and one representative use path close at hand, while routing the shared project story, global setup, and cross-component architecture to their authoritative locations.

Adapt the proof to the product:

- For a CLI, prefer a short command and representative output.
- For a library, prefer a minimal example that produces a meaningful result.
- For a service, prefer a start-and-call path with an observable response.
- For a UI application, prefer a current screenshot or a short interaction when it communicates more than text.
- For a monorepo, identify the few entry points readers actually choose between rather than mirroring the directory tree.

## Presentation Direction

Treat presentation as a layer over the editorial model, not as a substitute for it. Choose its intensity from the user's intent, the README level, the project's established identity, the quality of available proof, and the rendering surfaces.

- Default a repository-root README intended for public readers to a showcase presentation. Choose an editorial presentation instead only when the user requests a restrained treatment or a required rendering surface cannot support the relevant presentation elements.
- Default component, internal, and reference-oriented READMEs to an editorial presentation: native Markdown, clear hierarchy, and only the visual elements that improve comprehension.
- Compose a showcase opening from project identity, concise trust signals, a precise promise, a selling-point image when available, and a primary action. Keep the semantic content in Markdown and use minimal renderer-compatible HTML only for composition that Markdown cannot express, such as centering, image sizing, theme-aware images, or collapsible secondary choices.
- A selling-point image is a verified, renderer-compatible PNG, JPEG, WebP, GIF, or SVG asset referenced by a resolvable path or URL. It should show the actual architecture, output, comparison, or product experience.
- Reuse established branding and current repository-owned output wherever possible. Treat the creation of a logo, hero image, screenshot, recording, or other new asset as a separate deliverable within the user's authorized scope.
- Make visual assets legible at typical README widths, useful in supported light and dark themes, and understandable through concise alternative text and a caption when readers need help interpreting the evidence.

## Opening View

The opening should let the intended reader answer three questions without searching: what the project is, why it may matter to them, and what they should do next.

- State the project identity and outcome in precise language consistent with the implementation.
- Surface the primary action and strongest proof before secondary project information.
- For a showcase opening, actively build a compact badge row from verified repository facts even when the target has no existing badges. Prefer live package or release version, CI or build state, runtime or platform compatibility, licensing, and recognized standard compatibility.
- When live signals are unavailable, use restrained static badges for stable scope facts such as the required runtime, target platform, storage model, or protocol. Link each badge to its authoritative destination when that adds useful context.
- When a suitable selling-point image exists, place it immediately after the project identity, promise, and any compact trust signals. Use an architecture image when the advantage is a clear system structure, flow, or boundary; use a screenshot, result comparison, or short demonstration when the advantage is observable output or experience.
- Focus an architecture image on the differentiating idea rather than the complete module inventory. Give a result image enough context to show what produced it and what the reader should notice.
- When no suitable selling-point image exists, continue directly from the project identity, promise, and trust signals to the primary action. Explain any essential structure in prose.
- Include maturity, compatibility, or operational status when it materially changes an adoption decision.

## First Success

Present one canonical golden path rather than a collection of setup variants.

- Include only prerequisites that block the path.
- Make the sequence complete: install or prepare, run or call, and observe a recognizable result.
- Keep commands copyable and examples internally consistent. Explain placeholders where their meaning is not obvious and never use real secrets.
- Give each code block a purpose in the surrounding prose and make the next action clear.
- Route alternative platforms, deployment modes, exhaustive configuration, and advanced usage to existing documentation after the canonical path is understandable.

## Proof and Claims

- Prefer one representative demonstration that substantiates the central promise over several overlapping examples.
- Describe capabilities through the outcomes they enable. Group secondary capabilities around the few distinctions that shape a reader's decision.
- State comparative or performance claims only with the relevant conditions and a link to reproducible evidence.
- Keep screenshots, recordings, badges, and output samples synchronized with current behavior. Every such element should contribute information rather than decoration.
- Use restrained, factual language. Let verified behavior carry the persuasive work.

## Progressive Disclosure

Make the README the smallest complete front door, not a compressed manual.

- Keep the project identity, blocking prerequisites, canonical path, and immediate success signal in the README.
- Link detailed architecture, protocols, configuration catalogs, deployment variants, troubleshooting, contribution policy, security policy, and API reference to existing authoritative files when readers need them.
- Use repository-relative links when the repository host and local clones are the README's rendering surfaces. When the same README is published elsewhere, choose links and assets that resolve on every required surface; report an unresolved portability conflict rather than silently optimizing for one renderer.
- Define durable project-level concepts in one authoritative location and link to them instead of maintaining parallel explanations.

## Durable Editing

- Describe the current project rather than its change history. Include a roadmap only when it is maintained and sets useful expectations.
- Prefer stable capabilities, boundaries, and public entry points over volatile counts, exhaustive lists, internal module names, or transient implementation details.
- Preserve intentional project voice, terminology, branding, and useful custom sections. Improve hierarchy and precision without homogenizing the README into a generic template.
- Remove repetition and filler before removing information required for first success or an informed adoption decision.
- During a focused edit, recheck nearby examples and links whose meaning depends on the changed material, without rewriting unrelated sections.
