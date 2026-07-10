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

# 监控的关键文件类型
WATCH_EXTS="py html js css json md sh"

build_watch_filter() {
    local filter=""
    for ext in $WATCH_EXTS; do
        filter="$filter -e .$ext"
    done
    echo "$filter"
}

FILTER=$(build_watch_filter)

inotifywait -m -r \
    --exclude '__pycache__|\.git|\.pyc$|\.log$|nohup\.out|\.tmp$' \
    $FILTER \
    "$REPO_DIR" 2>>"$LOG_FILE" | while read -r directory event filename; do
    
    # 只关注修改和创建事件
    case "$event" in
        MODIFY|CREATE|MOVED_TO|DELETE|MOVED_FROM)
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检测到变更: ${directory}${filename} (${event})" >> "$LOG_FILE"
            
            # 防抖：等3秒看是否还有变更，然后执行一次提交
            # 用 flock 防止并发触发
            (
                flock -n 9 || exit 0
                sleep 3
                bash "$COMMIT_SCRIPT"
            ) 9>/tmp/zhice-autocommit.lock
            ;;
    esac
done
