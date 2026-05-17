#!/bin/bash
# Install the LAN MCP gateway as a per-user launchd agent.
# Reads ./token, bakes it into the plist's LAN_MCP_TOKEN env var, loads launchd.
# No sudo needed — this is a user agent listening on a high port.

set -euo pipefail

PROJECT_DIR="$HOME/mcp-gateway"
PLIST_SRC="$PROJECT_DIR/com.mcp-gateway.daemon.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.mcp-gateway.daemon.plist"
LOG_DIR="$HOME/Library/Logs/mcp-gateway"
LABEL="com.mcp-gateway.daemon"
TOKEN_FILE="$PROJECT_DIR/token"

if [ "$EUID" -eq 0 ]; then
    echo "ERROR: do NOT run with sudo. This is a per-user agent."
    exit 1
fi

if [ ! -x /opt/homebrew/bin/caddy ]; then
    echo "ERROR: /opt/homebrew/bin/caddy missing. Install with: brew install caddy"
    exit 2
fi

if [ ! -f "$TOKEN_FILE" ]; then
    echo "==> Generating fresh 32-byte token at $TOKEN_FILE"
    openssl rand -hex 32 > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
fi

TOKEN=$(cat "$TOKEN_FILE")

mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

LAN_MCP_TOKEN="$TOKEN" /opt/homebrew/bin/caddy validate \
    --config "$PROJECT_DIR/Caddyfile" --adapter caddyfile

# Bake the token into the destination plist (source uses a placeholder)
sed "s|__TOKEN_REPLACE_ME__|$TOKEN|g" "$PLIST_SRC" > "$PLIST_DST"
chmod 644 "$PLIST_DST"

UID_NUM=$(id -u)
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"
launchctl enable   "gui/$UID_NUM/$LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo "==> Installed. Waiting for :7777 ..."
for i in {1..15}; do
    if nc -z 127.0.0.1 7777 2>/dev/null; then
        echo "    gateway listening on :7777"
        break
    fi
    sleep 1
done

echo
echo "Token (share with client machines):"
echo "    $TOKEN"
echo
echo "Test (from host):"
echo "    curl -sS -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:7777/discord/mcp                       # expect 401"
echo "    curl -sS -o /dev/null -w '%{http_code}\\n' -H \"Authorization: Bearer \$TOKEN\" \\"
echo "         -X POST -H 'Content-Type: application/json' \\"
echo "         -H 'Accept: application/json, text/event-stream' \\"
echo "         -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"clientInfo\":{\"name\":\"curl\",\"version\":\"0\"}}}' \\"
echo "         http://127.0.0.1:7777/discord/mcp                                                              # expect 200"
echo
echo "Uninstall:    launchctl bootout gui/$UID_NUM/$LABEL && rm $PLIST_DST"
