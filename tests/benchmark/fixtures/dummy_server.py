"""Provide a fixture contestant implementing the benchmark REST protocol.

The fixture exercises the evaluation pipeline locally without a real backend. It is
test support rather than a framework component or the subject under evaluation.

Start with the valid ``/ask`` route:
    uv run uvicorn tests.benchmark.fixtures.dummy_server:app --port 8001
    uv run benchmark tests/benchmark/judges/vllm_bank.py --url http://localhost:8001

Set ``DUMMY_NO_ASK=1`` to exercise the missing-route failure path:
    DUMMY_NO_ASK=1 uv run uvicorn tests.benchmark.fixtures.dummy_server:app --port 8002
"""

import os

from fastapi import FastAPI

app = FastAPI()

if not os.environ.get("DUMMY_NO_ASK"):  # Omitting the route simulates an invalid contestant.


    @app.post("/ask")
    def ask(payload: dict) -> dict:
        # Echoing the question lets the heuristic judge match expected keywords.
        q = payload.get("question", "")
        return {"answer": f"关于「{q}」的回答如下：{q}", "sources": ["dummy:0001"]}
