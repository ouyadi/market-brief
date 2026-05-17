#!/bin/bash
# Install discord-selfbot MCP as a per-user launchd agent.
# Runs as the current user (no root needed). Auto-starts on login.

set -euo pipefail

PROJECT_DIR="$HOME/discord-selfbot-mcp"
PLIST_SRC="$PROJECT_DIR/com.discord-selfbot.daemon.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.discord-selfbot.daemon.plist"
LOG_DIR="$HOME/Library/Logs/discord-selfbot-mcp"
LABEL="com.discord-selfbot.daemon"
ENV_FILE="$HOME/.discord-selfbot.env"

if [ "$EUID" -eq 0 ]; then
    echo "ERROR: do NOT run with sudo. This is a per-user agent."
    exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE missing."
    echo "  cp $PROJECT_DIR/.env.example $ENV_FILE"
    echo "  chmod 600 $ENV_FILE"
    echo "  # then edit and put DISCORD_USER_TOKEN=..."
    exit 2
fi

# Refuse to install if the env file isn't 600 — the token is sensitive.
PERMS=$(stat -f '%Lp' "$ENV_FILE")
if [ "$PERMS" != "600" ]; then
    echo "ERROR: $ENV_FILE is mode $PERMS, must be 600. Fix with: chmod 600 $ENV_FILE"
    exit 3
fi

mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

UID_NUM=$(id -u)

# Bootout if already loaded
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true

cp "$PLIST_SRC" "$PLIST_DST"
chmod 644 "$PLIST_DST"

launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"
launchctl enable "gui/$UID_NUM/$LABEL"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

echo "==> Installed. Waiting for HTTP server to come up..."
HOST=$(grep -E '^MCP_HOST=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 | tr -d ' "' | head -1)
PORT=$(grep -E '^MCP_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 | tr -d ' "' | head -1)
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-6280}

for i in {1..20}; do
    if nc -z "$HOST" "$PORT" 2>/dev/null; then
        echo "    server listening on $HOST:$PORT"
        break
    fi
    sleep 1
done

echo
echo "Status:    launchctl print gui/$UID_NUM/$LABEL | head -30"
echo "Logs:"
echo "    $LOG_DIR/stdout.log"
echo "    $LOG_DIR/stderr.log"
echo
echo "Add to Claude Code (user scope):"
echo "    claude mcp add --transport http --scope user discord-selfbot http://$HOST:$PORT/mcp"
echo
echo "Uninstall:"
echo "    $PROJECT_DIR/uninstall-daemon.sh"
