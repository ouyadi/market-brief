#!/bin/bash
# Register the two LAN-hosted MCP servers with this machine's Claude Code.
# Reads the shared Bearer token from ./token (committed to this private repo).
# Idempotent — re-running just rewrites the same entries.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKEN_FILE="$HERE/token"

GATEWAY_HOST="${GATEWAY_HOST:-<gateway-host>}"
GATEWAY_PORT="${GATEWAY_PORT:-7777}"

if [ ! -f "$TOKEN_FILE" ]; then
    echo "ERROR: $TOKEN_FILE missing. Pull the repo again or copy it from the host."
    exit 2
fi

TOKEN=$(tr -d '[:space:]' < "$TOKEN_FILE")
BASE="http://${GATEWAY_HOST}:${GATEWAY_PORT}"

# Probe before we touch ~/.claude.json
echo "==> probing gateway at $BASE ..."
status=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$BASE/discord/mcp" || echo "000")
case "$status" in
    401) echo "    OK: $BASE reachable, requires auth (expected)." ;;
    000) echo "ERROR: cannot reach $BASE. Wrong network? Host down? Port closed?"; exit 3 ;;
    *)   echo "WARN: unexpected status $status (continuing anyway)." ;;
esac

if ! command -v claude >/dev/null 2>&1; then
    echo "ERROR: 'claude' CLI not on PATH. Install Claude Code first."
    exit 4
fi

# Remove any previous entries with the same names (loopback or LAN), then add fresh.
echo "==> registering with Claude Code (user scope) ..."
claude mcp remove --scope user discord-selfbot 2>/dev/null || true
claude mcp remove --scope user chatlog          2>/dev/null || true

claude mcp add --transport http --scope user \
    discord-selfbot "$BASE/discord/mcp" \
    --header "Authorization: Bearer $TOKEN"

claude mcp add --transport http --scope user \
    chatlog "$BASE/chatlog/mcp" \
    --header "Authorization: Bearer $TOKEN"

echo
echo "==> done. Restart Claude Code, then run /mcp inside a session to confirm both"
echo "    'discord-selfbot' and 'chatlog' show as connected."
