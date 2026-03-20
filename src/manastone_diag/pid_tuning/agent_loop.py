"""
PID 调参 Agent 循环 — 忠实复现 Karpathy autoresearch 架构

═══════════════════════════════════════════════════════════════
autoresearch 的真实架构（karpathy/autoresearch）：

  文件角色：
    train.py    → params.yaml    （agent 每轮修改这个文件）
    program.md  → program.md     （人写的研究方向，不变）
    results.tsv → results.tsv   （实验日志，追加写入）

  核心循环（无限循环，直到人中断）：

    while True:
        # 1. LLM 读文件，生成新的 params.yaml 全文
        new_params_text = llm(
            program.md +          # 人的研究指令
            current params.yaml + # 当前参数（含上一次的假设注释）
            results.tsv[-15:]     # 最近实验历史
        )

        # 2. Python 写文件 + git commit
        write(params.yaml, new_params_text)
        commit_hash = git_commit("exp_{n}: hypothesis...")

        # 3. Python 跑实验，评分
        score = run_experiment(parse_params(params.yaml))

        # 4. keep or discard
        if score > best_score:
            best_score = score
            results.tsv.append(commit_hash, score, "keep")
            save_best_params()
        else:
            git_reset_params()    # 撤销 params.yaml 到上一版本
            results.tsv.append(commit_hash, score, "discard")

        # 直到人中断 OR max_experiments 安全网触发

═══════════════════════════════════════════════════════════════
与之前版本的本质区别：

  旧版（tool_calls 传参数）：
    LLM 通过 tool_call(kp=X, ki=Y, kd=Z) 传数字
    Python 用这些数字跑实验
    → LLM 的推理过程隐藏在 arguments 里

  新版（文件编辑）：
    LLM 输出完整的 params.yaml 文本（含假设注释）
    Python 解析文件，跑实验，git commit/reset
    → LLM 的推理过程可见于文件注释和 git 历史
    → git log 就是研究日志（和 autoresearch 完全一致）
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .experiment import ExperimentConfig, ExperimentRunner
from .safety import SafetyGuard
from .workspace import PIDWorkspace

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
你是一位 PID 调参研究员。你的工作方式与 Karpathy autoresearch 完全一致：
  - 读取 program.md（研究任务）
  - 读取 params.yaml（当前参数，含上次实验的假设注释）
  - 读取 results.tsv（历史实验记录）
  - 输出：修改后的 params.yaml 全文

输出格式要求：
  - 直接输出 YAML 文本，不要加 markdown 代码块（```yaml 等）
  - 在文件顶部的 hypothesis 字段填写本次调整的工程依据
  - 保持文件结构不变，只修改 hypothesis 和 pid 下的数值
  - 参数必须在 safety_bounds 范围内

示例输出（直接输出这样的 YAML）：
  # PID 调参参数文件 — 由 AI Agent 自动修改
  # 假设（Hypothesis）：
  #   上次超调18%，Kd从3.0增至5.0以增加阻尼，同时Kp略降至18避免过激
  ...（其余字段不变）...
  pid:
    kp: 18.0
    ki: 0.3
    kd: 5.0
  ...

控制理论提示：
  超调 > 20%  → 减 Kp 或 增 Kd
  上升慢       → 增 Kp
  稳态误差 > 3% → 增 Ki（每次 +0.05~0.1）
  持续振荡     → 大幅减 Kp（×0.6），增 Kd，Ki 归零
"""

USER_PROMPT_TEMPLATE = """\
## program.md（研究任务）
{program_md}

---
## 当前 params.yaml（你需要修改这个文件）
{params_yaml}

---
## results.tsv（最近实验历史，score 越高越好，status=keep 表示有效改善）
{results_tsv}

---
## 上一次实验结果
{last_result}

请输出修改后的 params.yaml 全文。记住：不达目标不停止，持续实验。
"""


@dataclass
class AgentLoopResult:
    joint_name: str
    total_experiments: int
    elapsed_s: float
    best_score: float
    best_params: Dict[str, float]
    target_reached: bool
    stopped_by: str        # "target_reached" | "max_experiments" | "llm_error"
    workspace_dir: str
    experiment_log: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "joint_name": self.joint_name,
            "total_experiments": self.total_experiments,
            "elapsed_s": round(self.elapsed_s, 1),
            "best_score": round(self.best_score, 1),
            "best_params": self.best_params,
            "target_reached": self.target_reached,
            "stopped_by": self.stopped_by,
            "workspace_dir": self.workspace_dir,
            "experiment_log": self.experiment_log[-20:],  # 最近20条
        }


class PIDAgentLoop:
    """
    autoresearch 风格的 PID 调参 Agent 循环。

    LLM 的角色：读取文件 → 输出新的 params.yaml 全文
    Python 的角色：写文件 → git commit → 跑实验 → keep/discard → 追加日志
    """

    def __init__(
        self,
        llm_client: Any,
        runner: ExperimentRunner,
        safety: SafetyGuard,
        storage_dir: Any,
    ):
        self.llm = llm_client
        self.runner = runner
        self.safety = safety
        self.storage_dir = storage_dir

    async def run(
        self,
        joint_name: str,
        joint_group: str,
        target_score: float,
        max_experiments: int,      # 安全网，替代 max_turns
        bounds: Any,               # PIDSafetyBounds
        initial_kp: float = 10.0,
        initial_ki: float = 0.1,
        initial_kd: float = 2.0,
        setpoint_rad: float = 0.5,
        experiment_duration_s: float = 2.0,
    ) -> AgentLoopResult:
        start_time = time.time()
        workspace = PIDWorkspace(self.storage_dir, joint_name)
        experiment_log: List[Dict[str, Any]] = []
        best_score = 0.0
        best_params: Dict[str, float] = {}
        stopped_by = "max_experiments"

        # 初始化工作区文件
        workspace.initialize(
            initial_kp=initial_kp,
            initial_ki=initial_ki,
            initial_kd=initial_kd,
            setpoint_rad=setpoint_rad,
            duration_s=experiment_duration_s,
            target_score=target_score,
            bounds=bounds,
        )

        last_result_text = "（首次实验，无历史）"

        # ════════════════════════════════════════════════════
        # 主循环：autoresearch 风格，无限循环直到达标或安全网触发
        # ════════════════════════════════════════════════════
        while workspace.exp_count < max_experiments:
            exp_n = workspace.exp_count + 1
            log_entry: Dict[str, Any] = {"exp": exp_n}

            # ── Step 1: LLM 读文件，输出新的 params.yaml ─────
            prompt = USER_PROMPT_TEMPLATE.format(
                program_md=workspace.read_program(),
                params_yaml=workspace.read_params_text(),
                results_tsv=workspace.read_results_tail(15),
                last_result=last_result_text,
            )

            try:
                new_params_text = await self.llm.chat(
                    user_message=prompt,
                    system_prompt=SYSTEM_PROMPT,
                )
                # 清理 LLM 可能加的 markdown 代码块
                new_params_text = _strip_markdown_fences(new_params_text)
            except Exception as e:
                logger.error("LLM 调用失败（exp=%d）: %s", exp_n, e)
                stopped_by = "llm_error"
                break

            # ── Step 2: 写入文件 ──────────────────────────────
            if not workspace.write_new_params(new_params_text):
                logger.warning("exp=%d: LLM 输出的 YAML 无效，跳过本轮", exp_n)
                last_result_text = f"exp {exp_n}: 你上次输出的 YAML 格式无效，请重新输出合法的 YAML。"
                continue

            kp, ki, kd, hypothesis = workspace.extract_pid_from_params()

            # 安全边界钳制（和之前一样）
            kp = max(bounds.kp_min, min(bounds.kp_max, kp))
            ki = max(bounds.ki_min, min(bounds.ki_max, ki))
            kd = max(bounds.kd_min, min(bounds.kd_max, kd))

            log_entry.update({"kp": kp, "ki": ki, "kd": kd, "hypothesis": hypothesis})

            # ── Step 3: git commit（每次实验前提交，形成研究日志）──
            commit_msg = (
                f"exp_{exp_n:04d}: Kp={kp:.2f} Ki={ki:.3f} Kd={kd:.2f} | "
                f"{hypothesis[:60]}"
            )
            commit_hash = workspace.git_commit(commit_msg)
            log_entry["commit"] = commit_hash

            # ── Step 4: 跑实验 ────────────────────────────────
            config = ExperimentConfig(
                joint_name=joint_name,
                joint_group=joint_group,
                kp=kp, ki=ki, kd=kd,
                setpoint_rad=setpoint_rad,
                duration_s=experiment_duration_s,
            )
            result = await self.runner.run(config)
            score = result.metrics.score
            log_entry["score"] = score
            log_entry["grade"] = result.metrics.grade

            # ── Step 5: keep 或 discard ───────────────────────
            if score > best_score:
                # 改善 → 保留
                best_score = score
                best_params = {"kp": kp, "ki": ki, "kd": kd}
                workspace.save_best(kp, ki, kd, score)
                status = "keep"
                log_entry["status"] = "keep"
                logger.info(
                    "exp=%d keep: Kp=%.2f Ki=%.3f Kd=%.2f score=%.1f (+%.1f)",
                    exp_n, kp, ki, kd, score, score - (best_score - score + score)
                )
            else:
                # 未改善 → git revert params.yaml 回上一版本
                workspace.git_revert_params()
                status = "discard"
                log_entry["status"] = "discard"
                logger.info(
                    "exp=%d discard: score=%.1f < best=%.1f",
                    exp_n, score, best_score,
                )

            # ── Step 6: 追加 results.tsv ─────────────────────
            workspace.log_result(
                commit_hash=commit_hash,
                kp=kp, ki=ki, kd=kd,
                score=score, grade=result.metrics.grade,
                overshoot_pct=result.metrics.overshoot_pct,
                rise_time_s=result.metrics.rise_time_s,
                settling_time_s=result.metrics.settling_time_s,
                sse_pct=result.metrics.sse_pct,
                status=status,
                hypothesis=hypothesis,
            )

            experiment_log.append(log_entry)

            # 构造给 LLM 下一轮看的结果描述
            last_result_text = (
                f"exp {exp_n}: Kp={kp:.2f} Ki={ki:.3f} Kd={kd:.2f} "
                f"→ score={score:.1f} ({result.metrics.grade}) | status={status}\n"
                f"  超调={result.metrics.overshoot_pct:.1f}% "
                f"上升={result.metrics.rise_time_s:.3f}s "
                f"调节={result.metrics.settling_time_s:.3f}s "
                f"稳态误差={result.metrics.sse_pct:.2f}%\n"
                f"  诊断：{'; '.join(result.metrics.diagnosis)}"
            )
            if status == "discard":
                last_result_text += f"\n  ⚠ 未改善（当前最优 {best_score:.1f}），已回滚参数，请换方向。"
            else:
                last_result_text += f"\n  ✓ 改善！当前最优 {best_score:.1f}。"

            # ── 检查停止条件 ──────────────────────────────────
            if best_score >= target_score:
                stopped_by = "target_reached"
                logger.info("达到目标分数 %.1f >= %.1f，停止", best_score, target_score)
                break

        return AgentLoopResult(
            joint_name=joint_name,
            total_experiments=workspace.exp_count,
            elapsed_s=time.time() - start_time,
            best_score=best_score,
            best_params=best_params,
            target_reached=best_score >= target_score,
            stopped_by=stopped_by,
            workspace_dir=str(workspace.workspace_dir),
            experiment_log=experiment_log,
        )


def _strip_markdown_fences(text: str) -> str:
    """去掉 LLM 可能包裹的 ```yaml ... ``` 代码块"""
    lines = text.strip().splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)
