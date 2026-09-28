"""Run real package agents against deterministic model and evidence transports."""

import asyncio
import json

import httpx

from agent_chat.runtime import AGENTS

ANSWER = """## 找到了可以核对的依据

这是根据工具返回内容整理的回答。保留来源，才能继续追问。

| 观察 | 结果 | 来源 |
| --- | --- | --- |
| 接口响应 | 成功 | evidence |
| 会话上下文 | 已保留 | 当前会话 |

```python
def follow_evidence(question: str):
    return {"question": question, "evidence": "x" * 120}
```

[查看来源](https://example.org/evidence)
"""


class Packets(httpx.AsyncByteStream):
    def __init__(self, deltas):
        self.deltas = deltas

    async def __aiter__(self):
        for delta, stop in self.deltas:
            await asyncio.sleep(0.025)
            body = {"model": "fixture-model", "choices": [{"index": 0, "delta": delta, "finish_reason": stop}]}
            if stop:
                body["usage"] = {"prompt_tokens": 20, "completion_tokens": 10}
            yield ("data: " + json.dumps(body) + "\n\n").encode()
        yield b"data: [DONE]\n\n"


class FakeFactory:
    def __init__(self):
        self.calls = []
        self.agents = []

    def __call__(self, kind, config, credentials, storage):
        active = {"prompt": ""}

        async def model(request):
            body = json.loads(request.content)
            self.calls.append(body)
            messages = body["messages"]
            index = max(i for i, message in enumerate(messages) if message["role"] == "user")
            prompt = messages[index]["content"]
            active["prompt"] = prompt
            if "slow" in prompt and "tool" not in prompt:
                await asyncio.sleep(3600)
            if "failure" in prompt:
                return httpx.Response(500, text="fixture model failure")
            if not any(message["role"] == "tool" for message in messages[index + 1:]):
                tool, arguments = "web_fetch", {"requests": [{"url": "https://example.org/evidence"}]}
                if config["agent_options"].get("backend") == "rest":
                    tool, arguments = kind, {"requests": [{"path": "/repos/o/r"}]}
                    if "interim" in prompt:
                        arguments = {"requests": [{"path": "/search/repositories", "params": {"q": "evidence"}}]}
                if config["agent_options"].get("ptc"):
                    tool, arguments = "run_code", {"code": f"return await tools.{tool}({json.dumps(arguments)});"}
                call = {"tool_calls": [{"index": 0, "id": f"call-{len(self.calls)}", "type": "function",
                                        "function": {"name": tool, "arguments": json.dumps(arguments)}}]}
                key = credentials["api_key"]
                deltas = [({"reasoning_content": "先核对原始资料。"}, None)]
                if "secret" in prompt:
                    deltas += [({"reasoning_content": key[:5]}, None), ({"reasoning_content": key[5:]}, None)]
                if "interim" in prompt:
                    deltas.append(({"content": "阶段回答一：已找到初步线索。"}, None))
                deltas.append((call, "tool_calls"))
            elif "interim" in prompt and sum(message["role"] == "tool" for message in messages[index + 1:]) == 1:
                deltas = [({"reasoning_content": "继续核对第二处来源。"}, None)] * 60
                deltas.append(({"content": "阶段回答二：已交叉核对来源。"}, None))
                deltas.append(({"tool_calls": [{"index": 0, "id": f"call-{len(self.calls)}", "type": "function",
                    "function": {"name": "web_fetch", "arguments": json.dumps({"requests": [
                        {"url": "https://example.org/second-evidence"}]})}}]}, "tool_calls"))
            else:
                answer = ANSWER + ("\n已有上下文。" if index > 1 else "")
                if "long scroll" in prompt:
                    answer += "\n\n持续核对资料，阅读时应当保留当前滚动位置。" * 100
                if "secret" in prompt:
                    answer += credentials["api_key"]
                deltas = [({"reasoning_content": "资料已返回，整理可验证的结论。"}, None)]
                if "interim" in prompt:
                    deltas *= 60
                deltas += [({"content": answer[i:i + 25]}, None) for i in range(0, len(answer), 25)]
                deltas.append(({}, "stop"))
            if "no reasoning" in prompt:
                deltas = [(delta, stop) for delta, stop in deltas if "reasoning_content" not in delta]
            return httpx.Response(200, stream=Packets(deltas))

        async def source(request):
            if "slow tool" in active["prompt"]:
                await asyncio.sleep(3600)
            evidence = "evidence"
            if "secret" in active["prompt"]:
                evidence += " " + credentials["api_key"]
            if request.url.host == "example.org":
                return httpx.Response(200, text=f"<html><body><h1>{evidence}</h1></body></html>",
                                      headers={"Content-Type": "text/html"})
            if request.url.path == "/search/repositories":
                return httpx.Response(200, json={"total_count": 35, "items": [
                    {"id": index, "full_name": f"o/{name}", "html_url": f"https://github.com/o/{name}",
                     "description": "可核对的仓库说明。保留原始文档、版本信息与相关讨论，便于继续核对。"}
                    for index, name in enumerate(("r", "research", "references"), 1)
                ]})
            return httpx.Response(200, json={"id": 1, "name": evidence, "html_url": "https://github.com/o/r",
                                            "description": "可核对的仓库说明。"})

        connection = {"api_key": credentials["api_key"], "model_transport": httpx.MockTransport(model),
                      "web_transport": httpx.MockTransport(source),
                      "brave_api_key": credentials.get("brave_api_key", "")}
        if kind in {"github", "gitcode"}:
            connection[kind + "_token"] = credentials.get(kind + "_token", "")
            connection[kind + "_transport"] = httpx.MockTransport(source)
        agent = AGENTS[kind](config, storage, **connection)
        self.agents.append(agent)
        return agent
