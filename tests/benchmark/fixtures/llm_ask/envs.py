"""Define the self-contained runtime settings for the llm_ask fixture."""

import os

TIMEOUT = 3600.0  # Maximum time in seconds for an LLM call.

# Local OpenAI-compatible deployments may leave the API key empty.
LLM_ASK_URL = os.environ.get("LLM_ASK_URL", "http://localhost:8000/v1")
LLM_ASK_MODEL = os.environ.get("LLM_ASK_MODEL", "Qwen2.5-7B-Instruct")
LLM_ASK_API_KEY = os.environ.get("LLM_ASK_API_KEY", "")  # Empty values omit authorization.
