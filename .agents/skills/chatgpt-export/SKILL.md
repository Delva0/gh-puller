---
name: chatgpt-export
description: Export a public ChatGPT shared conversation to Markdown with a programmatic extractor, preserving all user and assistant content on the shared conversation branch. Use when saving or archiving a ChatGPT share link.
---

# ChatGPT Conversation Export

Use [scripts/export_chat.py](scripts/export_chat.py) to extract the page's embedded
conversation data. Generate the transcript programmatically without transcribing,
summarizing or rewriting its messages. The script uses only the Python standard
library and runs independently of the surrounding repository.

## Export

Resolve the script path relative to this skill directory and run Python through UV:

```bash
uv run --no-project python /path/to/chatgpt-export/scripts/export_chat.py \
  'https://chatgpt.com/share/SHARE_ID' \
  --output /path/to/destination/chat_YYYY-MM-DD.md
```

Honor the user's output location and date. Without `--output`, the script writes
`chat_<local date>.md` in the working directory. Existing files are refused.

For a reproducible export, add `--save-html /path/to/snapshot.html`. Replay that
exact page with `--html /path/to/snapshot.html` and a new output path. Store snapshots
with task artifacts or in a temporary directory, outside the skill resources.
Compare completeness against the same snapshot: separate requests can append
different follow-up suggestions to the final reply.

## Content and Verification

The export includes nonempty user and assistant messages in source order, including
progress replies, public thought summaries and reasoning duration. It retains
Markdown text and code blocks, converts citation markers using the page's link
metadata, and appends source lists. System records, tool records and empty messages
are excluded. Nontext content is retained as JSON; attachment binaries are not
downloaded, and redacted or unavailable content cannot be reconstructed.

The script validates the complete parent chain against the shared page's message
order before writing. It records message IDs and content hashes in the Markdown,
then prints user/assistant counts and the output hash. Check those counts and the
final message when reporting completion; assistant counts include process messages.
For exact comparison, replay the saved snapshot and ignore only the export timestamp.

If fetching or parsing fails, inspect the response or saved page before changing the
extractor. A login page, browser challenge or unsupported payload is not a transcript.
