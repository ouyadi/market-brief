#!/bin/bash
set -euo pipefail

PLIST_DST="$HOME/Library/LaunchAgents/com.discord-selfbot.daemon.plist"
LABEL="com.discord-selfbot.daemon"
UID_NUM=$(id -u)

if [ "$EUID" -eq 0 ]; then
    echo "ERROR: do NOT run with sudo."
    exit 1
fi

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || echo "    not loaded"
rm -f "$PLIST_DST"
echo "✓ Uninstalled. Logs preserved at ~/Library/Logs/discord-selfbot-mcp/"
echo "  Token preserved at ~/.discord-selfbot.env"
