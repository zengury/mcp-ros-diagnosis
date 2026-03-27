"""
MotionScenario — 运动场景定义库

把机器人的物理动作（走路、上楼、搬运等）翻译成可重复的
PID 实验配置序列。每个场景描述：
  - 哪个/哪些关节参与
  - 目标角度（阶跃幅度）
  - 运动持续时长
  - 典型负载特性（力矩、速度）
  - 场景上下文（供 LLM 调参时参考）

场景库的作用：
  研究员说 "模拟机器人上楼梯时左膝关节响应"
  → 找到场景 "stair_ascent"
  → 得到 setpoint_rad=1.1, duration_s=1.2, context="阶梯登升，需要高刚度低超调"
  → 作为 ExperimentConfig 的参数传入 ExperimentRunner
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ExperimentPhase:
    """
    运动场景中的单个实验阶段。

    一个运动（如"走路"）可能分多个阶段：
      - Phase 1: 抬腿（hip_pitch + knee 协同，大角度快速）
      - Phase 2: 落地缓冲（ankle，小角度，需低超调）
    每个阶段对应一次 ExperimentConfig。
    """
    joint_name: str           # 目标关节（如 "left_knee"）
    setpoint_rad: float       # 阶跃目标角度（rad）
    duration_s: float         # 实验时长（s）
    initial_position_rad: float = 0.0
    phase_label: str = ""     # 阶段标签（如 "swing_phase"），供日志/报告用
    phase_notes: str = ""     # 给 LLM 的物理提示（如 "承重期，需严格控制超调"）


@dataclass
class MotionScenario:
    """
    一个可重复的机器人运动场景。

    Attributes:
        scenario_id:      唯一标识（如 "stair_ascent"）
        name:             人类可读名称
        description:      场景描述（NL，供 LLM 调参上下文）
        robot_types:      适用的机器人类型（空=通用）
        joint_groups:     涉及的关节组
        phases:           按顺序执行的实验阶段列表
        target_score_hint: 对该场景建议的调参目标分数
        keywords:         用于关键词匹配（NL→场景的快速回退）
    """
    scenario_id: str
    name: str
    description: str
    phases: List[ExperimentPhase]
    robot_types: List[str] = field(default_factory=list)
    joint_groups: List[str] = field(default_factory=list)
    target_score_hint: float = 80.0
    keywords: List[str] = field(default_factory=list)

    def for_joint(self, joint_name: str) -> "MotionScenario":
        """
        返回一个新场景，把所有阶段的 joint_name 替换为指定关节。
        用于将通用场景应用到特定关节（如从 "left_knee" 换成 "right_knee"）。
        """
        new_phases = []
        for p in self.phases:
            new_phases.append(ExperimentPhase(
                joint_name=joint_name,
                setpoint_rad=p.setpoint_rad,
                duration_s=p.duration_s,
                initial_position_rad=p.initial_position_rad,
                phase_label=p.phase_label,
                phase_notes=p.phase_notes,
            ))
        return MotionScenario(
            scenario_id=self.scenario_id,
            name=self.name,
            description=self.description,
            phases=new_phases,
            robot_types=self.robot_types,
            joint_groups=self.joint_groups,
            target_score_hint=self.target_score_hint,
            keywords=self.keywords,
        )

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "description": self.description,
            "robot_types": self.robot_types,
            "joint_groups": self.joint_groups,
            "target_score_hint": self.target_score_hint,
            "phases": [
                {
                    "joint_name": p.joint_name,
                    "setpoint_rad": p.setpoint_rad,
                    "duration_s": p.duration_s,
                    "initial_position_rad": p.initial_position_rad,
                    "phase_label": p.phase_label,
                    "phase_notes": p.phase_notes,
                }
                for p in self.phases
            ],
        }


# ══════════════════════════════════════════════════════════════════════════════
# 预置场景库
# 关节名使用 G1 命名，通过 for_joint() 可适配其他机器人
# ══════════════════════════════════════════════════════════════════════════════

_LIBRARY: Dict[str, MotionScenario] = {}


def _reg(s: MotionScenario) -> MotionScenario:
    _LIBRARY[s.scenario_id] = s
    return s


# ── 静止 / 基准 ────────────────────────────────────────────────────────────────

_reg(MotionScenario(
    scenario_id="static_stand",
    name="静止站立",
    description="机器人双足站立，关节维持固定姿态。要求零稳态误差，允许适度响应时间。",
    robot_types=["unitree_g1", "unitree_b1"],
    joint_groups=["leg", "waist"],
    target_score_hint=85.0,
    keywords=["站立", "静止", "stand", "hold", "保持"],
    phases=[
        ExperimentPhase(
            joint_name="left_knee",
            setpoint_rad=0.4,
            duration_s=2.0,
            phase_label="static_hold",
            phase_notes="静态承重，稳态误差权重最高，允许较慢响应（rise_time<0.3s）",
        ),
    ],
))

_reg(MotionScenario(
    scenario_id="balance_perturbation",
    name="平衡扰动响应",
    description="外力推动机器人后，关节快速恢复平衡。要求快速上升，严格限制超调（超调>5%会摔倒）。",
    robot_types=["unitree_g1", "unitree_go2", "unitree_b1"],
    joint_groups=["leg", "waist"],
    target_score_hint=88.0,
    keywords=["平衡", "扰动", "恢复", "recovery", "perturbation", "推", "稳定"],
    phases=[
        ExperimentPhase(
            joint_name="left_hip_pitch",
            setpoint_rad=0.3,
            duration_s=1.0,
            phase_label="recovery_fast",
            phase_notes="平衡恢复，上升时间 <0.15s，超调必须 <5%，否则摔倒",
        ),
        ExperimentPhase(
            joint_name="left_knee",
            setpoint_rad=0.5,
            duration_s=1.2,
            initial_position_rad=0.0,
            phase_label="recovery_stabilize",
            phase_notes="膝关节吸收冲击，需要适度阻尼防止振荡",
        ),
    ],
))

# ── 步行 / 奔跑 ────────────────────────────────────────────────────────────────

_reg(MotionScenario(
    scenario_id="normal_walking",
    name="正常步行",
    description="典型人形步态，摆动相+支撑相交替，关节做周期性运动。",
    robot_types=["unitree_g1"],
    joint_groups=["leg"],
    target_score_hint=82.0,
    keywords=["走路", "步行", "walking", "gait", "步态", "行走"],
    phases=[
        ExperimentPhase(
            joint_name="left_knee",
            setpoint_rad=0.8,
            duration_s=0.6,
            phase_label="swing_flexion",
            phase_notes="摆动相屈膝，快速响应（rise_time<0.1s），允许小超调（<10%）",
        ),
        ExperimentPhase(
            joint_name="left_knee",
            setpoint_rad=0.1,
            duration_s=0.5,
            initial_position_rad=0.8,
            phase_label="stance_extension",
            phase_notes="支撑相伸膝，需要低超调，防止膝关节锁死（超调<5%）",
        ),
    ],
))

_reg(MotionScenario(
    scenario_id="fast_walking",
    name="快步行走",
    description="加速步态，步频提高约 30%，对关节响应速度要求更高。",
    robot_types=["unitree_g1"],
    joint_groups=["leg"],
    target_score_hint=80.0,
    keywords=["快走", "加速", "fast walk", "快速行走", "提速"],
    phases=[
        ExperimentPhase(
            joint_name="left_knee",
            setpoint_rad=1.0,
            duration_s=0.4,
            phase_label="fast_swing",
            phase_notes="快速摆动，上升时间要求 <0.08s，允许超调 <15%",
        ),
        ExperimentPhase(
            joint_name="left_hip_pitch",
            setpoint_rad=0.45,
            duration_s=0.35,
            phase_label="fast_hip_swing",
            phase_notes="髋部快速摆动，需要充分的 Kd 防止振荡",
        ),
    ],
))

# ── 楼梯 ────────────────────────────────────────────────────────────────────────

_reg(MotionScenario(
    scenario_id="stair_ascent",
    name="上楼梯",
    description="单脚抬起约 20cm 台阶，大角度屈膝屈髋，支撑腿承受较大力矩。",
    robot_types=["unitree_g1"],
    joint_groups=["leg"],
    target_score_hint=85.0,
    keywords=["上楼", "爬楼", "楼梯", "stair", "台阶", "ascent", "上台阶"],
    phases=[
        ExperimentPhase(
            joint_name="left_knee",
            setpoint_rad=1.2,
            duration_s=0.8,
            phase_label="step_up_flex",
            phase_notes="大角度屈膝（约70°），上楼梯最大角度，严格控制超调防止碰台阶",
        ),
        ExperimentPhase(
            joint_name="left_hip_pitch",
            setpoint_rad=0.7,
            duration_s=0.9,
            phase_label="hip_flex_step",
            phase_notes="髋部前屈配合抬腿，需与膝关节协调",
        ),
    ],
))

_reg(MotionScenario(
    scenario_id="stair_descent",
    name="下楼梯",
    description="控制落脚缓冲，膝关节需吸收冲击力，要求极低超调。",
    robot_types=["unitree_g1"],
    joint_groups=["leg"],
    target_score_hint=88.0,
    keywords=["下楼", "下台阶", "stair descent", "降台阶", "下坡"],
    phases=[
        ExperimentPhase(
            joint_name="left_knee",
            setpoint_rad=0.6,
            duration_s=1.0,
            initial_position_rad=1.2,
            phase_label="landing_absorption",
            phase_notes="落地缓冲，从屈曲恢复，超调>5%会踩空，严格控制",
        ),
        ExperimentPhase(
            joint_name="left_ankle_pitch",
            setpoint_rad=0.3,
            duration_s=0.7,
            phase_label="ankle_push_off",
            phase_notes="踝关节缓冲推力，小角度高精度控制",
        ),
    ],
))

# ── 蹲起 ────────────────────────────────────────────────────────────────────────

_reg(MotionScenario(
    scenario_id="squat",
    name="深蹲",
    description="双膝深蹲至约 90°，测试大角度高负载下的跟踪性能。",
    robot_types=["unitree_g1"],
    joint_groups=["leg"],
    target_score_hint=82.0,
    keywords=["蹲", "深蹲", "squat", "下蹲", "蹲起", "crouch"],
    phases=[
        ExperimentPhase(
            joint_name="left_knee",
            setpoint_rad=1.57,
            duration_s=1.5,
            phase_label="squat_down",
            phase_notes="深蹲约90°，最大承重工况，Kp需要足够大维持位置，注意热保护",
        ),
        ExperimentPhase(
            joint_name="left_knee",
            setpoint_rad=0.1,
            duration_s=1.0,
            initial_position_rad=1.57,
            phase_label="stand_up",
            phase_notes="站起，从深蹲恢复，需要足够输出力矩，控制速度防止踉跄",
        ),
    ],
))

_reg(MotionScenario(
    scenario_id="single_leg_stance",
    name="单腿站立",
    description="单腿承担全部体重（约 35kg），对支撑腿膝关节和髋关节刚度要求最高。",
    robot_types=["unitree_g1"],
    joint_groups=["leg"],
    target_score_hint=90.0,
    keywords=["单腿", "单足", "金鸡独立", "single leg", "支撑腿", "抬脚"],
    phases=[
        ExperimentPhase(
            joint_name="left_knee",
            setpoint_rad=0.35,
            duration_s=2.5,
            phase_label="single_support",
            phase_notes="单腿承重全身35kg，力矩最大工况。稳态误差<1%，超调<3%，否则摔倒",
        ),
        ExperimentPhase(
            joint_name="left_hip_roll",
            setpoint_rad=0.15,
            duration_s=2.0,
            phase_label="lateral_balance",
            phase_notes="侧向平衡关键关节，极低超调要求",
        ),
    ],
))

# ── 手臂动作 ────────────────────────────────────────────────────────────────────

_reg(MotionScenario(
    scenario_id="arm_wave",
    name="挥手",
    description="手臂快速大幅度摆动，负载轻，主要测试响应速度和振荡抑制。",
    robot_types=["unitree_g1"],
    joint_groups=["arm"],
    target_score_hint=78.0,
    keywords=["挥手", "摆臂", "wave", "swing arm", "手臂摆动"],
    phases=[
        ExperimentPhase(
            joint_name="left_shoulder_pitch",
            setpoint_rad=1.2,
            duration_s=0.5,
            phase_label="arm_raise",
            phase_notes="快速抬臂，惯量小，主要优化振荡计数（手臂末端颤抖）",
        ),
        ExperimentPhase(
            joint_name="left_shoulder_pitch",
            setpoint_rad=-0.5,
            duration_s=0.4,
            initial_position_rad=1.2,
            phase_label="arm_lower",
            phase_notes="快速落臂，关注末端位置精度",
        ),
    ],
))

_reg(MotionScenario(
    scenario_id="arm_reach",
    name="伸手抓取",
    description="手臂缓慢精确伸向目标点，低速高精度，末端轨迹误差要小。",
    robot_types=["unitree_g1", "xarm7"],
    joint_groups=["arm"],
    target_score_hint=88.0,
    keywords=["抓取", "伸手", "reach", "grab", "pick", "拾取", "末端精度", "精确"],
    phases=[
        ExperimentPhase(
            joint_name="left_elbow",
            setpoint_rad=1.0,
            duration_s=1.5,
            phase_label="elbow_extend",
            phase_notes="精确伸肘，稳态误差权重最高，允许较慢响应",
        ),
        ExperimentPhase(
            joint_name="left_wrist_pitch",
            setpoint_rad=0.5,
            duration_s=1.2,
            phase_label="wrist_orient",
            phase_notes="腕部对准，小惯量精确控制，防止超调导致抓取偏位",
        ),
    ],
))

_reg(MotionScenario(
    scenario_id="heavy_carry",
    name="搬运重物",
    description="双臂持重约 2kg，关节需克服额外力矩。测试积分项（Ki）的必要性。",
    robot_types=["unitree_g1", "xarm7"],
    joint_groups=["arm"],
    target_score_hint=83.0,
    keywords=["搬运", "持重", "carry", "load", "重物", "负载", "拿东西"],
    phases=[
        ExperimentPhase(
            joint_name="left_elbow",
            setpoint_rad=1.2,
            duration_s=2.0,
            phase_label="loaded_hold",
            phase_notes="持重保持，需要足够 Ki 消除重力引起的稳态误差，负载约2kg",
        ),
        ExperimentPhase(
            joint_name="left_shoulder_pitch",
            setpoint_rad=0.8,
            duration_s=1.5,
            phase_label="shoulder_loaded",
            phase_notes="肩部承重最大工况，力矩约为空载的3倍",
        ),
    ],
))

# ── 四足专用 ──────────────────────────────────────────────────────────────────

_reg(MotionScenario(
    scenario_id="quadruped_trot",
    name="四足对角步态（Trot）",
    description="Go2/B1 标准对角步态，两条对角腿同步摆动，关节快速周期运动。",
    robot_types=["unitree_go2", "unitree_b1"],
    joint_groups=["leg"],
    target_score_hint=80.0,
    keywords=["trot", "对角步", "小跑", "quadruped", "四足行走", "跑步"],
    phases=[
        ExperimentPhase(
            joint_name="lf_thigh",
            setpoint_rad=0.9,
            duration_s=0.3,
            phase_label="trot_swing",
            phase_notes="Trot 摆动相，高速（约3 Hz步频），上升时间<0.08s",
        ),
        ExperimentPhase(
            joint_name="lf_calf",
            setpoint_rad=-1.4,
            duration_s=0.3,
            phase_label="trot_calf",
            phase_notes="小腿快速折叠，与大腿协同，注意共振频率避让",
        ),
    ],
))

_reg(MotionScenario(
    scenario_id="quadruped_jump",
    name="四足跳跃",
    description="Go2 跳跃动作，腿部爆发力测试，需要极快上升时间（<50ms）。",
    robot_types=["unitree_go2"],
    joint_groups=["leg"],
    target_score_hint=75.0,
    keywords=["jump", "跳跃", "弹跳", "跳", "起跳"],
    phases=[
        ExperimentPhase(
            joint_name="lf_thigh",
            setpoint_rad=1.5,
            duration_s=0.2,
            phase_label="jump_load",
            phase_notes="跳跃蓄力，瞬间大力矩（接近额定23.7Nm），上升时间<0.05s",
        ),
    ],
))

# ── xArm 专用 ─────────────────────────────────────────────────────────────────

_reg(MotionScenario(
    scenario_id="xarm_assembly",
    name="装配作业",
    description="xArm7 精密装配，低速高精度，对末端轨迹精度要求严格（±0.1mm）。",
    robot_types=["xarm7"],
    joint_groups=["arm"],
    target_score_hint=92.0,
    keywords=["装配", "assembly", "精密", "插针", "精确", "高精度", "拧螺丝"],
    phases=[
        ExperimentPhase(
            joint_name="joint6",
            setpoint_rad=0.8,
            duration_s=2.0,
            phase_label="precision_orient",
            phase_notes="精密定向，稳态误差<0.1%，超调<2%，装配精度要求最高",
        ),
        ExperimentPhase(
            joint_name="joint4",
            setpoint_rad=0.5,
            duration_s=1.8,
            phase_label="approach",
            phase_notes="接近工件，低速精确，振荡会导致零件损坏",
        ),
    ],
))

_reg(MotionScenario(
    scenario_id="xarm_welding",
    name="焊接轨迹跟踪",
    description="xArm7 沿焊缝匀速移动，对速度稳定性要求高（速度波动<5%）。",
    robot_types=["xarm7"],
    joint_groups=["arm"],
    target_score_hint=85.0,
    keywords=["焊接", "welding", "轨迹", "路径", "匀速", "连续"],
    phases=[
        ExperimentPhase(
            joint_name="joint3",
            setpoint_rad=1.0,
            duration_s=3.0,
            phase_label="weld_path",
            phase_notes="焊接路径跟踪，上升后需稳定维持，速度波动直接影响焊缝质量",
        ),
    ],
))


class ScenarioLibrary:
    """
    预置场景查询接口。

    主要用途：
      1. 列出所有可用场景
      2. 按 scenario_id 精确查找
      3. 按 robot_type 过滤
      4. 关键词模糊匹配（作为 ScenarioInterpreter 的回退）
    """

    def all(self) -> List[MotionScenario]:
        return list(_LIBRARY.values())

    def get(self, scenario_id: str) -> Optional[MotionScenario]:
        return _LIBRARY.get(scenario_id)

    def for_robot(self, robot_type: str) -> List[MotionScenario]:
        """返回适用于指定机器人的所有场景（含通用场景）"""
        return [
            s for s in _LIBRARY.values()
            if not s.robot_types or robot_type in s.robot_types
        ]

    def keyword_match(self, text: str) -> Optional[MotionScenario]:
        """
        关键词模糊匹配：在 text 中查找已知关键词，返回匹配得分最高的场景。
        这是 ScenarioInterpreter 在 LLM 不可用时的回退方法。
        """
        text_lower = text.lower()
        best_scenario = None
        best_hits = 0

        for scenario in _LIBRARY.values():
            hits = sum(1 for kw in scenario.keywords if kw.lower() in text_lower)
            if hits > best_hits:
                best_hits = hits
                best_scenario = scenario

        return best_scenario if best_hits > 0 else None

    def summary(self) -> List[dict]:
        """返回场景摘要列表（供 MCP 工具展示）"""
        return [
            {
                "scenario_id": s.scenario_id,
                "name": s.name,
                "description": s.description,
                "robot_types": s.robot_types,
                "joint_groups": s.joint_groups,
                "phase_count": len(s.phases),
                "target_score_hint": s.target_score_hint,
                "keywords": s.keywords,
            }
            for s in _LIBRARY.values()
        ]
