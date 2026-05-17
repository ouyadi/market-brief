#!/bin/bash
# 卸载 chatlog launchd daemon
# 用法：sudo ./uninstall-daemon.sh

set -euo pipefail

PLIST_DST="/Library/LaunchDaemons/com.chatlog.daemon.plist"
LABEL="com.chatlog.daemon"

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: 必须以 sudo 运行"
    echo "用法: sudo $0"
    exit 1
fi

echo "==> 1. 卸载 daemon"
launchctl bootout system "$PLIST_DST" 2>/dev/null || echo "    daemon 未在跑"

echo "==> 2. 删除 plist"
rm -f "$PLIST_DST"

echo "==> 3. 杀掉残留进程"
pkill -f '$HOME/chatlog/chatlog' || true

echo
echo "✓ 卸载完成。日志保留在 /var/log/chatlog/，需要可手动删。"
echo "  配置文件保留在 /var/root/.chatlog/，重新装时直接复用。"
