#!/bin/bash
# Start the discord-selfbot MCP server in foreground.
# Used both for manual runs and by the launchd daemon.

set -euo pipefail

PROJECT_DIR="$HOME/discord-selfbot-mcp"
ENV_FILE="$HOME/.discord-selfbot.env"

cd "$PROJECT_DIR"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE missing. Copy .env.example there, set DISCORD_USER_TOKEN, chmod 600."
    exit 2
fi

if [ ! -d ".venv" ]; then
    echo "==> First run: creating uv venv and installing deps"
    $HOME/.local/bin/uv venv .venv
    $HOME/.local/bin/uv pip install --python .venv/bin/python -e .
fi

exec .venv/bin/python server.py
