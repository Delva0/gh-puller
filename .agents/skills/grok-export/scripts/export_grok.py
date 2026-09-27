"""Export all exposed Grok share context as one reversible, agent-readable Markdown document."""

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

CALL_FIELDS = {"toolUsageCard", "toolUsageCards", "toolCall", "toolCalls", "tool_calls"}
RESULT_FIELDS = {"toolResult", "toolResults", "toolUsageResults", "toolResponses"}
STREAM_FIELDS = {"responses", "steps", "inputChunks", "outputChunks", "toolResponses"}
TEXT_FIELDS = {"message", "text", "thinking", "reasoning", "content", "snippet", "preview", "parsedText"}
CONTINUATION_FIELDS = {"nextPageToken", "nextCursor", "continuationToken", "hasMore"}
ASSET_FIELDS = {"imageAttachments", "fileAttachments", "fileUris", "fileIds", "generatedImageUrls",
                "fileAttachmentsMetadata", "fileAttachmentAssetMetadata", "imageEditUris"}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def join(path, key):
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def walk(value, path="", response_id=None):
    if is_response(value):
        response_id = value["responseId"]
    yield path, value, response_id
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, join(path, key), response_id)
    elif isinstance(value, list):
        for key, child in enumerate(value):
            yield from walk(child, join(path, key), response_id)


def is_response(value):
    return isinstance(value, dict) and "responseId" in value and ("sender" in value or "role" in value)


def responses(document):
    return [value for _, value, _ in walk(document) if is_response(value)]


def share_id(url):
    parsed = urlsplit(url)
    match = re.fullmatch(r"/share/([A-Za-z0-9_-]+)/?", parsed.path)
    if parsed.scheme != "https" or parsed.netloc not in {"grok.com", "www.grok.com"} or not match:
        raise ValueError("Expected an HTTPS grok.com/share/<id> URL")
    return match[1]


def snapshot(name, body, provenance):
    result = {"name": name, "sha256": hashlib.sha256(body).hexdigest(), **provenance,
              "body": body.decode("utf-8", errors="replace")}
    try:
        result["document"] = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        result["decode_error"] = str(exc)
    return result


def fetch(name, url):
    result = subprocess.run(
        ["curl", "--silent", "--show-error", "--location", "--compressed", "--max-time", "45",
         "--proto", "=https", "--proto-redir", "=https", "--write-out", "\n%{http_code}", url],
        capture_output=True, check=False,
    )
    body, _, status = result.stdout.rpartition(b"\n")
    status = int(status) if status.isdigit() else 0
    provenance = {"url": url, "fetched_at": datetime.now(UTC).isoformat(), "http_status": status}
    if result.returncode or not 200 <= status < 300:
        provenance["acquisition_error"] = result.stderr.decode(errors="replace").strip() or f"HTTP {status}"
    return snapshot(name, body, provenance)


def subagents(document):
    for path, value, _ in walk(document):
        if isinstance(value, dict) and isinstance(value.get("sharedSubagents"), list):
            for index, child in enumerate(value["sharedSubagents"]):
                yield join(join(path, "sharedSubagents"), index), child


def acquire(url, captures):
    identifier = share_id(url)
    sources = []
    if captures:
        names = set()
        for capture in captures:
            name, separator, filename = capture.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name in names:
                raise ValueError("Each --snapshot must be a unique NAME=PATH with a filename-safe NAME")
            names.add(name)
            path = Path(filename)
            sources.append(snapshot(name, path.read_bytes(), {"input_path": str(path.resolve())}))
        return sources
    endpoint = f"https://grok.com/rest/app-chat/share_links_data/{quote(identifier, safe='')}"
    for name, suffix in [("chunks", "?useChunk=true"), ("flat", "")]:
        sources.append(fetch(name, endpoint + suffix))
    seen = set()
    # Appending while iterating follows nested public subagents and terminates on repeated IDs.
    for source in sources:
        for _, child in subagents(source.get("document")):
            child_id = child.get("conversationId") if isinstance(child, dict) else None
            if not isinstance(child_id, str) or not child_id or child_id in seen:
                continue
            seen.add(child_id)
            child_url = (f"https://grok.com/rest/app-chat/share_links/{quote(identifier, safe='')}"
                         f"/subagents/{quote(child_id, safe='')}")
            child_source = fetch(f"subagent-{len(seen):04}", child_url)
            child_source["subagent_conversation_id"] = child_id
            sources.append(child_source)
    return sources


def xml_cards(text):
    for match in re.finditer(r"<xai:tool_usage_card>.*?</xai:tool_usage_card>", text, re.DOTALL):
        values = {}
        for key in ("tool_usage_card_id", "tool_name", "tool_args"):
            item = re.search(fr"<xai:{key}>(.*?)</xai:{key}>", match[0], re.DOTALL)
            if item:
                values[key] = item[1]
        args = values.get("tool_args", "")
        if args.startswith("<![CDATA[") and args.endswith("]]>"):
            args = args[9:-3]
        try:
            values["arguments"] = json.loads(args)
        except ValueError as exc:
            values["parse_error"] = str(exc)
        yield values


def tool_index(sources):
    index, unindexed = {}, []
    for source in sources:
        for path, value, response_id in walk(source.get("document")):
            key = path.rsplit("/", 1)[-1]
            parent = path.rsplit("/", 2)[-2] if "/" in path else ""
            collection_item = key.isdigit() and parent in CALL_FIELDS | RESULT_FIELDS
            kind = "calls" if key in CALL_FIELDS or (collection_item and parent in CALL_FIELDS) else "results"
            relevant = key in CALL_FIELDS | RESULT_FIELDS or collection_item
            entries = [(kind, value)] if relevant and isinstance(value, dict) else []
            if isinstance(value, str) and "<xai:tool_usage_card>" in value:
                entries.extend(("calls", card) for card in xml_cards(value))
                if not entries:
                    unindexed.append({"source": source["name"], "pointer": path, "reason": "Malformed tool XML"})
            for kind, entry in entries:
                identifier = (entry.get("toolUsageCardId") or entry.get("toolCallId")
                              or entry.get("tool_usage_card_id") or entry.get("id"))
                if not identifier:
                    unindexed.append({"source": source["name"], "pointer": path,
                                      "reason": "Tool entry has no recognized call ID"})
                    continue
                record = index.setdefault((response_id, identifier), {
                    "response_id": response_id, "tool_call_id": identifier, "names": [], "calls": [], "results": [],
                })
                record[kind].append({"source": source["name"], "pointer": path})
                if kind == "calls":
                    names = ([entry["tool_name"]] if "tool_name" in entry else
                             [name for name, part in entry.items() if isinstance(part, dict) and "args" in part])
                    for name in names:
                        normalized = name[:1].upper() + name[1:]
                        if normalized not in record["names"]:
                            record["names"].append(normalized)
                if "parse_error" in entry:
                    unindexed.append({"source": source["name"], "pointer": path, "reason": entry["parse_error"]})
    return list(index.values()), unindexed


def coverage(sources, offline):
    calls, unindexed = tool_index(sources)
    issues, children, parents, excerpts, assets = [], [], {}, [], []
    all_responses, source_reports, conversations = {}, [], set()
    channels = Counter()
    for source in sources:
        document = source.get("document")
        rows = responses(document)
        for response in rows:
            all_responses[response["responseId"]] = response
            if response.get("parentResponseId"):
                parents[response["responseId"]] = response["parentResponseId"]
        if isinstance(document, dict) and isinstance(document.get("conversation"), dict):
            conversations.add(document["conversation"].get("conversationId"))
        errors = [source[key] for key in ("acquisition_error", "decode_error") if key in source]
        if not isinstance(document, dict) or not isinstance(document.get("responses"), list):
            errors.append("Source has no recognized response list; original content retained")
        issues.extend({"source": source["name"], "reason": error} for error in errors)
        source_reports.append({"name": source["name"], "responses": len(rows),
                               **{field: sum(len(r[field]) for r in rows if isinstance(r.get(field), list))
                                  for field in ("steps", "inputChunks", "outputChunks")}, "errors": errors})
        for path, value, _ in walk(document):
            key = path.rsplit("/", 1)[-1]
            location = {"source": source["name"], "pointer": path}
            if key in CONTINUATION_FIELDS and value:
                issues.append({**location, "reason": "Unresolved continuation marker", "value": value})
            if key in {"preview", "snippet"} and isinstance(value, str) and value:
                excerpts.append(len(value))
            if key in ASSET_FIELDS and value:
                assets.append(location)
            if key == "channel" and isinstance(value, str):
                channels[value] += 1
        for path, child in subagents(document):
            child_id = child.get("conversationId") if isinstance(child, dict) else None
            children.append({"source": source["name"], "pointer": path, "conversation_id": child_id})
    for child in children:
        child["fetched"] = bool(child["conversation_id"] and child["conversation_id"] in conversations)
        if not child["fetched"]:
            issues.append({**child, "reason": "Referenced subagent was not acquired"})
    issues.extend(unindexed)
    main = {s["name"]: s for s in sources if s["name"] in {"flat", "chunks"}}
    if len(main) != 2:
        issues.append({"reason": "Both flat and chunks snapshots are required for cross-view verification"})
    elif any(not responses(item.get("document")) for item in main.values()):
        issues.append({"reason": "A main share snapshot contains no conversation responses"})
    elif {r["responseId"] for r in responses(main["flat"].get("document"))} != {
        r["responseId"] for r in responses(main["chunks"].get("document"))
    }:
        issues.append({"reason": "Flat and chunk snapshots have different response IDs"})
    return {
        "scope": "Every field in the acquired public-share documents; hidden context is not reconstructed",
        "acquisition_complete": not issues, "offline": offline, "acquisition_issues": issues,
        "snapshots": source_reports, "unique_responses": len(all_responses),
        "unique_tool_calls": sum(bool(call["calls"]) for call in calls),
        "tool_names": dict(Counter(name for call in calls for name in call["names"])),
        "calls_without_results": [c["tool_call_id"] for c in calls if c["calls"] and not c["results"]],
        "results_without_calls": [c["tool_call_id"] for c in calls if c["results"] and not c["calls"]],
        "text_channels_across_views": dict(channels),
        "parents_not_in_share": [{"response_id": key, "parent_response_id": value}
                                 for key, value in parents.items() if value not in all_responses],
        "partial_responses": [key for key, row in all_responses.items() if row.get("partial")],
        "stream_errors": [{"response_id": key, "errors": row["streamErrors"]}
                          for key, row in all_responses.items() if row.get("streamErrors")],
        "subagents": children, "excerpt_fields_across_views": len(excerpts),
        "excerpt_fields_at_1000_characters": excerpts.count(1000), "attachment_reference_fields": assets,
        "limitations": [
            "Separate source views describe the same executions; source array order is retained.",
            "Notetaker headers and thinking timestamps are not detailed reasoning transcripts.",
            "Preview/snippet fields are excerpts, not verified full historical tool bodies.",
            "Remote assets are referenced; only content embedded in source JSON is archived.",
            "Unshared branches, omitted prompts, and hidden reasoning cannot be recovered from absent fields.",
        ],
    }


def fenced(text, language):
    fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", text)), default=0))
    return f"{fence}{language}\n{text}\n{fence}\n"


def encode_path(path):
    return quote(path, safe="/") or "-"


def render_value(value, path="", key="", depth=3):
    title = key or "Source document"
    if is_response(value):
        title = f"{value.get('sender', value.get('role'))} — {value['responseId']}"
    if isinstance(value, dict) and isinstance(value.get("text"), dict):
        title += f" — {value['text'].get('channel', 'text')}"
    title = " ".join(str(title).splitlines()).replace("<", "&lt;").replace(">", "&gt;")
    heading = "#" * min(depth, 6)
    parts = [f"{heading} {title}\n"]
    marker = encode_path(path)
    expand = isinstance(value, dict) and any(k in STREAM_FIELDS | TEXT_FIELDS for k in value)
    expand = expand or (isinstance(value, list) and (key in STREAM_FIELDS or key == "text") and bool(value))
    if expand:
        kind = "dict" if isinstance(value, dict) else "list"
        parts.append(f"<!-- grok-container {marker} {kind} -->\n")
        if isinstance(value, dict):
            fields = {}
            for child_key, child in value.items():
                if child_key in STREAM_FIELDS | TEXT_FIELDS and child:
                    if fields:
                        parts.extend([f"<!-- grok-fields {marker} json -->\n", fenced(dumps(fields), "json")])
                        fields = {}
                    parts.append(render_value(child, join(path, child_key), child_key, depth + 1))
                else:
                    fields[child_key] = child
            if fields:
                parts.extend([f"<!-- grok-fields {marker} json -->\n", fenced(dumps(fields), "json")])
        else:
            for index, child in enumerate(value):
                parts.append(render_value(child, join(path, index), key, depth + 1))
    else:
        kind = "text" if isinstance(value, str) and key in TEXT_FIELDS else "json"
        parts.append(f"<!-- grok-value {marker} {kind} -->\n")
        parts.append(fenced(value if kind == "text" else dumps(value), kind))
    return "\n".join(parts)


def render(url, sources, report):
    parts = ["# Complete Grok share context\n", f"Source: {url}\n",
             ("The source views below describe the same conversation. Each view preserves every field "
              "and its own array order, including thinking channels, tool calls, results, and metadata.\n"),
             "## Coverage\n", fenced(dumps(report), "json")]
    for source in sources:
        parts.extend([f"## Source view: {source['name']}\n",
                      fenced(dumps({k: v for k, v in source.items() if k not in {"document", "body"}}), "json"),
                      f"<!-- grok-source {source['name']} -->\n"])
        parts.append(render_value(source["document"] if "document" in source else source["body"]))
        parts.append("<!-- grok-end-source -->\n")
    return "\n".join(parts)


def read_documents(markdown):
    """Reconstruct source values from generated Markdown partitions for loss detection.

    Args:
        markdown: Unmodified Markdown generated by render, with its structural markers.

    Returns:
        Original source documents keyed by snapshot name, including opaque error bodies.
    """
    documents, current = {}, None
    lines = iter(markdown.split("\n"))
    for line in lines:
        if line.startswith("<!-- grok-source "):
            current = line.split()[2]
        elif line == "<!-- grok-end-source -->":
            current = None
        elif current and line.startswith(("<!-- grok-value ", "<!-- grok-container ", "<!-- grok-fields ")):
            _, tag, encoded, kind, _ = line.split()
            path = "" if encoded == "-" else unquote(encoded)
            if tag == "grok-container":
                value = {} if kind == "dict" else []
            else:
                opening = next(lines)
                while not opening:
                    opening = next(lines)
                fence = opening.removesuffix(kind)
                content = []
                for body_line in lines:
                    if body_line == fence:
                        break
                    content.append(body_line)
                text = "\n".join(content)
                value = text if kind == "text" else json.loads(text)
            if tag == "grok-fields":
                target = documents[current]
                if path:
                    for segment in path[1:].split("/"):
                        decoded = segment.replace("~1", "/").replace("~0", "~")
                        target = target[int(decoded)] if isinstance(target, list) else target[decoded]
                target.update(value)
                continue
            if not path:
                documents[current] = value
                continue
            segments = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
            parent = documents[current]
            for segment in segments[:-1]:
                parent = parent[int(segment)] if isinstance(parent, list) else parent[segment]
            if isinstance(parent, list):
                parent.append(value)
            else:
                parent[segments[-1]] = value
    return documents


def export(url, output, captures=(), raw_json=None):
    """Write a single complete Markdown after verifying its source reconstruction.

    Args:
        url: Public Grok share URL identifying the source conversation.
        output: New Markdown file; an existing file is never overwritten.
        captures: NAME=PATH files for offline acquisition instead of network requests.
        raw_json: Optional new JSON file, produced only when explicitly requested.

    Returns:
        Coverage report, including source acquisition failures and visibility limits.
    """
    share_id(url)
    if output.exists() or (raw_json is not None and (raw_json.exists() or raw_json == output)):
        raise FileExistsError("Choose new output paths; existing files are not overwritten")
    sources = acquire(url, captures)
    report = coverage(sources, bool(captures))
    markdown = render(url, sources, report)
    expected = {s["name"]: s.get("document", s["body"]) for s in sources}
    if read_documents(markdown) != expected:
        raise ValueError("Markdown round-trip lost source content; refusing to write an incomplete export")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as file:
        file.write(markdown)
    if raw_json is not None:
        raw_json.parent.mkdir(parents=True, exist_ok=True)
        with raw_json.open("x", encoding="utf-8") as file:
            file.write(dumps({"share_url": url, "coverage": report, "snapshots": sources}) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Public https://grok.com/share/<id> URL")
    parser.add_argument("--output", type=Path, required=True, help="New Markdown file")
    parser.add_argument("--raw-json", type=Path, help="Optional additional raw archive, disabled by default")
    parser.add_argument("--snapshot", action="append", default=[], metavar="NAME=PATH",
                        help="Use saved flat/chunks/subagent captures instead of network acquisition")
    args = parser.parse_args()
    try:
        report = export(args.url, args.output, args.snapshot, args.raw_json)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Export failed: {exc}\n")
    print(dumps({"output": str(args.output.resolve()), "responses": report["unique_responses"],
                 "tool_calls": report["unique_tool_calls"], "acquisition_complete": report["acquisition_complete"]}))
    return 0 if report["acquisition_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
