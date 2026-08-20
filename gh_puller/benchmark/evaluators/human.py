"""人类评测器:逐题人工评审(web 页面),从原 HumanSequenceJudge 迁移而来的静态评测器。

入参:question/ref/answer 均为 str(页面直接展示);输出评审表单数据
(结构由构造参数 judge_schema 定义,web UI 按其渲染表单,提交数据即本题评判结果)。
server 为其内部实现细节:首次 evaluate 时惰性起服并等待前端连接,后续复用,
不对上层暴露生命周期。
"""

import asyncio

import jsonschema
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

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
