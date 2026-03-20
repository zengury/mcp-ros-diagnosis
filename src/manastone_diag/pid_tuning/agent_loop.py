"""
PID 调参 Agent 循环 — "不达目标不罢休"的 LLM 驱动迭代

═══════════════════════════════════════════════════════════════
架构说明（与 pid_run_auto_tuning 的本质区别）：

  pid_run_auto_tuning   ← Python for-loop，LLM 是子函数
  PIDAgentLoop          ← LLM while-loop，Python 是工具执行者

LLM 的角色：
  - 研究员：分析每次实验结果，决定下一步调整方向
  - 决策者：自主选择调用哪个工具（实验/查历史/结束）
  - 终止者：只有 LLM 认为达标后才能调用 finish

Python 的角色：
  - 执行者：运行 LLM 决定调用的工具
  - 守卫者：拒绝 LLM 的提前 finish（分数未达标时注入"继续"消息）
  - 安全网：max_experiments 到达上限时强制停止

═══════════════════════════════════════════════════════════════
"不达目标不罢休"的实现机制：

  每次实验后：
    if score >= target:
        允许 LLM 调用 finish → 结束
    else:
        if LLM 调用了 finish → 拒绝，注入"继续"消息
        自动追加 user 消息 → "仍未达标，请继续"

  这样 LLM 即使想停，也会被 Python 推着继续实验。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .experiment import ExperimentConfig, ExperimentRunner
from .optimizer import TuningHistory
from .safety import SafetyGuard

logger = logging.getLogger(__name__)


# ── 工具定义（OpenAI function-calling 格式）─────────────────
AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_experiment",
            "description": (
                "执行一次 PID 阶跃响应实验，返回完整的控制性能评分和诊断。"
                "每次调用都会自动记录到历史。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kp": {"type": "number", "description": "比例增益（必须在安全边界内）"},
                    "ki": {"type": "number", "description": "积分增益"},
                    "kd": {"type": "number", "description": "微分增益"},
                    "reasoning": {
                        "type": "string",
                        "description": "你选择这组参数的工程依据（必填，体现你的分析过程）",
                    },
                },
                "required": ["kp", "ki", "kd", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_history",
            "description": (
                "获取本次调参会话的所有历史实验记录，用于分析趋势、"
                "避免重复尝试已失败的参数组合。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "结束调参会话，提交最终推荐参数。"
                "注意：只有当最新实验得分已达到目标分数时，系统才会接受此调用。"
                "如果分数未达标，此调用会被拒绝，你需要继续实验。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "final_kp": {"type": "number"},
                    "final_ki": {"type": "number"},
                    "final_kd": {"type": "number"},
                    "final_score": {"type": "number", "description": "该参数组合的实测得分"},
                    "conclusion": {
                        "type": "string",
                        "description": "调参结论：过程总结、参数物理意义、后续建议（100字内）",
                    },
                },
                "required": ["final_kp", "final_ki", "final_kd", "final_score", "conclusion"],
            },
        },
    },
]

SYSTEM_PROMPT = """\
你是一位精通经典控制理论的 PID 调参研究员，目标是通过反复实验为机器人关节整定出满足性能要求的 PID 参数。

你有三个工具：
  run_experiment(kp, ki, kd, reasoning) — 执行阶跃响应实验，获取评分（0-100）和诊断
  get_history()                          — 查看所有历史实验，分析趋势
  finish(...)                            — 提交最终结论（仅当分数达标时有效）

【核心原则：不达目标不停止】
你必须持续实验，直到达到目标分数。进展缓慢不是停止的理由。
当 finish 被系统拒绝时，说明分数未达标，你必须继续调整。

调参方法论：
1. 先建立对系统的认知：从中等保守参数出发，观察系统是欠阻尼还是过阻尼
2. 阶段性整定：先整定 Kp 让系统响应，再加 Kd 抑制超调，最后用 Ki 消除稳态误差
3. 每次调整幅度：分数较低时大幅调整（±30-50%），接近目标时小幅精调（±5-10%）
4. 利用历史：调用 get_history 分析哪些参数方向有效，避免重复踩坑

控制理论提示：
  超调 > 20%   → Kp 过大 或 Kd 不足，优先增大 Kd
  上升慢        → Kp 不足，增大 Kp
  稳态误差 > 3% → Ki 不足，小幅增加 Ki（每次 +0.05 ~ +0.1）
  持续振荡      → 系统不稳定，大幅减 Kp（×0.6），增 Kd，Ki 归零

reasoning 字段要求：必须填写，体现你的分析逻辑，如"上一次超调18%，Kd增大30%以抑制"。
"""


def _continue_message(current_best: float, target: float, experiment_count: int) -> str:
    """生成推动 LLM 继续实验的 user 消息"""
    gap = target - current_best
    if gap > 30:
        hint = "分数差距较大，可能需要大幅调整参数方向。建议先调用 get_history 梳理规律，再尝试新参数。"
    elif gap > 15:
        hint = "距目标还有一段距离，请分析当前瓶颈在哪个控制维度（超调/速度/稳态误差），针对性调整。"
    elif gap > 5:
        hint = "接近目标，请小幅精调参数，每次调整幅度控制在 5-10%。"
    else:
        hint = "非常接近目标！细心微调，优先从稳态误差或超调中任选一个维度着手。"

    return (
        f"当前最高得分：{current_best:.1f}/100，目标：{target}/100，"
        f"已进行 {experiment_count} 次实验。\n"
        f"尚未达到目标，请继续。{hint}"
    )


def _reject_finish_message(score_claimed: float, best_score: float, target: float) -> str:
    """LLM 提前调用 finish 时的拒绝消息"""
    return (
        f"finish 调用被拒绝：你提交的得分 {score_claimed:.1f} "
        f"（当前最高 {best_score:.1f}）未达到目标 {target}。\n"
        f"请继续实验，分析当前瓶颈并调整参数。"
    )


@dataclass
class AgentLoopResult:
    joint_name: str
    total_turns: int
    total_experiments: int
    elapsed_s: float
    best_score: float
    best_params: Dict[str, float]
    final_conclusion: str
    target_reached: bool
    turn_log: List[Dict[str, Any]] = field(default_factory=list)
    stopped_by: str = ""   # "llm_finish" | "max_experiments" | "llm_error"

    def to_dict(self) -> dict:
        return {
            "joint_name": self.joint_name,
            "total_turns": self.total_turns,
            "total_experiments": self.total_experiments,
            "elapsed_s": round(self.elapsed_s, 1),
            "best_score": round(self.best_score, 1),
            "best_params": self.best_params,
            "final_conclusion": self.final_conclusion,
            "target_reached": self.target_reached,
            "stopped_by": self.stopped_by,
            "turn_log": self.turn_log,
        }


class PIDAgentLoop:
    """
    "不达目标不罢休"的 PID 调参 Agent 循环。

    LLM 控制外层 while 循环，Python 负责：
      1. 执行 LLM 决定调用的工具
      2. 拒绝分数未达标时的 finish 调用（注入拒绝消息 → LLM 继续）
      3. 每次实验后自动追加"继续"消息（直到 LLM 自主决定 finish）
      4. max_experiments 上限作为安全兜底
    """

    def __init__(
        self,
        llm_client: Any,
        runner: ExperimentRunner,
        history: TuningHistory,
        safety: SafetyGuard,
    ):
        self.llm = llm_client
        self.runner = runner
        self.history = history
        self.safety = safety

    async def run(
        self,
        joint_name: str,
        joint_group: str,
        target_score: float,
        max_experiments: int,   # 安全兜底：最多跑多少次实验
        bounds: Any,
        setpoint_rad: float = 0.5,
        experiment_duration_s: float = 2.0,
    ) -> AgentLoopResult:
        start_time = time.time()
        turn_log: List[Dict[str, Any]] = []
        best_score = 0.0
        best_params: Dict[str, float] = {}
        total_experiments = 0
        total_turns = 0
        final_conclusion = ""
        stopped_by = "max_experiments"

        messages: List[Dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"关节：{joint_name}（组别：{joint_group}）\n"
                    f"调参目标：综合评分 ≥ {target_score}/100\n"
                    f"安全边界：Kp ∈ [{bounds.kp_min}, {bounds.kp_max}]  "
                    f"Ki ∈ [{bounds.ki_min}, {bounds.ki_max}]  "
                    f"Kd ∈ [{bounds.kd_min}, {bounds.kd_max}]\n"
                    f"阶跃目标：{setpoint_rad} rad，实验时长：{experiment_duration_s}s\n"
                    f"最大实验次数（安全限制）：{max_experiments}\n\n"
                    f"请开始调参。记住：不达目标不停止。"
                )
            }
        ]

        # ════════════════════════════════════════════════════
        # 主循环：LLM 驱动，无硬性轮数限制
        # 唯一出口：① LLM 调用 finish 且分数达标
        #            ② total_experiments >= max_experiments（安全网）
        # ════════════════════════════════════════════════════
        while total_experiments < max_experiments:
            total_turns += 1
            turn_entry: Dict[str, Any] = {"turn": total_turns, "actions": []}

            # ── 一次 LLM 调用 ────────────────────────────────
            try:
                msg = await self.llm.chat_with_tools(
                    messages=messages,
                    tools=AGENT_TOOLS,
                    system_prompt=SYSTEM_PROMPT,
                )
            except Exception as e:
                logger.error("LLM 调用失败（turn=%d）: %s", total_turns, e)
                stopped_by = "llm_error"
                turn_log.append(turn_entry)
                break

            assistant_msg = {k: v for k, v in msg.items() if not k.startswith("_")}
            messages.append(assistant_msg)
            total_turns += 0  # 已在循环顶部计数

            tool_calls = msg.get("tool_calls") or []

            # LLM 直接文字回复（没有 tool_calls）→ 注入"继续"消息推它继续
            if not tool_calls:
                text = msg.get("content", "")
                turn_entry["actions"].append({"type": "text_only", "content": text})
                logger.debug("LLM 文字回复（无 tool_call），注入继续消息")
                messages.append({
                    "role": "user",
                    "content": _continue_message(best_score, target_score, total_experiments),
                })
                turn_log.append(turn_entry)
                continue

            # ── 执行 LLM 决定调用的工具 ──────────────────────
            tool_results: List[Dict[str, Any]] = []
            experiment_ran_this_turn = False
            finish_accepted = False

            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                action_entry: Dict[str, Any] = {"tool": tool_name, "args": args}

                # ── run_experiment ───────────────────────────
                if tool_name == "run_experiment":
                    if total_experiments >= max_experiments:
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": f"实验次数已达上限（{max_experiments}），无法继续。",
                        })
                        action_entry["blocked"] = "max_experiments"
                    else:
                        result_str, score, params = await self._execute_experiment(
                            joint_name, joint_group, args, bounds, setpoint_rad, experiment_duration_s
                        )
                        total_experiments += 1
                        experiment_ran_this_turn = True
                        action_entry["score"] = score
                        action_entry["params"] = params

                        if score > best_score:
                            best_score = score
                            best_params = params.copy()

                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result_str,
                        })

                # ── get_history ──────────────────────────────
                elif tool_name == "get_history":
                    records = self.history.recent(joint_name, 20)
                    action_entry["records"] = len(records)
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(records, ensure_ascii=False),
                    })

                # ── finish ───────────────────────────────────
                elif tool_name == "finish":
                    claimed_score = float(args.get("final_score", 0))

                    if best_score >= target_score:
                        # ✅ 分数达标，接受 finish
                        final_conclusion = args.get("conclusion", "")
                        reported_kp = args.get("final_kp", best_params.get("kp", 0))
                        reported_ki = args.get("final_ki", best_params.get("ki", 0))
                        reported_kd = args.get("final_kd", best_params.get("kd", 0))
                        # 以历史最优为准（防止 LLM 报错参数）
                        if claimed_score >= best_score:
                            best_params = {
                                "kp": reported_kp,
                                "ki": reported_ki,
                                "kd": reported_kd,
                            }
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": "调参成功！系统已记录最优参数。",
                        })
                        action_entry["accepted"] = True
                        finish_accepted = True
                        stopped_by = "llm_finish"
                        logger.info(
                            "LLM 成功完成调参（turn=%d）：best_score=%.1f",
                            total_turns, best_score,
                        )
                    else:
                        # ❌ 分数未达标，拒绝 finish，注入继续消息
                        reject_msg = _reject_finish_message(claimed_score, best_score, target_score)
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": reject_msg,
                        })
                        action_entry["accepted"] = False
                        action_entry["reject_reason"] = reject_msg
                        logger.info(
                            "拒绝 LLM 的 finish 调用（best=%.1f < target=%.1f）",
                            best_score, target_score,
                        )

                else:
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"未知工具: {tool_name}",
                    })

                turn_entry["actions"].append(action_entry)

            messages.extend(tool_results)
            turn_log.append(turn_entry)

            # finish 被接受 → 退出主循环
            if finish_accepted:
                break

            # 本轮跑了实验且分数未达标 → 自动注入"继续"消息，推动 LLM 继续
            if experiment_ran_this_turn and best_score < target_score:
                messages.append({
                    "role": "user",
                    "content": _continue_message(best_score, target_score, total_experiments),
                })

        # ── 循环结束（安全网触发）────────────────────────────
        if stopped_by == "max_experiments" and not final_conclusion:
            best = self.history.best(joint_name)
            if best:
                final_conclusion = (
                    f"已达最大实验次数限制（{max_experiments}次），"
                    f"未能达到目标分数 {target_score}。"
                    f"历史最优：Kp={best_params.get('kp', '?')} "
                    f"Ki={best_params.get('ki', '?')} "
                    f"Kd={best_params.get('kd', '?')}，得分={best_score:.1f}。"
                )

        return AgentLoopResult(
            joint_name=joint_name,
            total_turns=total_turns,
            total_experiments=total_experiments,
            elapsed_s=time.time() - start_time,
            best_score=best_score,
            best_params=best_params,
            final_conclusion=final_conclusion,
            target_reached=best_score >= target_score,
            turn_log=turn_log,
            stopped_by=stopped_by,
        )

    async def _execute_experiment(
        self,
        joint_name: str,
        joint_group: str,
        args: Dict[str, Any],
        bounds: Any,
        setpoint_rad: float,
        duration_s: float,
    ) -> Tuple[str, float, Dict[str, float]]:
        kp = float(args.get("kp", 1.0))
        ki = float(args.get("ki", 0.0))
        kd = float(args.get("kd", 0.0))

        # 安全钳制（LLM 可能无视边界，这里强制而非拒绝，同时告知 LLM）
        kp_c = max(bounds.kp_min, min(bounds.kp_max, kp))
        ki_c = max(bounds.ki_min, min(bounds.ki_max, ki))
        kd_c = max(bounds.kd_min, min(bounds.kd_max, kd))
        was_clamped = (kp != kp_c or ki != ki_c or kd != kd_c)

        config = ExperimentConfig(
            joint_name=joint_name,
            joint_group=joint_group,
            kp=kp_c, ki=ki_c, kd=kd_c,
            setpoint_rad=setpoint_rad,
            duration_s=duration_s,
        )
        result = await self.runner.run(config)

        self.history.save(joint_name, {
            "experiment_id": result.experiment_id,
            "timestamp": result.timestamp,
            "kp": kp_c, "ki": ki_c, "kd": kd_c,
            "score": result.metrics.score,
            "grade": result.metrics.grade,
            "overshoot_pct": result.metrics.overshoot_pct,
            "rise_time_s": result.metrics.rise_time_s,
            "settling_time_s": result.metrics.settling_time_s,
            "sse_pct": result.metrics.sse_pct,
            "oscillation_count": result.metrics.oscillation_count,
            "diagnosis": result.metrics.diagnosis,
            "reasoning": args.get("reasoning", ""),
        })

        content = {
            "params_used": {"kp": kp_c, "ki": ki_c, "kd": kd_c},
            "clamped_to_bounds": was_clamped,
            "score": result.metrics.score,
            "grade": result.metrics.grade,
            "metrics": {
                "overshoot_pct": result.metrics.overshoot_pct,
                "rise_time_s": result.metrics.rise_time_s,
                "settling_time_s": result.metrics.settling_time_s,
                "sse_pct": result.metrics.sse_pct,
                "oscillation_count": result.metrics.oscillation_count,
                "peak_torque_nm": result.metrics.peak_torque_nm,
            },
            "diagnosis": result.metrics.diagnosis,
        }
        if result.safety_aborted:
            content["safety_aborted"] = True
            content["abort_reason"] = result.abort_reason

        return (
            json.dumps(content, ensure_ascii=False),
            result.metrics.score,
            {"kp": kp_c, "ki": ki_c, "kd": kd_c},
        )
