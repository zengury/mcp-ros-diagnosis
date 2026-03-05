#!/bin/bash
# 启动 Manastone Diagnostic

echo "🚀 启动 Manastone Diagnostic..."

# 检查是否在 G1 Orin NX 上
if [[ $(uname -n) == "unitree-desktop" ]] || [[ $(uname -n) == "ubuntu" ]]; then
    echo "✅ 检测到 G1 Orin NX"
    MOCK_MODE="false"
else
    echo "⚠️ 未检测到 G1，使用模拟数据模式"
    MOCK_MODE="true"
fi

export MANASTONE_MOCK_MODE=$MOCK_MODE
export PYTHONPATH="${PYTHONPATH}:$(pwd)/src"

# 启动 MCP Server（后台）
echo "📡 启动 MCP Server..."
python3 -m manastone_diag.server &
SERVER_PID=$!
echo "   PID: $SERVER_PID"

sleep 2

# 启动 Web UI
echo "🌐 启动 Web UI..."
python3 -m manastone_diag.ui

# 清理
kill $SERVER_PID 2>/dev/null
echo "🛑 已停止"
