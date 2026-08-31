#!/usr/bin/env bash
# 安装 codebase-memory-mcp(DeusData),但不对任何现有 agent 做配置。
# 官方安装器 -h 选项:
#   --dir PATH       安装目录(默认 ~/.local/bin)
#   --clients LIST   只配置指定的 client 列表(如 --clients=claude)
#   --skip-config    跳过 agent 自动配置(仅装二进制)
# 本脚本默认传 --skip-config,需要改配置时去掉它再跑一遍即可。
set -euo pipefail

INSTALLER_URL="https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh"
TMP_INSTALLER="$(mktemp)"
trap 'rm -f "$TMP_INSTALLER"' EXIT

curl -fsSL "$INSTALLER_URL" -o "$TMP_INSTALLER"
bash "$TMP_INSTALLER" --skip-config "$@"
