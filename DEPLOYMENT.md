# Manastone Diagnostic — 部署与使用手册

**版本**: v0.4 | **目标平台**: Unitree G1 · Jetson Orin NX

---

## 目录

1. [系统概述](#1-系统概述)
2. [能力边界](#2-能力边界)
3. [硬件与网络要求](#3-硬件与网络要求)
4. [安装](#4-安装)
5. [机器人配置](#5-机器人配置)
6. [启动与服务管理](#6-启动与服务管理)
7. [MCP Server 使用指南](#7-mcp-server-使用指南)
8. [LLM 配置](#8-llm-配置)
9. [故障知识库](#9-故障知识库)
10. [常见问题排查](#10-常见问题排查)

---

## 1. 系统概述

Manastone Diagnostic 是 Snakes™ 平台的第一个垂直 Agent，为 Unitree G1 现场工程师提供自然语言交互的故障诊断能力。

**v0.4 架构：每个硬件子系统对应一个独立 MCP Server**

```
工程师 / Claude / Cursor
         │
         │ MCP (SSE)
    ┌────┴─────────────────────────────────────────────┐
    │  manastone-core    :8080  诊断Agent、知识库、全局  │
    │  manastone-joints  :8081  关节电机监控              │
    │  manastone-power   :8082  电池监控                 │
    │  manastone-imu     :8083  姿态监控                 │
    │  manastone-hand    :8084  灵巧手（可选）            │
    │  manastone-vision  :8085  视觉（M2 stub）          │
    │  manastone-motion  :8086  运动控制器（M2 stub）    │
    └──────────────────────────────────────────────────┘
         │
         │ DDS Domain 0 (只读)
         ▼
    G1 RockChip (192.168.123.161)
```

**数据流**：`ROS2 DDS → DDS Bridge → Schema Engine → SemanticEvent → EventLog → LLM`

所有硬件特定知识（关节索引、阈值、事件规则）集中在 `config/robot_schema.yaml`，代码中无任何硬编码。

---

## 2. 能力边界

| 功能 | 状态 | 说明 |
|------|------|------|
| 关节温度/力矩/速度监控 | ✅ M1 | 基于 schema 规则自动检测 |
| 电池电压/SOC/电流监控 | ✅ M1 | |
| IMU 姿态/倾斜检测 | ✅ M1 | |
| 自然语言故障诊断 | ✅ M1 | 需要 LLM（本地或云端）|
| 语义事件持久化 | ✅ M1 | Append-Only SQLite EventLog |
| ROS2 话题自动发现 | ✅ M1 | `run_discovery` 工具 |
| 灵巧手监控 | ⚙️ 可选 | 需在 servers.yaml 中启用 |
| 视觉/运动控制器监控 | 🔜 M2 | 当前为 stub |
| 写入/控制机器人 | 🚫 M1 不支持 | 纯只读诊断 |

---

## 3. 硬件与网络要求

### 网络拓扑

```
开发机 / 工程师终端
   └── 192.168.123.x 网段
         ├── 192.168.123.164  Orin NX（部署目标）
         └── 192.168.123.161  G1 RockChip（运动控制器，只读）
```

### Orin NX 最低要求

| 项目 | 要求 |
|------|------|
| OS | Ubuntu 22.04 / JetPack 5.x |
| Python | 3.10 |
| ROS2 | Humble Hawksbill |
| 可用内存 | ≥ 4GB（无本地 LLM）/ ≥ 12GB（含 Qwen2.5-7B）|
| 可用磁盘 | ≥ 2GB（无模型）/ ≥ 20GB（含模型）|
| 网络 | 与 192.168.123.161 同 DDS Domain |

---

## 4. 安装

### 4.1 克隆仓库

```bash
git clone https://github.com/liuzhiqiang77-cell/mcp-ros-diagnosis.git
cd mcp-ros-diagnosis
```

### 4.2 创建 Python 环境

```bash
# 推荐使用 conda（与 ROS2 环境隔离）
conda create -n manastone python=3.10 -y
conda activate manastone

# 安装 manastone（不含 DDS，先用 mock 模式）
pip install -e .

# 真机模式另需安装 CycloneDDS（可选）
pip install -e ".[dds]"
```

### 4.3 验证安装

```bash
manastone-launcher --list
# 应输出：joints, power, imu, hand, vision, motion, core
```

---

## 5. 机器人配置

Manastone 通过两个配置文件完全描述机器人，**代码无需改动**。

### 5.1 config/servers.yaml — 控制启动哪些 Server

```yaml
servers:
  - id: joints
    enabled: true    # ← true = 随 manastone-launcher 启动
    port: 8081
  - id: hand
    enabled: false   # ← 没有灵巧手时改为 false
    port: 8084
  # ...
```

**修改方式**：
- 直接编辑 `config/servers.yaml`，修改 `enabled: true/false`
- 或命令行临时覆盖：`manastone-launcher --enable joints,power,core`

### 5.2 config/robot_schema.yaml — 机器人硬件拓扑

这是系统的唯一真相来源，定义：

- **motor_index_map**：关节索引→名称映射（来自 Unitree SDK G1JointIndex 枚举）
- **thresholds**：每个字段的 warning/critical 阈值
- **event_types**：语义事件类型定义

**切换机器人型号**：用对应的 schema 文件替换 `config/robot_schema.yaml`，无需修改代码。

### 5.3 支持的机器人配置包

| 机器人 | Schema 文件 | 状态 |
|--------|------------|------|
| Unitree G1 29-DOF | `config/robot_schema.yaml`（默认）| ✅ 内置 |
| Unitree G1 23-DOF | 修改 motor_index_map 中 `variant_29dof_only` 条目 | 🔧 手动 |
| 其他机器人 | 运行 `run_discovery` 工具自动生成草稿 | 📋 发现后配置 |

### 5.4 新机器人自动发现

```bash
# 1. 机器人上电，ROS2 在线
# 2. 启动 manastone-launcher（mock=false）
export MANASTONE_MOCK_MODE=false
manastone-launcher --enable core

# 3. 在 Claude / cursor 中调用 run_discovery 工具
# 生成 config/discovered_schema.yaml

# 4. 检查草稿，补充阈值，重命名
cp config/discovered_schema.yaml config/robot_schema.yaml
```

---

## 6. 启动与服务管理

### 6.1 Mock 模式（无真机，推荐初次测试）

```bash
conda activate manastone
export MANASTONE_MOCK_MODE=true
export MANASTONE_ROBOT_ID=g1_dev_01
manastone-launcher
```

启动后输出：
```
══════════════════════════════════════════════════════════
  Manastone Diagnostic  —  Multi-Server Mode
══════════════════════════════════════════════════════════
  Robot ID  : g1_dev_01
  Mode      : MOCK
  Schema    : config/robot_schema.yaml

  Servers:
    ✅ ENABLED      manastone-core         :8080  诊断Agent...
    ✅ ENABLED      manastone-joints       :8081  关节电机...
    ✅ ENABLED      manastone-power        :8082  电池...
    ✅ ENABLED      manastone-imu          :8083  姿态...
    ⬜ disabled     manastone-hand         :8084  灵巧手...
══════════════════════════════════════════════════════════
```

### 6.2 真机模式（G1 上线）

```bash
# 先 source ROS2 环境
source /opt/ros/humble/setup.bash
source <your_ros2_ws>/install/setup.bash

conda activate manastone
export MANASTONE_ROBOT_ID=g1_warehouse_01
export MANASTONE_MOCK_MODE=false
manastone-launcher
```

### 6.3 只启动部分 Server

```bash
# 仅启动核心 + 关节
manastone-launcher --enable joints,core

# 单独启动一个 server（调试用）
MANASTONE_PORT=8081 MANASTONE_MOCK_MODE=true manastone-joints
```

### 6.4 systemd 自启（生产部署）

```bash
sudo tee /etc/systemd/system/manastone.service > /dev/null << 'EOF'
[Unit]
Description=Manastone Diagnostic Multi-Server
After=network.target

[Service]
Type=simple
User=unitree
WorkingDirectory=/home/unitree/mcp-ros-diagnosis
Environment=MANASTONE_ROBOT_ID=g1_warehouse_01
Environment=MANASTONE_MOCK_MODE=false
ExecStartPre=/bin/bash -c 'source /opt/ros/humble/setup.bash'
ExecStart=/home/unitree/miniforge3/envs/manastone/bin/manastone-launcher
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable manastone
sudo systemctl start manastone
```

### 6.5 查看运行状态

```bash
sudo systemctl status manastone
journalctl -u manastone -f       # 实时日志
journalctl -u manastone -n 100   # 最近100行
```

---

## 7. MCP Server 使用指南

### 7.1 在 Claude Desktop 中连接

编辑 `~/.config/claude/claude_desktop_config.json`（Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "manastone-core": {
      "url": "http://192.168.123.164:8080/sse"
    },
    "manastone-joints": {
      "url": "http://192.168.123.164:8081/sse"
    },
    "manastone-power": {
      "url": "http://192.168.123.164:8082/sse"
    },
    "manastone-imu": {
      "url": "http://192.168.123.164:8083/sse"
    }
  }
}
```

### 7.2 可用工具总览

**manastone-core（诊断 Agent）**

| 工具 | 说明 |
|------|------|
| `system_status` | 全局健康总览，所有子系统聚合 |
| `active_warnings` | 当前所有活跃告警（WARNING/CRITICAL）|
| `diagnose(query)` | 自然语言故障诊断，结合 EventLog + 知识库 + LLM |
| `lookup_fault(code)` | 故障代码查询（FK-001 等）|
| `schema_overview` | 机器人硬件拓扑（话题、组件、事件类型）|
| `run_discovery` | 触发 ROS2 话题自动发现 |
| `server_registry` | 列出当前所有已启动的 MCP server |
| `recent_events` | 查询最近语义事件，支持按严重度/组件过滤 |
| `event_stats` | EventLog 统计（总事件数、按严重度分布）|

**manastone-joints（关节）**

| 工具 | 说明 |
|------|------|
| `joint_status(group)` | 所有关节当前温度、力矩、速度快照 |
| `joint_alerts` | 当前活跃关节告警 |
| `joint_history(joint_name)` | 单关节事件历史（含因果链）|
| `joint_compare` | 左右对称关节对比（不对称告警）|
| `joint_schema` | 关节拓扑：motor_index_map + 阈值规则 |

**manastone-power（电源）**

| 工具 | 说明 |
|------|------|
| `power_status` | 电压、电流、SOC、温度 |
| `power_alerts` | 当前活跃电源告警 |
| `power_history` | 电池事件历史 |
| `charge_estimate` | 基于当前放电率估算剩余工作时间 |

**manastone-imu（姿态）**

| 工具 | 说明 |
|------|------|
| `posture_status` | 当前 roll/pitch/yaw（度）及角速度 |
| `posture_alerts` | 当前活跃姿态告警 |
| `posture_history` | 倾斜事件历史 |
| `fall_risk` | 综合跌倒风险评估（多指标）|

**manastone-hand（灵巧手，需启用）**

| 工具 | 说明 |
|------|------|
| `hand_status` | 左右手关节、抓力、通信状态 |
| `hand_alerts` | 当前活跃灵巧手告警 |
| `hand_history(side)` | 手部事件历史 |
| `grasp_test` | 抓握自检（通信和响应状态）|

### 7.3 典型诊断流程

```
机器人出现异常
      │
      ▼
调用 system_status         ← 总览，确认严重程度
      │
      ├─ 有 CRITICAL ──► 调用 active_warnings     ← 定位到具体组件
      │                       │
      │                  调用 joint_history(组件)  ← 查看事件历史和时序
      │                       │
      │                  调用 diagnose("描述现象") ← LLM 综合分析
      │
      └─ 全部正常 ──────► 调用 joint_compare      ← 检查不对称
                              调用 recent_events    ← 检查近期异常
```

---

## 8. LLM 配置

### 8.1 云端 API（推荐，开发调试）

```bash
# .env 文件（放在项目根目录）
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx
OPENAI_API_BASE=https://api.openai.com/v1   # 或 Kimi/其他兼容API
LLM_MODEL=gpt-4o-mini
```

### 8.2 本地 Qwen2.5-7B（离线，生产推荐）

```bash
# 安装 llama.cpp Python 绑定
pip install llama-cpp-python

# 下载 GGUF 模型（约 4.5GB）
# 放到 models/ 目录

# 启动本地推理服务
python3 -m llama_cpp.server \
  --model models/qwen2.5-7b-instruct-q4_k_m.gguf \
  --host 127.0.0.1 --port 8081 --n_ctx 4096 &

# .env 保持空（无 OPENAI_API_KEY），系统自动使用本地
```

> Orin NX 16GB 上 Qwen2.5-7B 推理速度约 10-20 token/s，响应时间 15-30 秒。

---

## 9. 故障知识库

知识库位于 `knowledge/fault_library.yaml`，8 个内置故障条目：

| 故障码 | 名称 | 严重度 |
|--------|------|--------|
| FK-001 | 关节编码器通信异常 | CRITICAL |
| FK-002 | 关节电机过流保护 | CRITICAL |
| FK-003 | 关节过热保护 | WARNING |
| FK-004 | LiDAR 点云稀疏/缺失 | WARNING |
| FK-005 | RealSense 初始化失败 | WARNING |
| FK-006 | IMU 数据漂移 | NOTICE |
| FK-007 | 关节位置跟踪误差偏大 | NOTICE |
| FK-008 | 灵巧手通信断连 | WARNING |

**阈值规则**（在 `config/robot_schema.yaml` 中修改，无需重启）：

| 指标 | Warning | Critical |
|------|---------|----------|
| 关节温度 | ≥ 50°C | ≥ 70°C |
| 关节力矩（默认）| ≥ 30 Nm | ≥ 45 Nm |
| 关节力矩（膝/髋）| ≥ 35 Nm | ≥ 50 Nm |
| 关节速度 | — | ≥ 20 rad/s |
| 电池电压 | ≤ 46V | ≤ 43V |
| 电量 SOC | ≤ 20% | ≤ 10% |
| 机体倾斜 | ≥ 20° | ≥ 30° |

---

## 10. 常见问题排查

### Server 启动失败

```bash
# 查看详细错误
manastone-launcher --mock 2>&1 | head -50

# 检查 schema 是否有效
python3 -c "
import sys; sys.path.insert(0,'src')
from manastone_diag.schema import SchemaLoader
s = SchemaLoader('config/robot_schema.yaml').load()
print(f'OK: {s.robot_type} | {len(s.components)} components')
"
```

### 端口冲突

```bash
# 检查端口占用
ss -tlnp | grep -E '808[0-6]'

# 修改 servers.yaml 中的端口号，重启
```

### ROS2 数据收不到

```bash
# 确认 ROS2 环境已 source
source /opt/ros/humble/setup.bash

# 确认 domain_id 和机器人一致
export ROS_DOMAIN_ID=0

# 确认能收到 lowstate 话题
ros2 topic echo --once /lf/lowstate
```

### 告警一直不恢复

EventLog 告警恢复基于状态转换检测：只有当下一次轮询中该字段回到正常范围，才发出 recovery 事件。如果机器人已关机，最后一个 WARNING/CRITICAL 事件会一直显示为活跃，直到下次重新上线。这是正常行为。

### 更新代码后部署

```bash
# 开发机
cd mcp-ros-diagnosis
git pull
rsync -avz --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
  ./ unitree@192.168.123.164:~/mcp-ros-diagnosis/

# Orin NX
sudo systemctl restart manastone
```

---

## 附录：端口一览

| 服务 | 端口 | 说明 |
|------|------|------|
| manastone-core | 8080 | 诊断 Agent（必须启动）|
| manastone-joints | 8081 | 关节监控（必须启动）|
| manastone-power | 8082 | 电源监控 |
| manastone-imu | 8083 | 姿态监控 |
| manastone-hand | 8084 | 灵巧手（可选）|
| manastone-vision | 8085 | 视觉（M2）|
| manastone-motion | 8086 | 运动控制器（M2）|
| 本地 LLM | 8081* | Qwen2.5-7B（若 joints 不用 8081 则可用）|

> *本地 LLM 端口与 joints server 冲突时，修改 servers.yaml 中 joints 的 port。

---

## 附录：G1 29-DOF 关节索引表

来源：Unitree SDK `G1JointIndex` 枚举。此表已内置在 `config/robot_schema.yaml` 的 `motor_index_map` 中，无需手动维护。

| 索引 | 关节名 | 中文名 | 索引 | 关节名 | 中文名 |
|------|--------|--------|------|--------|--------|
| 0 | left_hip_pitch | 左髋俯仰 | 15 | left_shoulder_pitch | 左肩俯仰 |
| 1 | left_hip_roll | 左髋滚转 | 16 | left_shoulder_roll | 左肩滚转 |
| 2 | left_hip_yaw | 左髋偏航 | 17 | left_shoulder_yaw | 左肩偏航 |
| 3 | left_knee | 左膝关节 | 18 | left_elbow | 左肘关节 |
| 4 | left_ankle_pitch | 左踝俯仰 | 19 | left_wrist_roll | 左腕滚转 |
| 5 | left_ankle_roll | 左踝滚转 | 20 | left_wrist_pitch | 左腕俯仰 |
| 6 | right_hip_pitch | 右髋俯仰 | 21 | left_wrist_yaw | 左腕偏航 |
| 7 | right_hip_roll | 右髋滚转 | 22 | right_shoulder_pitch | 右肩俯仰 |
| 8 | right_hip_yaw | 右髋偏航 | 23 | right_shoulder_roll | 右肩滚转 |
| 9 | right_knee | 右膝关节 | 24 | right_shoulder_yaw | 右肩偏航 |
| 10 | right_ankle_pitch | 右踝俯仰 | 25 | right_elbow | 右肘关节 |
| 11 | right_ankle_roll | 右踝滚转 | 26 | right_wrist_roll | 右腕滚转 |
| 12 | waist_yaw | 腰偏航 | 27 | right_wrist_pitch | 右腕俯仰 |
| 13 | waist_roll | 腰滚转 | 28 | right_wrist_yaw | 右腕偏航 |
| 14 | waist_pitch | 腰俯仰 | | | |
