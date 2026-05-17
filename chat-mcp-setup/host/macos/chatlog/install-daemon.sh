#!/bin/bash
# 安装 chatlog 为 macOS launchd daemon
# 用法：sudo ./install-daemon.sh

set -euo pipefail

PLIST_SRC="$HOME/chatlog/com.chatlog.daemon.plist"
PLIST_DST="/Library/LaunchDaemons/com.chatlog.daemon.plist"
LOG_DIR="/var/log/chatlog"
LABEL="com.chatlog.daemon"

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: 必须以 sudo 运行（需要写 /Library/LaunchDaemons/）"
    echo "用法: sudo $0"
    exit 1
fi

echo "==> 1. 检查 chatlog 二进制"
if [ ! -x $HOME/chatlog/chatlog ]; then
    echo "ERROR: 未找到 $HOME/chatlog/chatlog"
    exit 1
fi

echo "==> 2. 创建日志目录 $LOG_DIR"
mkdir -p "$LOG_DIR"
chmod 755 "$LOG_DIR"

echo "==> 3. 检查是否已有 chatlog 在跑"
if pgrep -f '$HOME/chatlog/chatlog' > /dev/null 2>&1; then
    echo "    检测到已有 chatlog 进程，先停止它（你之前手动 sudo 启动的那个）"
    pkill -f '$HOME/chatlog/chatlog' || true
    sleep 2
fi

echo "==> 4. 卸载已有同名 daemon（如有）"
launchctl bootout system "$PLIST_DST" 2>/dev/null || true

echo "==> 5. 安装 plist 到 $PLIST_DST"
cp "$PLIST_SRC" "$PLIST_DST"
chown root:wheel "$PLIST_DST"
chmod 644 "$PLIST_DST"

echo "==> 6. 加载并启动 daemon"
launchctl bootstrap system "$PLIST_DST"
launchctl enable "system/$LABEL"
launchctl kickstart -k "system/$LABEL"

echo "==> 7. 等待 HTTP 服务就绪"
for i in {1..15}; do
    if curl -s --max-time 2 http://127.0.0.1:5030/health > /dev/null 2>&1; then
        echo "    HTTP 服务就绪"
        break
    fi
    sleep 1
done

echo
echo "✓ 安装完成"
echo
echo "状态查看："
echo "  sudo launchctl print system/$LABEL | head -30"
echo "  curl http://127.0.0.1:5030/health"
echo
echo "日志位置："
echo "  $LOG_DIR/stdout.log"
echo "  $LOG_DIR/stderr.log"
echo "  $LOG_DIR/script.log              # PTY 包装层日志"
echo "  $HOME/Documents/chatlog/log/chatlog.log  # chatlog 应用日志"
echo
echo "卸载："
echo "  sudo $HOME/chatlog/uninstall-daemon.sh"
