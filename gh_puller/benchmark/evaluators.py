"""评测器(自动化评测基础设施):LLM(vLLM API)/Claude Code(SDK)/Human(web 评审)。

评测器是底层静态工具,只有 evaluate 接口、无生命周期,被题库(上层)随意调用;
三种评测器(LLM/Claude/Human)结构兼容即可,无需继承(见 Evaluator)。
入参/输出约定由各实现自定(question/ref/answer 均为 Any),统一见各类 docstring:
- LLMEvaluator(半抽象基类):机制(HTTP 调用、解析失败重试、降级输出)由基类提供,
  请求体(payload)与判定解析是扩展点,由应用层题库以子类挂接,
  如 judges/vllm_mechanism/utils.py 的 auto_* 提示词与 coerce_verdict。
  模型地址与型号可用环境变量 LLM_JUDGE_URL / LLM_JUDGE_MODEL 覆盖,或由题库构造时传参。
- ClaudeEvaluator(半抽象基类):机制(SDK 会话、无状态逐题)由基类提供,
  agent 配置(options)、查询文本与判定解析是扩展点,由应用层题库以子类挂接,
  如 judges/vllm_mechanism/utils.py 的 auto_* 提示词与 MCP_SERVERS/SKILLS。
  模型可用环境变量 CLAUDE_JUDGE_MODEL 覆盖(缺省用 SDK 默认模型),需要 ANTHROPIC_API_KEY。
- HumanEvaluator:入参三字符串,输出评审表单数据(结构由 judge_schema 定义);
  server 为其内部实现细节:首次 evaluate 时惰性起服并等待前端连接,后续复用,
  不对上层暴露生命周期。
任一失败不得抛出,降级输出 {"dimensions": {}, "overall": 0, "reason": "评测失败: ..."}。
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

from gh_puller.agent import cc_result, llm_complete
from gh_puller.envs import CLAUDE_JUDGE_MODEL, LLM_JUDGE_API_KEY, LLM_JUDGE_MODEL, LLM_JUDGE_URL
from gh_puller.envs import TIMEOUT as GLOBAL_TIMEOUT

__all__ = ["Evaluator", "LLMEvaluator", "ClaudeEvaluator", "HumanEvaluator"]

# 单题评分超时:connect 短(端点不可达时快速降级),read 取全局单题超时上限
TIMEOUT = httpx.Timeout(connect=5.0, read=GLOBAL_TIMEOUT, write=30.0, pool=5.0)


class Evaluator(Protocol):
    """评测器协议。

    name: 评测器标识,写入 judgment["evaluator"]。
    evaluate: 评判单题,返回 JSON 可序列化 dict。题库负责拆字段、组装上下文,
    把返回 dict 原样放进 judgment。
    """

    name: str

    async def evaluate(self, question: Any, ref: Any, answer: Any) -> dict:
        ...


class LLMEvaluator:
    """vLLM 服务上的评分模型逐题评分(半抽象基类:请求体由题库子类提供)。"""

    name = "llm"

    # 扩展点:解析失败重试时向 payload["messages"] 追加的提示(机制默认,题库可覆盖)
    retry_nudge: str = "只输出 JSON,不要任何其他内容。"

    def __init__(self, url: str = "", model: str = ""):
        self.url = url or LLM_JUDGE_URL
        self.model = model or LLM_JUDGE_MODEL

    def make_payload(self, question: str, ref: str, answer: str) -> dict:
        """chat/completions 请求体组装(OpenAI 兼容契约:须含可追加的 messages 键);题库子类必须提供。"""
        raise NotImplementedError

    def coerce(self, data) -> dict:
        """判定规范化(维度补齐/限幅);题库子类必须提供。"""
        raise NotImplementedError

    async def evaluate(self, question: str, ref: str, answer: str) -> dict:
        payload = self.make_payload(question, ref, answer)
        headers = {"Authorization": f"Bearer {LLM_JUDGE_API_KEY}"} if LLM_JUDGE_API_KEY else None
        last_err: Exception | None = None
        for nudge in (False, True):  # 解析失败重试 1 次(第二次追加"只输出 JSON"提示)
            if nudge:
                payload["messages"].append({"role": "user", "content": self.retry_nudge})
            try:
                content = await llm_complete(
                    url=self.url, payload=payload, api_key=LLM_JUDGE_API_KEY,
                    timeout=TIMEOUT, headers=headers, session_name="judge:llm",
                )
                return self.coerce(json.loads(content))
            except Exception as e:  # 网络/HTTP/解析失败:继续下一轮,耗尽后降级
                last_err = e
        return {"dimensions": {}, "overall": 0, "reason": f"评测失败: {type(last_err).__name__}: {last_err}"}


class ClaudeEvaluator:
    """headless Claude agent 逐题评分(半抽象基类:agent 配置由题库子类提供)。"""

    name = "claude"

    def __init__(self, model: str = ""):
        self.model = model or CLAUDE_JUDGE_MODEL

    def make_options(self, question: str, ref: str, answer: str) -> ClaudeAgentOptions:
        """agent 配置组装(system_prompt/工具授权/模型等);题库子类必须提供。"""
        raise NotImplementedError

    def user_prompt(self, question: str, ref: str, answer: str) -> str:
        """单题请求文本(query);题库子类必须提供。"""
        raise NotImplementedError

    def coerce(self, data) -> dict:
        """判定规范化(维度补齐/限幅);题库子类必须提供。"""
        raise NotImplementedError

    async def evaluate(self, question: str, ref: str, answer: str) -> dict:
        try:
            result = await cc_result(
                self.make_options(question, ref, answer), self.user_prompt(question, ref, answer),
                session_name="judge:claude",
            )
            return self.coerce(json.loads(result))
        except Exception as e:  # SDK/解析异常:降级输出,不抛出
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
</html>"""


class HumanEvaluator:
    """逐题人工评审:题目/参考答案要点/参赛方回答展示于页面,表单提交数据即本题评判。"""

    name = "human"

    def __init__(self, judge_schema: dict, host: str = "127.0.0.1", port: int = 8002):
        self.judge_schema = judge_schema  # 评审表单 JSON Schema(web UI 按其渲染表单)
        self.host = host
        self.port = port  # 与参赛方默认端口(8001)区分
        self._ready = False  # server 是否已启动且前端已连接
        self._connected = asyncio.Event()  # 前端页面建立连接
        self._current: dict = {}  # 当前待评审题(question/ref/answer)
        self._fut: asyncio.Future | None = None  # 当前题的提交 future
        self._count = 0  # 已提交题数

    async def evaluate(self, question: str, ref: str, answer: str) -> dict:
        """展示本题并等待评审者提交;表单数据(已按 judge_schema 校验)即本题评判结果。"""
        await self._ensure_server()
        self._current = {
            "index": self._count,
            "question": question,
            "ref": ref,
            "answer": answer,
        }
        self._fut = asyncio.get_running_loop().create_future()
        data = await self._fut  # POST /submit 校验通过后 resolve
        self._count += 1
        return data

    async def _ensure_server(self) -> None:
        """惰性起服:启动 uvicorn 并等待前端建立连接;已就绪则 no-op。"""
        if self._ready:
            return
        app = self._make_app()
        server = uvicorn.Server(uvicorn.Config(app, host=self.host, port=self.port, log_level="warning"))
        task = asyncio.create_task(server.serve())
        while not server.started:  # 端口监听就绪后再打印 URL
            if task.done():  # 启动失败(如端口占用)则抛出
                await task
            await asyncio.sleep(0.05)
        print(f"[{self.name} evaluator] 评审页面:http://{self.host}:{self.port}(浏览器打开后开始逐题评审)", flush=True)
        await self._connected.wait()  # 等待前端建立连接
        self._ready = True

    def _make_app(self) -> FastAPI:
        """构建评审页面与交互接口(闭包引用本实例)。"""
        app = FastAPI()

        @app.get("/")
        async def page():
            return HTMLResponse(PAGE)

        @app.get("/schema")
        async def schema() -> dict:
            return self.judge_schema

        @app.get("/state")
        async def state() -> dict:
            self._connected.set()  # 页面首次轮询即视为连接建立
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
