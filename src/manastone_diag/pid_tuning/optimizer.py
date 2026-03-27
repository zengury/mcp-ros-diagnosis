"""
PID 搜索策略辅助器

职责：
  1. 管理实验历史（JSON 持久化）
  2. 为 LLM 构建结构化的"当前状态 + 历史记录"提示词
  3. 解析 LLM 返回的 JSON 参数建议
  4. 在 LLM 不可用时提供规则兜底策略

搜索逻辑与 AutoResearch 闭环类比：
  [历史] → [LLM 分析] → [新假设 Kp/Ki/Kd] → [实验] → [评分] → [回到历史]

LLM Prompt 设计原则：
  - 提供完整历史（分数 + 指标 + 波形诊断文字）
  - 要求 LLM 给出调整方向的"工程依据"
  - 强制 JSON 输出格式（kp, ki, kd + reasoning）
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一位精通经典控制理论的 PID 调参专家。
你将基于历史实验数据，为指定关节推荐下一组 Kp/Ki/Kd 参数。

调参原则（供你参考）：
- Kp 增大 → 响应加快，但超调增大
- Ki 增大 → 消除稳态误差，但可能引起振荡
- Kd 增大 → 抑制超调和振荡，但放大高频噪声
- 常见策略：先整定 Kp（让系统响应），再加 Kd（抑制超调），最后微调 Ki（消稳态误差）

你必须以严格 JSON 格式回复，不要输出任何其他内容：
{
  "kp": <float>,
  "ki": <float>,
  "kd": <float>,
  "reasoning": "<不超过100字的调整依据>",
  "expected_improvement": "<预期改善方向>"
}"""


class TuningHistory:
    """
    调参历史管理器。
    每个关节独立一个 JSON 文件，存储在 storage/pid_history/ 目录下。
    """

    def __init__(self, storage_dir: Path):
        self.history_dir = storage_dir / "pid_history"
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, joint_name: str) -> Path:
        safe_name = joint_name.replace("/", "_")
        return self.history_dir / f"{safe_name}.json"

    def load(self, joint_name: str) -> List[Dict[str, Any]]:
        """加载关节的完整调参历史"""
        p = self._path(joint_name)
        if not p.exists():
            return []
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("无法读取历史文件 %s: %s", p, e)
            return []

    def save(self, joint_name: str, entry: Dict[str, Any]) -> None:
        """追加一条实验记录"""
        history = self.load(joint_name)
        history.append(entry)
        p = self._path(joint_name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    def best(self, joint_name: str) -> Optional[Dict[str, Any]]:
        """返回历史中得分最高的实验"""
        history = self.load(joint_name)
        if not history:
            return None
        return max(history, key=lambda x: x.get("score", 0))

    def recent(self, joint_name: str, n: int = 10) -> List[Dict[str, Any]]:
        """返回最近 n 条记录"""
        return self.load(joint_name)[-n:]

    def clear(self, joint_name: str) -> int:
        """清空某关节的历史，返回删除条数"""
        history = self.load(joint_name)
        p = self._path(joint_name)
        if p.exists():
            p.unlink()
        return len(history)


class PIDOptimizer:
    """
    PID 参数搜索策略，结合 LLM 分析和规则兜底。
    """

    def __init__(
        self,
        history: TuningHistory,
        llm_client: Optional[Any] = None,  # LLMClient
    ):
        self.history = history
        self.llm = llm_client

    def build_llm_prompt(
        self,
        joint_name: str,
        joint_group: str,
        bounds_info: Dict[str, Any],
        current_params: Dict[str, float],
        current_score: float,
        recent_history: List[Dict[str, Any]],
    ) -> str:
        """构建发给 LLM 的用户提示词（包含完整上下文）"""
        history_text = ""
        for i, h in enumerate(recent_history[-8:], 1):  # 最多提供最近 8 次
            history_text += (
                f"\n  [{i}] Kp={h.get('kp', '?'):.2f} Ki={h.get('ki', '?'):.3f} "
                f"Kd={h.get('kd', '?'):.2f} → 得分={h.get('score', '?'):.1f} "
                f"超调={h.get('overshoot_pct', '?'):.1f}% "
                f"上升={h.get('rise_time_s', '?'):.3f}s "
                f"调节={h.get('settling_time_s', '?'):.3f}s "
                f"稳态误差={h.get('sse_pct', '?'):.2f}%"
            )
            if h.get("diagnosis"):
                history_text += f"\n       诊断：{h['diagnosis'][0]}"

        best = self.history.best(joint_name)
        best_text = (
            f"Kp={best.get('kp', '?'):.2f} Ki={best.get('ki', '?'):.3f} "
            f"Kd={best.get('kd', '?'):.2f} (得分={best.get('score', '?'):.1f})"
            if best else "尚无历史最优"
        )

        return f"""关节：{joint_name}（组别：{joint_group}）
当前参数：Kp={current_params['kp']:.2f}  Ki={current_params['ki']:.3f}  Kd={current_params['kd']:.2f}
当前得分：{current_score:.1f}/100

安全边界：
  Kp ∈ [{bounds_info['kp_min']}, {bounds_info['kp_max']}]
  Ki ∈ [{bounds_info['ki_min']}, {bounds_info['ki_max']}]
  Kd ∈ [{bounds_info['kd_min']}, {bounds_info['kd_max']}]

历史最优：{best_text}

最近实验记录（从早到晚）：{history_text or '（无历史）'}

请根据以上信息，提出下一组 Kp/Ki/Kd 参数以提高得分。"""

    async def propose_next(
        self,
        joint_name: str,
        joint_group: str,
        current_params: Dict[str, float],
        current_score: float,
        bounds: Any,  # PIDSafetyBounds
    ) -> Tuple[Dict[str, float], str]:
        """
        建议下一组参数。优先使用 LLM，回退到规则策略。

        Returns:
            ({"kp": float, "ki": float, "kd": float}, reasoning_text)
        """
        recent = self.history.recent(joint_name, 8)
        bounds_info = {
            "kp_min": bounds.kp_min, "kp_max": bounds.kp_max,
            "ki_min": bounds.ki_min, "ki_max": bounds.ki_max,
            "kd_min": bounds.kd_min, "kd_max": bounds.kd_max,
        }

        # ── 尝试 LLM ────────────────────────────────────────────
        if self.llm and self.llm.is_available():
            try:
                prompt = self.build_llm_prompt(
                    joint_name, joint_group, bounds_info,
                    current_params, current_score, recent,
                )
                response = await self.llm.chat(
                    user_message=prompt,
                    system_prompt=SYSTEM_PROMPT,
                )
                params, reasoning = self._parse_llm_response(response, bounds)
                if params:
                    return params, reasoning
            except Exception as e:
                logger.warning("LLM 调参建议失败，回退到规则策略: %s", e)

        # ── 规则兜底策略 ─────────────────────────────────────────
        return self._rule_based_next(
            current_params, current_score, recent, bounds
        )

    def _parse_llm_response(
        self, response: str, bounds: Any
    ) -> Tuple[Optional[Dict[str, float]], str]:
        """解析 LLM 返回的 JSON，并校验参数在安全边界内"""
        # 提取 JSON（LLM 有时会在 JSON 前后加多余文字）
        match = re.search(r'\{[^{}]+\}', response, re.DOTALL)
        if not match:
            logger.warning("LLM 响应无法提取 JSON: %s", response[:200])
            return None, ""

        try:
            data = json.loads(match.group())
            kp = float(data["kp"])
            ki = float(data["ki"])
            kd = float(data["kd"])
            reasoning = data.get("reasoning", "") + " " + data.get("expected_improvement", "")
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            logger.warning("LLM JSON 解析失败: %s", e)
            return None, ""

        # 安全边界校验（LLM 有时会忽略约束）
        kp = max(bounds.kp_min, min(bounds.kp_max, kp))
        ki = max(bounds.ki_min, min(bounds.ki_max, ki))
        kd = max(bounds.kd_min, min(bounds.kd_max, kd))

        return {"kp": round(kp, 3), "ki": round(ki, 4), "kd": round(kd, 3)}, reasoning.strip()

    def _rule_based_next(
        self,
        current: Dict[str, float],
        current_score: float,
        history: List[Dict[str, Any]],
        bounds: Any,
    ) -> Tuple[Dict[str, float], str]:
        """
        经典调参规则（Ziegler-Nichols 启发 + 历史感知）：
          - 若无历史：使用临界比例法初始猜测
          - 若超调 > 15%：增 Kd，减 Kp
          - 若上升慢（> 0.8s）：增 Kp
          - 若稳态误差 > 2%：增 Ki
          - 若振荡严重（> 8次）：减 Kp，增 Kd
        """
        kp = current["kp"]
        ki = current["ki"]
        kd = current["kd"]
        reason = ""

        if not history:
            # 首次实验：小幅探索初始点
            kp = (bounds.kp_min + bounds.kp_max) * 0.2
            ki = 0.0
            kd = kp * 0.1
            reason = "首次实验，使用保守初始值（P控制为主）"
        else:
            last = history[-1]
            overshoot = last.get("overshoot_pct", 0)
            rise_time = last.get("rise_time_s", 1.0)
            sse = last.get("sse_pct", 0)
            osc = last.get("oscillation_count", 0)

            if osc > 8:
                kp = kp * 0.7
                kd = min(bounds.kd_max, kd * 1.3)
                reason = f"振荡严重（{osc}次），降 Kp+升 Kd"
            elif overshoot > 15:
                kp = kp * 0.85
                kd = min(bounds.kd_max, kd * 1.25)
                reason = f"超调 {overshoot:.1f}%，降 Kp、升 Kd"
            elif rise_time > 0.8:
                kp = min(bounds.kp_max, kp * 1.3)
                reason = f"响应慢（{rise_time:.2f}s），升 Kp"
            elif sse > 2.0:
                ki = min(bounds.ki_max, ki + 0.05)
                reason = f"稳态误差 {sse:.1f}%，升 Ki"
            else:
                # 随机微扰，跳出局部最优
                kp = kp * random.uniform(0.9, 1.1)
                ki = ki * random.uniform(0.9, 1.1)
                kd = kd * random.uniform(0.9, 1.1)
                reason = "当前性能良好，小幅随机扰动继续探索"

        kp = round(max(bounds.kp_min, min(bounds.kp_max, kp)), 3)
        ki = round(max(bounds.ki_min, min(bounds.ki_max, ki)), 4)
        kd = round(max(bounds.kd_min, min(bounds.kd_max, kd)), 3)

        return {"kp": kp, "ki": ki, "kd": kd}, f"[规则策略] {reason}"
