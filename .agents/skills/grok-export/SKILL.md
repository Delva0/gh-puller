---
name: grok-export
description: Export public Grok shares as one complete, agent-readable Markdown file, preserving every exposed message, thinking step, tool call, result, subagent conversation, and metadata field. Use for archiving or inspecting grok.com/share conversations and execution traces.
---

# Grok Export

Export one complete Markdown file by default. Preserve all exposed agent context,
not just user/assistant prose. Do not create multiple output files unless requested.

```bash
uv run --no-project python <skill-dir>/scripts/export_grok.py \
  'https://grok.com/share/<share-id>' --output <conversation.md>
```

Resolve the script relative to this skill. The file contains the source, coverage
report, every response and native step/chunk, tool arguments and outputs, thinking
channels, metadata, attachment records, and public subagent conversations. Text is
readable, structured payloads remain JSON, and no content is shortened. Unknown
fields and empty values are preserved too.

The exporter fetches both flat and `useChunk=true` representations and follows
listed public subagent conversations. Keep both views in the same file: chunks
preserve channels and interleaved results, while legacy XML can preserve explicit
`null` arguments omitted by structured cards. Views describe the same executions;
do not double-count their shared tool-call IDs or present them as consecutive turns.

Read the coverage report before claiming completion. Exit status 2 means a source
failed or the schema needs follow-up; the partial Markdown is still saved. Inspect
the reported failures, continuation markers, and subagent references, and complete
the acquisition where the public interface permits. Report remaining limitations.

For an explicitly requested raw supplement, add `--raw-json <archive.json>`.
For saved captures, use `--snapshot flat=<path> --snapshot chunks=<path>` and add
other named subagent captures as needed. Offline mode never fetches missing data.

## Fidelity

Preserve source array order and every role, channel, tag, ID, parameter, result,
error, citation, timestamp, parent link, attachment, embedded content, and future
field. Do not sort chunks by step ID: results and text can interleave.

Distinguish detailed thinking from notetaker headers/summaries and thinking times.
Copy reasoning text present in the source; do not reconstruct absent reasoning or
prompts. Empty fields do not prove that a tool never ran.

Keep snippets/previews as recorded. Never fetch today's page and label it as the
historical tool output. Remote asset URLs are references, not archived binaries;
identify that limitation when attachments exist. If the user also requests the
files, save public assets separately with source URLs and acquisition results.

Read [references/protocol.md](references/protocol.md) when diagnosing missing data,
interpreting source fields, or updating the extractor. The website API can change.

Return a link to the **single Markdown**, message/tool counts, and concrete source
limitations. Describe completeness as all **exposed share data**, not recovery of
hidden prompts, private reasoning, unshared branches, or omitted tool bodies.
