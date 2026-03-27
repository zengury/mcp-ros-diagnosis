"""
PID 调参实验运行器

解决核心挑战：环境一致性 + 可重复性。

支持三种模式：
  mock_euler  模式：基于二阶线性系统物理仿真（默认，无需额外依赖）
  mock_mujoco 模式：基于 MuJoCo 物理引擎仿真（需安装 mujoco>=3.0）
  real        模式：通过 ROS2 /lowcmd 下发目标位置，读取 /lowstate 采集响应

仿真模型（mock_euler 模式）：
  关节被建模为带粘性阻尼的旋转刚体：
    J·dω/dt = u - B·ω
  其中 u 为 PID 控制输出，J 为转动惯量，B 为阻尼系数。
  不同关节组有不同的 J/B 参数，使仿真更接近真实特性。

MuJoCo 模式（mock_mujoco）：
  使用 MuJoCo 刚体动力学引擎，建模为单关节铰链，
  通过 actuator/position 驱动，可更准确地模拟非线性摩擦、
  重力补偿及惯量耦合效应。需要安装 mujoco>=3.0。
  若 mujoco 未安装，自动回退到 mock_euler 并打印警告。
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from .scorer import StepResponseMetrics, compute_metrics
from .safety import SafetyGuard, SafetyCheckResult

logger = logging.getLogger(__name__)


# ── 各关节组的物理模型参数 ─────────────────────────────────────
_JOINT_PHYSICS: Dict[str, Dict[str, float]] = {
    "leg":   {"J": 0.10, "B": 0.50},   # 腿部：惯量大、阻尼高
    "waist": {"J": 0.07, "B": 0.40},   # 腰部：中等
    "arm":   {"J": 0.03, "B": 0.20},   # 手臂：惯量小、阻尼低
    "default": {"J": 0.05, "B": 0.30},
}

# 环境一致性检查项目清单（每次实验前记录）
ENVIRONMENT_CHECKLIST = [
    "battery_soc_pct",
    "joint_temp_c",
    "ambient_load",   # 简化：0=单关节测试，1=全身站立
]


@dataclass
class ExperimentConfig:
    """单次 PID 实验的完整配置（确保可重复性）"""
    joint_name: str
    joint_group: str = "default"
    kp: float = 5.0
    ki: float = 0.1
    kd: float = 0.5
    setpoint_rad: float = 0.5           # 阶跃目标（rad），默认 ~28.6°
    duration_s: float = 2.0             # 实验时长
    sample_hz: float = 500.0           # 采样率
    initial_position_rad: float = 0.0  # 初始位置
    mock_mode: bool = True
    # 仿真后端：euler（默认）/ mujoco（需安装 mujoco>=3.0）
    sim_backend: str = "euler"

    def dt(self) -> float:
        return 1.0 / self.sample_hz

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExperimentResult:
    """单次实验的完整结果（包含元数据，支持可重复性分析）"""
    experiment_id: str
    config: ExperimentConfig
    timestamp: float
    metrics: StepResponseMetrics
    safety_aborted: bool = False
    abort_reason: Optional[str] = None
    # 环境快照（用于一致性比对）
    env_snapshot: Dict[str, Any] = field(default_factory=dict)
    # 原始时间序列（可选，用于波形可视化）
    raw_times: Optional[List[float]] = None
    raw_positions: Optional[List[float]] = None

    def to_dict(self, include_raw: bool = False) -> dict:
        d = {
            "experiment_id": self.experiment_id,
            "timestamp": self.timestamp,
            "config": self.config.to_dict(),
            "metrics": {
                "score": self.metrics.score,
                "grade": self.metrics.grade,
                "overshoot_pct": self.metrics.overshoot_pct,
                "rise_time_s": self.metrics.rise_time_s,
                "settling_time_s": self.metrics.settling_time_s,
                "sse_pct": self.metrics.sse_pct,
                "iae": self.metrics.iae,
                "oscillation_count": self.metrics.oscillation_count,
                "peak_torque_nm": self.metrics.peak_torque_nm,
                "diagnosis": self.metrics.diagnosis,
            },
            "safety_aborted": self.safety_aborted,
            "abort_reason": self.abort_reason,
            "env_snapshot": self.env_snapshot,
        }
        if include_raw and self.raw_times:
            d["raw_times"] = self.raw_times
            d["raw_positions"] = self.raw_positions
        return d


class ExperimentRunner:
    """
    实验运行器（支持 mock / real 两种模式）

    职责：
      - 执行单次 PID 阶跃响应测试
      - 记录环境快照（一致性保障）
      - 实时运行安全监控
      - 返回完整的 ExperimentResult
    """

    def __init__(
        self,
        safety_guard: SafetyGuard,
        mock_mode: bool = True,
        dds_bridge: Optional[Any] = None,  # DDSBridge，real 模式必须提供
    ):
        self.safety = safety_guard
        self.mock_mode = mock_mode
        self.dds_bridge = dds_bridge
        self._exp_counter = 0

    def _next_id(self) -> str:
        self._exp_counter += 1
        ts = int(time.time())
        return f"exp_{ts}_{self._exp_counter:04d}"

    async def run(
        self,
        config: ExperimentConfig,
        env_snapshot: Optional[Dict[str, Any]] = None,
    ) -> ExperimentResult:
        """
        执行一次 PID 实验。

        Args:
            config: 完整实验配置
            env_snapshot: 调用方提供的环境快照（电量、温度等）

        Returns:
            ExperimentResult，无论实验成败都会返回（通过 safety_aborted 标志区分）
        """
        exp_id = self._next_id()
        snapshot = env_snapshot or {}

        logger.info(
            "开始实验 %s: %s Kp=%.2f Ki=%.3f Kd=%.2f",
            exp_id, config.joint_name, config.kp, config.ki, config.kd,
        )

        if self.mock_mode:
            if config.sim_backend == "mujoco":
                times, positions, torques, velocities, aborted, abort_reason = (
                    await self._run_mujoco(config)
                )
            else:
                times, positions, torques, velocities, aborted, abort_reason = (
                    await self._run_mock(config)
                )
        else:
            times, positions, torques, velocities, aborted, abort_reason = (
                await self._run_real(config)
            )

        if aborted or len(positions) < 10:
            # 实验被中止，填充空指标
            empty_metrics = StepResponseMetrics(
                setpoint=config.setpoint_rad,
                duration_s=0,
                dt_s=config.dt(),
                score=0.0,
                grade="F",
                diagnosis=[abort_reason or "实验中止，无有效数据"],
            )
            return ExperimentResult(
                experiment_id=exp_id,
                config=config,
                timestamp=time.time(),
                metrics=empty_metrics,
                safety_aborted=True,
                abort_reason=abort_reason,
                env_snapshot=snapshot,
                raw_times=times,
                raw_positions=positions,
            )

        metrics = compute_metrics(
            times=times,
            positions=positions,
            setpoint=config.setpoint_rad,
            torques=torques,
            velocities=velocities,
        )

        logger.info(
            "实验 %s 完成：score=%.1f grade=%s overshoot=%.1f%% rise=%.3fs settle=%.3fs",
            exp_id, metrics.score, metrics.grade,
            metrics.overshoot_pct, metrics.rise_time_s, metrics.settling_time_s,
        )

        return ExperimentResult(
            experiment_id=exp_id,
            config=config,
            timestamp=time.time(),
            metrics=metrics,
            env_snapshot=snapshot,
            raw_times=times,
            raw_positions=positions,
        )

    async def _run_mock(
        self, config: ExperimentConfig
    ) -> Tuple[List[float], List[float], List[float], List[float], bool, Optional[str]]:
        """
        基于二阶线性系统的物理仿真。

        使用欧拉积分法离散化：
          v_new = v + (u - B*v) / J * dt
          x_new = x + v * dt
        """
        physics = _JOINT_PHYSICS.get(config.joint_group, _JOINT_PHYSICS["default"])
        J = physics["J"]
        B = physics["B"]

        dt = config.dt()
        n = int(config.duration_s / dt)
        setpoint = config.setpoint_rad

        x = config.initial_position_rad
        v = 0.0
        integral_e = 0.0
        prev_error = setpoint - x

        times = []
        positions = []
        torques = []
        velocities = []

        start_temp = 35.0  # 模拟初始温度

        for i in range(n):
            t = i * dt
            error = setpoint - x
            integral_e += error * dt

            # 防积分饱和（anti-windup）
            max_integral = 2.0 / (config.ki + 1e-9)
            integral_e = max(-max_integral, min(max_integral, integral_e))

            d_error = (error - prev_error) / dt
            u = config.kp * error + config.ki * integral_e + config.kd * d_error

            # 安全监控（每 100 步检查一次）
            if i % 100 == 0 and i > 0:
                bounds = self.safety.get_bounds(config.joint_name, config.joint_group)
                temp_rise = len(torques) * dt * abs(u) * 0.001  # 简化热模型
                stop, reason = self.safety.runtime_check(
                    elapsed_s=t,
                    current_torque_nm=abs(u),
                    current_velocity_rad_s=abs(v),
                    temp_rise_c=temp_rise,
                    joint_name=config.joint_name,
                    joint_group=config.joint_group,
                )
                if stop:
                    logger.warning("实验中止（仿真）：%s", reason)
                    return times, positions, torques, velocities, True, reason

            # 力矩限幅（电机物理限制）
            bounds = self.safety.get_bounds(config.joint_name, config.joint_group)
            u_clamped = max(-bounds.max_torque_nm, min(bounds.max_torque_nm, u))

            # 物理积分（欧拉法）
            a = (u_clamped - B * v) / J
            v = v + a * dt
            x = x + v * dt

            times.append(t)
            positions.append(x)
            torques.append(abs(u_clamped))
            velocities.append(abs(v))
            prev_error = error

        return times, positions, torques, velocities, False, None

    async def _run_mujoco(
        self, config: ExperimentConfig
    ) -> Tuple[List[float], List[float], List[float], List[float], bool, Optional[str]]:
        """
        基于 MuJoCo 物理引擎的关节仿真（需要 mujoco>=3.0）。

        建模为单关节铰链（hinge joint），通过 position actuator 驱动，
        内置 MuJoCo 的刚体动力学求解器计算运动响应。
        比 Euler 积分更准确：含非线性摩擦、几何惯量、接触动力学。

        若 mujoco 未安装，自动回退到 Euler mock 并记录警告。
        """
        try:
            import mujoco  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "mujoco 未安装（pip install mujoco），回退到 Euler 仿真。"
                "安装后可获得更精确的物理仿真。"
            )
            return await self._run_mock(config)

        physics = _JOINT_PHYSICS.get(config.joint_group, _JOINT_PHYSICS["default"])
        J = physics["J"]
        B = physics["B"]
        bounds = self.safety.get_bounds(config.joint_name, config.joint_group)

        # ── 动态生成 MuJoCo XML 模型 ───────────────────────────
        # 单关节铰链：转动惯量 J，阻尼 B，PID position actuator
        xml = f"""
<mujoco model="pid_joint_test">
  <option timestep="{config.dt()}" integrator="RK4"/>
  <worldbody>
    <body name="link" pos="0 0 0">
      <inertial pos="0 0 0" mass="{J * 10:.3f}" diaginertia="{J:.4f} {J:.4f} {J:.4f}"/>
      <joint name="test_joint" type="hinge" axis="0 0 1"
             damping="{B:.4f}"
             frictionloss="0.01"
             range="-3.14159 3.14159"/>
      <geom type="capsule" size="0.02 0.1"/>
    </body>
  </worldbody>
  <actuator>
    <position name="pid_act" joint="test_joint"
               kp="{config.kp:.4f}"
               kv="{config.kd:.4f}"
               forcerange="-{bounds.max_torque_nm:.1f} {bounds.max_torque_nm:.1f}"/>
  </actuator>
</mujoco>
"""
        try:
            model = mujoco.MjModel.from_xml_string(xml)
            data = mujoco.MjData(model)

            # 设置初始位置
            data.qpos[0] = config.initial_position_rad
            data.qvel[0] = 0.0
            mujoco.mj_forward(model, data)

            n = int(config.duration_s / config.dt())
            times: List[float] = []
            positions: List[float] = []
            torques: List[float] = []
            velocities: List[float] = []

            integral_e = 0.0
            prev_error = config.setpoint_rad - config.initial_position_rad

            for i in range(n):
                # Ki 积分项通过外部累积（MuJoCo position actuator 仅含 Kp+Kd）
                error = config.setpoint_rad - data.qpos[0]
                integral_e += error * config.dt()
                max_integral = 2.0 / (config.ki + 1e-9)
                integral_e = max(-max_integral, min(max_integral, integral_e))
                ki_term = config.ki * integral_e

                # 将 Ki 项叠加到 actuator 目标（等效偏置）
                data.ctrl[0] = config.setpoint_rad + ki_term / (config.kp + 1e-9)

                mujoco.mj_step(model, data)

                torque_actual = float(data.actuator_force[0])

                # 运行时安全检查（每 100 步）
                if i % 100 == 0 and i > 0:
                    stop, reason = self.safety.runtime_check(
                        elapsed_s=data.time,
                        current_torque_nm=abs(torque_actual),
                        current_velocity_rad_s=abs(data.qvel[0]),
                        temp_rise_c=len(torques) * config.dt() * abs(torque_actual) * 0.001,
                        joint_name=config.joint_name,
                        joint_group=config.joint_group,
                    )
                    if stop:
                        logger.warning("MuJoCo 实验中止：%s", reason)
                        return times, positions, torques, velocities, True, reason

                times.append(float(data.time))
                positions.append(float(data.qpos[0]))
                torques.append(abs(torque_actual))
                velocities.append(abs(float(data.qvel[0])))
                prev_error = error

            logger.info(
                "MuJoCo 仿真完成：%d 步，最终位置 %.4f rad（目标 %.4f rad）",
                n, positions[-1], config.setpoint_rad
            )
            return times, positions, torques, velocities, False, None

        except Exception as e:
            logger.error("MuJoCo 仿真异常：%s，回退到 Euler 仿真", e)
            return await self._run_mock(config)

    async def _run_real(
        self, config: ExperimentConfig
    ) -> Tuple[List[float], List[float], List[float], List[float], bool, Optional[str]]:
        """
        真机模式：通过 ROS2 发送控制命令并采集响应。

        TODO（M2）：
          1. 通过 /lowcmd 话题发布目标位置
          2. 以配置的采样率读取 /lowstate motor_state
          3. 实时运行安全监控，超限则发布零力矩命令
        """
        if not self.dds_bridge:
            return [], [], [], [], True, "真机模式需要 DDSBridge，当前未初始化"

        logger.warning("真机 PID 实验功能（M2）：当前版本仅支持 mock 模式")
        return [], [], [], [], True, "真机 PID 实验功能待 M2 版本实现"
