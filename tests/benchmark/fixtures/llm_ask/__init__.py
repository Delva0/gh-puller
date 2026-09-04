"""Provide the built-in LLM-backed ``/ask`` fixture.

POST requests forward questions directly to an OpenAI-compatible endpoint. See
``envs.py`` for environment-controlled endpoint and model settings.

Start and probe the fixture:
    uv run uvicorn tests.llm_ask.server:app --port 8001
    curl -s -X POST http://localhost:8001/ask -H 'Content-Type: application/json' -d '{"question":"ping"}'
"""
