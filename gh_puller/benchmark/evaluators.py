"""Provide LLM, Claude Code, and browser-based benchmark evaluators.

Question banks specialize the automated evaluators through prompt, inference, and
verdict hooks. Automated failures produce a zero-score verdict instead of aborting the
benchmark.
"""

import asyncio
import json
from typing import Any, Protocol

import httpx
import jsonschema
import uvicorn
from claude_agent_sdk import ClaudeAgentOptions
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from gh_puller.agent import ClaudeCode, OpenAI
from gh_puller.envs import CLAUDE_JUDGE_MODEL, LLM_JUDGE_API_KEY, LLM_JUDGE_MODEL, LLM_JUDGE_URL
from gh_puller.envs import TIMEOUT as GLOBAL_TIMEOUT

__all__ = ["ClaudeEvaluator", "Evaluator", "HumanEvaluator", "LLMEvaluator"]

# Fail quickly on unreachable endpoints while allowing a full evaluation response.
TIMEOUT = httpx.Timeout(connect=5.0, read=GLOBAL_TIMEOUT, write=30.0, pool=5.0)


class Evaluator(Protocol):
    """Evaluate one answer and return a JSON-serializable verdict."""

    name: str

    async def evaluate(self, question: Any, ref: Any, answer: Any) -> dict:
        ...


class LLMEvaluator:
    """Evaluate answers with an OpenAI-compatible scoring model."""

    name = "llm"

    # Extension point for the second attempt after invalid JSON.
    retry_nudge: str = "只输出 JSON,不要任何其他内容。"

    def __init__(self, url: str = "", model: str = ""):
        self.url = url or LLM_JUDGE_URL
        self.model = model or LLM_JUDGE_MODEL

    def system_prompt(self, question: str, ref: str, answer: str) -> str:
        """Return the evaluator instruction, or no instruction by default."""
        return ""

    def user_prompt(self, question: str, ref: str, answer: str) -> str:
        """Build one evaluation turn; problem-specific subclasses must provide it."""
        raise NotImplementedError

    def request_parameters(self, question: str, ref: str, answer: str) -> dict:
        """Return OpenAI-compatible inference parameters outside messages and tools."""
        return {}

    def coerce(self, data) -> dict:
        """Normalize a model verdict; question-bank subclasses must implement it."""
        raise NotImplementedError

    async def evaluate(self, question: str, ref: str, answer: str) -> dict:
        prompt = self.user_prompt(question, ref, answer)
        system_prompt = self.system_prompt(question, ref, answer)
        parameters = self.request_parameters(question, ref, answer)
        headers = {"Authorization": f"Bearer {LLM_JUDGE_API_KEY}"} if LLM_JUDGE_API_KEY else None
        last_err: Exception | None = None
        for nudge in (False, True):  # Retry invalid output once with a JSON-only reminder.
            turn = f"{prompt}\n\n{self.retry_nudge}" if nudge else prompt
            try:
                llm = OpenAI(
                    {
                        "model": self.model,
                        "base_url": self.url,
                        "api_key": LLM_JUDGE_API_KEY,
                        "system_prompt": system_prompt,
                        "parameters": parameters,
                    },
                )
                async with llm.session(session_name="judge:llm"):
                    content = await llm.result(turn, timeout=TIMEOUT, headers=headers)
                return self.coerce(json.loads(content))
            except Exception as e:  # Exhaust both transport and parsing retries before degrading.
                last_err = e
        return {"dimensions": {}, "overall": 0, "reason": f"评测失败: {type(last_err).__name__}: {last_err}"}


class ClaudeEvaluator:
    """Evaluate answers in independent headless Claude Code sessions."""

    name = "claude"

    def __init__(self, model: str = ""):
        self.model = model or CLAUDE_JUDGE_MODEL

    def make_options(self, question: str, ref: str, answer: str) -> ClaudeAgentOptions:
        """Build SDK options; question-bank subclasses must implement it."""
        raise NotImplementedError

    def user_prompt(self, question: str, ref: str, answer: str) -> str:
        """Build one request; question-bank subclasses must implement it."""
        raise NotImplementedError

    def coerce(self, data) -> dict:
        """Normalize an SDK verdict; question-bank subclasses must implement it."""
        raise NotImplementedError

    async def evaluate(self, question: str, ref: str, answer: str) -> dict:
        try:
            options = self.make_options(question, ref, answer)
            config = vars(options) if hasattr(options, "__dict__") else dict(options)
            gen = ClaudeCode(config)
            async with gen.session(session_name="judge:claude"):
                result = await gen.result(self.user_prompt(question, ref, answer))
            return self.coerce(json.loads(result))
        except Exception as e:  # Evaluation failures become a verdict rather than aborting the run.
            return {"dimensions": {}, "overall": 0, "reason": f"评测失败: {type(e).__name__}: {e}"}


PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>HumanEvaluator 评审</title>
<style>
  body { font-family: sans-serif; max-width: 640px; margin: 40px auto; }
  .block { margin: 12px 0; padding: 12px; border: 1px solid #ccc; border-radius: 6px; }
  .q { font-weight: bold; }
  label { display: block; margin: 8px 0 2px; }
  input, select, textarea { width: 100%; box-sizing: border-box; padding: 4px; }
  button { margin-top: 12px; padding: 6px 24px; }
  #status { color: #666; }
</style>
</head>
<body>
<h3>逐题评审</h3>
<div id="status">等待题目…</div>
<div id="form-wrap" hidden>
  <div class="block"><div id="idx" class="q"></div><div id="question"></div></div>
  <div class="block"><div>参考答案要点</div><div id="ref"></div></div>
  <div class="block"><div>参赛方回答</div><div id="answer"></div></div>
  <form id="form" onsubmit="submitJudge(event)"></form>
</div>
<script>
let schema = null, current = -1, lastSubmitted = -2;
async function poll() {
  if (!schema) schema = await (await fetch("/schema")).json();
  const st = await (await fetch("/state")).json();
  if (st.index >= 0 && st.index !== current) {
    current = st.index;
    document.getElementById("idx").textContent = `第 ${current + 1} 题`;
    document.getElementById("question").textContent = st.question;
    document.getElementById("ref").textContent = st.ref;
    document.getElementById("answer").textContent = st.answer;
    renderForm();
    document.getElementById("form-wrap").hidden = false;
    document.getElementById("status").textContent = "";
  } else if (document.getElementById("form-wrap").hidden) {
    document.getElementById("status").textContent =
      st.index === lastSubmitted ? "已提交,等待下一题…" : "等待题目…";
  }
}
function renderForm() {
  const f = document.getElementById("form");
  f.innerHTML = "";
  for (const [name, prop] of Object.entries(schema.properties)) {
    const label = document.createElement("label");
    label.textContent = prop.title || name;
    f.appendChild(label);
    let el;
    if (prop.enum) {
      el = document.createElement("select");
      for (const v of prop.enum) {
        const o = document.createElement("option");
        o.value = v; o.textContent = v;
        el.appendChild(o);
      }
    } else if (prop.type === "integer" || prop.type === "number") {
      el = document.createElement("input"); el.type = "number";
    } else if (prop.type === "boolean") {
      el = document.createElement("input"); el.type = "checkbox";
    } else {
      el = document.createElement("input"); el.type = "text";
    }
    if (schema.required && schema.required.includes(name)) el.required = true;
    el.name = name;
    f.appendChild(el);
  }
  const b = document.createElement("button");
  b.textContent = "提交"; b.type = "submit";
  f.appendChild(b);
}
async function submitJudge(ev) {
  ev.preventDefault();
  const data = {};
  for (const el of ev.target.elements) if (el.name) {
    data[el.name] = el.type === "checkbox" ? el.checked
      : el.type === "number" ? Number(el.value) : el.value;
  }
  const r = await fetch("/submit", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
  if (r.ok) {
    lastSubmitted = current;
    document.getElementById("form-wrap").hidden = true;
    document.getElementById("status").textContent = "已提交,等待下一题…";
  } else {
    document.getElementById("status").textContent = "提交失败：" + (await r.json()).detail;
  }
}
setInterval(poll, 500);
poll();
</script>
</body>
</html>"""  # noqa: E501 - Embedded review page is kept as one literal.


class HumanEvaluator:
    """Collect one schema-validated human verdict per answer in a browser."""

    name = "human"

    def __init__(self, judge_schema: dict, host: str = "127.0.0.1", port: int = 8002):
        self.judge_schema = judge_schema
        self.host = host
        self.port = port
        self._ready = False
        self._connected = asyncio.Event()
        self._current: dict = {}
        self._fut: asyncio.Future | None = None
        self._count = 0

    async def evaluate(self, question: str, ref: str, answer: str) -> dict:
        """Display one answer and wait for its schema-validated verdict."""
        await self._ensure_server()
        self._current = {
            "index": self._count,
            "question": question,
            "ref": ref,
            "answer": answer,
        }
        self._fut = asyncio.get_running_loop().create_future()
        data = await self._fut
        self._count += 1
        return data

    async def _ensure_server(self) -> None:
        """Start the review server lazily and wait for its first browser client."""
        if self._ready:
            return
        app = self._make_app()
        server = uvicorn.Server(uvicorn.Config(app, host=self.host, port=self.port, log_level="warning"))
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():  # Propagate startup failures such as an occupied port.
                await task
            await asyncio.sleep(0.05)
        print(f"[{self.name} evaluator] 评审页面:http://{self.host}:{self.port}(浏览器打开后开始逐题评审)", flush=True)
        await self._connected.wait()
        self._ready = True

    def _make_app(self) -> FastAPI:
        """Build review routes closed over this evaluator's state."""
        app = FastAPI()

        @app.get("/")
        async def page():
            return HTMLResponse(PAGE)

        @app.get("/schema")
        async def schema() -> dict:
            return self.judge_schema

        @app.get("/state")
        async def state() -> dict:
            self._connected.set()
            if self._current:
                return {"index": self._current["index"], **self._current}
            return {"index": -1}

        @app.post("/submit")
        async def submit(request: Request):
            data = await request.json()
            try:
                jsonschema.validate(data, self.judge_schema)
            except jsonschema.ValidationError as e:
                return JSONResponse({"detail": e.message}, status_code=422)
            if self._fut is not None and not self._fut.done():
                self._fut.set_result(data)
            return {"ok": True}

        return app
