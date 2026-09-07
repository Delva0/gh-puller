#!/usr/bin/env bash
# Install the codebase-memory-mcp binary without changing agent configuration.
# Additional arguments are forwarded to the upstream installer.
set -euo pipefail

INSTALLER_URL="https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh"
TMP_INSTALLER="$(mktemp)"
trap 'rm -f "$TMP_INSTALLER"' EXIT

curl -fsSL "$INSTALLER_URL" -o "$TMP_INSTALLER"
bash "$TMP_INSTALLER" --skip-config "$@"
