"""
PID 调参安全围栏 (Safety Guard)

解决核心挑战：防止 Agent 在探索中输出极端参数导致硬件损坏。

三层防护：
  1. 静态边界 (Static Bounds)   ：Kp/Ki/Kd 的绝对允许范围，从 schema 读取
  2. 实验前检查 (Pre-check)     ：电量、温度、通信状态
  3. 实验中监控 (Runtime Watch) ：力矩饱和、速度超限、温升过快
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class PIDSafetyBounds:
    """单个关节的 PID 参数安全边界"""
    kp_min: float = 0.1
    kp_max: float = 50.0
    ki_min: float = 0.0
    ki_max: float = 5.0
    kd_min: float = 0.0
    kd_max: float = 10.0
    # 实验期间的运行限制
    max_torque_nm: float = 30.0         # 超过此值触发紧急停止
    max_velocity_rad_s: float = 15.0    # 超过此值触发紧急停止
    max_temp_rise_c: float = 5.0        # 单次实验允许温升上限


# 各关节组的默认边界（比 schema 更保守的内置兜底值）
_GROUP_DEFAULTS: Dict[str, PIDSafetyBounds] = {
    "leg": PIDSafetyBounds(
        kp_min=1.0, kp_max=80.0,
        ki_min=0.0, ki_max=3.0,
        kd_min=0.0, kd_max=20.0,
        max_torque_nm=50.0,
        max_velocity_rad_s=18.0,
    ),
    "waist": PIDSafetyBounds(
        kp_min=1.0, kp_max=60.0,
        ki_min=0.0, ki_max=2.0,
        kd_min=0.0, kd_max=15.0,
        max_torque_nm=40.0,
        max_velocity_rad_s=10.0,
    ),
    "arm": PIDSafetyBounds(
        kp_min=0.5, kp_max=40.0,
        ki_min=0.0, ki_max=3.0,
        kd_min=0.0, kd_max=10.0,
        max_torque_nm=25.0,
        max_velocity_rad_s=12.0,
    ),
    "default": PIDSafetyBounds(),
}


@dataclass
class SafetyCheckResult:
    """安全检查结果"""
    passed: bool
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "violations": self.violations,
            "warnings": self.warnings,
        }


class SafetyGuard:
    """
    PID 调参安全围栏。
    完全无状态：每次检查都接受外部传入的当前硬件数据。
    """

    def __init__(self, schema_pid_bounds: Optional[Dict[str, dict]] = None):
        """
        Args:
            schema_pid_bounds: 从 robot_schema.yaml 读取的 pid_safety_bounds 配置
                               key = joint_name 或 "default"
        """
        self._schema_bounds = schema_pid_bounds or {}

    def get_bounds(self, joint_name: str, joint_group: str = "default") -> PIDSafetyBounds:
        """
        获取指定关节的安全边界（schema 覆盖 > 组默认 > 全局默认）
        """
        # 1. 从 schema 查找 joint 级别覆盖
        schema_entry = self._schema_bounds.get(joint_name) or self._schema_bounds.get("default")
        if schema_entry:
            return PIDSafetyBounds(**schema_entry)

        # 2. 按关节组使用内置默认
        return _GROUP_DEFAULTS.get(joint_group, _GROUP_DEFAULTS["default"])

    def check_pid_params(
        self,
        joint_name: str,
        kp: float,
        ki: float,
        kd: float,
        joint_group: str = "default",
    ) -> SafetyCheckResult:
        """
        静态参数边界检查。在任何实验前必须调用。
        """
        bounds = self.get_bounds(joint_name, joint_group)
        violations: List[str] = []
        warnings: List[str] = []

        # Kp 检查
        if kp < bounds.kp_min or kp > bounds.kp_max:
            violations.append(
                f"Kp={kp:.2f} 超出安全范围 [{bounds.kp_min}, {bounds.kp_max}]"
            )
        elif kp > bounds.kp_max * 0.8:
            warnings.append(f"Kp={kp:.2f} 接近上限（>{bounds.kp_max * 0.8:.1f}），注意超调")

        # Ki 检查
        if ki < bounds.ki_min or ki > bounds.ki_max:
            violations.append(
                f"Ki={ki:.3f} 超出安全范围 [{bounds.ki_min}, {bounds.ki_max}]"
            )
        elif ki > bounds.ki_max * 0.7:
            warnings.append(f"Ki={ki:.3f} 偏高，可能引起积分饱和振荡")

        # Kd 检查
        if kd < bounds.kd_min or kd > bounds.kd_max:
            violations.append(
                f"Kd={kd:.2f} 超出安全范围 [{bounds.kd_min}, {bounds.kd_max}]"
            )
        elif kd > bounds.kd_max * 0.9:
            warnings.append(f"Kd={kd:.2f} 接近上限，高频噪声可能导致抖动")

        return SafetyCheckResult(passed=len(violations) == 0, violations=violations, warnings=warnings)

    def pre_experiment_check(
        self,
        joint_name: str,
        current_temp_c: float,
        battery_soc_pct: float,
        comm_lost: int,
        joint_group: str = "default",
    ) -> SafetyCheckResult:
        """
        实验前环境条件检查。

        Args:
            current_temp_c:   当前关节温度
            battery_soc_pct:  电池电量百分比
            comm_lost:        通信丢失计数
        """
        bounds = self.get_bounds(joint_name, joint_group)
        violations: List[str] = []
        warnings: List[str] = []

        # 温度检查（调参本身会发热，留足余量）
        if current_temp_c >= 60.0:
            violations.append(
                f"关节 {joint_name} 温度过高 ({current_temp_c:.1f}°C ≥ 60°C)，拒绝开始实验"
            )
        elif current_temp_c >= 50.0:
            warnings.append(f"温度偏高 ({current_temp_c:.1f}°C)，建议冷却后再调参")

        # 电量检查（调参过程中断会导致无效数据）
        if battery_soc_pct < 20.0:
            violations.append(
                f"电量不足 ({battery_soc_pct:.0f}% < 20%)，调参期间断电将损坏结果"
            )
        elif battery_soc_pct < 30.0:
            warnings.append(f"电量偏低 ({battery_soc_pct:.0f}%)，建议充电后调参")

        # 通信检查
        if comm_lost > 0:
            violations.append(
                f"关节 {joint_name} 存在通信丢失 (lost={comm_lost})，实验数据不可信"
            )

        return SafetyCheckResult(passed=len(violations) == 0, violations=violations, warnings=warnings)

    def runtime_check(
        self,
        elapsed_s: float,
        current_torque_nm: float,
        current_velocity_rad_s: float,
        temp_rise_c: float,
        joint_name: str,
        joint_group: str = "default",
    ) -> Tuple[bool, Optional[str]]:
        """
        实验运行中的实时安全监控。

        Returns:
            (should_stop, reason_or_none)
        """
        bounds = self.get_bounds(joint_name, joint_group)

        if abs(current_torque_nm) > bounds.max_torque_nm:
            return True, (
                f"力矩超限：{current_torque_nm:.1f} Nm > {bounds.max_torque_nm:.1f} Nm，"
                f"紧急停止实验（已运行 {elapsed_s:.2f}s）"
            )

        if abs(current_velocity_rad_s) > bounds.max_velocity_rad_s:
            return True, (
                f"速度超限：{current_velocity_rad_s:.2f} rad/s > {bounds.max_velocity_rad_s:.1f} rad/s，"
                f"紧急停止实验"
            )

        if temp_rise_c > bounds.max_temp_rise_c:
            return True, (
                f"温升过快：{temp_rise_c:.1f}°C 超过单次实验允许上限 {bounds.max_temp_rise_c:.1f}°C"
            )

        return False, None
