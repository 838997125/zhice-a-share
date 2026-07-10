#!/bin/bash
# 智策项目自动提交脚本
# 当 /opt/tradingagents-app 有代码变更时自动 commit & push 到 GitHub
# 由 systemd path unit 或 inotifywait 触发

set -euo pipefail

REPO_DIR="/opt/tradingagents-app"
BRANCH="main"
LOG_FILE="/var/log/zhice-autocommit.log"
MAX_WAIT=30  # 文件变更后等待多少秒无新变更再提交

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

cd "$REPO_DIR"

# 等待文件写入稳定
sleep 3

# 检查是否有变更（排除 __pycache__、.pyc、日志等）
CHANGED=$(git status --porcelain -- \
    ':!__pycache__/' \
    ':!*.pyc' \
    ':!*.log' \
    ':!nohup.out' \
    ':!*.tmp' \
    ':!.env' \
    2>/dev/null)

if [ -z "$CHANGED" ]; then
    exit 0
fi

log "检测到代码变更："
log "$CHANGED"

# Stage 所有变更（排除敏感文件）
git add -A -- \
    ':!__pycache__/' \
    ':!*.pyc' \
    ':!*.log' \
    ':!nohup.out' \
    ':!*.tmp' \
    ':!.env'

# 生成提交信息
COMMIT_MSG="auto: $(date '+%Y-%m-%d %H:%M') 代码更新

变更文件:
$(echo "$CHANGED" | head -20)"

# Commit
if git diff --cached --quiet; then
    log "无实际变更需要提交"
    exit 0
fi

git commit -m "$COMMIT_MSG" 2>&1 | tee -a "$LOG_FILE"

# Push
log "推送到 GitHub..."
MAX_RETRIES=3
for i in $(seq 1 $MAX_RETRIES); do
    if git push origin "$BRANCH" 2>&1 | tee -a "$LOG_FILE"; then
        log "✅ 推送成功"
        exit 0
    else
        log "推送失败 (尝试 $i/$MAX_RETRIES)，等待重试..."
        sleep 5
    fi
done

log "❌ 推送失败，已重试 $MAX_RETRIES 次"
exit 1
