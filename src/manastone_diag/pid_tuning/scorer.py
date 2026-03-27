"""
PID 阶跃响应质量评分器

把一段时间序列"翻译"成 0-100 分，解决的核心挑战是：
    让 Agent 像有经验的工程师一样"读懂"波形。

评分维度：
  - 超调量 (Overshoot)     ：峰值超出目标的百分比
  - 上升时间 (Rise Time)   ：从 10% 到 90% 目标值的时间
  - 调节时间 (Settling Time)：最后一次离开 ±2% 误差带的时间
  - 稳态误差 (SSE)         ：稳定后与目标值的偏差
  - 振荡程度 (Oscillation) ：误差符号翻转次数

总分 = 100 - 各维度扣分之和（最低 0 分）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class StepResponseMetrics:
    """从时间序列中提取的控制性能指标"""

    # 原始配置
    setpoint: float
    duration_s: float
    dt_s: float

    # 核心指标（由 compute_metrics 填充）
    overshoot_pct: float = 0.0          # 超调量 %（负值 = 欠调，通常扣分较少）
    rise_time_s: float = 0.0            # 上升时间 s
    settling_time_s: float = 0.0        # 调节时间 s
    sse_pct: float = 0.0                # 稳态误差 %（相对目标值）
    iae: float = 0.0                    # 积分绝对误差（归一化）
    oscillation_count: int = 0          # 误差过零次数
    peak_torque_nm: float = 0.0         # 实验期间峰值力矩 Nm
    max_velocity_rad_s: float = 0.0     # 最大关节速度 rad/s

    # 最终得分
    score: float = 0.0
    grade: str = ""                     # A/B/C/D/F
    diagnosis: List[str] = field(default_factory=list)  # 文字诊断


def compute_metrics(
    times: List[float],
    positions: List[float],
    setpoint: float,
    torques: Optional[List[float]] = None,
    velocities: Optional[List[float]] = None,
    settle_band_pct: float = 0.02,      # ±2% 调节带
) -> StepResponseMetrics:
    """
    从阶跃响应时间序列计算全套控制性能指标。

    Args:
        times:      时间戳列表 [s]
        positions:  关节位置列表 [rad]
        setpoint:   目标位置 [rad]
        torques:    力矩列表（可选）[Nm]
        velocities: 速度列表（可选）[rad/s]
        settle_band_pct: 调节带宽（相对目标值的百分比）

    Returns:
        StepResponseMetrics，包含所有指标和综合评分
    """
    n = len(positions)
    if n < 10 or setpoint == 0:
        return StepResponseMetrics(setpoint=setpoint, duration_s=0, dt_s=0)

    dt = times[1] - times[0] if len(times) > 1 else 0.01
    duration = times[-1] - times[0]

    # ── 1. 超调量 ──────────────────────────────────────────────
    peak = max(positions)
    overshoot_pct = (peak - setpoint) / abs(setpoint) * 100 if peak > setpoint else 0.0

    # ── 2. 上升时间（10% → 90%）───────────────────────────────
    t10 = _find_first_crossing(times, positions, 0.1 * setpoint)
    t90 = _find_first_crossing(times, positions, 0.9 * setpoint)
    rise_time_s = (t90 - t10) if (t10 is not None and t90 is not None) else duration

    # ── 3. 调节时间（最后一次离开 ±band 的时间）──────────────
    band = abs(setpoint) * settle_band_pct
    settling_time_s = duration  # 默认：全程未调节
    for i in range(n - 1, -1, -1):
        if abs(positions[i] - setpoint) > band:
            settling_time_s = times[i]
            break
    else:
        settling_time_s = 0.0  # 从未离开过调节带（瞬间到达）

    # ── 4. 稳态误差（后 20% 时间段均值）──────────────────────
    tail_start = int(n * 0.8)
    ss_value = sum(positions[tail_start:]) / max(1, n - tail_start)
    sse_pct = abs(ss_value - setpoint) / abs(setpoint) * 100

    # ── 5. 积分绝对误差（IAE，归一化）────────────────────────
    errors = [abs(p - setpoint) for p in positions]
    iae = sum(errors) * dt / (abs(setpoint) * duration + 1e-9)

    # ── 6. 振荡次数（误差符号翻转次数）──────────────────────
    signed_errors = [p - setpoint for p in positions]
    oscillation_count = sum(
        1 for i in range(1, len(signed_errors))
        if signed_errors[i] * signed_errors[i - 1] < 0
    )

    # ── 7. 力矩 / 速度峰值（安全相关）────────────────────────
    peak_torque_nm = max(torques) if torques else 0.0
    max_velocity_rad_s = max(abs(v) for v in velocities) if velocities else 0.0

    # ── 综合评分 ───────────────────────────────────────────────
    score, diagnosis = _compute_score(
        overshoot_pct, rise_time_s, settling_time_s, sse_pct, oscillation_count
    )
    grade = _score_to_grade(score)

    return StepResponseMetrics(
        setpoint=setpoint,
        duration_s=duration,
        dt_s=dt,
        overshoot_pct=round(overshoot_pct, 2),
        rise_time_s=round(rise_time_s, 3),
        settling_time_s=round(settling_time_s, 3),
        sse_pct=round(sse_pct, 2),
        iae=round(iae, 4),
        oscillation_count=oscillation_count,
        peak_torque_nm=round(peak_torque_nm, 2),
        max_velocity_rad_s=round(max_velocity_rad_s, 3),
        score=round(score, 1),
        grade=grade,
        diagnosis=diagnosis,
    )


def _find_first_crossing(
    times: List[float], values: List[float], threshold: float
) -> Optional[float]:
    """找到 values 第一次超过 threshold 的时间（线性插值）"""
    for i in range(1, len(values)):
        if values[i] >= threshold and values[i - 1] < threshold:
            # 线性插值
            ratio = (threshold - values[i - 1]) / (values[i] - values[i - 1])
            return times[i - 1] + ratio * (times[i] - times[i - 1])
    return None


def _compute_score(
    overshoot_pct: float,
    rise_time_s: float,
    settling_time_s: float,
    sse_pct: float,
    oscillation_count: int,
) -> Tuple[float, List[str]]:
    """
    计算综合评分，同时输出各维度诊断文字。

    扣分规则：
      超调量：  0% → 0分, 10% → -15分, 20%+ → -30分
      上升时间：<0.3s → 0分, 1.0s → -20分
      调节时间：<0.5s → 0分, 2.0s → -25分
      稳态误差：<0.5% → 0分, 5%+ → -25分
      振荡：    ≤2次 → 0分, 10次+ → -15分（额外）
    """
    diagnosis = []

    # 超调扣分（分段线性）
    if overshoot_pct <= 0:
        overshoot_penalty = 0.0
    elif overshoot_pct <= 10:
        overshoot_penalty = overshoot_pct * 1.5
    else:
        overshoot_penalty = 15 + (overshoot_pct - 10) * 1.5
    overshoot_penalty = min(overshoot_penalty, 30)

    if overshoot_pct > 20:
        diagnosis.append(f"超调严重 ({overshoot_pct:.1f}%)：Kp 可能过大，或 Kd 不足")
    elif overshoot_pct > 5:
        diagnosis.append(f"超调偏高 ({overshoot_pct:.1f}%)：可适当增大 Kd 抑制")

    # 上升时间扣分
    rise_penalty = max(0.0, min(20.0, (rise_time_s - 0.3) / 0.7 * 20))

    if rise_time_s > 1.0:
        diagnosis.append(f"响应偏慢（上升时间 {rise_time_s:.2f}s）：可适当增大 Kp")
    elif rise_time_s > 0.5:
        diagnosis.append(f"上升时间一般（{rise_time_s:.2f}s）：有继续提速空间")

    # 调节时间扣分
    settle_penalty = max(0.0, min(25.0, (settling_time_s - 0.5) / 1.5 * 25))

    if settling_time_s > 1.5:
        diagnosis.append(f"调节缓慢（{settling_time_s:.2f}s）：系统振荡或 Ki 过小")
    elif settling_time_s > 0.8:
        diagnosis.append(f"调节时间偏长（{settling_time_s:.2f}s）：可微调 Ki/Kd")

    # 稳态误差扣分
    sse_penalty = max(0.0, min(25.0, sse_pct * 5))

    if sse_pct > 3.0:
        diagnosis.append(f"稳态误差过大 ({sse_pct:.1f}%)：Ki 不足，无法消除静差")
    elif sse_pct > 1.0:
        diagnosis.append(f"稳态误差偏高 ({sse_pct:.1f}%)：适当增大 Ki")

    # 振荡额外扣分
    osc_penalty = 0.0
    if oscillation_count > 10:
        osc_penalty = min(15.0, (oscillation_count - 2) * 1.0)
        diagnosis.append(f"持续振荡（{oscillation_count}次过零）：系统不稳定，Kp 可能过大")
    elif oscillation_count > 5:
        diagnosis.append(f"轻微振荡（{oscillation_count}次）：Kd 可适当增大")

    total_penalty = overshoot_penalty + rise_penalty + settle_penalty + sse_penalty + osc_penalty
    score = max(0.0, 100.0 - total_penalty)

    if not diagnosis:
        diagnosis.append("响应质量良好，各项指标均在合理范围内")

    return score, diagnosis


def _score_to_grade(score: float) -> str:
    if score >= 90:
        return "A"
    elif score >= 75:
        return "B"
    elif score >= 60:
        return "C"
    elif score >= 40:
        return "D"
    else:
        return "F"
