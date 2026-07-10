#!/bin/bash
# 智策项目文件监控守护进程
# 监控 /opt/tradingagents-app 代码文件变更，触发自动提交
# 由 systemd service 管理

set -euo pipefail

REPO_DIR="/opt/tradingagents-app"
COMMIT_SCRIPT="$REPO_DIR/auto-commit.sh"
LOG_FILE="/var/log/zhice-autocommit.log"

mkdir -p "$(dirname "$LOG_FILE")"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 文件监控守护进程启动" >> "$LOG_FILE"

# 监控文件修改、创建、删除、移动事件
# 用 --excludei 排除不关心的目录/文件
inotifywait -m -r \
    -e modify,create,delete,move \
    --excludei '__pycache__|\.git/|\.pyc$|\.log$|nohup\.out|\.tmp$|\.bak$' \
    "$REPO_DIR" 2>>"$LOG_FILE" | while read -r directory event filename; do
    
    # 只关注代码文件
    case "$filename" in
        *.py|*.html|*.js|*.css|*.json|*.md|*.sh)
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检测到变更: ${directory}${filename} (${event})" >> "$LOG_FILE"
            
            # 防抖：用 flock 防止并发，等3秒无新变更再提交
            (
                flock -n 9 || exit 0
                sleep 3
                bash "$COMMIT_SCRIPT"
            ) 9>/tmp/zhice-autocommit.lock
            ;;
    esac
done
