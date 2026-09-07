"""Connect ordinary web tools to the surrounding environment's search and fetch backend.

Requests and responses cross a JSON-lines transport without query rewriting or
answer synthesis. The runner owns transport lifetime; tool outputs enter the
normal BaseAgent observations. Live web evidence is independent of Graphub's
offline index boundary.
"""

import asyncio
import json
import sys
import termios
import tty

from .agent import Tool


class Web:
    def __init__(self):
        self.sequence = 0
        self.terminal = None
        self.reader = asyncio.StreamReader(limit=16 << 20)

    async def __aenter__(self):
        if sys.stdin.isatty():
            self.terminal = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin)  # Large web replies exceed a terminal's canonical line buffer.
        self.transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self.reader), sys.stdin,
        )
        return self

    async def __aexit__(self, *_exc):
        if self.terminal is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.terminal)
        self.transport.close()

    async def exchange(self, arguments: dict) -> str:
        """Request one backend operation and retain the exact returned evidence.

        Args:
            arguments: Native web backend parameters. The caller sends the returned
                JSON-lines request to its web tool and replies with the same id and
                either result or error. No automatic retry consumes an extra request.
        """
        self.sequence += 1
        print(json.dumps({"graphub_web": self.sequence, "arguments": arguments}), flush=True)
        line = await self.reader.readline()
        if not line:
            raise ConnectionError("Web backend transport closed")
        reply = json.loads(line)
        if reply["id"] != self.sequence:
            raise ValueError("Web reply does not match its request")
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"] if isinstance(reply["result"], str) else json.dumps(reply["result"], ensure_ascii=False)

    def tools(self):
        """Return the shared WebSearch and WebFetch definitions."""
        async def search(args):
            query = {"q": args["query"]}
            if args.get("domains"):
                query["domains"] = args["domains"]
            return await self.exchange({"search_query": [query], "response_length": "long"})

        async def fetch(args):
            page = {"ref_id": args["url"]}
            if "line" in args:
                page["lineno"] = args["line"]
            return await self.exchange({"open": [page], "response_length": "long"})

        return [
            Tool("WebSearch", "Search the public web and return source-linked results.", {
                "type": "object", "properties": {"query": {"type": "string"},
                    "domains": {"type": "array", "items": {"type": "string"}}},
                "required": ["query"], "additionalProperties": False,
            }, search),
            Tool("WebFetch", "Read a web page by URL; optionally start at a reported line number.", {
                "type": "object", "properties": {"url": {"type": "string"},
                    "line": {"type": "integer", "minimum": 0}},
                "required": ["url"], "additionalProperties": False,
            }, fetch),
        ]
