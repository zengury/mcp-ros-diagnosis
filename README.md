# Manastone Diagnostic

**X2 语义化运维诊断工具** - 运行在机器人机载 Orin 上的自包含诊断系统。

## 特性

- ✅ **完全离线可用** - 无需外部网络，零依赖
- 🤖 **本地 LLM** - 内置 Qwen2.5-7B，Orin NX 本地推理
- 🔧 **MCP Server** - 标准 MCP 协议，兼容 Claude Desktop/Cursor
- 🌐 **Web UI** - 浏览器访问，手机/平板即用
- 📊 **语义化诊断** - DDS 原始数据 → 自然语言解释

## 快速开始

### 1. 安装

```bash
# 推荐：先使用独立 conda 环境（更安全）
conda create -n mcp-ros-diagnosis python=3.10 -y
conda activate mcp-ros-diagnosis

# 在机器人 Orin / 开发机上执行
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

#### 模式 B：连接 X2 真机（ROS2）

```bash
# 先加载 ROS2 环境（确保 rclpy 和 aimdk_msgs 可用）
source /opt/ros/<distro>/setup.bash
source <your_ros2_ws>/install/setup.bash

# 再激活项目 conda 环境
conda activate mcp-ros-diagnosis

# 启动服务
manastone-diag
```

默认会订阅以下 ROS2 话题（来自 X2 部署信息）：

- `/aima/hal/joint/leg/state`
- `/aima/hal/joint/waist/state`
- `/aima/hal/joint/arm/state`
- `/aima/hal/joint/head/state`
- `/aima/hal/pmu/state`

如需覆盖话题名，可设置环境变量：

- `MANASTONE_TOPIC_JOINT_LEG_STATE`
- `MANASTONE_TOPIC_JOINT_WAIST_STATE`
- `MANASTONE_TOPIC_JOINT_ARM_STATE`
- `MANASTONE_TOPIC_JOINT_HEAD_STATE`
- `MANASTONE_TOPIC_PMU_STATE`

### Extension 扩展机制

可通过环境变量动态加载 extension（模块内需提供 `register(server)` 函数）：

```bash
export MANASTONE_EXTENSIONS="manastone_diag.extensions.demo_extension"
manastone-diag
```

加载后会新增对应 MCP tools/resources，并在 `g1://system/health` 的 `extensions` 字段中显示已启用扩展。

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
