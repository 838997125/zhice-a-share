#!/bin/bash
# apply-patches.sh — 将 patches/ 下的补丁应用到 tradingagents 包
#
# 用法：
#   1. 先 pip install tradingagents==0.7.0
#   2. 然后运行 bash patches/apply-patches.sh
#
# 补丁基于 tradingagents 0.7.0 原版生成，修复以下问题：
#   - fix-indicator-descriptions-in-data.patch: 技术指标说明文本不再混入数据值
#   - fix-research-manager-no-data-dump.patch: Research Manager 禁止复制原始数据
#   - fix-trader-no-data-dump.patch: Trader 禁止复制原始数据

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PATCH_DIR="$SCRIPT_DIR"

# 自动查找 tradingagents 包路径（兼容 venv 和系统 python）
# 优先使用 PYTHON 环境变量，其次尝试 python3 / python
find_pkg() {
    local py_bin="${PYTHON:-}"
    [ -z "$py_bin" ] && py_bin=$(command -v python3 2>/dev/null) || true
    [ -z "$py_bin" ] && py_bin=$(command -v python 2>/dev/null) || true
    [ -z "$py_bin" ] && return 1
    "$py_bin" -c "import tradingagents; print(tradingagents.__path__[0])" 2>/dev/null
}
PKG_DIR=$(find_pkg) || {
    echo "❌ 找不到 tradingagents 包，请先 pip install tradingagents==0.7.0"
    echo "   如果使用 venv，请先 activate 或设置 PYTHON 环境变量，例如:"
    echo "   PYTHON=/opt/tradingagents/bin/python bash patches/apply-patches.sh"
    exit 1
}

echo "📍 tradingagents 包路径: $PKG_DIR"
echo ""

# 补丁文件列表
PATCHES=(
    "fix-indicator-descriptions-in-data.patch"
    "fix-research-manager-no-data-dump.patch"
    "fix-trader-no-data-dump.patch"
)

APPLIED=0
SKIPPED=0
FAILED=0

for patch_file in "${PATCHES[@]}"; do
    patch_path="$PATCH_DIR/$patch_file"
    
    if [ ! -f "$patch_path" ]; then
        echo "⚠️  补丁文件不存在: $patch_file"
        FAILED=$((FAILED + 1))
        continue
    fi
    
    # 尝试应用补丁（先 dry-run 检查是否已应用）
    if patch --dry-run -p1 -d "$PKG_DIR/.." < "$patch_path" >/dev/null 2>&1; then
        # 尚未应用，执行补丁
        if patch -p1 -d "$PKG_DIR/.." < "$patch_path" >/dev/null 2>&1; then
            echo "✅ 已应用: $patch_file"
            APPLIED=$((APPLIED + 1))
        else
            echo "❌ 应用失败: $patch_file"
            FAILED=$((FAILED + 1))
        fi
    else
        # 检查是否已经应用过
        if patch --dry-run -p1 -R -d "$PKG_DIR/.." < "$patch_path" >/dev/null 2>&1; then
            echo "⏭️  已应用过，跳过: $patch_file"
            SKIPPED=$((SKIPPED + 1))
        else
            echo "❌ 无法应用（文件可能已被修改）: $patch_file"
            FAILED=$((FAILED + 1))
        fi
    fi
done

echo ""
echo "📊 结果: 应用 $APPLIED, 跳过 $SKIPPED, 失败 $FAILED"

if [ "$FAILED" -gt 0 ]; then
    exit 1
fi
