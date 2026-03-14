"""
诊断编排器 v0.4
基于新的 EventLog + SchemaLoader 架构重写。

职责：
  - 接收用户自然语言查询
  - 从 EventLog 取活跃告警 + 最近事件作为上下文
  - 检索 Skill 知识库（YAML + SKILL.md）
  - 调用 LLM 生成诊断回复
  - LLM 不可用时降级为基于规则的回复
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from ..llm import LLMClient

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 Manastone Diagnostic，Unitree G1 人形机器人的专属运维诊断助手。

你的工作方式：
1. 优先参考【活跃告警】中已检测到的异常，这是基于传感器实时数据的客观事实
2. 结合用户描述，从【故障知识库】中找到最相关的故障条目
3. 给出有依据的诊断和分步骤的处理建议

回答格式：
- 用中文回答，简洁直接
- 先说判断（1-2句），再说操作建议
- 如果活跃告警与用户描述吻合，主动指出
- 不确定时说明，不编造数据

可用工具提示：
- 工程师可以用 joint_history(关节名) 查看某关节的完整事件历史
- 工程师可以用 component_status() 查看当前所有关节实时数据
"""

# 关键词 → Skill 文件 ID 映射
_SKILL_KEYWORDS: dict[str, list[str]] = {
    "joint-overheat":      ["热", "温", "烫", "过热", "temperature", "散热", "发烫"],
    "gait-instability":    ["走", "步态", "偏", "不稳", "倒", "gait", "walk", "摔", "平衡"],
    "communication-fault": ["通信", "连接", "dds", "话题", "延迟", "心跳", "掉线", "断开", "lost"],
    "power-system":        ["电", "电池", "充电", "power", "电压", "断电", "欠压", "过压", "soc"],
    "sensor-calibration":  ["传感器", "imu", "摄像头", "相机", "激光", "标定", "漂移", "陀螺"],
}


class DiagnosticOrchestrator:
    def __init__(self, llm: LLMClient, knowledge_dir: str, skills_dir: str | None = None):
        self.llm = llm
        if skills_dir is None:
            skills_dir = os.path.join(knowledge_dir, "skills")
        self.yaml_skills = self._load_yaml_skills(knowledge_dir)
        self.skill_files = self._load_skill_files(skills_dir)
        logger.info("Orchestrator ready: %d YAML faults, %d skill docs",
                    len(self.yaml_skills), len(self.skill_files))

    # ── 知识库加载 ────────────────────────────────────────────

    def _load_yaml_skills(self, knowledge_dir: str) -> list[dict]:
        path = Path(knowledge_dir) / "fault_library.yaml"
        if not path.exists():
            logger.warning("fault_library.yaml not found: %s", path)
            return []
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f).get("faults", [])

    def _load_skill_files(self, skills_dir: str) -> list[dict]:
        result = []
        p = Path(skills_dir)
        if not p.exists():
            return result
        for skill_dir in sorted(p.iterdir()):
            sf = skill_dir / "SKILL.md"
            if not sf.exists():
                continue
            content = sf.read_text(encoding="utf-8")
            meta = {"id": skill_dir.name, "name": skill_dir.name, "category": ""}
            for line in content.splitlines()[:25]:
                if "**id**:" in line:     meta["id"]       = line.split(":", 1)[1].strip()
                elif "**name**:" in line:  meta["name"]     = line.split(":", 1)[1].strip()
                elif "**category**:" in line: meta["category"] = line.split(":", 1)[1].strip()
            result.append({**meta, "content": content,
                           "excerpt": self._extract_excerpt(content, 2000)})
        return result

    @staticmethod
    def _extract_excerpt(content: str, max_chars: int) -> str:
        lines, in_code = [], False
        for line in content.splitlines():
            if line.startswith("```"):
                in_code = not in_code; continue
            if in_code: continue
            if re.match(r'^[│┌└╔╗╚╝═─┬┴├┤┼╠╣╦╩╪╬▲▼\s]+$', line): continue
            lines.append(line)
        text = re.sub(r'\n{3,}', '\n\n', "\n".join(lines))
        return text[:max_chars]

    # ── 检索 ──────────────────────────────────────────────────

    def _find_yaml_skills(self, query: str, context_text: str) -> list[dict]:
        combined = (query + " " + context_text).lower()
        scored = []
        for skill in self.yaml_skills:
            score = 0
            for w in skill.get("name", "").split():
                if w in combined: score += 3
            for sym in skill.get("symptoms", []):
                for w in sym.split():
                    if len(w) >= 2 and w in combined: score += 2
            # 硬触发词
            kw_map = {
                "FK-001": ["编码", "通信", "encoder", "lost"],
                "FK-002": ["过流", "overcurrent", "堵转"],
                "FK-003": ["热", "温", "烫", "temperature"],
                "FK-004": ["lidar", "点云", "激光"],
                "FK-005": ["realsense", "相机", "深度"],
                "FK-006": ["imu", "漂移", "陀螺"],
                "FK-007": ["跟踪", "位置误差", "抖动"],
                "FK-008": ["灵巧手", "hand", "手指"],
            }
            for fid, kws in kw_map.items():
                if skill.get("id") == fid and any(k in combined for k in kws):
                    score += 5
            if score > 0:
                scored.append((score, skill))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:3]]

    def _find_skill_files(self, query: str, context_text: str) -> list[dict]:
        combined = (query + " " + context_text).lower()
        scored = []
        for skill in self.skill_files:
            score = sum(3 for kw in _SKILL_KEYWORDS.get(skill["id"], []) if kw in combined)
            score += sum(2 for w in skill.get("name","").split() if len(w) >= 2 and w in combined)
            if score > 0:
                scored.append((score, skill))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:2]]

    # ── 格式化 ────────────────────────────────────────────────

    def _fmt_active_warnings(self, warnings: list[dict]) -> str:
        if not warnings:
            return "（当前无活跃告警）"
        lines = []
        for w in warnings[:10]:
            lines.append(
                f"  [{w.get('severity','?')}] {w.get('event_type','')} | "
                f"{w.get('component_name', w.get('component_id','?'))} | "
                f"值={w.get('value','?')}{w.get('unit','')}"
            )
        return "\n".join(lines)

    def _fmt_yaml_skills(self, skills: list[dict]) -> str:
        if not skills:
            return "（未找到相关故障知识）"
        lines = []
        for sk in skills:
            lines += [
                f"## [{sk['id']}] {sk['name']} (严重度: {sk['severity']})",
                f"根因: {sk.get('root_cause_explanation','').strip()[:200]}",
                "可能原因: " + "；".join(sk.get("possible_causes", [])[:3]),
                "立即处理: " + "；".join((sk.get("repair_guide",{}).get("immediate",[]))[:2]),
                "",
            ]
        return "\n".join(lines)

    # ── 主入口 ────────────────────────────────────────────────

    async def handle_query(
        self,
        user_message: str,
        context: dict[str, Any],
    ) -> str:
        """
        诊断主流程。

        context 期望包含：
          active_warnings: list[dict]  — 来自 EventLog.get_active_warnings()
          joint_snapshot:  dict | None — 来自 DDSBridge.get_topic_data()
          event_stats:     dict        — 来自 EventLog.stats()
        """
        active_warnings = context.get("active_warnings", [])
        event_stats     = context.get("event_stats", {})

        # 构建上下文文本供知识检索
        context_text = " ".join(
            f"{w.get('event_type','')} {w.get('component_name','')}"
            for w in active_warnings
        )

        # 检索知识库
        yaml_skills = self._find_yaml_skills(user_message, context_text)
        skill_files = self._find_skill_files(user_message, context_text)

        # 构建 prompt
        prompt = f"""用户问题：{user_message}

【活跃告警（基于实时传感器，客观事实）】
{self._fmt_active_warnings(active_warnings)}

事件统计：总计 {event_stats.get('total_events',0)} 条事件，
当前活跃告警 {event_stats.get('active_warnings',0)} 个

【故障知识库】
{self._fmt_yaml_skills(yaml_skills)}"""

        if skill_files:
            excerpts = "\n\n".join(
                f"### {sf['name']}\n{sf['excerpt']}" for sf in skill_files
            )
            prompt += f"\n\n【运维手册参考】\n{excerpts}"

        # 调用 LLM
        try:
            return await self.llm.chat(prompt, system_prompt=SYSTEM_PROMPT)
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            return self._fallback(active_warnings, yaml_skills)

    def _fallback(self, warnings: list[dict], skills: list[dict]) -> str:
        lines = ["**（LLM 不可用，基于规则响应）**", ""]
        if warnings:
            lines.append("**当前活跃告警：**")
            for w in warnings:
                lines.append(f"- [{w.get('severity')}] {w.get('component_name','?')}: "
                             f"{w.get('event_type','')} 值={w.get('value','?')}{w.get('unit','')}")
        else:
            lines.append("当前无活跃告警。")
        if skills:
            lines += ["", "**相关故障知识：**"]
            for sk in skills:
                lines.append(f"- [{sk['id']}] {sk['name']}")
                for step in sk.get("repair_guide", {}).get("immediate", [])[:2]:
                    lines.append(f"  → {step}")
        return "\n".join(lines)
