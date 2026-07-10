#!/bin/bash
# 智策 Web 启动脚本
# 用法: bash start_web.sh [port]
PORT=${1:-8460}
cd "$(dirname "$0")"

# 使用 /opt/tradingagents venv
VENV_PYTHON="/opt/tradingagents/bin/python"
if [ ! -f "$VENV_PYTHON" ]; then
    echo "❌ venv python not found: $VENV_PYTHON"
    exit 1
fi

echo "🚀 启动智策 Web 服务 (端口 $PORT)..."
echo "   前端页面: http://0.0.0.0:$PORT"
echo "   股票搜索: http://0.0.0.0:$PORT/api/search?keyword=茅台"
echo "   分析接口: http://0.0.0.0:$PORT/api/analyze?ticker=600519&date=2025-06-16"
echo ""
exec "$VENV_PYTHON" -m uvicorn webapp:app --host 0.0.0.0 --port "$PORT" --log-level info
