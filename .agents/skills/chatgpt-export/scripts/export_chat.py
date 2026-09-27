"""Export a ChatGPT shared conversation from its embedded data to Markdown.

This standalone, standard-library script retains user messages, assistant replies,
progress updates and public thought summaries in source order. Citation metadata
supplies Markdown links and source lists. Nontext content stays as JSON; attachment
binaries are not downloaded. Existing exports are never overwritten.

Run via UV with the script path and share URL. The default destination is
chat_<local date>.md in the working directory.
Use --html for a saved page, --save-html to retain the fetched page for replay,
and --output to choose the destination.
"""
# ruff: noqa: INP001 - Standalone skill script, not an importable package.

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

STREAM = re.compile(r'window\.__reactRouterContext\.streamController\.enqueue\(("(?:\\.|[^"\\])*")\)')
ROUTE = "routes/share.$shareId.($action)"


def shared_conversation(page):
    """Decode the route's indexed JSON graph without executing page JavaScript."""
    chunks = STREAM.findall(page)
    if not chunks:
        raise ValueError("No shared conversation data found; the page may require login or a browser challenge")
    values, _ = json.JSONDecoder().raw_decode("".join(json.loads(chunk) for chunk in chunks))
    cache = {}

    def decode(index):
        if index in {-1, -5}:
            return None
        if index < 0:
            raise ValueError("Unsupported serialized reference: " + str(index))
        if index in cache:
            return cache[index]
        value = values[index]
        if isinstance(value, dict):
            result = {}
            cache[index] = result
            result.update({decode(int(key[1:])): decode(ref) for key, ref in value.items()})
            return result
        if isinstance(value, list):
            result = []
            cache[index] = result
            result.extend(decode(ref) for ref in value)
            return result
        return value

    key = "_" + str(values.index(ROUTE))
    loader = next(value for value in values if isinstance(value, dict) and key in value)
    response = decode(loader[key])["serverResponse"]
    if response["type"] != "data":
        raise ValueError("The shared page did not return conversation data")
    return response["data"]


def ordered_messages(conversation):
    """Require the complete current branch before selecting user and assistant messages."""
    nodes = conversation["linear_conversation"]
    mapping = conversation["mapping"]
    branch, seen = [], set()
    current = conversation["current_node"]
    while current is not None:
        if current in seen:
            raise ValueError("Conversation parent chain contains a cycle")
        seen.add(current)
        branch.append(current)
        current = mapping[current].get("parent")
    if [node["id"] for node in nodes] != branch[::-1]:
        raise ValueError("Linear conversation is incomplete or out of order")
    for node in nodes:
        message = node.get("message")
        if message and message["author"]["role"] in {"user", "assistant"}:
            yield message


def json_block(value):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    return f"{fence}json\n{text}\n{fence}"


def content_text(content):
    """Preserve unfamiliar content as JSON instead of silently omitting it."""
    kind = content["content_type"]
    if kind in {"text", "multimodal_text"}:
        return "\n\n".join(part if isinstance(part, str) else json_block(part) for part in content["parts"])
    if kind == "reasoning_recap":
        return content["content"]
    if kind == "thoughts":
        blocks = []
        for thought in content["thoughts"]:
            if thought.get("summary"):
                blocks.append("### " + thought["summary"])
            if thought.get("content"):
                blocks.append(thought["content"])
            if thought.get("chunks"):
                blocks.append(json_block(thought["chunks"]))
        return "\n\n".join(blocks)
    return json_block(content)


def source_link(source):
    title = source.get("title") or source.get("attribution") or source["url"]
    title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return f"[{title}](<{source['url']}>)"


def message_text(message):
    """Replace only citation markers; keep all other original text verbatim."""
    text = content_text(message["content"])
    sources = []
    for reference in message.get("metadata", {}).get("content_references", []):
        if reference["type"] == "sources_footnote":
            sources.extend(reference.get("sources", []))
        elif reference.get("matched_text") and reference.get("alt"):
            text = text.replace(reference["matched_text"], reference["alt"])
    if sources:
        text += "\n\n### 引用来源\n\n" + "\n".join("- " + source_link(source) for source in sources)
    return text


def label(message):
    if message["author"]["role"] == "user":
        return "用户"
    kind = message["content"]["content_type"]
    if kind == "thoughts":
        return "AI（公开思考摘要）"
    if kind == "reasoning_recap":
        return "AI（思考耗时）"
    return {"commentary": "AI（过程回复）", "final": "AI（回答）"}.get(message.get("channel"), "AI")


def export_markdown(conversation, source, exported_at):
    """Return the export and message counts, omitting only empty dialogue records."""
    messages = [(message, message_text(message)) for message in ordered_messages(conversation)]
    messages = [(message, text) for message, text in messages if text]
    if not messages:
        raise ValueError("No user or assistant content found")
    counts = Counter(message["author"]["role"] for message, _ in messages)
    sections = [(f"# {conversation['title']}\n\n"
                f"来源：[{source}]({source})  \n导出时间：{exported_at}\n\n"
                f"程序提取分享页内嵌数据，按原顺序保留 {counts['user']} 条用户消息和 {counts['assistant']} 条 AI 消息"
                "（含过程回复、公开思考摘要和思考耗时）。正文保持原文，引用标记转换为页面提供的链接，保留引用来源列表。"
                 "空消息、系统记录和工具记录不属于用户与 AI 对话正文。")]
    for number, (message, text) in enumerate(messages, 1):
        digest = sha256(json.dumps(message["content"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        sections.append(f"## {number:02d} · {label(message)}\n\n"
                        f"<!-- message-id: {message['id']}; content-sha256: {digest} -->\n\n{text}")
    return "\n\n---\n\n".join(sections) + "\n", counts


def share_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc not in {"chatgpt.com", "chat.openai.com"}
            or not re.fullmatch(r"/share/[\w-]+/?", parsed.path)):
        raise argparse.ArgumentTypeError("Expected an HTTPS ChatGPT share URL")
    return "https://chatgpt.com" + parsed.path.rstrip("/")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", type=share_url, help="Public ChatGPT share URL")
    parser.add_argument("--output", "-o", type=Path,
                        help="Destination; default: chat_<local date>.md in the working directory")
    parser.add_argument("--html", type=Path, help="Read a saved share page instead of downloading it")
    parser.add_argument("--save-html", type=Path, help="Keep the exact page used for this export")
    args = parser.parse_args()
    now = datetime.now().astimezone()
    output = args.output or Path.cwd() / f"chat_{now.date().isoformat()}.md"
    if output.exists():
        parser.error(f"Output already exists: {output}; choose another --output path")
    if args.save_html and (args.save_html.exists() or args.save_html.resolve() == output.resolve()):
        parser.error("--save-html must be a new file distinct from --output")
    if args.html:
        page = args.html.read_text(encoding="utf-8")
    else:
        # share_url restricts the request to an HTTPS ChatGPT share page.
        request = Request(args.url, headers={"User-Agent": "Mozilla/5.0"})  # noqa: S310
        with urlopen(request, timeout=60) as response:  # noqa: S310
            page = response.read().decode("utf-8")
    if args.save_html:
        args.save_html.parent.mkdir(parents=True, exist_ok=True)
        with args.save_html.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(page)
    conversation = shared_conversation(page)
    if conversation["conversation_id"] != args.url.rsplit("/", 1)[-1]:
        parser.error("The page's conversation ID does not match the requested share URL")
    markdown, counts = export_markdown(conversation, args.url, now.isoformat(timespec="seconds"))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(markdown)
    print(json.dumps({"output": str(output), "messages": dict(counts), "bytes": len(markdown.encode()),
                      "sha256": sha256(markdown.encode()).hexdigest()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
