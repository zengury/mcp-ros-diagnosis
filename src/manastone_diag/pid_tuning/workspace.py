"""
PID 调参工作区管理器

等价于 autoresearch 中的文件 + git 管理层：
  train.py     → params.yaml   （agent 每轮修改这个文件）
  program.md   → program.md    （人写的研究方向，不变）
  results.tsv  → results.tsv   （实验日志，追加写入）

git 作为研究日志：
  改善 → git commit params.yaml（提交哈希 = 实验 ID）
  未改善 → git reset --hard HEAD~1（撤销，文件回到上一版本）
"""
from __future__ import annotations

import csv
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# params.yaml 的模板——LLM 读懂格式后按此修改
PARAMS_TEMPLATE = """\
# PID 调参参数文件 — 由 AI Agent 自动修改
# 格式：每次修改时在顶部写明假设（hypothesis），然后更新数值
#
# 假设（Hypothesis）：
#   {hypothesis}
#
# 实验编号：{exp_num}
# 上一次得分：{prev_score}

joint: {joint_name}

pid:
  kp: {kp}    # 比例增益
  ki: {ki}    # 积分增益
  kd: {kd}    # 微分增益

experiment:
  setpoint_rad: {setpoint_rad}
  duration_s: {duration_s}

safety_bounds:
  kp_range: [{kp_min}, {kp_max}]
  ki_range: [{ki_min}, {ki_max}]
  kd_range: [{kd_min}, {kd_max}]
"""

PROGRAM_MD_TEMPLATE = """\
# PID 调参研究程序

## 任务
为机器人关节 `{joint_name}` 整定 PID 参数，使综合评分 ≥ {target_score}/100。

## 评分标准（0-100 分）
- 超调量 (Overshoot)：超出目标位置的百分比，越低越好
- 上升时间 (Rise Time)：从静止到到达目标的时间，越快越好
- 调节时间 (Settling Time)：系统稳定所需时间，越短越好
- 稳态误差 (SSE)：最终位置与目标的偏差，越小越好

## 调参规则
1. **只修改 `params.yaml`**，不修改其他文件
2. 每次修改时，在 `hypothesis` 字段写明调整依据（必填）
3. 参数必须在 safety_bounds 范围内，否则会被自动钳制
4. 评分提升则保留修改（git keep），否则自动回滚（git discard）

## 控制理论指导
- 超调 > 20%  → 减 Kp 或 增 Kd
- 上升慢       → 增 Kp
- 稳态误差大   → 增 Ki（每次 +0.05~0.1）
- 持续振荡     → 大幅减 Kp，增 Kd，Ki 暂时归零

## 运行规则
- 持续实验直到达到目标分数，或人类中断
- 每次修改幅度：得分低时大幅调（±30~50%），接近目标时小幅精调（±5~10%）
- 查看 `results.tsv` 了解历史趋势，避免重复踩坑
"""


@dataclass
class ExperimentRecord:
    exp_num: int
    commit_hash: str
    kp: float
    ki: float
    kd: float
    score: float
    grade: str
    overshoot_pct: float
    rise_time_s: float
    settling_time_s: float
    sse_pct: float
    status: str       # "keep" | "discard" | "crash"
    hypothesis: str
    timestamp: float


class PIDWorkspace:
    """
    autoresearch 风格的 PID 调参工作区。

    目录结构（per-joint）：
      storage/pid_workspace/{joint_name}/
        params.yaml     ← agent 读写（等价于 train.py）
        program.md      ← 人写的研究方向（等价于 program.md）
        results.tsv     ← 实验日志（等价于 results.tsv）
        best_params.yaml ← 历史最优快照
    """

    TSV_HEADER = [
        "exp_num", "commit_hash", "kp", "ki", "kd",
        "score", "grade", "overshoot_pct", "rise_time_s",
        "settling_time_s", "sse_pct", "status", "hypothesis"
    ]

    def __init__(self, storage_dir: Path, joint_name: str):
        self.joint_name = joint_name
        self.workspace_dir = storage_dir / "pid_workspace" / joint_name
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        self.params_path = self.workspace_dir / "params.yaml"
        self.program_path = self.workspace_dir / "program.md"
        self.results_path = self.workspace_dir / "results.tsv"
        self.best_path = self.workspace_dir / "best_params.yaml"

        self._exp_counter = self._count_experiments()

    # ── 初始化 ────────────────────────────────────────────────

    def initialize(
        self,
        initial_kp: float,
        initial_ki: float,
        initial_kd: float,
        setpoint_rad: float,
        duration_s: float,
        target_score: float,
        bounds: "PIDSafetyBounds",
    ) -> None:
        """初始化工作区文件（首次或清空后调用）"""
        # 写 params.yaml 初始版本
        self._write_params(
            kp=initial_kp, ki=initial_ki, kd=initial_kd,
            hypothesis="初始参数，开始系统性探索",
            exp_num=0, prev_score=0.0,
            setpoint_rad=setpoint_rad, duration_s=duration_s,
            bounds=bounds,
        )
        # 写 program.md
        if not self.program_path.exists():
            self.program_path.write_text(
                PROGRAM_MD_TEMPLATE.format(
                    joint_name=self.joint_name,
                    target_score=target_score,
                ),
                encoding="utf-8",
            )
        # 初始化 results.tsv
        if not self.results_path.exists():
            with open(self.results_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f, delimiter="\t")
                writer.writerow(self.TSV_HEADER)

    def _count_experiments(self) -> int:
        if not self.results_path.exists():
            return 0
        with open(self.results_path, encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)  # 减掉 header

    # ── 文件读写 ──────────────────────────────────────────────

    def read_params(self) -> dict:
        """读取当前 params.yaml，返回 dict"""
        if not self.params_path.exists():
            return {}
        with open(self.params_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def read_params_text(self) -> str:
        """读取 params.yaml 原始文本（供 LLM 阅读）"""
        if not self.params_path.exists():
            return ""
        return self.params_path.read_text(encoding="utf-8")

    def read_program(self) -> str:
        """读取 program.md 全文"""
        if not self.program_path.exists():
            return ""
        return self.program_path.read_text(encoding="utf-8")

    def read_results_tail(self, n: int = 15) -> str:
        """读取最近 n 条实验记录（TSV 格式，供 LLM 分析趋势）"""
        if not self.results_path.exists():
            return "（无历史记录）"
        with open(self.results_path, encoding="utf-8") as f:
            lines = f.readlines()
        header = lines[0] if lines else ""
        tail = lines[max(1, len(lines) - n):]
        return "".join([header] + tail)

    def write_new_params(self, new_yaml_text: str) -> bool:
        """
        将 LLM 生成的新 params.yaml 写入文件。
        返回 True 表示写入成功并通过基本格式验证。
        """
        try:
            parsed = yaml.safe_load(new_yaml_text)
            if not parsed or "pid" not in parsed:
                logger.warning("LLM 输出的 YAML 格式不正确，缺少 'pid' 字段")
                return False
            self.params_path.write_text(new_yaml_text, encoding="utf-8")
            return True
        except yaml.YAMLError as e:
            logger.warning("LLM 输出无法解析为 YAML: %s", e)
            return False

    def extract_pid_from_params(self) -> tuple[float, float, float, str]:
        """从当前 params.yaml 提取 kp, ki, kd 和 hypothesis"""
        data = self.read_params()
        pid = data.get("pid", {})
        kp = float(pid.get("kp", 1.0))
        ki = float(pid.get("ki", 0.0))
        kd = float(pid.get("kd", 0.0))
        # 从 YAML 注释里提取 hypothesis（从文本里找）
        text = self.read_params_text()
        hypothesis = "未填写"
        for line in text.splitlines():
            if line.strip().startswith("#   ") and "→" not in line and ":" not in line:
                hypothesis = line.strip("# ").strip()
                if hypothesis:
                    break
        return kp, ki, kd, hypothesis

    def _write_params(
        self,
        kp: float, ki: float, kd: float,
        hypothesis: str, exp_num: int, prev_score: float,
        setpoint_rad: float, duration_s: float,
        bounds: "PIDSafetyBounds",
    ) -> None:
        content = PARAMS_TEMPLATE.format(
            hypothesis=hypothesis,
            exp_num=exp_num,
            prev_score=prev_score,
            joint_name=self.joint_name,
            kp=kp, ki=ki, kd=kd,
            setpoint_rad=setpoint_rad,
            duration_s=duration_s,
            kp_min=bounds.kp_min, kp_max=bounds.kp_max,
            ki_min=bounds.ki_min, ki_max=bounds.ki_max,
            kd_min=bounds.kd_min, kd_max=bounds.kd_max,
        )
        self.params_path.write_text(content, encoding="utf-8")

    # ── Git 操作 ──────────────────────────────────────────────

    def _find_repo_root(self) -> Optional[Path]:
        """向上查找 git repo 根目录，找不到返回 None"""
        path = self.workspace_dir
        for _ in range(10):
            if (path / ".git").exists():
                return path
            parent = path.parent
            if parent == path:
                break
            path = parent
        return None

    def git_commit(self, message: str) -> str:
        """提交 params.yaml，返回 commit hash（短）。不在 git repo 时静默跳过。"""
        repo_root = self._find_repo_root()
        if repo_root is None:
            logger.debug("不在 git repo 中，跳过 commit")
            return f"no-git-{int(time.time())}"
        try:
            subprocess.run(
                ["git", "add", str(self.params_path)],
                cwd=repo_root, capture_output=True, check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", message, "--allow-empty"],
                cwd=repo_root, capture_output=True, check=True,
            )
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo_root, capture_output=True, text=True, check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            logger.warning("git commit 失败: %s", e)
            return f"commit-err-{int(time.time())}"

    def git_revert_params(self) -> bool:
        """撤销 params.yaml 到上一次 commit。不在 git repo 时跳过。"""
        repo_root = self._find_repo_root()
        if repo_root is None:
            return False
        try:
            subprocess.run(
                ["git", "checkout", "HEAD~1", "--", str(self.params_path.relative_to(repo_root))],
                cwd=repo_root, capture_output=True, check=True,
            )
            return True
        except subprocess.CalledProcessError:
            try:
                subprocess.run(
                    ["git", "checkout", "HEAD", "--", str(self.params_path.relative_to(repo_root))],
                    cwd=repo_root, capture_output=True, check=True,
                )
            except Exception:
                pass
            return False

    # ── 结果日志 ──────────────────────────────────────────────

    def log_result(
        self,
        commit_hash: str,
        kp: float, ki: float, kd: float,
        score: float, grade: str,
        overshoot_pct: float, rise_time_s: float,
        settling_time_s: float, sse_pct: float,
        status: str,
        hypothesis: str,
    ) -> None:
        """追加一条实验记录到 results.tsv"""
        self._exp_counter += 1
        with open(self.results_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter="\t")
            writer.writerow([
                self._exp_counter, commit_hash,
                round(kp, 3), round(ki, 4), round(kd, 3),
                round(score, 1), grade,
                round(overshoot_pct, 1), round(rise_time_s, 3),
                round(settling_time_s, 3), round(sse_pct, 2),
                status, hypothesis[:80],
            ])

    def save_best(self, kp: float, ki: float, kd: float, score: float) -> None:
        """保存历史最优参数快照"""
        content = (
            f"# 历史最优 PID 参数\n"
            f"# 得分: {score:.1f}/100\n"
            f"# 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"kp: {kp}\nki: {ki}\nkd: {kd}\n"
        )
        self.best_path.write_text(content, encoding="utf-8")

    @property
    def exp_count(self) -> int:
        return self._exp_counter
