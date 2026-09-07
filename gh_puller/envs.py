"""Read process-wide configuration shared by ``gh_puller`` subpackages.

Values are captured at import time. Credentials and settings owned by one application
remain with that application or its underlying SDK.
"""

import os


def _path(name: str, default: str) -> str:
    return os.path.expanduser(os.environ.get(name, default))


# --- Agent adapters ---

DSH_HOME = _path("DSH_HOME", "~/.gh-puller/dsh-home")
DSH_BIN = _path("DSH_BIN", "")
DSH_SESSION_ROOT = _path("DSH_SESSION_ROOT", "~/.gh-puller/dsh-sessions")
# DSH loads .env from its runtime directory, so keep it outside task checkouts.
DSH_RUNTIME_CWD = _path("DSH_RUNTIME_CWD", "~/.gh-puller/dsh-runtime")


# --- DeepWiki ---

DEEPWIKI_ROOT = _path("DEEPWIKI_ROOT", "~/.gh-puller/deepwiki")
# Chat estimates tokens as four characters each and avoids a tokenizer dependency.
CHAT_TOKEN_LIMIT_ESTIMATE = int(os.environ.get("DEEPWIKI_CHAT_TOKEN_LIMIT", "7500"))


# --- Agent monitoring ---

# The file sink and monitor hub share this flat directory of session JSONL files.
AGENT_MONITOR_DIR = _path("AGENT_MONITOR_DIR", "~/.gh-puller/generator-sessions")
# Compact logs omit model deltas; raw logs retain the complete event sequence.
AGENT_MONITOR_FILE_RAW = os.environ.get("AGENT_MONITOR_FILE_RAW", "0") == "1"
AGENT_MONITOR_WEBUI_URL = os.environ.get("AGENT_MONITOR_WEBUI_URL", "ws://localhost:8765/ws")
# Zero disables heartbeats; the monitor lease should remain several times longer.
AGENT_MONITOR_HEARTBEAT_SECS = int(os.environ.get("AGENT_MONITOR_HEARTBEAT_SECS", "30"))
AGENT_MONITOR_PHOENIX_URL = os.environ.get("AGENT_MONITOR_PHOENIX_URL", "http://localhost:6006/")
OTEL_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "gh-puller")


# --- Benchmark ---

TIMEOUT = 3600.0  # Shared limit for participant requests and evaluator calls.
JUDGE_EVALUATOR = os.environ.get("JUDGE_EVALUATOR", "llm")
LLM_JUDGE_URL = os.environ.get("LLM_JUDGE_URL", "http://localhost:8000/v1")
LLM_JUDGE_MODEL = os.environ.get("LLM_JUDGE_MODEL", "Qwen2.5-7B-Instruct")
LLM_JUDGE_API_KEY = os.environ.get("LLM_JUDGE_API_KEY", "")
CLAUDE_JUDGE_MODEL = os.environ.get("CLAUDE_JUDGE_MODEL", "")
