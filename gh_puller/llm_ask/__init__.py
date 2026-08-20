"""内置方法:llm_ask(用 LLM 包装的 ask 路由)。

纯 LLM 问答:POST /ask 收到 question 后直接调 vLLM OpenAI 兼容端点生成回答,无额外路由逻辑。
配置入口:本方法 env.py(LLM_ASK_URL / LLM_ASK_MODEL,可环境变量覆盖)。

启动与自测:
    uv run uvicorn gh_puller.llm_ask.server:app --port 8001
    curl -s -X POST http://localhost:8001/ask -H 'Content-Type: application/json' -d '{"question":"ping"}'
"""
