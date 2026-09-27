#!/usr/bin/env bash
# Synchronize versioned vLLM sources and prebuild local CBM indexes once.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec uv --directory "$SCRIPT_DIR/.." run --frozen \
    python -m vllm_kb_adapter.update "$@"
