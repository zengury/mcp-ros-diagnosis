# Manastone Diagnostic

**Unitree G1 / Humanoid Robot Operations Agent**
Part of the [Snakes™](https://github.com/liuzhiqiang77-cell) Agent Platform

---

## Architecture

Manastone is built around **one MCP server per hardware subsystem**. Each server runs on a dedicated port, shares a common DDS bridge and event log, and can be enabled/disabled independently via `config/servers.yaml`.

```
manastone-core       :8080   Diagnosis agent, schema overview, global alerts
manastone-joints     :8081   Joint motor monitoring (temp, torque, velocity, comm)
manastone-power      :8082   Battery voltage, current, SOC, temperature
manastone-imu        :8083   Body posture, tilt detection, fall risk
manastone-hand       :8084   Dexterous hand joints (DEX3, optional)
manastone-vision     :8085   Camera health, depth sensor (M2, stub)
manastone-motion     :8086   Locomotion controller state (M2, stub)
manastone-pid-tuner  :8087   AI auto PID tuning (autoresearch style)
```

Data flow: `ROS2 DDS → DDS Bridge → Schema Engine → SemanticEvent → EventLog → LLM`

---

## Quick Start

### Mock mode (no robot)
```bash
conda create -n manastone python=3.10 -y && conda activate manastone
pip install -e .
export MANASTONE_MOCK_MODE=true
manastone-launcher
```

### Real robot (G1)
```bash
source /opt/ros/humble/setup.bash
conda activate manastone
export MANASTONE_ROBOT_ID=g1_site_01
manastone-launcher
```

### Select which servers to start
```bash
# Edit config/servers.yaml: set enabled: true/false per server
# Or override from command line:
manastone-launcher --enable joints,power,core
manastone-launcher --list   # show all available servers
```

---

## AI PID Auto-Tuning

The PID tuner implements [Karpathy autoresearch](https://github.com/karpathy/autoresearch) architecture: the LLM **edits files** instead of passing numbers via tool calls, and **git** is the research log.

### How it works

```
while best_score < target:
    new_yaml = LLM.read(program.md + params.yaml + results.tsv)
    write(params.yaml, new_yaml)          # LLM edits the file
    commit_hash = git commit              # git is the research log
    score = run_experiment(kp, ki, kd)   # physics sim or real joint
    if score > best:
        save_best()                       # keep improvement
    else:
        git checkout HEAD~1 params.yaml  # discard, roll back
    append results.tsv                   # structured log
```

### Enable the PID tuner

```yaml
# config/servers.yaml
- id: pid_tuner
  port: 8087
  enabled: true   # disabled by default for safety
```

Configure your LLM:
```bash
export MANASTONE_LLM_REMOTE=true
export MANASTONE_LLM_API_KEY=sk-...
export MANASTONE_LLM_REMOTE_URL=https://api.openai.com/v1
export MANASTONE_LLM_REMOTE_MODEL=gpt-4o
```

### Run auto-tuning

Ask the MCP client (e.g. Claude) to call `pid_run_research_loop`:

```
请对 left_knee 关节运行自动调参，目标分数 80 分，使用 Mock 模式
```

Or directly via MCP:
```json
{
  "tool": "pid_run_research_loop",
  "joint_name": "left_knee",
  "joint_group": "leg",
  "target_score": 80,
  "max_experiments": 50,
  "mock_mode": true
}
```

Expected output:
```json
{
  "best_score": 83.7,
  "best_params": {"kp": 18.0, "ki": 0.30, "kd": 5.0},
  "target_reached": true,
  "total_experiments": 12,
  "workspace_dir": "storage/pid_workspace/left_knee"
}
```

### Workspace file structure

```
storage/pid_workspace/{joint_name}/
├── params.yaml        # current params (LLM reads and writes this)
├── program.md         # tuning task description (human-written, fixed)
├── results.tsv        # experiment log (append-only)
└── best_params.yaml   # best params snapshot (updated in real-time)
```

`params.yaml` carries the LLM's reasoning as a comment:
```yaml
# PID 调参参数文件 — 由 AI Agent 自动修改
# 假设（Hypothesis）：
#   上次超调18%，Kd从3.0增至5.0以增加阻尼，同时Kp略降至18
pid:
  kp: 18.0
  ki: 0.30
  kd: 5.0
```

### View research history

```bash
# Full git log = research log
git log --oneline storage/pid_workspace/left_knee/params.yaml

# Live experiment stream
tail -f storage/pid_workspace/left_knee/results.tsv

# All experiment results
cat storage/pid_workspace/left_knee/results.tsv
```

### Scoring (0–100)

| Metric | Weight | Target |
|--------|--------|--------|
| Overshoot | 25 pts | < 5% |
| Rise Time | 20 pts | < 0.5s |
| Settling Time | 25 pts | < 1.0s |
| Steady-State Error | 20 pts | < 2% |
| Oscillation Count | 10 pts | 0 |

Grades: **A** (≥90) / **B** (≥75) / **C** (≥60) / **D** (≥45) / **F** (<45)

### Safety — three-layer guard

```
Layer 1: Static bounds (before writing params)
  kp/ki/kd clamped to ranges in robot_schema.yaml

Layer 2: Pre-experiment check
  battery SOC > 20%, joint temp < 60°C, comm_lost == 0

Layer 3: Runtime monitoring (during experiment)
  |torque| > 60 Nm → immediate abort
  |velocity| > 20 rad/s → immediate abort
  temp rise > 5°C/exp → immediate abort
```

### PID Tuner MCP Tools

| Tool | Description |
|------|-------------|
| `pid_safety_check` | Pre-experiment safety check |
| `pid_run_experiment` | Single PID experiment + score |
| `pid_propose_params` | LLM/rule parameter suggestion (one-shot) |
| `pid_run_auto_tuning` | Python for-loop mode (no LLM file editing) |
| `pid_run_research_loop` | **autoresearch loop** — keep going until target |
| `pid_get_history` | Query experiment history |
| `pid_get_best` | Get best params found so far |
| `pid_clear_history` | Reset workspace (requires confirm=true) |

---

## Robot Configuration

All hardware-specific knowledge lives in `config/robot_schema.yaml`:
- **motor_index_map** — maps `motor_state[i]` array index to joint name
  (sourced from Unitree SDK `G1JointIndex` enum, no hardcoded indices in code)
- **thresholds** — warning/critical values per field, per joint
- **event_types** — semantic event catalog with severity and retention
- **pid_safety_bounds** — per-joint PID parameter bounds and safety limits

To add a new robot: run `manastone-launcher --discover` or edit `robot_schema.yaml` following the Unitree G1 reference.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MANASTONE_ROBOT_ID` | `robot_01` | Robot identifier (used in EventLog filename) |
| `MANASTONE_MOCK_MODE` | `false` | `true` = offline test without real DDS |
| `MANASTONE_SCHEMA_PATH` | `config/robot_schema.yaml` | Path to robot schema |
| `MANASTONE_STORAGE_DIR` | `storage` | Storage directory |
| `MANASTONE_LLM_REMOTE` | `false` | `true` = use cloud LLM API |
| `MANASTONE_LLM_API_KEY` | _(empty)_ | API key for remote LLM |
| `MANASTONE_LLM_REMOTE_URL` | _(empty)_ | Remote LLM endpoint (OpenAI-compatible) |
| `MANASTONE_LLM_REMOTE_MODEL` | `gpt-4o` | Remote model name |
| `MANASTONE_LLM_LOCAL_URL` | `http://localhost:8000/v1` | Local LLM endpoint |

---

## MCP Tools Reference

### manastone-core (port 8080)
`system_status` · `active_warnings` · `diagnose` · `lookup_fault`
`schema_overview` · `run_discovery` · `server_registry` · `recent_events` · `event_stats`

### manastone-joints (port 8081)
`joint_status` · `joint_alerts` · `joint_history` · `joint_compare` · `joint_schema`

### manastone-power (port 8082)
`power_status` · `power_alerts` · `power_history` · `charge_estimate`

### manastone-imu (port 8083)
`posture_status` · `posture_alerts` · `posture_history` · `fall_risk`

### manastone-hand (port 8084)
`hand_status` · `hand_alerts` · `hand_history` · `grasp_test`

### manastone-pid-tuner (port 8087)
`pid_safety_check` · `pid_run_experiment` · `pid_propose_params`
`pid_run_auto_tuning` · `pid_run_research_loop` · `pid_get_history`
`pid_get_best` · `pid_clear_history`

---

## License

MIT
