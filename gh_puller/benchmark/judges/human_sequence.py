"""human sequence judge：逐题人工评审的半抽象基础设施。

在 sequence judge 基础上提供 web 交互：本地起评审页面，逐题展示
（题目 + 参考答案要点 + 参赛方回答），评审者填写表单并点击提交，表单数据
即本题评判结果。扩展点有二：题目序列（继承 SequenceJudge，子类 override
load_questions() 规定数据源）与评审表单 schema（judge_schema，JSON Schema，
web UI 按其渲染表单）。本类不定义数据源与表单语义，均由子类规定。
"""

import asyncio

import jsonschema
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from gh_puller.benchmark.judges.sequence import SequenceJudge
from gh_puller.benchmark.types import Answer

PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>HumanSequenceJudge 评审</title>
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
    document.getElementById("idx").textContent = `第 ${current + 1} / ${st.total} 题`;
    document.getElementById("question").textContent = st.question;
    document.getElementById("ref").textContent = st.ref_answer.join("、");
    document.getElementById("answer").textContent = st.answer;
    renderForm();
    document.getElementById("form-wrap").hidden = false;
    document.getElementById("status").textContent = "";
  } else if (document.getElementById("form-wrap").hidden) {
    document.getElementById("status").textContent =
      st.index === lastSubmitted ? "已提交，等待下一题…" : "等待题目…";
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
    document.getElementById("status").textContent = "已提交，等待下一题…";
  } else {
    document.getElementById("status").textContent = "提交失败：" + (await r.json()).detail;
  }
}
setInterval(poll, 500);
poll();
</script>
</body>
</html>"""


class HumanSequenceJudge(SequenceJudge):
    """半抽象基类：子类 = 题目序列（load_questions）+ 评审表单 schema。"""

    # 扩展点：评审表单 JSON Schema（子类必定义；web UI 据此渲染表单，提交数据即本题评判）
    judge_schema: dict = {}
    judge_name: str = "human-sequence"
    host: str = "127.0.0.1"
    port: int = 8002  # 与参赛方默认端口（8001）区分

    def __init__(self):
        self._connected = asyncio.Event()  # 前端页面建立连接
        self._current: dict = {}  # 当前待评审题（index/question/ref_answer/answer）
        self._fut: asyncio.Future | None = None  # 当前题的提交 future
        self._count = 0  # 已提交题数（兼当前题索引）
        self._total = 0  # 题目总数（/state 展示用）

    async def __call__(self, ask) -> dict:
        app = self._make_app()
        server = uvicorn.Server(uvicorn.Config(app, host=self.host, port=self.port, log_level="warning"))
        serve_task = asyncio.create_task(server.serve())
        while not server.started:  # 端口监听就绪后再打印 URL
            if serve_task.done():  # 启动失败（如端口占用）则抛出
                await serve_task
            await asyncio.sleep(0.05)
        print(f"[{self.judge_name}] 评审页面：http://{self.host}:{self.port}（浏览器打开后开始逐题评审）", flush=True)
        try:
            await self._connected.wait()  # 等待前端建立连接
            self._total = len(self.load_questions())
            return await super().__call__(ask)  # 复用逐题循环：ask → judge_one（等前端提交）
        finally:
            server.should_exit = True
            await serve_task

    async def judge_one(self, q, a: Answer) -> dict:
        """单题评审：页面展示本题，等待评审者提交表单（数据已按 judge_schema 校验）；结果 = 上下文四字段 + judgment（表单数据）。"""
        self._current = {
            "index": self._count,
            "question": self.question_text(q),
            "ref_answer": list(q["ref_answer"]),
            "answer": a.text,
        }
        self._fut = asyncio.get_running_loop().create_future()
        data = await self._fut  # POST /submit 校验通过后 resolve
        self._count += 1
        return {
            "id": q.get("id", ""),
            "question": self._current["question"],
            "ref_answer": self._current["ref_answer"],
            "answer": self._current["answer"],
            "judgment": data,  # 评审者提交的表单数据（已按 judge_schema 校验）
        }

    def _make_app(self) -> FastAPI:
        """构建评审页面与交互接口（闭包引用本实例）。"""
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
                return {"index": self._current["index"], "total": self._total, **self._current}
            return {"index": -1, "total": self._total}

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
