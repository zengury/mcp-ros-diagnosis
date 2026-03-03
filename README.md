# Manastone Diagnostic

**G1 语义化运维诊断工具** - 运行在宇树 G1 机载 Orin NX 上的自包含诊断系统。

## 特性

- ✅ **完全离线可用** - 无需外部网络，零依赖
- 🤖 **本地 LLM** - 内置 Qwen2.5-7B，Orin NX 本地推理
- 🔧 **MCP Server** - 标准 MCP 协议，兼容 Claude Desktop/Cursor
- 🌐 **Web UI** - 浏览器访问，手机/平板即用
- 📊 **语义化诊断** - DDS 原始数据 → 自然语言解释

## 快速开始

### 1. 安装

```bash
# 在 G1 Orin NX 上执行
git clone <repo-url>
cd manastone-diagnostic

# 安装依赖
pip install -e .

# 下载本地 LLM 模型（可选，首次运行自动下载）
./scripts/install.sh
```

### 2. 启动

#### 模式 A：模拟数据模式（无真机测试）

```bash
export MANASTONE_MOCK_MODE=true
manastone-diag  # 启动 MCP Server
# 或
manastone-ui    # 启动 Web UI
```

#### 模式 B：连接真机

```bash
# 确保 G1 网络可达
ping 192.168.123.164

# 启动服务
manastone-diag
```

### 3. 访问

- **Web UI**: http://192.168.123.164:7860
- **MCP Server**: http://192.168.123.164:8080

## 项目结构

```
manastone-diagnostic/
├── src/manastone_diag/
│   ├── server.py          # MCP Server 入口
│   ├── ui.py              # Gradio Web UI
│   ├── config.py          # 配置管理
│   ├── dds_bridge/        # DDS 订阅与缓存
│   │   ├── subscriber.py
│   │   └── cache.py
│   ├── resources/         # MCP Resources
│   │   └── joints.py      # g1://joints/status
│   ├── tools/             # MCP Tools
│   ├── semantic/          # 语义化转换
│   ├── knowledge/         # Skill 库
│   ├── orchestrator/      # Agent 编排
│   ├── llm/               # 本地/远程 LLM
│   └── storage/           # 数据存储
├── knowledge/             # 故障知识库 YAML
├── tests/                 # 测试
└── scripts/               # 安装脚本
```

## Week 1 里程碑

- [x] 项目框架搭建
- [x] DDS Bridge 骨架
- [x] 模拟数据模式
- [x] joints Resource
- [x] MCP Server 基础
- [x] Gradio Web UI 基础

## 核心设计原则

1. **只读诊断** - 不暴露任何控制类 Tool
2. **自包含** - 单台 G1 上所有组件就绪
3. **LLM 只做认知** - 规则匹配和编排是确定性逻辑

## 许可证

MIT License
