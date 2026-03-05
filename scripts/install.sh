#!/bin/bash
# Manastone Diagnostic 安装脚本
# 运行在 G1 Orin NX 上

set -e

echo "🔧 Manastone Diagnostic 安装脚本"
echo "=================================="

# 检查系统
if [[ $(uname -m) != "aarch64" ]]; then
    echo "⚠️ 警告: 非 ARM64 架构，建议在 G1 Orin NX 上运行"
fi

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 未安装"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2 | cut -d'.' -f1,2)
echo "✅ Python 版本: $PYTHON_VERSION"

# 安装 Python 依赖
echo ""
echo "📦 安装 Python 依赖..."
pip3 install -e . || pip install -e .

# 创建目录
echo ""
echo "📁 创建数据目录..."
mkdir -p ~/.manastone/{logs,cache,models}

# 下载本地 LLM 模型（可选）
echo ""
echo "🤖 检查本地 LLM 模型..."
MODEL_DIR="./models"
MODEL_FILE="$MODEL_DIR/qwen2.5-7b-instruct-q4_k_m.gguf"

if [ -f "$MODEL_FILE" ]; then
    echo "✅ 模型已存在: $MODEL_FILE"
else
    echo "⬇️ 需要下载 Qwen2.5-7B 模型 (~4.5GB)"
    echo "   下载地址: https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF"
    echo ""
    echo "   手动下载命令:"
    echo "   mkdir -p $MODEL_DIR"
    echo "   cd $MODEL_DIR"
    echo "   wget https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m.gguf"
    echo ""
    echo "   或使用 huggingface-cli:"
    echo "   huggingface-cli download Qwen/Qwen2.5-7B-Instruct-GGUF qwen2.5-7b-instruct-q4_k_m.gguf --local-dir $MODEL_DIR"
fi

# 编译 llama.cpp（可选）
echo ""
echo "🔨 检查 llama.cpp..."
if command -v llama-server &> /dev/null; then
    echo "✅ llama-server 已安装"
else
    echo "⚠️ llama-server 未找到"
    echo "   安装方法:"
    echo "   git clone https://github.com/ggerganov/llama.cpp"
    echo "   cd llama.cpp"
    echo "   cmake -B build -DLLAMA_CUDA=ON"
    echo "   cmake --build build --config Release -j$(nproc)"
    echo "   sudo cp build/bin/llama-server /usr/local/bin/"
fi

# 设置权限
echo ""
echo "🔐 设置权限..."
chmod +x scripts/*.sh 2>/dev/null || true

# 创建启动脚本
echo ""
echo "📝 创建启动脚本..."
cat > start.sh << 'EOF'
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
EOF

chmod +x start.sh

echo ""
echo "=================================="
echo "✅ 安装完成！"
echo ""
echo "启动命令:"
echo "  ./start.sh              # 启动全部服务"
echo "  manastone-diag          # 仅启动 MCP Server"
echo "  manastone-ui            # 仅启动 Web UI"
echo ""
echo "访问地址:"
echo "  Web UI:    http://localhost:7860"
echo "  MCP:       http://localhost:8080"
echo ""
echo "模拟模式:"
echo "  export MANASTONE_MOCK_MODE=true"
echo "  ./start.sh"
echo ""
